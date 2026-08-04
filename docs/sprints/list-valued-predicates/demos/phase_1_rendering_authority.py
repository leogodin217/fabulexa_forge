#!/usr/bin/env python
"""
Demo: The one predicate-rendering authority (`render_predicate_condition`)
Sprint: list-valued-predicates
Phase: 1

Pure-function demo, no emit: `render_predicate_condition` is the sole place
that renders `=` or `IN` over a config predicate value (both former private
`_render_typed_literal` forks are deleted this phase). Shows the rendering
matrix (scalar `=`, one-element `IN`, and multi-element `IN`, preserving
element order) across VARCHAR / BIGINT / BOOLEAN / DECIMAL(p,s); the
alias-qualified form the point-in-time membership FK uses; and the two
refusals the renderer consolidation introduces: an unrecognized SQL type,
and a parameterized type string that passes a naive prefix test but fails
the shared anchored grammar.
"""

from __future__ import annotations

from fabulexa_forge._sql import render_predicate_condition
from fabulexa_forge.errors import ExportError


def demo_scalar_matches_typed_literal_composition() -> None:
    """Scalar renders `"col" = <lit>`, byte-identical to a manual composition,
    across every column type the contract's recommended mapping produces."""
    cases = [
        ("prop__status", "active", "VARCHAR", "\"prop__status\" = 'active'"),
        ("prop__count", "7", "BIGINT", "\"prop__count\" = CAST('7' AS BIGINT)"),
        ("prop__ready", "true", "BOOLEAN", "\"prop__ready\" = CAST('true' AS BOOLEAN)"),
        (
            "prop__amount",
            "9.99",
            "DECIMAL(10,2)",
            "\"prop__amount\" = CAST('9.99' AS DECIMAL(10,2))",
        ),
    ]
    for column, value, sql_type, expected in cases:
        rendered = render_predicate_condition(column, value, sql_type, None)
        assert rendered == expected
        print(f"SCALAR {sql_type}: {rendered}")


def demo_list_renders_in_preserving_order() -> None:
    """A one-element list still renders `IN`, not `=`; a multi-element list
    renders `IN` with elements in config order."""
    one_element = render_predicate_condition(
        "prop__decision_type", ["referred"], "VARCHAR", None
    )
    assert one_element == "\"prop__decision_type\" IN ('referred')"
    print(f"ONE-ELEMENT LIST: {one_element}")

    multi_element = render_predicate_condition(
        "prop__decision_type",
        ["referred", "admitted", "discharged"],
        "VARCHAR",
        None,
    )
    assert multi_element == (
        "\"prop__decision_type\" IN ('referred', 'admitted', 'discharged')"
    )
    print(f"MULTI-ELEMENT LIST: {multi_element}")

    multi_bigint = render_predicate_condition(
        "prop__priority", ["1", "2", "3"], "BIGINT", None
    )
    assert multi_bigint == (
        "\"prop__priority\" IN (CAST('1' AS BIGINT),"
        " CAST('2' AS BIGINT), CAST('3' AS BIGINT))"
    )
    print(f"MULTI-ELEMENT LIST (typed): {multi_bigint}")


def demo_alias_qualified_form() -> None:
    """`alias` qualifies the column as `<alias>."<column>"` — the form the
    point-in-time membership FK's correlated subquery needs."""
    unqualified = render_predicate_condition("elem__role", "lead", "VARCHAR", None)
    assert unqualified == "\"elem__role\" = 'lead'"
    print(f"UNQUALIFIED: {unqualified}")

    qualified = render_predicate_condition("elem__role", "lead", "VARCHAR", "h")
    assert qualified == "h.\"elem__role\" = 'lead'"
    print(f"ALIAS-QUALIFIED: {qualified}")

    qualified_list = render_predicate_condition(
        "elem__role", ["lead", "backup"], "VARCHAR", "h"
    )
    assert qualified_list == "h.\"elem__role\" IN ('lead', 'backup')"
    print(f"ALIAS-QUALIFIED LIST: {qualified_list}")


def demo_str_never_takes_list_branch() -> None:
    """Discrimination is on `isinstance(value, str)`, never `Sequence` — a
    single-character string still renders `=`, not `IN` over its characters."""
    rendered = render_predicate_condition("prop__code", "a", "VARCHAR", None)
    assert rendered == "\"prop__code\" = 'a'"
    print(f"str VALUE (never a Sequence of characters): {rendered}")


def demo_unrecognized_type_refused() -> None:
    """An unrecognized SQL type is refused — no silent VARCHAR fallback."""
    try:
        render_predicate_condition("prop__blob_col", "x", "BLOB", None)
    except ExportError as exc:
        print(f"REFUSED (unrecognized type BLOB): {type(exc).__name__}: {exc}")
    else:
        raise AssertionError("expected ExportError for an unrecognized SQL type")


def demo_unanchored_parameterized_type_refused() -> None:
    """A parameterized type string that passes a naive prefix test
    (`.startswith('VARCHAR(')`) but fails the shared anchored grammar is
    refused — the anchored grammar exists precisely to stop a sidecar-
    supplied type string from closing the CAST and appending SQL."""
    malicious_type = "VARCHAR(10)) FROM read_csv('/etc/passwd') --"
    try:
        render_predicate_condition("prop__x", "v", malicious_type, None)
    except ExportError as exc:
        print(
            "REFUSED (prefix-passing, unanchored parameterized type):"
            f" {type(exc).__name__}: {exc}"
        )
    else:
        raise AssertionError(
            "expected ExportError for an unanchored parameterized type string"
        )


def main() -> int:
    print("=== scalar renders, byte-identical across recommended-mapping types ===")
    demo_scalar_matches_typed_literal_composition()
    print()
    print("=== list renders IN, preserving element order ===")
    demo_list_renders_in_preserving_order()
    print()
    print("=== alias-qualified form ===")
    demo_alias_qualified_form()
    print()
    print("=== str is a scalar, never a Sequence of characters ===")
    demo_str_never_takes_list_branch()
    print()
    print("=== refusal: unrecognized SQL type ===")
    demo_unrecognized_type_refused()
    print()
    print("=== refusal: prefix-passing but unanchored parameterized type ===")
    demo_unanchored_parameterized_type_refused()
    print()
    print("SUCCESS: render_predicate_condition is the one rendering authority")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
