"""Sidecar-driven candidate config generator for `fabulexa-forge init`.

Reads the sidecar (and DISTINCT discriminators where needed) and emits a
commented candidate config the author edits. Honest about being a starting
point (~70-80%); classification stays author-authoritative.

Role is read exclusively from `record_roles` in the sidecar. Bare-string
kinds resolve their role directly; the object-valued kind (actor) splits per
declared sub-type. `enum_domains` and reference topology are not consulted.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Callable

from fabulexa_forge.errors import ElectionUnionUnsafe, InitRequiresRecordRoles
from fabulexa_forge.exporters.election import check_edge_union_safety, resolve_election
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.slice_only import is_non_exempt_slice_only
from fabulexa_forge.reader.records_columns import (
    REF_INDEX_PREFIX,
    records_column_role,
)
from fabulexa_forge.reader.relations import distinct_prop_values
from fabulexa_forge.reader.sidecar import TableSpec

if TYPE_CHECKING:
    from fabulexa_forge.config.models import KeySurface
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import PresentationKeys, RecordRoles, Sidecar


# Registry role token -> config role token
_ROLE_MAP: dict[str, str] = {
    "dimension": "dim",
    "fact": "fact",
}


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


def _domains_for_kinds(
    sidecar: "Sidecar", record_roles: "RecordRoles"
) -> dict[str, tuple[str, ...]]:
    """Every proposed kind's declared sub-type domain, `()` for a flat kind.

    Args:
        sidecar: The open emit's sidecar.
        record_roles: The sidecar's `record_roles` view.

    Returns:
        kind -> `sidecar.subtype_values(kind)`, over `record_roles.kinds()`.
    """
    return {kind: sidecar.subtype_values(kind) for kind in record_roles.kinds()}


def _population_declared(
    presentation_keys: "PresentationKeys | None", kind: str, sub_type: str | None
) -> bool:
    """Whether one population carries a `presentation_keys` declaration.

    A flat kind's `key` entry, or a partitioned kind's per-sub-type entry
    (`key_for`) — presence alone, independent of the kind's rollup claim
    (a rollup with no claim still leaves each individually-declared
    sub-type's own entry present).

    Args:
        presentation_keys: The open emit's `presentation_keys` view, or None.
        kind: The population's kind.
        sub_type: The population's discriminator value, or None for a flat kind.

    Returns:
        True iff the population has its own registry entry.
    """
    if presentation_keys is None or kind not in presentation_keys.kinds():
        return False
    try:
        if sub_type is None:
            presentation_keys.key(kind)
        else:
            presentation_keys.key_for(kind, sub_type)
    except (KeyError, ValueError):
        return False
    return True


def _natural_expanded_surfaces(
    presentation_keys: "PresentationKeys | None",
    domains: "dict[str, tuple[str, ...]]",
) -> "dict[tuple[str, str | None], KeySurface]":
    """The doc's natural per-population proposal: declared -> presentation_id,
    undeclared -> record_index — total over every population `domains` covers.

    Args:
        presentation_keys: The open emit's `presentation_keys` view, or None.
        domains: Every proposed kind's sub-type domain, from `_domains_for_kinds`.

    Returns:
        (kind, sub_type) -> the natural election, one entry per population.
    """
    expanded: "dict[tuple[str, str | None], KeySurface]" = {}
    for kind, domain in domains.items():
        sub_types: tuple[str | None, ...] = domain if domain else (None,)
        for sub_type in sub_types:
            expanded[(kind, sub_type)] = (
                "presentation_id"
                if _population_declared(presentation_keys, kind, sub_type)
                else "record_index"
            )
    return expanded


def _build_keys_config(
    expanded: "dict[tuple[str, str | None], KeySurface]",
    domains: "dict[str, tuple[str, ...]]",
) -> "dict[str, KeySurface | dict[str, KeySurface]]":
    """The config `keys` block shape from an expanded per-population map.

    Mirrors the registry's own shape (doc § `init` proposals): a flat kind
    proposes the scalar; a partitioned kind proposes the per-sub-type map,
    collapsed to the scalar when every sub-type agrees.

    Args:
        expanded: (kind, sub_type) -> elected surface, total over `domains`.
        domains: Every proposed kind's sub-type domain.

    Returns:
        The `ExportConfig.keys`-shaped proposal.
    """
    config: "dict[str, KeySurface | dict[str, KeySurface]]" = {}
    for kind, domain in domains.items():
        if not domain:
            config[kind] = expanded[(kind, None)]
            continue
        sub_map: "dict[str, KeySurface]" = {
            sub_type: expanded[(kind, sub_type)] for sub_type in domain
        }
        values = set(sub_map.values())
        config[kind] = next(iter(values)) if len(values) == 1 else sub_map
    return config


def _reference_edges(all_tables: tuple[TableSpec, ...]) -> list[tuple[str, str, str]]:
    """Every `references` column across every records table — the reference graph.

    Args:
        all_tables: All sidecar TableSpec objects.

    Returns:
        (source_kind, column_name, target_kind) triples, in sidecar order.
    """
    edges: list[tuple[str, str, str]] = []
    for table in all_tables:
        if not isinstance(table, TableSpec):
            continue
        if not table.name.startswith("records__"):
            continue
        kind = table.name[len("records__") :]
        for col in table.columns:
            if col.references:
                edges.append((kind, col.name, col.references))
    return edges


def _self_gate_keys_proposal(
    sidecar: "Sidecar",
    all_tables: tuple[TableSpec, ...],
    domains: "dict[str, tuple[str, ...]]",
    expanded: "dict[tuple[str, str | None], KeySurface]",
) -> "tuple[dict[str, KeySurface | dict[str, KeySurface]], dict[str, str]]":
    """Gate the natural proposal through `resolve_election` + edge union safety.

    Doc § `init` proposals: `init` runs its own proposal through the exact
    machinery the export would run. Dimensional's plan-time gate over an
    ungrained proposal (no `fk:` columns are proposed — FK candidates stay
    comments) is `check_edge_union_safety` over the emit's reference graph:
    per `references` column, gated against the target kind's full declared
    domain with no `target_key` override (an uncommented FK candidate
    inherits). A kind implicated in a failure degrades to uniform
    `record_index` — always passing, by construction (doc's Invariants).
    One pass suffices: each edge's verdict depends only on its own target
    kind's populations, so degrading the implicated kinds cannot newly break
    an edge that previously passed.

    Args:
        sidecar: The open emit's sidecar.
        all_tables: All sidecar TableSpec objects.
        domains: Every proposed kind's sub-type domain.
        expanded: The natural per-population proposal, mutated in place with
            any degradations.

    Returns:
        (keys_config, degraded) — the gated `ExportConfig.keys`-shaped
        proposal, and kind -> a one-line reason naming the forcing gate, for
        every kind the gate degraded.
    """
    election = resolve_election(sidecar, _build_keys_config(expanded, domains))
    degraded: dict[str, str] = {}
    for source_kind, column, target_kind in _reference_edges(all_tables):
        if target_kind not in domains:
            continue
        if target_kind in degraded:
            continue
        edge_name = f"{source_kind}.{column}"
        try:
            check_edge_union_safety(
                election,
                target_kind,
                domains[target_kind],
                edge_name,
                surface_override=None,
            )
        except ElectionUnionUnsafe as exc:
            degraded[target_kind] = f"ElectionUnionUnsafe: {exc}"

    if not degraded:
        return _build_keys_config(expanded, domains), degraded

    for kind in degraded:
        sub_types: tuple[str | None, ...] = domains[kind] if domains[kind] else (None,)
        for sub_type in sub_types:
            expanded[(kind, sub_type)] = "record_index"
    return _build_keys_config(expanded, domains), degraded


def _write_keys_block(
    w: Callable[[str], None],
    keys_config: "dict[str, KeySurface | dict[str, KeySurface]]",
    degraded: dict[str, str],
) -> None:
    """Write the proposed `keys:` block, one line per kind (or per sub-type).

    A degraded kind always renders as a scalar `record_index` (uniform
    election collapses by construction) with a trailing comment naming the
    forcing gate.

    Args:
        w: Line-writing callable.
        keys_config: The gated `ExportConfig.keys`-shaped proposal.
        degraded: kind -> reason, for every kind the self-gate forced.
    """
    w("keys:")
    for kind, election in keys_config.items():
        if isinstance(election, dict):
            w(f"  {kind}:")
            for sub_type, surface in election.items():
                w(f"    {sub_type}: {surface}")
        else:
            reason = f"  # NOTE: {degraded[kind]}" if kind in degraded else ""
            w(f"  {kind}: {election}{reason}")
    w("")


def _write_dim_scd2_stub(
    w: Callable[[str], None],
    kind: str,
    name: str,
    all_tables: tuple[TableSpec, ...],
    sidecar: "Sidecar",
    notice_sink: "NoticeSink",
    filter_line: str | None,
    id_surface: "KeySurface",
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
        id_surface: The population's elected surface (`record_index` or
            `presentation_id`) — the id column's `from:` value, aligning the
            dim's declared key with the proposed election.
        owned_columns: The sub-type's declared columns for pruning, or None to
            propose the full union (bare-string kinds, or emits without the
            `sub_type_columns` field).
        advisory_comment: The `presentation_id` natural-key advisory comment
            line for this kind, or None when the block carries no whole-table
            claim, or when `id_surface` already subsumes it.
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
    w(f"        - {{name: id, from: {id_surface}}}")
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
            w(f"        - {{name: {short}, from: {col}}}  # tracked -> per-version")
        else:
            w(f"        - {{name: {short}, from: {col}}}")
    w("        - {name: valid_from, derived: {scd_window: valid_from}}")
    w("        - {name: valid_to, derived: {scd_window: valid_to}}")
    w("")


def _write_dim_type1_stub(
    w: Callable[[str], None],
    kind: str,
    name: str,
    id_surface: "KeySurface",
    filter_line: str | None = None,
    advisory_comment: str | None = None,
) -> None:
    """Write a SCD-1 dim table stub block.

    Args:
        w: Line-writing callable.
        kind: The record kind name.
        name: The proposed output table name.
        id_surface: The population's elected surface (`record_index` or
            `presentation_id`) — the id column's `from:` value, aligning the
            dim's declared key with the proposed election.
        filter_line: Optional filter YAML line to include in source, or None.
        advisory_comment: The `presentation_id` natural-key advisory comment
            line for this kind, or None when the block carries no whole-table
            claim, or when `id_surface` already subsumes it.
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
    w(f"        - {{name: id, from: {id_surface}}}")
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

    domains = _domains_for_kinds(sidecar, record_roles)
    expanded = _natural_expanded_surfaces(presentation_keys, domains)
    keys_config, degraded = _self_gate_keys_proposal(
        sidecar, all_tables, domains, expanded
    )

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
    _write_keys_block(w, keys_config, degraded)
    w("dimensional:")
    w("  tables:")

    for kind in record_roles.kinds():
        advisory_comment = _presentation_id_advisory_comment(presentation_keys, kind)
        if record_roles.is_subtyped(kind):
            # Object-valued kind: split per declared sub-type
            has_tracked = _columns_have_history_tracked(kind, all_tables)
            for sub_type in record_roles.sub_types(kind):
                owned = _owned_columns(sidecar, kind, sub_type)
                registry_role = record_roles.role_of(kind, sub_type)
                config_role = _ROLE_MAP[registry_role]
                name = f"{config_role}_{kind}_{sub_type}"
                filter_line = (
                    f"        filter: {{prop__{kind}_type: {sub_type}}}"
                    "  # one slice per sub-type"
                )
                w(f"    # --- {config_role}: {kind} sub-type '{sub_type}' ---")
                if config_role == "dim":
                    id_surface = expanded[(kind, sub_type)]
                    dim_advisory = (
                        None if id_surface == "presentation_id" else advisory_comment
                    )
                    if has_tracked:
                        _write_dim_scd2_stub(
                            w,
                            kind,
                            name,
                            all_tables,
                            sidecar,
                            notice_sink,
                            filter_line,
                            id_surface,
                            owned_columns=owned,
                            advisory_comment=dim_advisory,
                        )
                    else:
                        _write_dim_type1_stub(
                            w,
                            kind,
                            name,
                            id_surface,
                            filter_line,
                            advisory_comment=dim_advisory,
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
        else:
            # Bare-string kind: single role
            registry_role = record_roles.role_of(kind, None)
            config_role = _ROLE_MAP[registry_role]
            has_tracked = _columns_have_history_tracked(kind, all_tables)
            has_discriminator = _kind_has_discriminator(kind, all_tables)

            if config_role == "dim":
                id_surface = expanded[(kind, None)]
                dim_advisory = (
                    None if id_surface == "presentation_id" else advisory_comment
                )
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
                        id_surface,
                        advisory_comment=dim_advisory,
                    )
                else:
                    w(f"    # --- Type-1 dim: {kind} ---")
                    _write_dim_type1_stub(
                        w,
                        kind,
                        f"dim_{kind}",
                        id_surface,
                        advisory_comment=dim_advisory,
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

    Reads `record_roles` for warehouse role (authoritative), the sidecar tables
    for grain/column shape, `history_tracked` for SCD class, and the reader's
    DISTINCT introspection for fact-discriminator fan-out values. Role is never
    inferred from reference topology, and `enum_domains` is not consulted.

    New behavior: slice_only columns are skipped from column proposals
    (joining identity and lifecycle columns as never-proposed), one
    'slice-only-column-omitted' Notice per skip; the exempt discriminator
    remains proposable and drives filter pre-fill unchanged.

    A bare-string kind resolves its role from `record_roles[kind]`. The
    object-valued kind (today only `actor`) splits per declared sub-type from
    `record_roles[kind].sub_types()`, with each sub-type's role resolved
    independently via `role_of(kind, sub_type)`. The registry tokens
    `"dimension"` / `"fact"` map to config `role` tokens `dim` / `fact`. `scd`
    class is per-kind (from `history_tracked`); role is per-sub-type. The fact
    modelling-discriminator `SELECT DISTINCT` fan-out (for a bare-string fact
    kind carrying a non-taxonomy `prop__<kind>_type`) is unchanged.

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

    New behavior (key election): additionally proposes a `keys:` block —
    `presentation_id` for every population with its own `presentation_keys`
    registry entry, `record_index` elsewhere; a flat kind proposes the
    scalar, a partitioned kind the per-sub-type map (collapsed to the scalar
    when every sub-type agrees). Self-gated through `resolve_election` +
    `check_edge_union_safety` over the emit's reference graph before any
    line is written: a kind implicated in a union-unsafe pair degrades to
    the uniform `record_index` scalar, with a comment naming the forcing
    gate — the proposal never fails its own gate. Each proposed dim's id
    column sources `from:` its population's elected surface, keeping the
    shipped `id` name; where the election is `presentation_id` this
    subsumes the natural-key advisory comment on that stub (retained on
    every other stub). FK candidates stay comments, `target_key`-free.

    Args:
        emit: The open emit. Its sidecar must carry `record_roles`.
        notice_sink: Receiver for proposal notices.

    Returns:
        A YAML string: a commented candidate `mode: dimensional` config, with
        a proposed `keys:` block. One table stub per dimension/fact kind (per
        declared sub-type for the object-valued kind), with SCD-2 window
        columns where `history_tracked` applies, FK-candidate comments per
        reference column, membership-FK candidate comments for kinds that own
        a membership table, and the `presentation_id` natural-key advisory
        comment where the block claims it (subsumed on a dim stub whose own
        election is `presentation_id`). No `exclude` block is proposed.

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
