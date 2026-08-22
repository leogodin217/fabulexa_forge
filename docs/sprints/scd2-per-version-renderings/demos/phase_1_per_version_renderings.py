#!/usr/bin/env python
"""
Demo: Per-version value renderings on scd: type2
Sprint: scd2-per-version-renderings
Phase: 1

A pure per-row value rendering (`derived: decimal` / `json_precision` /
`timestamp` / `date_parse` / `value_map`) is now legal on an `scd: type2`
column over a **tracked** source, evaluated per version: the rendering
authority compiles against the versioned reconstruction's cast per-version
value (`CAST("_versions"."prop__<p>" AS <sidecar declared type>)`) instead
of the composed records relation's current-state value — the same builder
every other attach site uses. Version structure is election-invariant: the
rendering never creates, merges, suppresses, or renumbers a version row.
`derived: ordinal` (and `fk` / `correlation` / `derived: elapsed`) stay
refused on type2 — their refusal was never about value purity.

Shows:
  1. A type2 dim with `derived: decimal` over a tracked, float64-noisy
     DOUBLE `engagement_score` and `derived: value_map` over a tracked
     `tier` code — printing each version row's rendered values, including
     two adjacent versions whose rounded decimal collides (4.800000000000001
     and 4.804 both render 4.80).
  2. The same table exported with a plain `from: prop__engagement_score`
     instead of the decimal rendering — version count and `valid_from` /
     `valid_to` identical to (1): the election changes presentation only.
  3. `derived: ordinal` on the same table still refused, with the widened
     `Scd2ColumnModeSupported` message naming `derived: decimal` and
     `derived: json_precision` as now-admitted modes.
"""

from __future__ import annotations

import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    ColumnDecl,
    DecimalSpec,
    DerivedSpec,
    DimensionalConfig,
    ExportConfig,
    OrdinalSpec,
    SourceDecl,
    TableDecl,
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

_ACCOUNT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__engagement_score",
        "type": "DOUBLE",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__tier",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
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

_HOUR_NS = 3_600_000_000_000
_SLICE_AT_NS = 3 * _HOUR_NS

# (sim_time, engagement_score, tier) history — four versions on independent
# change points:
#   [0h, 1h):  4.800000000000001 / bronze
#   [1h, 2h):  4.804              / bronze   (decimal collides with v1: 4.80)
#   [2h, 3h):  4.804              / silver
#   [3h, open): 91.23456          / gold
_ENGAGEMENT_HISTORY = [
    (0, "4.800000000000001"),
    (1 * _HOUR_NS, "4.804"),
    (3 * _HOUR_NS, "91.23456"),
]
_TIER_HISTORY = [
    (0, "bronze"),
    (2 * _HOUR_NS, "silver"),
    (3 * _HOUR_NS, "gold"),
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
    """Write the `account` fixture emit: one record whose tracked
    `engagement_score` (noisy DOUBLE) and `tier` (code) both change over
    time, on independent change points.

    Args:
        emit_dir: Directory to write base.json + run.duckdb into.
    """
    emit_dir.mkdir(parents=True, exist_ok=True)

    tables: list[dict[str, object]] = [
        {
            "name": "records__account",
            "category": "records",
            "record_kind": "account",
            "columns": _ACCOUNT_COLUMNS,
            "rows": 1,
        },
        {
            "name": "history",
            "category": "fixed",
            "columns": _HISTORY_COLUMNS,
            "rows": len(_ENGAGEMENT_HISTORY) + len(_TIER_HISTORY),
        },
    ]
    base_json: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": _SLICE_AT_NS}],
        "tables": tables,
        "record_roles": {"account": "dimension"},
        "runtime": {"timezone": _ANCHOR_ZONE, "start_datetime": _ANCHOR_START_DATETIME},
    }
    (emit_dir / "base.json").write_text(json.dumps(base_json), encoding="utf-8")

    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_create_ddl("records__account", _ACCOUNT_COLUMNS))
    conn.execute(
        'INSERT INTO "records__account" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "acc001", 0, True, 0, _SLICE_AT_NS, 91.23456, "gold"],
    )
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    for sim_time, value in _ENGAGEMENT_HISTORY:
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "account", "acc001", "engagement_score", sim_time, value],
        )
    for sim_time, value in _TIER_HISTORY:
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "account", "acc001", "tier", sim_time, value],
        )
    conn.close()


def _rendered_table_decl() -> TableDecl:
    """The type2 dim: tracked `engagement_score` rendered `DECIMAL(5,2)`,
    tracked `tier` rendered through `value_map`."""
    return TableDecl(
        name="dim_account",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="account"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(
                name="engagement_score",
                derived=DerivedSpec(
                    decimal=DecimalSpec(
                        **{"from": "prop__engagement_score", "as": [5, 2]}
                    )
                ),
            ),
            ColumnDecl(
                name="tier_code",
                derived=DerivedSpec(
                    value_map=ValueMapSpec(
                        **{"from": "prop__tier"},
                        map={"bronze": 1, "silver": 2, "gold": 3},
                    )
                ),
            ),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
            ColumnDecl(name="valid_to", derived=DerivedSpec(scd_window="valid_to")),
        ],
    )


def _unrendered_table_decl() -> TableDecl:
    """Same dim, but `engagement_score` is a plain `from:` — no rendering
    election — to compare version structure against the rendered table."""
    return TableDecl(
        name="dim_account_plain",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="account"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="engagement_score", **{"from": "prop__engagement_score"}),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
            ColumnDecl(name="valid_to", derived=DerivedSpec(scd_window="valid_to")),
        ],
    )


def show_per_version_rendering(tmp_path: Path, emit_dir: Path) -> None:
    """Export both dims; print the rendered version rows; assert version
    count and valid_from/valid_to are identical between them (version
    structure is election-invariant)."""
    config = ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[_rendered_table_decl(), _unrendered_table_decl()]
        ),
    )
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None, "the fixture sidecar carries a runtime anchor"
        export_dimensional(emit, config, out_path, "duckdb", anchor, _discard_notice)

    con = duckdb.connect(str(out_path), read_only=True)
    rendered_rows = con.sql(
        "SELECT engagement_score, tier_code, valid_from, valid_to"
        ' FROM "dim_account" ORDER BY valid_from'
    ).fetchall()
    plain_rows = con.sql(
        "SELECT engagement_score, valid_from, valid_to"
        ' FROM "dim_account_plain" ORDER BY valid_from'
    ).fetchall()
    con.close()

    print("  rendered (dim_account): engagement_score, tier_code, valid_from, valid_to")
    for row in rendered_rows:
        print(f"    {row}")

    if len(rendered_rows) != 4:
        raise _fail(f"expected 4 versions, got {len(rendered_rows)}")

    scores = [row[0] for row in rendered_rows]
    expected_scores = [
        Decimal("4.80"),
        Decimal("4.80"),
        Decimal("4.80"),
        Decimal("91.23"),
    ]
    if scores != expected_scores:
        raise _fail(f"expected rounded decimal series, got {scores}")
    if scores[0] != scores[1]:
        raise _fail("expected the two noisy raw values to collide at DECIMAL(5,2)")
    print("  OK: adjacent versions with colliding rounded values both emitted (4.80)")

    tiers = [row[1] for row in rendered_rows]
    if tiers != [1, 1, 2, 3]:
        raise _fail(f"expected tier_code to change per version, got {tiers}")
    print("  OK: value_map renders per version over the tracked tier")

    rendered_bounds = [(row[2], row[3]) for row in rendered_rows]
    plain_bounds = [(row[1], row[2]) for row in plain_rows]
    if len(plain_rows) != len(rendered_rows) or rendered_bounds != plain_bounds:
        raise _fail(
            "expected identical version count and valid_from/valid_to across"
            f" elections; rendered={rendered_bounds}, plain={plain_bounds}"
        )
    print("  OK: version count and valid_from/valid_to unchanged by the election")


def show_ordinal_mode_refusal(emit_dir: Path) -> None:
    """`derived: ordinal` on a type2 table is still refused, with the
    widened Scd2ColumnModeSupported message naming decimal/json_precision
    as now-admitted modes."""
    table_decl = TableDecl(
        name="dim_account_ordinal",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="account"),
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
                "derived: decimal",
                "derived: json_precision",
            ):
                if needle not in message:
                    raise _fail(f"refusal message missing {needle!r}: {message}")
            print("  OK: ordinal refused; message names decimal/json_precision")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "main"
        build_fixture_emit(emit_dir)

        print("1-2. Per-version decimal/value_map rendering, structure invariant:")
        show_per_version_rendering(tmp_path, emit_dir)
        print()

        print("3. Refusal: derived: ordinal on a type2 table:")
        show_ordinal_mode_refusal(emit_dir)
        print()

    print(
        "SUCCESS: scd: type2 dims compile derived decimal/value_map columns"
        " per version over tracked sources, with version count and"
        " valid_from/valid_to unchanged by the election, while derived:"
        " ordinal stays refused under the widened Scd2ColumnModeSupported"
        " message"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
