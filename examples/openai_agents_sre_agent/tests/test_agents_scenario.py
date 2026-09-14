"""端到端：真实 OpenAI Agents SDK Agent + 录制 + 回放 + 对比 + 用例。

与 examples/langgraph_sre_agent/tests/test_scenario.py 对称，但这里回答的是另一个问题：
同一套复现语义在**第二个框架**上是否同样成立。最重要的一条仍然是复现的确定性；
除此之外，本文件还包含三条只在「有两个框架」时才可能成立的证据：

* 两个框架录下来的行为内容逐字段一致（同一份场景、同一批台词）；
* **同一份录制**分别用两个适配层回放，结论与产出完全一致；
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
    """除时间戳、ID 与 effect_source 之外的行为内容指纹（与 LangGraph 侧同一条定义）。"""

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
    """回放可信度的地基：除时间戳、ID 与 effect_source 外逐字段一致。"""

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
    """同一份场景与台词在两个框架上产生逐字段一致的行为内容。

    这是「两个框架可比」的前提：如果连录制都对不上，后面的对比就没有意义。
    """

    record_parent("lg-parent", agent_module=langgraph_agent)
    record_parent("oa-parent")

    with session_scope() as session:
        langgraph_events = storage.get_events(session, "lg-parent")
        agents_events = storage.get_events(session, "oa-parent")

    assert behavioral_fingerprint(langgraph_events) == behavioral_fingerprint(agents_events)
    assert storage.final_output_of(langgraph_events) == storage.final_output_of(agents_events)


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
