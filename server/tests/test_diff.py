"""Agent Diff：对齐、差异判定与判定语。"""

from __future__ import annotations

from agent_flight_recorder.models import Event, EventType, RunRecord, RunStatus, utcnow
from afr_server.diff import diff_runs


def event(seq: int, event_type: EventType, **kwargs) -> Event:
    return Event(
        id=f"e{seq}",
        run_id="r",
        seq=seq,
        type=event_type,
        started_at=utcnow(),
        **kwargs,
    )


def tool(seq: int, name: str, args: dict, text: str = "ok") -> Event:
    return event(seq, EventType.TOOL_CALL, name=name, input={"args": args}, output={"text": text})


def run(run_id: str, model: str = "m") -> RunRecord:
    return RunRecord(id=run_id, agent_name="agent", model=model, status=RunStatus.SUCCEEDED, started_at=utcnow())


def test_identical_runs_report_no_differences() -> None:
    events = [tool(1, "query", {"q": "x"}), tool(2, "notify", {"c": "#a"})]
    diff = diff_runs(run("a"), events, run("b"), events)
    assert all(item.status == "same" for item in diff.tools)
    assert diff.first_divergence is None
    assert "完全一致" in diff.verdict


def test_changed_tool_args_are_flagged() -> None:
    a = [tool(1, "query", {"q": "x"})]
    b = [tool(1, "query", {"q": "y"})]
    diff = diff_runs(run("a"), a, run("b"), b)
    assert diff.tools[0].status == "changed"
    assert diff.tools[0].detail["args_changed"] is True


def test_extra_tool_in_one_run_is_flagged_only_b() -> None:
    a = [tool(1, "query", {"q": "x"})]
    b = [tool(1, "query", {"q": "x"}), tool(2, "notify", {"c": "#a"})]
    diff = diff_runs(run("a"), a, run("b"), b)
    statuses = [item.status for item in diff.tools]
    assert "only_b" in statuses


def test_missing_tool_in_one_run_is_flagged_only_a() -> None:
    a = [tool(1, "query", {"q": "x"}), tool(2, "notify", {"c": "#a"})]
    b = [tool(1, "query", {"q": "x"})]
    diff = diff_runs(run("a"), a, run("b"), b)
    assert "only_a" in [item.status for item in diff.tools]


def test_effect_source_alone_does_not_mark_a_tool_changed() -> None:
    """回放天然会把 live 变成 recorded，这不该让整张对比表飘红。"""

    from agent_flight_recorder.models import EffectSource

    a = [tool(1, "query", {"q": "x"})]
    a[0].effect_source = EffectSource.LIVE
    b = [tool(1, "query", {"q": "x"})]
    b[0].effect_source = EffectSource.RECORDED

    diff = diff_runs(run("a"), a, run("b"), b)
    assert diff.tools[0].status == "same"
    # 来源仍然随载荷返回，供界面单独标注。
    assert diff.tools[0].a["effect_source"] == "live"
    assert diff.tools[0].b["effect_source"] == "recorded"


def test_summary_delta_reports_direction() -> None:
    a = [event(1, EventType.MODEL_CALL, name="m", tokens={"input": 100, "output": 50, "total": 150})]
    b = [event(1, EventType.MODEL_CALL, name="m", tokens={"input": 300, "output": 50, "total": 350})]
    diff = diff_runs(run("a"), a, run("b"), b)
    tokens = next(item for item in diff.summary if item.field == "输入 tokens")
    assert tokens.a == 100
    assert tokens.b == 300
    assert tokens.delta == 200


def test_error_count_direction_is_inverted() -> None:
    from agent_flight_recorder.models import ErrorInfo

    a = [event(1, EventType.ERROR, error=ErrorInfo(type="X", message="boom"))]
    b: list[Event] = []
    diff = diff_runs(run("a"), a, run("b"), b)
    errors = next(item for item in diff.summary if item.field == "错误数")
    assert errors.delta == -1
    assert errors.hint == "更好"


def test_ambiguous_counts_get_no_judgement() -> None:
    """调用次数增减的语义是模糊的，不应该替用户下结论。"""

    a = [tool(1, "query", {"q": "x"})]
    b = [tool(1, "query", {"q": "x"}), tool(2, "query", {"q": "y"})]
    diff = diff_runs(run("a"), a, run("b"), b)
    calls = next(item for item in diff.summary if item.field == "工具调用次数")
    assert calls.delta == 1
    assert calls.hint == ""


def test_token_reduction_is_reported_as_better() -> None:
    a = [event(1, EventType.MODEL_CALL, name="m", tokens={"input": 500, "output": 10, "total": 510})]
    b = [event(1, EventType.MODEL_CALL, name="m", tokens={"input": 100, "output": 10, "total": 110})]
    diff = diff_runs(run("a"), a, run("b"), b)
    tokens = next(item for item in diff.summary if item.field == "输入 tokens")
    assert tokens.hint == "更好"


def test_final_output_change_drives_verdict() -> None:
    a = [event(1, EventType.RUN_FINISHED, output={"result": "Redis 连接池耗尽"})]
    b = [event(1, EventType.RUN_FINISHED, output={"result": "配置回归"})]
    diff = diff_runs(run("a"), a, run("b"), b)
    assert diff.final_output["changed"] is True
    assert "最终结论发生变化" in diff.verdict


def test_first_divergence_points_at_earliest_step() -> None:
    a = [tool(1, "query", {"q": "x"}), tool(2, "notify", {"c": "#a"})]
    b = [tool(1, "query", {"q": "x"}), tool(2, "notify", {"c": "#b"})]
    diff = diff_runs(run("a"), a, run("b"), b)
    assert diff.first_divergence is not None
    assert diff.first_divergence.kind.value == "tool_args"
    assert diff.first_divergence.parent_seq == 2


def test_labels_include_model_and_replay_marker() -> None:
    from afr_server.diff import run_label

    replayed = RunRecord(
        id="r",
        agent_name="agent",
        model="m",
        status=RunStatus.SUCCEEDED,
        parent_run_id="p",
        replay_from_seq=4,
        started_at=utcnow(),
    )
    label = run_label(replayed)
    assert "m" in label
    assert "replay@4" in label
