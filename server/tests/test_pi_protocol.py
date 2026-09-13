"""The real TypeScript serializer must satisfy Python ingestion and UI contracts."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_typescript_payload_is_ingested_and_pi_replay_is_local(client):
    root = Path(__file__).resolve().parents[2] / "integrations" / "pi"
    if not (root / "node_modules" / "tsx").exists() or not shutil.which("node"):
        pytest.skip("install integrations/pi dependencies to run cross-language contract")
    result = subprocess.run(
        ["node", "--import", "tsx", "test/protocol-fixture.ts"], cwd=root,
        capture_output=True, text=True, check=True, timeout=30,
    )
    payload = json.loads(result.stdout)
    from agent_flight_recorder.models import IngestRequest
    IngestRequest.model_validate(payload)
    response = client.post("/v1/ingest", json=payload)
    assert response.status_code == 200
    assert response.json()["accepted"] == 4
    run_id = payload["run"]["id"]
    detail = client.get(f"/v1/runs/{run_id}").json()
    assert detail["run"]["metadata"]["runtime"] == "pi"
    assert detail["summary"]["tokens_total"] == 12
    assert client.post(f"/v1/runs/{run_id}/replay", json={"from_seq": 1}).status_code == 409
