"""Tests for `build_kind_label_expr` (`exporters/source/columns.py`) — the one
labeling authority both the junction render's `member__<f>__kind` column and
the event log's `<f>_kind` entry values render through.
"""

from __future__ import annotations

import duckdb

from fabulexa_forge.exporters.source.columns import build_kind_label_expr


def _eval(expr: str, value: object) -> object:
    """Evaluate a `build_kind_label_expr` expression against one scalar
    value, substituted as `?`, and return the single result cell."""
    sql = expr.replace('"_mem"."member__actor__kind"', "?")
    con = duckdb.connect()
    row = con.execute(f"SELECT {sql}", [value] * sql.count("?")).fetchone()
    assert row is not None
    return row[0]


def test_empty_labels_is_byte_identical_passthrough() -> None:
    """Empty `labels` -> `value_expr` returned unchanged."""
    value_expr = '"_mem"."member__actor__kind"'
    assert build_kind_label_expr(value_expr, ()) == value_expr


def test_one_pair_recodes_that_value_identity_fallthrough_otherwise() -> None:
    """One (kind, label) pair recodes a matching value; any other value
    renders verbatim."""
    expr = build_kind_label_expr(
        '"_mem"."member__actor__kind"', (("actor", "clinician"),)
    )
    assert _eval(expr, "actor") == "clinician"
    assert _eval(expr, "location") == "location"


def test_null_stays_null() -> None:
    """A NULL value falls through the CASE and stays NULL."""
    expr = build_kind_label_expr(
        '"_mem"."member__actor__kind"', (("actor", "clinician"),)
    )
    assert _eval(expr, None) is None


def test_label_value_containing_a_quote_is_sql_escaped() -> None:
    """A label carrying a single quote round-trips through the CASE
    unbroken (SQL-escaped, not spliced raw)."""
    expr = build_kind_label_expr(
        '"_mem"."member__actor__kind"', (("actor", "O'Brien's kind"),)
    )
    assert _eval(expr, "actor") == "O'Brien's kind"
