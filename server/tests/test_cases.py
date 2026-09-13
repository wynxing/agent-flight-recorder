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
