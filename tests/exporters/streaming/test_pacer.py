"""Tests for streaming pacer: resolve_clock and pace_events."""

from __future__ import annotations

import pytest

from fabulexa_export.config.models import ClockConfig
from fabulexa_export.errors import ClockSpeedUnresolvable, ExporterError
from fabulexa_export.exporters.streaming.pacer import (
    ResolvedClock,
    pace_events,
    resolve_clock,
)
from fabulexa_export.exporters.streaming.types import StreamEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_event(seq: int, event_sim_time: int) -> StreamEvent:
    """Build a minimal StreamEvent for pacing tests."""
    return StreamEvent(
        seq=seq,
        op="c",
        kind="patient",
        record_id=f"r{seq}",
        presentation_id=None,
        event_sim_time=event_sim_time,
        ts=event_sim_time,
        after={"id": f"r{seq}"},
        topic="patients",
        route_table="patient",
    )


def realtime_clock(
    speed: float = 60.0, idle_cap_seconds: float | None = None
) -> ClockConfig:
    """Build a realtime ClockConfig."""
    data: dict[str, object] = {"mode": "realtime", "speed": speed}
    if idle_cap_seconds is not None:
        data["idle_cap_seconds"] = idle_cap_seconds
    return ClockConfig.model_validate(data)


def fast_clock() -> ClockConfig:
    """Build a fast ClockConfig."""
    return ClockConfig.model_validate({"mode": "fast"})


# ---------------------------------------------------------------------------
# resolve_clock truth table
# ---------------------------------------------------------------------------


def test_resolve_clock_absent_config_no_cli_returns_none() -> None:
    """Absent config + no CLI ⇒ None (fast)."""
    result = resolve_clock(None, None, None, False)
    assert result is None


def test_resolve_clock_fast_config_no_cli_returns_none() -> None:
    """Fast config + no CLI ⇒ None (fast)."""
    result = resolve_clock(fast_clock(), None, None, False)
    assert result is None


def test_resolve_clock_realtime_config_no_cli_returns_resolved() -> None:
    """Realtime config + no CLI ⇒ ResolvedClock(speed, cap)."""
    cfg = realtime_clock(speed=10.0, idle_cap_seconds=5.0)
    result = resolve_clock(cfg, None, None, False)
    assert result == ResolvedClock(speed=10.0, idle_cap_seconds=5.0)


def test_resolve_clock_fast_flag_over_realtime_returns_none() -> None:
    """`--fast` over realtime config ⇒ None."""
    cfg = realtime_clock(speed=10.0)
    result = resolve_clock(cfg, None, None, True)
    assert result is None


def test_resolve_clock_fast_plus_speed_returns_resolved() -> None:
    """fast config + `--speed S` ⇒ ResolvedClock(S, uncapped)."""
    result = resolve_clock(None, 30.0, None, False)
    assert result == ResolvedClock(speed=30.0, idle_cap_seconds=None)


def test_resolve_clock_fast_plus_speed_and_cap_returns_resolved() -> None:
    """fast config + `--speed S --idle-cap C` ⇒ ResolvedClock(S, C)."""
    result = resolve_clock(None, 30.0, 2.0, False)
    assert result == ResolvedClock(speed=30.0, idle_cap_seconds=2.0)


def test_resolve_clock_fast_plus_idle_cap_only_raises() -> None:
    """fast config + `--idle-cap C` only (no speed) ⇒ ClockSpeedUnresolvable."""
    with pytest.raises(ClockSpeedUnresolvable):
        resolve_clock(None, None, 2.0, False)


def test_resolve_clock_realtime_plus_cli_speed_overrides() -> None:
    """realtime config + `--speed S'` ⇒ speed overridden, cap inherited."""
    cfg = realtime_clock(speed=10.0, idle_cap_seconds=3.0)
    result = resolve_clock(cfg, 99.0, None, False)
    assert result == ResolvedClock(speed=99.0, idle_cap_seconds=3.0)


def test_resolve_clock_realtime_plus_cli_cap_overrides() -> None:
    """realtime config + `--idle-cap C'` ⇒ cap overridden, speed inherited."""
    cfg = realtime_clock(speed=10.0, idle_cap_seconds=3.0)
    result = resolve_clock(cfg, None, 99.0, False)
    assert result == ResolvedClock(speed=10.0, idle_cap_seconds=99.0)


# ---------------------------------------------------------------------------
# pace_events
# ---------------------------------------------------------------------------


def test_pace_events_first_event_immediate() -> None:
    """First event is released without sleeping."""
    sleeps: list[float] = []
    time = [0.0]

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        time[0] += s

    def fake_monotonic() -> float:
        return time[0]

    events = [make_event(1, 0)]
    clock = ResolvedClock(speed=1.0, idle_cap_seconds=None)
    result = list(pace_events(events, clock, fake_sleep, fake_monotonic))
    assert sleeps == []
    assert len(result) == 1


def test_pace_events_consecutive_gap_delay() -> None:
    """Consecutive gap Δ ns ⇒ delay Δ/1e9/speed seconds."""
    sleeps: list[float] = []
    time = [0.0]

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        time[0] += s

    def fake_monotonic() -> float:
        return time[0]

    delta_ns = 2_000_000_000
    events = [make_event(1, 0), make_event(2, delta_ns)]
    clock = ResolvedClock(speed=2.0, idle_cap_seconds=None)
    list(pace_events(events, clock, fake_sleep, fake_monotonic))
    assert len(sleeps) == 1
    assert abs(sleeps[0] - delta_ns / 1e9 / 2.0) < 1e-9


def test_pace_events_uncapped_vs_capped() -> None:
    """Cap applies min; uncapped uses full delay."""
    sleeps_uncapped: list[float] = []
    sleeps_capped: list[float] = []
    time = [0.0]

    def fake_sleep_uncapped(s: float) -> None:
        sleeps_uncapped.append(s)
        time[0] += s

    def fake_sleep_capped(s: float) -> None:
        sleeps_capped.append(s)

    def fake_monotonic() -> float:
        return time[0]

    events = [make_event(1, 0), make_event(2, 10_000_000_000)]
    uncapped = ResolvedClock(speed=1.0, idle_cap_seconds=None)
    capped = ResolvedClock(speed=1.0, idle_cap_seconds=2.0)
    list(pace_events(events, uncapped, fake_sleep_uncapped, fake_monotonic))
    time[0] = 0.0
    list(pace_events(events, capped, fake_sleep_capped, fake_monotonic))
    assert sleeps_uncapped[0] == 10.0
    assert sleeps_capped[0] == 2.0


def test_pace_events_equal_sim_time_zero_delay() -> None:
    """Two events with equal event_sim_time ⇒ zero delay (no sleep)."""
    sleeps: list[float] = []
    time = [0.0]

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        time[0] += s

    def fake_monotonic() -> float:
        return time[0]

    events = [make_event(1, 1000), make_event(2, 1000)]
    clock = ResolvedClock(speed=1.0, idle_cap_seconds=None)
    list(pace_events(events, clock, fake_sleep, fake_monotonic))
    assert sleeps == []


def test_pace_events_consumer_slower_clamped_to_zero() -> None:
    """Consumer slower (monotonic jumps ahead) ⇒ computed sleep clamped to 0."""
    sleeps: list[float] = []
    calls = [0]

    def fake_sleep(s: float) -> None:
        sleeps.append(s)

    def fake_monotonic() -> float:
        calls[0] += 1
        if calls[0] == 1:
            return 0.0
        return 100.0

    events = [make_event(1, 0), make_event(2, 1_000_000_000)]
    clock = ResolvedClock(speed=1.0, idle_cap_seconds=None)
    list(pace_events(events, clock, fake_sleep, fake_monotonic))
    assert sleeps == []


def test_pace_events_speed_1_delay_equals_raw_ns() -> None:
    """speed=1.0 ⇒ delay equals the raw Δ/1e9 seconds."""
    sleeps: list[float] = []
    time = [0.0]

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        time[0] += s

    def fake_monotonic() -> float:
        return time[0]

    delta_ns = 5_000_000_000
    events = [make_event(1, 0), make_event(2, delta_ns)]
    clock = ResolvedClock(speed=1.0, idle_cap_seconds=None)
    list(pace_events(events, clock, fake_sleep, fake_monotonic))
    assert abs(sleeps[0] - 5.0) < 1e-9


def test_pace_events_drift_free() -> None:
    """Per-event fake processing time does not accumulate in the schedule."""
    sleeps: list[float] = []
    time = [0.0]
    processing = 0.05

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        time[0] += s

    def fake_monotonic() -> float:
        return time[0]

    events = [make_event(i, i * 1_000_000_000) for i in range(5)]
    clock = ResolvedClock(speed=1.0, idle_cap_seconds=None)

    for _ in pace_events(events, clock, fake_sleep, fake_monotonic):
        time[0] += processing

    total_sleep = sum(sleeps)
    total_schedule = 4.0
    assert total_sleep < total_schedule


def test_pace_events_order_and_payload_pass_through() -> None:
    """Order and payload are identical to input (pure pass-through)."""
    time = [0.0]

    def fake_sleep(s: float) -> None:
        time[0] += s

    def fake_monotonic() -> float:
        return time[0]

    events = [make_event(i, i * 1_000_000_000) for i in range(1, 6)]
    clock = ResolvedClock(speed=10.0, idle_cap_seconds=None)
    result = list(pace_events(events, clock, fake_sleep, fake_monotonic))
    assert result == events


def test_pace_events_empty_input_no_sleeps_empty_output() -> None:
    """Empty input ⇒ no sleeps, empty output."""
    sleeps: list[float] = []

    def fake_sleep(s: float) -> None:
        sleeps.append(s)

    def fake_monotonic() -> float:
        return 0.0

    clock = ResolvedClock(speed=1.0, idle_cap_seconds=None)
    result = list(pace_events([], clock, fake_sleep, fake_monotonic))
    assert result == []
    assert sleeps == []


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


def test_clock_speed_unresolvable_is_exporter_error() -> None:
    """ClockSpeedUnresolvable is an ExporterError (not ExportError)."""
    from fabulexa_export.errors import ExportError

    err = ClockSpeedUnresolvable("test")
    assert isinstance(err, ExporterError)
    assert not isinstance(err, ExportError)


# ---------------------------------------------------------------------------
# Package imports
# ---------------------------------------------------------------------------


def test_three_names_importable_from_streaming_package() -> None:
    """ResolvedClock, resolve_clock, pace_events importable from streaming."""
    from fabulexa_export.exporters.streaming import ResolvedClock as RC
    from fabulexa_export.exporters.streaming import pace_events as pe
    from fabulexa_export.exporters.streaming import resolve_clock as rc

    assert RC is ResolvedClock
    assert rc is resolve_clock
    assert pe is pace_events
