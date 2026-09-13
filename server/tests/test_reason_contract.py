"""成因分类的跨层契约：码表、闭集行为与跨语言一致性。

这一层守的是 issue #6 的核心断言：随便挑一个成因，从它产生的地方一路到控制台呈现
给用户的那句话，中间不能有任何一次「这里先写个字符串，以后再统一」。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from agent_flight_recorder.replay.reasons import (
    CAUSE_CODES,
    InconclusiveCode,
    InconclusiveReason,
    most_significant,
)

ROOT = Path(__file__).resolve().parents[2]


def test_code_set_is_the_declared_closed_set() -> None:
    """枚举与 ``CAUSE_CODES`` 必须逐字一致：多一个少一个都说明有人绕过了分类。"""

    assert set(CAUSE_CODES) == {code.value for code in InconclusiveCode}
    assert len(CAUSE_CODES) == len(set(CAUSE_CODES))
    assert "unknown" in CAUSE_CODES


def test_novel_code_falls_to_unknown_and_keeps_the_original_text() -> None:
    """闭集：未知取值必须显式落「未知成因」，并把原文留在 detail。"""

    parsed = InconclusiveReason.from_code("something_nobody_defined", "原文说明")
    assert parsed.code == InconclusiveCode.UNKNOWN.value
    assert parsed.detail == "原文说明"

    # detail 为空时，原始码本身也不能丢。
    assert InconclusiveReason.from_code("brand_new_code").detail == "brand_new_code"


def test_legacy_free_text_is_parsed_without_misclassification() -> None:
    """历史自由文本 reason 的兼容路径：能认码的认码，认不出的保留原文。"""

    # 裸码。
    assert InconclusiveReason.legacy("recording_loss").code == "recording_loss"
    # 码 + 冒号 + 英文长句：拆成 code / detail，散文不再充当码。
    split = InconclusiveReason.legacy("incomplete_recording: missing boundary or event sequence gap")
    assert split.code == "incomplete_recording"
    assert split.detail == "missing boundary or event sequence gap"
    # 服务端历史上自造的第三个名字，收敛回共享分类的同一个码。
    assert InconclusiveReason.legacy("replay_recording_loss").code == "recording_loss"
    # pi 侧的旧码。
    assert InconclusiveReason.legacy("no_recording: step 2").code == "missing_recorded_response"
    assert InconclusiveReason.legacy("model_output_truncated").code == "truncated_context"
    assert InconclusiveReason.legacy("unsupported_context: truncated messages").code == "truncated_context"

    # 认不出的散文：不猜，落 unknown 并原样保留。
    prose = InconclusiveReason.legacy("[afr] 父 Run 中没有与本次调用匹配的 read 录制结果。")
    assert prose.code == "unknown"
    assert prose.detail == "[afr] 父 Run 中没有与本次调用匹配的 read 录制结果。"


def test_structured_and_legacy_forms_round_trip() -> None:
    """同一份成因在「结构」与「文本」两种形态之间来回不会失真。"""

    cause = InconclusiveReason.from_code("recording_loss", "丢事件说明")
    assert InconclusiveReason.from_dict(cause.model_dump()) == cause
    assert InconclusiveReason.from_dict(cause.to_text()) == cause
    # （Pydantic 的等值比较只看字段，因此这里比较的是 code 与 detail。）
    assert InconclusiveReason.from_dict({"reason": cause.to_text()}) == cause


def test_most_significant_picks_the_most_fundamental_cause() -> None:
    """同时成立多个成因时，取更根本的那个（录制不足优先于单步偏离）。"""

    missing = InconclusiveReason.from_code("missing_recorded_response")
    loss = InconclusiveReason.from_code("recording_loss")
    assert most_significant([missing, loss]) is loss
    assert most_significant([]) is None


# ---------------------------------------------------------------- 真实能力表

#: 文档里用来标注「这一侧只做兼容解析、没有产生路径」的固定短语。
NO_PRODUCTION_PATH = "解析兼容，暂无产生路径"


def _capability_table() -> dict[str, dict[str, str]]:
    """从 docs/replay-semantics.md 第 8 节的表里读出「哪一侧声称能产生哪个码」。

    读的是文档本身，而不是在这里抄一份：抄一份只能证明抄对了。
    """

    lines = (ROOT / "docs/replay-semantics.md").read_text(encoding="utf-8").splitlines()
    header = next((i for i, line in enumerate(lines) if line.startswith("| 成因码 |")), None)
    assert header is not None, "docs 第 8 节里没有真实产生路径能力表"

    columns = [cell.strip() for cell in lines[header].strip("|").split("|")]
    assert columns[1] == "Python（SDK / 服务端）真实产生路径", columns
    assert columns[2] == "pi 真实产生路径", columns

    table: dict[str, dict[str, str]] = {}
    for line in lines[header + 1 :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if set(cells[0]) <= {"-", " "}:
            continue  # 表头下的分隔行
        table[cells[0].strip("`")] = {"python": cells[1], "pi": cells[2]}
    return table


#: Python 侧（SDK + 服务端）每个码的真实产生点：文件 + 必须出现的代码片段。
#: 文档说「Python 能产生」的码，必须在这里找到落到源码里的对应物。
PYTHON_PRODUCTION_SITES: dict[str, tuple[str, str]] = {
    "incomplete_recording": (
        "sdk/agent_flight_recorder/replay/engine.py",
        "InconclusiveCode.INCOMPLETE_RECORDING.value",
    ),
    "event_sequence_gap": (
        "sdk/agent_flight_recorder/replay/engine.py",
        "InconclusiveCode.EVENT_SEQUENCE_GAP.value",
    ),
    "redacted_replay_data": (
        "sdk/agent_flight_recorder/replay/engine.py",
        "InconclusiveCode.REDACTED_REPLAY_DATA.value",
    ),
    "recording_loss": (
        "server/afr_server/replay_runner.py",
        "InconclusiveCode.RECORDING_LOSS.value",
    ),
    "truncated_context": (
        "sdk/agent_flight_recorder/replay/engine.py",
        "InconclusiveCode.TRUNCATED_CONTEXT.value",
    ),
    "missing_recorded_response": (
        "sdk/agent_flight_recorder/replay/langgraph_adapter.py",
        "InconclusiveCode.MISSING_RECORDED_RESPONSE.value",
    ),
    "missing_initial_state": (
        "sdk/agent_flight_recorder/replay/engine.py",
        "InconclusiveCode.MISSING_INITIAL_STATE.value",
    ),
    "side_effect_blocked": (
        "server/afr_server/cases.py",
        "InconclusiveCode.SIDE_EFFECT_BLOCKED.value",
    ),
    "unknown": (
        "server/afr_server/cases.py",
        "InconclusiveCode.UNKNOWN.value",
    ),
}

#: pi 侧每个码的真实产生点：文件里必须出现该码的字符串字面量。
PI_PRODUCTION_SITES: dict[str, str] = {
    "incomplete_recording": "integrations/pi/src/core.ts",
    "event_sequence_gap": "integrations/pi/src/core.ts",
    "redacted_replay_data": "integrations/pi/src/core.ts",
    "truncated_context": "integrations/pi/src/runner.ts",
    "missing_recorded_response": "integrations/pi/src/core.ts",
    "model_context_changed": "integrations/pi/src/runner.ts",
    "final_output_changed": "integrations/pi/src/runner.ts",
    "unknown": "integrations/pi/src/runner.ts",
}


def test_documented_production_sites_really_exist() -> None:
    """文档声称某侧能产生某个码，就必须能在那一侧的源码里找到那个产生点。"""

    table = _capability_table()
    assert set(table) == set(CAUSE_CODES), "能力表必须覆盖闭集里的每一个码"

    for code, cells in table.items():
        if NO_PRODUCTION_PATH not in cells["python"]:
            assert code in PYTHON_PRODUCTION_SITES, (
                f"文档说 Python 侧能产生 {code}，但测试里没有登记它的产生点"
            )
            path, snippet = PYTHON_PRODUCTION_SITES[code]
            assert snippet in (ROOT / path).read_text(encoding="utf-8"), (
                f"{code} 的产生点不在文档所写的 {path}"
            )
        else:
            assert code not in PYTHON_PRODUCTION_SITES, (
                f"{code} 标了「暂无产生路径」，却仍然登记着 Python 侧产生点"
            )

        if NO_PRODUCTION_PATH not in cells["pi"]:
            assert code in PI_PRODUCTION_SITES, (
                f"文档说 pi 侧能产生 {code}，但测试里没有登记它的产生点"
            )
            source = (ROOT / PI_PRODUCTION_SITES[code]).read_text(encoding="utf-8")
            assert f"'{code}'" in source or f'"{code}"' in source, (
                f"{code} 的产生点不在文档所写的 {PI_PRODUCTION_SITES[code]}"
            )
        else:
            assert code not in PI_PRODUCTION_SITES, (
                f"{code} 标了「暂无产生路径」，却仍然登记着 pi 侧产生点"
            )


def _production_hits(code: str, *, pi: bool) -> list[str]:
    """在某一侧的生产源码里找这个码的痕迹（字符串字面量或枚举成员）。

    刻意排除定义码的地方（Python 的 reasons.py / pi 的 reasons.ts）：闭集的定义
    本身不是产生路径，把它算进来会让「只做兼容解析」的码看起来像有产生点。
    """

    needles = [f"'{code}'", f'"{code}"']
    if not pi:
        needles.append(code.upper())  # InconclusiveCode 的成员名，如 MODEL_CONTEXT_CHANGED
    roots = (
        [ROOT / "integrations/pi/src"]
        if pi
        else [ROOT / "sdk/agent_flight_recorder", ROOT / "server/afr_server"]
    )
    definitions = {"reasons.ts"} if pi else {"reasons.py"}
    hits: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.ts" if pi else "*.py")):
            if path.name in definitions:
                continue
            text = path.read_text(encoding="utf-8")
            if any(needle in text for needle in needles):
                hits.append(path.relative_to(ROOT).as_posix())
    return hits


def test_codes_marked_as_parse_only_really_have_no_production_path() -> None:
    """「解析兼容，暂无产生路径」是断言而不是备注：源码里必须真的找不到产生点。"""

    table = _capability_table()
    for code, cells in table.items():
        if NO_PRODUCTION_PATH in cells["python"]:
            assert _production_hits(code, pi=False) == [], (
                f"{code} 被标成 Python 侧只做兼容解析，但源码里仍有产生它的痕迹"
            )
        if NO_PRODUCTION_PATH in cells["pi"]:
            assert _production_hits(code, pi=True) == [], (
                f"{code} 被标成 pi 侧只做兼容解析，但源码里仍有产生它的痕迹"
            )


# ---------------------------------------------------------------- 跨语言

def _ts_enum_values() -> list[str]:
    """从 pi 的 TypeScript 联合类型里读出取值，而不是读一份抄写的清单。"""

    source = (ROOT / "integrations/pi/src/reasons.ts").read_text(encoding="utf-8")
    block = re.search(r"export type InconclusiveCode =([^;]+);", source)
    assert block, "pi 的 InconclusiveCode 联合类型没有找到"
    return re.findall(r"'([a-z_]+)'", block.group(1))


def test_typescript_and_python_share_one_code_set() -> None:
    """两套语言的定义必须逐字一致，且装饰的是同一个闭集。"""

    assert _ts_enum_values() == list(CAUSE_CODES)


def test_console_code_table_covers_the_same_set_and_gives_every_code_an_action() -> None:
    """控制台的码表覆盖同一个集合，且每个码都有一句可操作的中文说明。"""

    source = (ROOT / "web/src/utils/format.ts").read_text(encoding="utf-8")
    table = re.search(r"export const INCONCLUSIVE_CODES = \[(.*?)\] as const", source, re.S)
    assert table, "控制台的码表没有找到"
    assert re.findall(r"'([a-z_]+)'", table.group(1)) == list(CAUSE_CODES)

    cause_info = re.search(r"export const CAUSE_INFO[^{]+\{(.*)\n\}", source, re.S)
    assert cause_info, "控制台的成因说明映射没有找到"
    for code in CAUSE_CODES:
        entry = re.search(rf"\n  {code}: \{{(.*?)\n  \}}", cause_info.group(1), re.S)
        assert entry, f"控制台缺少 {code} 的说明"
        label = re.search(r"label: '([^']+)'", entry.group(1))
        action = re.search(r"action: '([^']+)'", entry.group(1))
        assert label and label.group(1).strip(), f"{code} 缺少中文标签"
        assert action and action.group(1).strip(), f"{code} 缺少可操作说明"


def test_typescript_legacy_parser_agrees_with_python() -> None:
    """pi 的兼容解析不能与 Python 侧说出两个结果（闭集行为也要一致）。"""

    if not (ROOT / "integrations/pi/node_modules/tsx").exists() or not shutil.which("node"):
        pytest.skip("install integrations/pi dependencies to run cross-language contract")

    cases = [
        "recording_loss",
        "incomplete_recording: missing boundary or event sequence gap",
        "replay_recording_loss",
        "no_recording: step 2",
        "model_output_truncated",
        "unsupported_context: truncated messages",
        # 语义反转的那条：两侧都必须落 unknown，不能判成 blocked。
        "side_effect_executed",
        "side_effect_executed: step 4",
        "[afr] 父 Run 中没有与本次调用匹配的 read 录制结果。",
        "",
    ]
    parsed = _run_ts(
        "import { legacyReason } from '../src/reasons.ts';"
        "const cases = JSON.parse(process.argv[2]);"
        "console.log(JSON.stringify(cases.map((c) => legacyReason(c))));",
        json.dumps(cases),
    )
    assert parsed == [
        InconclusiveReason.legacy(case).model_dump(mode="json") for case in cases
    ]


def test_typescript_unknown_values_fall_to_unknown() -> None:
    """跨语言的闭集行为一致：两侧都不允许把未知取值当成已知成因。"""

    if not (ROOT / "integrations/pi/node_modules/tsx").exists() or not shutil.which("node"):
        pytest.skip("install integrations/pi dependencies to run cross-language contract")

    parsed = _run_ts(
        "import { reasonOf } from '../src/reasons.ts';"
        "console.log(JSON.stringify(reasonOf({ code: 'not_a_code', detail: 'x' })));",
        None,
    )
    assert parsed == {"code": "unknown", "detail": "x"}


def _run_ts(body: str, argument: str | None) -> Any:
    """跑一段真的 TypeScript。

    必须落成文件再交给 tsx：``node -e`` 不经过 tsx 的加载钩子，`.ts` 会在解析期就失败。
    文件建在 pi 包内，这样 ``../src/reasons.ts`` 这类相对导入按文件位置解析得到。
    """

    import tempfile

    with tempfile.TemporaryDirectory(dir=ROOT / "integrations/pi") as directory:
        script = Path(directory) / "probe.ts"
        script.write_text(body, encoding="utf-8")
        command = ["node", "--import", "tsx", str(script)]
        if argument is not None:
            command.append(argument)
        result = subprocess.run(
            command,
            cwd=ROOT / "integrations/pi",
            capture_output=True,
            # 输出里有中文说明：必须显式按 UTF-8 解码，否则 Windows 默认的 GBK 会解码失败。
            encoding="utf-8",
            check=True,
            timeout=60,
        )
    return json.loads(result.stdout)
