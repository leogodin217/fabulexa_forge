"""Sidecar-driven candidate config generator for `fabulexa-forge init`.

Reads the sidecar (and DISTINCT discriminators where needed) and emits a
commented candidate config the author edits. Honest about being a starting
point (~70-80%); classification stays author-authoritative.

Role is read exclusively from `record_roles` in the sidecar; reference
topology is not consulted. Splitting into per-sub-type stubs is a separate
question, answered by `Sidecar.subtype_values` (the declared `<kind>_type`
domain from `enum_domains`) — independent of whether `record_roles[kind]` is
object-valued (role varies per sub-type, today only `actor`) or a bare string
(role uniform across sub-types, e.g. `entity`, `resource`). Either way, a
sub-typed kind gets one stub per declared sub-type.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Callable

from fabulexa_forge.errors import InitRequiresRecordRoles
from fabulexa_forge.exporters.keys_init import propose_key_election, render_keys_block
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.slice_only import is_non_exempt_slice_only
from fabulexa_forge.reader.records_columns import (
    REF_INDEX_PREFIX,
    records_column_role,
)
from fabulexa_forge.reader.relations import distinct_prop_values
from fabulexa_forge.reader.sidecar import TableSpec

if TYPE_CHECKING:
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import PresentationKeys, Sidecar


# Registry role token -> config role token
_ROLE_MAP: dict[str, str] = {
    "dimension": "dim",
    "fact": "fact",
}

#: The proposed dim key's `from:` source — the uniform active election
#: (docs/architecture/key-election.md § `init` proposals): every proposed dim
#: key column aligns with the population's active `record_index` election.
_DIM_ID_SURFACE = "record_index"


def _kind_has_discriminator(kind: str, all_tables: tuple[TableSpec, ...]) -> bool:
    """Return True when the kind's records table has a discriminator-like column.

    A discriminator is a `prop__<kind>_type` column on the records table.
    This is used ONLY for the modelling-discriminator (fact) path where the
    <kind>_type is NOT a sub-type signal; it does NOT use SELECT DISTINCT.

    Args:
        kind: The record kind name.
        all_tables: All sidecar TableSpec objects.

    Returns:
        True when the kind has a <kind>_type column, False otherwise.
    """
    records_table_name = f"records__{kind}"
    discriminator_col = f"prop__{kind}_type"
    for table in all_tables:
        if not isinstance(table, TableSpec):
            continue
        if table.name == records_table_name:
            for col in table.columns:
                if col.name == discriminator_col:
                    return True
    return False


def _versions_per_record(sidecar: Sidecar, kind: str, prop: str) -> str:
    """Render the versions-per-record evidence for one tracked property.

    A tracked column whose series carries many versions per record is a timeline,
    not a slowly-changing attribute: proposing it as an SCD-2 column materializes
    a dimension that many times its entity count. The ratio is the author's cue to
    move it to its own fact grain, so the candidate states it rather than leaving
    the author to measure it after exporting.

    Reads the sidecar's advisory `row_census`; when the emit carries none, says so
    rather than staying silent, so an unmeasured proposal is never mistaken for a
    measured-and-fine one.

    Args:
        sidecar: The open emit's sidecar.
        kind: The record kind name.
        prop: The bare property name (no `prop__` prefix), as `history_series` keys it.

    Returns:
        A comment fragment — the ratio, the absent-series case, or the no-census case.
    """
    census = sidecar.row_census
    if census is None:
        return "versions/record unknown (no row_census in this emit)"
    series = census.history_series.get(kind, {}).get(prop)
    if series is None or series.records == 0:
        return "versions/record unknown (no rows in this series)"
    return f"{series.rows / series.records:.1f} versions/record"


def _columns_have_history_tracked(kind: str, all_tables: tuple[TableSpec, ...]) -> bool:
    """Return True when any column on the kind's records table is history_tracked.

    Args:
        kind: The record kind name.
        all_tables: All sidecar TableSpec objects.

    Returns:
        True when history_tracked=True on any column.
    """
    records_table_name = f"records__{kind}"
    for table in all_tables:
        if not isinstance(table, TableSpec):
            continue
        if table.name == records_table_name:
            for col in table.columns:
                if col.history_tracked is True:
                    return True
    return False


def _get_tracked_columns(kind: str, all_tables: tuple[TableSpec, ...]) -> list[str]:
    """Return column names flagged history_tracked=True for a kind's records table.

    Args:
        kind: The record kind name.
        all_tables: All sidecar TableSpec objects.

    Returns:
        List of column names with history_tracked=True.
    """
    records_table_name = f"records__{kind}"
    tracked = []
    for table in all_tables:
        if not isinstance(table, TableSpec):
            continue
        if table.name == records_table_name:
            for col in table.columns:
                if col.history_tracked is True:
                    tracked.append(col.name)
    return tracked


def _get_records_columns(kind: str, all_tables: tuple[TableSpec, ...]) -> list[str]:
    """Return all column names from the kind's records table.

    Args:
        kind: The record kind name.
        all_tables: All sidecar TableSpec objects.

    Returns:
        List of column names.
    """
    records_table_name = f"records__{kind}"
    for table in all_tables:
        if not isinstance(table, TableSpec):
            continue
        if table.name == records_table_name:
            return [col.name for col in table.columns]
    return []


def _owned_columns(
    sidecar: "Sidecar", kind: str, sub_type: str
) -> frozenset[str] | None:
    """Value columns sub-type `sub_type` of `kind` declares, per the sidecar.

    The set init prunes each per-sub-type stub to, read from the sidecar's
    `sub_type_columns` partition. None signals 'do not prune' (union-schema
    fallback): the partition is absent (an older emit) or does not cover this
    kind/sub-type. A present-but-empty partition entry returns an empty
    frozenset — the sub-type owns no value column, so every partitionable
    column is pruned.

    Args:
        sidecar: The open emit's sidecar.
        kind: The sub-typed record kind.
        sub_type: The declared sub-type.

    Returns:
        The owned column-name set, or None when the partition does not cover it.
    """
    partition = sidecar.sub_type_columns()
    if partition is None:
        return None
    try:
        return frozenset(partition.columns_for(kind, sub_type))
    except KeyError:
        return None


def _is_pruned(col: str, owned: frozenset[str] | None, discriminator: str) -> bool:
    """Whether `col` is omitted from a per-sub-type stub as structurally inapplicable.

    True only for a sub-type-partitionable value column (`prop__` / `ref_index__`,
    never the kind-wide `discriminator`) the sub-type does not declare. Never
    prunes when `owned` is None, so the union-schema behaviour is preserved
    exactly on emits without the `sub_type_columns` field.

    Args:
        col: The records-table column name under consideration.
        owned: The sub-type's declared columns, or None to disable pruning.
        discriminator: The kind's `prop__<kind>_type` column (never pruned).

    Returns:
        True to omit the column, False to keep it.
    """
    if owned is None:
        return False
    if col == discriminator:
        return False
    if not (col.startswith("prop__") or col.startswith(REF_INDEX_PREFIX)):
        return False
    return col not in owned


def _distinct_values(emit: "Emit", table: str, column: str) -> list[str]:
    """Run SELECT DISTINCT on a column and return the values as strings.

    Delegates to reader.distinct_prop_values for composition-conformance.
    Expects table = 'records__<kind>' and column = 'prop__<property_name>'.

    Args:
        emit: The open emit.
        table: DuckDB table name (records__<kind>).
        column: Column name (prop__<property_name>).

    Returns:
        List of distinct string values in native-type ORDER BY 1 order.
    """
    kind = table[len("records__") :] if table.startswith("records__") else table
    property_name = column[len("prop__") :] if column.startswith("prop__") else column
    return distinct_prop_values(emit, kind, property_name)


def _membership_kinds_and_props(
    all_tables: tuple[TableSpec, ...],
) -> list[tuple[str, str, list[str]]]:
    """Return (kind, property, elem_columns) for each membership table.

    Args:
        all_tables: All sidecar TableSpec objects.

    Returns:
        List of (record_kind, property_name, elem_col_names) tuples.
    """
    result = []
    for table in all_tables:
        if not isinstance(table, TableSpec):
            continue
        if table.category == "membership" and table.record_kind and table.property:
            elem_cols = [
                col.name for col in table.columns if col.name.startswith("elem__")
            ]
            result.append((table.record_kind, table.property, elem_cols))
    return result


def _write_dim_scd2_stub(
    w: Callable[[str], None],
    kind: str,
    name: str,
    all_tables: tuple[TableSpec, ...],
    sidecar: "Sidecar",
    notice_sink: "NoticeSink",
    filter_line: str | None,
    owned_columns: frozenset[str] | None = None,
    advisory_comment: str | None = None,
) -> None:
    """Write a SCD-2 dim table stub block.

    Skips non-exempt temporal_class: slice_only columns from the column
    proposal loop (via is_non_exempt_slice_only), emitting one
    'slice-only-column-omitted' notice per skip naming kind and column, in
    sidecar column order. The exempt discriminator remains proposable.

    When `owned_columns` is provided (a per-sub-type stub over an emit carrying
    `sub_type_columns`), columns the sub-type does not declare are pruned as
    structurally inapplicable — the stub proposes only this sub-type's columns
    instead of the whole union. None (the default) preserves union-schema
    behaviour.

    Args:
        w: Line-writing callable.
        kind: The record kind name.
        name: The proposed output table name.
        all_tables: All sidecar TableSpec objects.
        sidecar: The open emit's sidecar.
        notice_sink: Receiver for skip notices.
        filter_line: Filter YAML line to include in source, or None for no filter.
        owned_columns: The sub-type's declared columns for pruning, or None to
            propose the full union (bare-string kinds, or emits without the
            `sub_type_columns` field).
        advisory_comment: The `presentation_id` natural-key advisory comment
            line for this kind, or None when the block carries no whole-table
            claim.
    """
    tracked_cols = _get_tracked_columns(kind, all_tables)
    all_cols = _get_records_columns(kind, all_tables)
    discriminator = f"prop__{kind}_type"
    w(f"    - name: {name}")
    w("      role: dim  # proposal: dimension kind")
    w("      scd: type2  # proposal: history_tracked columns present")
    w("      source:")
    w("        grain: records")
    w(f"        kind: {kind}")
    if filter_line is not None:
        w(filter_line)
    w("      key: [id, valid_from]")
    if advisory_comment is not None:
        w(advisory_comment)
    w("      columns:")
    w(f"        - {{name: id, from: {_DIM_ID_SURFACE}}}")
    for col in all_cols:
        if records_column_role(col) not in ("payload", "presentation"):
            continue
        if _is_pruned(col, owned_columns, discriminator):
            continue
        if is_non_exempt_slice_only(sidecar, kind, col):
            notice_sink(
                Notice(
                    code="slice-only-column-omitted",
                    message=(
                        f"kind '{kind}': column '{col}' is temporal_class:"
                        " slice_only; omitted from the SCD-2 stub's column proposal"
                    ),
                )
            )
            continue
        short = col.replace("prop__", "")
        if col in tracked_cols:
            evidence = _versions_per_record(sidecar, kind, short)
            w(
                f"        - {{name: {short}, from: {col}}}"
                f"  # tracked -> per-version; {evidence}"
            )
        else:
            w(f"        - {{name: {short}, from: {col}}}")
    w("        - {name: valid_from, derived: {scd_window: valid_from}}")
    w("        - {name: valid_to, derived: {scd_window: valid_to}}")
    w("")


def _write_dim_type1_stub(
    w: Callable[[str], None],
    kind: str,
    name: str,
    filter_line: str | None = None,
    advisory_comment: str | None = None,
) -> None:
    """Write a SCD-1 dim table stub block.

    Args:
        w: Line-writing callable.
        kind: The record kind name.
        name: The proposed output table name.
        filter_line: Optional filter YAML line to include in source, or None.
        advisory_comment: The `presentation_id` natural-key advisory comment
            line for this kind, or None when the block carries no whole-table
            claim.
    """
    w(f"    - name: {name}")
    w("      role: dim  # proposal: dimension kind")
    w("      scd: type1  # proposal: no tracked columns detected")
    w("      source:")
    w("        grain: records")
    w(f"        kind: {kind}")
    if filter_line is not None:
        w(filter_line)
    w("      key: [id]")
    if advisory_comment is not None:
        w(advisory_comment)
    w("      columns:")
    w(f"        - {{name: id, from: {_DIM_ID_SURFACE}}}")
    w(
        "        # Add more columns from prop__* here;"
        " e.g. {name: name, from: prop__name}"
    )
    w("")


def _write_fact_stub(
    w: Callable[[str], None],
    kind: str,
    name: str,
    all_tables: tuple[TableSpec, ...],
    filter_line: str | None = None,
    owned_columns: frozenset[str] | None = None,
    advisory_comment: str | None = None,
) -> None:
    """Write a fact table stub block with FK-candidate comments.

    When `owned_columns` is provided (a per-sub-type stub over an emit carrying
    `sub_type_columns`), reference columns the sub-type does not declare are
    pruned from the FK-candidate comments as structurally inapplicable. None
    (the default) lists every reference on the union table.

    Args:
        w: Line-writing callable.
        kind: The record kind name.
        name: The proposed output table name.
        all_tables: All sidecar TableSpec objects.
        filter_line: Optional filter YAML line to include in source, or None.
        owned_columns: The sub-type's declared columns for pruning, or None to
            list the full union (bare-string kinds, or emits without the
            `sub_type_columns` field).
        advisory_comment: The `presentation_id` natural-key advisory comment
            line for this kind, or None when the block carries no whole-table
            claim.
    """
    discriminator = f"prop__{kind}_type"
    w(f"    - name: {name}")
    w("      role: fact  # proposal: fact kind")
    w("      source:")
    w("        grain: records")
    w(f"        kind: {kind}")
    if filter_line is not None:
        w(filter_line)
    w("      key: [id]")
    if advisory_comment is not None:
        w(advisory_comment)
    w("      columns:")
    w("        - {name: id, from: record_id}")
    for tbl in all_tables:
        if tbl.name == f"records__{kind}":
            for tbl_col in tbl.columns:
                if tbl_col.references:
                    if _is_pruned(tbl_col.name, owned_columns, discriminator):
                        continue
                    ref = tbl_col.references
                    w(
                        f"        # FK candidate:"
                        f" {{name: {ref}_id,"
                        f" fk: {{to: dim_{ref},"
                        " via: reference}}"
                        "}"
                    )
    w("        # Add more columns from prop__* here")
    w("")


def _presentation_id_advisory_comment(
    presentation_keys: "PresentationKeys | None", kind: str
) -> str | None:
    """The advisory comment naming `presentation_id` as a kind's natural key.

    Consulted once per proposed kind: a flat kind's `key` entry, or a
    partitioned kind's rollup with a non-None `unique_within`, both declare a
    whole-table uniqueness claim over `presentation_id`
    (`PresentationKeys.whole_table_claim`). An absent block, a kind absent
    from the block, or a no-claim rollup yield no comment. Subsumed on a
    dim stub whose own population elects `presentation_id` — the caller
    passes None there instead of this comment.

    Args:
        presentation_keys: The open emit's `presentation_keys` view (from
            `Sidecar.presentation_keys()`, fetched once by the caller), or
            None when the emit carries no block.
        kind: The record kind under proposal.

    Returns:
        A single advisory comment line, or None when no whole-table claim holds.
    """
    if presentation_keys is None or kind not in presentation_keys.kinds():
        return None
    claim = presentation_keys.whole_table_claim(kind)
    if claim.unique_within is None:
        return None
    return (
        "      # NOTE: the emit's presentation_keys block declares"
        f" `presentation_id` a natural key for '{kind}',"
        f" unique within {claim.unique_within}"
    )


def _write_sub_type_stub(
    w: Callable[[str], None],
    kind: str,
    sub_type: str,
    config_role: str,
    has_tracked: bool,
    all_tables: tuple[TableSpec, ...],
    sidecar: "Sidecar",
    notice_sink: "NoticeSink",
    advisory_comment: str | None,
) -> None:
    """Write one dim/fact stub for a single (kind, sub_type) population.

    Shared by both sub-typed splitting paths in `_build_candidate_yaml`: a
    kind whose `record_roles` entry is object-valued (role varies per
    sub-type, today only `actor`) and a bare-role kind that still carries a
    declared `<kind>_type` domain (role uniform, e.g. `entity`, `resource`).
    Both pass one sub-type at a time; only where the sub-type set comes from
    differs.

    Args:
        w: Line-writing callable.
        kind: The record kind name.
        sub_type: The sub-type discriminator value for this stub.
        config_role: `"dim"` or `"fact"`, already mapped from the registry role.
        has_tracked: Whether the kind's records table carries a
            `history_tracked` column (kind-level, precomputed by the caller).
        all_tables: All sidecar TableSpec objects.
        sidecar: The open emit's sidecar.
        notice_sink: Receiver for slice-only-column-omitted skip notices.
        advisory_comment: The `presentation_id` natural-key advisory comment
            for this kind, or None.
    """
    owned = _owned_columns(sidecar, kind, sub_type)
    name = f"{config_role}_{kind}_{sub_type}"
    filter_line = (
        f"        filter: {{prop__{kind}_type: {sub_type}}}  # one slice per sub-type"
    )
    w(f"    # --- {config_role}: {kind} sub-type '{sub_type}' ---")
    if config_role == "dim":
        if has_tracked:
            _write_dim_scd2_stub(
                w,
                kind,
                name,
                all_tables,
                sidecar,
                notice_sink,
                filter_line,
                owned_columns=owned,
                advisory_comment=advisory_comment,
            )
        else:
            _write_dim_type1_stub(
                w,
                kind,
                name,
                filter_line,
                advisory_comment=advisory_comment,
            )
    else:
        _write_fact_stub(
            w,
            kind,
            name,
            all_tables,
            filter_line,
            owned_columns=owned,
            advisory_comment=advisory_comment,
        )


def _build_candidate_yaml(emit: "Emit", notice_sink: "NoticeSink") -> str:
    """Build a commented candidate YAML config from the sidecar.

    Reads record_roles for role (authoritative), the sidecar tables for
    grain/column shape, history_tracked for SCD class, and the reader's
    DISTINCT introspection for fact-discriminator fan-out values.

    Args:
        emit: The open emit. Its sidecar must carry record_roles (checked by
            generate_init_config before this is called).
        notice_sink: Receiver for slice-only-column-omitted skip notices
            (threaded to the SCD-2 stub writer).

    Returns:
        A YAML string with candidate config and inline comments.
    """
    sidecar = emit.sidecar
    all_tables = sidecar.tables()
    record_roles = sidecar.record_roles()
    # Caller guarantees record_roles is not None
    assert record_roles is not None
    presentation_keys = sidecar.presentation_keys()
    proposal = propose_key_election(sidecar)

    membership_info = _membership_kinds_and_props(all_tables)

    buf = io.StringIO()

    def w(line: str = "") -> None:
        buf.write(line + "\n")

    w("# Candidate dimensional export config — generated by `fabulexa-forge init`")
    w("# This is a ~70-80% starting point. Review all role/scd proposals.")
    w("# Classification (role, scd) is AUTHOR-AUTHORITATIVE — confirm or flip each.")
    w("")
    w("mode: dimensional")
    w("")
    for line in render_keys_block(proposal):
        w(line)
    w("dimensional:")
    w("  tables:")

    for kind in record_roles.kinds():
        advisory_comment = _presentation_id_advisory_comment(presentation_keys, kind)
        sub_types = sidecar.subtype_values(kind)
        if record_roles.is_subtyped(kind):
            # Object-valued kind (today only `actor`): role varies per
            # sub-type, so `record_roles` is the authoritative sub-type set.
            has_tracked = _columns_have_history_tracked(kind, all_tables)
            for sub_type in record_roles.sub_types(kind):
                registry_role = record_roles.role_of(kind, sub_type)
                config_role = _ROLE_MAP[registry_role]
                _write_sub_type_stub(
                    w,
                    kind,
                    sub_type,
                    config_role,
                    has_tracked,
                    all_tables,
                    sidecar,
                    notice_sink,
                    advisory_comment,
                )
        elif sub_types:
            # Bare-role kind (record_roles[kind] is a plain string, role
            # uniform across sub-types) that still carries a declared
            # `<kind>_type` domain — e.g. `entity`, `resource`. Splitting is
            # the sidecar's own sub-typing signal (Sidecar.subtype_values),
            # independent of record_roles's object-vs-string shape: one
            # conformed dim/fact would union unrelated sub-type schemas into
            # a mostly-NULL table.
            has_tracked = _columns_have_history_tracked(kind, all_tables)
            registry_role = record_roles.role_of(kind, None)
            config_role = _ROLE_MAP[registry_role]
            for sub_type in sub_types:
                _write_sub_type_stub(
                    w,
                    kind,
                    sub_type,
                    config_role,
                    has_tracked,
                    all_tables,
                    sidecar,
                    notice_sink,
                    advisory_comment,
                )
        else:
            # Flat kind: single role, no declared <kind>_type domain
            registry_role = record_roles.role_of(kind, None)
            config_role = _ROLE_MAP[registry_role]
            has_tracked = _columns_have_history_tracked(kind, all_tables)
            has_discriminator = _kind_has_discriminator(kind, all_tables)

            if config_role == "dim":
                if has_tracked:
                    w(
                        f"    # --- SCD-2 dim: {kind}"
                        " (history_tracked columns detected) ---"
                    )
                    _write_dim_scd2_stub(
                        w,
                        kind,
                        f"dim_{kind}",
                        all_tables,
                        sidecar,
                        notice_sink,
                        None,
                        advisory_comment=advisory_comment,
                    )
                else:
                    w(f"    # --- Type-1 dim: {kind} ---")
                    _write_dim_type1_stub(
                        w,
                        kind,
                        f"dim_{kind}",
                        advisory_comment=advisory_comment,
                    )
            else:
                # Fact: modelling-discriminator path (SELECT DISTINCT observed values)
                if has_discriminator:
                    discriminator_col = f"prop__{kind}_type"
                    records_table = f"records__{kind}"
                    values = _distinct_values(emit, records_table, discriminator_col)
                    if values:
                        for val in values:
                            w(f"    # --- fact: {kind} discriminator slice '{val}' ---")
                            filter_line = (
                                f"        filter: {{{discriminator_col}: {val}}}"
                                "  # SELECT DISTINCT observed value"
                            )
                            _write_fact_stub(
                                w,
                                kind,
                                f"fact_{kind}_{val}",
                                all_tables,
                                filter_line,
                                advisory_comment=advisory_comment,
                            )
                    else:
                        w(
                            f"    # --- fact: {kind}"
                            " (discriminator, no observed values) ---"
                        )
                        _write_fact_stub(
                            w,
                            kind,
                            f"fact_{kind}",
                            all_tables,
                            advisory_comment=advisory_comment,
                        )
                else:
                    w(f"    # --- fact: {kind} ---")
                    _write_fact_stub(
                        w,
                        kind,
                        f"fact_{kind}",
                        all_tables,
                        advisory_comment=advisory_comment,
                    )

        # Membership FK candidates for this kind
        for mem_kind, mem_prop, elem_cols in membership_info:
            if mem_kind == kind and elem_cols:
                w(f"    # Membership FK: {kind}.{mem_prop} -> membership table")
                for elem_col in elem_cols:
                    field = elem_col.replace("elem__", "")
                    w(
                        f"    # {{name: {field}_id, fk: {{to: dim_<target>,"
                        f" via: membership, where: {{{elem_col}: <value>}}}}}}"
                    )
                w("")

    return buf.getvalue()


def generate_init_config(emit: "Emit", notice_sink: "NoticeSink") -> str:
    """Generate a commented candidate dimensional export config from an emit.

    Reads `record_roles` for warehouse role (authoritative), `Sidecar.
    subtype_values` (`enum_domains[<kind>][<kind>_type]`) for whether and how
    a kind splits into per-sub-type stubs, the sidecar tables for grain/column
    shape, `history_tracked` for SCD class, and the reader's DISTINCT
    introspection for fact-discriminator fan-out on kinds with no declared
    `<kind>_type` domain. Role is never inferred from reference topology.

    New behavior: slice_only columns are skipped from column proposals
    (joining identity and lifecycle columns as never-proposed), one
    'slice-only-column-omitted' Notice per skip; the exempt discriminator
    remains proposable and drives filter pre-fill unchanged.

    Splitting and role resolution are separate questions. Whether a kind
    splits into per-sub-type stubs follows `Sidecar.subtype_values(kind)`
    (non-empty iff the sidecar declares a `<kind>_type` domain) —
    independent of `record_roles[kind]`'s shape. Role resolution follows
    `record_roles`: the object-valued kind (today only `actor`) resolves each
    sub-type's role independently via `role_of(kind, sub_type)`; every other
    sub-typed kind (e.g. `entity`, `resource`) has one role uniform across all
    its sub-types, resolved once via `role_of(kind, None)`. The registry
    tokens `"dimension"` / `"fact"` map to config `role` tokens `dim` /
    `fact`. `scd` class is per-kind (from `history_tracked`). The fact
    modelling-discriminator `SELECT DISTINCT` fan-out (for a bare-string fact
    kind with no declared `<kind>_type` domain but a `prop__<kind>_type`
    column) is unchanged — the fallback for a kind `subtype_values` reports
    as flat.

    Kind order and sub-type order follow the registry's lexicographic key order;
    the fact fan-out uses the reader's DISTINCT native-type order. No topology
    traversal, RNG, or clock participates — output is a pure function of
    (emit, code version).

    New behavior: when the sidecar's `presentation_keys` block carries a
    whole-table claim for a proposed kind (a flat `key` entry, or a
    partitioned kind's rollup with a non-None `unique_within`), the kind's
    stub gains one advisory comment naming `presentation_id` as the
    contract-declared natural key and its scope. A kind absent from the
    block, an absent block, or a no-claim rollup adds no comment. The block
    is consulted via `Sidecar.presentation_keys()` and shares its
    strict-on-read behavior.

    Also proposes a `keys:` block through `exporters.keys_init.propose_key_election`
    / `render_keys_block` — the cross-mode election menu: uniform `record_index`
    active for every population, with each population's resolvable alternatives
    (`record_id` always, `presentation_id` where the registry declares the
    population) offered as swap-not-join comments. Every proposed dim's id
    column sources `from:` the active election (`record_index`), keeping the
    shipped `id` name; the `presentation_id` natural-key advisory comment is
    retained on every stub whose kind carries a whole-table claim. FK
    candidates stay comments, `target_key`-free.

    Args:
        emit: The open emit. Its sidecar must carry `record_roles`.
        notice_sink: Receiver for proposal notices.

    Returns:
        A YAML string: a commented candidate `mode: dimensional` config, with
        a proposed `keys:` block. One table stub per dimension/fact kind (per
        declared sub-type for every sub-typed kind, object-valued or bare-role
        alike), with SCD-2 window
        columns where `history_tracked` applies, FK-candidate comments per
        reference column, membership-FK candidate comments for kinds that own
        a membership table, and the `presentation_id` natural-key advisory
        comment where the block claims it. No `exclude` block is proposed.

    Raises:
        InitRequiresRecordRoles: The sidecar omits `record_roles`.
        PresentationKeysInvalidError: The sidecar's `presentation_keys` block
            is present and incoherent.
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    if emit.sidecar.record_roles() is None:
        raise InitRequiresRecordRoles(
            "The sidecar omits `record_roles`; `init` cannot propose roles without"
            " the registry. Re-emit with a producer that includes `record_roles`."
        )
    return _build_candidate_yaml(emit, notice_sink)
