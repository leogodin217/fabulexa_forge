"""The mode-neutral `base_relations` compile indirection (name-shadowing wrap).

`shadow_base_relations` is the one realization every mode's pure compile
surface (`dimensional.engine.build_query_specs`,
`source.engine.build_source_query_specs`) applies when its caller supplies a
non-None `base_relations` mapping — see the design doc (§ The compile
indirection) for the binding rules this wrap exists to satisfy. Relocated
here, mode-neutral, the `query_spec.py` precedent: a second mode composes the
identical wrap with no cross-mode import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["apply_base_relations", "shadow_base_relations"]


def shadow_base_relations(sql: str, base_relations: "Mapping[str, str]") -> str:
    """Wrap a compiled query so mapped base-table names resolve to replacements.

    Emits ``WITH "<name>" AS (<replacing SELECT>), ... SELECT * FROM
    (<sql>)`` — a wrap, not a textual prefix, because sql may already open
    with its own WITH. Binding rules are contract (design doc § The compile
    indirection, binding rules): a replacing relation's self-read binds
    physical — DuckDB's binder treats a bare unqualified self-read as a
    circular CTE reference, so the replacing SELECT schema-qualifies its
    self-read (`main.<table>`) to reach the physical table (pinned by
    test; the truncated-tape builders in `derivations/truncated_tape.py`
    do this for every base table they replace); its cross-reads are
    binding-insensitive by construction (the builders inline truncation
    predicates); the compiled query's unqualified quoted reads shadow
    totally.

    Args:
        sql: The compiled query (a complete SELECT, possibly opening with WITH).
        base_relations: Physical base-table name -> replacing relation SELECT.
            Must be non-empty; the None case never reaches this function.

    Returns:
        The wrapped SELECT.
    """
    ctes = ", ".join(
        f'"{name}" AS ({replacement})' for name, replacement in base_relations.items()
    )
    return f"WITH {ctes} SELECT * FROM (\n{sql}\n)"


def apply_base_relations(sql: str, base_relations: "Mapping[str, str] | None") -> str:
    """Apply the base_relations compile indirection when given, unchanged otherwise.

    The shared None-handling every pure compile surface threads: with
    `base_relations=None`, `sql` returns unwrapped — byte-identical to the
    pre-parameter surface. Otherwise wraps via `shadow_base_relations`.

    Args:
        sql: One compiled query (a complete SELECT).
        base_relations: Physical base-table name -> replacing relation SELECT,
            or None for no indirection.

    Returns:
        `sql` unchanged when `base_relations` is None; the shadow-wrapped
        query otherwise.
    """
    if base_relations is None:
        return sql
    return shadow_base_relations(sql, base_relations)
