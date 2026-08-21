#!/usr/bin/env python
"""
Demo: The dimensional exporter's two new derived kinds — `derived: {decimal:
...}` and `derived: {json_precision: ...}` — plan-time gated and rendered
through the same shared authorities every mode composes (`render_decimal_expr`
/ `render_json_precision_expr`), plus the `DecimalSourceIsDouble` refusal for
a non-DOUBLE `from`.
Sprint: value-rendering-elections
Phase: 5

Builds a scratch emit (one `widget` records kind carrying a DOUBLE amount
property, a VARCHAR JSON payload property, and a VARCHAR label property),
declares a fact table deriving a `DECIMAL(6,3)` column and a leaf-rounded
JSON column, runs `validate_table` (the plan-time gates), renders both
columns' SQL via `build_column_expr`, and prints the rendered row. Then shows
a `decimal` derived column sourced from the VARCHAR label column refused at
plan time.
"""

import sys
import tempfile
from pathlib import Path

# The vendored fixture-sidecar authority lives under tests/_support — reused
# here (as pytest itself does) rather than hand-rolling a base.json, so the
# demo's scratch emit is built through the one sidecar-conformant authority
# every fixture in this repo goes through.
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import duckdb  # noqa: E402
from _support.notices import discard_notice_sink  # noqa: E402
from _support.sidecar_builder import (  # noqa: E402
    identity_column,
    prop_column,
    write_emit,
)

from fabulexa_forge._sql import register_render_functions  # noqa: E402
from fabulexa_forge.config.models import (  # noqa: E402
    ColumnDecl,
    DecimalSpec,
    DerivedSpec,
    DimensionalConfig,
    JsonPrecisionSpec,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.errors import DecimalSourceIsDouble  # noqa: E402
from fabulexa_forge.exporters.dimensional.columns import build_column_expr  # noqa: E402
from fabulexa_forge.exporters.dimensional.validation import validate_table  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402

_MS = 1_000_000  # one sim-time "tick", in nanoseconds

_WIDGET_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__amount", "DOUBLE", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__payload", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__label", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]


def _build_demo_emit(tmp_path: Path) -> Path:
    """Write the demo's scratch emit: one `widget` kind carrying a DOUBLE
    amount property, a VARCHAR JSON payload property, and a VARCHAR label
    property (the non-DOUBLE source for the refusal demo), one row.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    columns_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _WIDGET_COLUMNS)
    conn.execute(f'CREATE TABLE "records__widget" ({columns_ddl})')
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "w001",
            0,
            True,
            0,
            0,
            12.3456,
            '{"discount": 1.2345, "sku": "A1"}',
            "premium",
        ],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__widget",
                "category": "records",
                "columns": _WIDGET_COLUMNS,
                "rows": 1,
                "record_kind": "widget",
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100 * _MS}],
    )
    return tmp_path


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = _build_demo_emit(Path(tmp))

        # --- happy path: derived: decimal + derived: json_precision --------
        amount_col = ColumnDecl(
            name="amount",
            derived=DerivedSpec(
                decimal=DecimalSpec(**{"from": "prop__amount", "as": [6, 3]})
            ),
        )
        payload_col = ColumnDecl(
            name="payload",
            derived=DerivedSpec(
                json_precision=JsonPrecisionSpec(
                    **{"from": "prop__payload", "leaves": {"discount": 2}}
                )
            ),
        )
        table_decl = TableDecl(
            name="widgets",
            role="fact",
            source=SourceDecl(grain="records", kind="widget"),
            key=["amount"],
            columns=[amount_col, payload_col],
        )
        config = DimensionalConfig(tables=[table_decl])

        with open_emit(emit_dir) as emit:
            validate_table(table_decl, config, emit.sidecar, None, discard_notice_sink)

        amount_expr, _ = build_column_expr(amount_col, None, table_decl=table_decl)
        payload_expr, _ = build_column_expr(payload_col, None, table_decl=table_decl)

        conn = duckdb.connect()
        register_render_functions(conn)
        conn.execute(
            'CREATE TABLE "_grain" ("prop__amount" DOUBLE, "prop__payload" VARCHAR)'
        )
        conn.execute(
            'INSERT INTO "_grain" VALUES (?, ?)',
            [12.3456, '{"discount": 1.2345, "sku": "A1"}'],
        )
        row = conn.execute(
            f'SELECT {amount_expr}, {payload_expr} FROM "_grain"'
        ).fetchone()
        conn.close()
        assert row is not None
        amount_rendered, payload_rendered = row

        print("rendered dimensional row (derived: decimal + derived: json_precision):")
        print(f"  amount:  {amount_rendered!r}")
        print(f"  payload: {payload_rendered!r}")

        assert str(amount_rendered) == "12.346"
        assert payload_rendered == '{"discount": 1.23, "sku": "A1"}'

        # --- refusal: decimal derived from a non-DOUBLE column -------------
        bad_col = ColumnDecl(
            name="bad_amount",
            derived=DerivedSpec(
                decimal=DecimalSpec(**{"from": "prop__label", "as": [4, 3]})
            ),
        )
        bad_table_decl = TableDecl(
            name="widgets",
            role="fact",
            source=SourceDecl(grain="records", kind="widget"),
            key=["bad_amount"],
            columns=[bad_col],
        )
        bad_config = DimensionalConfig(tables=[bad_table_decl])
        try:
            with open_emit(emit_dir) as emit:
                validate_table(
                    bad_table_decl, bad_config, emit.sidecar, None, discard_notice_sink
                )
        except DecimalSourceIsDouble as exc:
            print(f"DecimalSourceIsDouble fired: {exc}")
        else:
            raise AssertionError("expected DecimalSourceIsDouble to fire")

    print(
        "SUCCESS: derived: decimal and derived: json_precision render through"
        " the shared authorities; a non-DOUBLE decimal source is refused at"
        " plan time"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
