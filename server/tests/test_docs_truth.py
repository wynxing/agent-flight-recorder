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

#: 用例集版本（issue #24）交付之后不再为真的说法。
#: 旧的 §5.2 把「数据集版本管理」整体列为未交付，那会被读成「这批跑的是哪一版用例」也还没有——
#: 这句话现在只对了一半（跨版本对比、迁移工具仍然没做，归属已经有了）。
STALE_VERSION_CLAIMS = (
    "LLM Judge 与数据集版本管理",
    "不含 LLM Judge、数据集版本管理",
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


def test_docs_admit_the_case_set_version() -> None:
    """用例集版本已交付：文档必须承认它，且不能再把「数据集版本管理」整体算作未交付。"""

    for relative in ("README.md", "docs/PRD.md", "docs/architecture.md", "docs/protocol.md"):
        assert "用例集版本" in _read(relative), f"{relative} 没有提到已交付的用例集版本"

    for relative in ("README.md", "docs/PRD.md"):
        text = _read(relative)
        for stale in STALE_VERSION_CLAIMS:
            assert stale not in text, f"{relative} 里仍留着已被推翻的说法：{stale}"


def test_the_docs_keep_the_unbuilt_parts_of_versioning() -> None:
    """「做了什么」必须与「没做什么」一起出现：边界被删掉比被写错更危险。

    这三件事**没有**做：跨版本的对比视图、版本迁移 / 合并 / 分支、跨批次的趋势看板。
    另外还有一条事实性边界必须留着——历史批次没有版本记录，而且不回填。
    """

    readme = _read("README.md")
    architecture = _read("docs/architecture.md")

    for surface in ("跨版本的对比视图", "版本迁移", "趋势看板"):
        assert surface in readme, f"README 的边界一节没有说明「{surface}」没做"

    # 「无版本记录」是历史批次的真实状态：它在 README 与架构文档里都要说清，
    # 而且必须与「不回填」这条立场一起出现。
    assert "无版本记录" in readme
    assert "按当前用例反推" in readme
    assert "无版本记录" in architecture
    assert "按当前用例反推" in architecture


def test_the_docs_state_the_effective_definition_rule() -> None:
    """「最近一次结论所用的定义」包含执行覆盖：这条规则必须在文档里，且不许被简化掉。

    只写 `last_definition_digest` 而不说它覆盖了单次运行的覆盖，读者会以为「摘要不同 = 定义被改了」——
    那正是这一层要避免的错误归属（审核在真机上抓到过：实际从第 1 步回放，却记成第 15 步那一版）。
    这三份文档各自承担不同读者：协议给实现者、架构给维护者、README 给使用者。
    """

    for relative in ("docs/protocol.md", "docs/architecture.md", "README.md"):
        body = _read(relative)
        assert "有效定义" in body, f"{relative} 没有说明「有效定义」这条规则"
        assert "last_definition_overrides" in body, f"{relative} 没有给出覆盖那一列"

    # 预算是**有意**不进定义的：这条边界也要留在文档里（否则下一个人会把它加回去）。
    for relative in ("docs/protocol.md", "README.md"):
        assert "预算" in _read(relative), f"{relative} 没有说明预算与定义的关系"


def test_the_docs_do_not_claim_an_override_always_changes_the_digest() -> None:
    """「有覆盖 ⇒ 摘要不同」是错的：覆盖成它本来就等于的值时，两份摘要相同。

    审核指出这四处都这么写过。措辞上的错误不变量比不写更糟——读者会据此得出「摘要相同 ⇒ 没覆盖过」
    这条不成立的推论。正确的说法是**双向**的：摘要相同当且仅当有效值相同。代码注释也在检查范围内，
    因为它是那三份文档里那句话的出处。
    """

    for relative in (
        "README.md",
        "docs/protocol.md",
        "docs/architecture.md",
        "server/afr_server/case_versions.py",
    ):
        body = _read(relative)
        assert "有覆盖时必然不同" not in body, f"{relative} 里仍留着被推翻的说法"
        assert "有覆盖时两份摘要必然不同" not in body, f"{relative} 里仍留着被推翻的说法"
        assert "有效值" in body, f"{relative} 没有说明「有效值相同则摘要相同」"
