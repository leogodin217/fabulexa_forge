"""`schema_drift`: atomic set-semantics column rename / retype / drop.

See `docs/architecture/pending/corrupter-engine-and-manifest.md` § What each
operation breaks, § `schema_drift` transforms the working table's catalog,
§ Retype and the round-trip (normative) for the impact rule and the DuckDB
`CAST` retype-validity oracle this handler implements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

from fabulexa_export.config.models import SchemaDrift
from fabulexa_export.corrupters.manifest import DefectRecord
from fabulexa_export.corrupters.operations._impact import (
    column_locator,
    property_name_for_prop_column,
)
from fabulexa_export.corrupters.state import OperationOutcome, WorkingTable
from fabulexa_export.corrupters.validate import _apply_drift_to_spec
from fabulexa_export.errors import CorruptValidationError
from fabulexa_export.reader.conformance import _ROUND_TRIPPABLE_TYPES, to_csv_text

if TYPE_CHECKING:
    import random

    from fabulexa_export.config.models import CorruptOperation
    from fabulexa_export.corrupters.manifest import ImpactCode
    from fabulexa_export.corrupters.state import CorruptState
    from fabulexa_export.reader.sidecar import ColumnSpec, Sidecar, TableSpec


def _check_no_name_collisions(evolved_spec: "TableSpec", table_name: str) -> None:
    """DriftNoTargetCollision: the evolved catalog names no column twice.

    Catches both a rename map with two sources sharing a target name, and a
    rename target that collides with a surviving (untouched) column — either
    would leave the evolved catalog with a duplicate column name.

    Args:
        evolved_spec: The table's TableSpec after folding this drift in.
        table_name: The table name, for the error message.

    Raises:
        CorruptValidationError: Two or more columns of `evolved_spec` share a
            name.
    """
    names = [col.name for col in evolved_spec.columns]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in names:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    if duplicates:
        raise CorruptValidationError(
            f"schema_drift on {table_name!r}: rename produces colliding column"
            f" name(s) {sorted(duplicates)!r}"
        )


def _cast_column(
    data: "pa.Table", column: str, target_type: str, table_name: str
) -> "pa.Array":
    """Cast `column`'s every value to `target_type` via DuckDB `CAST`.

    The one type-validity oracle for `retype_to`: DuckDB performs the cast, so
    an unrecognized type name or an impossible conversion fails here rather
    than being silently coerced (a `TRY_CAST`-to-NULL would fabricate data).

    Args:
        data: The working table's current Arrow content.
        column: The column to retype.
        target_type: The DuckDB type literal to cast to.
        table_name: The table name, for the error message.

    Returns:
        The cast column, as an Arrow array aligned with `data`'s row order.

    Raises:
        CorruptValidationError: `target_type` is not a recognized DuckDB type,
            or the cast is impossible for some value in `column`.
    """
    import duckdb

    conn = duckdb.connect(":memory:")
    try:
        conn.register("working", data)
        sql = f'SELECT CAST("{column}" AS {target_type}) AS "{column}" FROM working'
        try:
            result = conn.execute(sql).fetch_arrow_table()
        except duckdb.Error as exc:
            raise CorruptValidationError(
                f"schema_drift on {table_name!r}: cannot retype {column!r} to"
                f" {target_type!r}: {exc}"
            ) from exc
    finally:
        conn.close()
    return result.column(column)


def _latest_history_value(
    history_data: "pa.Table | None",
    fork_path: str,
    kind: str,
    record_id: str,
    property_name: str,
) -> str | None:
    """The most recent `history.value` for one working series, or None.

    "Most recent" is the row with the greatest `sim_time` (deterministic
    value-descending tie-break, matching the reader's own C6 resolution) —
    not bounded by `slice_at`; a sound over-declaration, same convention as
    `history_series_exists`.

    Args:
        history_data: The working `history` table's current Arrow, or None
            when the emit carries no `history` table.
        fork_path: The sole branch's fork_path.
        kind: The owning record's kind.
        record_id: The owning record's id.
        property_name: The tracked property name.

    Returns:
        The series' latest value text, or None when no working `history` row
        matches (fork_path, kind, record_id, property).
    """
    if history_data is None or history_data.num_rows == 0:
        return None
    fork_paths = history_data.column("fork_path")
    kinds = history_data.column("kind")
    record_ids = history_data.column("record_id")
    properties = history_data.column("property")
    sim_times = history_data.column("sim_time")
    values = history_data.column("value")
    best: tuple[int, str] | None = None
    for i in range(history_data.num_rows):
        if not (
            fork_paths[i].as_py() == fork_path
            and kinds[i].as_py() == kind
            and record_ids[i].as_py() == record_id
            and properties[i].as_py() == property_name
        ):
            continue
        candidate = (sim_times[i].as_py(), str(values[i].as_py()))
        if best is None or candidate > best:
            best = candidate
    return best[1] if best is not None else None


def _retype_trips_c6(
    column: str,
    col_spec: "ColumnSpec",
    table_spec: "TableSpec",
    target_type: str,
    fork_path: str,
    history_data: "pa.Table | None",
    record_ids: list[object],
    cast_values: list[object],
) -> bool:
    """Whether a retype of `column` to `target_type` breaks C6 for any row.

    Gated first on `retype_to`'s round-trippability (never calls `to_csv_text`
    on a non-round-trippable target — it would raise); only when that gate
    passes does it compare each row's cast value against its series' latest
    working `history.value`, for rows with a series at all.

    Args:
        column: The retyped column's (pre-drift) name.
        col_spec: The column's pre-drift ColumnSpec.
        table_spec: The table's pre-drift TableSpec (for record_kind).
        target_type: The DuckDB type literal `column` is cast to.
        fork_path: The sole branch's fork_path.
        history_data: The working `history` table's current Arrow, or None.
        record_ids: Every row's `record_id`, in `cast_values`' row order.
        cast_values: `column`'s cast values, in `data`'s row order.

    Returns:
        True iff `column` is history_tracked, `target_type` is
        round-trippable, and at least one row with a working history series
        diverges from that series' latest value under the new encoding.
    """
    if not col_spec.history_tracked:
        return False
    if target_type.upper().strip() not in _ROUND_TRIPPABLE_TYPES:
        return False
    if table_spec.record_kind is None:
        return False
    property_name = property_name_for_prop_column(column)
    for record_id, value in zip(record_ids, cast_values):
        if value is None:
            continue
        assert isinstance(record_id, str)
        latest = _latest_history_value(
            history_data, fork_path, table_spec.record_kind, record_id, property_name
        )
        if latest is None:
            continue
        if to_csv_text(value, target_type) != latest:
            return True
    return False


class SchemaDriftCorrupter:
    """Corrupter for `kind: schema_drift` — column rename / retype / drop."""

    def apply(
        self,
        state: "CorruptState",
        operation: "CorruptOperation",
        rule: str,
        rng: "random.Random",
        fork_path: str,
        sidecar: "Sidecar",
    ) -> OperationOutcome:
        assert isinstance(operation, SchemaDrift)
        table_name = operation.target.table
        # Permanent: table_only_target_and_one_action guarantees the concrete
        # `table` selector for schema_drift; this narrows for mypy-strict.
        assert table_name is not None
        working_table = state.tables[table_name]
        spec = working_table.spec
        columns_by_name = {col.name: col for col in spec.columns}

        rename_to = operation.rename_to or {}
        retype_to = operation.retype_to or {}
        drop = operation.drop or []

        evolved_spec = _apply_drift_to_spec(spec, operation)
        _check_no_name_collisions(evolved_spec, table_name)

        history_working = state.tables.get("history")
        history_data = history_working.data if history_working is not None else None
        # DriftColumnsNonStructural confines retype_to to records prop__ / membership
        # elem__ columns, both of whose tables always carry record_id.
        record_ids: list[object] = working_table.data.column("record_id").to_pylist()

        new_data = working_table.data
        defects: list[DefectRecord] = []

        for column in sorted(retype_to):
            target_type = retype_to[column]
            cast_array = _cast_column(new_data, column, target_type, table_name)
            fires_c6 = _retype_trips_c6(
                column,
                columns_by_name[column],
                spec,
                target_type,
                fork_path,
                history_data,
                record_ids,
                cast_array.to_pylist(),
            )
            impact: tuple["ImpactCode", ...] = (
                ("C6",) if fires_c6 else ("beyond-c1-c12",)
            )
            defects.append(
                DefectRecord.model_validate(
                    {
                        "class": "column_retype",
                        "rule": rule,
                        "impact": impact,
                        "location": column_locator(table_name, column),
                    }
                )
            )
            field_index = new_data.schema.get_field_index(column)
            new_data = new_data.set_column(field_index, column, cast_array)

        for column in sorted(rename_to):
            col_spec = columns_by_name[column]
            impact = ("C11",) if col_spec.history_tracked else ("beyond-c1-c12",)
            defects.append(
                DefectRecord.model_validate(
                    {
                        "class": "column_rename",
                        "rule": rule,
                        "impact": impact,
                        "location": column_locator(table_name, rename_to[column]),
                    }
                )
            )

        for column in sorted(drop):
            col_spec = columns_by_name[column]
            impact = ("C11",) if col_spec.history_tracked else ("beyond-c1-c12",)
            defects.append(
                DefectRecord.model_validate(
                    {
                        "class": "column_drop",
                        "rule": rule,
                        "impact": impact,
                        "location": column_locator(table_name, column),
                    }
                )
            )

        if drop:
            new_data = new_data.drop_columns(list(drop))
        if rename_to:
            new_names = [rename_to.get(name, name) for name in new_data.schema.names]
            new_data = new_data.rename_columns(new_names)

        state.tables[table_name] = WorkingTable(spec=evolved_spec, data=new_data)

        units = len(rename_to) + len(retype_to) + len(drop)
        return OperationOutcome(
            kind="schema_drift",
            tables=(table_name,),
            units_selected=units,
            units_affected=units,
            defects=tuple(defects),
        )
