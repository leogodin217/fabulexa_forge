"""Shared event-log row-mapping helpers (`exporters/source/events.py`).

Both `test_events_render.py` and `test_value_election_events.py` execute a
compiled `build_event_log_sql` SELECT and assert against its fixed output
shape; factored once here rather than duplicated per module.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from fabulexa_forge.reader.emit import Emit

EVENT_LOG_COLUMNS = ("id", "item_type", "item_id", "event", "occurred_at", "changes")
"""The event log's fixed output columns, positional order."""


def event_log_rows(emit: "Emit", sql: str) -> list[dict[str, object]]:
    """Execute `sql` and zip every row against the event log's fixed columns."""
    return [dict(zip(EVENT_LOG_COLUMNS, row)) for row in emit.query(sql, ())]


def changes_of(row: dict[str, object]) -> dict[str, object]:
    """Parse one row's `changes` VARCHAR cell as JSON."""
    assert isinstance(row["changes"], str)
    return cast("dict[str, object]", json.loads(row["changes"]))


def row_for(
    rows: list[dict[str, object]], item_id: object, event: str
) -> dict[str, object]:
    """The sole row matching (item_id, event); asserts exactly one match."""
    matches = [r for r in rows if r["item_id"] == item_id and r["event"] == event]
    assert len(matches) == 1, (
        f"expected exactly one ({item_id}, {event}) row: {matches}"
    )
    return matches[0]
