"""MixerRunState: the single mutable state object shared across one mixer run.

One event loop owns a MixerRunState; no lock is required.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from fabulexa_export.anchor import EffectiveAnchor
    from fabulexa_export.exporters.streaming.mixer.consumer import ConsumerRunState
    from fabulexa_export.exporters.streaming.mixer.scheduler import (
        ControlState,
        FrontierState,
    )
    from fabulexa_export.exporters.streaming.types import StreamEvent


@dataclass
class MixerRunState:
    """The mutable state one mixer run shares between its request handlers and its
    release task — the single object the FastAPI app reads and mutates. One event loop
    owns it; no lock guards it.
    """

    control: "ControlState"
    frontier: "FrontierState"
    buffers: "dict[str, deque[StreamEvent]]"
    anchor: "EffectiveAnchor"
    monotonic: Callable[[], float]
    play_origin_monotonic: float | None
    consumer: "ConsumerRunState | None" = None
    """Present iff launched with --consumer; the second control + derived-state pair the
    same event loop owns. None for a producer-only run (today's behavior)."""
