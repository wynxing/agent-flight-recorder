"""服务端内部直接写库的上报通道。

回放由服务端自己发起，结果没必要绕一圈 HTTP 再回到自己。
"""

from __future__ import annotations

from typing import Any

from agent_flight_recorder.models import IngestRequest, IngestResponse

from .db import session_scope
from .storage import ingest_batch


class DirectTransport:
    def send(self, payload: dict[str, Any]) -> IngestResponse:
        request = IngestRequest.model_validate(payload)
        with session_scope() as session:
            return ingest_batch(session, request)

