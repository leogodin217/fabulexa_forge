"""Realtime pacing for the streaming exporter.

Provides the resolved clock policy type and the two functions that implement
drift-free event pacing: resolve_clock (config × CLI precedence) and pace_events
(schedule events against a monotonic real-time origin).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fabulexa_forge.errors import ClockSpeedUnresolvable

if TYPE_CHECKING:
    from fabulexa_forge.config.models import ClockConfig
    from fabulexa_forge.exporters.streaming.types import StreamEvent


@dataclass(frozen=True)
class ResolvedClock:
    """The resolved realtime pacing policy for one stream run.

    Only realtime is represented; a fast (unpaced) run is the absence of a
    ResolvedClock (None), mirroring EffectiveAnchor's None. Produced by resolve_clock
    from the config `clock` block and the CLI overrides.
    """

    speed: float
    """The resolved sim-to-real multiplier; always > 0 — config-path values are guarded
    by `Field(gt=0)` and CLI-path values by the flag-level positivity check in
    cmd_stream, so resolve_clock never produces a non-positive speed."""
    idle_cap_seconds: float | None
    """The resolved ceiling in real seconds on inter-event delay, or None for
    uncapped."""


def resolve_clock(
    config_clock: ClockConfig | None,
    cli_speed: float | None,
    cli_idle_cap_seconds: float | None,
    cli_fast: bool,
) -> ResolvedClock | None:
    """Resolve the one effective pacing policy for a stream invocation.

    Applies CLI-wins precedence per knob, mirroring resolve_effective_anchor. `--fast`
    forces unpaced. Otherwise `--speed` / `--idle-cap` escalate an absent-or-fast
    config to realtime; a realtime config supplies any knob the CLI leaves unset.

    Positivity is a precondition, not this function's job: CLI-supplied `--speed` /
    `--idle-cap` are positivity-checked (> 0) at the flag level in cmd_stream before
    this call, and the config path is guarded by `Field(gt=0)`. Every value reaching
    here is therefore > 0, so the returned `ResolvedClock.speed > 0` invariant holds.

    Args:
        config_clock: The validated `clock` block, or None when absent (≡ fast).
        cli_speed: `--speed` value, or None when unset.
        cli_idle_cap_seconds: `--idle-cap` value, or None when unset.
        cli_fast: True when `--fast` was given.

    Returns:
        A ResolvedClock when the run is realtime, or None when the run is fast
        (unpaced) — the caller then delivers without the pacer.

    Raises:
        ClockSpeedUnresolvable: The run resolves to realtime (a realtime config, or
            `--speed`/`--idle-cap` given) but no speed is resolvable — e.g. `--idle-cap`
            over a fast/absent config with no `--speed`.
    """
    if cli_fast:
        return None

    config_speed: float | None = (
        config_clock.speed
        if (config_clock is not None and config_clock.mode == "realtime")
        else None
    )
    config_cap: float | None = (
        config_clock.idle_cap_seconds
        if (config_clock is not None and config_clock.mode == "realtime")
        else None
    )
    config_is_realtime = config_speed is not None

    cli_requests_realtime = cli_speed is not None or cli_idle_cap_seconds is not None

    if not config_is_realtime and not cli_requests_realtime:
        return None

    speed: float | None = cli_speed if cli_speed is not None else config_speed

    if speed is None:
        raise ClockSpeedUnresolvable(
            "Cannot resolve clock speed: the run is realtime (--idle-cap given or "
            "realtime config) but no speed is available. Provide --speed or add a "
            "realtime clock block with speed set."
        )

    cap: float | None = (
        cli_idle_cap_seconds if cli_idle_cap_seconds is not None else config_cap
    )

    return ResolvedClock(speed=speed, idle_cap_seconds=cap)


def pace_events(
    events: Iterable[StreamEvent],
    clock: ResolvedClock,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> Iterator[StreamEvent]:
    """Yield each event after sleeping until its scheduled real-time release.

    A pure pass-through in sequence and payload: the yielded events are identical to
    `events` in order and content; only the wall-clock timing of each yield is
    governed. Keys solely off StreamEvent.event_sim_time, so it is content-, format-,
    and sink-agnostic.

    The release schedule is drift-free against a fixed origin captured from monotonic()
    at the first event: event i releases at origin + the running sum over k in (1, i] of
    min((t_k - t_{k-1}) / 1e9 / clock.speed, clock.idle_cap_seconds), where t is
    event_sim_time in nanoseconds and an absent cap omits the min. The first event is
    released immediately. A computed sleep below zero (a consumer slower than the
    schedule) is clamped to zero — the pacer falls behind, never advances past the
    schedule. Relies on event_sim_time being non-decreasing in seq order (the canonical
    merge guarantee); it does not defend against a decrease.

    Args:
        events: The merged, seq-stamped event stream in canonical order.
        clock: The resolved realtime pacing policy (speed and optional cap).
        sleep: Blocking sleep of N real seconds; time.sleep in production, a fake in
            tests.
        monotonic: Monotonic real-clock reading in seconds; time.monotonic in
            production, a fake in tests.

    Returns:
        An iterator over the same events, in the same order, each yielded at its
        scheduled real-time instant.
    """
    origin: float | None = None
    prev_sim_time: int | None = None
    scheduled_offset: float = 0.0

    for event in events:
        if origin is None:
            origin = monotonic()
            prev_sim_time = event.event_sim_time
            yield event
            continue

        delta_ns = event.event_sim_time - prev_sim_time  # type: ignore[operator]
        delay = delta_ns / 1e9 / clock.speed
        if clock.idle_cap_seconds is not None:
            delay = min(delay, clock.idle_cap_seconds)
        scheduled_offset += delay

        elapsed = monotonic() - origin
        to_sleep = scheduled_offset - elapsed
        if to_sleep > 0.0:
            sleep(to_sleep)

        prev_sim_time = event.event_sim_time
        yield event
