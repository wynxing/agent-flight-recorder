"""上报传输层。

刻意只依赖标准库：SDK 会被装进用户的 Agent 进程里，不能因为要上报就拖进一个
HTTP 客户端依赖。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol

from .models import PROTOCOL_VERSION, IngestResponse

DEFAULT_ENDPOINT = "http://127.0.0.1:7710"


class Transport(Protocol):
    def send(self, payload: dict[str, Any]) -> IngestResponse: ...


class HttpTransport:
    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, *, timeout: float = 5.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    @property
    def url(self) -> str:
        return f"{self.endpoint}/v1/ingest"

    def send(self, payload: dict[str, Any]) -> IngestResponse:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-AFR-Protocol": str(PROTOCOL_VERSION),
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read().decode("utf-8")
        return IngestResponse.model_validate_json(raw)

    def __repr__(self) -> str:  # pragma: no cover - 便于排查
        return f"HttpTransport(endpoint={self.endpoint!r})"


class NullTransport:
    """丢弃一切。用于测试与显式关闭上报的本地录制。"""

    def send(self, payload: dict[str, Any]) -> IngestResponse:
        run_id = payload.get("run", {}).get("id", "")
        events = payload.get("events", [])
        max_seq = max((event.get("seq", 0) for event in events), default=0)
        return IngestResponse(run_id=run_id, accepted=len(events), max_seq=max_seq)


class FailingTransport:
    """永远失败。用来证明录制链路出错时不会打断 Agent。"""

    def send(self, payload: dict[str, Any]) -> IngestResponse:
        raise ConnectionError("simulated ingest failure")


def is_connection_refused(error: BaseException) -> bool:
    if isinstance(error, urllib.error.URLError):
        reason = getattr(error, "reason", None)
        return isinstance(reason, ConnectionError) or "refused" in str(reason).lower()
    return False

