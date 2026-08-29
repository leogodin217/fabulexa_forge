"""Shared `init`-proposal documentation-annotation primitives.

The three proposal engines (dimensional, source, streaming) each annotate
their emitted YAML with comments drawn from the emit's documentation view
(design doc § `init` annotations): a scenario block at the top, a
source-table description on table/dim/fact/stream stubs, discriminator
glosses on sub-type values, and property `description` (unit appended) on
proposed property/column entries. Comments never alter grammar — every
function here returns comment *text*, never a `#`-prefixed or indented line;
callers place and prefix it to match their own site's YAML shape (a
standalone line, a trailing suffix, or nested inside an already fully
commented alternative block). Absence is silence throughout: an undocumented
item yields `None` (or `[]` for the scenario block), never a placeholder.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.reader.documentation import ColumnDoc
    from fabulexa_forge.reader.sidecar import Sidecar


def scenario_comment_lines(sidecar: "Sidecar") -> list[str]:
    """The scenario comment block for the top of a generated config.

    Args:
        sidecar: The open emit's sidecar.

    Returns:
        `#`-prefixed comment lines carrying `scenario_description`, or `[]`
        when the emit declares none.
    """
    description = sidecar.documentation().scenario_description()
    if description is None:
        return []
    lines = ["# Scenario:"]
    lines.extend(f"#   {line}" for line in description.splitlines())
    return lines


def table_description(sidecar: "Sidecar", table_name: str) -> str | None:
    """One table's source-table description text, or None when undeclared.

    Args:
        sidecar: The open emit's sidecar.
        table_name: A table the sidecar declares.

    Returns:
        The description text (no `#` prefix), or None.

    Raises:
        TableNotFoundError: table_name is not declared by the sidecar.
    """
    return sidecar.documentation().table_description(table_name)


def sub_type_gloss(sidecar: "Sidecar", kind: str, sub_type: str) -> str | None:
    """One sub-type value's discriminator gloss text, or None when absent.

    Args:
        sidecar: The open emit's sidecar.
        kind: The sub-typed record kind.
        sub_type: A value of `kind`'s declared `<kind>_type` domain.

    Returns:
        The gloss text (no `#` prefix), or None when the domain carries no
        `enum_domains` entry for `(kind, f"{kind}_type")`, or no gloss for
        this particular value.
    """
    try:
        options = sidecar.documentation().enum_options(kind, f"{kind}_type")
    except KeyError:
        return None
    for option in options:
        if option.value == sub_type:
            return option.description
    return None


def sub_type_line_suffix(sidecar: "Sidecar", kind: str, sub_type: str) -> str:
    """The trailing gloss comment for a `sub_types: [<sub_type>]` line.

    Args:
        sidecar: The open emit's sidecar.
        kind: The sub-typed record kind.
        sub_type: The declared sub-type value on this line.

    Returns:
        `"  # {gloss}"`, or `""` when the value carries no discriminator gloss.
    """
    gloss = sub_type_gloss(sidecar, kind, sub_type)
    return f"  # {gloss}" if gloss else ""


def _render_column_doc(doc: "ColumnDoc") -> str:
    """Render a resolved ColumnDoc as comment text: description, unit appended.

    Args:
        doc: The resolved documentation (at least one of description/unit set).

    Returns:
        `"description (unit)"`, `"description"`, or `"(unit)"` — whichever
        of description/unit are present.
    """
    if doc.description is None:
        return f"({doc.unit})"
    if doc.unit is None:
        return doc.description
    return f"{doc.description} ({doc.unit})"


def column_doc_text(
    sidecar: "Sidecar", table_name: str, column_name: str
) -> str | None:
    """One declared column's documentation, rendered as comment text.

    Args:
        sidecar: The open emit's sidecar.
        table_name: The column's declared table.
        column_name: The declared column name.

    Returns:
        The rendered text (no `#` prefix), or None when the column carries
        no documentation.

    Raises:
        TableNotFoundError: table_name is not declared by the sidecar.
        ColumnNotFoundError: column_name is not declared by that table.
    """
    doc = sidecar.documentation().column_doc(table_name, column_name)
    if doc is None:
        return None
    return _render_column_doc(doc)


def membership_field_doc_text(
    sidecar: "Sidecar", table_name: str, field: str
) -> str | None:
    """One membership element-schema field's documentation, as comment text.

    A reference field's schema carries `member__<f>__kind` /
    `member__<f>__id`; the contract forwards the field declaration's
    attributes onto both, so either answers identically — this reads the
    `__kind` half by convention. A non-reference field reads its own
    `elem__<f>` column.

    Args:
        sidecar: The open emit's sidecar.
        table_name: The membership table.
        field: The bare element-schema field name.

    Returns:
        The rendered text (no `#` prefix), or None when undocumented.

    Raises:
        TableNotFoundError: table_name is not declared by the sidecar.
        ColumnNotFoundError: neither candidate column is declared by that table.
    """
    column_names = {col.name for col in sidecar.table(table_name).columns}
    kind_column = f"member__{field}__kind"
    column_name = kind_column if kind_column in column_names else f"elem__{field}"
    return column_doc_text(sidecar, table_name, column_name)
