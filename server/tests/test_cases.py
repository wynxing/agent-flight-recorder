"""Case 的创建、执行与断言闭环（不依赖具体 Agent）。"""

from __future__ import annotations




def test_case_can_be_created_from_a_run(client, make_run) -> None:
    client.post("/v1/ingest", json=make_run())
    response = client.post(
        "/v1/cases",
        json={
            "name": "延迟根因定位",
            "source_run_id": "run-1",
            "from_seq": 2,
            "assertions": [
                {"type": "tool_called", "tool": "query"},
                {"type": "final_output_contains", "value": "root cause"},
            ],
            "preset": "regress",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "延迟根因定位"
    assert body["preset"] == "regress"
    assert len(body["assertions"]) == 2
    assert body["source_run"]["id"] == "run-1"


def test_incomplete_execution_cannot_pass_assertions(client, make_run, monkeypatch):
    from types import SimpleNamespace
    from afr_server import cases
    from agent_flight_recorder.replay.reasons import InconclusiveReason
    client.post("/v1/ingest", json=make_run())
    monkeypatch.setattr(
        cases,
        "execute_replay",
        lambda *args: SimpleNamespace(
            status="succeeded", complete=False, reason=InconclusiveReason.from_code("recording_loss")
        ),
    )
    stored = []
    monkeypatch.setattr(cases, "_store", lambda *args: stored.append(args))
    cases._execute(None, "run-1", "case", {"assertions": [{"type": "no_error"}]})
    assert stored[0][2] == "inconclusive"
    # 结论必须带成因，且成因就是录制不完整，而不是副作用被拦截。
    assert stored[0][4].code == "recording_loss"


def test_case_creation_requires_existing_run(client, make_run) -> None:
    response = client.post("/v1/cases", json={"name": "x", "source_run_id": "missing"})
    assert response.status_code == 404


def test_case_listing_and_reading(client, make_run) -> None:
    client.post("/v1/ingest", json=make_run())
    created = client.post(
        "/v1/cases", json={"name": "case", "source_run_id": "run-1", "assertions": []}
    ).json()

    listed = client.get("/v1/cases").json()["cases"]
    assert [item["id"] for item in listed] == [created["id"]]
    assert client.get(f"/v1/cases/{created['id']}").json()["name"] == "case"
    assert client.get("/v1/cases/missing").status_code == 404


def test_case_timestamps_are_serialized_as_utc(client, make_run) -> None:
    """用例的时间戳必须带时区。

    不带 Z 的 ISO 串会被前端当成本地时间解析，刚跑完的用例会显示成 8 小时前
    （东八区），而运行记录那边是带 Z 的——两边不一致本身就是 bug。
    """

    client.post("/v1/ingest", json=make_run())
    created = client.post(
        "/v1/cases", json={"name": "case", "source_run_id": "run-1", "assertions": []}
    ).json()

    assert created["created_at"].endswith("Z"), created["created_at"]


def test_case_defaults_to_reproduce_when_no_preset_is_set(client, make_run) -> None:
    """没声明 preset 的用例按复现语义执行，因此它不该被标成回归模式。"""

    client.post("/v1/ingest", json=make_run())
    created = client.post(
        "/v1/cases", json={"name": "case", "source_run_id": "run-1", "assertions": []}
    ).json()

    assert created["preset"] is None


def test_case_run_returns_immediately_with_a_run_id(client, make_run) -> None:
    client.post("/v1/ingest", json=make_run())
    created = client.post(
        "/v1/cases", json={"name": "case", "source_run_id": "run-1", "assertions": []}
    ).json()

    response = client.post(f"/v1/cases/{created['id']}/run", json={})
    assert response.status_code == 200
    run_id = response.json()["run_id"]

    # 回放 Run 必须在发起的那一刻就存在，否则用户点开刚拿到的链接会看到 404。
    detail = client.get(f"/v1/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["run"]["parent_run_id"] == "run-1"


def test_case_run_on_missing_case_returns_404(client) -> None:
    assert client.post("/v1/cases/missing/run", json={}).status_code == 404


def _events_with_blocked_tool(run_id: str = "run-1") -> list:
    """构造一次「副作用被闸门拦截」的回放事件流。

    ``effect_source == "blocked"`` 是一次执行被安全策略拒绝时写下的来源标记：
    它说明的是「这次执行没有真实发生」，与「录制不完整」完全是两回事。
    """

    from agent_flight_recorder.models import Event, EventType, EffectSource, utcnow

    return [
        Event(id=f"{run_id}-b1", run_id=run_id, seq=1, type=EventType.RUN_STARTED,
              started_at=utcnow(), input={"task": "t"}),
        Event(id=f"{run_id}-b2", run_id=run_id, seq=2, type=EventType.TOOL_CALL, name="notify_oncall",
              started_at=utcnow(), input={"args": {}}, output={"text": "blocked"},
              effect_source=EffectSource.BLOCKED),
        Event(id=f"{run_id}-b3", run_id=run_id, seq=3, type=EventType.RUN_FINISHED,
              started_at=utcnow(), output={"result": "blocked", "status": "succeeded"}),
    ]


def test_blocked_side_effect_and_incomplete_recording_get_different_codes(monkeypatch) -> None:
    """两种「无法判断」必须分开，并给出不同的 code（issue #6 验收 4）。"""

    from types import SimpleNamespace

    from afr_server import cases
    from agent_flight_recorder.replay.reasons import InconclusiveReason

    stored = []
    monkeypatch.setattr(cases, "_store", lambda *args: stored.append(args))
    monkeypatch.setattr(cases, "final_output_of", lambda events: "blocked")

    # 录制不完整：complete=False。
    monkeypatch.setattr(cases, "execute_replay", lambda *a: SimpleNamespace(status="succeeded", complete=False, reason=InconclusiveReason.from_code("recording_loss")))
    monkeypatch.setattr(cases, "get_events", lambda *a, **k: [])
    cases._execute(None, "run-1", "case", {"assertions": [{"type": "no_error"}]})

    # 副作用被拦截：录制本身是完整的，执行被策略拒绝。
    monkeypatch.setattr(cases, "execute_replay", lambda *a: SimpleNamespace(status="succeeded", complete=True, reason=None))
    monkeypatch.setattr(cases, "get_events", lambda *a, **k: _events_with_blocked_tool())
    cases._execute(None, "run-2", "case", {"assertions": [{"type": "no_error"}]})

    assert [entry[2] for entry in stored] == ["inconclusive", "inconclusive"]
    incomplete_cause, blocked_cause = stored[0][4], stored[1][4]
    assert incomplete_cause.code == "recording_loss"
    assert blocked_cause.code == "side_effect_blocked"
    assert incomplete_cause.code != blocked_cause.code
    # 成因必须带上现场信息，而不是只有一句「无法判断」。
    assert "notify_oncall" in blocked_cause.detail


def test_complete_recording_without_blocked_steps_still_reaches_a_real_verdict(monkeypatch) -> None:
    """把两种成因分开之后，正常的通过/失败不能被顺手算成 inconclusive。"""

    from types import SimpleNamespace
    from afr_server import cases

    stored = []
    monkeypatch.setattr(cases, "_store", lambda *args: stored.append(args))
    monkeypatch.setattr(cases, "get_events", lambda *a, **k: [])
    monkeypatch.setattr(cases, "final_output_of", lambda events: "ok")
    monkeypatch.setattr(
        cases,
        "execute_replay",
        lambda *a: SimpleNamespace(status="succeeded", complete=True, reason=None),
    )
    monkeypatch.setattr(
        cases,
        "evaluate_assertions",
        lambda *a: [SimpleNamespace(passed=False, model_dump=lambda mode=None: {"passed": False})],
    )
    cases._execute(None, "run-1", "case", {"assertions": [{"type": "no_error"}]})
    assert stored[0][2] == "failed"
    assert stored[0][4] is None


def test_case_status_is_recorded_when_agent_is_unavailable(client, make_run) -> None:
    """没有注册可重建的 Agent 时，用例应当明确失败，而不是永远停在"执行中"。"""

    client.post("/v1/ingest", json=make_run())
    created = client.post(
        "/v1/cases", json={"name": "case", "source_run_id": "run-1", "assertions": []}
    ).json()
    client.post(f"/v1/cases/{created['id']}/run", json={})

    result = _await_case(client, created["id"])
    assert result["last_status"] in {"failed", "error"}
    assert result["last_results"]


def _await_case(client, case_id: str, timeout: float = 15.0) -> dict:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.get(f"/v1/cases/{case_id}").json()
        if result["last_status"] != "running":
            return result
        time.sleep(0.1)
    raise AssertionError("用例执行没有在预期时间内结束")
