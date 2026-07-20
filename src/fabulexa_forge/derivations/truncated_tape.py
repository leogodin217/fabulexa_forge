"""The truncated-tape surface: relation builders and sidecar view for T.

Four relation builders (history, membership, records, and the records builder's
ref_index__ re-derivation) and one sidecar view presenting the emit as if its
slice ended at `at_sim_time` (inclusive). Each builder returns a complete
SELECT that replaces its base table inside a mode's full-export compile
(§ Shaped state, docs/architecture/pending/playback.md); totality over
structurally-conformant input holds as for the folds. Unlike the folds' point-
in-time relations (membership_state_at.py, state_at.py), these builders carry
no canonical ORDER BY — a replacing relation's order is imposed by the compile
that reads it — and, except for their declared deviations, they present each
base table's column shape verbatim rather than a reshaped payload.
build_truncated_records_sql shares its tracked-property as-of join with
state_at.py through derivations.properties.build_history_asof_join, so the
windowed-rank pattern never drifts between the two point-in-time
reconstructions.

Layer-direction invariant: imports only the reader, fabulexa_forge.errors, and
stdlib. Never imports exporters.* or config.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.reader.sidecar import ColumnSpec, TableSpec

from fabulexa_forge._sql import _sql_literal, is_recognized_sql_type
from fabulexa_forge.derivations.properties import build_history_asof_join
from fabulexa_forge.errors import ExportError
from fabulexa_forge.reader.sidecar import Sidecar
from fabulexa_forge.reader.slice_only import (
    is_exempt_discriminator,
    is_non_exempt_slice_only,
)

#: records__<kind> identity columns projected verbatim by
#: build_truncated_records_sql — record_index included (slice-stable by
#: contract); ref_index__<name> is deliberately excluded, since it is
#: re-derived rather than read verbatim.
_VERBATIM_IDENTITY_COLUMNS: tuple[str, ...] = ("fork_path", "record_id", "record_index")


def build_truncated_history_sql(
    fork_path: str,
    at_sim_time: int,
) -> str:
    """The history table truncated at T.

    Rows with sim_time <= at_sim_time, filtered to fork_path; column shape
    verbatim (history is a fixed table).

    Args:
        fork_path: The sole branch, from require_single_branch.
        at_sim_time: The inclusive truncation position T (ns); >= 0.

    Returns:
        A complete SELECT with the history table's column shape.

    Raises:
        Nothing — history is a fixed table; there is no resolvability to
        check.
    """
    fp_lit = _sql_literal(fork_path)
    return (
        'SELECT * FROM "history"'
        f' WHERE "fork_path" = {fp_lit} AND "sim_time" <= {at_sim_time}'
    )


def build_truncated_membership_sql(
    sidecar: Sidecar,
    fork_path: str,
    owner_kind: str,
    property_name: str,
    at_sim_time: int,
) -> str:
    """membership__<owner_kind>__<property_name> truncated at T.

    Intervals with joined_sim_time <= at_sim_time, filtered to fork_path;
    left_sim_time masked NULL when > at_sim_time (an interval still open at
    T, exactly as a slice-at-T emit renders it); every other column verbatim.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        owner_kind: The membership table's owner kind.
        property_name: The membership table's collection property.
        at_sim_time: The inclusive truncation position T (ns); >= 0.

    Returns:
        A complete SELECT with the membership table's column shape.

    Raises:
        TableNotFoundError: No membership__<owner_kind>__<property_name>
            table is in the sidecar.
    """
    table_name = f"membership__{owner_kind}__{property_name}"
    cols = sidecar.columns(table_name)  # raises TableNotFoundError if absent
    fp_lit = _sql_literal(fork_path)

    select_parts = [
        (
            f'CASE WHEN "left_sim_time" > {at_sim_time} THEN NULL'
            f' ELSE "left_sim_time" END AS "left_sim_time"'
            if col.name == "left_sim_time"
            else f'"{col.name}"'
        )
        for col in cols
    ]
    select_sql = ", ".join(select_parts)

    return (
        f'SELECT {select_sql} FROM "{table_name}"'
        f' WHERE "fork_path" = {fp_lit} AND "joined_sim_time" <= {at_sim_time}'
    )


def _render_active_expr(at_sim_time: int) -> str:
    """The `active` column horizon-rendered at the inclusive truncation T.

    Args:
        at_sim_time: The inclusive truncation position T (ns).

    Returns:
        A `CASE ... AS "active"` SELECT expression: FALSE iff the record's
        physical deactivated_at is set and at-or-before T, TRUE otherwise —
        the physical `active` column itself is never read (mirrors
        state_at.py's horizon rendering, at an inclusive bound).
    """
    return (
        'CASE WHEN "_rec"."deactivated_at" IS NOT NULL'
        f' AND "_rec"."deactivated_at" <= {at_sim_time}'
        ' THEN FALSE ELSE TRUE END AS "active"'
    )


def _render_deactivated_at_expr(at_sim_time: int) -> str:
    """The `deactivated_at` column horizon-rendered at the inclusive T.

    Args:
        at_sim_time: The inclusive truncation position T (ns).

    Returns:
        A `CASE ... AS "deactivated_at"` SELECT expression: the physical
        value when at-or-before T, NULL otherwise (not yet deactivated as
        of T).
    """
    return (
        'CASE WHEN "_rec"."deactivated_at" IS NOT NULL'
        f' AND "_rec"."deactivated_at" <= {at_sim_time}'
        ' THEN "_rec"."deactivated_at" ELSE NULL END AS "deactivated_at"'
    )


def _build_recorded_trail(
    fork_path: str,
    kind: str,
    at_sim_time: int,
) -> tuple[str, str]:
    """Build the `last_mutation_sim_time` recorded-trail JOIN and expression.

    greatest(created_sim_time, the record's latest tracked-history sim_time
    <= T across every tracked property, deactivated_at when <= T) — the last
    recorded content change at T, never the physical last_mutation_sim_time
    (whose advances need not leave history — a high-water mark only).
    Membership activity is deliberately not a component.

    Args:
        fork_path: The sole branch fork_path.
        kind: The record kind.
        at_sim_time: The inclusive truncation position T (ns).

    Returns:
        A 2-tuple (join_sql, value_expr): the LEFT JOIN supplying the
        record's latest at-or-before-T history sim_time, and the
        GREATEST(...) SELECT expression.
    """
    fp_lit = _sql_literal(fork_path)
    kind_lit = _sql_literal(kind)
    join_sql = (
        f" LEFT JOIN ("
        f'SELECT "record_id", MAX("sim_time") AS "max_sim_time" FROM "history"'
        f' WHERE "fork_path" = {fp_lit} AND "kind" = {kind_lit}'
        f' AND "sim_time" <= {at_sim_time}'
        f' GROUP BY "record_id"'
        f') AS "_trail" ON "_trail"."record_id" = "_rec"."record_id"'
    )
    deactivated_component = (
        'CASE WHEN "_rec"."deactivated_at" IS NOT NULL'
        f' AND "_rec"."deactivated_at" <= {at_sim_time}'
        ' THEN "_rec"."deactivated_at" ELSE NULL END'
    )
    value_expr = (
        'GREATEST("_rec"."created_sim_time", "_trail"."max_sim_time",'
        f" {deactivated_component})"
    )
    return join_sql, value_expr


def _try_cast_expr(value_expr: str, sql_type: str) -> str:
    """TRY_CAST a reconstructed VARCHAR history value to its declared type.

    Gates sql_type through the shared type allow-list
    (`_sql.is_recognized_sql_type` — the same gate `render_typed_literal`
    applies to literal values) before splicing it into CAST syntax, so a
    sidecar-declared type can never carry SQL beyond the CAST position.

    Args:
        value_expr: The VARCHAR-typed SQL expression to cast.
        sql_type: The column's sidecar-declared DuckDB type.

    Returns:
        A `TRY_CAST(<value_expr> AS <sql_type>)` expression — NULL, never an
        error, when value_expr does not parse as sql_type (the codec
        round-trip's totality).

    Raises:
        ExportError: sql_type is not a recognized DuckDB type.
    """
    if not is_recognized_sql_type(sql_type):
        raise ExportError(
            f"build_truncated_records_sql: unrecognized SQL type '{sql_type}'"
            " — no silent VARCHAR fallback"
        )
    return f"TRY_CAST({value_expr} AS {sql_type})"


def _build_ref_index_join(
    fork_path: str,
    target_kind: str,
    prop: str,
    value_expr: str,
    at_sim_time: int,
) -> tuple[str, str]:
    """Build the `ref_index__<prop>` re-derivation JOIN and expression.

    Resolves the reconstructed prop__<prop> value against the *truncated*
    target spine — records__<target_kind> filtered to fork_path and
    created_sim_time <= T inline (the one-consistent-truncated-world rule;
    the cross-read carries its inline truncation predicate rather than
    depending on any external binding) — projecting the matched row's
    record_index. A LEFT JOIN: NULL beside a NULL reference, and NULL beside
    a verbatim non-NULL reference that resolves to no truncated spine row
    (dangling, mispointed, or naming a record created after T).

    Args:
        fork_path: The sole branch fork_path.
        target_kind: The referenced record kind (the sibling prop__<prop>
            column's sidecar `references` value).
        prop: The reference property's bare name (used only to build a
            unique JOIN alias).
        value_expr: The reconstructed prop__<prop> SQL expression (the
            reference's as-of record_id, or NULL).
        at_sim_time: The inclusive truncation position T (ns).

    Returns:
        A 2-tuple (join_sql, ref_expr): the LEFT JOIN clause and the
        `ref_index__<prop>` SELECT expression (the matched row's
        record_index, or NULL).
    """
    fp_lit = _sql_literal(fork_path)
    alias = f"_ref_{prop}"
    join_sql = (
        f' LEFT JOIN "records__{target_kind}" AS "{alias}"'
        f' ON "{alias}"."record_id" = {value_expr}'
        f' AND "{alias}"."fork_path" = {fp_lit}'
        f' AND "{alias}"."created_sim_time" <= {at_sim_time}'
    )
    return join_sql, f'"{alias}"."record_index"'


def build_truncated_records_sql(
    sidecar: Sidecar,
    fork_path: str,
    kind: str,
    at_sim_time: int,
) -> str:
    """records__<kind> reconstructed as of T.

    One row per record with created_sim_time <= at_sim_time, filtered to
    fork_path. Columns are the physical table's shape with the declared
    deviations. Non-exempt slice_only columns are absent — except a
    sub-typed kind's slice_only discriminator prop__<kind>_type, carried
    verbatim (the classification carve-out); the column-list deviation is
    mirrored by build_truncated_sidecar. last_mutation_sim_time is presented
    as the recorded trail — greatest(created_sim_time, the record's latest
    tracked history sim_time <= at_sim_time, deactivated_at when <=
    at_sim_time): the last recorded content change at T, never the physical
    value; membership activity is deliberately not a component. Otherwise:
    identity columns and record_index verbatim; active / deactivated_at
    horizon-rendered; presentation_id verbatim; each prop__<p> of
    temporal_class constant verbatim, of class tracked reconstructed as of T
    and TRY_CAST back to the column's sidecar-declared type (the codec
    round-trip; NULL where a corrupted history value does not parse as the
    declared type — a cast never errors, the totality invariant); each
    ref_index__<name> re-derived from the reconstructed prop__<name> via the
    target kind's truncated spine (the one-consistent-truncated-world rule):
    NULL beside a NULL reference, and NULL beside a verbatim non-NULL
    reference that resolves to no truncated spine row (dangling, mispointed,
    or naming a record created after T).

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind to reconstruct.
        at_sim_time: The inclusive truncation position T (ns); >= 0.

    Returns:
        A complete SELECT with the records table's column shape minus its
        non-exempt slice_only columns, the last_mutation_sim_time column
        presenting the recorded trail.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
        ExportError: A prop__ column declares an unrecognized SQL type.
    """
    table_name = f"records__{kind}"
    cols = sidecar.columns(table_name)  # raises TableNotFoundError if absent
    fp_lit = _sql_literal(fork_path)

    select_parts: list[str] = []
    joins: list[str] = []
    prop_value_exprs: dict[str, str] = {}
    prop_cols: dict[str, "ColumnSpec"] = {}

    for col in cols:
        name = col.name
        if (
            name in _VERBATIM_IDENTITY_COLUMNS
            or name == "presentation_id"
            or name == "created_sim_time"
        ):
            select_parts.append(f'"_rec"."{name}"')
        elif name == "active":
            select_parts.append(_render_active_expr(at_sim_time))
        elif name == "deactivated_at":
            select_parts.append(_render_deactivated_at_expr(at_sim_time))
        elif name == "last_mutation_sim_time":
            join_sql, trail_expr = _build_recorded_trail(fork_path, kind, at_sim_time)
            joins.append(join_sql)
            select_parts.append(f'{trail_expr} AS "last_mutation_sim_time"')
        elif name.startswith("ref_index__"):
            prop = name[len("ref_index__") :]
            target_kind = prop_cols[f"prop__{prop}"].references
            assert target_kind is not None  # C1: a ref_index__ sibling implies one
            join_sql, ref_expr = _build_ref_index_join(
                fork_path, target_kind, prop, prop_value_exprs[prop], at_sim_time
            )
            joins.append(join_sql)
            select_parts.append(f'{ref_expr} AS "{name}"')
        elif name.startswith("prop__"):
            prop = name[len("prop__") :]
            prop_cols[name] = col
            if is_non_exempt_slice_only(sidecar, kind, name):
                # Dropped from the projection, but its value may still be
                # needed to re-derive a ref_index__ sibling.
                prop_value_exprs[prop] = f'"_rec"."{name}"'
                continue
            if is_exempt_discriminator(sidecar, kind, name):
                select_parts.append(f'"_rec"."{name}"')
                prop_value_exprs[prop] = f'"_rec"."{name}"'
                continue
            if sidecar.temporal_class(table_name, name) == "tracked":
                join_sql, history_expr = build_history_asof_join(
                    fork_path, kind, prop, f"_h_{prop}", at_sim_time, inclusive=True
                )
                joins.append(join_sql)
                cast_expr = _try_cast_expr(history_expr, col.type)
                select_parts.append(f'{cast_expr} AS "{name}"')
                prop_value_exprs[prop] = cast_expr
            else:  # constant
                value_expr = f'"_rec"."{name}"'
                select_parts.append(value_expr)
                prop_value_exprs[prop] = value_expr

    select_sql = ", ".join(select_parts)
    joins_sql = "".join(joins)

    return (
        f"SELECT {select_sql}"
        f' FROM "{table_name}" AS "_rec"'
        f"{joins_sql}"
        f' WHERE "_rec"."fork_path" = {fp_lit}'
        f' AND "_rec"."created_sim_time" <= {at_sim_time}'
    )


def _truncate_table_columns(sidecar: Sidecar, table: "TableSpec") -> "TableSpec":
    """Drop a records__<kind> table's non-exempt slice_only columns.

    Every other table entry passes through unchanged. Shared by
    build_truncated_sidecar's per-table fold.

    Args:
        sidecar: The open emit's physical sidecar.
        table: One table entry from sidecar.tables().

    Returns:
        table unchanged, unless it is a records-category table with a
        resolvable kind, in which case a copy with its non-exempt slice_only
        columns dropped.
    """
    if table.category != "records" or table.record_kind is None:
        return table
    kind = table.record_kind
    kept = tuple(
        col
        for col in table.columns
        if not is_non_exempt_slice_only(sidecar, kind, col.name)
    )
    if kept == table.columns:
        return table
    return replace(table, columns=kept)


def build_truncated_sidecar(sidecar: Sidecar) -> Sidecar:
    """The truncated tape's sidecar view.

    Identical to the physical sidecar except that each records__<kind>
    table entry's column list drops every temporal_class slice_only
    column — a sub-typed kind's slice_only discriminator
    prop__<kind>_type excepted (the classification carve-out) — exactly
    the columns build_truncated_records_sql does not project
    (last_mutation_sim_time stays declared: the truncated relation
    presents it as the recorded trail). Every other table entry and every
    other sidecar field is unchanged — the branch's slice bound included,
    which is why no compile path under state may read a slice bound from
    the sidecar (a stated invariant of the compile indirection). Pure and
    T-independent: the dropped column set is a
    function of the declared schema, not of the truncation position.
    Column-list agreement with the relation builders is a stated invariant
    of the surface.

    Args:
        sidecar: The open emit's physical sidecar.

    Returns:
        A Sidecar describing the truncated tape; tier-2 state presents it
        over the already-open connection through the reader's public Emit
        composition (the truncated emit view).
    """
    new_tables = tuple(
        _truncate_table_columns(sidecar, table) for table in sidecar.tables()
    )
    return Sidecar(
        raw=sidecar.raw,
        base_format_version=sidecar.base_format_version,
        branches=sidecar.branches(),
        tables=new_tables,
        runtime=sidecar.runtime(),
        pinned_ids=sidecar.pinned_ids(),
        enum_domains=sidecar.enum_domains(),
        record_roles=sidecar.record_roles(),
    )
