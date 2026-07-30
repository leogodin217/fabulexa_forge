#!/usr/bin/env python
"""
Demo: Dimensional mode election — FK inheritance over a dim's source
population set, explicit target_key override, out-of-set NULL, the dim-key
agreement and inheritance-ambiguity gates, and the pre-election
target_key: presentation_id subsumption
(`exporters/dimensional/populations.py`, `exporters/dimensional/fk.py`,
`exporters/dimensional/validation.py`, `exporters/dimensional/engine.py`).

Sprint: key-election
Phase: 6

Builds one declared, two-kind emit:
  - `entity` — sub-typed (`alpha` / `beta`), only `alpha` presentation_id
    declared (`ALPHA_` prefix).
  - `booking` — untracked fact kind; `prop__entity_id` references `entity`.
    b1 -> e1 (alpha); b2 -> e2 (beta).

`dim_entity_alpha` is a dim over `entity` filtered to `alpha`
(`source.filter: {prop__entity_type: alpha}`), keyed `from: presentation_id`.

Under `keys: {entity: {alpha: presentation_id, beta: record_index}}`:

1. `fact_booking.entity_alpha_id` (no target_key) inherits `dim_entity_alpha`'s
   population set's one election (`presentation_id`) — b1 renders `ALPHA_001`.
2. The same edge's b2 (out-of-set: e2 is `beta`, outside the alpha-filtered
   dim's population set) renders NULL — the join's identity relation is
   restricted to the dim's population, so a beta record_id matches no row.
3. `fact_booking.entity_alpha_index_id` (`target_key: record_index`, an
   explicit per-edge override beside the inherited column) renders the
   target's `BIGINT` record_index instead — b1 -> 0.
4. A dim keyed `from: record_id` instead of `presentation_id`, with an
   inherited (non-`record_id`) edge into it, fires `ElectionDimKeyDisagrees`
   statically (no data read).
5. A dim with no discriminator filter (the whole mixed-election `entity`
   domain) targeted by an inherited edge fires `ElectionInheritanceAmbiguous`
   statically.
6. `fact_booking.entity_alpha_explicit_pid_id` (`target_key: presentation_id`)
   still renders `ALPHA_...` codes with **no** `keys` block at all — the
   pre-election `target_key: presentation_id` subsumption: the shipped
   column-presence check is now the registry-membership check over the
   dim's source population set, and needs no election.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import (
    ColumnDecl,
    DimensionalConfig,
    ExportConfig,
    FkClause,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.errors import ElectionDimKeyDisagrees, ElectionInheritanceAmbiguous
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.reader.emit import open_emit

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
        "name": "prop__entity_id",
        "type": "VARCHAR",
        "references": "entity",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {"name": "ref_index__entity_id", "type": "BIGINT"},
]

# entity: e1 alpha (registry-declared ALPHA_001), e2 beta (undeclared).
_ENTITY_ROWS: list[tuple[object, ...]] = [
    ("trunk", "e1", "ALPHA_001", 10, True, None, 10, 0, "alpha"),
    ("trunk", "e2", None, 10, True, None, 10, 1, "beta"),
]

# booking: b1 -> alpha entity e1 (in dim_entity_alpha's population set);
# b2 -> beta entity e2 (outside it).
_BOOKING_ROWS: list[tuple[object, ...]] = [
    ("trunk", "b1", 20, True, None, 20, 0, "e1", 0),
    ("trunk", "b2", 25, True, None, 25, 1, "e2", 1),
]

_PRESENTATION_KEYS: dict[str, object] = {
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
    }
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
    """Write the star-over-a-declared-kind emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_ddl("records__entity", _ENTITY_COLUMNS))
    conn.execute(_ddl("records__booking", _BOOKING_COLUMNS))
    _insert_all(conn, "records__entity", _ENTITY_COLUMNS, _ENTITY_ROWS)
    _insert_all(conn, "records__booking", _BOOKING_COLUMNS, _BOOKING_ROWS)
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
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
        ],
        "enum_domains": {"entity": {"entity_type": ["alpha", "beta"]}},
        "presentation_keys": _PRESENTATION_KEYS,
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _fail(message: str) -> "SystemExit":
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def _dim_entity_alpha(key_from: str) -> TableDecl:
    """The alpha-filtered dim, keyed `from: key_from`."""
    return TableDecl(
        name="dim_entity_alpha",
        role="dim",
        source=SourceDecl(
            grain="records", kind="entity", filter={"prop__entity_type": "alpha"}
        ),
        key=["entity_key"],
        columns=[ColumnDecl(name="entity_key", from_=key_from)],
    )


def _dim_entity_all() -> TableDecl:
    """The unfiltered dim over entity's whole (mixed-election) domain."""
    return TableDecl(
        name="dim_entity_all",
        role="dim",
        source=SourceDecl(grain="records", kind="entity"),
        key=["entity_key"],
        columns=[ColumnDecl(name="entity_key", from_="record_id")],
    )


def _fact_booking(*fk_columns: ColumnDecl) -> TableDecl:
    return TableDecl(
        name="fact_booking",
        role="fact",
        source=SourceDecl(grain="records", kind="booking"),
        key=["booking_key"],
        columns=[
            ColumnDecl(name="booking_key", from_="record_id"),
            *fk_columns,
        ],
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)

        elected_keys = {"entity": {"alpha": "presentation_id", "beta": "record_index"}}

        # ---- 1 & 2. Inheritance + out-of-set NULL, 3. explicit override ----
        print("=== inherited presentation_id, out-of-set NULL, explicit override ===")
        config_elected = ExportConfig(
            mode="dimensional",
            keys=elected_keys,
            dimensional=DimensionalConfig(
                tables=[
                    _dim_entity_alpha(key_from="presentation_id"),
                    _fact_booking(
                        ColumnDecl(
                            name="entity_alpha_id",
                            fk=FkClause(to="dim_entity_alpha", via="reference"),
                        ),
                        ColumnDecl(
                            name="entity_alpha_index_id",
                            fk=FkClause(
                                to="dim_entity_alpha",
                                via="reference",
                                target_key="record_index",
                            ),
                        ),
                    ),
                ]
            ),
        )
        with open_emit(emit_dir) as emit:
            election = resolve_election(emit.sidecar, config_elected.keys)
            specs = build_query_specs(
                emit,
                config_elected.dimensional,
                None,
                None,
                lambda _n: None,
                base_relations=None,
                election=election,
            )
            fact_sql = next(s.sql for s in specs if s.table_name == "fact_booking")
            rows = {r[0]: r for r in emit.query(fact_sql, ())}
        b1, b2 = rows["b1"], rows["b2"]
        if "ALPHA_001" not in b1:
            raise _fail(f"b1 {b1!r} should inherit ALPHA_001")
        if None not in b2 or "ALPHA_001" in b2:
            raise _fail(f"b2 {b2!r} should render NULL (out-of-set beta target)")
        if 0 not in b1:
            raise _fail(f"b1 {b1!r} should carry the explicit record_index override 0")
        print(f"  b1 = {b1}")
        print(f"  b2 = {b2}")
        print("  OK: inherited=ALPHA_001, out-of-set=NULL, override=record_index 0")
        print()

        # ---- 4. ElectionDimKeyDisagrees ------------------------------------
        print("=== a dim keyed from: record_id disagrees with an inherited edge ===")
        config_bad_key = ExportConfig(
            mode="dimensional",
            keys=elected_keys,
            dimensional=DimensionalConfig(
                tables=[
                    _dim_entity_alpha(key_from="record_id"),
                    _fact_booking(
                        ColumnDecl(
                            name="entity_alpha_id",
                            fk=FkClause(to="dim_entity_alpha", via="reference"),
                        )
                    ),
                ]
            ),
        )
        try:
            with open_emit(emit_dir) as emit:
                election = resolve_election(emit.sidecar, config_bad_key.keys)
                build_query_specs(
                    emit,
                    config_bad_key.dimensional,
                    None,
                    None,
                    lambda _n: None,
                    base_relations=None,
                    election=election,
                )
            raise _fail("expected ElectionDimKeyDisagrees")
        except ElectionDimKeyDisagrees as exc:
            print(f"  OK: {exc}")
        print()

        # ---- 5. ElectionInheritanceAmbiguous -------------------------------
        print("=== an unfiltered dim over a mixed-election kind is ambiguous ===")
        config_ambiguous = ExportConfig(
            mode="dimensional",
            keys=elected_keys,
            dimensional=DimensionalConfig(
                tables=[
                    _dim_entity_all(),
                    _fact_booking(
                        ColumnDecl(
                            name="entity_id",
                            fk=FkClause(to="dim_entity_all", via="reference"),
                        )
                    ),
                ]
            ),
        )
        try:
            with open_emit(emit_dir) as emit:
                election = resolve_election(emit.sidecar, config_ambiguous.keys)
                build_query_specs(
                    emit,
                    config_ambiguous.dimensional,
                    None,
                    None,
                    lambda _n: None,
                    base_relations=None,
                    election=election,
                )
            raise _fail("expected ElectionInheritanceAmbiguous")
        except ElectionInheritanceAmbiguous as exc:
            print(f"  OK: {exc}")
        print()

        # ---- 6. target_key: presentation_id subsumption, no keys block ----
        print("=== explicit target_key: presentation_id renders with NO keys block ===")
        config_no_keys = ExportConfig(
            mode="dimensional",
            dimensional=DimensionalConfig(
                tables=[
                    _dim_entity_alpha(key_from="presentation_id"),
                    _fact_booking(
                        ColumnDecl(
                            name="entity_alpha_explicit_pid_id",
                            fk=FkClause(
                                to="dim_entity_alpha",
                                via="reference",
                                target_key="presentation_id",
                            ),
                        )
                    ),
                ]
            ),
        )
        if config_no_keys.keys is not None:
            raise _fail("this scenario must declare no keys block")
        with open_emit(emit_dir) as emit:
            election = resolve_election(emit.sidecar, config_no_keys.keys)
            specs = build_query_specs(
                emit,
                config_no_keys.dimensional,
                None,
                None,
                lambda _n: None,
                base_relations=None,
                election=election,
            )
            fact_sql = next(s.sql for s in specs if s.table_name == "fact_booking")
            rows = {r[0]: r for r in emit.query(fact_sql, ())}
        b1 = rows["b1"]
        if "ALPHA_001" not in b1:
            raise _fail(f"b1 {b1!r} should render ALPHA_001 via the explicit override")
        print(f"  b1 = {b1}")
        print("  OK: explicit target_key: presentation_id needs no keys block")
        print()

        print(
            "SUCCESS: dimensional FKs inherit a dim's source population set's"
            " election, restrict out-of-set targets to NULL, honor an explicit"
            " target_key override, gate dim-key agreement and inheritance"
            " ambiguity statically, and the presentation_id subsumption needs"
            " no keys block"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
