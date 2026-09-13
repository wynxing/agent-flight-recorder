"""入库：去重、脱敏、断点续传。"""

from __future__ import annotations


def test_ingest_stores_run_and_events(client, make_run) -> None:
    response = client.post("/v1/ingest", json=make_run())
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 4
    assert body["duplicates"] == 0
    assert body["max_seq"] == 4


def test_duplicate_events_are_counted_not_stored(client, make_run) -> None:
    payload = make_run()
    client.post("/v1/ingest", json=payload)

    again = client.post("/v1/ingest", json=payload).json()
    assert again["accepted"] == 0
    assert again["duplicates"] == 4
    assert len(client.get("/v1/runs/run-1/timeline").json()["events"]) == 4


def test_duplicates_within_one_batch_are_dropped(client, make_run) -> None:
    payload = make_run()
    payload["events"].append(dict(payload["events"][-1], id="dup"))
    body = client.post("/v1/ingest", json=payload).json()
    assert body["duplicates"] == 1
    assert body["accepted"] == 4


def test_resume_with_later_batch(client, make_run) -> None:
    client.post("/v1/ingest", json=make_run())

    follow_up = make_run()
    follow_up["events"] = [
        {
            "id": "run-1-e5",
            "run_id": "run-1",
            "seq": 5,
            "type": "run_finished",
            "started_at": "2026-09-13T00:00:06Z",
            "output": {"result": "done", "status": "succeeded"},
        }
    ]
    body = client.post("/v1/ingest", json=follow_up).json()
    assert body["accepted"] == 1
    assert body["max_seq"] == 5
    assert client.get("/v1/runs/run-1").json()["event_count"] == 5


def test_events_with_foreign_run_id_are_rejected(client, make_run) -> None:
    payload = make_run()
    payload["events"][0]["run_id"] = "other-run"
    body = client.post("/v1/ingest", json=payload).json()
    assert body["rejected"]
    assert body["rejected"][0]["reason"] == "run_id mismatch"
    assert body["accepted"] == 3


def test_secrets_are_redacted_on_ingest(client, make_run) -> None:
    payload = make_run()
    payload["events"][2]["input"] = {
        "args": {"password": "hunter2", "note": "sk-abcdefghijklmnopqrst"}
    }
    payload["run"]["metadata"] = {"api_key": "sk-zzzzzzzzzzzzzzzzzzzz"}
    client.post("/v1/ingest", json=payload)

    timeline = client.get("/v1/runs/run-1/timeline").json()
    args = timeline["events"][2]["input"]["args"]
    assert args["password"] == "[REDACTED:sensitive_key]"
    assert "[REDACTED:openai_key]" in args["note"]
    assert timeline["events"][2]["redactions"]

    run = client.get("/v1/runs/run-1").json()["run"]
    assert run["metadata"]["api_key"] == "[REDACTED:sensitive_key]"
    assert "sensitive_key" in run["redactions"]


def test_summary_is_recomputed_on_ingest(client, make_run) -> None:
    client.post("/v1/ingest", json=make_run())
    summary = client.get("/v1/runs/run-1").json()["summary"]
    assert summary["model_calls"] == 1
    assert summary["tool_calls"] == 1
    assert summary["tokens_total"] == 15
    assert summary["effect_counts"] == {"live": 2}


def test_parent_run_id_is_preserved(client, make_run) -> None:
    client.post("/v1/ingest", json=make_run("parent-run"))
    client.post("/v1/ingest", json=make_run("child-run", parent_run_id="parent-run", replay_from_seq=3))

    detail = client.get("/v1/runs/child-run").json()
    assert detail["run"]["parent_run_id"] == "parent-run"
    assert detail["run"]["replay_from_seq"] == 3
    assert detail["parent"]["id"] == "parent-run"
    children = client.get("/v1/runs/parent-run").json()["children"]
    assert [child["id"] for child in children] == ["child-run"]

