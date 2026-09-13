"""录制链路最硬的一条约束：录制失败绝不允许打断被观测的 Agent。"""

from __future__ import annotations

from typing import Any

from agent_flight_recorder.client import FailingTransport, NullTransport
from agent_flight_recorder.models import EventType, RunStatus, summarize_events
from agent_flight_recorder.recorder import Recorder


class CapturingTransport:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]):
        from agent_flight_recorder.models import IngestResponse

        self.payloads.append(payload)
        events = payload.get("events", [])
        return IngestResponse(
            run_id=payload["run"]["id"],
            accepted=len(events),
            max_seq=events[-1]["seq"] if events else 0,
        )

    @property
    def events(self) -> list[dict[str, Any]]:
        return [event for payload in self.payloads for event in payload.get("events", [])]


def test_records_lifecycle_events() -> None:
    transport = CapturingTransport()
    recorder = Recorder("demo", transport=transport, flush_interval=0.0)
    recorder.start(task="investigate")
    recorder.record_state_snapshot({"step": 1})
    recorder.finish(result="done")
    assert recorder.flush() is True
    recorder.close()

    types = [event["type"] for event in transport.events]
    assert types == ["run_started", "state_snapshot", "run_finished"]
    assert [event["seq"] for event in transport.events] == [1, 2, 3]
    assert transport.payloads[0]["run"]["status"] == RunStatus.SUCCEEDED.value


def test_transport_failure_never_raises() -> None:
    recorder = Recorder("demo", transport=FailingTransport(), flush_interval=0.0)
    recorder.start(task="x")
    # 关键断言：上报失败时调用方完全感知不到，只有 stats 里留下痕迹。
    assert recorder.flush() is False
    recorder.close(drain_timeout=0.2)
    assert recorder.stats.batches_failed >= 1
    assert recorder.stats.last_error is not None
    assert recorder.stats.healthy is False


def test_failed_batch_is_requeued_for_retry() -> None:
    class FlakyTransport(CapturingTransport):
        def __init__(self) -> None:
            super().__init__()
            self.fail_first = True

        def send(self, payload: dict[str, Any]):
            if self.fail_first:
                self.fail_first = False
                raise ConnectionError("transient")
            return super().send(payload)

    transport = FlakyTransport()
    recorder = Recorder("demo", transport=transport, flush_interval=0.0)
    recorder.start(task="x")
    assert recorder.flush() is False
    assert recorder.flush() is True
    recorder.close()
    assert [event["type"] for event in transport.events] == ["run_started"]


def test_close_drains_buffer() -> None:
    transport = CapturingTransport()
    recorder = Recorder("demo", transport=transport, flush_interval=0.0)
    recorder.start(task="x")
    recorder.finish(result="y")
    recorder.close()
    assert len(transport.events) == 2


def test_records_after_close_are_ignored_not_raised() -> None:
    recorder = Recorder("demo", transport=NullTransport(), flush_interval=0.0)
    recorder.close()
    assert recorder.record(EventType.MODEL_CALL, name="m") is None


def test_record_error_captures_traceback() -> None:
    recorder = Recorder("demo", transport=NullTransport(), flush_interval=0.0)
    try:
        raise ValueError("boom")
    except ValueError as exc:
        event = recorder.record_error(exc)
    assert event is not None
    assert event.error is not None
    assert event.error.type == "ValueError"
    assert "boom" in (event.error.stack or "")


def test_context_manager_marks_failed_run() -> None:
    transport = CapturingTransport()
    try:
        with Recorder("demo", transport=transport, flush_interval=0.0) as recorder:
            recorder.start(task="x")
            raise RuntimeError("agent exploded")
    except RuntimeError:
        pass
    types = [event["type"] for event in transport.events]
    assert "error" in types
    assert transport.payloads[-1]["run"]["status"] == RunStatus.FAILED.value


def test_summary_matches_recorded_events() -> None:
    recorder = Recorder("demo", transport=NullTransport(), flush_interval=0.0)
    recorder.start(task="x")
    recorder.record(EventType.MODEL_CALL, name="m")
    recorder.finish(result="y")
    with recorder._lock:
        events = list(recorder._buffer)
    assert summarize_events(events).model_calls == 1

