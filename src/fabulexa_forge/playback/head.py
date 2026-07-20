"""The playback head: `open_playback` and the `Playback` class.

Binds an open emit and a validated atom selection into a pull-only,
deterministic tape head. Phase 6 delivers `events`; later phases add
`snapshot` and `seek` to the same class.

Layer-direction invariant: imports the reader, the derivations single-branch
guard, `fabulexa_forge.playback.*`, and stdlib. Never imports exporters.* or
config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterator

from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.events import PlaybackEvent, iter_playback_events
from fabulexa_forge.playback.selection import resolve_selection

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.playback.selection import ResolvedSelection
    from fabulexa_forge.playback.types import PlaybackSelection
    from fabulexa_forge.reader.emit import Emit


def _validate_event_bounds(
    start_sim_time: int | None,
    end_sim_time: int | None,
) -> None:
    """AskBoundsValid: reject a negative bound or an inverted range.

    Args:
        start_sim_time: The caller's inclusive lower bound, or None.
        end_sim_time: The caller's exclusive upper bound, or None.

    Raises:
        PlaybackError: Either bound is negative, or both are given with
            start_sim_time > end_sim_time.
    """
    if start_sim_time is not None and start_sim_time < 0:
        raise PlaybackError(f"start_sim_time must be >= 0, got {start_sim_time}")
    if end_sim_time is not None and end_sim_time < 0:
        raise PlaybackError(f"end_sim_time must be >= 0, got {end_sim_time}")
    if (
        start_sim_time is not None
        and end_sim_time is not None
        and start_sim_time > end_sim_time
    ):
        raise PlaybackError(
            f"start_sim_time ({start_sim_time}) must be <= end_sim_time"
            f" ({end_sim_time})"
        )


class Playback:
    """A tape head: pull-only, deterministic answers over one emit + selection."""

    def __init__(
        self,
        emit: "Emit",
        resolved: "ResolvedSelection",
        anchor: "EffectiveAnchor | None",
        fork_path: str,
    ) -> None:
        self._emit = emit
        self._resolved = resolved
        self._anchor = anchor
        self._fork_path = fork_path

    def events(
        self,
        start_sim_time: int | None,
        end_sim_time: int | None,
    ) -> Iterator[PlaybackEvent]:
        """Iterate in-scope events in canonical total order, lazily.

        Half-open bounds on event_sim_time: yields events with
        start_sim_time <= event_sim_time < end_sim_time. None means unbounded
        on that side. seq is entry-point-invariant: numbering continues the
        whole-stream order regardless of start_sim_time.

        Args:
            start_sim_time: Inclusive lower bound (ns), or None for tape start.
            end_sim_time: Exclusive upper bound (ns), or None for tape end.

        Returns:
            A lazy iterator; no work happens until pulled.

        Raises:
            PlaybackError: start_sim_time > end_sim_time, or a negative bound.
        """
        _validate_event_bounds(start_sim_time, end_sim_time)
        return iter_playback_events(
            self._emit,
            self._resolved,
            self._anchor,
            self._fork_path,
            start_sim_time,
            end_sim_time,
        )


def open_playback(
    emit: "Emit",
    selection: "PlaybackSelection",
    anchor: "EffectiveAnchor | None",
) -> Playback:
    """Bind a playback head to an open emit and a validated atom selection.

    Validates every selection element against the sidecar (fail-fast, before
    any data read) and enforces the trunk-only single-branch guard. Performs
    no table reads. The caller owns emit's lifetime and resolves the anchor
    (resolve_effective_anchor or None for raw sim-time rendering).

    Args:
        emit: An open emit (version-gated by open_emit).
        selection: The atom selection; validated per Validation Rules.
        anchor: The resolved effective anchor, or None to render raw sim-time
            integers everywhere.

    Returns:
        A Playback head bound to (emit, selection, anchor).

    Raises:
        PlaybackError: The selection fails a validation rule (empty selection,
            duplicate atom, unknown kind / sub-type / property / membership
            table / field, a duplicate property / field name, a slice_only
            property, sub_types / owner_sub_types on a non-sub-typed kind
            or against an undeclared discriminator column,
            an empty record_ids / owner_record_ids set).
        ExportError: The sidecar enumerates zero or more than one branch
            (single-branch guard, passed through).
    """
    fork_path = require_single_branch(emit.sidecar)
    resolved = resolve_selection(emit.sidecar, selection)
    return Playback(emit, resolved, anchor, fork_path)
