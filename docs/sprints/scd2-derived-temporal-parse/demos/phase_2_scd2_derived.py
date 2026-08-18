#!/usr/bin/env python
"""
Demo: SCD-2 per-record derived columns
Sprint: scd2-derived-temporal-parse
Phase: 2

`scd: type2` dims accept `derived: timestamp` / `date_parse` / `value_map`
from untracked sources: the type2 build compiles them through the same
per-column builders the records grain uses, bound to the reader records
relation, so the value is constant across a record's version rows. The
`Scd2DerivedSourceUntracked` rule refuses a derived spec that sources a
history-tracked property; the amended `Scd2ColumnModeSupported` message
still refuses `derived: ordinal` (and `fk` / `correlation` / `derived:
elapsed`) on a type2 table.

Shows:
  1. A type2 dim declaring a tracked `tier`, `derived: date_parse` on
     `birth_date`, an elected `derived: timestamp` on `created_sim_time`,
     and `derived: value_map` on `region` — three version rows for one
     record with per-version `tier` beside version-constant derived values.
  2. Two load-time refusals: a derived parse sourcing the tracked
     `prop__tier` (`Scd2DerivedSourceUntracked`), and `derived: ordinal` on
     the type2 table (amended `Scd2ColumnModeSupported` message).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    ColumnDecl,
    DateParseSpec,
    DerivedSpec,
    DimensionalConfig,
    ExportConfig,
    OrdinalSpec,
    SourceDecl,
    TableDecl,
    TimestampSpec,
    ValueMapSpec,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.engine import (
    build_query_specs,
    export_dimensional,
)
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.reader.emit import open_emit

_ANCHOR_ZONE = "UTC"
_ANCHOR_START_DATETIME = "2024-01-01T00:00:00+00:00"

_ACTOR_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__tier",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__birth_date",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__region",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
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


def _fail(message: str) -> SystemExit:
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def _discard_notice(notice: Notice) -> None:
    pass


def _create_ddl(table_name: str, columns: list[dict[str, object]]) -> str:
    """Build a `CREATE TABLE` statement from a sidecar-shaped column list."""
    col_sql = ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)
    return f'CREATE TABLE "{table_name}" ({col_sql})'


def build_fixture_emit(emit_dir: Path) -> None:
    """Write the `actor` fixture emit: one record whose tracked `tier`
    changes three times, alongside untracked `birth_date` and `region`
    payload columns.

    Args:
        emit_dir: Directory to write base.json + run.duckdb into.
    """
    emit_dir.mkdir(parents=True, exist_ok=True)

    tables: list[dict[str, object]] = [
        {
            "name": "records__actor",
            "category": "records",
            "record_kind": "actor",
            "columns": _ACTOR_COLUMNS,
            "rows": 1,
        },
        {
            "name": "history",
            "category": "fixed",
            "columns": _HISTORY_COLUMNS,
            "rows": 3,
        },
    ]
    base_json: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 30}],
        "tables": tables,
        "record_roles": {"actor": "dimension"},
        "runtime": {"timezone": _ANCHOR_ZONE, "start_datetime": _ANCHOR_START_DATETIME},
    }
    (emit_dir / "base.json").write_text(json.dumps(base_json), encoding="utf-8")

    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "a001", 0, True, 30, 0, "gold", "1990-05-20", "north"],
    )
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    for sim_time, value in ((10, "bronze"), (20, "silver"), (30, "gold")):
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "actor", "a001", "tier", sim_time, value],
        )
    conn.close()


def _dim_table_decl() -> TableDecl:
    """The type2 dim: tracked `tier`, and the three derived per-record modes."""
    return TableDecl(
        name="dim_actor",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="tier", **{"from": "prop__tier"}),
            ColumnDecl(
                name="birth_date",
                derived=DerivedSpec(
                    date_parse=DateParseSpec(
                        **{"from": "prop__birth_date", "format": "%Y-%m-%d"}
                    )
                ),
            ),
            ColumnDecl(
                name="created_at",
                derived=DerivedSpec(
                    timestamp=TimestampSpec(
                        source="created_sim_time", **{"as": "timestamp"}
                    )
                ),
            ),
            ColumnDecl(
                name="region",
                derived=DerivedSpec(
                    value_map=ValueMapSpec(
                        **{"from": "prop__region"}, map={"north": 1, "south": 2}
                    )
                ),
            ),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
            ColumnDecl(name="valid_to", derived=DerivedSpec(scd_window="valid_to")),
        ],
    )


def show_derived_export(tmp_path: Path, emit_dir: Path) -> None:
    """Export the type2 dim and print per-version tier beside the
    version-constant derived columns."""
    config = ExportConfig(
        mode="dimensional", dimensional=DimensionalConfig(tables=[_dim_table_decl()])
    )
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None, "the fixture sidecar carries a runtime anchor"
        export_dimensional(emit, config, out_path, "duckdb", anchor, _discard_notice)

    con = duckdb.connect(str(out_path), read_only=True)
    rows = con.sql(
        "SELECT id, tier, birth_date, created_at, region, valid_from, valid_to"
        ' FROM "dim_actor" ORDER BY valid_from'
    ).fetchall()
    con.close()

    if len(rows) != 3:
        raise _fail(f"expected 3 versions, got {len(rows)}")
    for row in rows:
        print(f"  {row}")

    tiers = [row[1] for row in rows]
    if tiers != ["bronze", "silver", "gold"]:
        raise _fail(f"expected tier to change per version, got {tiers}")
    derived_cols = [row[2:5] for row in rows]
    if len(set(derived_cols)) != 1:
        raise _fail(f"expected derived columns constant across versions, got {rows}")
    print("  OK: tier varies per version; birth_date/created_at/region stay constant")


def show_derived_source_untracked_refusal(emit_dir: Path) -> None:
    """A derived date_parse sourcing the tracked prop__tier is refused."""
    table_decl = TableDecl(
        name="dim_actor_bad",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(
                name="bad",
                derived=DerivedSpec(
                    date_parse=DateParseSpec(
                        **{"from": "prop__tier", "format": "%Y-%m-%d"}
                    )
                ),
            ),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
            ColumnDecl(name="valid_to", derived=DerivedSpec(scd_window="valid_to")),
        ],
    )
    config = DimensionalConfig(tables=[table_decl])
    with open_emit(emit_dir) as emit:
        try:
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=_discard_notice,
                base_relations=None,
            )
            raise _fail("expected Scd2DerivedSourceUntracked, export succeeded")
        except ExportError as exc:
            message = str(exc)
            print(f"  {message}")
            for needle in ("bad", "dim_actor_bad", "prop__tier", "history-tracked"):
                if needle not in message:
                    raise _fail(f"refusal message missing {needle!r}: {message}")
            print("  OK: Scd2DerivedSourceUntracked refuses the tracked source")


def show_ordinal_mode_refusal(emit_dir: Path) -> None:
    """`derived: ordinal` on a type2 table is refused with the amended
    Scd2ColumnModeSupported message naming the admitted derived modes."""
    table_decl = TableDecl(
        name="dim_actor_ordinal",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(
                name="rank",
                derived=DerivedSpec(
                    ordinal=OrdinalSpec(partition_by="id", order_by="valid_from")
                ),
            ),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
            ColumnDecl(name="valid_to", derived=DerivedSpec(scd_window="valid_to")),
        ],
    )
    config = DimensionalConfig(tables=[table_decl])
    with open_emit(emit_dir) as emit:
        try:
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=_discard_notice,
                base_relations=None,
            )
            raise _fail("expected Scd2ColumnModeSupported, export succeeded")
        except ExportError as exc:
            message = str(exc)
            print(f"  {message}")
            for needle in (
                "derived: ordinal",
                "derived: timestamp",
                "derived: value_map",
            ):
                if needle not in message:
                    raise _fail(f"refusal message missing {needle!r}: {message}")
            print("  OK: amended Scd2ColumnModeSupported names the admitted modes")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "main"
        build_fixture_emit(emit_dir)

        print("1. Type2 dim with derived timestamp / date_parse / value_map:")
        show_derived_export(tmp_path, emit_dir)
        print()

        print("2a. Refusal: derived source is history-tracked:")
        show_derived_source_untracked_refusal(emit_dir)
        print()

        print("2b. Refusal: derived: ordinal on a type2 table:")
        show_ordinal_mode_refusal(emit_dir)
        print()

    print(
        "SUCCESS: scd: type2 dims compile derived timestamp/date_parse/value_map"
        " columns from untracked sources through the records-grain builders,"
        " refuse tracked derived sources, and keep ordinal/elapsed/fk/"
        " correlation refused with the amended message"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
