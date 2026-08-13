#!/usr/bin/env python
"""
Demo: `init` membership-estate proposals for a sub-typed owner
Sprint: source-row-selection
Phase: 4

`generate_source_init_config` proposes a sub-typed owner's membership estate
per declared sub-type (design doc § `init` proposals): a
`membership__clinician__ward_allocation` table, owned by the sub-typed
`clinician` kind (`day` / `night`), proposes two junction stubs --
`clinician_day_ward_allocation` / `clinician_night_ward_allocation`, each
`sub_types: [<sub_type>]` -- with the last carrying a commented
combine-alternative (one whole junction, `sub_types:` omitted), mirroring the
owner's own per-sub-type state-table split. The `versions` events stub
appends one commented membership entry per declared sub-type, each carrying
`sub_types: [<sub_type>]`.

Shows:
  1. The generated candidate YAML (junction split + commented membership
     event-source entries).
  2. The candidate loads and plans clean as generated (proposals only, no
     `where` anywhere).
  3. Uncommenting the full per-sub-type membership event-source set still
     plans clean: the entries share the default item-type
     `clinician.ward_allocation` under the extended sharing exception, and
     their both-declared disjoint `sub_types` satisfy the overlap gate.
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
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.source.init import generate_source_init_config
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"
_ANCHOR = EffectiveAnchor(
    start_instant=datetime(2024, 1, 1, tzinfo=timezone.utc), timezone=ZoneInfo("UTC")
)

_CLINICIAN_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__clinician_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]

_WARD_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__ward", "type": "VARCHAR"},
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

# clinician: c1 (day), c2 (night).
_CLINICIAN_ROWS: list[tuple[object, ...]] = [
    ("trunk", "c1", 0, True, None, 0, 0, "day", "on-duty"),
    ("trunk", "c2", 0, True, None, 0, 1, "night", "on-duty"),
]
_WARD_ROWS: list[tuple[object, ...]] = [
    ("trunk", "c1", 0, None, "A"),
    ("trunk", "c2", 0, None, "B"),
]


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


def build_emit(emit_dir: Path) -> None:
    """Write the sub-typed clinician / ward_allocation demo emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_ddl("records__clinician", _CLINICIAN_COLUMNS))
    conn.execute(_ddl("membership__clinician__ward_allocation", _WARD_COLUMNS))
    conn.execute(_ddl("history", _HISTORY_COLUMNS))

    _insert_all(conn, "records__clinician", _CLINICIAN_COLUMNS, _CLINICIAN_ROWS)
    _insert_all(
        conn, "membership__clinician__ward_allocation", _WARD_COLUMNS, _WARD_ROWS
    )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 999}],
        "enum_domains": {"clinician": {"clinician_type": ["day", "night"]}},
        "tables": [
            {
                "name": "records__clinician",
                "category": "records",
                "record_kind": "clinician",
                "columns": _CLINICIAN_COLUMNS,
                "rows": len(_CLINICIAN_ROWS),
            },
            {
                "name": "membership__clinician__ward_allocation",
                "category": "membership",
                "record_kind": "clinician",
                "property": "ward_allocation",
                "columns": _WARD_COLUMNS,
                "rows": len(_WARD_ROWS),
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 0,
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _fail(message: str) -> "SystemExit":
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def _plan_clean(emit_dir: Path, content: str, tmp_path: Path, label: str) -> None:
    """Load `content` and build a source plan against `emit_dir` — must not raise."""
    config_path = tmp_path / f"{label}.yaml"
    config_path.write_text(content, encoding="utf-8")
    config = load_export_config(config_path)
    notices: list[Notice] = []
    with open_emit(emit_dir) as emit:
        election = resolve_election(emit.sidecar, config.keys)
        build_source_plan(emit, config, _ANCHOR, election, False, notices.append)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "emit"
        build_emit(emit_dir)

        notices: list[Notice] = []
        with open_emit(emit_dir) as emit:
            content = generate_source_init_config(emit, notices.append)

        print("=== 1. generated candidate YAML ===")
        print(content)

        if "sub_types: [day]" not in content or "sub_types: [night]" not in content:
            raise _fail("expected a per-sub-type junction split (day / night)")
        if "where" in content:
            raise _fail("init must never propose a 'where' clause")
        print("OK: per-sub-type junction split proposed, no 'where' anywhere")
        print()

        print("=== 2. proposal-as-generated plans clean ===")
        _plan_clean(emit_dir, content, tmp_path, "as_generated")
        print("OK: the generated candidate loads and plans without error")
        print()

        print("=== 3. uncommenting the full membership event-source set ===")
        uncommented = content.replace("# - membership:", "- membership:").replace(
            "#   sub_types:", "  sub_types:"
        )
        if uncommented == content:
            raise _fail("expected commented membership entries to uncomment")
        _plan_clean(emit_dir, uncommented, tmp_path, "uncommented")
        print(
            "OK: uncommented per-sub-type membership sources plan clean --"
            " shared default item-type 'clinician.ward_allocation' + disjoint"
            " sub_types satisfy the overlap gate"
        )

        print()
        print(
            "SUCCESS: init proposes a sub-typed owner's membership estate per"
            " sub-type (junction stubs + commented event-source entries); the"
            " candidate plans clean as generated and with the full membership"
            " event-source set uncommented"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
