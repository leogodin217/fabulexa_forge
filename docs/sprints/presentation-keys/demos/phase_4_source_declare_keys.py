#!/usr/bin/env python
"""
Demo: Source mode declare_keys — resolve_source_table_keys + engine wiring
Sprint: presentation-keys
Phase: 4

Builds a small emit spanning three source genres:

- `records__visit`: a tracked (changelog-genre) kind carrying a flat
  whole-column presentation_keys claim, owning a membership property
  (`membership__visit__team`, junction genre — never keyed).
- `records__actor`: an untracked, object-registry kind splitting into
  `consultant` (dimension role) / `nurse` (fact role); the presentation_keys
  block declares a partition only for `consultant` — presence is the claim,
  so `nurse` gets identity keys only.

Runs `mode: source` + `declare_keys: true` to DuckDB under both
`change_delivery` values and prints each output table's `duckdb_constraints()`:

- `change_delivery: changelog` (default): `visit` is undeclared (multiple
  rows per record post-fold — no honest key), `visit_team` (junction) is
  undeclared, `consultant` gets PRIMARY KEY (id) + UNIQUE (presentation_id),
  `nurse` gets PRIMARY KEY (id) only.
- `change_delivery: snapshot`: `visit` now reconstructs one row per record
  and carries the same whole-table-claimed keys as `consultant`.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Literal

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import ExportConfig, SourceConfig
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.source.engine import export_source
from fabulexa_forge.reader.emit import open_emit

_RECORDS_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
]

_VISIT_COLUMNS: list[dict[str, object]] = [
    *_RECORDS_COLUMNS,
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]

_ACTOR_COLUMNS: list[dict[str, object]] = [
    *_RECORDS_COLUMNS,
    {
        "name": "prop__actor_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__actor__kind", "type": "VARCHAR"},
    {"name": "member__actor__id", "type": "VARCHAR"},
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

#: `visit`'s flat key claim: unique within the branch, stable across branch
#: and slice — a plain record_index-class declaration.
_VISIT_PRESENTATION_KEYS: dict[str, object] = {
    "key": {
        "unique_within": "branch",
        "branch_stable": True,
        "slice_stable": True,
        "key_space": {"class": "record_index", "prefix": "", "width": 4},
    }
}

#: `actor`'s partitioned entry declares only `consultant` — presence is the
#: claim, so `nurse` (absent) resolves to identity keys only. A singleton
#: sub_types set's rollup equals its own scalars (§ combined_claim).
_ACTOR_PRESENTATION_KEYS: dict[str, object] = {
    "sub_types": {
        "consultant": {
            "unique_within": "branch",
            "branch_stable": True,
            "slice_stable": True,
            "key_space": {"class": "record_index", "prefix": "", "width": 4},
        }
    },
    "unique_within": "branch",
    "branch_stable": True,
    "slice_stable": True,
}


def _create_ddl(table_name: str, columns: list[dict[str, object]]) -> str:
    cols = ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)
    return f'CREATE TABLE "{table_name}" ({cols})'


def _write_emit(emit_dir: Path) -> None:
    """Write a run.duckdb + base.json pair spanning the changelog, split-unit,
    and junction genres."""
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    try:
        conn.execute(_create_ddl("records__visit", _VISIT_COLUMNS))
        conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
        conn.execute(_create_ddl("membership__visit__team", _MEMBERSHIP_COLUMNS))
        conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

        conn.execute(
            'INSERT INTO "records__visit" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
            ["trunk", "v001", 1001, 0, True, 0, 0, "open"],
        )
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "visit", "v001", "status", 0, "open"],
        )
        conn.execute(
            'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
            ["trunk", "act001", 2001, 0, True, 0, 0, "consultant"],
        )
        conn.execute(
            'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
            ["trunk", "act002", 2002, 0, True, 0, 1, "nurse"],
        )
        conn.execute(
            'INSERT INTO "membership__visit__team" VALUES (?, ?, ?, NULL, ?, ?)',
            ["trunk", "v001", 0, "actor", "act001"],
        )
    finally:
        conn.close()

    base_json = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__visit",
                "category": "records",
                "record_kind": "visit",
                "columns": _VISIT_COLUMNS,
                "rows": 1,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": _ACTOR_COLUMNS,
                "rows": 2,
            },
            {
                "name": "membership__visit__team",
                "category": "membership",
                "record_kind": "visit",
                "property": "team",
                "columns": _MEMBERSHIP_COLUMNS,
                "rows": 1,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 1,
            },
        ],
        "record_roles": {"actor": {"consultant": "dimension", "nurse": "fact"}},
        "enum_domains": {"actor": {"actor_type": ["consultant", "nurse"]}},
        "runtime": {
            "timezone": "UTC",
            "start_datetime": "2024-01-01T00:00:00+00:00",
        },
        "presentation_keys": {
            "visit": _VISIT_PRESENTATION_KEYS,
            "actor": _ACTOR_PRESENTATION_KEYS,
        },
    }
    (emit_dir / "base.json").write_text(json.dumps(base_json), encoding="utf-8")


def _print_constraints(db_path: Path, table_name: str) -> None:
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = conn.execute(
            "SELECT constraint_type, constraint_column_names"
            " FROM duckdb_constraints() WHERE table_name = ?",
            [table_name],
        ).fetchall()
    finally:
        conn.close()
    print(f"  {table_name}: {rows if rows else '(no declared constraints)'}")


def _run_duckdb_with_keys(
    emit_dir: Path, out_path: Path, change_delivery: Literal["changelog", "snapshot"]
) -> dict[str, int]:
    config = ExportConfig(
        mode="source",
        source=SourceConfig(declare_keys=True, change_delivery=change_delivery),
    )
    notices: list[Notice] = []
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        counts = export_source(
            emit, config, out_path, "duckdb", anchor, notice_sink=notices.append
        )
    print(
        f"\n== mode: source, declare_keys: true, change_delivery: {change_delivery} =="
    )
    print(f"row counts: {counts}")
    print("duckdb_constraints():")
    for table_name in ("visit", "visit_team", "consultant", "nurse"):
        _print_constraints(out_path, table_name)
    print(f"notices: {[n.code for n in notices]}")
    return counts


def main() -> int:
    tmp_dir = Path(tempfile.mkdtemp(prefix="fabulexa_forge_phase4_demo_"))
    try:
        _write_emit(tmp_dir)
        _run_duckdb_with_keys(tmp_dir, tmp_dir / "changelog.duckdb", "changelog")
        _run_duckdb_with_keys(tmp_dir, tmp_dir / "snapshot.duckdb", "snapshot")
        print(
            "\nSUCCESS: changelog + junction declare no keys under changelog"
            " delivery; the changelog-genre table gains the whole-table-claimed"
            " keys under snapshot delivery; the claimed split unit"
            " (consultant) declares presentation_id UNIQUE, the unclaimed one"
            " (nurse) declares identity keys only"
        )
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
