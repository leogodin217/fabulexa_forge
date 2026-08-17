"""Tests for `render_comparison_text` / `render_comparison_json` (Phase 3):
report determinism and shape for both an equal and an unequal result.
"""

from __future__ import annotations

import json

from fabulexa_forge.compare.render import render_comparison_json, render_comparison_text
from fabulexa_forge.compare.report import (
    ComparisonResult,
    RowDiscrepancies,
    SchemaDiscrepancy,
    TableComparison,
)

_EQUAL_RESULT = ComparisonResult(
    equal=True,
    tables=(
        TableComparison(
            table="people",
            schema=(),
            expected_rows=2,
            actual_rows=2,
            rows=RowDiscrepancies(
                columns=("id", "name"),
                missing=(),
                extra=(),
                missing_total=0,
                extra_total=0,
            ),
        ),
    ),
)

_UNEQUAL_RESULT = ComparisonResult(
    equal=False,
    tables=(
        TableComparison(
            table="orders",
            schema=(
                SchemaDiscrepancy("column-extra", "orders", "note", None, "VARCHAR"),
            ),
            expected_rows=3,
            actual_rows=3,
            rows=RowDiscrepancies(
                columns=("id", "total"),
                missing=(("1", "10.0"),),
                extra=(("1", "11.0"),),
                missing_total=1,
                extra_total=1,
            ),
        ),
        TableComparison(
            table="people",
            schema=(),
            expected_rows=2,
            actual_rows=2,
            rows=RowDiscrepancies(
                columns=("id", "name"),
                missing=(),
                extra=(),
                missing_total=0,
                extra_total=0,
            ),
        ),
        TableComparison(
            table="regions",
            schema=(SchemaDiscrepancy("table-missing", "regions", None, None, None),),
            expected_rows=5,
            actual_rows=None,
            rows=None,
        ),
    ),
)


def test_text_render_equal_result_is_verdict_plus_one_line_per_table() -> None:
    """An equal result renders one verdict line and one line per equal table."""
    text = render_comparison_text(_EQUAL_RESULT)
    lines = text.splitlines()
    assert lines[0] == "EQUAL"
    assert len(lines) == 2
    assert lines[1] == "  people: equal (2 rows)"


def test_text_render_unequal_result_shows_blocks_and_totals() -> None:
    """An unequal result renders a verdict line, then a block per table
    carrying discrepancies — schema kinds and row listings visible, and
    totals shown even when listings are truncated."""
    text = render_comparison_text(_UNEQUAL_RESULT)
    assert text.startswith("NOT EQUAL\n")
    assert "  orders: NOT EQUAL" in text
    assert "column-extra column=note actual_type=VARCHAR" in text
    assert "missing_total=1 extra_total=1" in text
    assert "missing: (1, 10.0)" in text
    assert "extra: (1, 11.0)" in text
    assert "  people: equal (2 rows)" in text
    assert "  regions: NOT EQUAL" in text
    assert "table-missing" in text


def test_text_render_is_deterministic() -> None:
    """Same result -> same string, across repeated calls."""
    assert render_comparison_text(_UNEQUAL_RESULT) == render_comparison_text(
        _UNEQUAL_RESULT
    )


def test_json_render_parses_back_and_mirrors_shape() -> None:
    """JSON render parses back and mirrors the ComparisonResult shape: nested
    tables, null for None fields, tuples as arrays."""
    parsed = json.loads(render_comparison_json(_UNEQUAL_RESULT))
    assert parsed["equal"] is False
    assert [t["table"] for t in parsed["tables"]] == ["orders", "people", "regions"]
    regions = parsed["tables"][2]
    assert regions["actual_rows"] is None
    assert regions["rows"] is None
    orders = parsed["tables"][0]
    assert orders["rows"]["missing"] == [["1", "10.0"]]
    assert orders["rows"]["extra"] == [["1", "11.0"]]


def test_json_render_is_byte_stable() -> None:
    """Sorted keys, fixed separators; two renders of the same result match."""
    first = render_comparison_json(_UNEQUAL_RESULT)
    second = render_comparison_json(_UNEQUAL_RESULT)
    assert first == second
    assert ", " not in first
    assert ": " not in first
