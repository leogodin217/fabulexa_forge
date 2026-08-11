#!/usr/bin/env python
"""
Demo: Message-key election (StreamConfig.keys -> streaming render sites)
Sprint: streaming-declared-streams
Phase: 3

Builds a two-kind emit -- `location` (flat, record_index-electable) and
`person` (flat, presentation_id-electable, with a `prop__home_location`
reference to `location`) -- and streams the same single `people` KindStream
twice through the same emit:

  1. No `keys` block: the default election. Every render site is
     record_id-keyed, byte-identical to Phase 2.
  2. `keys: {person: presentation_id, location: record_index}`: `people`'s
     `u` and `d` messages re-key to `presentation_id` (the `record_id`
     after-image entry renamed, the standalone `presentation_id` entry
     absorbed -- no duplicate column), and the after-image's
     `prop__home_location` reference renders `location`'s own elected
     surface (`record_index`, digit-form), not `person`'s.

Exercises the Phase 3 source-step surface: `StreamConfig.keys`,
`build_elected_identity_index`, `elect_after_image_columns`,
`StreamEvent.key_column` / `key_value`, and their rendering through
`render_jsonl_object` -- no driver/CLI, migrated in an earlier phase but
unexercised by election here (the jsonl sink alone is enough to show every
render site the design doc's key-election table lists).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import duckdb  # noqa: E402
from _support.sidecar_builder import (  # noqa: E402
    identity_column,
    prop_column,
    write_emit,
)

from fabulexa_forge.config.models import StreamConfig  # noqa: E402
from fabulexa_forge.exporters.streaming.engine import iter_stream_events  # noqa: E402
from fabulexa_forge.exporters.streaming.jsonl import render_jsonl_object  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402

_LOCATION_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_PERSON_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__home_location",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="location",
    ),
    identity_column("ref_index__home_location", "BIGINT"),
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

# location loc1: created t0 "open"; status changes t20 -> "closed".
_LOCATION_ROWS = [("trunk", "loc1", 0, True, None, 20, 0, "open")]
# person p1: created t5 "new", home_location=loc1 (constant); status changes
# t15 -> "active"; deactivated t30.
_PERSON_ROWS = [
    ("trunk", "p1", "PER_001", 5, False, 30, 15, 0, "new", "loc1", 0),
]
_HISTORY_ROWS = [
    ("trunk", "location", "loc1", "status", 0, "open"),
    ("trunk", "location", "loc1", "status", 20, "closed"),
    ("trunk", "person", "p1", "status", 5, "new"),
    ("trunk", "person", "p1", "status", 15, "active"),
]

_PRESENTATION_KEYS: dict[str, object] = {
    "person": {
        "key": {
            "unique_within": "emit",
            "branch_stable": False,
            "slice_stable": False,
            "key_space": {"class": "counter", "prefix": "PER_", "width": 3},
        }
    }
}


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _build_demo_emit(emit_dir: Path) -> None:
    """Write the `location` + `person` emit to `emit_dir`."""
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__location", _LOCATION_COLS))
    conn.execute(_ddl("records__person", _PERSON_COLS))
    conn.execute(_ddl("history", _HISTORY_COLS))
    for row in _LOCATION_ROWS:
        placeholders = ", ".join("?" for _ in _LOCATION_COLS)
        conn.execute(
            f'INSERT INTO "records__location" VALUES ({placeholders})', list(row)
        )
    for row in _PERSON_ROWS:
        placeholders = ", ".join("?" for _ in _PERSON_COLS)
        conn.execute(
            f'INSERT INTO "records__person" VALUES ({placeholders})', list(row)
        )
    for row in _HISTORY_ROWS:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    write_emit(
        emit_dir,
        tables=[
            {
                "name": "records__location",
                "category": "records",
                "columns": _LOCATION_COLS,
                "rows": len(_LOCATION_ROWS),
                "record_kind": "location",
            },
            {
                "name": "records__person",
                "category": "records",
                "columns": _PERSON_COLS,
                "rows": len(_PERSON_ROWS),
                "record_kind": "person",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLS,
                "rows": len(_HISTORY_ROWS),
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        extra={"presentation_keys": _PRESENTATION_KEYS},
    )


def _build_config(keys: dict[str, object] | None) -> StreamConfig:
    return StreamConfig(
        content="state-changes",
        streams=[
            {
                "name": "people",
                "kind": "person",
                "properties": ["status", "home_location"],
            }
        ],
        keys=keys,
    )


def _u_and_d_messages(emit_dir: Path, config: StreamConfig) -> tuple[dict, dict]:
    with open_emit(emit_dir) as emit:
        events = list(iter_stream_events(emit, config, None))
    u_event = next(e for e in events if e.op == "u")
    d_event = next(e for e in events if e.op == "d")
    return render_jsonl_object(u_event), render_jsonl_object(d_event)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
        _build_demo_emit(emit_dir)

        print("Run 1: no `keys` block (default record_id election)")
        default_config = _build_config(None)
        default_u, default_d = _u_and_d_messages(emit_dir, default_config)
        print(f"  u: {default_u}")
        print(f"  d: {default_d}")

        print("\nRun 2: keys: {person: presentation_id, location: record_index}")
        elected_config = _build_config(
            {"person": "presentation_id", "location": "record_index"}
        )
        elected_u, elected_d = _u_and_d_messages(emit_dir, elected_config)
        print(f"  u: {elected_u}")
        print(f"  d: {elected_d}")

        # -- Assertions the demo proves --

        # Default: record_id keys every message; after-image identity ships
        # record_id verbatim, home_location verbatim as loc1's raw record_id.
        assert default_u["key"] == {"record_id": "p1"}
        assert default_d["key"] == {"record_id": "p1"}
        assert default_u["after"] == {
            "record_id": "p1",
            "presentation_id": "PER_001",
            "prop__status": "active",
            "prop__home_location": "loc1",
        }
        assert default_d["after"] is None

        # Elected: person's messages key by presentation_id, including the
        # 'd' tombstone.
        assert elected_u["key"] == {"presentation_id": "PER_001"}
        assert elected_d["key"] == {"presentation_id": "PER_001"}
        assert elected_d["after"] is None

        # After-image identity re-keyed to presentation_id; the standalone
        # presentation_id entry is absorbed (no duplicate column); the
        # reference renders location's own elected surface (record_index,
        # digit-form "0"), not person's presentation_id.
        assert elected_u["after"] == {
            "presentation_id": "PER_001",
            "prop__status": "active",
            "prop__home_location": "0",
        }

    print("\nSUCCESS: key election -- key map, after-image re-key + absorption,")
    print("and cross-kind reference translation through the target's own surface")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
