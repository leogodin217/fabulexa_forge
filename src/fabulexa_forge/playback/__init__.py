"""The playback package: base-layer emit -> shaped read-only replay surface.

A downstream reader-first surface over an open emit — event streams, point-
in-time snapshots, and seeks, resolved from one caller atom selection.
Public exports grow per phase; Phase 5 exposes the atom-selection types and
the playback-seam exception. Phase 6 adds the event stream: `PlaybackEvent`,
`open_playback`, and the `Playback` head. Phase 7 adds `PlaybackSnapshot` /
`PlaybackPosition` (`Playback.snapshot` / `Playback.seek`). The internal
resolved-selection seam (`fabulexa_forge.playback.selection`) is not
re-exported here — later phases within the package import it directly.
Phase 10 adds tier-2 shaped playback: `ShapedTable`, `ShapedTableDecl`,
`open_shaped_playback`, and the `ShapedPlayback` head.

Layer-direction invariant: tier 1 (`types`, `errors`, `selection`, `events`,
`head`, `snapshot`, `stamp`) imports no `exporters.*` / `config` name. Tier 2
(`shaped`) is the seam's one deliberate crossing into `exporters.*` /
`config` — it wraps the exporters' own compile surfaces rather than
reimplementing their business rules; see `shaped.py`'s own docstring.
"""

from __future__ import annotations

from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.events import PlaybackEvent
from fabulexa_forge.playback.head import Playback, open_playback
from fabulexa_forge.playback.shaped import (
    ShapedPlayback,
    ShapedTable,
    ShapedTableDecl,
    open_shaped_playback,
)
from fabulexa_forge.playback.snapshot import PlaybackPosition, PlaybackSnapshot
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
    "PlaybackPosition",
    "PlaybackSelection",
    "PlaybackSnapshot",
    "RecordAtom",
    "RecordAtomSelection",
    "ShapedPlayback",
    "ShapedTable",
    "ShapedTableDecl",
    "open_playback",
    "open_shaped_playback",
]
