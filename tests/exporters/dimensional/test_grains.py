"""Tests for dimensional exporter grain SQL builders.

Verifies each grain produces correct row counts, ordering, column values,
and virtual column behavior (lead_sim_time).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, write_emit

from exporters._emit_fixtures import (
    _create_ddl,
    _table_spec,
    build_no_runtime_emit,
    build_test_emit,
)
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    ColumnDecl,
    DerivedSpec,
    DimensionalConfig,
    OrdinalSpec,
    SourceDecl,
    TableDecl,
    TimestampSpec,
    ValueMapSpec,
)
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.reader.emit import open_emit


def _col(name: str, **mode: object) -> ColumnDecl:
    """Build a ColumnDecl with one mode. Handles 'from' key aliasing."""
    return ColumnDecl(name=name, **mode)


def _from_col(name: str, src: str) -> ColumnDecl:
    return ColumnDecl(name=name, **{"from": src})


# ---------------------------------------------------------------------------
# history_interval emit builder
# ---------------------------------------------------------------------------

_JOURNEY_INSTANCE_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {"name": "prop__state", "type": "VARCHAR"},
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _build_history_interval_emit(
    tmp_path: Path,
    *,
    include_second_record: bool = False,
) -> Path:
    """Build a minimal emit for history_interval grain tests.

    Includes records__journey_instance (required by versioned-intervals derivation)
    and history rows for journey_instance.state.

    Records:
      - j001: state changes at sim_time 5, 15, 25 (always present)
      - j002: state changes at sim_time 10, 30 (included when include_second_record=True)

    Args:
        tmp_path: Directory to write the emit artifacts into.
        include_second_record: When True, adds j002 for multi-record ordering tests.
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__journey_instance", _JOURNEY_INSTANCE_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    record_rows = [("trunk", "j001", 5, True, None, 25, 0, "completed")]
    history_rows = [
        ("trunk", "journey_instance", "j001", "state", 5, "waiting"),
        ("trunk", "journey_instance", "j001", "state", 15, "in_progress"),
        ("trunk", "journey_instance", "j001", "state", 25, "completed"),
    ]
    if include_second_record:
        record_rows.append(("trunk", "j002", 10, True, None, 30, 1, "completed"))
        history_rows.extend(
            [
                ("trunk", "journey_instance", "j002", "state", 10, "waiting"),
                ("trunk", "journey_instance", "j002", "state", 30, "completed"),
            ]
        )

    for row in record_rows:
        conn.execute(
            'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            list(row),
        )
    for row in history_rows:
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            list(row),
        )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__journey_instance",
                "records",
                _JOURNEY_INSTANCE_COLUMNS,
                len(record_rows),
                record_kind="journey_instance",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, len(history_rows)),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            }
        },
    )
    return tmp_path


# ---------------------------------------------------------------------------
# records grain (Type-1 dim with filter)
# ---------------------------------------------------------------------------


def test_records_grain_all_rows(tmp_path: Path) -> None:
    """records grain with no filter returns all rows of the source table."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_entity",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(grain="records", kind="entity"),
                    key=["id"],
                    columns=[_from_col("id", "record_id")],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        assert len(specs) == 1
        table = emit.query_arrow(specs[0].sql, ())
        assert table.num_rows == 2


def test_records_grain_filter_selects_discriminator_slice(tmp_path: Path) -> None:
    """records grain with filter selects only the matching sub-type."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_consultant",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(
                        grain="records",
                        kind="entity",
                        filter={"prop__entity_type": "consultant"},
                    ),
                    key=["id"],
                    columns=[
                        _from_col("id", "record_id"),
                        _from_col("name", "prop__name"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())
        assert table.num_rows == 1
        assert table.column("name")[0].as_py() == "Dr. Smith"


def test_records_grain_filter_excludes_other_subtypes(tmp_path: Path) -> None:
    """records grain filter for 'nurse' excludes consultants."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_nurse",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(
                        grain="records",
                        kind="entity",
                        filter={"prop__entity_type": "nurse"},
                    ),
                    key=["id"],
                    columns=[_from_col("id", "record_id")],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())
        assert table.num_rows == 1


# ---------------------------------------------------------------------------
# history_point grain
# ---------------------------------------------------------------------------


def test_history_point_grain_rows_by_kind_and_property(tmp_path: Path) -> None:
    """history_point returns one row per matching kind.property change."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_state_changes",
                    role="fact",
                    source=SourceDecl(
                        grain="history_point",
                        kind="journey_instance",
                        property="state",
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("new_state", "value"),
                        _from_col("changed_at", "sim_time"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())
        # 3 history rows for journey_instance.state
        assert table.num_rows == 3


def test_history_point_grain_value_filter(tmp_path: Path) -> None:
    """history_point with value filter returns only matching rows."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_completed",
                    role="fact",
                    source=SourceDecl(
                        grain="history_point",
                        kind="journey_instance",
                        property="state",
                        value="completed",
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("state_val", "value"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())
        assert table.num_rows == 1
        assert table.column("state_val")[0].as_py() == "completed"


# ---------------------------------------------------------------------------
# history_interval grain
# ---------------------------------------------------------------------------


def test_history_interval_grain_lead_sim_time(tmp_path: Path) -> None:
    """history_interval grain: one row per interval, last row has lead_sim_time=NULL."""
    emit_dir = _build_history_interval_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_journey_states",
                    role="fact",
                    source=SourceDecl(
                        grain="history_interval",
                        kind="journey_instance",
                        property="state",
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("state", "value"),
                        _from_col("entered_at_raw", "sim_time"),
                        _from_col("exited_at_raw", "lead_sim_time"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())

        assert table.num_rows == 3
        # Last row's lead_sim_time should be NULL (open-ended interval)
        exited = table.column("exited_at_raw").to_pylist()
        assert exited[-1] is None
        # First two should have values
        assert exited[0] is not None
        assert exited[1] is not None


def test_history_interval_lead_equals_next_sim_time(tmp_path: Path) -> None:
    """lead_sim_time equals the next row's sim_time in the series."""
    emit_dir = _build_history_interval_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_intervals",
                    role="fact",
                    source=SourceDecl(
                        grain="history_interval",
                        kind="journey_instance",
                        property="state",
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("sim_start", "sim_time"),
                        _from_col("sim_end", "lead_sim_time"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())

        starts = table.column("sim_start").to_pylist()
        ends = table.column("sim_end").to_pylist()
        # Each interval's end = next interval's start
        assert ends[0] == starts[1]
        assert ends[1] == starts[2]
        assert ends[2] is None


# ---------------------------------------------------------------------------
# membership grain
# ---------------------------------------------------------------------------


def test_membership_grain_one_row_per_binding(tmp_path: Path) -> None:
    """membership grain emits one row per binding interval."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_team",
                    role="fact",
                    source=SourceDecl(
                        grain="membership",
                        kind="journey_instance",
                        property="team_members",
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("member_id", "member__entity__id"),
                        _from_col("role_name", "elem__role_name"),
                        _from_col("joined", "joined_sim_time"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())

        assert table.num_rows == 1
        assert table.column("role_name")[0].as_py() == "surgeon"
        assert table.column("member_id")[0].as_py() == "e001"


def test_membership_grain_where_predicate(tmp_path: Path) -> None:
    """membership grain where predicate filters by elem__ column."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_surgeons",
                    role="fact",
                    source=SourceDecl(
                        grain="membership",
                        kind="journey_instance",
                        property="team_members",
                        where={"elem__role_name": "surgeon"},
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("entity_id", "member__entity__id"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())

        assert table.num_rows == 1


def test_membership_grain_where_no_match(tmp_path: Path) -> None:
    """membership grain where predicate yields empty table for non-matching value."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_nurses",
                    role="fact",
                    source=SourceDecl(
                        grain="membership",
                        kind="journey_instance",
                        property="team_members",
                        where={"elem__role_name": "nurse"},
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())

        assert table.num_rows == 0


# ---------------------------------------------------------------------------
# Column modes via query execution
# ---------------------------------------------------------------------------


def test_from_projects_record_id_and_props(tmp_path: Path) -> None:
    """from: projects record_id and prop__ columns off the grain surface."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_entity",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(grain="records", kind="entity"),
                    key=["id"],
                    columns=[
                        _from_col("id", "record_id"),
                        _from_col("etype", "prop__entity_type"),
                        _from_col("dept", "prop__department"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())

        col_names = table.schema.names
        assert "id" in col_names
        assert "etype" in col_names
        assert "dept" in col_names


def test_correlation_projects_and_renames(tmp_path: Path) -> None:
    """correlation: projects a reference-id column with no join."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_state",
                    role="fact",
                    source=SourceDecl(
                        grain="history_point",
                        kind="journey_instance",
                        property="state",
                    ),
                    key=["journey_id"],
                    columns=[
                        ColumnDecl(name="journey_id", correlation="record_id"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())

        assert "journey_id" in table.schema.names
        assert "record_id" not in table.schema.names


def test_derived_timestamp_with_runtime_returns_timestamp_type(tmp_path: Path) -> None:
    """derived: timestamp with runtime anchor returns non-null typed column."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_changes",
                    role="fact",
                    source=SourceDecl(
                        grain="history_point",
                        kind="journey_instance",
                        property="state",
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        ColumnDecl(
                            name="changed_at",
                            derived=DerivedSpec(
                                timestamp=TimestampSpec(source="sim_time")
                            ),
                        ),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            anchor,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())

        import pyarrow as pa

        assert pa.types.is_timestamp(table.schema.field("changed_at").type)


def test_derived_timestamp_without_runtime_returns_raw_int(tmp_path: Path) -> None:
    """derived: timestamp without runtime anchor yields raw sim_time integer."""
    emit_dir = build_no_runtime_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_changes",
                    role="fact",
                    source=SourceDecl(
                        grain="history_point",
                        kind="journey_instance",
                        property="state",
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        ColumnDecl(
                            name="changed_at",
                            derived=DerivedSpec(
                                timestamp=TimestampSpec(source="sim_time")
                            ),
                        ),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())

        import pyarrow as pa

        assert pa.types.is_integer(table.schema.field("changed_at").type)


def test_derived_ordinal_deterministic(tmp_path: Path) -> None:
    """derived: ordinal produces ROW_NUMBER with record_id tie-break."""
    emit_dir = _build_history_interval_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_intervals_ord",
                    role="fact",
                    source=SourceDecl(
                        grain="history_interval",
                        kind="journey_instance",
                        property="state",
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("ts_raw", "sim_time"),
                        ColumnDecl(
                            name="seq",
                            derived=DerivedSpec(
                                ordinal=OrdinalSpec(
                                    partition_by="record_id", order_by="ts_raw"
                                )
                            ),
                        ),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())

        seq_vals = table.column("seq").to_pylist()
        assert sorted(seq_vals) == list(range(1, len(seq_vals) + 1))


def test_derived_value_map_known_and_unknown(tmp_path: Path) -> None:
    """value_map maps known values and sends unmapped to NULL."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_state_coded",
                    role="fact",
                    source=SourceDecl(
                        grain="history_point",
                        kind="journey_instance",
                        property="state",
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        ColumnDecl(
                            name="state_code",
                            derived=DerivedSpec(
                                value_map=ValueMapSpec(
                                    **{
                                        "from": "value",
                                        "map": {"waiting": 1, "completed": 3},
                                    }
                                )
                            ),
                        ),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())

        codes = table.column("state_code").to_pylist()
        # waiting->1, in_progress->NULL, completed->3
        assert 1 in codes
        assert 3 in codes
        assert None in codes  # in_progress is unmapped


def test_null_col_produces_typed_null_column(tmp_path: Path) -> None:
    """null: true produces a VARCHAR-typed all-NULL column."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_entity_padded",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(grain="records", kind="entity"),
                    key=["id"],
                    columns=[
                        _from_col("id", "record_id"),
                        ColumnDecl(name="missing_field", null=True),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())

        import pyarrow as pa

        assert pa.types.is_large_string(
            table.schema.field("missing_field").type
        ) or pa.types.is_string(table.schema.field("missing_field").type)
        vals = table.column("missing_field").to_pylist()
        assert all(v is None for v in vals)


# ---------------------------------------------------------------------------
# Determinism and ordering
# ---------------------------------------------------------------------------


def test_build_query_specs_deterministic_sql(tmp_path: Path) -> None:
    """build_query_specs returns identical SQL on two calls with the same config."""
    emit_dir = build_test_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            TableDecl(
                name="dim_entity",
                role="dim",
                scd="type1",
                source=SourceDecl(grain="records", kind="entity"),
                key=["id"],
                columns=[_from_col("id", "record_id")],
            )
        ]
    )
    with open_emit(emit_dir) as emit:
        specs1 = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        specs2 = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )

        assert specs1[0].sql == specs2[0].sql


def test_query_spec_order_by_ends_in_record_id(tmp_path: Path) -> None:
    """Every QuerySpec carries an ORDER BY ending in the grain identity column."""
    emit_dir = build_test_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            TableDecl(
                name="dim_entity",
                role="dim",
                scd="type1",
                source=SourceDecl(grain="records", kind="entity"),
                key=["id"],
                columns=[_from_col("id", "record_id")],
            )
        ]
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

        sql = specs[0].sql
        # ORDER BY should appear and end with record_id
        assert "ORDER BY" in sql
        order_clause = sql[sql.rfind("ORDER BY") :]
        assert '"record_id"' in order_clause


def test_history_interval_multi_interval_order_by_record_id_then_version_start(
    tmp_path: Path,
) -> None:
    """history_interval multi-interval records order by (record_id, version_start).

    Two records each with multiple intervals: the combined result must be
    sorted (record_id, version_start) — version_start is the tightened key
    (exposed as sim_time on the _grain surface).
    """
    emit_dir = _build_history_interval_emit(tmp_path, include_second_record=True)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_journey_intervals",
                    role="fact",
                    source=SourceDecl(
                        grain="history_interval",
                        kind="journey_instance",
                        property="state",
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("version_start", "sim_time"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        table = emit.query_arrow(specs[0].sql, ())

        pairs = list(
            zip(
                table.column("record_id").to_pylist(),
                table.column("version_start").to_pylist(),
            )
        )
        assert pairs == sorted(pairs), "rows must be ordered (record_id, version_start)"


# ---------------------------------------------------------------------------
# Typed-predicate tests (Part C: Step 5)
# ---------------------------------------------------------------------------

# Column definitions for typed-predicate emit
_TYPED_ENTITY_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__entity_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__score",
        "type": "BIGINT",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__active_flag",
        "type": "BOOLEAN",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_TYPED_MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__role_name", "type": "VARCHAR"},
    {"name": "elem__priority", "type": "BIGINT"},
    {"name": "member__entity__kind", "type": "VARCHAR"},
    {"name": "member__entity__id", "type": "VARCHAR"},
]


def _build_typed_predicate_emit(tmp_path: Path) -> Path:
    """Build an emit with BIGINT and BOOLEAN columns for typed-predicate testing."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__entity", _TYPED_ENTITY_COLUMNS))
    conn.execute(_create_ddl("membership__journey__roles", _TYPED_MEMBERSHIP_COLUMNS))

    # score=100 (BIGINT), active_flag=True (BOOLEAN)
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "e001", 5, True, 5, 0, "consultant", 100, True],
    )
    # score=200, active_flag=False
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "e002", 6, True, 6, 1, "nurse", 200, False],
    )

    # membership rows: priority=1 for e001 (surgeon), priority=2 for e002 (nurse)
    conn.execute(
        'INSERT INTO "membership__journey__roles" VALUES (?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j001", 5, "surgeon", 1, "entity", "e001"],
    )
    conn.execute(
        'INSERT INTO "membership__journey__roles" VALUES (?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j001", 10, "nurse", 2, "entity", "e002"],
    )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__entity",
                "records",
                _TYPED_ENTITY_COLUMNS,
                2,
                record_kind="entity",
            ),
            _table_spec(
                "membership__journey__roles",
                "membership",
                _TYPED_MEMBERSHIP_COLUMNS,
                2,
                record_kind="journey",
                property_name="roles",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return tmp_path


def test_records_filter_bigint_column_selects_correctly(tmp_path: Path) -> None:
    """records grain filter on a BIGINT column uses typed CAST literal and selects correctly."""
    emit_dir = _build_typed_predicate_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_high_score",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(
                        grain="records",
                        kind="entity",
                        filter={"prop__score": "100"},
                    ),
                    key=["id"],
                    columns=[_from_col("id", "record_id")],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        # Verify the SQL uses CAST form for the BIGINT filter
        assert "CAST('100' AS BIGINT)" in specs[0].sql
        table = emit.query_arrow(specs[0].sql, ())

        # Only e001 has score=100
        assert table.num_rows == 1
        assert table.column("id")[0].as_py() == "e001"


def test_records_filter_boolean_column_selects_correctly(tmp_path: Path) -> None:
    """records grain filter on a BOOLEAN column uses typed CAST literal and selects correctly."""
    emit_dir = _build_typed_predicate_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_active",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(
                        grain="records",
                        kind="entity",
                        filter={"prop__active_flag": "true"},
                    ),
                    key=["id"],
                    columns=[_from_col("id", "record_id")],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        # Verify the SQL uses CAST form for the BOOLEAN filter
        assert "CAST('true' AS BOOLEAN)" in specs[0].sql
        table = emit.query_arrow(specs[0].sql, ())

        # Only e001 has active_flag=True
        assert table.num_rows == 1
        assert table.column("id")[0].as_py() == "e001"


def test_membership_where_bigint_column_selects_correctly(tmp_path: Path) -> None:
    """membership grain where on a BIGINT elem__ column uses typed CAST literal."""
    emit_dir = _build_typed_predicate_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_priority_one",
                    role="fact",
                    source=SourceDecl(
                        grain="membership",
                        kind="journey",
                        property="roles",
                        where={"elem__priority": "1"},
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("entity_id", "member__entity__id"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        # Verify typed literal in membership where
        assert "CAST('1' AS BIGINT)" in specs[0].sql
        table = emit.query_arrow(specs[0].sql, ())

        # Only priority=1 row (e001/surgeon)
        assert table.num_rows == 1
        assert table.column("entity_id")[0].as_py() == "e001"


def test_varchar_filter_predicate_stays_quoted(tmp_path: Path) -> None:
    """VARCHAR filter predicate is single-quoted (byte-stable, no CAST)."""
    emit_dir = _build_typed_predicate_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_consultant",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(
                        grain="records",
                        kind="entity",
                        filter={"prop__entity_type": "consultant"},
                    ),
                    key=["id"],
                    columns=[_from_col("id", "record_id")],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        # VARCHAR filter must NOT use CAST form
        assert "CAST('consultant'" not in specs[0].sql
        assert "'consultant'" in specs[0].sql
        table = emit.query_arrow(specs[0].sql, ())

        assert table.num_rows == 1


# ---------------------------------------------------------------------------
# List-valued predicates (list-valued-predicates sprint, Phase 2)
# ---------------------------------------------------------------------------


def test_records_grain_filter_list_selects_multiple_subtypes(tmp_path: Path) -> None:
    """records grain with a list filter renders IN and selects every listed value."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_staff",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(
                        grain="records",
                        kind="entity",
                        filter={"prop__entity_type": ["consultant", "nurse"]},
                    ),
                    key=["id"],
                    columns=[
                        _from_col("id", "record_id"),
                        _from_col("etype", "prop__entity_type"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        assert "IN (" in specs[0].sql
        table = emit.query_arrow(specs[0].sql, ())
        assert table.num_rows == 2
        assert set(table.column("etype").to_pylist()) == {"consultant", "nurse"}


def test_membership_grain_where_list_predicate(tmp_path: Path) -> None:
    """membership grain with a list where predicate selects every listed elem__ value."""
    emit_dir = _build_typed_predicate_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_roles",
                    role="fact",
                    source=SourceDecl(
                        grain="membership",
                        kind="journey",
                        property="roles",
                        where={"elem__role_name": ["surgeon", "nurse"]},
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("role_name", "elem__role_name"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        assert "IN (" in specs[0].sql
        table = emit.query_arrow(specs[0].sql, ())
        assert table.num_rows == 2
        assert set(table.column("role_name").to_pylist()) == {"surgeon", "nurse"}


def test_history_point_grain_value_list_filter(tmp_path: Path) -> None:
    """history_point grain with a list value filter selects every listed value."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_terminal_states",
                    role="fact",
                    source=SourceDecl(
                        grain="history_point",
                        kind="journey_instance",
                        property="state",
                        value=["waiting", "completed"],
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("state_val", "value"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        assert "IN (" in specs[0].sql
        table = emit.query_arrow(specs[0].sql, ())
        assert table.num_rows == 2
        assert set(table.column("state_val").to_pylist()) == {"waiting", "completed"}
