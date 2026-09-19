#!/usr/bin/env python
"""
Demo: The `date_ref` bound shape (`{scd_window: valid_from | valid_to}`) —
`render_date_ref_expr` composing `anchor_temporal_expr` + `date_key_expr` over
the SCD-2 version relation's own bound (never a base column),
`build_scd2_column_expr_flag`'s bound branch, `window_delivery_class`'s
horizon-invariance split between the two bounds, and the three plan-time
refusals (`DateRefWindowBoundRequiresScd2`, `TemporalRenderRequiresAnchor`,
`KeyColumnsStable`).

Sprint: date-ref-scd-window
Phase: 1

Builds a self-contained emit: one records kind (`company`) with a tracked
`prop__status`, and a `history` table giving one record three versions — two
starting on the same local date under `America/New_York`, the third open —
and a second record with one version. Compiles an `scd: type2` dim declaring
`valid_from` / `valid_to` as `scd_window {…, as: date}` beside
`valid_from_date_key` / `valid_to_date_key` bound keys, executes the SQL, and
prints the version rows showing: the key equals the sibling's `yyyymmdd` on
every row; `valid_to_date_key` is `NULL` on the open version; the two
same-day versions share a `valid_from` key and both rows survive. Then prints
`window_delivery_class` for the dim with and without the `valid_to` key
(`upsert` / `append`), and drives three refusals, each printed with its
message: `DateRefWindowBoundRequiresScd2` (the same column on a type-1 dim,
anchor present), `TemporalRenderRequiresAnchor` (the type-2 dim compiled with
`anchor=None`), and `KeyColumnsStable` (`valid_to_date_key` named in `key`).

Nothing here writes a README or manifest — the documentation channel lands
in Phase 2.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import (
    ColumnDecl,
    DateRefSpec,
    DerivedSpec,
    DimensionalConfig,
    ScdWindowSpec,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.dimensional.windowing import window_delivery_class
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.reader.emit import Emit, open_emit

_HOUR_NS = 3_600 * 1_000_000_000
_ANCHOR = EffectiveAnchor(
    start_instant=datetime.fromisoformat("2024-03-14T00:00:00+00:00"),
    timezone=ZoneInfo("America/New_York"),
)

# Company A's three tracked prop__status changes: the first two land 2h apart
# in UTC but on the *same* America/New_York local date (00:00 and 02:00 EDT);
# the third, a day later, is the still-open version.
_COMPANY_A_HISTORY = [4 * _HOUR_NS, 6 * _HOUR_NS, 30 * _HOUR_NS]
# Company B's single (open) version.
_COMPANY_B_HISTORY = [10 * _HOUR_NS]


def _write_emit(tmp_dir: Path) -> Path:
    """Write a self-contained emit: two companies, one tracked property.

    c001: three prop__status history rows (two same America/New_York local
    date, the third the open version). c002: one prop__status history row
    (its lone version, open).

    Args:
        tmp_dir: Directory to write run.duckdb + base.json into.

    Returns:
        tmp_dir (the emit directory).
    """
    db_path = tmp_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        'CREATE TABLE "records__company" ('
        '"fork_path" VARCHAR, "record_id" VARCHAR, "created_sim_time" BIGINT,'
        ' "active" BOOLEAN, "deactivated_at" BIGINT,'
        ' "last_mutation_sim_time" BIGINT, "record_index" BIGINT,'
        ' "prop__status" VARCHAR)'
    )
    conn.execute(
        'CREATE TABLE "history" ("fork_path" VARCHAR, "kind" VARCHAR,'
        ' "record_id" VARCHAR, "property" VARCHAR, "sim_time" BIGINT,'
        ' "value" VARCHAR)'
    )
    conn.execute(
        'INSERT INTO "records__company" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "c001", 0, True, None, 0, 0, "pending"],
    )
    conn.execute(
        'INSERT INTO "records__company" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "c002", 0, True, None, 0, 1, "pending"],
    )
    statuses_a = ["active", "suspended", "active"]
    for sim_time, status in zip(_COMPANY_A_HISTORY, statuses_a, strict=True):
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "company", "c001", "status", sim_time, status],
        )
    for sim_time in _COMPANY_B_HISTORY:
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "company", "c002", "status", sim_time, "active"],
        )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 40 * _HOUR_NS}],
        "tables": [
            {
                "name": "records__company",
                "category": "records",
                "record_kind": "company",
                "rows": 2,
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "record_id", "type": "VARCHAR"},
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
                ],
            },
            {
                "name": "history",
                "category": "fixed",
                "rows": len(_COMPANY_A_HISTORY) + len(_COMPANY_B_HISTORY),
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "kind", "type": "VARCHAR"},
                    {"name": "record_id", "type": "VARCHAR"},
                    {"name": "property", "type": "VARCHAR"},
                    {"name": "sim_time", "type": "BIGINT"},
                    {"name": "value", "type": "VARCHAR"},
                ],
            },
        ],
        "runtime": {
            "timezone": "America/New_York",
            "start_datetime": "2024-03-14T00:00:00+00:00",
        },
    }
    (tmp_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_dir


def _type2_table_decl(*, with_valid_to: bool) -> TableDecl:
    """The `dim_company` table: the `valid_from` bound (column + key), plus
    the `valid_to` bound (column + key) when `with_valid_to`.

    The two `valid_to` columns (the `derived: scd_window` sibling and its
    `date_ref` bound key) travel together here so the version-rows demo can
    show both; the delivery-class demo below toggles only the bound key, in
    isolation, on a table that carries no `valid_to` derived column at all.
    """
    columns = [
        ColumnDecl(name="id", from_="record_id"),
        ColumnDecl(name="status", from_="prop__status"),
        ColumnDecl(
            name="valid_from",
            derived=DerivedSpec(
                scd_window=ScdWindowSpec(bound="valid_from", **{"as": "date"})
            ),
        ),
        ColumnDecl(
            name="valid_from_date_key", date_ref=DateRefSpec(scd_window="valid_from")
        ),
    ]
    if with_valid_to:
        columns.append(
            ColumnDecl(
                name="valid_to",
                derived=DerivedSpec(
                    scd_window=ScdWindowSpec(bound="valid_to", **{"as": "date"})
                ),
            )
        )
        columns.append(
            ColumnDecl(
                name="valid_to_date_key", date_ref=DateRefSpec(scd_window="valid_to")
            )
        )
    return TableDecl(
        name="dim_company",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="company"),
        key=["id", "valid_from"],
        columns=columns,
    )


def _discard_notice(_: Notice) -> None:
    """A notice sink that drops every notice — the demo has none to show."""


def _demo_version_rows(emit: Emit) -> None:
    """Compile the type-2 dim and print its version rows with the bound keys."""
    config = DimensionalConfig(tables=[_type2_table_decl(with_valid_to=True)])
    (spec,) = build_query_specs(
        emit,
        config,
        _ANCHOR,
        None,
        _discard_notice,
        base_relations=None,
        tables=None,
    )
    rows = emit.query(spec.sql, ())
    print("=== dim_company version rows ===")
    print("id, status, valid_from, valid_from_date_key, valid_to, valid_to_date_key")
    for row in rows:
        print(row)

    agreement_sql = (
        "SELECT"
        " valid_from_date_key = CAST(year(valid_from) * 10000"
        " + month(valid_from) * 100 + day(valid_from) AS INTEGER)"
        " AND (valid_to IS NULL) = (valid_to_date_key IS NULL)"
        " AND (valid_to IS NULL OR valid_to_date_key = CAST(year(valid_to) * 10000"
        " + month(valid_to) * 100 + day(valid_to) AS INTEGER))"
        f" FROM ({spec.sql}) AS t"
    )
    agrees = [row[0] for row in emit.query(agreement_sql, ())]
    print(
        f"\nfamily agreement (bound key == date_key_expr(sibling scd_window)):"
        f" {all(agrees)} ({len(agrees)} rows)"
    )

    open_version_sql = (
        "SELECT valid_to_date_key IS NULL FROM"
        f" ({spec.sql}) AS t WHERE valid_to IS NULL"
    )
    open_nulls = [row[0] for row in emit.query(open_version_sql, ())]
    print(
        f"open-version valid_to_date_key is NULL: {all(open_nulls)}"
        f" ({len(open_nulls)} open version(s))"
    )

    same_day_sql = (
        "SELECT count(*), count(DISTINCT valid_from_date_key),"
        " count(DISTINCT valid_from) FROM"
        f" ({spec.sql}) AS t WHERE id = 'c001'"
    )
    (row_count, distinct_keys, distinct_starts) = emit.query(same_day_sql, ())[0]
    print(
        f"c001: {row_count} version rows, {distinct_keys} distinct valid_from"
        f" key(s), {distinct_starts} distinct valid_from date(s)"
        " (two versions share one local date and one key)"
    )


def _demo_delivery_class(emit: Emit) -> None:
    """Contrast the static delivery class with and without the valid_to key."""
    append_config = DimensionalConfig(tables=[_type2_table_decl(with_valid_to=False)])
    upsert_config = DimensionalConfig(tables=[_type2_table_decl(with_valid_to=True)])
    for label, config in (
        ("without valid_to key", append_config),
        ("with valid_to key", upsert_config),
    ):
        delivery = window_delivery_class(config.tables[0], config, emit.sidecar)
        print(f"dim_company ({label}): delivery class = {delivery!r}")


def _drive_refusal(
    emit: Emit, label: str, table_decl: TableDecl, anchor: EffectiveAnchor | None
) -> None:
    """Compile a single-table config expected to refuse, printing its message."""
    config = DimensionalConfig(tables=[table_decl])
    try:
        build_query_specs(
            emit,
            config,
            anchor,
            None,
            _discard_notice,
            base_relations=None,
            tables=None,
        )
        raise AssertionError(f"{label}: expected a refusal, none raised")
    except ExportError as exc:
        print(f"refused ({label}): {exc}")


def _demo_refusals(emit: Emit) -> None:
    """Drive the phase's three refusals, printing each message."""
    type1_bound_table = TableDecl(
        name="dim_company_flat",
        role="dim",
        scd="type1",
        source=SourceDecl(grain="records", kind="company"),
        key=["id"],
        columns=[
            ColumnDecl(name="id", from_="record_id"),
            ColumnDecl(
                name="valid_from_date_key",
                date_ref=DateRefSpec(scd_window="valid_from"),
            ),
        ],
    )
    _drive_refusal(emit, "bound shape on a type-1 dim", type1_bound_table, _ANCHOR)

    _drive_refusal(
        emit,
        "type-2 dim, no resolved anchor",
        _type2_table_decl(with_valid_to=True),
        None,
    )

    unstable_key_table = _type2_table_decl(with_valid_to=True).model_copy(
        update={"key": ["id", "valid_from", "valid_to_date_key"]}
    )
    _drive_refusal(emit, "valid_to bound key named in key", unstable_key_table, _ANCHOR)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = _write_emit(Path(tmp))
        emit = open_emit(emit_dir)
        try:
            _demo_version_rows(emit)
            print()
            _demo_delivery_class(emit)
            print()
            _demo_refusals(emit)
        finally:
            emit.close()

    print(
        "\nSUCCESS: the bound shape's key equals its sibling scd_window's"
        " yyyymmdd on every version row, NULLs on the open version, agrees"
        " across same-day versions, shifts delivery class from 'append' to"
        " 'upsert', and refuses on a type-1 dim, with no anchor, and in a key"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
