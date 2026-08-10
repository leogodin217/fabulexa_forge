"""FK labeled-edge pathfind for the dimensional exporter.

Builds JOIN SQL fragments for two edge types:
  - via: reference  — transitive equality joins along prop__<x> columns that
                      carry a `references` annotation in the sidecar. Multi-hop.
  - via: membership — locate the membership__<kind>__<property> table for the
                      anchor kind, join on record_id + the where predicate,
                      project member__<member_field>__id whose kind = dim's
                      source kind.

`check_fk_slice_only` enforces SliceOnlyColumnRefused over the traversed hop
chain (and, for point-in-time membership FKs, the as_of column) — called from
validate_table immediately after build_fk_expr.

All functions are module-level for independent testability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from fabulexa_forge.config.models import (
        ColumnDecl,
        DimensionalConfig,
        KeySurface,
        TableDecl,
    )
    from fabulexa_forge.reader.sidecar import ColumnSpec, Sidecar

from fabulexa_forge._sql import render_predicate_condition
from fabulexa_forge.derivations.reference_resolution import (
    _collect_reference_columns,
    _find_all_reference_paths,
    _path_hint_to_cols,
    build_membership_edge_sql,
    build_reference_path_sql,
    get_fork_path_from_sidecar,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.populations import (
    DimSourcePopulations,
    dim_identity_relation_at_end_sql,
    dim_population_sub_types,
)
from fabulexa_forge.exporters.election import build_population_spine_sql
from fabulexa_forge.exporters.slice_only import (
    is_non_exempt_slice_only,
    slice_only_refusal_message,
)
from fabulexa_forge.reader.errors import TableNotFoundError

# ---------------------------------------------------------------------------
# FK surface dispatch (shared by all four builders)
# ---------------------------------------------------------------------------


def _fk_identity_relation_sql(
    sidecar: "Sidecar",
    fork_path: str,
    resolved_surface: "Literal['record_index', 'presentation_id']",
    dim_populations: DimSourcePopulations,
) -> str:
    """The (possibly population-restricted) FK identity relation to LEFT JOIN.

    Composes the dimensional entry-point relation
    (`dim_identity_relation_at_end_sql`) and, when the destination dim's
    source population set is a proper subset of its kind's declared domain,
    restricts it to that set via a semi-join on the population spine
    (`build_population_spine_sql`) — the sprint contracts' § 4 restriction
    rule: the full domain needs no restriction; a proper subset composes one
    so an out-of-set target row resolves to no matching relation row (NULL
    through the LEFT JOIN, never a collision-hiding fabrication).

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `get_fork_path_from_sidecar`.
        resolved_surface: The FK's one resolved non-record_id surface.
        dim_populations: The destination dim's source population set.

    Returns:
        A complete SELECT producing (record_id, `resolved_surface`).
    """
    relation_sql = dim_identity_relation_at_end_sql(
        sidecar, fork_path, dim_populations.kind, resolved_surface
    )
    if not dim_populations.proper_subset:
        return relation_sql
    spine_sql = build_population_spine_sql(
        sidecar,
        fork_path,
        dim_populations.kind,
        dim_population_sub_types(dim_populations),
    )
    return (
        f'SELECT "record_id", "{resolved_surface}" FROM ({relation_sql}) AS "_ident"'
        f' WHERE "_ident"."record_id" IN ({spine_sql})'
    )


def _dispatch_fk_surface(
    record_id_expr: str,
    sidecar: "Sidecar",
    fork_path: str,
    resolved_surface: "KeySurface",
    dim_populations: DimSourcePopulations,
    ident_alias: str,
    output_name: str,
) -> "tuple[str, str | None]":
    """Project one FK's resolved surface off a record-id-producing expression.

    The shared private dispatch the doc's four FK builders replace their
    local `target_key == 'presentation_id'` arm with (sprint contracts § 4):
    `record_id` projects `record_id_expr` verbatim, no extra join;
    `record_index` / `presentation_id` LEFT JOINs the (possibly restricted)
    identity relation keyed on `record_id_expr` and projects the surface
    column — the out-of-set → NULL posture falls out of the LEFT JOIN, no
    CASE logic.

    Args:
        record_id_expr: A qualified SQL expression producing the resolved
            target's record_id (or NULL when unresolved/kind-mismatched).
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `get_fork_path_from_sidecar`.
        resolved_surface: The FK's one resolved surface.
        dim_populations: The destination dim's source population set.
        ident_alias: A unique alias for the identity-relation join.
        output_name: The FK column's output name.

    Returns:
        (select_expr, extra_join_clause_or_None).
    """
    if resolved_surface == "record_id":
        return f'{record_id_expr} AS "{output_name}"', None
    relation_sql = _fk_identity_relation_sql(
        sidecar, fork_path, resolved_surface, dim_populations
    )
    join = (
        f'LEFT JOIN ({relation_sql}) AS "{ident_alias}"'
        f' ON "{ident_alias}"."record_id" = {record_id_expr}'
    )
    select_expr = f'"{ident_alias}"."{resolved_surface}" AS "{output_name}"'
    return select_expr, join


# ---------------------------------------------------------------------------
# FK target validation
# ---------------------------------------------------------------------------


def check_fk_target_is_dim(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    config: "DimensionalConfig",
) -> "TableDecl":
    """Enforce FkTargetIsDim: fk.to names a declared role='dim' table.

    Args:
        col_decl: The column declaration with fk set.
        table_decl: The output table declaration (for error messages).
        config: The dimensional config (to search declared tables).

    Returns:
        The target TableDecl.

    Raises:
        ExportError: fk.to does not name a declared dimension.
    """
    assert col_decl.fk is not None
    to_name = col_decl.fk.to
    for t in config.tables:
        if t.name == to_name and t.role == "dim":
            return t
    raise ExportError(
        f"FK target '{to_name}' on '{table_decl.name}.{col_decl.name}'"
        " is not a declared dimension"
    )


# ---------------------------------------------------------------------------
# Reference FK SQL builder
# ---------------------------------------------------------------------------


def build_reference_fk_expr(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    anchor_kind: str,
    anchor_alias: str,
    target_kind: str,
    sidecar: "Sidecar",
    source_grain: str,
    resolved_surface: "KeySurface",
    dim_populations: DimSourcePopulations,
) -> tuple[str, list[str]]:
    """Build the SELECT expression + JOIN clauses for a via:reference FK column.

    Composes the reference-path derivation: resolves the hop chain, calls
    build_reference_path_sql to produce a subquery resolving to record_id,
    and LEFT JOINs it on record_id. Fan-out-free: the derivation guarantees
    at most one resolved per anchor record_id. The resolved record_id is then
    projected through `_dispatch_fk_surface` — verbatim under `record_id`, or
    LEFT JOINed against the (possibly population-restricted) identity
    relation under `record_index` / `presentation_id`.

    For any non-records grain (history_point, history_interval, membership) the
    reference chain starts from records__<anchor_kind> joined on record_id; only
    a records grain carries the anchor's prop__ columns on the grain row. Rows
    whose record_id has no matching records row emit NULL — the documented
    non-records-grain FK limitation. Every emitted JOIN alias is namespaced by
    the output column name, so multiple FK columns on one table never collide.

    Args:
        col_decl: The FK column declaration (fk.via == 'reference').
        table_decl: The output table declaration (for error messages).
        anchor_kind: The anchor grain's record kind.
        anchor_alias: SQL alias for the grain's base table (e.g. "_grain").
        target_kind: The dim's source kind (the FK lands here).
        sidecar: The open emit's sidecar.
        source_grain: The grain type ('records', 'history_point',
            'history_interval', 'membership'); only 'records' skips the
            preamble records JOIN.
        resolved_surface: The FK's one resolved surface (inherited or the
            explicit `target_key`).
        dim_populations: The destination dim's source population set.

    Returns:
        (select_expr, join_clauses) — insert join_clauses before the ORDER BY,
        use select_expr in the SELECT list.

    Raises:
        ExportError: ReferencePathResolvable — no path, or ambiguous path.
    """
    assert col_decl.fk is not None

    ref_map = _collect_reference_columns(sidecar)
    path_hint = col_decl.fk.path
    context_label = f"{table_decl.name}.{col_decl.name}"

    if path_hint is not None:
        hops = _path_hint_to_cols(path_hint, anchor_kind, sidecar, context_label)
    else:
        paths = _find_all_reference_paths(anchor_kind, target_kind, ref_map)
        if not paths:
            raise ExportError(
                f"no reference path from '{anchor_kind}' to '{target_kind}'"
                f" for '{context_label}'"
            )
        if len(paths) > 1:
            raise ExportError(
                f"ambiguous reference path from '{anchor_kind}' to '{target_kind}'"
                f" for '{context_label}';"
                " supply `path` (ordered prop__ columns)"
            )
        hops = paths[0]

    alias_ns = f"_fk_{col_decl.name}"
    fork_path = get_fork_path_from_sidecar(sidecar)

    deriv_sql = build_reference_path_sql(
        sidecar=sidecar,
        fork_path=fork_path,
        anchor_kind=anchor_kind,
        hop_columns=hops,
        terminal_projection="record_id",
    )
    deriv_alias = f"{alias_ns}_rp"

    join_clauses = [
        f'LEFT JOIN ({deriv_sql}) AS "{deriv_alias}"'
        f' ON "{deriv_alias}"."record_id" = "{anchor_alias}"."record_id"'
    ]
    select_expr, extra_join = _dispatch_fk_surface(
        f'"{deriv_alias}"."resolved"',
        sidecar,
        fork_path,
        resolved_surface,
        dim_populations,
        f"{alias_ns}_ident",
        col_decl.name,
    )
    if extra_join is not None:
        join_clauses.append(extra_join)
    return select_expr, join_clauses


# ---------------------------------------------------------------------------
# Membership FK SQL builder
# ---------------------------------------------------------------------------


def _find_membership_table(
    anchor_kind: str,
    property_hint: str | None,
    sidecar: "Sidecar",
    table_decl: "TableDecl",
    col_decl: "ColumnDecl",
) -> "tuple[str, str]":
    """Locate the membership__<anchor_kind>__<property> table for an FK.

    When property_hint is given, use it directly; otherwise infer from the
    single collection-struct property owned by anchor_kind.

    Args:
        anchor_kind: The anchor grain's record kind.
        property_hint: FK's property field, or None when to be inferred.
        sidecar: The open emit's sidecar.
        table_decl: The output table declaration (for error messages).
        col_decl: The FK column declaration (for error messages).

    Returns:
        (table_name, property_name) — resolved membership table name and property.

    Raises:
        ExportError: MembershipEdgeResolvable — the table cannot be resolved uniquely.
    """
    prefix = f"membership__{anchor_kind}__"
    matching: list[str] = []
    for t in sidecar.tables():
        if t.name.startswith(prefix):
            matching.append(t.name)

    if property_hint is not None:
        expected = f"{prefix}{property_hint}"
        if expected not in matching:
            raise ExportError(
                f"membership FK '{table_decl.name}.{col_decl.name}' is unresolvable:"
                f" table '{expected}' not found in emit"
            )
        return expected, property_hint

    if not matching:
        raise ExportError(
            f"membership FK '{table_decl.name}.{col_decl.name}' is unresolvable:"
            f" no membership table for kind '{anchor_kind}' found in emit"
        )
    if len(matching) > 1:
        raise ExportError(
            f"membership FK '{table_decl.name}.{col_decl.name}' is unresolvable:"
            f" kind '{anchor_kind}' owns multiple collection-struct properties"
            f" {[t[len(prefix) :] for t in matching]};"
            " supply 'property' to disambiguate"
        )
    prop_name = matching[0][len(prefix) :]
    return matching[0], prop_name


def _find_member_field(
    mem_table_name: str,
    member_field_hint: str | None,
    sidecar: "Sidecar",
    table_decl: "TableDecl",
    col_decl: "ColumnDecl",
) -> str:
    """Resolve the member_field (the reference field) on the membership table.

    When member_field_hint is given, verify it exists; otherwise infer from
    the single member__<f>__id column on the table.

    Args:
        mem_table_name: The resolved membership table name.
        member_field_hint: The FK's member_field, or None when to be inferred.
        sidecar: The open emit's sidecar.
        table_decl: The output table declaration (for error messages).
        col_decl: The FK column declaration (for error messages).

    Returns:
        The member field name (the <f> in member__<f>__id).

    Raises:
        ExportError: MembershipEdgeResolvable — field absent or ambiguous.
    """
    try:
        cols = sidecar.columns(mem_table_name)
    except Exception:
        raise ExportError(
            f"membership FK '{table_decl.name}.{col_decl.name}' is unresolvable:"
            f" cannot read columns for '{mem_table_name}'"
        )

    ref_fields: list[str] = []
    for c in cols:
        if c.name.startswith("member__") and c.name.endswith("__id"):
            # member__<f>__id -> <f>
            inner = c.name[len("member__") : -len("__id")]
            ref_fields.append(inner)

    if member_field_hint is not None:
        if member_field_hint not in ref_fields:
            raise ExportError(
                f"membership FK '{table_decl.name}.{col_decl.name}' is unresolvable:"
                f" member_field '{member_field_hint}' not found on '{mem_table_name}'"
            )
        return member_field_hint

    if not ref_fields:
        raise ExportError(
            f"membership FK '{table_decl.name}.{col_decl.name}' is unresolvable:"
            f" no member__<f>__id column found on '{mem_table_name}'"
        )
    if len(ref_fields) > 1:
        raise ExportError(
            f"membership FK '{table_decl.name}.{col_decl.name}' is unresolvable:"
            f" multiple reference fields {ref_fields} on '{mem_table_name}';"
            " supply 'member_field' to disambiguate"
        )
    return ref_fields[0]


def _check_where_columns_exist(
    where: dict[str, str | list[str]],
    mem_table_name: str,
    sidecar: "Sidecar",
    table_decl: "TableDecl",
    col_decl: "ColumnDecl",
) -> None:
    """Verify that all where predicate columns are elem__ columns on the table.

    Args:
        where: The FK's where dict (col_name -> value).
        mem_table_name: The resolved membership table name.
        sidecar: The open emit's sidecar.
        table_decl: The output table declaration (for error messages).
        col_decl: The FK column declaration (for error messages).

    Raises:
        ExportError: MembershipEdgeResolvable — a where column is not an elem__ column.
    """
    try:
        cols = sidecar.columns(mem_table_name)
    except TableNotFoundError as exc:
        raise ExportError(
            f"membership FK '{table_decl.name}.{col_decl.name}' is unresolvable:"
            f" cannot read columns for '{mem_table_name}'"
        ) from exc
    col_names = {c.name for c in cols}
    for where_col in where:
        if where_col not in col_names or not where_col.startswith("elem__"):
            raise ExportError(
                f"membership FK '{table_decl.name}.{col_decl.name}' is unresolvable:"
                f" where column '{where_col}' is not an elem__ column"
                f" on '{mem_table_name}'"
            )


def build_membership_fk_expr_on_records(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    anchor_kind: str,
    target_kind: str,
    sidecar: "Sidecar",
    resolved_surface: "KeySurface",
    dim_populations: DimSourcePopulations,
) -> tuple[str, list[str]]:
    """Build SELECT expr + JOIN clauses for a via:membership FK (records/history grain).

    Composes the membership-edge derivation: resolves membership table and
    member_field, calls build_membership_edge_sql to produce a subquery
    resolving to the member's record_id, and LEFT JOINs it on record_id. The
    resolved record_id is then projected through `_dispatch_fk_surface`.

    Args:
        col_decl: The FK column declaration (fk.via == 'membership').
        table_decl: The output table declaration (for error messages).
        anchor_kind: The anchor grain's record kind.
        target_kind: The dim's source kind.
        sidecar: The open emit's sidecar.
        resolved_surface: The FK's one resolved surface.
        dim_populations: The destination dim's source population set.

    Returns:
        (select_expr, join_clauses).

    Raises:
        ExportError: MembershipEdgeResolvable.
    """
    assert col_decl.fk is not None
    fk = col_decl.fk
    where = fk.where or {}

    mem_table_name, prop_name = _find_membership_table(
        anchor_kind, fk.property, sidecar, table_decl, col_decl
    )
    member_field = _find_member_field(
        mem_table_name, fk.member_field, sidecar, table_decl, col_decl
    )
    _check_where_columns_exist(where, mem_table_name, sidecar, table_decl, col_decl)

    fork_path = get_fork_path_from_sidecar(sidecar)

    deriv_sql = build_membership_edge_sql(
        sidecar=sidecar,
        fork_path=fork_path,
        owner_kind=anchor_kind,
        property_name=prop_name,
        member_field=member_field,
        member_kind=target_kind,
        where_predicate=dict(where),
    )
    mem_alias = f"_fk_{col_decl.name}_mem"

    join_clauses = [
        f'LEFT JOIN ({deriv_sql}) AS "{mem_alias}"'
        f' ON "{mem_alias}"."record_id" = "_grain"."record_id"'
    ]
    select_expr, extra_join = _dispatch_fk_surface(
        f'"{mem_alias}"."resolved"',
        sidecar,
        fork_path,
        resolved_surface,
        dim_populations,
        f"_fk_{col_decl.name}_ident",
        col_decl.name,
    )
    if extra_join is not None:
        join_clauses.append(extra_join)
    return select_expr, join_clauses


def build_membership_fk_expr_on_membership(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    anchor_kind: str,
    target_kind: str,
    sidecar: "Sidecar",
    resolved_surface: "KeySurface",
    dim_populations: DimSourcePopulations,
) -> tuple[str, list[str]]:
    """Build SELECT expr for a via:membership FK when the grain IS a membership grain.

    The binding is already the grain — member__<member_field>__id, filtered
    by target_kind (a row whose member kind mismatches resolves to NULL), is
    the resolved record_id; projected through `_dispatch_fk_surface`.

    `fk.where` never reaches this builder: there is no separate membership
    relation left to narrow here, so `TableDecl.membership_grain_fk_where_refused`
    rejects the combination at parse time (row narrowing on this grain is
    `source.where`'s job).

    Args:
        col_decl: The FK column declaration (fk.via == 'membership').
        table_decl: The output table declaration (for error messages).
        anchor_kind: The anchor grain's record kind (the membership owner kind).
        target_kind: The dim's source kind.
        sidecar: The open emit's sidecar.
        resolved_surface: The FK's one resolved surface.
        dim_populations: The destination dim's source population set.

    Returns:
        (select_expr, join_clauses) — [] under a `record_id` resolution (the
        grain already carries the value); the identity-relation join
        otherwise.

    Raises:
        ExportError: MembershipEdgeResolvable.
    """
    assert col_decl.fk is not None
    fk = col_decl.fk

    mem_table_name, _prop = _find_membership_table(
        anchor_kind, fk.property, sidecar, table_decl, col_decl
    )
    member_field = _find_member_field(
        mem_table_name, fk.member_field, sidecar, table_decl, col_decl
    )

    id_col = f"member__{member_field}__id"
    kind_col = f"member__{member_field}__kind"
    fork_path = get_fork_path_from_sidecar(sidecar)

    record_id_expr = (
        f'CASE WHEN "_grain"."{kind_col}" = \'{target_kind}\''
        f' THEN "_grain"."{id_col}" ELSE NULL END'
    )
    select_expr, extra_join = _dispatch_fk_surface(
        record_id_expr,
        sidecar,
        fork_path,
        resolved_surface,
        dim_populations,
        f"_fk_{col_decl.name}_ident",
        col_decl.name,
    )
    return select_expr, [extra_join] if extra_join is not None else []


# ---------------------------------------------------------------------------
# Point-in-time membership FK SQL builder
# ---------------------------------------------------------------------------


def build_point_in_time_membership_fk_expr(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    anchor_kind: str,
    target_kind: str,
    sidecar: "Sidecar",
    resolved_surface: "KeySurface",
    dim_populations: DimSourcePopulations,
) -> tuple[str, list[str]]:
    """Build SELECT expr + JOIN clauses for a point-in-time via:membership FK.

    The grain is neither the owner nor the member.  `fk.as_of` names the
    grain column carrying firing time T; `fk.member_path` is the ordered
    reference chain from the grain kind to the MEMBER identity.

    The OWNER is the dim's source kind (`target_kind`).  The membership table
    is `membership__<target_kind>__<property>`.

    The correlated scalar subquery is DETERMINISTIC (ORDER BY joined_sim_time
    DESC, record_id ASC LIMIT 1) — guarantees ≤1 result per grain row, no
    fan-out. Under a non-`record_id` resolved surface, the subquery JOINs the
    (possibly population-restricted) identity relation instead of the target
    kind's records table directly — an owner outside the dim's source
    population set resolves to no join row (NULL), matching the doc's
    out-of-set condition.

    Args:
        col_decl: The FK column declaration (fk.via=='membership', fk.as_of set).
        table_decl: The output table declaration (for error messages).
        anchor_kind: The anchor grain's record kind.
        target_kind: The dim's source kind (the OWNER kind in the membership).
        sidecar: The open emit's sidecar.
        resolved_surface: The FK's one resolved surface.
        dim_populations: The destination dim's source population set.

    Returns:
        (select_expr, join_clauses) where join_clauses are the member-path
        LEFT JOINs and select_expr is the deterministic correlated subquery.

    Raises:
        ExportError: Any structural assumption fails (missing column, table,
            member_path unresolvable, etc.).
    """
    assert col_decl.fk is not None
    fk = col_decl.fk
    assert fk.as_of is not None
    assert fk.member_path is not None

    as_of = fk.as_of
    context_label = f"{table_decl.name}.{col_decl.name}"

    # --- Validate as_of column exists on the grain surface ---
    grain_table = f"records__{anchor_kind}"
    try:
        grain_cols = sidecar.columns(grain_table)
    except Exception:
        raise ExportError(
            f"point-in-time FK '{context_label}':"
            f" kind '{anchor_kind}' has no records table"
        )
    grain_col_names = {c.name for c in grain_cols}
    if as_of not in grain_col_names:
        raise ExportError(
            f"point-in-time FK '{context_label}':"
            f" as_of column '{as_of}' not found on '{grain_table}'"
        )

    # --- Resolve member_path to get member identity expr P ---
    hops = _path_hint_to_cols(fk.member_path, anchor_kind, sidecar, context_label)

    join_clauses: list[str] = []
    prev_alias = "_grain"
    member_kind = anchor_kind
    for i, hop_col in enumerate(hops):
        hop_kind = hop_col.references
        assert hop_kind is not None
        hop_table = f"records__{hop_kind}"
        hop_alias = f"_fk_{col_decl.name}_mp_{i}"
        join_clauses.append(
            f'LEFT JOIN "{hop_table}" AS "{hop_alias}"'
            f' ON "{hop_alias}"."record_id" = "{prev_alias}"."{hop_col.name}"'
        )
        prev_alias = hop_alias
        member_kind = hop_kind

    # The terminal alias's record_id is the member identity expr P
    # member_kind is the kind of the terminal hop (the MEMBER kind)
    member_id_expr = f'"{prev_alias}"."record_id"'

    # --- Resolve membership table (owner = target_kind) ---
    mem_table_name, _prop = _find_membership_table(
        target_kind, fk.property, sidecar, table_decl, col_decl
    )

    # --- Resolve member_field ---
    member_field = _find_member_field(
        mem_table_name, fk.member_field, sidecar, table_decl, col_decl
    )

    mf_id_col = f"member__{member_field}__id"
    mf_kind_col = f"member__{member_field}__kind"

    # --- Render where predicate against the membership table's elem__ columns ---
    # Consistent with the on_records / membership-grain paths: a `where` narrows
    # the membership interval by elem__ column values. Silently dropping it would
    # let a misconfigured FK resolve unfiltered (Principle #7).
    where = fk.where or {}
    extra_where = ""
    if where:
        _check_where_columns_exist(where, mem_table_name, sidecar, table_decl, col_decl)
        mem_col_types = {c.name: c.type for c in sidecar.columns(mem_table_name)}
        for where_col, value in where.items():
            condition = render_predicate_condition(
                where_col, value, mem_col_types[where_col], "h"
            )
            extra_where += f"   AND {condition}\n"

    # INT64 max sentinel for open-interval containment
    _INT64_MAX = 9223372036854775807

    # Determine what to project and whether we need an identity-relation join
    if resolved_surface != "record_id":
        fork_path = get_fork_path_from_sidecar(sidecar)
        relation_sql = _fk_identity_relation_sql(
            sidecar, fork_path, resolved_surface, dim_populations
        )
        rec_join = f'JOIN ({relation_sql}) AS r ON r."record_id" = h."record_id"'
        proj = f'r."{resolved_surface}"'
        inner = (
            f'SELECT {proj} FROM "{mem_table_name}" h\n'
            f"   {rec_join}\n"
            f' WHERE h."{mf_id_col}" = {member_id_expr}\n'
            f"   AND h.\"{mf_kind_col}\" = '{member_kind}'\n"
            f"{extra_where}"
            f'   AND h."joined_sim_time" <= "_grain"."{as_of}"\n'
            f'   AND "_grain"."{as_of}" < COALESCE(h."left_sim_time", {_INT64_MAX})\n'
            f' ORDER BY h."joined_sim_time" DESC, h."record_id" ASC LIMIT 1'
        )
    else:
        proj = 'h."record_id"'
        inner = (
            f'SELECT {proj} FROM "{mem_table_name}" h\n'
            f' WHERE h."{mf_id_col}" = {member_id_expr}\n'
            f"   AND h.\"{mf_kind_col}\" = '{member_kind}'\n"
            f"{extra_where}"
            f'   AND h."joined_sim_time" <= "_grain"."{as_of}"\n'
            f'   AND "_grain"."{as_of}" < COALESCE(h."left_sim_time", {_INT64_MAX})\n'
            f' ORDER BY h."joined_sim_time" DESC, h."record_id" ASC LIMIT 1'
        )

    select_expr = f'({inner}) AS "{col_decl.name}"'
    return select_expr, join_clauses


# ---------------------------------------------------------------------------
# Unified FK expression builder (dispatches on grain + via)
# ---------------------------------------------------------------------------


def build_fk_expr(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    source_grain: str,
    anchor_kind: str,
    target_kind: str,
    sidecar: "Sidecar",
    resolved_surface: "KeySurface",
    dim_populations: DimSourcePopulations,
) -> tuple[str, list[str]]:
    """Build the SELECT expression + JOIN clauses for an fk column.

    Dispatches on fk.via and grain type. Returns a (select_expr, join_clauses)
    pair; caller integrates them into the grain SQL. `resolved_surface` and
    `dim_populations` are resolved once by the caller
    (`resolve_fk_surface` / `resolve_dim_source_populations`, sprint contracts
    § 3) — this function never touches `Election`; the shipped
    `target_key == 'presentation_id'` column-presence check is gone,
    subsumed by the statically-earlier registry-membership check
    (`check_edge_union_safety` under the resolved-surface override).

    Args:
        col_decl: The FK column declaration (exactly one fk set).
        table_decl: The output table declaration (for error messages).
        source_grain: The table's grain type ('records', 'history_point',
            'history_interval', or 'membership').
        anchor_kind: The record kind of the grain's anchor row.
        target_kind: The dim's source kind (from the target TableDecl).
        sidecar: The open emit's sidecar.
        resolved_surface: The FK's one resolved surface (inherited or the
            explicit `target_key`).
        dim_populations: The destination dim's source population set.

    Returns:
        (select_expr, join_clauses).

    Raises:
        ExportError: Any FK validation or pathfind failure.
    """
    assert col_decl.fk is not None
    via = col_decl.fk.via

    if via == "reference":
        return build_reference_fk_expr(
            col_decl=col_decl,
            table_decl=table_decl,
            anchor_kind=anchor_kind,
            anchor_alias="_grain",
            target_kind=target_kind,
            sidecar=sidecar,
            source_grain=source_grain,
            resolved_surface=resolved_surface,
            dim_populations=dim_populations,
        )

    # via == "membership"
    if col_decl.fk.as_of is not None:
        return build_point_in_time_membership_fk_expr(
            col_decl=col_decl,
            table_decl=table_decl,
            anchor_kind=anchor_kind,
            target_kind=target_kind,
            sidecar=sidecar,
            resolved_surface=resolved_surface,
            dim_populations=dim_populations,
        )
    if source_grain == "membership":
        return build_membership_fk_expr_on_membership(
            col_decl=col_decl,
            table_decl=table_decl,
            anchor_kind=anchor_kind,
            target_kind=target_kind,
            sidecar=sidecar,
            resolved_surface=resolved_surface,
            dim_populations=dim_populations,
        )
    return build_membership_fk_expr_on_records(
        col_decl=col_decl,
        table_decl=table_decl,
        anchor_kind=anchor_kind,
        target_kind=target_kind,
        sidecar=sidecar,
        resolved_surface=resolved_surface,
        dim_populations=dim_populations,
    )


# ---------------------------------------------------------------------------
# SliceOnlyColumnRefused over fk-traversed hops
# ---------------------------------------------------------------------------


def _resolve_reference_hops_for_slice_check(
    path_hint: "list[str] | None",
    anchor_kind: str,
    target_kind: str,
    sidecar: "Sidecar",
    context_label: str,
) -> "list[ColumnSpec]":
    """Re-resolve a via:reference hop chain for the slice-only check.

    Mirrors build_reference_fk_expr's resolution (path hint or unique
    pathfind). Called only after build_fk_expr has already resolved and
    built the same fk column's SQL successfully, so resolution here is
    assumed to succeed too — no error branches.

    Args:
        path_hint: The fk's author-hinted path, or None for pathfind.
        anchor_kind: The anchor grain's record kind.
        target_kind: The dim's source kind.
        sidecar: The open emit's sidecar.
        context_label: Human-readable label for _path_hint_to_cols.

    Returns:
        The resolved hop chain.
    """
    if path_hint is not None:
        return _path_hint_to_cols(path_hint, anchor_kind, sidecar, context_label)
    ref_map = _collect_reference_columns(sidecar)
    paths = _find_all_reference_paths(anchor_kind, target_kind, ref_map)
    return paths[0]


def _check_hop_chain_slice_only(
    hops: "list[ColumnSpec]",
    start_kind: str,
    table_decl: "TableDecl",
    col_decl: "ColumnDecl",
    sidecar: "Sidecar",
) -> None:
    """Refuse SliceOnlyColumnRefused over a traversed reference-hop chain.

    Each hop column lives on the current kind's records table; the kind
    advances to the hop's target kind via ColumnSpec.references.

    Args:
        hops: The resolved hop chain (each a ColumnSpec on its owning kind's
            records table).
        start_kind: The kind owning the first hop column.
        table_decl: The output table declaration (for error messages).
        col_decl: The fk column declaration (for error messages).
        sidecar: The open emit's sidecar.

    Raises:
        ExportError: A traversed hop is non-exempt slice_only.
        TemporalClassUnavailableError: Propagated.
    """
    current_kind = start_kind
    for hop_col in hops:
        if is_non_exempt_slice_only(sidecar, current_kind, hop_col.name):
            raise ExportError(
                slice_only_refusal_message(
                    table_decl.name,
                    col_decl.name,
                    "fk hop column",
                    current_kind,
                    hop_col.name,
                )
            )
        hop_kind = hop_col.references
        assert hop_kind is not None
        current_kind = hop_kind


def check_fk_slice_only(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    source_grain: str,
    anchor_kind: str,
    target_kind: str,
    sidecar: "Sidecar",
) -> None:
    """Enforce SliceOnlyColumnRefused over an fk column's traversed hops.

    via: reference — the resolved hop chain (path hint or unique pathfind,
    the same helpers build_reference_fk_expr uses), each hop's kind advanced
    via ColumnSpec.references. via: membership with as_of — the member_path
    hop chain plus the as_of column on records__<anchor_kind>. Plain
    membership fk consults no classed column (member/element columns are
    classless): no-op. Called from validate_table immediately after
    build_fk_expr, so path-resolution failures keep their existing messages.

    Args:
        col_decl: The FK column declaration.
        table_decl: The output table declaration (for error messages).
        source_grain: The table's grain type.
        anchor_kind: The record kind of the grain's anchor row.
        target_kind: The dim's source kind.
        sidecar: The open emit's sidecar.

    Raises:
        ExportError: A traversed hop or the as_of column is non-exempt
            slice_only.
        TemporalClassUnavailableError: Propagated.
    """
    assert col_decl.fk is not None
    fk = col_decl.fk
    context_label = f"{table_decl.name}.{col_decl.name}"

    if fk.via == "reference":
        hops = _resolve_reference_hops_for_slice_check(
            fk.path, anchor_kind, target_kind, sidecar, context_label
        )
        _check_hop_chain_slice_only(hops, anchor_kind, table_decl, col_decl, sidecar)
        return

    # via == "membership"
    if fk.as_of is None:
        return

    assert fk.member_path is not None
    hops = _path_hint_to_cols(fk.member_path, anchor_kind, sidecar, context_label)
    _check_hop_chain_slice_only(hops, anchor_kind, table_decl, col_decl, sidecar)

    if is_non_exempt_slice_only(sidecar, anchor_kind, fk.as_of):
        raise ExportError(
            slice_only_refusal_message(
                table_decl.name, col_decl.name, "as_of column", anchor_kind, fk.as_of
            )
        )
