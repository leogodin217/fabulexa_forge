"""Mode-neutral row-selection spine: the fan-out-free, horizon-free
owner/record parent lookup source's row selection composes, promoted here (the
`exporters/election.py` precedent) so streaming resolves its `where` record
sets and membership owner scoping through the same device rather than growing
a sibling (design doc § The selection-spine device). Moved verbatim from
`exporters/source/renders.py` and `exporters/source/plan.py`, with renames:
`SourceWhereEntry` -> `WhereEntry`, `_needs_population_filter` ->
`needs_population_filter`, `_where_predicate_elements` ->
`where_predicate_elements`, `_check_where_values_observed` ->
`check_where_values_observed` (its per-notice wording is now the caller's
`message` callable — source and streaming render different nouns over the
same two-case structure).

Layer-direction invariant: imports the reader (`build_records_relation_sql`,
`Sidecar`), `fabulexa_forge._sql` (`_sql_literal`, `render_predicate_condition`),
`fabulexa_forge.exporters.populations` (`Population`), and
`fabulexa_forge.exporters.notices` (`Notice`, `NoticeSink`). Never imports
`exporters.source.*` or `exporters.streaming.*` — both modes import this
module, never each other through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.populations import Population
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import _sql_literal, render_predicate_condition
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.reader.relations import build_records_relation_sql

#: Prefix marking a records-table scalar-property column — the spine's own
#: addressing of the records-column taxonomy, mirrored from
#: `exporters.source.columns._PROP_PREFIX` (not imported: this module is
#: mode-neutral and must not depend on `exporters.source`).
_PROP_PREFIX = "prop__"


@dataclass(frozen=True)
class WhereEntry:
    """One resolved `where` entry: gate-passed and plan-time-typed."""

    key: str
    """The key as written (source-column or bare form)."""
    source_column: str
    """The base-table column identity (`prop__<p>`) on the subject kind's
    records table."""
    sql_type: str
    """The column's sidecar-declared DuckDB type."""
    value: "str | list[str]"
    """The config value, verbatim — what the rendering authority compiles."""
    typed_values: tuple[object, ...]
    """Per-element `cast_predicate_element` results, config element order —
    the disjointness gate's comparison set (doc § Event-source disjointness)."""


def needs_population_filter(
    sidecar: "Sidecar", kind: str, populations: "tuple[Population, ...]"
) -> bool:
    """Whether an addressed population set needs a discriminator filter.

    Shared by a `state` render's own population filter and
    `build_selection_spine_sql`'s owner-narrowing (doc § The parent lookup):
    false for a flat kind (single population, `sub_type=None` — no
    discriminator column exists) or when `populations` addresses the kind's
    full declared domain (the design doc's no-op-filter-not-composed rule).

    Args:
        sidecar: The open emit's sidecar.
        kind: The addressed kind.
        populations: The resolved population set.

    Returns:
        True iff a discriminator IN-predicate must be composed.
    """
    if populations[0].sub_type is None:
        return False
    domain = set(sidecar.subtype_values(kind))
    return {p.sub_type for p in populations} != domain


def build_selection_spine_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    populations: "tuple[Population, ...]",
    where: "tuple[WhereEntry, ...]",
) -> str | None:
    """The per-row selection spine: a `record_id`-producing SELECT over the
    kind's records spine of the records satisfying the population set AND
    the predicate conjunction (each entry via `render_predicate_condition`
    on its `source_column` / `sql_type`), or None when neither restricts
    (`populations` covers the declared domain or the kind is flat, and
    `where` is empty). Fan-out-free (`record_id` is unique on the spine);
    evaluates current spine values (doc § Invariants #1). One seam for both
    directions: records-source narrowing, and the parent lookup when callers
    pass the owner kind of a membership unit.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        kind: The subject kind (the owner kind for a membership caller).
        populations: The unit's addressed populations.
        where: The unit's resolved predicate entries; empty = none.

    Returns:
        The spine SELECT for an `IN`-semi-join, or None when no restriction
        applies.
    """
    needs_filter = needs_population_filter(sidecar, kind, populations)
    if not needs_filter and not where:
        return None

    relation_sql = build_records_relation_sql(sidecar, fork_path, kind, {})
    conditions: list[str] = []
    if needs_filter:
        values = ", ".join(
            _sql_literal(p.sub_type) for p in populations if p.sub_type is not None
        )
        conditions.append(f'"_spine"."{_PROP_PREFIX}{kind}_type" IN ({values})')
    conditions.extend(
        render_predicate_condition(
            entry.source_column, entry.value, entry.sql_type, "_spine"
        )
        for entry in where
    )
    return (
        'SELECT "record_id" FROM ('
        f"{relation_sql}"
        f') AS "_spine" WHERE {" AND ".join(conditions)}'
    )


def where_predicate_elements(value: "str | list[str]") -> list[str]:
    """Normalize a `where` value to its element list, in config order.

    Args:
        value: A scalar (treated as a one-element list) or a list.

    Returns:
        The value's elements, in order.
    """
    return [value] if isinstance(value, str) else list(value)


def check_where_values_observed(
    sidecar: "Sidecar",
    entries: "tuple[WhereEntry, ...]",
    subject_kind: str,
    notice_sink: "NoticeSink",
    message: Callable[[str, str, bool], str],
) -> None:
    """Emit the `discriminator-value-unobserved` notice per out-of-domain
    `where` element (doc § The constant-column gate; dimensional's
    `check_discriminator_value_observed`). A column with no `enum_domains`
    entry is unchecked. Never an error.

    Args:
        sidecar: The open emit's sidecar.
        entries: The unit's resolved `where` entries.
        subject_kind: The `enum_domains` key.
        notice_sink: Receiver for the notices.
        message: Renders one notice's text from `(key, element,
            wholly_unobserved)` — the per-mode wording delta (source's
            unit-shaped wording, streaming's stream-shaped two-case wording).
    """
    kind_domains = sidecar.enum_domains().get(subject_kind, {})
    for entry in entries:
        bare_prop = entry.source_column[len(_PROP_PREFIX) :]
        observed_values = kind_domains.get(bare_prop, ())
        if not observed_values:
            continue

        elements = where_predicate_elements(entry.value)
        unobserved = [e for e in elements if e not in observed_values]
        if not unobserved:
            continue

        wholly_unobserved = len(unobserved) == len(elements)
        for element in unobserved:
            notice_sink(
                Notice(
                    code="discriminator-value-unobserved",
                    message=message(entry.key, element, wholly_unobserved),
                )
            )
