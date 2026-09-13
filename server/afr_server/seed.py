"""首次启动时跑一次示例 Agent，让控制台开箱即有数据。"""

from __future__ import annotations

import logging

from agent_flight_recorder.recorder import Recorder
from agent_flight_recorder.registry import AgentSpec, load_agent_specs

from .cases import create_case, run_case_blocking
from .db import session_scope
from .storage import list_runs
from .transport import DirectTransport

logger = logging.getLogger(__name__)


def seed_if_empty() -> str | None:
    with session_scope() as session:
        _, total = list_runs(session, limit=1)
    if total:
        return None

    specs = [spec for spec in load_agent_specs().values() if spec.seed is not None]
    if not specs:
        logger.info("没有注册可播种的 Agent，跳过 seed")
        return None

    spec = specs[0]
    recorder = Recorder(
        spec.name,
        model=spec.default_model,
        agent_version=spec.version,
        transport=DirectTransport(),
        flush_interval=0.0,
        labels={"seed": "true"},
    )
    run_id: str | None = None
    try:
        run_id = spec.seed(recorder)
    except Exception as exc:  # noqa: BLE001 - 播种失败不能阻止平台启动
        logger.warning("seed 失败：%s", exc)
    finally:
        # 必须先把缓冲排空：只有 Run 与事件都落了库，自带用例才可能回放它。
        recorder.close(drain_timeout=5.0)

    if run_id is None:
        return None

    logger.info("seeded run %s from agent %s", run_id, spec.name)
    _seed_cases(spec, run_id)
    return run_id


def _seed_cases(spec: AgentSpec, seed_run_id: str) -> None:
    """把 Agent 自带的演示用例建出来，并立刻跑一次。

    "开箱就有一条红色的回归用例"是演示闭环的一部分：用户什么都不用建，
    切一下 Prompt 版本就能看到它变绿。单条用例失败只记日志，不能拖垮播种。
    """

    for template in spec.seed_cases:
        try:
            with session_scope() as session:
                row = create_case(
                    session,
                    name=template.name,
                    source_run_id=seed_run_id,
                    assertions=template.assertions,
                    description=template.description,
                    from_seq=template.from_seq,
                    to_seq=template.to_seq,
                    # 与 seed 运行的 labels 对齐，演示数据在界面上同样可识别。
                    labels={**template.labels, "seed": "true"},
                    preset=template.preset,
                    model=template.model,
                    system_prompt=template.system_prompt,
                )
                case_id = row.id

            run_case_blocking(case_id)
            logger.info("seeded case %s (%s)", case_id, template.name)
        except Exception as exc:  # noqa: BLE001 - 单条用例失败不影响平台启动
            logger.warning("seed case 失败（%s）：%s", template.name, exc)
