"""Test-facing NoticeSink implementations.

Every migrated call site threads one of these two sinks: `discard_notice_sink`
for tests indifferent to notices, `RecordingNoticeSink` for tests asserting on
notice sequence and content.
"""

from __future__ import annotations

from fabulexa_forge.exporters.notices import Notice


def discard_notice_sink(notice: Notice) -> None:
    """Swallow a notice. The migration sink for tests indifferent to notices.

    Args:
        notice: The notice to discard.

    Returns:
        None.
    """


class RecordingNoticeSink:
    """Callable NoticeSink that appends every received Notice to `self.notices`
    (a list, in delivery order) for sequence and content assertions.
    """

    def __init__(self) -> None:
        self.notices: list[Notice] = []

    def __call__(self, notice: Notice) -> None:
        self.notices.append(notice)
