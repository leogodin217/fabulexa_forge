#!/usr/bin/env python
"""
Demo: Playback tier-1 identity projection — `RecordAtomSelection.identity`.

Plays a small `patient` emit (one record, minting a `presentation_id`
surrogate) twice through the same tape:

  - `identity=None` (the default): the full available set — the event
    `after` map and the `record_state` snapshot carry both `record_id` and
    `presentation_id`.
  - `identity=("record_id",)`: the surrogate is suppressed from the `after`
    map and absent from `record_state`'s columns — projection only.

In both runs, the typed `PlaybackEvent.presentation_id` field stays
populated: the projection governs the published maps, never the typed
event fields.

Sprint: author-selectable-identity
Phase: 4
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.playback import (
    Playback,
    PlaybackSelection,
    RecordAtomSelection,
    open_playback,
)
from fabulexa_forge.reader.emit import open_emit

#: records__patient column order: identity, presentation, lifecycle,
#: record_index, then the one tracked prop__ column.
_PATIENT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _ddl(table: str, columns: list[dict[str, object]]) -> str:
    """Build a CREATE TABLE statement from a base.json-shaped column list."""
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)
    return f'CREATE TABLE "{table}" ({parts})'


def _build_demo_emit(emit_dir: Path) -> None:
    """Write a one-record `patient` emit: created, then a status update."""
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_ddl("records__patient", _PATIENT_COLUMNS))
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "p1", "PT_001", 0, True, 10, 0, "active"],
    )

    conn.execute(_ddl("history", _HISTORY_COLUMNS))
    history_rows = [
        ("trunk", "patient", "p1", "status", 0, "new"),
        ("trunk", "patient", "p1", "status", 10, "active"),
    ]
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": _PATIENT_COLUMNS,
                "rows": 1,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": len(history_rows),
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _play(
    emit_dir: Path, identity: tuple[str, ...] | None
) -> tuple[dict[str, object] | None, list[str], str | None]:
    """Play the demo emit under one `identity` selection.

    Returns:
        (the create event's after-image, record_state's column names, the
        typed PlaybackEvent.presentation_id of the create event).
    """
    selection = PlaybackSelection(
        records=(RecordAtomSelection("patient", (), None, None, identity),),
        memberships=(),
    )
    with open_emit(emit_dir) as emit:
        playback: Playback = open_playback(emit, selection, None)
        events = list(playback.events(None, None))
        columns = playback.snapshot(100).record_state("patient").column_names
    create_event = next(e for e in events if e.op == "c")
    return create_event.after, list(columns), create_event.presentation_id


def main() -> int:
    errors: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
        _build_demo_emit(emit_dir)

        print("identity=None (default): full available set")
        default_after, default_columns, default_pid = _play(emit_dir, None)
        print(f"  after:          {default_after}")
        print(f"  record_state columns: {default_columns}")
        print(f"  PlaybackEvent.presentation_id: {default_pid}")

        if default_after is None or "presentation_id" not in default_after:
            errors.append("default run: after map missing presentation_id")
        if "presentation_id" not in default_columns:
            errors.append("default run: record_state missing presentation_id column")
        if default_pid is None:
            errors.append("default run: typed presentation_id is None")

        print("\nidentity=('record_id',): surrogate suppressed")
        narrow_after, narrow_columns, narrow_pid = _play(emit_dir, ("record_id",))
        print(f"  after:          {narrow_after}")
        print(f"  record_state columns: {narrow_columns}")
        print(f"  PlaybackEvent.presentation_id: {narrow_pid}")

        if narrow_after is None or "presentation_id" in narrow_after:
            errors.append("narrow run: after map still carries presentation_id")
        if "presentation_id" in narrow_columns:
            errors.append("narrow run: record_state still carries presentation_id")
        if narrow_pid is None:
            errors.append(
                "narrow run: typed presentation_id suppressed — projection must "
                "never touch the typed PlaybackEvent fields"
            )

    for error in errors:
        print(f"FAILURE: {error}")
    if errors:
        return 1

    print(
        "SUCCESS: identity=None publishes record_id + presentation_id; "
        "identity=('record_id',) suppresses the surrogate in both the after "
        "map and record_state, while PlaybackEvent.presentation_id stays "
        "populated in both runs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
