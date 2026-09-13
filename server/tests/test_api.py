"""API 契约：列表、时间线、OTel 导出、回放前置校验。"""

from __future__ import annotations




def test_health_and_agents(client, make_run) -> None:
    assert client.get("/v1/health").json()["ok"] is True
    assert isinstance(client.get("/v1/agents").json(), list)


def test_runs_list_and_filters(client, make_run) -> None:
    client.post("/v1/ingest", json=make_run("root-1"))
    client.post("/v1/ingest", json=make_run("child-1", parent_run_id="root-1", replay_from_seq=2))

    all_runs = client.get("/v1/runs").json()
    assert all_runs["total"] == 2
    assert {item["is_replay"] for item in all_runs["runs"]} == {True, False}

    roots = client.get("/v1/runs?roots_only=true").json()
    assert [item["run"]["id"] for item in roots["runs"]] == ["root-1"]

    by_agent = client.get("/v1/runs?agent_name=demo-agent").json()
    assert by_agent["total"] == 2

    none_found = client.get("/v1/runs?agent_name=other").json()
    assert none_found["total"] == 0


def test_timeline_is_ordered_and_supports_after_seq(client, make_run) -> None:
    client.post("/v1/ingest", json=make_run())
    events = client.get("/v1/runs/run-1/timeline").json()["events"]
    assert [event["seq"] for event in events] == [1, 2, 3, 4]

    tail = client.get("/v1/runs/run-1/timeline?after_seq=2").json()["events"]
    assert [event["seq"] for event in tail] == [3, 4]


def test_missing_run_returns_404(client) -> None:
    assert client.get("/v1/runs/nope").status_code == 404
    assert client.get("/v1/runs/nope/timeline").status_code == 404
    assert client.get("/v1/runs/nope/otel").status_code == 404


def test_otel_export_uses_genai_semantics(client, make_run) -> None:
    client.post("/v1/ingest", json=make_run())
    spans = client.get("/v1/runs/run-1/otel").json()["spans"]
    operations = [span["attributes"].get("gen_ai.operation.name") for span in spans]
    assert "chat" in operations
    assert "execute_tool" in operations
    chat = next(span for span in spans if span["attributes"].get("gen_ai.operation.name") == "chat")
    assert chat["attributes"]["gen_ai.usage.input_tokens"] == 10
    assert chat["attributes"]["gen_ai.request.model"] == "test-model"


def test_replay_requires_registered_agent(client, make_run) -> None:
    client.post("/v1/ingest", json=make_run())
    response = client.post("/v1/runs/run-1/replay", json={"from_seq": 2})
    assert response.status_code == 409
    assert "afr.agents" in response.json()["detail"]


def test_replay_rejects_out_of_range_step(client, make_run) -> None:
    client.post("/v1/ingest", json=make_run())
    response = client.post("/v1/runs/run-1/replay", json={"from_seq": 99})
    assert response.status_code == 400
    assert "超出" in response.json()["detail"]


def test_replay_on_missing_run_returns_404(client) -> None:
    assert client.post("/v1/runs/nope/replay", json={"from_seq": 1}).status_code == 404


def test_diff_endpoint_compares_two_runs(client, make_run) -> None:
    client.post("/v1/ingest", json=make_run("a"))
    client.post("/v1/ingest", json=make_run("b"))

    result = client.get("/v1/diff", params={"a": "a", "b": "b"}).json()
    assert result["tools"][0]["status"] == "same"
    assert result["final_output"]["changed"] is False
    assert result["first_divergence"] is None

    assert client.get("/v1/diff", params={"a": "a", "b": "missing"}).status_code == 404


