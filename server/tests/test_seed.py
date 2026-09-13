"""播种链路：开箱即有数据，且自带一条开局即失败的回归用例。"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture()
def seeded_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """一个真的会播种的客户端。

    conftest 的 app_env 默认关掉播种（否则每个测试都要等一次 seed），
    这里显式打开——本文件测的就是播种本身。
    """

    monkeypatch.setenv("AFR_DB_PATH", str(tmp_path / "afr-seed.db"))
    monkeypatch.setenv("AFR_SEED_ON_STARTUP", "true")

    from afr_server import config, db

    config.get_settings.cache_clear()
    db.reset_engine()
    db.init_db()

    from afr_server.main import app
    from fastapi.testclient import TestClient

    try:
        with TestClient(app) as client:
            yield client
    finally:
        db.reset_engine()
        config.get_settings.cache_clear()


def _wait_for_seeded_case(client: Any, timeout: float = 90.0) -> dict[str, Any]:
    """播种跑在后台线程里，用例拿到结论才说明 run 与 case 都已经落库。"""

    deadline = time.time() + timeout
    while time.time() < deadline:
        cases = client.get("/v1/cases").json()["cases"]
        if cases and cases[0]["last_status"] not in (None, "running"):
            return cases[0]
        time.sleep(0.2)
    raise AssertionError(f"播种未在 {timeout:.0f}s 内产出用例结论")


def test_seed_creates_a_single_parent_run(seeded_client: Any) -> None:
    _wait_for_seeded_case(seeded_client)

    runs = seeded_client.get("/v1/runs").json()["runs"]
    parents = [item for item in runs if item["run"]["parent_run_id"] is None]

    assert len(parents) == 1
    assert parents[0]["run"]["labels"]["seed"] == "true"


def test_seed_creates_exactly_one_case(seeded_client: Any) -> None:
    case = _wait_for_seeded_case(seeded_client)

    assert len(seeded_client.get("/v1/cases").json()["cases"]) == 1
    assert case["from_seq"] == 15
    assert case["labels"]["seed"] == "true"
    assert case["assertions"] == [
        {
            "type": "final_output_contains",
            "value": "REDIS_POOL_SIZE",
            "tool": None,
            "args_contains": None,
            "note": "",
        }
    ]


def test_seeded_case_starts_red(seeded_client: Any) -> None:
    """自带用例必须是失败的：它证明的是"按原样重跑，问题仍然复现"。"""

    case = _wait_for_seeded_case(seeded_client)

    assert case["last_status"] == "failed"
    assert case["last_results"]
    assert all(not item["passed"] for item in case["last_results"])
    assert any("REDIS_POOL_SIZE" in item["detail"] for item in case["last_results"])


def test_seeded_case_turns_green_with_the_grounded_prompt(seeded_client: Any) -> None:
    """同一条断言，换 Prompt 就通过：红绿翻转是真实发生的行为差异。"""

    case = _wait_for_seeded_case(seeded_client)
    grounded = seeded_client.get("/v1/agents").json()[0]["prompt_presets"]["grounded"]

    run_id = seeded_client.post(
        f"/v1/cases/{case['id']}/run", json={"system_prompt": grounded}
    ).json()["run_id"]

    deadline = time.time() + 90
    reread: dict[str, Any] = {}
    while time.time() < deadline:
        reread = seeded_client.get(f"/v1/cases/{case['id']}").json()
        if reread["last_run_id"] == run_id and reread["last_status"] != "running":
            break
        time.sleep(0.2)

    assert reread["last_run_id"] == run_id, "换 Prompt 的回放没有落到用例上"
    assert reread["last_status"] == "passed"
    assert all(item["passed"] for item in reread["last_results"])
