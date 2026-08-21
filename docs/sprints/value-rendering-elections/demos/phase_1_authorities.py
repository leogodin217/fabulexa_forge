#!/usr/bin/env python
"""
Demo: The two new rendering authorities (decimal, json_precision), the
registered scalar they compose through, and registration at reader open.
Sprint: value-rendering-elections
Phase: 1

Registers the shared rendering functions on a scratch DuckDB connection
(the same call `open_emit` makes), then:
- renders a DOUBLE column to `DECIMAL(4,3)` text through render_decimal_expr
- rounds one JSON leaf in place, byte-preserving every other byte, through
  render_json_precision_expr / forge_json_precision
- shows the decimal authority's overflow error naming table, column, and
  the offending value
"""

import duckdb

from fabulexa_forge._sql import (
    register_render_functions,
    render_decimal_expr,
    render_json_precision_expr,
)


def main() -> int:
    conn = duckdb.connect()
    register_render_functions(conn)

    # --- decimal election ---------------------------------------------
    decimal_expr = render_decimal_expr(
        source_expr="price",
        precision=4,
        scale=3,
        column_label="price",
        table_label="orders",
    )
    (rendered_decimal,) = conn.execute(
        f"SELECT {decimal_expr} FROM (SELECT 1.23456::DOUBLE AS price)"
    ).fetchone()
    print(f"decimal: 1.23456 -> DECIMAL(4,3) -> {rendered_decimal}")
    assert str(rendered_decimal) == "1.235"

    # --- json_precision election ---------------------------------------
    json_expr = render_json_precision_expr(
        source_expr="payload",
        leaves={"discount_pct": 2},
        column_label="payload",
        table_label="orders",
    )
    payload_in = '{"sku": "A1",   "discount_pct": 0.005, "note": null}'
    (rendered_payload,) = conn.execute(
        f"SELECT {json_expr} FROM (SELECT '{payload_in}' AS payload)"
    ).fetchone()
    print(f"json_precision: {payload_in!r}")
    print(f"             -> {rendered_payload!r}")
    assert rendered_payload == '{"sku": "A1",   "discount_pct": 0.01, "note": null}'

    # --- overflow error naming table/column/value -----------------------
    try:
        conn.execute(
            f"SELECT {decimal_expr} FROM (SELECT 12345.0::DOUBLE AS price)"
        ).fetchone()
    except duckdb.Error as exc:
        print(f"overflow guard fired: {exc}")
        assert "orders" in str(exc)
        assert "price" in str(exc)
        assert "12345" in str(exc)
    else:
        raise AssertionError("expected the decimal authority's overflow guard to fire")

    print("SUCCESS: decimal + json_precision authorities render and guard correctly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
