"""文档真值守卫：已交付的能力不能被写成未交付，未验证面也不能被悄悄删掉。

docs/PRD.md 与 README.md 描述的是**当前范围**，因此它们会随着能力交付而更新；但没有任何
机制阻止它们停留在旧状态——第 11 轮交付第二个框架适配后，PRD 与 README 仍然写着
「只验证过一个框架」「暂缓其他框架」，就是这种漂移。

这里用最小的一组断言守住两件事：

* 已经交付的接入面必须在文档里被承认（不再出现被推翻的旧结论）；
* 「验证到哪一层」的边界必须继续写在风险表里——边界被删掉比被写错更危险，
  因为读者会以为未验证的面已经验证过了。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 交付第二个框架适配之后，这些句子就不再为真，不允许回来。
STALE_CLAIMS = (
    "只验证过一个框架",
    "暂缓通用 TypeScript SDK、其他框架",
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_prd_and_readme_admit_the_second_framework_adapter() -> None:
    """第二条适配路径已交付：文档必须承认它，且不能再出现被推翻的旧结论。"""

    for relative in ("docs/PRD.md", "README.md"):
        text = _read(relative)
        for stale in STALE_CLAIMS:
            assert stale not in text, f"{relative} 里仍留着已被推翻的说法：{stale}"

    # 两个接入面各自的运行标识与选路机制，是「第二框架已交付」这句话的最小证据面。
    assert "openai-agents" in _read("docs/PRD.md")
    for relative in ("docs/PRD.md", "README.md", "docs/architecture.md"):
        assert "AgentSpec.runtime" in _read(relative), f"{relative} 没有说明按 runtime 选适配层"
        assert "OpenAI Agents SDK" in _read(relative), f"{relative} 没有提到第二个框架"


def test_the_risk_table_keeps_the_unverified_surfaces() -> None:
    """风险表那一行必须同时给出「验证到哪一层」与「还剩哪些未验证面」。

    只写「框架无关已由第二个框架支撑」是不够的：读者需要知道流式、真实模型、handoffs 等
    面**没有**被验证。把这一段删掉会让文档比事实更乐观，因此这里逐项钉住。
    """

    architecture = _read("docs/architecture.md")
    row = next(
        (line for line in architecture.splitlines() if line.startswith("| 框架适配层有两条路径")),
        None,
    )
    assert row is not None, "风险表里找不到框架适配那一行"

    for surface in ("流式调用", "真实模型", "handoffs / guardrails / MCP", "failure_error_function"):
        assert surface in row, f"风险表那一行没有说明「{surface}」尚未验证"


def test_readme_still_points_at_the_full_boundary_section() -> None:
    """README 只给结论是不够的：它必须把读者送到写清边界的架构文档。"""

    readme = _read("README.md")
    assert "docs/architecture.md" in readme
    assert "框架适配的验证边界" in readme
