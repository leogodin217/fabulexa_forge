"""Reference-resolution derivations: reference-path and membership-edge.

Provides two derivation SQL builders plus the shared path-resolution helpers
that the FK and lookup modes (and their validation rules) all share.

  - build_reference_path_sql — fan-out-free: every hop is keyed on record_id;
    at most one resolved value per anchor record_id.
  - build_membership_edge_sql — not fan-out-free; cardinality is the author's
    responsibility via the where predicate.

Path-resolution helpers (_collect_reference_columns, _find_all_reference_paths,
_path_hint_to_cols) previously lived in fk.py.  They moved here so that
validation rules and the derivation share exactly one implementation — the
"resolvable?" answer is the same function in both call sites.

Layer-direction invariant: imports only the reader, fabulexa_export.errors,
and stdlib. Never imports exporters.* or config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_export.reader.sidecar import ColumnSpec, Sidecar

from fabulexa_export.errors import ExportError

# ---------------------------------------------------------------------------
# Private literal-rendering helper (mirrors exporters.dimensional.columns so
# this module stays below the exporters layer — no cross-layer import needed).
# ---------------------------------------------------------------------------

_INTEGER_TYPES = {
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "UHUGEINT",
}
_FLOAT_TYPES = {"DOUBLE", "FLOAT", "REAL"}


def _render_typed_literal(value: str, sql_type: str) -> str:
    """Render a scalar value as a SQL literal typed to sql_type.

    Byte-identical to exporters.dimensional.columns.render_typed_literal.
    Mirrored here so this module never imports from exporters.*.

    VARCHAR (or VARCHAR( prefix) → single-quoted with '' escaping.
    Integer / float / DECIMAL / BOOLEAN families → CAST('<escaped>' AS <type>).
    Unknown types → raise ExportError.
    """
    escaped = value.replace("'", "''")
    upper = sql_type.upper()

    if upper == "VARCHAR" or upper.startswith("VARCHAR("):
        return f"'{escaped}'"

    if upper in _INTEGER_TYPES:
        return f"CAST('{escaped}' AS {sql_type})"

    if upper in _FLOAT_TYPES:
        return f"CAST('{escaped}' AS {sql_type})"

    if upper == "BOOLEAN":
        return f"CAST('{escaped}' AS {sql_type})"

    if upper.startswith("DECIMAL(") or upper.startswith("NUMERIC("):
        return f"CAST('{escaped}' AS {sql_type})"

    raise ExportError(
        f"render_typed_literal: unrecognized SQL type '{sql_type}'"
        " — no silent VARCHAR fallback"
    )


#: The two fixed columns every reference-path derivation SELECT produces.
REFERENCE_RESOLUTION_COLUMNS: tuple[str, ...] = ("record_id", "resolved")


# ---------------------------------------------------------------------------
# Sidecar helpers
# ---------------------------------------------------------------------------


def get_fork_path_from_sidecar(sidecar: "Sidecar") -> str:
    """Extract the sole fork_path from the sidecar's branches list.

    Args:
        sidecar: The open emit's sidecar.

    Returns:
        The fork_path string of the sole branch.

    Raises:
        ExportError: The sidecar has no branches or multiple branches.
    """
    branches = sidecar.branches()
    if not branches:
        raise ExportError("sidecar has no branches; expected exactly one (trunk-only)")
    if len(branches) > 1:
        raise ExportError(
            f"sidecar has {len(branches)} branches;"
            " only single-branch emits are supported"
        )
    return branches[0].fork_path


# ---------------------------------------------------------------------------
# Shared path-resolution helpers (relocated from fk.py)
# ---------------------------------------------------------------------------


def _collect_reference_columns(
    sidecar: "Sidecar",
) -> dict[str, list["ColumnSpec"]]:
    """Collect all prop__ columns annotated with a references kind per record kind.

    Returns:
        Mapping of source_kind -> list[ColumnSpec] where ColumnSpec.references is set.
    """
    by_kind: dict[str, list["ColumnSpec"]] = {}
    for table in sidecar.tables():
        if table.category != "records" or table.record_kind is None:
            continue
        kind = table.record_kind
        ref_cols: list[ColumnSpec] = []
        for col in table.columns:
            if col.references is not None and col.name.startswith("prop__"):
                ref_cols.append(col)
        if ref_cols:
            by_kind[kind] = ref_cols
    return by_kind


def _find_all_reference_paths(
    from_kind: str,
    to_kind: str,
    ref_map: dict[str, list["ColumnSpec"]],
) -> list[list["ColumnSpec"]]:
    """Find all chains of reference prop__ columns from from_kind to to_kind.

    Uses BFS to find all non-cyclic paths.

    Args:
        from_kind: The anchor record kind.
        to_kind: The target dim's source kind.
        ref_map: Mapping from kind to its reference ColumnSpecs.

    Returns:
        List of paths; each path is an ordered list of ColumnSpec (one per hop).
    """
    if from_kind == to_kind:
        return [[]]  # trivial zero-hop path — same kind, no joins needed

    # BFS: queue of (current_kind, path_so_far, visited_kinds)
    queue: list[tuple[str, list["ColumnSpec"], set[str]]] = [
        (from_kind, [], {from_kind})
    ]
    found: list[list["ColumnSpec"]] = []

    while queue:
        current_kind, path, visited = queue.pop(0)
        for col in ref_map.get(current_kind, []):
            next_kind = col.references
            assert next_kind is not None
            if next_kind in visited:
                continue
            new_path = path + [col]
            if next_kind == to_kind:
                found.append(new_path)
            else:
                queue.append((next_kind, new_path, visited | {next_kind}))

    return found


def _path_hint_to_cols(
    path_hint: list[str],
    from_kind: str,
    sidecar: "Sidecar",
    context_label: str,
) -> list["ColumnSpec"]:
    """Resolve a path hint (ordered prop__ column names) to ColumnSpec hops.

    Each entry in path_hint must be a prop__ column on the current hop's kind
    that carries a references annotation. The hop chain must be non-cyclic.

    Args:
        path_hint: Ordered prop__ column names (one per hop).
        from_kind: The anchor record kind.
        sidecar: The open emit's sidecar.
        context_label: A human-readable label for error messages (e.g.
            'table_name.col_name').

    Returns:
        Ordered list of ColumnSpec objects for the path.

    Raises:
        ExportError: A hop column is not a references column on its kind.
    """
    result: list[ColumnSpec] = []
    current_kind = from_kind

    for hop_name in path_hint:
        table_name = f"records__{current_kind}"
        try:
            cols = sidecar.columns(table_name)
        except Exception:
            raise ExportError(
                f"no reference path for '{context_label}':"
                f" kind '{current_kind}' has no records table"
            )

        col_spec: ColumnSpec | None = None
        for c in cols:
            if c.name == hop_name:
                col_spec = c
                break

        if col_spec is None or col_spec.references is None:
            raise ExportError(
                f"path column '{hop_name}' is not a references column"
                f" on kind '{current_kind}'"
                f" (used in '{context_label}')"
            )

        result.append(col_spec)
        current_kind = col_spec.references

    return result


# ---------------------------------------------------------------------------
# Reference-path SQL builder
# ---------------------------------------------------------------------------


def build_reference_path_sql(
    sidecar: "Sidecar",
    fork_path: str,
    anchor_kind: str,
    hop_columns: list["ColumnSpec"],
    terminal_projection: str,
) -> str:
    """Build the reference-path derivation SELECT.

    Produces REFERENCE_RESOLUTION_COLUMNS (record_id, resolved) from a
    chain of LEFT JOINs through records tables following the hop_columns.
    Fan-out-free: every hop is keyed on record_id, so at most one resolved
    value per anchor record_id. Unresolvable anchors produce NULL for resolved.

    The zero-hop self case (empty hop_columns) projects the anchor record's own
    record_id or prop__<property> directly — no JOIN.

    Args:
        sidecar: The open emit's sidecar (for fork_path filtering).
        fork_path: The sole branch, from require_single_branch.
        anchor_kind: The anchor record kind (root of the hop chain).
        hop_columns: Ordered ColumnSpec hops (each must have .references set);
            empty for the zero-hop self case.
        terminal_projection: Either 'record_id' (FK) or 'prop__<property>'
            (lookup) on the terminal table.

    Returns:
        A complete SELECT producing (record_id, resolved) ordered by record_id.

    Raises:
        ExportError: terminal_projection is neither 'record_id' nor a
            'prop__' column name (defensive; callers pre-validate).
    """
    if terminal_projection != "record_id" and not terminal_projection.startswith(
        "prop__"
    ):
        raise ExportError(
            f"terminal_projection must be 'record_id' or 'prop__<property>';"
            f" got '{terminal_projection}'"
        )

    anchor_table = f"records__{anchor_kind}"
    escaped_fp = fork_path.replace("'", "''")

    if not hop_columns:
        # Zero-hop: project directly from the anchor records table
        return (
            f'SELECT "record_id", "{terminal_projection}" AS "resolved"'
            f' FROM "{anchor_table}"'
            f" WHERE \"fork_path\" = '{escaped_fp}'"
        )

    # Multi-hop: chain LEFT JOINs through records tables
    join_parts: list[str] = []
    prev_alias = "_rp_anchor"
    for i, hop_col in enumerate(hop_columns):
        hop_kind = hop_col.references
        assert hop_kind is not None
        hop_alias = f"_rp_hop_{i}"
        join_parts.append(
            f'LEFT JOIN "records__{hop_kind}" AS "{hop_alias}"'
            f' ON "{hop_alias}"."record_id" = "{prev_alias}"."{hop_col.name}"'
        )
        prev_alias = hop_alias

    joins = " ".join(join_parts)
    terminal_alias = prev_alias

    return (
        f'SELECT "_rp_anchor"."record_id",'
        f' "{terminal_alias}"."{terminal_projection}" AS "resolved"'
        f' FROM "{anchor_table}" AS "_rp_anchor"'
        f" {joins}"
        f' WHERE "_rp_anchor"."fork_path" = \'{escaped_fp}\''
    )


# ---------------------------------------------------------------------------
# Membership-edge SQL builder
# ---------------------------------------------------------------------------


def build_membership_edge_sql(
    sidecar: "Sidecar",
    fork_path: str,
    owner_kind: str,
    property_name: str,
    member_field: str,
    member_kind: str,
    where_predicate: dict[str, str],
) -> str:
    """Build the membership-edge derivation SELECT.

    Reproduces today's LEFT JOIN over the membership table, narrowing by
    member__<field>__kind = member_kind (the interpretive act that keeps
    this out of the faithful membership relation). Projects record_id (owner)
    and member__<field>__id as resolved. Not fan-out-free: cardinality is the
    author's responsibility via where_predicate.

    Args:
        sidecar: The open emit's sidecar (column types for literal rendering).
        fork_path: The sole branch, from require_single_branch.
        owner_kind: The membership owner's record kind.
        property_name: The collection-struct property naming the membership table.
        member_field: The member field name (the <f> in member__<f>__id).
        member_kind: The target member record kind to narrow on.
        where_predicate: elem__ column -> required value; empty for no extra filter.

    Returns:
        A complete SELECT producing (record_id, resolved) — record_id is the owner,
        resolved is member__<field>__id.

    Raises:
        ExportError: member_field produces a column not found on the membership table.
    """
    from fabulexa_export.reader.errors import TableNotFoundError

    mem_table = f"membership__{owner_kind}__{property_name}"
    id_col = f"member__{member_field}__id"
    kind_col = f"member__{member_field}__kind"

    # Verify member_field columns exist
    try:
        cols = sidecar.columns(mem_table)
    except TableNotFoundError:
        raise ExportError(
            f"membership-edge derivation: table '{mem_table}' not found in emit"
        )

    col_names = {c.name for c in cols}
    if id_col not in col_names:
        raise ExportError(
            f"membership-edge derivation: '{id_col}' not found on '{mem_table}'"
        )

    col_types: dict[str, str] = {c.name: c.type for c in cols}
    escaped_fp = fork_path.replace("'", "''")

    conditions: list[str] = [
        f"\"fork_path\" = '{escaped_fp}'",
        f"\"{kind_col}\" = '{member_kind}'",
    ]
    for col_name, value in where_predicate.items():
        if col_name not in col_types:
            raise ExportError(
                f"membership-edge derivation: where_predicate column '{col_name}'"
                f" not found on '{mem_table}'"
            )
        sql_type = col_types[col_name]
        literal = _render_typed_literal(value, sql_type)
        conditions.append(f'"{col_name}" = {literal}')

    where = " AND ".join(conditions)
    return (
        f'SELECT "record_id", "{id_col}" AS "resolved" FROM "{mem_table}" WHERE {where}'
    )
