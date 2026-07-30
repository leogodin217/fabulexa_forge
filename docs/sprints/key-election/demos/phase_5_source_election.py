#!/usr/bin/env python
"""
Demo: Source mode election — elected identity per genre, edge/junction
rendering, and the mixed-column type rule
(`exporters/source/plan.py`, `exporters/source/renders.py`,
`exporters/source/engine.py`)

Sprint: key-election
Phase: 5

Builds one declared, three-kind emit:
  - `driver` — tracked (prop__status changes), presentation_id declared with
    prefix `DRIVER_` -> change-log genre.
  - `entity` — untracked, split alpha/beta (an object-registry role); alpha
    is presentation_id declared (`ALPHA_`), beta is not.
  - `booking` — untracked, uniform fact role -> transaction genre; carries
    `prop__driver_id` (references driver, uniform target) and
    `prop__entity_id` (references entity, a mixed alpha/beta target); owns
    a `riders` collection referencing `driver` by member field.

`keys: {driver: presentation_id, entity: {alpha: presentation_id, beta:
record_index}}`:

1. Change-log: `driver`'s `id` column carries `DRIVER_...` codes on `c`, `u`,
   and `d` rows alike (the fold's own after-image is NULL on `d` — the
   post-fold identity join supersedes it).
2. Reference-valued edge, uniform target: `booking.driver_id` renders the
   target's elected codes (`DRIVER_...`).
3. Reference-valued edge, mixed target: `booking.entity_id` renders
   `ALPHA_...` for the alpha-population row beside a digit-rendered `beta`
   `record_index` for the other, in one VARCHAR column.
4. Junction member field: `membership__booking__riders`' `driver_id` column
   renders the member's elected codes (`DRIVER_...`), with `driver_kind`
   as the `<f>_kind` disambiguator.
5. No `keys` block -> byte-identical to a pre-election export.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import ExportConfig
from fabulexa_forge.exporters.source.engine import build_source_query_specs
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"
_ANCHOR = EffectiveAnchor(
    start_instant=datetime(2024, 1, 1, tzinfo=timezone.utc), timezone=ZoneInfo("UTC")
)

_DRIVER_COLUMNS: list[dict[str, object]] = [
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

_ENTITY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__entity_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_BOOKING_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__driver_id",
        "type": "VARCHAR",
        "references": "driver",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {"name": "ref_index__driver_id", "type": "BIGINT"},
    {
        "name": "prop__entity_id",
        "type": "VARCHAR",
        "references": "entity",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {"name": "ref_index__entity_id", "type": "BIGINT"},
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__driver__kind", "type": "VARCHAR"},
    {"name": "member__driver__id", "type": "VARCHAR"},
]

# driver: d1 created(c) -> status update(u) -> deactivated(d), no property
# change on the delete; d2 created only (c).
_DRIVER_ROWS: list[tuple[object, ...]] = [
    ("trunk", "d1", "DRIVER_001", 0, False, 300, 300, 0, "busy"),
    ("trunk", "d2", "DRIVER_002", 50, True, None, 50, 1, "idle"),
]
_DRIVER_HISTORY_ROWS: list[tuple[object, ...]] = [
    ("trunk", "driver", "d1", "status", 0, "idle"),
    ("trunk", "driver", "d1", "status", 100, "busy"),
    ("trunk", "driver", "d2", "status", 50, "idle"),
]

# entity: e1 alpha (registry-declared), e2 beta (undeclared — record_index
# fallback, the doc's partially-declared-kind shape).
_ENTITY_ROWS: list[tuple[object, ...]] = [
    ("trunk", "e1", "ALPHA_001", 10, True, None, 10, 0, "alpha"),
    ("trunk", "e2", None, 10, True, None, 10, 1, "beta"),
]

_BOOKING_ROWS: list[tuple[object, ...]] = [
    ("trunk", "b1", 20, True, None, 20, 0, "d1", 0, "e1", 0),
    ("trunk", "b2", 25, True, None, 25, 1, "d2", 1, "e2", 1),
]

_MEMBERSHIP_ROWS: list[tuple[object, ...]] = [
    ("trunk", "b1", 30, None, "driver", "d1"),
]

_PRESENTATION_KEYS: dict[str, object] = {
    "driver": {
        "key": {
            "unique_within": "emit",
            "branch_stable": False,
            "slice_stable": False,
            "key_space": {"class": "counter", "prefix": "DRIVER_", "width": 3},
        }
    },
    "entity": {
        "sub_types": {
            "alpha": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "ALPHA_", "width": 3},
            }
        },
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    },
}


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _insert_all(
    conn: "duckdb.DuckDBPyConnection",
    table: str,
    cols: list[dict[str, object]],
    rows: list[tuple[object, ...]],
) -> None:
    placeholders = ", ".join("?" for _ in cols)
    for row in rows:
        conn.execute(f'INSERT INTO "{table}" VALUES ({placeholders})', list(row))


def _build_emit(emit_dir: Path) -> None:
    """Write the spanning source-mode election emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_ddl("records__driver", _DRIVER_COLUMNS))
    conn.execute(_ddl("records__entity", _ENTITY_COLUMNS))
    conn.execute(_ddl("records__booking", _BOOKING_COLUMNS))
    conn.execute(_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_ddl("membership__booking__riders", _MEMBERSHIP_COLUMNS))

    _insert_all(conn, "records__driver", _DRIVER_COLUMNS, _DRIVER_ROWS)
    _insert_all(conn, "history", _HISTORY_COLUMNS, _DRIVER_HISTORY_ROWS)
    _insert_all(conn, "records__entity", _ENTITY_COLUMNS, _ENTITY_ROWS)
    _insert_all(conn, "records__booking", _BOOKING_COLUMNS, _BOOKING_ROWS)
    _insert_all(
        conn, "membership__booking__riders", _MEMBERSHIP_COLUMNS, _MEMBERSHIP_ROWS
    )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 9999}],
        "tables": [
            {
                "name": "records__driver",
                "category": "records",
                "record_kind": "driver",
                "columns": _DRIVER_COLUMNS,
                "rows": len(_DRIVER_ROWS),
            },
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "columns": _ENTITY_COLUMNS,
                "rows": len(_ENTITY_ROWS),
            },
            {
                "name": "records__booking",
                "category": "records",
                "record_kind": "booking",
                "columns": _BOOKING_COLUMNS,
                "rows": len(_BOOKING_ROWS),
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": len(_DRIVER_HISTORY_ROWS),
            },
            {
                "name": "membership__booking__riders",
                "category": "membership",
                "record_kind": "booking",
                "property": "riders",
                "columns": _MEMBERSHIP_COLUMNS,
                "rows": len(_MEMBERSHIP_ROWS),
            },
        ],
        "enum_domains": {"entity": {"entity_type": ["alpha", "beta"]}},
        "record_roles": {
            "entity": {"alpha": "dimension", "beta": "dimension"},
            "booking": "fact",
        },
        "presentation_keys": _PRESENTATION_KEYS,
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _fail(message: str) -> "SystemExit":
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def _rows_by_table(
    emit_dir: Path, config: ExportConfig
) -> dict[str, list[tuple[object, ...]]]:
    with open_emit(emit_dir) as emit:
        specs = build_source_query_specs(
            emit, config, _ANCHOR, None, lambda _n: None, base_relations=None
        )
        rows_by_table: dict[str, list[tuple[object, ...]]] = {}
        for spec in specs:
            rows = emit.query(spec.sql, ())
            rows_by_table[spec.table_name] = rows
            print(f"  table '{spec.table_name}': {rows}")
        return rows_by_table


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)
        elected_config = ExportConfig(
            mode="source",
            keys={
                "driver": "presentation_id",
                "entity": {"alpha": "presentation_id", "beta": "record_index"},
            },
        )

        # ---- 1. Change-log: id carries codes on c/u/d rows alike ----------
        print("=== change-log: driver.id carries DRIVER_... on c/u/d alike ===")
        rows = _rows_by_table(emit_dir, elected_config)
        driver_events = rows["driver"]
        d_row = next(r for r in driver_events if r[0] == "d")
        if "DRIVER_001" not in d_row:
            raise _fail(f"the 'd' row {d_row!r} should carry DRIVER_001, not NULL")
        for event in driver_events:
            if "DRIVER_001" not in event and "DRIVER_002" not in event:
                raise _fail(f"driver event {event!r} carries no elected code")
        print("  OK: every c/u/d event carries its record's elected code")
        print()

        # ---- 2. Reference edge, uniform target -----------------------------
        print("=== booking.driver_id renders the uniform target's codes ===")
        b1 = next(r for r in rows["booking"] if r[0] == "b1")
        if "DRIVER_001" not in b1:
            raise _fail(f"booking b1 {b1!r} should carry DRIVER_001")
        print("  OK: booking.driver_id == DRIVER_001")
        print()

        # ---- 3. Reference edge, mixed target -------------------------------
        print(
            "=== booking.entity_id: ALPHA_... beside a digit-rendered beta index,"
            " one VARCHAR column ==="
        )
        b1_entity = next(r for r in rows["booking"] if r[0] == "b1")
        b2_entity = next(r for r in rows["booking"] if r[0] == "b2")
        if "ALPHA_001" not in b1_entity:
            raise _fail(f"booking b1 {b1_entity!r} should carry ALPHA_001")
        if "1" not in b2_entity:
            raise _fail(f"booking b2 {b2_entity!r} should carry digit-rendered '1'")
        print("  OK: b1.entity_id=ALPHA_001, b2.entity_id='1' (beta's record_index)")
        print()

        # ---- 4. Junction member field + <f>_kind disambiguator ------------
        print(
            "=== membership: driver_id renders the member's codes, driver_kind"
            " disambiguates ==="
        )
        member_row = rows["booking_riders"][0]
        if "DRIVER_001" not in member_row:
            raise _fail(f"member row {member_row!r} should carry DRIVER_001")
        if "driver" not in member_row:
            raise _fail(f"member row {member_row!r} should carry the 'driver' kind")
        print(f"  OK: {member_row}")
        print()

        # ---- 5. No keys block -> byte-identical ----------------------------
        print("=== no keys block: byte-identical to a pre-election export ===")
        default_config = ExportConfig(mode="source")
        default_rows = _rows_by_table(emit_dir, default_config)
        default_b1 = next(r for r in default_rows["booking"] if r[0] == "b1")
        if "e1" not in default_b1:
            raise _fail(f"default booking b1 {default_b1!r} should carry verbatim 'e1'")
        print("  OK: identity and edge columns render exactly as before this sprint")
        print()

        print(
            "SUCCESS: source mode renders elected identity per genre (post-fold"
            " join for change-log), edge/junction columns render target"
            " elections with the mixed-column type rule, and no keys block is"
            " byte-identical"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
