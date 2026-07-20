"""The playback package: base-layer emit -> shaped read-only replay surface.

A downstream reader-first surface over an open emit — event streams, point-
in-time snapshots, and seeks, resolved from one caller atom selection.
Public exports grow per phase; Phase 5 exposes the atom-selection types and
the playback-seam exception. Phase 6 adds the event stream: `PlaybackEvent`,
`open_playback`, and the `Playback` head. The internal resolved-selection
seam (`fabulexa_forge.playback.selection`) is not re-exported here — later
phases within the package import it directly.

Layer-direction invariant: this package imports no `exporters.*` / `config`
name.
"""

from __future__ import annotations

from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.events import PlaybackEvent
from fabulexa_forge.playback.head import Playback, open_playback
from fabulexa_forge.playback.types import (
    MembershipAtom,
    MembershipAtomSelection,
    PlaybackSelection,
    RecordAtom,
    RecordAtomSelection,
)

__all__ = [
    "MembershipAtom",
    "MembershipAtomSelection",
    "Playback",
    "PlaybackError",
    "PlaybackEvent",
    "PlaybackSelection",
    "RecordAtom",
    "RecordAtomSelection",
    "open_playback",
]
