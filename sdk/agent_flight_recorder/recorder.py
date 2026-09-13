"""Recorder —— 录制链路的核心。

设计上只有一条硬约束：录制失败绝不允许打断或拖慢被观测的 Agent。
因此录制路径只做内存追加（带锁的 list append），网络上报全部交给后台线程，
任何异常都被吞掉并计入 stats。

代价是短生命周期脚本必须在退出前显式 flush()/close()，否则缓冲区内的事件会丢。
这种静默失效是录制管线最容易出、也最难发现的故障，所以 Recorder 暴露
stats.last_error，并提供 doctor() 做一次真实的端到端往返自检。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .client import DEFAULT_ENDPOINT, HttpTransport, NullTransport, Transport
from .models import (
    SDK_NAME,
    SDK_VERSION,
    EffectPolicy,
    EffectSource,
    ErrorInfo,
    Event,
    EventType,
    IngestRequest,
    RunRecord,
    RunStatus,
    SdkInfo,
    SideEffect,
    TokenUsage,
    new_id,
    utcnow,
)
from .serialization import to_jsonable

MAX_BUFFERED_EVENTS = 20000


@dataclass
class RecorderStats:
    events_recorded: int = 0
    events_dropped: int = 0
    batches_sent: int = 0
    batches_failed: int = 0
    last_error: str | None = None
    last_success_at: datetime | None = None

    @property
    def healthy(self) -> bool:
        return self.batches_failed == 0 and self.events_dropped == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "events_recorded": self.events_recorded,
            "events_dropped": self.events_dropped,
            "batches_sent": self.batches_sent,
            "batches_failed": self.batches_failed,
            "last_error": self.last_error,
            "healthy": self.healthy,
        }


class Recorder:
    """一次 Run 的录制器。

    典型用法::

        rec = Recorder(agent_name="sre-agent", model="gpt-5")
        rec.start(task="investigate checkout-api latency")
        ...   # 中间件在每一步把事件写进 rec
        rec.finish(result="...")
        rec.close()   # 必须：否则缓冲区里的事件会随进程退出丢失
    """

    def __init__(
        self,
        agent_name: str,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        agent_version: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        labels: dict[str, str] | None = None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        replay_from_seq: int | None = None,
        effect_policy: EffectPolicy | None = None,
        transport: Transport | None = None,
        enabled: bool = True,
        flush_interval: float = 1.0,
        max_batch_size: int = 200,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.endpoint = endpoint
        self.agent_version = agent_version
        self.model = model
        self.prompt_version = prompt_version
        self.labels = dict(labels or {})
        self.metadata = dict(metadata or {})
        self.effect_policy = effect_policy
        self.parent_run_id = parent_run_id
        self.replay_from_seq = replay_from_seq

        self.enabled = enabled
        self.stats = RecorderStats()
        self._on_error = on_error
        self._flush_interval = max(0.05, flush_interval)
        self._max_batch_size = max(1, max_batch_size)

        self._transport: Transport = transport or (
            HttpTransport(endpoint) if enabled else NullTransport()
        )
        self._lock = threading.Lock()
        self._buffer: list[Event] = []
        self._seq = 0
        self._closed = False

        self.run = RunRecord(
            id=run_id or new_id(),
            agent_name=agent_name,
            agent_version=agent_version,
            model=model,
            status=RunStatus.RUNNING,
            parent_run_id=parent_run_id,
            replay_from_seq=replay_from_seq,
            effect_policy=effect_policy,
            prompt_version=prompt_version,
            labels=self.labels,
            metadata=self.metadata,
            started_at=utcnow(),
        )

        self._worker: threading.Thread | None = None
        if enabled and flush_interval > 0:
            self._worker = threading.Thread(
                target=self._flush_loop,
                name=f"afr-flush-{self.run.id[:8]}",
                daemon=True,
            )
            self._worker.start()

    # ---------------------------------------------------------------- 生命周期

    @property
    def run_id(self) -> str:
        return self.run.id

    @property
    def closed(self) -> bool:
        return self._closed

    def start(
        self,
        *,
        task: str | None = None,
        input: dict[str, Any] | None = None,
        **attributes: Any,
    ) -> str:
        """发出 run_started 事件并返回 run_id。"""

        payload: dict[str, Any] = {}
        if task is not None:
            payload["task"] = to_jsonable(task)
        if input is not None:
            payload["input"] = to_jsonable(input)
        self.record(EventType.RUN_STARTED, input=payload or None, **attributes)
        return self.run.id

    def finish(
        self,
        *,
        result: Any = None,
        status: RunStatus = RunStatus.SUCCEEDED,
        **attributes: Any,
    ) -> None:
        self.run.status = status
        self.run.ended_at = utcnow()
        self.record(
            EventType.RUN_FINISHED,
            output={"result": to_jsonable(result), "status": status.value},
            **attributes,
        )

    def fail(self, error: BaseException | str, **attributes: Any) -> None:
        self.record_error(error, **attributes)
        self.finish(status=RunStatus.FAILED)

    # ---------------------------------------------------------------- 事件写入

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def record(
        self,
        event_type: EventType,
        *,
        name: str | None = None,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        error: ErrorInfo | None = None,
        tokens: TokenUsage | None = None,
        cost_usd: float | None = None,
        side_effect: SideEffect | None = None,
        effect_source: EffectSource | None = None,
        parent_seq: int | None = None,
        source_seq: int | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        duration_ms: float | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Event | None:
        """追加一个事件。任何失败都只记录在 stats 里，绝不向上抛。"""

        try:
            if self._closed:
                return None
            event = Event(
                id=new_id(),
                run_id=self.run.id,
                seq=self.next_seq(),
                type=event_type,
                parent_seq=parent_seq,
                source_seq=source_seq,
                name=name,
                started_at=started_at or utcnow(),
                ended_at=ended_at,
                duration_ms=duration_ms,
                input=input,
                output=output,
                error=error,
                tokens=tokens,
                cost_usd=cost_usd,
                side_effect=side_effect,
                effect_source=effect_source,
                attributes=dict(attributes or {}),
            )
            with self._lock:
                self._buffer.append(event)
                self._trim_locked()
            self.stats.events_recorded += 1
            return event
        except Exception as exc:  # noqa: BLE001 - 录制永远不能打断 Agent
            self._note_error(exc)
            return None

    def record_error(self, error: BaseException | str, **attributes: Any) -> Event | None:
        if isinstance(error, BaseException):
            info = ErrorInfo(
                type=type(error).__name__,
                message=str(error),
                stack=format_traceback(error),
            )
        else:
            info = ErrorInfo(type="Error", message=str(error))
        return self.record(EventType.ERROR, error=info, attributes=dict(attributes))

    def record_state_snapshot(self, state: Any, **attributes: Any) -> Event | None:
        return self.record(
            EventType.STATE_SNAPSHOT,
            output={"state": to_jsonable(state)},
            attributes=dict(attributes),
        )

    # ---------------------------------------------------------------- 上报

    def flush(self) -> bool:
        """把缓冲区里的一个批次发出去。返回是否成功。永不抛异常。"""

        try:
            events = self._drain()
            if not events:
                return True
            return self._send(events)
        except Exception as exc:  # noqa: BLE001
            self._note_error(exc)
            return False

    def close(self, *, drain_timeout: float = 5.0) -> None:
        """停止后台线程并尽力把剩余事件发完。"""

        if self._closed:
            return
        self._closed = True
        if self._worker is not None:
            self._worker.join(timeout=drain_timeout)

        deadline = time.monotonic() + drain_timeout
        while True:
            with self._lock:
                pending = bool(self._buffer)
            if not pending or time.monotonic() >= deadline:
                break
            if not self.flush():
                break

    def __enter__(self) -> "Recorder":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None and self.run.status is RunStatus.RUNNING:
            self.run.status = RunStatus.FAILED
            self.run.ended_at = utcnow()
            self.record_error(exc)
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
        return False

    # ---------------------------------------------------------------- 内部

    def _drain(self) -> list[Event]:
        with self._lock:
            if not self._buffer:
                return []
            batch = self._buffer[: self._max_batch_size]
            del self._buffer[: len(batch)]
            return batch

    def _send(self, events: list[Event]) -> bool:
        payload = IngestRequest(
            sdk=SdkInfo(name=SDK_NAME, version=SDK_VERSION),
            run=self.run,
            events=events,
        ).model_dump(mode="json")

        try:
            self._transport.send(payload)
        except Exception as exc:  # noqa: BLE001 - 网络问题不能影响 Agent
            self.stats.batches_failed += 1
            self._requeue(events)
            self._note_error(exc)
            return False

        self.stats.batches_sent += 1
        self.stats.last_success_at = utcnow()
        return True

    def _requeue(self, events: list[Event]) -> None:
        with self._lock:
            self._buffer[0:0] = events
            self._trim_locked()

    def _trim_locked(self) -> None:
        overflow = len(self._buffer) - MAX_BUFFERED_EVENTS
        if overflow > 0:
            del self._buffer[:overflow]
            self.stats.events_dropped += overflow

    def _flush_loop(self) -> None:
        while not self._closed:
            time.sleep(self._flush_interval)
            try:
                if self._buffer:
                    self.flush()
            except Exception as exc:  # noqa: BLE001
                self._note_error(exc)

    def _note_error(self, exc: BaseException) -> None:
        self.stats.last_error = f"{type(exc).__name__}: {exc}"
        if self._on_error is not None:
            try:
                self._on_error(exc)
            except Exception:  # noqa: BLE001
                pass


def format_traceback(error: BaseException) -> str:
    import traceback

    return "".join(traceback.format_exception(type(error), error, error.__traceback__))[-8000:]


@dataclass
class DoctorReport:
    """doctor() 的结果。这是一次真实往返，不是配置检查。"""

    ok: bool
    endpoint: str
    checks: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "endpoint": self.endpoint, "checks": self.checks}


def doctor(endpoint: str = DEFAULT_ENDPOINT, *, timeout: float = 5.0) -> DoctorReport:
    """端到端自检：真的发一条事件出去，并确认服务端接受。

    为什么需要它：凭证文件存在、进程里 import 不进 SDK 这类故障，任何基于
    配置存在性的检查都会通过。只有真实往返能证明录制链路是活的。
    """

    checks: list[dict[str, Any]] = []
    base = endpoint.rstrip("/")

    try:
        with urllib.request.urlopen(f"{base}/v1/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        health_ok = bool(payload.get("ok"))
        checks.append({"name": "health", "ok": health_ok, "detail": payload})
    except Exception as exc:  # noqa: BLE001
        checks.append({"name": "health", "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
        return DoctorReport(ok=False, endpoint=base, checks=checks)

    recorder = Recorder(
        "afr-doctor",
        endpoint=base,
        agent_version=SDK_VERSION,
        enabled=True,
        flush_interval=0.0,
        metadata={"doctor": True},
    )
    recorder.start(task="afr self check")
    recorder.finish(result="ok")
    ok = recorder.flush()
    recorder.close(drain_timeout=1.0)

    checks.append(
        {
            "name": "ingest_roundtrip",
            "ok": ok,
            "detail": recorder.stats.as_dict(),
        }
    )
    return DoctorReport(ok=health_ok and ok, endpoint=base, checks=checks)
