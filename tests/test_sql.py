"""Tests for `fabulexa_forge._sql`: the one predicate-rendering authority.

Covers `render_predicate_condition` — the sole place that renders `=` or `IN`
over a config predicate value — plus its composition of `render_typed_literal`
and `quote_identifier`. `render_typed_literal` / `is_recognized_sql_type` /
`quote_identifier` themselves are exercised indirectly through the authority;
this module owns their contract as exposed through it.
"""

from __future__ import annotations

import pytest

from fabulexa_forge._sql import quote_identifier, render_predicate_condition
from fabulexa_forge.errors import ExportError

# ---------------------------------------------------------------------------
# Scalar rendering — byte-identical to a manual render_typed_literal composition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column,value,sql_type,expected",
    [
        ("prop__status", "active", "VARCHAR", "\"prop__status\" = 'active'"),
        ("prop__count", "7", "BIGINT", "\"prop__count\" = CAST('7' AS BIGINT)"),
        (
            "prop__ready",
            "true",
            "BOOLEAN",
            "\"prop__ready\" = CAST('true' AS BOOLEAN)",
        ),
        (
            "prop__amount",
            "9.99",
            "DECIMAL(10,2)",
            "\"prop__amount\" = CAST('9.99' AS DECIMAL(10,2))",
        ),
    ],
)
def test_scalar_renders_equals_typed_literal(
    column: str, value: str, sql_type: str, expected: str
) -> None:
    """A scalar value renders `"col" = <typed literal>` for each recommended
    column type, byte-identical to a manual `render_typed_literal` composition."""
    assert render_predicate_condition(column, value, sql_type, None) == expected


def test_scalar_varchar_renders_raw_single_quoted_literal() -> None:
    """VARCHAR renders the raw single-quoted literal, no CAST — the history
    `value` surface's untyped comparison."""
    rendered = render_predicate_condition("value", "completed", "VARCHAR", None)
    assert rendered == "\"value\" = 'completed'"


# ---------------------------------------------------------------------------
# List rendering — IN, preserving element order
# ---------------------------------------------------------------------------


def test_one_element_list_renders_in_not_equals() -> None:
    """A one-element list still renders `IN`, never collapses to `=`."""
    rendered = render_predicate_condition(
        "prop__decision_type", ["referred"], "VARCHAR", None
    )
    assert rendered == "\"prop__decision_type\" IN ('referred')"


def test_multi_element_list_preserves_config_order() -> None:
    """A multi-element list renders `IN` with elements in config order, not
    sorted or deduplicated by the renderer."""
    rendered = render_predicate_condition(
        "prop__decision_type",
        ["referred", "admitted", "discharged"],
        "VARCHAR",
        None,
    )
    assert rendered == (
        "\"prop__decision_type\" IN ('referred', 'admitted', 'discharged')"
    )


def test_list_elements_typed_individually() -> None:
    """Each list element is typed by `render_typed_literal` against sql_type —
    a list is exactly the scalar rule applied element-wise."""
    rendered = render_predicate_condition(
        "prop__priority", ["1", "2", "3"], "BIGINT", None
    )
    assert rendered == (
        "\"prop__priority\" IN (CAST('1' AS BIGINT),"
        " CAST('2' AS BIGINT), CAST('3' AS BIGINT))"
    )


# ---------------------------------------------------------------------------
# Alias qualification
# ---------------------------------------------------------------------------


def test_alias_none_leaves_column_unqualified() -> None:
    """alias=None produces an unqualified condition."""
    rendered = render_predicate_condition("elem__role", "lead", "VARCHAR", None)
    assert rendered == "\"elem__role\" = 'lead'"


def test_alias_qualifies_scalar_condition() -> None:
    """A non-None alias qualifies the column as `<alias>."<column>"`, spliced
    verbatim — the point-in-time membership FK's correlated-subquery form."""
    rendered = render_predicate_condition("elem__role", "lead", "VARCHAR", "h")
    assert rendered == "h.\"elem__role\" = 'lead'"


def test_alias_qualifies_list_condition() -> None:
    """An alias qualifies the column in the IN form too."""
    rendered = render_predicate_condition(
        "elem__role", ["lead", "backup"], "VARCHAR", "h"
    )
    assert rendered == "h.\"elem__role\" IN ('lead', 'backup')"


# ---------------------------------------------------------------------------
# str discrimination — never treated as a Sequence of characters
# ---------------------------------------------------------------------------


def test_single_char_str_value_takes_scalar_branch() -> None:
    """Discrimination is on `isinstance(value, str)`, never `Sequence` — a
    single-character string renders `=`, not `IN` over its (one) character."""
    rendered = render_predicate_condition("prop__code", "a", "VARCHAR", None)
    assert rendered == "\"prop__code\" = 'a'"


def test_multi_char_str_value_never_iterated() -> None:
    """A multi-character string is one scalar literal, never split into an
    IN list of its characters."""
    rendered = render_predicate_condition("prop__code", "abc", "VARCHAR", None)
    assert rendered == "\"prop__code\" = 'abc'"
    assert "IN" not in rendered


# ---------------------------------------------------------------------------
# Refusal — unrecognized SQL type
# ---------------------------------------------------------------------------


def test_unrecognized_type_raises_export_error() -> None:
    """An unrecognized SQL type raises ExportError — no silent VARCHAR fallback."""
    with pytest.raises(ExportError, match="unrecognized SQL type"):
        render_predicate_condition("prop__blob_col", "x", "BLOB", None)


def test_unrecognized_type_raises_for_list_value_too() -> None:
    """The list branch raises the same ExportError for an unrecognized type,
    on the first offending element."""
    with pytest.raises(ExportError, match="unrecognized SQL type"):
        render_predicate_condition("prop__blob_col", ["x", "y"], "BLOB", None)


# ---------------------------------------------------------------------------
# Column quoting
# ---------------------------------------------------------------------------


def test_column_quoted_via_quote_identifier() -> None:
    """The column name is quoted exactly as `quote_identifier` would render it,
    including doubling an embedded quote."""
    rendered = render_predicate_condition('weird"col', "v", "VARCHAR", None)
    assert rendered.startswith(quote_identifier('weird"col'))
