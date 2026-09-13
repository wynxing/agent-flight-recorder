"""端到端：真实 LangGraph Agent + 录制 + 回放 + 对比 + 用例。

这里最重要的一条是 test_reproduce_is_deterministic。那条过不了，回放就不可信，
后面所有能力都建立在它之上。
"""

from __future__ import annotations

import json

import pytest
from agent_flight_recorder import EffectMode, EffectPolicy, Recorder, RunStatus
from agent_flight_recorder.replay.engine import ReplayPlan
from afr_server import replay_runner, storage
from afr_server.db import session_scope
from afr_server.diff import diff_runs
from afr_server.transport import DirectTransport
from sre_agent.agent import AGENT_NAME, run_scenario
from sre_agent.prompts import GROUNDED_SYSTEM_PROMPT


def record_parent(run_id: str = "parent-run") -> str:
    recorder = Recorder(
        AGENT_NAME,
        transport=DirectTransport(),
        flush_interval=0.0,
        run_id=run_id,
    )
    run_scenario(recorder)
    recorder.close()
    return run_id


def behavioral_fingerprint(events) -> list[tuple]:
    """除时间戳、ID 与 effect_source 之外的行为内容指纹。"""

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
                tuple(
                    sorted(str(call.get("args")) for call in (output.get("tool_calls") or []))
                ),
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


def test_state_snapshots_are_recorded_at_step_boundaries(parent_run: str) -> None:
    """Checkpoint 不是独立能力，它是事件日志在步边界的投影。"""

    with session_scope() as session:
        events = storage.get_events(session, parent_run)
    snapshots = [event for event in events if event.type.value == "state_snapshot"]
    assert len(snapshots) == 6
    assert all(event.output and "state" in event.output for event in snapshots)


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


def test_read_tools_still_execute_when_explicitly_allowed(parent_run: str) -> None:
    """闸门只拦写操作与对外动作。

    从第 1 步开始，因此所有工具步骤都在分叉点之后：只读工具真实执行，
    而被标为 external 的通知工具仍然被拦截。
    """

    policy = EffectPolicy(default=EffectMode.RECORDED, by_kind={"tool_call": EffectMode.LIVE})
    plan = ReplayPlan(parent_run_id=parent_run, from_seq=1, policy=policy)
    replay_runner.execute_replay(plan, "read-live-run")

    with session_scope() as session:
        events = storage.get_events(session, "read-live-run")
    prometheus = next(event for event in events if event.name == "prometheus_query")
    notify = next(event for event in events if event.name == "notify_oncall")

    assert prometheus.effect_source.value == "live"
    assert prometheus.attributes.get("severity") != "warning"
    assert notify.effect_source.value == "dry_run"
    assert notify.attributes["severity"] == "warning"


def test_replay_is_recorded_as_a_new_run_pointing_at_its_parent(parent_run: str) -> None:
    plan = ReplayPlan.reproduce(parent_run, from_seq=3)
    replay_runner.execute_replay(plan, "child-run")

    with session_scope() as session:
        child = storage.run_to_record(storage.get_run(session, "child-run"))
        children = storage.list_children(session, parent_run)

    assert child.parent_run_id == parent_run
    assert child.replay_from_seq == 3
    assert [row.id for row in children] == ["child-run"]
