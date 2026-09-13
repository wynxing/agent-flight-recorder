"""首次启动时跑一次示例 Agent，让控制台开箱即有数据。"""

from __future__ import annotations

import logging

from agent_flight_recorder.recorder import Recorder
from agent_flight_recorder.registry import load_agent_specs

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
    try:
        run_id = spec.seed(recorder)
        logger.info("seeded run %s from agent %s", run_id, spec.name)
        return run_id
    except Exception as exc:  # noqa: BLE001 - 播种失败不能阻止平台启动
        logger.warning("seed 失败：%s", exc)
        return None
    finally:
        recorder.close(drain_timeout=5.0)

