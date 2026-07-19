#!/usr/bin/env python
"""
Demo: Dimensional refusal, lookup regate, init skip (SliceOnlyColumnRefused)

Sprint: slice-only-policy
Phase: 2

Builds a standalone emit with two kinds:
  - actor: sub-typed via prop__actor_type (the exempt discriminator, declared
    slice_only on purpose — exempt at ANY class), plus prop__loyalty_tier
    (non-exempt slice_only), prop__status (tracked), and two reference
    columns to team — prop__team_id (slice_only) and prop__home_team_id
    (constant).
  - team: sub-typed via prop__team_type (tracked, exempt discriminator) and
    prop__budget (non-exempt slice_only).

Demonstrates, directly against build_query_specs:
  - `from: prop__loyalty_tier` -> SliceOnlyColumnRefused
  - a records `filter: {prop__loyalty_tier: ...}` key -> SliceOnlyColumnRefused
  - an `fk via: reference` hop over prop__team_id -> SliceOnlyColumnRefused
  - a `lookup` terminal on team.prop__budget -> LookupColumnSafety (constant-only)
  - the same reads through the exempt discriminator (prop__actor_type,
    prop__team_type) pass despite carrying slice_only / tracked classes
  - a `tracked` discriminator `lookup` terminal (team.prop__team_type) passes
    (the deliberate loosening)

And, against generate_init_config on a second minimal emit:
  - a non-exempt slice_only column is skipped from the SCD-2 stub's column
    list, with one 'slice-only-column-omitted' notice
  - the kind itself, and its tracked column, are still proposed
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.dimensional.init import generate_init_config
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.reader.emit import open_emit

# ---------------------------------------------------------------------------
# Emit 1: actor + team, for the refusal / regate / carve-out demonstrations
# ---------------------------------------------------------------------------

_ACTOR_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__actor_type",
        "type": "VARCHAR",
        "history_tracked": False,
        # exempt discriminator: slice_only, still projectable
        "temporal_class": "slice_only",
    },
    {
        "name": "prop__loyalty_tier",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "slice_only",  # non-exempt: refused everywhere it's read
    },
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__team_id",
        "type": "VARCHAR",
        "references": "team",
        "history_tracked": False,
        "temporal_class": "slice_only",  # fk hop refusal target
    },
    {"name": "ref_index__team_id", "type": "BIGINT"},
    {
        "name": "prop__home_team_id",
        "type": "VARCHAR",
        "references": "team",
        "history_tracked": False,
        "temporal_class": "constant",  # a safe hop for the lookup demonstrations
    },
    {"name": "ref_index__home_team_id", "type": "BIGINT"},
]

_TEAM_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__team_type",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",  # exempt discriminator, tracked -> the loosening
    },
    {
        "name": "prop__budget",
        "type": "BIGINT",
        "history_tracked": False,
        "temporal_class": "slice_only",  # lookup-terminal refusal target
    },
]

# The passing config: exempt discriminator from/filter, tracked-discriminator lookup.
YAML_PASS = """
mode: dimensional
dimensional:
  tables:
    - name: dim_actor
      role: dim
      scd: type1
      source:
        grain: records
        kind: actor
        filter:
          prop__actor_type: patient
      key: [id]
      columns:
        - name: id
          from: record_id
        - name: kind
          from: prop__actor_type
        - name: home_team_kind
          lookup:
            property: team_type
            to: team
            path: [prop__home_team_id]
"""

YAML_REFUSE_FROM = """
mode: dimensional
dimensional:
  tables:
    - name: dim_actor
      role: dim
      scd: type1
      source:
        grain: records
        kind: actor
      key: [id]
      columns:
        - name: id
          from: record_id
        - name: tier
          from: prop__loyalty_tier
"""

YAML_REFUSE_FILTER = """
mode: dimensional
dimensional:
  tables:
    - name: dim_actor
      role: dim
      scd: type1
      source:
        grain: records
        kind: actor
        filter:
          prop__loyalty_tier: gold
      key: [id]
      columns:
        - name: id
          from: record_id
"""

YAML_REFUSE_FK = """
mode: dimensional
dimensional:
  tables:
    - name: dim_team
      role: dim
      scd: type1
      source:
        grain: records
        kind: team
      key: [id]
      columns:
        - name: id
          from: record_id
    - name: fact_actor
      role: fact
      source:
        grain: records
        kind: actor
      key: [id]
      columns:
        - name: id
          from: record_id
        - name: team_id
          fk:
            to: dim_team
            via: reference
            path: [prop__team_id]
"""

YAML_REFUSE_LOOKUP = """
mode: dimensional
dimensional:
  tables:
    - name: dim_actor
      role: dim
      scd: type1
      source:
        grain: records
        kind: actor
      key: [id]
      columns:
        - name: id
          from: record_id
        - name: team_budget
          lookup:
            property: budget
            to: team
            path: [prop__home_team_id]
"""


def _build_actor_team_emit(emit_dir: Path) -> None:
    """Write the actor + team run.duckdb + base.json emit."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    team_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _TEAM_COLUMNS)
    conn.execute(f'CREATE TABLE "records__team" ({team_ddl})')
    conn.execute(
        'INSERT INTO "records__team" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "t1", 0, True, 0, 0, "red", 1000],
    )

    actor_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _ACTOR_COLUMNS)
    conn.execute(f'CREATE TABLE "records__actor" ({actor_ddl})')
    conn.execute(
        'INSERT INTO "records__actor" VALUES'
        " (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "trunk",
            "a1",
            0,
            True,
            0,
            0,
            "patient",
            "gold",
            "active",
            "t1",
            0,
            "t1",
            0,
        ],
    )
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "tables": [
            {
                "name": "records__team",
                "category": "records",
                "columns": _TEAM_COLUMNS,
                "rows": 1,
                "record_kind": "team",
            },
            {
                "name": "records__actor",
                "category": "records",
                "columns": _ACTOR_COLUMNS,
                "rows": 1,
                "record_kind": "actor",
            },
        ],
        "enum_domains": {
            "actor": {"actor_type": ["patient", "staff"]},
            "team": {"team_type": ["red", "blue"]},
        },
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


class NoticeCollector:
    """Callable NoticeSink appending every received Notice to `self.notices`."""

    def __init__(self) -> None:
        self.notices: list[Notice] = []

    def __call__(self, notice: Notice) -> None:
        self.notices.append(notice)


def _compile(emit_dir: Path, config_yaml: str, tmp_path: Path, name: str) -> None:
    """Parse and compile one YAML config against the actor/team emit."""
    config_path = tmp_path / f"{name}.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")
    config = load_export_config(config_path)
    assert config.dimensional is not None
    with open_emit(emit_dir) as emit:
        build_query_specs(
            emit, config.dimensional, None, None, notice_sink=NoticeCollector()
        )


def _expect_refused(
    emit_dir: Path, config_yaml: str, tmp_path: Path, name: str, must_contain: str
) -> None:
    """Compile a config expected to raise ExportError naming `must_contain`."""
    try:
        _compile(emit_dir, config_yaml, tmp_path, name)
    except ExportError as exc:
        if must_contain not in str(exc):
            print(
                f"FAIL: {name} refused, but message missing {must_contain!r}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        print(f"REFUSED ({name}): {exc}")
        return
    print(f"FAIL: {name} compiled but should have been refused", file=sys.stderr)
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Emit 2: a bare-string dim kind for the init skip demonstration
# ---------------------------------------------------------------------------

_EMPLOYEE_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__name",
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
    {
        "name": "prop__ssn",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "slice_only",
    },
]


def _build_employee_emit(emit_dir: Path) -> None:
    """Write a minimal employee (bare-string dim, history_tracked) emit."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _EMPLOYEE_COLUMNS)
    conn.execute(f'CREATE TABLE "records__employee" ({ddl})')
    conn.execute(
        'INSERT INTO "records__employee" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "e1", 0, True, 0, 0, "Alice", "active", "123-45-6789"],
    )
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__employee",
                "category": "records",
                "columns": _EMPLOYEE_COLUMNS,
                "rows": 1,
                "record_kind": "employee",
            },
        ],
        "record_roles": {"employee": "dimension"},
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "emit"
        _build_actor_team_emit(emit_dir)

        # --- Passing config: exempt discriminator + tracked-discriminator lookup ---
        _compile(emit_dir, YAML_PASS, tmp_path, "pass")
        print(
            "PASSED (pass): exempt discriminator from/filter + tracked lookup terminal"
        )

        # --- Four refusal surfaces ---
        _expect_refused(
            emit_dir,
            YAML_REFUSE_FROM,
            tmp_path,
            "refuse_from",
            "temporal_class: slice_only",
        )
        _expect_refused(
            emit_dir,
            YAML_REFUSE_FILTER,
            tmp_path,
            "refuse_filter",
            "filter key",
        )
        _expect_refused(
            emit_dir,
            YAML_REFUSE_FK,
            tmp_path,
            "refuse_fk",
            "fk hop column",
        )
        _expect_refused(
            emit_dir,
            YAML_REFUSE_LOOKUP,
            tmp_path,
            "refuse_lookup",
            "terminal property",
        )

        # --- init: slice_only column skipped, kind + discriminator still proposed ---
        employee_dir = tmp_path / "employee_emit"
        _build_employee_emit(employee_dir)
        collector = NoticeCollector()
        with open_emit(employee_dir) as emit:
            candidate_yaml = generate_init_config(emit, collector)

        if "prop__ssn" in candidate_yaml:
            print(
                "FAIL: init proposed the slice_only column prop__ssn", file=sys.stderr
            )
            return 1
        if "kind: employee" not in candidate_yaml:
            print("FAIL: init did not propose the employee kind", file=sys.stderr)
            return 1
        if "prop__status" not in candidate_yaml:
            print("FAIL: init dropped the tracked column too", file=sys.stderr)
            return 1
        skip_notices = [
            n for n in collector.notices if n.code == "slice-only-column-omitted"
        ]
        if len(skip_notices) != 1 or "prop__ssn" not in skip_notices[0].message:
            print(
                "FAIL: expected exactly one skip notice naming prop__ssn,"
                f" got {collector.notices}",
                file=sys.stderr,
            )
            return 1
        print(f"SKIPPED (init): {skip_notices[0].message}")

        print(
            "SUCCESS: slice_only refused on from/filter/fk/lookup, exempt discriminator"
            " and tracked-discriminator lookup pass, init skips with a notice"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
