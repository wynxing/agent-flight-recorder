"""端到端：真实 OpenAI Agents SDK Agent + 录制 + 回放 + 对比 + 用例。

与 examples/langgraph_sre_agent/tests/test_scenario.py 对称，但这里回答的是另一个问题：
同一套复现语义在**第二个框架**上是否同样成立。最重要的一条仍然是复现的确定性；
除此之外，本文件还包含三条只在「有两个框架」时才可能成立的证据：

* 两个框架录下来的**行为指纹**（定义好的语义内容：事件类型与名称、工具参数、模型输出文本、
  工具调用的名称与参数）一致（同一份场景、同一批台词）；
* **同一份录制**分别用两个适配层回放，**行为指纹与最终产出**相等。这不是「完整 Event 逐字段
  相等」：框架自己的消息/状态外壳与运行身份字段本来就不同，逐字段的边界由
  `test_cross_framework_difference_is_confined_to_the_framework_envelope` 钉住；
* 平台按 Agent 自己声明的 runtime 选择适配层（服务端不再只认识 LangGraph）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from afr_server import replay_runner, storage
from afr_server.db import session_scope
from afr_server.diff import diff_runs
from afr_server.transport import DirectTransport
from agent_flight_recorder import EffectMode, EffectPolicy, Recorder, RunStatus
from agent_flight_recorder.replay import langgraph_adapter, openai_agents_adapter
from agent_flight_recorder.replay.adapters import adapter_names, load_adapter
from agent_flight_recorder.replay.engine import ReplayPlan, ReplaySession
from oa_sre_agent.agent import RUNTIME, agent_spec
from oa_sre_agent.prompts import GROUNDED_SYSTEM_PROMPT
from oa_sre_agent.scripted_model import SCRIPTED_MODEL_NAME

#: 与 LangGraph 示例同名的场景模块：两个框架要回放同一份录制，就得能被同一套测试驱动。
from sre_agent import agent as langgraph_agent


def record_parent(run_id: str = "parent-run", *, agent_module: Any = None) -> str:
    module = agent_module or __import__("oa_sre_agent.agent", fromlist=["x"])
    recorder = Recorder(
        module.AGENT_NAME,
        transport=DirectTransport(),
        flush_interval=0.0,
        run_id=run_id,
        model=SCRIPTED_MODEL_NAME,
    )
    module.run_scenario(recorder)
    recorder.close()
    return run_id


def behavioral_fingerprint(events) -> list[tuple]:
    """参与位置对齐的语义内容指纹（与 LangGraph 侧同一条定义）。

    覆盖：事件类型、名称、工具参数、模型输出文本、工具调用的名称与参数。
    不覆盖：事件里的其余字段——包括运行身份（id / run_id / started_at）与**框架自己的
    消息/状态外壳**（input.messages、output.message、output.state）。后者在两个框架之间
    本来就不一样（LangChain 是 AIMessage 的 dict，Agents SDK 是 Responses API 的条目），
    因此「两个框架一致」这句话只能限定在这条指纹 + final output 上，**不是**完整 Event
    逐字段相等；全部字段的边界由 test_cross_framework_difference_is_confined_to_the_framework_envelope
    钉住，而这条指纹「确实覆盖了这些语义字段」由 test_the_fingerprint_covers_the_semantic_content 钉住。
    """

    fingerprint = []
    for event in events:
        if event.type.value not in {"model_call", "tool_call"}:
            continue
        payload = event.input or {}
        output = event.output or {}
        fingerprint.append(
            (
                event.type.value,
                event.name,
                json.dumps(payload.get("args"), sort_keys=True, default=str),
                output.get("text"),
                tuple(call.get("name") for call in (output.get("tool_calls") or [])),
                tuple(sorted(str(call.get("args")) for call in (output.get("tool_calls") or []))),
            )
        )
    return fingerprint


@pytest.fixture()
def parent_run(afr_db) -> str:
    return record_parent()


def test_agent_records_a_complete_timeline(parent_run: str) -> None:
    with session_scope() as session:
        events = storage.get_events(session, parent_run)
        summary = storage.row_to_summary(storage.get_run(session, parent_run))

    types = [event.type.value for event in events]
    assert types[0] == "run_started"
    assert types[-1] == "run_finished"
    assert summary.model_calls == 6
    assert summary.tool_calls == 5
    assert summary.error_count == 0

    tool_names = [event.name for event in events if event.type.value == "tool_call"]
    assert tool_names == [
        "prometheus_query",
        "loki_query",
        "k8s_describe",
        "github_deployments",
        "notify_oncall",
    ]
    # 工具事件挂在产出它的模型步骤上：两个框架用的是同一套 linkage。
    model_seqs = [event.seq for event in events if event.type.value == "model_call"]
    tool_parents = [event.parent_seq for event in events if event.type.value == "tool_call"]
    assert tool_parents == model_seqs[:5]


def test_state_snapshots_are_recorded_at_step_boundaries(parent_run: str) -> None:
    """Checkpoint 是事件日志在步边界的投影：Agents SDK 这边的「状态」就是条目列表。"""

    with session_scope() as session:
        events = storage.get_events(session, parent_run)
    snapshots = [event for event in events if event.type.value == "state_snapshot"]
    assert len(snapshots) == 6
    assert all(event.output and "state" in event.output for event in snapshots)
    assert all(event.output["state"]["items"] for event in snapshots)


def test_parent_run_ends_with_the_wrong_conclusion(parent_run: str) -> None:
    """演示场景里，默认 Prompt 会把症状当成根因。这是需要被修复的失败。"""

    with session_scope() as session:
        events = storage.get_events(session, parent_run)
    final = storage.final_output_of(events)
    assert final is not None
    assert "Redis 连接池" in final


def test_reproduce_is_deterministic(parent_run: str) -> None:
    """回放可信度的地基：行为指纹（语义内容）与最终产出与父 Run 相等。

    刻意不说「除运行时字段外逐字段一致」：那条更强的话不成立（见 `behavioral_fingerprint` 的
    说明）。跨框架的字段级边界另有一条断言。
    """

    plan = ReplayPlan.reproduce(parent_run, from_seq=1)
    result = replay_runner.execute_replay(plan, "repro-run")
    assert result.status == RunStatus.SUCCEEDED.value
    assert result.first_fork is None

    with session_scope() as session:
        parent_events = storage.get_events(session, parent_run)
        replay_events = storage.get_events(session, "repro-run")
        summary = storage.row_to_summary(storage.get_run(session, "repro-run"))

    assert behavioral_fingerprint(parent_events) == behavioral_fingerprint(replay_events)
    assert storage.final_output_of(parent_events) == storage.final_output_of(replay_events)


    # 复现不产生任何真实调用。
    assert summary.effect_counts == {"recorded": 11}


def test_reproduced_model_calls_keep_the_original_token_accounting(parent_run: str) -> None:
    plan = ReplayPlan.reproduce(parent_run, from_seq=1)
    replay_runner.execute_replay(plan, "repro-run")

    with session_scope() as session:
        parent_events = storage.get_events(session, parent_run)
        replay_events = storage.get_events(session, "repro-run")

    def tokens(events):
        return [
            (event.tokens.total if event.tokens else None)
            for event in events
            if event.type.value == "model_call"
        ]

    assert tokens(parent_events) == tokens(replay_events)


def test_regress_with_better_prompt_changes_the_conclusion(parent_run: str) -> None:
    plan = ReplayPlan.regress(
        parent_run, from_seq=15, overrides={"system_prompt": GROUNDED_SYSTEM_PROMPT}
    )
    result = replay_runner.execute_replay(plan, "regress-run")
    assert result.status == RunStatus.SUCCEEDED.value
    assert result.first_fork is not None
    assert result.first_fork.parent_seq == 15

    with session_scope() as session:
        events = storage.get_events(session, "regress-run")
        summary = storage.row_to_summary(storage.get_run(session, "regress-run"))

    final = storage.final_output_of(events)
    assert final is not None
    assert "REDIS_POOL_SIZE" in final
    # 分叉点之前按录制复现，之后只有模型真实执行。
    assert summary.effect_counts == {"recorded": 9, "live": 2}


def test_diff_pinpoints_the_divergence(parent_run: str) -> None:
    plan = ReplayPlan.regress(
        parent_run, from_seq=15, overrides={"system_prompt": GROUNDED_SYSTEM_PROMPT}
    )
    replay_runner.execute_replay(plan, "regress-run")

    with session_scope() as session:
        parent_events = storage.get_events(session, parent_run)
        replay_events = storage.get_events(session, "regress-run")
        a = storage.run_to_record(storage.get_run(session, parent_run))
        b = storage.run_to_record(storage.get_run(session, "regress-run"))

    diff = diff_runs(a, parent_events, b, replay_events)
    assert diff.final_output["changed"] is True
    # 工具调用完全一致：差异只在推理与结论上，这正是换 Prompt 的预期效果。
    assert all(item.status == "same" for item in diff.tools)
    assert diff.first_divergence is not None
    assert diff.first_divergence.kind.value == "model_output"


def test_side_effect_gate_blocks_external_tool_by_default(parent_run: str) -> None:
    policy = EffectPolicy(
        default=EffectMode.RECORDED,
        by_kind={
            "model_call": EffectMode.LIVE,
            "tool_call": EffectMode.LIVE,
        },
    )
    plan = ReplayPlan(
        parent_run_id=parent_run,
        from_seq=15,
        policy=policy,
        overrides={"system_prompt": GROUNDED_SYSTEM_PROMPT},
    )
    replay_runner.execute_replay(plan, "gated-run")

    with session_scope() as session:
        events = storage.get_events(session, "gated-run")
    notify = next(event for event in events if event.name == "notify_oncall")

    assert notify.effect_source.value == "dry_run"
    assert notify.attributes["severity"] == "warning"
    assert notify.attributes["reason"] == "side_effect_gate"
    assert notify.attributes["synthetic"] is True
    assert notify.side_effect.value == "external"


def test_explicit_permission_executes_but_leaves_a_warning(parent_run: str) -> None:
    policy = EffectPolicy(
        default=EffectMode.RECORDED,
        by_kind={"model_call": EffectMode.LIVE, "tool_call": EffectMode.LIVE},
        allow_side_effect_execution=True,
    )
    plan = ReplayPlan(
        parent_run_id=parent_run,
        from_seq=15,
        policy=policy,
        overrides={"system_prompt": GROUNDED_SYSTEM_PROMPT},
    )
    replay_runner.execute_replay(plan, "allowed-run")

    with session_scope() as session:
        events = storage.get_events(session, "allowed-run")
    notify = next(event for event in events if event.name == "notify_oncall")

    assert notify.effect_source.value == "live"
    assert notify.attributes["severity"] == "warning"
    assert notify.attributes["side_effect_executed"] is True


def test_replay_is_recorded_as_a_new_run_pointing_at_its_parent(parent_run: str) -> None:
    plan = ReplayPlan.reproduce(parent_run, from_seq=3)
    replay_runner.execute_replay(plan, "child-run")

    with session_scope() as session:
        child = storage.run_to_record(storage.get_run(session, "child-run"))
        children = storage.list_children(session, parent_run)

    assert child.parent_run_id == parent_run
    assert child.replay_from_seq == 3
    assert [row.id for row in children] == ["child-run"]


def test_agent_spec_declares_its_runtime_and_one_seed_case() -> None:
    spec = agent_spec()

    assert spec.runtime == RUNTIME == "openai-agents"
    assert RUNTIME in adapter_names()
    assert load_adapter(RUNTIME) is openai_agents_adapter

    seed_cases = spec.seed_cases
    assert len(seed_cases) == 1
    case = seed_cases[0]
    assert case.from_seq == 15
    assert case.system_prompt is None
    assert case.preset is None
    assert case.assertions == [{"type": "final_output_contains", "value": "REDIS_POOL_SIZE"}]


def test_replay_refuses_to_guess_the_state_when_the_parent_recorded_one(afr_db) -> None:
    """父 Run 记了 input 但没有 state 也没有 task_only 声明时，必须显式失败。

    与 LangGraph 侧同一条边界（docs/replay-semantics.md 第 1 节）：引擎不会用默认的
    task 消息假装状态已经恢复。第二个框架上这条语义不变。
    """

    parent = record_parent("legacy-parent")
    with session_scope() as session:
        row = storage.get_run(session, "legacy-parent")
        metadata = dict(row.meta or {})
        metadata.pop("afr_replay_context", None)
        row.meta = metadata
        session.add(row)

    result = replay_runner.execute_replay(ReplayPlan.reproduce(parent, from_seq=1), "no-context-run")

    assert result.complete is False
    assert result.reason is not None
    assert result.reason.code == "missing_initial_state"
    assert result.status == RunStatus.FAILED.value


# ---------------------------------------------------------------- 两个框架的对比


#: 跨框架对比时允许不同的运行身份字段：每次回放都是新的 Run。
RUNTIME_FIELDS = {"id", "run_id", "started_at"}

#: 跨框架对比时允许不同的**框架外壳**字段。同一份文本在两个框架里的原生载体不同：
#: LangChain 是 AIMessage / HumanMessage 序列化出来的 dict，Agents SDK 是 Responses API 的
#: 条目（content 块、function_call 条目、instructions 等），状态快照同理（messages vs items）。
#: 这些不是回放语义差异：参与对齐的语义内容由行为指纹与 final output 比较。
FRAMEWORK_ENVELOPE = {
    ("input", "messages"),
    ("input", "message_count"),
    ("output", "message"),
    ("output", "state"),
}


def differing_fields(left: Any, right: Any) -> set[tuple[str, str]]:
    """两个 Run 的事件里，哪些字段（含子字段）取值不同。

    比行为指纹更宽：指纹只看参与对齐的语义内容，这里看**全部**字段，因此能用来证明
    「跨框架的差异只落在运行身份与框架外壳上」——而不是只把这句话写进文档。
    子字段只在两侧都是 dict 时展开；否则记成 (字段, "")。
    """

    assert len(left) == len(right), (
        f"两个 Run 的事件数不同（{len(left)} vs {len(right)}），无法逐步比对"
    )
    diffs: set[tuple[str, str]] = set()
    for a, b in zip(left, right):
        payload_a, payload_b = a.model_dump(), b.model_dump()
        for key in set(payload_a) | set(payload_b):
            value_a, value_b = payload_a.get(key), payload_b.get(key)
            if value_a == value_b:
                continue
            if isinstance(value_a, dict) and isinstance(value_b, dict):
                for sub in set(value_a) | set(value_b):
                    if value_a.get(sub) != value_b.get(sub):
                        diffs.add((key, sub))
            else:
                diffs.add((key, ""))
    return diffs


def _replay_with(adapter: Any, parent: str, run_id: str, factory: Any, side_effects: Any):
    """用指定的适配层回放一份录制（绕开按 agent_name 的解析）。

    这就是「同一份录制、两个适配层」的最小构造：父 Run 是既有的，适配层与 Agent
    构建函数由测试指定。平台路径走的是同一条 run_replay 契约。
    """

    with session_scope() as session:
        parent_run = storage.run_to_record(storage.get_run(session, parent))
        parent_events = storage.get_events(session, parent)

    plan = ReplayPlan.reproduce(parent, from_seq=1)
    recorder = Recorder(
        parent_run.agent_name,
        transport=DirectTransport(),
        flush_interval=0.0,
        run_id=run_id,
    )
    session_obj = ReplaySession(plan, parent_run, parent_events)
    result = adapter.run_replay(
        session=session_obj,
        recorder=recorder,
        agent_factory=factory,
        tool_side_effects=side_effects,
    )
    recorder.close()
    return result


def test_the_two_frameworks_record_the_same_behavior(afr_db) -> None:
    """同一份场景与台词在两个框架上产生一致的行为指纹（语义内容），最终产出也相同。

    这是「两个框架可比」的前提：如果连录制的语义内容都对不上，后面的对比就没有意义。
    这里比的是定义好的行为指纹与最终产出，**不是**完整 Event 逐字段相等（外壳与运行身份
    字段本来就会不同）。
    """

    record_parent("lg-parent", agent_module=langgraph_agent)
    record_parent("oa-parent")

    with session_scope() as session:
        langgraph_events = storage.get_events(session, "lg-parent")
        agents_events = storage.get_events(session, "oa-parent")

    assert behavioral_fingerprint(langgraph_events) == behavioral_fingerprint(agents_events)
    assert storage.final_output_of(langgraph_events) == storage.final_output_of(agents_events)


def test_the_fingerprint_covers_the_semantic_content(afr_db) -> None:
    """行为指纹自己不能退化：它要是把语义字段丢了（极端情况是返回空列表），
    本文件里所有基于它的「相等」断言都会变成空转——没有任何比较对象，永远相等。

    这是「写了承诺却能被删掉而不被发现」的那一类：多个测试都只比较指纹，没人看它盖了什么。
    这里把指纹的覆盖面钉成可执行的断言：每个模型调用与工具调用都各占一项，工具事件带正确的
    名称与参数，模型事件带输出文本或工具调用。
    """

    parent = record_parent("fingerprint-parent", agent_module=langgraph_agent)
    with session_scope() as session:
        events = storage.get_events(session, parent)

    fingerprint = behavioral_fingerprint(events)
    kinds = [item[0] for item in fingerprint]
    assert len(fingerprint) == 11, "行为指纹必须逐一覆盖每个行为步骤（6 次模型调用 + 5 次工具调用）"
    assert kinds.count("model_call") == 6
    assert kinds.count("tool_call") == 5

    tools = [item for item in fingerprint if item[0] == "tool_call"]
    assert [item[1] for item in tools] == [
        "prometheus_query",
        "loki_query",
        "k8s_describe",
        "github_deployments",
        "notify_oncall",
    ]
    # 工具参数确实进了指纹（不是空串占位）。
    assert all(item[2] and item[2] != "null" for item in tools)

    models = [item for item in fingerprint if item[0] == "model_call"]
    # 每次模型调用都带输出文本，且至少有一次带工具调用：两者都是指纹实际比较的内容。
    assert all(item[3] for item in models)
    assert any(item[4] for item in models)


def test_the_same_recording_replays_identically_on_both_frameworks(afr_db) -> None:
    """本轮的核心结论：同一个父 Run，两个适配层给出同一结论与同一产出。"""

    parent = record_parent("shared-parent", agent_module=langgraph_agent)
    with session_scope() as session:
        parent_events = storage.get_events(session, parent)

    langgraph_result = _replay_with(
        langgraph_adapter, parent, "same-lg", langgraph_agent.agent_spec().build,
        langgraph_agent.agent_spec().tool_side_effects,
    )
    agents_result = _replay_with(
        openai_agents_adapter, parent, "same-oa", agent_spec().build, agent_spec().tool_side_effects,
    )

    assert langgraph_result.status == agents_result.status == RunStatus.SUCCEEDED.value
    assert langgraph_result.first_fork is None and agents_result.first_fork is None

    with session_scope() as session:
        first = storage.get_events(session, "same-lg")
        second = storage.get_events(session, "same-oa")
        summary = storage.row_to_summary(storage.get_run(session, "same-oa"))

    assert behavioral_fingerprint(first) == behavioral_fingerprint(second)
    assert behavioral_fingerprint(parent_events) == behavioral_fingerprint(second)
    assert storage.final_output_of(first) == storage.final_output_of(second)
    assert storage.final_output_of(parent_events) == storage.final_output_of(second)
    assert summary.effect_counts == {"recorded": 11}


def test_an_openai_agents_recording_can_be_replayed_by_the_langgraph_adapter(afr_db) -> None:
    """反向也要成立：录制的生产方与回放的适配层是两件可以拆开的事。"""

    parent = record_parent("oa-parent")
    with session_scope() as session:
        parent_events = storage.get_events(session, parent)

    result = _replay_with(
        langgraph_adapter, parent, "oa-repro-lg", langgraph_agent.agent_spec().build,
        langgraph_agent.agent_spec().tool_side_effects,
    )

    assert result.status == RunStatus.SUCCEEDED.value
    assert result.first_fork is None
    with session_scope() as session:
        replay_events = storage.get_events(session, "oa-repro-lg")
    assert behavioral_fingerprint(parent_events) == behavioral_fingerprint(replay_events)
    # 指纹相等还不够：最终产出也要相等（这句话同样要在证据里站得住）。
    assert storage.final_output_of(parent_events) == storage.final_output_of(replay_events)


def test_cross_framework_difference_is_confined_to_the_framework_envelope(afr_db) -> None:
    """同一份录制、两个适配层：差异只允许落在运行身份与框架外壳上。

    这条比行为指纹严格。指纹只看参与位置对齐的语义内容（模型输出文本、工具名与参数），
    事件里其余字段一律不看——工具事件的 side_effect 被漏记、错误状态不一致、某个标注
    丢失，指纹都不会发现。这里逐字段比对，于是「跨框架复现」的边界变成可执行的断言：

    * 语义字段（含 side_effect / error / tokens / attributes）必须在两个框架上一致；
    * 只允许 RUNTIME_FIELDS 与 FRAMEWORK_ENVELOPE 里的字段不同。

    envelope 差异本身也被钉住（至少要真的存在消息外壳差异）：如果哪天两个框架的消息载体
    变得一致，应当同时改掉 docs/architecture.md 里那句「框架原生载体不同」，而不是让这里
    静默通过。
    """

    parent = record_parent("envelope-parent", agent_module=langgraph_agent)

    _replay_with(
        langgraph_adapter, parent, "env-lg", langgraph_agent.agent_spec().build,
        langgraph_agent.agent_spec().tool_side_effects,
    )
    _replay_with(
        openai_agents_adapter, parent, "env-oa", agent_spec().build, agent_spec().tool_side_effects,
    )

    with session_scope() as session:
        first = storage.get_events(session, "env-lg")
        second = storage.get_events(session, "env-oa")

    diffs = differing_fields(first, second)
    stray = diffs - FRAMEWORK_ENVELOPE - {(field, "") for field in RUNTIME_FIELDS}
    assert stray == set(), f"跨框架回放出现了外壳之外的差异：{sorted(stray)}"
    # 语义字段一个都不许进差异集：上面那条断言真正的价值所在。
    assert not any(field in {"side_effect", "error", "tokens", "attributes"} for field, _ in diffs)
    # 外壳差异确实存在（否则这段说明与文档就该一起改）。
    assert {("output", "message"), ("input", "messages")} & diffs
