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


def test_error_verdict_carries_a_cause(monkeypatch) -> None:
    """契约：结论不是 passed / failed 时必须带成因，error 也不例外。

    error 的成因回答的是「为什么没有可信结论」——这次回放本身就没跑成功。它与
    「录制不完整」「副作用被拦截」是三类不同的问题，因此同样不能没有成因。
    """

    from types import SimpleNamespace

    from afr_server import cases

    stored = []
    monkeypatch.setattr(cases, "_store", lambda *args: stored.append(args))
    monkeypatch.setattr(cases, "get_events", lambda *a, **k: [])
    monkeypatch.setattr(cases, "final_output_of", lambda events: "boom")
    monkeypatch.setattr(
        cases,
        "execute_replay",
        lambda *a: SimpleNamespace(status="failed", complete=True, reason=None),
    )
    cases._execute(None, "run-1", "case", {"assertions": [{"type": "no_error"}]})

    verdict, cause = stored[0][2], stored[0][4]
    assert verdict == "error"
    assert cause is not None
    assert cause.code == "unknown"
    # 成因要能让人看出是执行本身失败，而不是录制质量或副作用策略。
    assert "failed" in cause.detail


def test_hard_failure_carries_a_cause_too(monkeypatch) -> None:
    """执行直接抛异常时（error 结论的另一条来源）同样要留下成因。"""

    from afr_server import cases

    stored = []
    monkeypatch.setattr(cases, "_store", lambda *args: stored.append(args))

    def _explode(*args):
        raise RuntimeError("agent 进程挂了")

    monkeypatch.setattr(cases, "_execute", _explode)
    cases._job(None, "run-1", "case", {"assertions": []})

    verdict, cause = stored[0][2], stored[0][4]
    assert verdict == "error"
    assert cause is not None
    assert cause.code == "unknown"
    assert "RuntimeError" in cause.detail


def test_passed_verdict_has_no_cause(monkeypatch) -> None:
    """passed / failed 是「有结论」，因此不带成因——成因只解释为什么没有可信结论。"""

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
        lambda *a: [SimpleNamespace(passed=True, model_dump=lambda mode=None: {"passed": True})],
    )
    cases._execute(None, "run-1", "case", {"assertions": [{"type": "no_error"}]})

    assert stored[0][2] == "passed"
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


def test_a_cases_record_is_committed_as_a_whole_not_column_by_column(client, make_run, monkeypatch) -> None:
    """用例行的「最近一次结论」必须整条落库，不能按列合并。

    批量运行里同一条用例会被多个条件并发执行，两个格子都要写这一行。如果写入是
    「读出行对象 -> 改属性 -> 让 ORM 只提交与它读到的那份快照相比有变化的列」，那么在
    对方写过之后才提交的那一方会**漏掉自己没改动的列**：它那份旧快照里恰好已经等于
    新值的列会被留在原处，于是最终落库的是「A 这次执行的 run_id + B 那次执行的条件」。
    用例页承诺「最近一次条件」与「最近一次执行」属于同一次执行，这种组合直接违背它
    （CI 上真实出现过：last_run_id 落在 default 格子，last_condition 却是 grounded）。

    这里不需要真的开线程：并发在这个问题上的本质，就是「提交之前先读到了一份旧快照」。
    格子 B 在它自己的会话里先把这一行读出来（这正是并发时的读），格子 A 随后写完整条，
    格子 B 再提交——按列合并的写法此时会漏掉 last_run_id。
    """

    import contextlib

    from afr_server import cases
    from afr_server.db import get_engine
    from afr_server.storage import get_case
    from agent_flight_recorder.models import ReplayPreset
    from sqlmodel import Session

    client.post("/v1/ingest", json=make_run())
    case_id = client.post(
        "/v1/cases", json={"name": "case", "source_run_id": "run-1", "assertions": []}
    ).json()["id"]

    # 格子 B 的准备阶段：这一行于是带着 B 的 run_id、且还没有结论。
    # 返回的第四项是这次执行显式给出的覆盖（issue #24 的「有效定义」）：预设就是覆盖的一种，
    # 因此这里必须真的传下去了——否则「最近一次结论按什么判的」会漏掉它。
    run_b, _, _, overrides_b = cases._prepare_case_run(
        case_id, from_seq=1, preset=ReplayPreset.REGRESS
    )
    assert overrides_b == {"from_seq": 1, "preset": "regress"}

    # B 在自己的会话里读这一行——并发时，这就是「读在对方写之前」。
    session_b = Session(get_engine(), expire_on_commit=False)
    loaded = get_case(session_b, case_id)
    assert loaded is not None
    assert loaded.last_run_id == run_b
    session_b.commit()  # 结束这次读，但保留已经读到的那份快照

    # 格子 A 先跑完，把它那一次执行整条写下去。
    run_a, _, _, _ = cases._prepare_case_run(case_id, from_seq=1, preset=ReplayPreset.REPRODUCE)
    cases._store(case_id, run_a, "failed", [], None, {"prompt": "default"})

    # 格子 B 随后用自己那份快照提交自己的结论。
    real_scope = cases.session_scope

    @contextlib.contextmanager
    def b_scope():
        yield session_b
        session_b.commit()

    monkeypatch.setattr(cases, "session_scope", b_scope)
    try:
        cases._store(case_id, run_b, "passed", [], None, {"prompt": "grounded"})
    finally:
        session_b.close()
        monkeypatch.setattr(cases, "session_scope", real_scope)

    recorded = client.get(f"/v1/cases/{case_id}").json()
    pair = (recorded["last_run_id"], (recorded["last_condition"] or {}).get("prompt"))

    # 不变量：这一对必须来自同一次执行，不能是两次执行各出一半。
    assert pair in {(run_a, "default"), (run_b, "grounded")}
    # B 是最后提交的那一格，因此「最近一次结论」整条都是 B 的。
    assert pair == (run_b, "grounded")


def _await_case(client, case_id: str, timeout: float = 15.0) -> dict:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.get(f"/v1/cases/{case_id}").json()
        if result["last_status"] != "running":
            return result
        time.sleep(0.1)
    raise AssertionError("用例执行没有在预期时间内结束")
