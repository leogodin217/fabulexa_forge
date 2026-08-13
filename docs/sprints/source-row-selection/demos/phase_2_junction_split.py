#!/usr/bin/env python
"""
Demo: Membership-unit selection on junction tables (the NHS ward-allocation shape)
Sprint: source-row-selection
Phase: 2

A sub-typed owner (`clinician`: day/night, mixed key election — day elects
`presentation_id`, night elects `record_index`) owns a membership table
(`membership__clinician__ward_allocation`). `sub_types` splits it into two
declared junction tables through the parent lookup (design doc § The parent
lookup): each narrowed junction's owner column is typed by *its own*
addressed population's election, not the kind's full mixed domain (which
would fall back to VARCHAR).

A flat owner (`site`) owning `membership__site__coverage` splits by a
constant owner property `where: {region: ...}` instead — no `sub_types` axis
needed, since the owner carries no discriminator.

Shows:
  1. Full export: `day_ward` / `night_ward` junction tables (owner `sub_types`
     split), `north_coverage` / `south_coverage` junction tables (owner
     `where` split) — row-disjoint, together covering every membership
     interval.
  2. Owner column typing: `night_ward`'s owner column resolves the narrowed
     population's own election (`record_index` -> BIGINT), not the VARCHAR
     fallback an unrestricted (mixed-election) junction over the same kind
     would carry.
  3. Refusal: an unknown owner `sub_types` value (`SourceTableSubTypeUnknown`)
     and `sub_types` on a flat owner (`SourceSubTypesOnFlatKind`).
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import yaml

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.config.models import (
    ExportConfig,
    MembershipRef,
    SourceConfig,
    SourceTableDecl,
)
from fabulexa_forge.errors import SourceSubTypesOnFlatKind, SourceTableSubTypeUnknown
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.source.engine import export_source
from fabulexa_forge.exporters.source.plan import (
    SourceJunctionTablePlan,
    SourcePlan,
    build_source_plan,
)
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"
_ANCHOR = EffectiveAnchor(
    start_instant=datetime(2024, 1, 1, tzinfo=timezone.utc), timezone=ZoneInfo("UTC")
)

_CONFIG_YAML = """
mode: source
keys:
  clinician:
    day: presentation_id
    night: record_index
source:
  tables:
    - name: day_ward
      membership: {kind: clinician, property: ward_allocation}
      sub_types: [day]
    - name: night_ward
      membership: {kind: clinician, property: ward_allocation}
      sub_types: [night]
    - name: north_coverage
      membership: {kind: site, property: coverage}
      where:
        region: north
    - name: south_coverage
      membership: {kind: site, property: coverage}
      where:
        region: south
"""

_CLINICIAN_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "VARCHAR"},
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
]

_SITE_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__region",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_WARD_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__ward", "type": "VARCHAR"},
]

_COVERAGE_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__zone", "type": "VARCHAR"},
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

# clinician: c1 (day, presentation_id-carrying), c2 (night).
_CLINICIAN_ROWS: list[tuple[object, ...]] = [
    ("trunk", "c1", "CLIN-001", 0, True, None, 0, 0, "day"),
    ("trunk", "c2", "CLIN-002", 0, True, None, 0, 1, "night"),
]
# site: s1 (region north), s2 (region south).
_SITE_ROWS: list[tuple[object, ...]] = [
    ("trunk", "s1", 0, True, None, 0, 0, "north"),
    ("trunk", "s2", 0, True, None, 0, 1, "south"),
]
_WARD_ROWS: list[tuple[object, ...]] = [
    ("trunk", "c1", 0, None, "A"),
    ("trunk", "c2", 0, None, "B"),
]
_COVERAGE_ROWS: list[tuple[object, ...]] = [
    ("trunk", "s1", 0, None, "X"),
    ("trunk", "s2", 0, None, "Y"),
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
    """Write the ward-allocation / coverage demo emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_ddl("records__clinician", _CLINICIAN_COLUMNS))
    conn.execute(_ddl("records__site", _SITE_COLUMNS))
    conn.execute(_ddl("membership__clinician__ward_allocation", _WARD_COLUMNS))
    conn.execute(_ddl("membership__site__coverage", _COVERAGE_COLUMNS))
    conn.execute(_ddl("history", _HISTORY_COLUMNS))

    _insert_all(conn, "records__clinician", _CLINICIAN_COLUMNS, _CLINICIAN_ROWS)
    _insert_all(conn, "records__site", _SITE_COLUMNS, _SITE_ROWS)
    _insert_all(
        conn, "membership__clinician__ward_allocation", _WARD_COLUMNS, _WARD_ROWS
    )
    _insert_all(conn, "membership__site__coverage", _COVERAGE_COLUMNS, _COVERAGE_ROWS)
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 999}],
        "enum_domains": {"clinician": {"clinician_type": ["day", "night"]}},
        "presentation_keys": {
            "clinician": {
                "sub_types": {
                    "day": {
                        "unique_within": "emit",
                        "branch_stable": False,
                        "slice_stable": False,
                        "key_space": {
                            "class": "counter",
                            "prefix": "CLIN_",
                            "width": 3,
                        },
                    },
                    "night": {
                        "unique_within": "emit",
                        "branch_stable": False,
                        "slice_stable": False,
                        "key_space": {
                            "class": "counter",
                            "prefix": "NIGHT_",
                            "width": 3,
                        },
                    },
                },
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
            }
        },
        "tables": [
            {
                "name": "records__clinician",
                "category": "records",
                "record_kind": "clinician",
                "columns": _CLINICIAN_COLUMNS,
                "rows": len(_CLINICIAN_ROWS),
            },
            {
                "name": "records__site",
                "category": "records",
                "record_kind": "site",
                "columns": _SITE_COLUMNS,
                "rows": len(_SITE_ROWS),
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
                "name": "membership__site__coverage",
                "category": "membership",
                "record_kind": "site",
                "property": "coverage",
                "columns": _COVERAGE_COLUMNS,
                "rows": len(_COVERAGE_ROWS),
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


def _junction_table(plan: SourcePlan, name: str) -> SourceJunctionTablePlan:
    table = next(t for t in plan.tables if t.name == name)
    assert isinstance(table, SourceJunctionTablePlan)
    return table


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "emit"
        build_emit(emit_dir)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(_CONFIG_YAML, encoding="utf-8")
        config: ExportConfig = load_export_config(config_path)
        assert yaml.safe_load(_CONFIG_YAML)["source"]["tables"][0]["name"] == "day_ward"

        notices: list[Notice] = []

        with open_emit(emit_dir) as emit:
            # ---- 1. Full export: sub_types / where splits, row-disjoint ----
            out_dir = tmp_path / "full_export"
            out_dir.mkdir()
            row_counts = export_source(
                emit, config, out_dir, "csv", _ANCHOR, notices.append
            )
            print("=== 1. full export: owner sub_types / where splits ===")
            for name in ("day_ward", "night_ward", "north_coverage", "south_coverage"):
                print(f"  {name}: {row_counts[name]} rows")
            if any(row_counts[name] != 1 for name in row_counts):
                raise _fail(f"expected a 1/1 split each: got {row_counts}")
            print("  OK: each owner kind's two membership intervals split 1/1")
            print()

            # ---- 2. Owner column typing: narrowed population's own election
            print("=== 2. owner column typing: narrowed, not VARCHAR fallback ===")
            election = resolve_election(emit.sidecar, config.keys)
            plan = build_source_plan(
                emit, config, _ANCHOR, election, windowed=False, notices=notices.append
            )
            night_ward = _junction_table(plan, "night_ward")
            owner_edge = next(
                e for e in night_ward.edge_surfaces if e.source_column == "record_id"
            )
            print(
                f"  night_ward owner column rendered_type: {owner_edge.rendered_type}"
            )
            if owner_edge.rendered_type != "BIGINT":
                raise _fail(
                    "expected the narrowed 'night' population's own election"
                    f" (record_index -> BIGINT): got {owner_edge.rendered_type}"
                )
            # An unrestricted junction over the same mixed-election owner
            # would fall back to VARCHAR (no single agreed type) — the
            # narrowed unit's own agreement is what avoids that fallback.
            unrestricted_config = ExportConfig(
                mode="source",
                source=SourceConfig(
                    tables=(
                        SourceTableDecl(
                            name="all_ward",
                            membership=MembershipRef(
                                kind="clinician", property="ward_allocation"
                            ),
                        ),
                    )
                ),
                keys=config.keys,
            )
            unrestricted_plan = build_source_plan(
                emit,
                unrestricted_config,
                _ANCHOR,
                resolve_election(emit.sidecar, unrestricted_config.keys),
                False,
                notices.append,
            )
            all_ward = _junction_table(unrestricted_plan, "all_ward")
            all_owner_edge = next(
                e for e in all_ward.edge_surfaces if e.source_column == "record_id"
            )
            print(
                "  all_ward (unrestricted, full mixed domain) rendered_type:"
                f" {all_owner_edge.rendered_type}"
            )
            if all_owner_edge.rendered_type != "VARCHAR":
                raise _fail(
                    f"expected the mixed-domain fallback VARCHAR: got"
                    f" {all_owner_edge.rendered_type}"
                )
            print("  OK: narrowing to 'night' resolves BIGINT; unrestricted falls back")
            print()

            # ---- 3. Refusal: owner sub_types domain validation ----
            print("=== 3. refusal: owner sub_types domain validation ===")
            unknown_config = ExportConfig(
                mode="source",
                source=SourceConfig(
                    tables=(
                        SourceTableDecl(
                            name="evening_ward",
                            membership=MembershipRef(
                                kind="clinician", property="ward_allocation"
                            ),
                            sub_types=("evening",),
                        ),
                    )
                ),
                keys=config.keys,
            )
            try:
                build_source_plan(
                    emit,
                    unknown_config,
                    _ANCHOR,
                    resolve_election(emit.sidecar, unknown_config.keys),
                    False,
                    notices.append,
                )
            except SourceTableSubTypeUnknown as exc:
                print(f"  REFUSED (unknown owner sub_type 'evening'): {exc}")
            else:
                raise AssertionError("expected SourceTableSubTypeUnknown")

            flat_owner_config = ExportConfig(
                mode="source",
                source=SourceConfig(
                    tables=(
                        SourceTableDecl(
                            name="bad_site_split",
                            membership=MembershipRef(kind="site", property="coverage"),
                            sub_types=("north",),
                        ),
                    )
                ),
            )
            try:
                build_source_plan(
                    emit,
                    flat_owner_config,
                    _ANCHOR,
                    resolve_election(emit.sidecar, flat_owner_config.keys),
                    False,
                    notices.append,
                )
            except SourceSubTypesOnFlatKind as exc:
                print(f"  REFUSED (sub_types on flat owner 'site'): {exc}")
            else:
                raise AssertionError("expected SourceSubTypesOnFlatKind")

        print()
        print(
            "SUCCESS: owner sub_types / where split a sub-typed owner's junction"
            " and a flat owner's junction into row-disjoint tables; a narrowed"
            " owner column types by its own election, not the mixed-domain"
            " VARCHAR fallback; owner-domain misuse refuses at plan time"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
