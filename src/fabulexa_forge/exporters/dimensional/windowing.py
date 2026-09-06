"""Horizon windowing for the dimensional exporter.

A window's content is the mode's unchanged full-export compile over the tape
truncated at the window's horizons; this module owns everything that turns
two horizon compiles into one delivered window:

- `window_delivery_class` — the static per-table delivery class: 'snapshot'
  (replace the table with the end-horizon state), 'upsert' (deliver the rows
  of the end-horizon state absent from the start-horizon state, reconciled
  by the declared key), or 'append' (an upsert whose delta never revises an
  earlier window's row — every value channel horizon-invariant).
- `check_window_key_invariant` — WindowKeyMutable: an 'upsert' table's key
  must be a stable row identity (every key column a horizon-invariant
  channel), or delete-by-key silently leaks re-keyed rows.
- `check_window_key_unique` — WindowKeyDuplicate: an 'upsert' table's key
  must be unique in the end-horizon state, or delete-by-key removes a
  sibling row the delta does not restore.
- `check_windowed_reserved_names` — IncrementalReservedName: no author
  table named for the warehouse's bookkeeping tables.
- `compose_window_delta_sql` — the ordered multiset difference of two
  wrapped horizon compiles.

A value channel is *horizon-invariant* when its value on a row cannot differ
between two horizons at both of which the row exists. The reading per column
form is `_channel_variance`; the classifier and the key gate read the same
function so the two cannot drift.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fabulexa_forge.config.models import ColumnDecl, DimensionalConfig, TableDecl
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import quote_identifier
from fabulexa_forge.config.models import (
    scd_window_bound,
    timestamp_render,
)
from fabulexa_forge.derivations.reference_resolution import (
    _collect_reference_columns,
    _find_all_reference_paths,
    _path_hint_to_cols,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.fk import check_fk_target_is_dim
from fabulexa_forge.exporters.dimensional.validation import check_source_table_exists
from fabulexa_forge.exporters.reserved_names import RESERVED_TABLE_NAMES
from fabulexa_forge.reader.errors import TableNotFoundError
from fabulexa_forge.reader.records_columns import (
    REF_INDEX_PREFIX,
    records_structural_column_is_mutable,
)

WindowDelivery = Literal["append", "snapshot", "upsert"]
"""The static per-table window delivery class."""

#: Each grain's raw event-time key: the column a row's existence is
#: prefix-monotone in under truncation, so an ordinal ordered by it never
#: renumbers an earlier row.
_GRAIN_TIME_KEY: dict[str, str] = {
    "records": "created_sim_time",
    "history_point": "sim_time",
    "history_interval": "sim_time",
    "membership": "joined_sim_time",
}

#: Grain-surface columns whose value the truncated tape may change between
#: horizons (an interval end that closes later; a trail that advances).
_VARYING_SURFACE_COLUMNS: frozenset[str] = frozenset({"lead_sim_time", "left_sim_time"})


def _source_variance(
    name: str,
    grain: str,
    sidecar: "Sidecar",
    source_table_name: str,
    versioned: bool = False,
) -> str | None:
    """Why a grain-surface column may vary between horizons, or None if invariant.

    Structural records columns answer through the reader's structural-temporal
    surface; `prop__` columns through the sidecar's `history_tracked` flag —
    except on a versioned (SCD-2) table, where a version row carries its
    version's value and a tracked read is invariant; a `ref_index__` sibling
    follows its `prop__`; interval ends vary; everything else on a history or
    membership surface is verbatim under truncation.
    """
    if name in _VARYING_SURFACE_COLUMNS:
        return f"is the interval end {name}"
    if grain == "records":
        prop_name = name
        if name.startswith(REF_INDEX_PREFIX):
            prop_name = "prop__" + name[len(REF_INDEX_PREFIX) :]
        if prop_name.startswith("prop__"):
            try:
                for col in sidecar.columns(source_table_name):
                    if col.name == prop_name:
                        if col.history_tracked is False or versioned:
                            return None
                        return f"reads tracked property '{prop_name[6:]}'"
            except TableNotFoundError:
                pass
            return f"reads property '{prop_name[6:]}' of unknown constancy"
        try:
            if records_structural_column_is_mutable(name):
                return f"reads {name}, which advances after creation"
        except ValueError:
            return f"reads producer column '{name}' of unknown constancy"
    return None


def _channel_variance(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    config: "DimensionalConfig",
    sidecar: "Sidecar",
    source_table_name: str,
) -> str | None:
    """Why a column's value channel may vary between horizons, or None if invariant."""
    grain = table_decl.source.grain
    source = col_decl.from_ if col_decl.from_ is not None else col_decl.correlation
    derived = col_decl.derived
    if derived is not None:
        if derived.value_map is not None:
            source = derived.value_map.from_
        elif derived.decimal is not None:
            source = derived.decimal.from_
        elif derived.date_parse is not None:
            source = derived.date_parse.from_
        elif derived.json_precision is not None:
            source = derived.json_precision.from_
        elif derived.timestamp is not None:
            source = derived.timestamp.source
        elif derived.scd_window is not None:
            if scd_window_bound(derived.scd_window) == "valid_to":
                return "is the SCD-2 valid_to bound, closed by the next version"
            return None
        elif derived.elapsed is not None:
            return "derived: elapsed — the counterpart row may land later"
        elif derived.ordinal is not None:
            return _ordinal_variance(
                derived.ordinal.partition_by,
                derived.ordinal.order_by,
                table_decl,
                config,
                sidecar,
                source_table_name,
            )
    if source is not None:
        return _source_variance(
            source, grain, sidecar, source_table_name, table_decl.scd == "type2"
        )
    if col_decl.fk is not None:
        if col_decl.fk.via == "membership":
            if grain == "membership" and col_decl.fk.as_of is None:
                return None
            return "fk via: membership — the binding may join or resolve later"
        return _reference_fk_variance(col_decl, table_decl, config, sidecar)
    # lookup (constant reads by LookupColumnSafety) and null
    return None


def _ordinal_variance(
    partition_by: str,
    order_by: str,
    table_decl: "TableDecl",
    config: "DimensionalConfig",
    sidecar: "Sidecar",
    source_table_name: str,
) -> str | None:
    """An ordinal is invariant iff ordered by the grain's raw time key under a
    window-monotone rendering and partitioned by an invariant sibling."""
    siblings = {c.name: c for c in table_decl.columns}
    order_col = siblings[order_by]
    raw_key = _GRAIN_TIME_KEY[table_decl.source.grain]
    if table_decl.scd == "type2":
        return "derived: ordinal on an SCD-2 dim"
    ordered_by_key = order_col.from_ == raw_key or (
        order_col.derived is not None
        and order_col.derived.timestamp is not None
        and order_col.derived.timestamp.source == raw_key
        and timestamp_render(order_col.derived.timestamp) != "time"
    )
    if not ordered_by_key:
        return f"derived: ordinal ordered by '{order_by}', not the grain's {raw_key}"
    partition_variance = _channel_variance(
        siblings[partition_by], table_decl, config, sidecar, source_table_name
    )
    if partition_variance is not None:
        return (
            f"derived: ordinal partitioned by '{partition_by}',"
            f" which {partition_variance}"
        )
    return None


def _reference_fk_variance(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    config: "DimensionalConfig",
    sidecar: "Sidecar",
) -> str | None:
    """A reference fk is invariant iff every hop column is history_tracked: false."""
    assert col_decl.fk is not None
    if not sidecar.history_tracked_available():
        return "fk via: reference over an emit that does not declare history_tracked"
    anchor_kind = table_decl.source.kind
    if col_decl.fk.path is not None:
        hops = _path_hint_to_cols(
            col_decl.fk.path, anchor_kind, sidecar, f"{table_decl.name}.{col_decl.name}"
        )
    else:
        target_kind = check_fk_target_is_dim(col_decl, table_decl, config).source.kind
        paths = _find_all_reference_paths(
            anchor_kind, target_kind, _collect_reference_columns(sidecar)
        )
        hops = paths[0] if paths else []
    for hop in hops:
        if hop.history_tracked is not False:
            return f"fk hop '{hop.name}' is a tracked reference"
    return None


def _filter_reads_mutable_column(
    table_decl: "TableDecl", sidecar: "Sidecar", source_table_name: str
) -> bool:
    """Whether the table's records `filter` reads a column that can change."""
    if not table_decl.source.filter:
        return False
    return any(
        _source_variance(name, "records", sidecar, source_table_name) is not None
        for name in table_decl.source.filter
    )


def window_delivery_class(
    table_decl: "TableDecl",
    config: "DimensionalConfig",
    sidecar: "Sidecar",
) -> WindowDelivery:
    """The table's static delivery class — a pure function of declaration + sidecar.

    Type-1 dims and tables whose `filter` reads a mutable column are
    'snapshot' (their row set can shrink); a table every one of whose value
    channels is horizon-invariant is 'append'; everything else is 'upsert'.
    Never raises on a declaration the always-on rules admit.

    Args:
        table_decl: The output table declaration.
        config: The dimensional config (fk resolution).
        sidecar: The open emit's sidecar.

    Returns:
        'snapshot', 'upsert', or 'append'.
    """
    source_table_name = check_source_table_exists(table_decl.source, sidecar)
    if table_decl.role == "dim" and table_decl.scd == "type1":
        return "snapshot"
    if _filter_reads_mutable_column(table_decl, sidecar, source_table_name):
        return "snapshot"
    if all(
        _channel_variance(col, table_decl, config, sidecar, source_table_name) is None
        for col in table_decl.columns
    ):
        return "append"
    return "upsert"


def check_window_key_invariant(
    table_decl: "TableDecl",
    config: "DimensionalConfig",
    sidecar: "Sidecar",
) -> None:
    """Enforce WindowKeyMutable on an 'upsert' table: its key is a stable row identity.

    Args:
        table_decl: The output table declaration (its key and columns).
        config: The dimensional config (fk resolution).
        sidecar: The open emit's sidecar.

    Raises:
        ExportError: A key column's value can change between windows; the
            message names the table, the column, and the varying source.
    """
    if window_delivery_class(table_decl, config, sidecar) != "upsert":
        return
    source_table_name = check_source_table_exists(table_decl.source, sidecar)
    columns = {c.name: c for c in table_decl.columns}
    for key_col in table_decl.key:
        variance = _channel_variance(
            columns[key_col], table_decl, config, sidecar, source_table_name
        )
        if variance is not None:
            raise ExportError(
                f"table '{table_decl.name}' key column '{key_col}': its value can"
                f" change between windows ({variance}); an upsert-delivered table"
                " reconciles by key, so key on columns that identify the row for"
                " the whole run"
            )


def check_windowed_reserved_names(table_decl: "TableDecl") -> None:
    """Enforce IncrementalReservedName: no author table named for a bookkeeping table.

    Raises:
        ExportError: The table is named `_export_meta` / `_export_windows`.
    """
    if table_decl.name in RESERVED_TABLE_NAMES:
        raise ExportError(
            f"table '{table_decl.name}': name '{table_decl.name}' is reserved under"
            " incremental export"
        )


def compose_window_delta_sql(
    end_sql: str,
    start_sql: str,
    output_columns: "Sequence[str]",
) -> str:
    """The delta of one table between two wrapped horizon compiles.

    `end EXCEPT ALL start` — the multiset of rows present at the end horizon
    and not at the start horizon, under distinct semantics (NULL equals
    NULL) — ordered by every output column in declared order, a total order
    over distinct rows that needs no internal column. Each input is already
    wrapped by its own name-shadowing CTE block and is treated as an opaque
    subquery; sibling subqueries' CTE scopes do not interact.

    Args:
        end_sql: The end-horizon compiled, wrapped query.
        start_sql: The start-horizon compiled, wrapped query.
        output_columns: The table's output column names in declared order.

    Returns:
        A complete, deterministic SELECT.
    """
    cols = ", ".join(quote_identifier(c) for c in output_columns)
    return (
        f"SELECT {cols} FROM ("
        f"SELECT {cols} FROM ({end_sql}) AS _end"
        f" EXCEPT ALL "
        f"SELECT {cols} FROM ({start_sql}) AS _start"
        f") AS _delta ORDER BY {cols}"
    )


def check_window_key_unique(
    emit: "Emit",
    table_decl: "TableDecl",
    end_sql: str,
) -> None:
    """Enforce WindowKeyDuplicate: the key is unique in the end-horizon relation.

    Counts key values occurring more than once under distinct semantics
    (NULL equals NULL), agreeing with the writer's IS NOT DISTINCT FROM
    delete. Executed at compile, before any write.

    Args:
        emit: The open physical emit.
        table_decl: The output table declaration (its key).
        end_sql: The end-horizon compiled, wrapped query.

    Raises:
        ExportError: The key is not unique in the relation.
    """
    key = ", ".join(quote_identifier(c) for c in table_decl.key)
    (row,) = emit.query(
        f"SELECT count(*) FROM (SELECT {key} FROM ({end_sql}) AS _end"
        f" GROUP BY {key} HAVING count(*) > 1) AS _dups",
        (),
    )
    duplicates = row[0]
    assert isinstance(duplicates, int)
    if duplicates > 0:
        raise ExportError(
            f"table '{table_decl.name}': key {list(table_decl.key)} is not unique at"
            f" the window horizon ({duplicates} duplicated key values); an"
            " upsert-delivered table reconciles by key, so declare a key that"
            " identifies one row"
        )
