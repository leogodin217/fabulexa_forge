"""Regression tests for three dimensional-exporter fixes.

1. SCD-2 builders honor table_decl.source.filter: a discriminator-split
   scd: type2 dim contains only the filtered sub-type's rows (full export and
   windowed __rows).
2. Scd2ColumnModeSupported: validate_table rejects column modes the type2
   build does not define (fk, correlation, derived: ordinal / elapsed)
   instead of rendering them as silent NULLs, while admitting the pure
   per-version value renderings timestamp / date_parse / value_map / decimal
   / json_precision.
3. Windowed fact export fails fast when no output column projects the grain's
   window key, instead of falling back to the raw key name.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, write_emit

from exporters._emit_fixtures import _create_ddl, _table_spec
from fabulexa_forge.config.models import (
    ColumnDecl,
    DateParseSpec,
    DecimalSpec,
    DerivedSpec,
    DimensionalConfig,
    ElapsedSpec,
    FkClause,
    JsonPrecisionSpec,
    OrdinalSpec,
    SourceDecl,
    TableDecl,
    TimestampSpec,
    ValueMapSpec,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.dimensional.validation import (
    check_scd2_column_mode_supported,
)
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit

# ---------------------------------------------------------------------------
# Emit: actor kind discriminator-split on prop__actor_type
# ---------------------------------------------------------------------------

_ACTOR_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT", "history_tracked": False},
    {"name": "active", "type": "BOOLEAN", "history_tracked": False},
    {"name": "deactivated_at", "type": "BIGINT", "history_tracked": False},
    {"name": "last_mutation_sim_time", "type": "BIGINT", "history_tracked": False},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__actor_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    {"name": "kind", "type": "VARCHAR"},
    identity_column("record_id", "VARCHAR"),
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _build_split_emit(tmp_path: Path) -> Path:
    """Emit with a patient (a001, 2 status versions) and a staff (s001, 1)."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    actor_rows: list[tuple[object, ...]] = [
        ("trunk", "a001", 5, True, None, 20, 0, "Alice", "discharged", "patient"),
        ("trunk", "s001", 5, True, None, 15, 1, "Sam", "active", "staff"),
    ]
    for row in actor_rows:
        conn.execute(
            'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            list(row),
        )
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    history_rows: list[tuple[object, ...]] = [
        ("trunk", "actor", "a001", "status", 10, "admitted"),
        ("trunk", "actor", "a001", "status", 20, "discharged"),
        ("trunk", "actor", "s001", "status", 15, "active"),
    ]
    for hist_row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(hist_row))
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor",
                "records",
                _ACTOR_COLUMNS,
                len(actor_rows),
                record_kind="actor",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, len(history_rows)),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


def _build_three_subtype_emit(tmp_path: Path) -> Path:
    """Emit with three actor_type sub-types: patient (2 versions), staff (1),
    visitor (1) — for list-filter semi-join restriction tests."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    actor_rows: list[tuple[object, ...]] = [
        ("trunk", "a001", 5, True, None, 20, 0, "Alice", "discharged", "patient"),
        ("trunk", "s001", 5, True, None, 15, 1, "Sam", "active", "staff"),
        ("trunk", "v001", 5, True, None, 5, 2, "Vic", "browsing", "visitor"),
    ]
    for row in actor_rows:
        conn.execute(
            'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            list(row),
        )
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    history_rows: list[tuple[object, ...]] = [
        ("trunk", "actor", "a001", "status", 10, "admitted"),
        ("trunk", "actor", "a001", "status", 20, "discharged"),
        ("trunk", "actor", "s001", "status", 15, "active"),
        ("trunk", "actor", "v001", "status", 5, "browsing"),
    ]
    for hist_row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(hist_row))
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor",
                "records",
                _ACTOR_COLUMNS,
                len(actor_rows),
                record_kind="actor",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, len(history_rows)),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


def _make_scd2_decl(
    filter_: dict[str, str | list[str]] | None,
    extra_columns: list[ColumnDecl] | None = None,
) -> TableDecl:
    """A dim_patient scd: type2 decl, optionally discriminator-filtered."""
    return TableDecl(
        name="dim_patient",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="actor", filter=filter_),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="name", **{"from": "prop__name"}),
            ColumnDecl(name="status", **{"from": "prop__status"}),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
            ColumnDecl(name="valid_to", derived=DerivedSpec(scd_window="valid_to")),
        ]
        + (extra_columns or []),
    )


# ---------------------------------------------------------------------------
# SCD-2 honors source.filter
# ---------------------------------------------------------------------------


def test_scd2_full_export_honors_source_filter(tmp_path: Path) -> None:
    """A discriminator-split type2 dim contains only the filtered sub-type."""
    emit_dir = _build_split_emit(tmp_path)
    config = DimensionalConfig(
        tables=[_make_scd2_decl({"prop__actor_type": "patient"})]
    )
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    # Only a001 (patient): 2 versions. s001 (staff) must be absent.
    assert result.num_rows == 2
    assert set(rows["id"]) == {"a001"}
    assert rows["name"] == ["Alice", "Alice"]
    assert rows["status"] == ["admitted", "discharged"]


def test_scd2_full_export_without_filter_keeps_all_subtypes(tmp_path: Path) -> None:
    """No filter: the whole kind's versions appear (contrast case)."""
    emit_dir = _build_split_emit(tmp_path)
    config = DimensionalConfig(tables=[_make_scd2_decl(None)])
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    assert result.num_rows == 3
    assert set(rows["id"]) == {"a001", "s001"}


def test_scd2_windowed_rows_honor_source_filter(tmp_path: Path) -> None:
    """The windowed __rows SELECT also restricts to the filtered sub-type."""
    emit_dir = _build_split_emit(tmp_path)
    config = DimensionalConfig(
        tables=[_make_scd2_decl({"prop__actor_type": "patient"})]
    )
    window = Window(index=0, start_ns=0, end_ns=100, label="w0")
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            config,
            None,
            window,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        assert specs[0].table_name == "dim_patient__rows"
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    assert result.num_rows == 2
    assert set(rows["id"]) == {"a001"}


def test_scd2_full_export_honors_list_filter(tmp_path: Path) -> None:
    """A list filter on the discriminator restricts to the listed sub-types.

    The versioned-intervals semi-join composed from a two-value list matches
    exactly patient + staff rows, excluding visitor.
    """
    emit_dir = _build_three_subtype_emit(tmp_path)
    config = DimensionalConfig(
        tables=[_make_scd2_decl({"prop__actor_type": ["patient", "staff"]})]
    )
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    # a001 (patient): 2 versions; s001 (staff): 1 version. v001 (visitor) absent.
    assert result.num_rows == 3
    assert set(rows["id"]) == {"a001", "s001"}


# ---------------------------------------------------------------------------
# Scd2ColumnModeSupported: unimplemented modes rejected at validate time
# ---------------------------------------------------------------------------


def _type2_decl_with(col_decl: ColumnDecl) -> TableDecl:
    return TableDecl(
        name="dim_patient",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
            col_decl,
        ],
    )


_UNSUPPORTED_MODE_COLUMNS: list[ColumnDecl] = [
    ColumnDecl(name="hospital_id", fk=FkClause(to="dim_hospital", via="reference")),
    ColumnDecl(name="link_id", correlation="prop__link_id"),
    ColumnDecl(
        name="row_num",
        derived=DerivedSpec(
            ordinal=OrdinalSpec(partition_by="id", order_by="valid_from")
        ),
    ),
    ColumnDecl(
        name="wait_minutes",
        derived=DerivedSpec(
            elapsed=ElapsedSpec(
                correlate_on="id",
                other_where={"status": "admitted"},
                start_source="last_mutation_sim_time",
                end_source="last_mutation_sim_time",
                unit="minutes",
            )
        ),
    ),
]


@pytest.mark.parametrize(
    "col_decl",
    _UNSUPPORTED_MODE_COLUMNS,
    ids=["fk", "correlation", "ordinal", "elapsed"],
)
def test_scd2_unsupported_column_mode_raises(col_decl: ColumnDecl) -> None:
    """Every mode the type2 builder does not implement raises at validate time."""
    table_decl = _type2_decl_with(col_decl)
    with pytest.raises(ExportError, match="not supported on an scd: type2 table"):
        check_scd2_column_mode_supported(col_decl, table_decl)


_SUPPORTED_MODE_COLUMNS: list[ColumnDecl] = [
    ColumnDecl(name="status", **{"from": "prop__status"}),
    ColumnDecl(name="placeholder", null=True),
    ColumnDecl(name="valid_to", derived=DerivedSpec(scd_window="valid_to")),
    ColumnDecl(
        name="mutated_at",
        derived=DerivedSpec(timestamp=TimestampSpec(source="last_mutation_sim_time")),
    ),
    ColumnDecl(
        name="birth_date",
        derived=DerivedSpec(
            date_parse=DateParseSpec(**{"from": "prop__dob", "format": "%Y-%m-%d"})
        ),
    ),
    ColumnDecl(
        name="status_code",
        derived=DerivedSpec(
            value_map=ValueMapSpec(**{"from": "prop__status"}, map={"admitted": 1})
        ),
    ),
    ColumnDecl(
        name="score",
        derived=DerivedSpec(
            decimal=DecimalSpec(**{"from": "prop__score"}, **{"as": (4, 3)})
        ),
    ),
    ColumnDecl(
        name="payload",
        derived=DerivedSpec(
            json_precision=JsonPrecisionSpec(
                **{"from": "prop__payload"}, leaves={"amount": 2}
            )
        ),
    ),
]


@pytest.mark.parametrize(
    "col_decl",
    _SUPPORTED_MODE_COLUMNS,
    ids=[
        "from",
        "null",
        "scd_window",
        "timestamp",
        "date_parse",
        "value_map",
        "decimal",
        "json_precision",
    ],
)
def test_scd2_supported_column_modes_pass(col_decl: ColumnDecl) -> None:
    """from/null/derived: scd_window and every value rendering
    (timestamp/date_parse/value_map/decimal/json_precision) stay supported
    on type2 — mode-gate only; the source-column-type gates
    (DecimalSourceIsDouble, JsonPrecisionSourceIsVarchar, ...) are separate
    checks run by validate_table."""
    check_scd2_column_mode_supported(col_decl, _type2_decl_with(col_decl))


def test_non_type2_table_exempt_from_mode_gate() -> None:
    """The gate applies only to scd: type2 tables."""
    col_decl = ColumnDecl(
        name="mutated_at",
        derived=DerivedSpec(timestamp=TimestampSpec(source="last_mutation_sim_time")),
    )
    fact_decl = TableDecl(
        name="fact_actor",
        role="fact",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id"],
        columns=[ColumnDecl(name="id", **{"from": "record_id"}), col_decl],
    )
    # Should not raise
    check_scd2_column_mode_supported(col_decl, fact_decl)


def test_scd2_unsupported_mode_rejected_via_build_query_specs(tmp_path: Path) -> None:
    """The gate is wired into validate_table: build_query_specs rejects it."""
    emit_dir = _build_split_emit(tmp_path)
    ordinal_col = ColumnDecl(
        name="row_num",
        derived=DerivedSpec(
            ordinal=OrdinalSpec(partition_by="id", order_by="valid_from")
        ),
    )
    config = DimensionalConfig(
        tables=[_make_scd2_decl(None, extra_columns=[ordinal_col])]
    )
    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportError, match="not supported on an scd: type2 table"):
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=discard_notice_sink,
                base_relations=None,
            )


# ---------------------------------------------------------------------------
# Windowed fact: missing window-key projection fails fast
# ---------------------------------------------------------------------------


def test_windowed_records_fact_missing_window_key_raises(tmp_path: Path) -> None:
    """No output column projects last_mutation_sim_time → pre-flight ExportError."""
    emit_dir = _build_split_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            TableDecl(
                name="fact_actor",
                role="fact",
                source=SourceDecl(grain="records", kind="actor"),
                key=["id"],
                columns=[
                    ColumnDecl(name="id", **{"from": "record_id"}),
                    ColumnDecl(name="name", **{"from": "prop__name"}),
                ],
            )
        ]
    )
    window = Window(index=0, start_ns=0, end_ns=100, label="w0")
    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportError, match="window key 'last_mutation_sim_time'"):
            build_query_specs(
                emit,
                config,
                None,
                window,
                notice_sink=discard_notice_sink,
                base_relations=None,
            )


def test_windowed_history_point_fact_missing_window_key_raises(
    tmp_path: Path,
) -> None:
    """No output column projects sim_time → pre-flight ExportError."""
    emit_dir = _build_split_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            TableDecl(
                name="fact_status",
                role="fact",
                source=SourceDecl(
                    grain="history_point", kind="actor", property="status"
                ),
                key=["record_id"],
                columns=[
                    ColumnDecl(name="record_id", **{"from": "record_id"}),
                    ColumnDecl(name="val", **{"from": "value"}),
                ],
            )
        ]
    )
    window = Window(index=0, start_ns=0, end_ns=100, label="w0")
    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportError, match="window key 'sim_time'"):
            build_query_specs(
                emit,
                config,
                None,
                window,
                notice_sink=discard_notice_sink,
                base_relations=None,
            )


def test_windowed_records_fact_with_window_key_passes(tmp_path: Path) -> None:
    """Projecting the window key (from:) keeps windowed export working."""
    emit_dir = _build_split_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            TableDecl(
                name="fact_actor",
                role="fact",
                source=SourceDecl(grain="records", kind="actor"),
                key=["id"],
                columns=[
                    ColumnDecl(name="id", **{"from": "record_id"}),
                    ColumnDecl(name="mutated_at", **{"from": "last_mutation_sim_time"}),
                ],
            )
        ]
    )
    window = Window(index=0, start_ns=0, end_ns=18, label="w0")
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            config,
            None,
            window,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    # Window [0, 18): only s001 (last_mutation_sim_time=15) lands in it.
    rows = result.to_pydict()
    assert rows["id"] == ["s001"]
