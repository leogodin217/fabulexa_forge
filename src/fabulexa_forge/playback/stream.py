"""Tier-2 stream playback: bounded stream events and seek over a StreamConfig.

`open_stream_playback` runs streaming's eager business-rule pass
(`resolve_streams`) at open — the same pass a full stream export runs,
verbatim — so an invalid streaming config's `ExportError` passes through
unchanged and a valid config opens having run only the pass's own reads (the
selection-resolution spine read included; § Open-time behavior and errors —
a declared, scoped divergence from tier 1's open-reads-the-sidecar-only
rule). `StreamPlayback.events()` promotes the engine's own bounded resolved
iterator (`iter_resolved_stream_events`) verbatim: bounds select, never
recompute, and seq stays entry-point-invariant. `StreamPlayback.seek()`
composes the engine's snapshot phase (`iter_resolved_snapshot_events`) with
the live phase (`events(T + 1, end)`) — Debezium snapshot-then-stream, with
`end = T + 1` the snapshot phase alone (§ Seek).

Layer-direction invariant: tier-2 sibling of `shaped.py` — imports `config`
and streaming's pure surfaces only (`engine`'s `resolve_streams`,
`iter_resolved_stream_events`, `iter_resolved_snapshot_events`,
`build_topic_set`), never `driver`, `kafka_sink`, or `pacer`.
"""

from __future__ import annotations

from itertools import chain
from typing import TYPE_CHECKING, Iterator

from fabulexa_forge.exporters.streaming.engine import (
    StreamResolution,
    build_topic_set,
    iter_resolved_snapshot_events,
    iter_resolved_stream_events,
    resolve_streams,
)
from fabulexa_forge.playback.errors import PlaybackError

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import StreamConfig
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.streaming.types import StreamEvent
    from fabulexa_forge.reader.emit import Emit


def _validate_stream_event_bounds(start: int | None, end: int | None) -> None:
    """Reject a negative bound or an inverted range (a caller-contract
    violation, never a data condition).

    Args:
        start: The caller's inclusive lower bound, or None.
        end: The caller's exclusive upper bound, or None.

    Raises:
        PlaybackError: Either bound is negative, or both are given with
            start > end.
    """
    if start is not None and start < 0:
        raise PlaybackError(f"start must be >= 0, got {start}")
    if end is not None and end < 0:
        raise PlaybackError(f"end must be >= 0, got {end}")
    if start is not None and end is not None and start > end:
        raise PlaybackError(f"start ({start}) must be <= end ({end})")


def _validate_seek_position(at_sim_time: int, end: int | None) -> None:
    """Reject a negative seek position or an end bound at or before it.

    Args:
        at_sim_time: The caller's inclusive position T.
        end: The caller's exclusive live-phase end bound, or None.

    Raises:
        PlaybackError: at_sim_time is negative, or end <= at_sim_time.
    """
    if at_sim_time < 0:
        raise PlaybackError(f"at_sim_time must be >= 0, got {at_sim_time}")
    if end is not None and end <= at_sim_time:
        raise PlaybackError(f"end ({end}) must be > at_sim_time ({at_sim_time})")


class StreamPlayback:
    """A stream-shaped playback head: bounded events, seek, and the topic set.

    Deterministic and pull-only; outstanding lazy answers are independently
    pullable. All positions and bounds are raw sim-time nanoseconds.
    """

    def __init__(
        self,
        emit: "Emit",
        config: "StreamConfig",
        anchor: "EffectiveAnchor | None",
        resolution: StreamResolution,
    ) -> None:
        self._emit = emit
        self._config = config
        self._anchor = anchor
        self._resolution = resolution

    def topics(self) -> tuple[str, ...]:
        """The run's topic set: the declared stream names, declaration order.

        Returns:
            The declared topic names — declared intent, independent of data,
            so a caller provisions sinks before the first ask.
        """
        return build_topic_set(self._config)

    def events(
        self,
        start: int | None,
        end: int | None,
    ) -> "Iterator[StreamEvent]":
        """Yield the in-scope events with start <= event_sim_time < end.

        Canonical total order, seq stamped entry-point-invariantly (the
        first event of a bounded ask carries 1 + N, N = in-scope events
        strictly before start). (None, None) is the whole tape,
        byte-identical to the shipped whole-tape run. Lazy: nothing computes
        until the iterator is pulled.

        Args:
            start: Inclusive lower bound (ns), or None for tape start.
            end: Exclusive upper bound (ns), or None for tape end.

        Returns:
            An iterator of StreamEvent in canonical order.

        Raises:
            PlaybackError: start > end, or a negative bound.
        """
        _validate_stream_event_bounds(start, end)
        return iter_resolved_stream_events(
            self._emit, self._config, self._anchor, self._resolution, start, end
        )

    def seek(self, at_sim_time: int, end: int | None = None) -> "Iterator[StreamEvent]":
        """Snapshot-then-stream from position T (inclusive) to end (exclusive).

        state-changes content: first the 'r' phase — one read event per
        record live at T per covering stream, ordered
        (stream_name, record_id), each carrying seq = N and the record's
        published state at T — then every event of events(T + 1, end).
        membership-events content: the initial phase is empty (an
        append-only fact log has no per-key state to seed); the answer is
        events(T + 1, end). end = T + 1 yields the snapshot phase alone.

        Args:
            at_sim_time: The seek position T (ns), inclusive.
            end: Exclusive upper bound (ns) of the live phase, or None for
                tape end.

        Returns:
            An iterator of StreamEvent: the snapshot phase, then the live
            phase, matching a full play byte-for-byte over [T + 1, end).

        Raises:
            PlaybackError: at_sim_time is negative, or end <= at_sim_time.
        """
        _validate_seek_position(at_sim_time, end)
        snapshot_phase = iter_resolved_snapshot_events(
            self._emit, self._config, self._anchor, self._resolution, at_sim_time
        )
        live_phase = iter_resolved_stream_events(
            self._emit,
            self._config,
            self._anchor,
            self._resolution,
            at_sim_time + 1,
            end,
        )
        return chain(snapshot_phase, live_phase)


def open_stream_playback(
    emit: "Emit",
    config: "StreamConfig",
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
) -> StreamPlayback:
    """Bind a stream head to an open emit and a declared stream configuration.

    Runs the streaming exporter's full eager business-rule pass at open,
    verbatim: per-stream resolvability, vocabulary, naming, selection,
    change scope, and the election gates — the pass's selection-resolution
    spine read included (open is not sidecar-only; § Open-time behavior and
    errors). Pull-only thereafter — no answer computes and no event
    materializes until an iterator is pulled.

    Args:
        emit: An open emit (version-gated by open_emit).
        config: The validated streaming configuration (either content type).
        anchor: The resolved effective anchor, or None (events then carry
            raw-ns ts values; a later debezium render resolution will refuse
            the missing anchor at its own gate).
        notice_sink: Receiver for the open pass's notices (required — the
            notice-channel contract; a caller wanting silence passes a
            discarding sink).

    Returns:
        A StreamPlayback head bound to (emit, config, anchor, notice_sink).

    Raises:
        ExportError: A streaming business rule failed (the existing gate
            identities, including the election subclasses), or the
            single-branch guard tripped — passed through unchanged.
        TemporalClassUnavailableError: Propagated from the eager pass's
            slice_only check (a reader-domain identity, passed through).
    """
    resolution = resolve_streams(emit, config, notice_sink)
    return StreamPlayback(emit, config, anchor, resolution)
