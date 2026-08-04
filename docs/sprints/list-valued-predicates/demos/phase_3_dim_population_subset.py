#!/usr/bin/env python
"""
Demo: List-valued dim source population subset selection.

Sprint: list-valued-predicates
Phase: 3

Builds a minimal emit with:
  - `staff` — a sub-typed records kind (consultant / registrar / nurse /
    porter, via `prop__staff_type`).
  - `shift` — a records kind carrying a `prop__staff_id` reference to `staff`.

A YAML export config declares two dims over `staff`:
  - `dim_staff_clinical`, filtered to the three-element list
    `[consultant, registrar, nurse]` — a proper subset excluding porter. Its
    source population set is exactly those three sub-types
    (§ The dim source population set).
  - `dim_staff_all`, filtered to the full four-element domain in a
    different declaration order (`[porter, nurse, registrar, consultant]`)
    — a list naming the full domain composes no restriction, identical to
    omitting the filter.

`fact_shift` carries one reference FK to each dim (both inheriting an
elected `record_index` surface from `keys:`). Running the export shows:
  - `dim_staff_clinical` contains only the subset's three rows.
  - The porter-owned shift's FK to `dim_staff_clinical` resolves NULL — the
    out-of-subset owner, closure with no dangling reference.
  - The same shift's FK to `dim_staff_all` resolves normally — the
    full-domain list restricts nothing.

Then shows the per-element domain gate: a dim filtered to
`[consultant, orderly]` (`orderly` is not a declared staff sub-type) raises
an `ExportError` naming the offending element.
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
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.exporters.notices import render_notice_stderr
from fabulexa_forge.reader.emit import open_emit

_STAFF_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__staff_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_SHIFT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__staff_id",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
        "references": "staff",
    },
    {"name": "ref_index__staff_id", "type": "BIGINT"},
]

# consultant/registrar/nurse are the clinical subset; porter is left out.
_STAFF_ROWS: list[tuple[object, ...]] = [
    ("trunk", "s1", 10, True, None, 10, 0, "consultant"),
    ("trunk", "s2", 11, True, None, 11, 1, "registrar"),
    ("trunk", "s3", 12, True, None, 12, 2, "nurse"),
    ("trunk", "s4", 13, True, None, 13, 3, "porter"),
]

# sh1's owner (consultant) is in the clinical subset; sh2's (porter) is not.
_SHIFT_ROWS: list[tuple[object, ...]] = [
    ("trunk", "sh1", 20, True, None, 20, 0, "s1", 0),
    ("trunk", "sh2", 21, True, None, 21, 1, "s4", 3),
]

_SUBSET_CONFIG_YAML = """
mode: dimensional
keys:
  staff:
    consultant: record_index
    registrar: record_index
    nurse: record_index
    porter: record_index
dimensional:
  tables:
    - name: dim_staff_clinical
      role: dim
      scd: type1
      source:
        grain: records
        kind: staff
        filter:
          prop__staff_type: [consultant, registrar, nurse]
      key: [staff_index]
      columns:
        - {name: staff_index, from: record_index}
        - {name: staff_type, from: prop__staff_type}
    - name: dim_staff_all
      role: dim
      scd: type1
      source:
        grain: records
        kind: staff
        filter:
          prop__staff_type: [porter, nurse, registrar, consultant]
      key: [staff_index]
      columns:
        - {name: staff_index, from: record_index}
        - {name: staff_type, from: prop__staff_type}
    - name: fact_shift
      role: fact
      source:
        grain: records
        kind: shift
      key: [shift_id]
      columns:
        - {name: shift_id, from: record_id}
        - name: staff_clinical_id
          fk: {to: dim_staff_clinical, via: reference}
        - name: staff_all_id
          fk: {to: dim_staff_all, via: reference}
"""

_DOMAIN_GATE_CONFIG_YAML = """
mode: dimensional
dimensional:
  tables:
    - name: dim_staff_bad
      role: dim
      scd: type1
      source:
        grain: records
        kind: staff
        filter:
          prop__staff_type: [consultant, orderly]
      key: [record_id]
      columns:
        - {name: record_id, from: record_id}
    - name: fact_shift_bad
      role: fact
      source:
        grain: records
        kind: shift
      key: [shift_id]
      columns:
        - {name: shift_id, from: record_id}
        - name: staff_id
          fk: {to: dim_staff_bad, via: reference}
"""


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
    """Write the two-kind emit (staff + shift) into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_ddl("records__staff", _STAFF_COLUMNS))
    conn.execute(_ddl("records__shift", _SHIFT_COLUMNS))
    _insert_all(conn, "records__staff", _STAFF_COLUMNS, _STAFF_ROWS)
    _insert_all(conn, "records__shift", _SHIFT_COLUMNS, _SHIFT_ROWS)
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            {
                "name": "records__staff",
                "category": "records",
                "record_kind": "staff",
                "columns": _STAFF_COLUMNS,
                "rows": len(_STAFF_ROWS),
            },
            {
                "name": "records__shift",
                "category": "records",
                "record_kind": "shift",
                "columns": _SHIFT_COLUMNS,
                "rows": len(_SHIFT_ROWS),
            },
        ],
        "enum_domains": {
            "staff": {"staff_type": ["consultant", "registrar", "nurse", "porter"]}
        },
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _write_yaml(tmp_dir: Path, name: str, text: str) -> Path:
    path = tmp_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def _fail(message: str) -> "SystemExit":
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def demo_subset_closure_and_full_domain(tmp_dir: Path, emit_dir: Path) -> None:
    """The three-element subset restricts its dim and FK closure; the
    full-domain list (different order) restricts nothing."""
    print("=== list-valued discriminator selects a dim's source population ===")
    config_path = _write_yaml(tmp_dir, "subset.yaml", _SUBSET_CONFIG_YAML)
    config = load_export_config(config_path)

    out_path = tmp_dir / "out.duckdb"
    with open_emit(emit_dir) as emit:
        counts = export_dimensional(
            emit, config, out_path, "duckdb", None, notice_sink=render_notice_stderr
        )

    out_conn = duckdb.connect(str(out_path), read_only=True)
    clinical_rows = out_conn.execute(
        "SELECT staff_type FROM dim_staff_clinical ORDER BY staff_type"
    ).fetchall()
    all_rows = out_conn.execute(
        "SELECT staff_type FROM dim_staff_all ORDER BY staff_type"
    ).fetchall()
    shift_rows = out_conn.execute(
        "SELECT shift_id, staff_clinical_id, staff_all_id FROM fact_shift"
        " ORDER BY shift_id"
    ).fetchall()
    out_conn.close()

    if counts["dim_staff_clinical"] != 3:
        raise _fail(
            f"dim_staff_clinical should have 3 rows (the three-element"
            f" subset); got {counts['dim_staff_clinical']}"
        )
    if counts["dim_staff_all"] != 4:
        raise _fail(
            f"dim_staff_all should have 4 rows (the full domain, unrestricted);"
            f" got {counts['dim_staff_all']}"
        )

    print(f"  dim_staff_clinical ({counts['dim_staff_clinical']} rows, subset):")
    for row in clinical_rows:
        print(f"    {row}")
    print(f"  dim_staff_all ({counts['dim_staff_all']} rows, full domain):")
    for row in all_rows:
        print(f"    {row}")
    print("  fact_shift (shift_id, staff_clinical_id, staff_all_id):")
    for row in shift_rows:
        print(f"    {row}")

    by_shift = {row[0]: (row[1], row[2]) for row in shift_rows}
    if by_shift["sh1"] != (0, 0):
        raise _fail(
            f"sh1 (consultant, in both sets) should resolve 0/0; got {by_shift['sh1']}"
        )
    if by_shift["sh2"] != (None, 3):
        raise _fail(
            "sh2 (porter) should resolve NULL against the clinical subset and"
            f" 3 against the unrestricted full-domain dim; got {by_shift['sh2']}"
        )

    print(
        "  OK: the subset restricts dim_staff_clinical's population and its"
        " FK closure (sh2's porter owner -> NULL); the full-domain list"
        " (different order) restricts nothing (sh2's porter owner -> 3)"
    )
    print()


def demo_domain_gate_refusal(tmp_dir: Path, emit_dir: Path) -> None:
    """A list element outside the kind's declared domain raises ExportError,
    naming the offending element."""
    print("=== per-element domain gate: an out-of-domain list element ===")
    config_path = _write_yaml(tmp_dir, "domain_gate.yaml", _DOMAIN_GATE_CONFIG_YAML)
    config = load_export_config(config_path)

    out_path = tmp_dir / "out_bad.duckdb"
    try:
        with open_emit(emit_dir) as emit:
            export_dimensional(
                emit,
                config,
                out_path,
                "duckdb",
                None,
                notice_sink=render_notice_stderr,
            )
    except ExportError as exc:
        if "orderly" not in str(exc):
            raise _fail(f"expected the refusal to name 'orderly'; got: {exc}") from exc
        print(f"  OK: {exc}")
    else:
        raise _fail("expected ExportError for an out-of-domain list element")
    print()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        emit_dir = tmp_dir / "emit"
        _build_emit(emit_dir)

        demo_subset_closure_and_full_domain(tmp_dir, emit_dir)
        demo_domain_gate_refusal(tmp_dir, emit_dir)

        print(
            "SUCCESS: a list on a sub-typed dim's discriminator selects exactly"
            " those populations, closing FK output over the subset; a"
            " full-domain list restricts nothing; an out-of-domain element"
            " fails loudly, naming itself"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
