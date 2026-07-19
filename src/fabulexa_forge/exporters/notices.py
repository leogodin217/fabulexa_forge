"""The mode-neutral notice channel.

`Notice` is one informational, non-fatal fact about an export plan; `NoticeSink`
is the caller-supplied receiver every policing surface calls synchronously as a
notice is discovered; `render_notice_stderr` is the CLI's sink. Notices never
alter output data, table sets, or the exit code — see the design doc
(docs/architecture/pending/slice-only-policy.md) § Notice semantics.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["Notice", "NoticeSink", "render_notice_stderr"]


@dataclass(frozen=True)
class Notice:
    """One informational, non-fatal fact about an export plan.

    Deterministic: the same emit, config, and code version produce the same
    notice sequence. A notice never alters output data or the exit code.
    """

    code: str
    """Stable machine-readable identifier (kebab-case, e.g.
    'slice-only-column-omitted'). Test assertions key on it."""

    message: str
    """Fully rendered human-readable text naming the concrete subject
    (table, column, unit) — self-contained, no interpolation left."""


NoticeSink = Callable[[Notice], None]
"""Receiver for notices, called synchronously as each notice is discovered.

The CLI passes a stderr renderer; tests pass a list-appender; a library
caller passes whatever it likes. Never None — a caller that wants silence
passes a discarding sink."""


def render_notice_stderr(notice: Notice) -> None:
    """Write one notice line to stderr: ``notice: {message}``.

    The CLI's NoticeSink for every verb that compiles an export plan.

    Args:
        notice: The notice to render.

    Returns:
        None.

    Raises:
        Nothing.
    """
    print(f"notice: {notice.message}", file=sys.stderr)
