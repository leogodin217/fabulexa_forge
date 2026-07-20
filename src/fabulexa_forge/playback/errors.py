"""The playback-seam exception surface.

One exception type; playback raises it for every contract violation. Never
raised for a data condition — corrupted, drifted, or otherwise defective tape
data flows through untouched (permissive playback); only a business rule over
the sidecar or an ask argument can fail.

Layer-direction invariant: imports nothing but stdlib. Never imports
exporters.*, config, or the reader.
"""

from __future__ import annotations


class PlaybackError(Exception):
    """A playback-seam contract violation: an unresolvable selection, an
    invalid ask argument, or a seam-level shape gate (the source-shape
    anchor requirement). Never raised for a data condition — semantic
    defects flow through (permissive playback)."""
