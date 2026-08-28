#!/usr/bin/env python
"""
Demo: Dimensional provenance stamping -- ColumnProvenance carried through
build_grain_sql's fifth element, QuerySpec, and forwarded verbatim onto
TableReport.

Sprint: documentation-channel
Phase: 3

Builds a fixture emit (records__team, records__actor referencing team,
records__tick_decision) and compiles two dimensional tables:

- dim_actor: `actor_id` (from, a straight projection), `display_name`
  (correlation, a rename), `status_label` (derived: value_map, a value
  rendering election), `team_name` (lookup, the looked-up property's own
  (table, column)) -- every column faithfully carried, so every one gets a
  provenance entry.
- fact_decision: `decision_id` / `journey_id` (from, carried) plus `seq`
  (derived: ordinal) and `wait_minutes` (derived: elapsed) -- both computed,
  so neither gets a provenance entry.

Prints each compiled spec's provenance map, then runs the full export and
shows the written TableReport's provenance/kind_values equal the spec's --
the forwarding `write_query_specs` performs, unchanged from the plan.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import (
    ColumnDecl,
    DerivedSpec,
    DimensionalConfig,
    ElapsedSpec,
    LookupClause,
    OrdinalSpec,
    SourceDecl,
    TableDecl,
    ValueMapSpec,
)
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.query_spec import write_query_specs
from fabulexa_forge.reader.emit import open_emit

_TEAM_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__team_name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_ACTOR_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__full_name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__team_id",
        "type": "VARCHAR",
        "references": "team",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {"name": "ref_index__team_id", "type": "BIGINT"},
]

_DECISION_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__journey_id",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__decision_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]


def _create_ddl(table_name: str, columns: list[dict[str, object]]) -> str:
    """Build a CREATE TABLE DDL statement from a column spec list."""
    frags = ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)
    return f'CREATE TABLE "{table_name}" ({frags})'


def _build_emit(emit_dir: Path) -> None:
    """Write a fixture emit: one team, two actors, two decisions in a journey."""
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))

    conn.execute(_create_ddl("records__team", _TEAM_COLUMNS))
    conn.execute(
        'INSERT INTO "records__team" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "t1", 0, True, None, 0, 0, "Cardiology"],
    )

    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a1", 10, True, None, 10, 0, "Dr. Smith", "A", "t1", 0],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a2", 20, True, None, 20, 1, "Dr. Jones", "I", "t1", 0],
    )

    conn.execute(_create_ddl("records__tick_decision", _DECISION_COLUMNS))
    conn.execute(
        'INSERT INTO "records__tick_decision" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "d1", 100, True, None, 100, 0, "j1", "arrival"],
    )
    conn.execute(
        'INSERT INTO "records__tick_decision" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "d2", 145, True, None, 145, 1, "j1", "triage"],
    )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"},
        "tables": [
            {
                "name": "records__team",
                "category": "records",
                "record_kind": "team",
                "rows": 1,
                "columns": _TEAM_COLUMNS,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "rows": 2,
                "columns": _ACTOR_COLUMNS,
            },
            {
                "name": "records__tick_decision",
                "category": "records",
                "record_kind": "tick_decision",
                "rows": 2,
                "columns": _DECISION_COLUMNS,
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _dimensional_config() -> DimensionalConfig:
    """A dim (rename + lookup + derived value rendering) and a fact (elapsed + seq)."""
    dim_actor = TableDecl(
        name="dim_actor",
        role="dim",
        source=SourceDecl(grain="records", kind="actor"),
        key=["actor_id"],
        columns=[
            ColumnDecl(name="actor_id", **{"from": "record_id"}),
            ColumnDecl(name="display_name", correlation="prop__full_name"),
            ColumnDecl(
                name="status_label",
                derived=DerivedSpec(
                    value_map=ValueMapSpec(
                        **{"from": "prop__status"},
                        map={"A": "Active", "I": "Inactive"},
                    )
                ),
            ),
            ColumnDecl(
                name="team_name",
                lookup=LookupClause(property="team_name", to="team"),
            ),
        ],
    )
    fact_decision = TableDecl(
        name="fact_decision",
        role="fact",
        source=SourceDecl(grain="records", kind="tick_decision"),
        key=["decision_id"],
        columns=[
            ColumnDecl(name="decision_id", **{"from": "record_id"}),
            ColumnDecl(name="journey_id", **{"from": "prop__journey_id"}),
            ColumnDecl(name="changed_at", **{"from": "last_mutation_sim_time"}),
            ColumnDecl(
                name="seq",
                derived=DerivedSpec(
                    ordinal=OrdinalSpec(
                        partition_by="journey_id", order_by="changed_at"
                    )
                ),
            ),
            ColumnDecl(
                name="wait_minutes",
                derived=DerivedSpec(
                    elapsed=ElapsedSpec(
                        correlate_on="prop__journey_id",
                        other_where={"prop__decision_type": "arrival"},
                        start_source="last_mutation_sim_time",
                        end_source="last_mutation_sim_time",
                        unit="minutes",
                    )
                ),
            ),
        ],
    )
    return DimensionalConfig(tables=[dim_actor, fact_decision])


def _discard(_notice: Notice) -> None:
    """Discard a plan notice -- the demo is indifferent to them."""


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
        _build_emit(emit_dir)

        with open_emit(emit_dir) as emit:
            config = _dimensional_config()
            specs = build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=_discard,
                base_relations=None,
            )

            print("=== Compiled provenance, per spec ===")
            for spec in specs:
                print(f"\n{spec.table_name}:")
                for col_name, entry in spec.provenance.items():
                    print(
                        f"  {col_name} -> ({entry.source_table}, {entry.source_column})"
                    )
                computed = [
                    col.name
                    for tbl in config.tables
                    if tbl.name == spec.table_name
                    for col in tbl.columns
                    if col.name not in spec.provenance
                ]
                print(f"  computed (no entry): {computed}")
                print(f"  kind_values: {dict(spec.kind_values)!r}")

            out = emit_dir / "export.duckdb"
            report = write_query_specs(emit, specs, out, fmt="duckdb")

        print("\n=== TableReport forwards the spec's maps verbatim ===")
        by_name = {spec.table_name: spec for spec in specs}
        ok = True
        for table in report.tables:
            spec = by_name[table.name]
            matches = (
                table.provenance == spec.provenance
                and table.kind_values == spec.kind_values
            )
            print(f"{table.name}: report == spec -> {matches}")
            ok = ok and matches

    dim_spec = by_name["dim_actor"]
    fact_spec = by_name["fact_decision"]
    expected_dim_cols = {"actor_id", "display_name", "status_label", "team_name"}
    if set(dim_spec.provenance) != expected_dim_cols:
        return 1
    if dim_spec.provenance["team_name"].source_table != "records__team":
        return 1
    if dim_spec.provenance["team_name"].source_column != "prop__team_name":
        return 1
    if "seq" in fact_spec.provenance or "wait_minutes" in fact_spec.provenance:
        return 1
    if not ok:
        return 1

    print(
        "\nSUCCESS: carried columns stamp (table, column) provenance;"
        " computed columns get no entry; TableReport forwards both maps"
        " verbatim from the compiled QuerySpec"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
