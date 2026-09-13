"""服务端不再自造成因名，历史自由文本 reason 也不会导致崩溃或错误分类。

对应 issue #6 验收 5（服务端与 SDK 共用分类）与验收 7（向后兼容）。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_flight_recorder.models import RunStatus, utcnow
from agent_flight_recorder.replay.engine import ReplayPlan, ReplayResult


def test_server_recording_loss_uses_the_shared_code(monkeypatch, client, make_run) -> None:
    """记录器丢事件时，服务端用的是共享分类的 ``recording_loss``。

    以前这里是自造的 ``replay_recording_loss``——同一份「不完整」概念在三个地方
    有三个名字，调用方无法稳定判定。
    """

    client.post("/v1/ingest", json=make_run())

    from afr_server import replay_runner, storage

    class _Stats:
        events_dropped = 3
        batches_failed = 1

    class _Recorder:
        def __init__(self, *args, **kwargs) -> None:
            self.stats = _Stats()
            self.run_id = kwargs.get("run_id", "replay-run")
            # apply_plan_to_recorder 会往 Run 头上写父子关系，这里给一个最小的替身。
            self.run = SimpleNamespace(
                parent_run_id=None, replay_from_seq=None, effect_policy=None,
                agent_version=None, prompt_version=None,
            )

        def close(self, *args, **kwargs) -> None:
            return None

    monkeypatch.setattr(replay_runner, "Recorder", _Recorder)
    # apply_plan_to_recorder 会往 Run 头上写父子关系；替身已经带了 run，因此无需真的落库。
    monkeypatch.setattr(
        replay_runner, "resolve_agent_spec",
        lambda name: SimpleNamespace(build=lambda **kwargs: None, tool_side_effects={}),
    )
    monkeypatch.setattr(
        replay_runner, "run_replay",
        lambda **kwargs: ReplayResult(
            parent_run_id="run-1", from_seq=1, policy=ReplayPlan.reproduce("run-1", 1).policy,
            status=RunStatus.SUCCEEDED.value,
        ),
    )

    plan = ReplayPlan.reproduce("run-1", 1)
    # 回放 Run 在真正执行之前已经存在（这也是 UI 立刻可见的来源）。
    replay_runner.prepare_run(plan, "replay-run")
    result = replay_runner.execute_replay(plan, "replay-run")
    assert result.complete is False
    assert result.reason is not None
    assert result.reason.code == "recording_loss"
    assert result.reason.code != "replay_recording_loss"

    detail = client.get("/v1/runs/replay-run").json()
    assert detail["replay"]["complete"] is False
    assert detail["replay"]["cause"]["code"] == "recording_loss"


def test_historical_free_text_reason_is_structured_and_preserved(client, make_run) -> None:
    """历史数据里已有的自由文本 reason：认得出码的认码，认不出的保留原文。"""

    client.post("/v1/ingest", json=make_run("legacy-known", metadata={
        "afr_replay": {"complete": False, "reason": "replay_recording_loss"},
    }))
    known = client.get("/v1/runs/legacy-known").json()["replay"]
    assert known["cause"]["code"] == "recording_loss"

    client.post("/v1/ingest", json=make_run("legacy-novel", metadata={
        "afr_replay": {"complete": False, "reason": "something nobody classified"},
    }))
    novel = client.get("/v1/runs/legacy-novel").json()["replay"]
    # 未知取值必须落到「未知成因」，且原文不能丢。
    assert novel["cause"]["code"] == "unknown"
    assert novel["cause"]["detail"] == "something nobody classified"


def test_historical_sentence_with_colon_is_split_not_used_as_a_code(client, make_run) -> None:
    """「码 + 冒号 + 英文长句」的历史写法被拆开：码是码，说明是说明。"""

    client.post("/v1/ingest", json=make_run("legacy-split", metadata={
        "afr_replay": {
            "complete": False,
            "reason": "incomplete_recording: missing boundary or event sequence gap",
        },
    }))
    meta = client.get("/v1/runs/legacy-split").json()["replay"]
    assert meta["cause"]["code"] == "incomplete_recording"
    assert meta["cause"]["detail"] == "missing boundary or event sequence gap"


def test_historical_payload_without_a_reason_does_not_crash(client, make_run) -> None:
    """连 reason 都没有的历史载荷也只是「未分类」，不能报错。"""

    client.post("/v1/ingest", json=make_run("legacy-bare", metadata={
        "afr_replay": {"complete": False},
    }))
    meta = client.get("/v1/runs/legacy-bare").json()["replay"]
    assert meta["cause"]["code"] == "unknown"


def test_completed_replay_has_no_cause(client, make_run) -> None:
    """顺利完成、给出结论的回放不该带成因，避免把「有结论」误显示成「无法判断」。"""

    client.post("/v1/ingest", json=make_run("ok-run", metadata={
        "afr_replay": {"complete": True, "verdict": "succeeded", "reason": None},
    }))
    meta = client.get("/v1/runs/ok-run").json()["replay"]
    assert meta["cause"] is None


def test_case_stored_legacy_cause_is_still_readable(client, make_run) -> None:
    """用例行里若是旧的自由文本成因，读取路径也必须兼容（不崩、不误分类）。"""

    client.post("/v1/ingest", json=make_run())
    created = client.post(
        "/v1/cases", json={"name": "case", "source_run_id": "run-1", "assertions": []}
    ).json()

    from afr_server.db import session_scope
    from afr_server.tables import CaseTable

    with session_scope() as session:
        row = session.get(CaseTable, created["id"])
        row.last_status = "inconclusive"
        row.last_cause = "no_recording: step 2, tool_call read"
        session.add(row)

    item = client.get(f"/v1/cases/{created['id']}").json()
    assert item["last_status"] == "inconclusive"
    assert item["last_cause"]["code"] == "missing_recorded_response"


def test_case_cause_column_is_added_to_an_existing_database(tmp_path, monkeypatch) -> None:
    """已有库缺列时也要能启动：轻量迁移补齐 cases.last_cause。"""

    import sqlite3

    from sqlalchemy import create_engine, inspect

    db_path = tmp_path / "old.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE cases (id VARCHAR PRIMARY KEY, name VARCHAR, last_status VARCHAR)"
    )
    connection.commit()
    connection.close()

    import afr_server.db as db

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    db.init_db()

    columns = {column["name"] for column in inspect(engine).get_columns("cases")}
    assert "last_cause" in columns
