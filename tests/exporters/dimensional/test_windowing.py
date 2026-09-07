"""Unit tests for exporters/dimensional/windowing.py.

The delivery classifier per column form, KeyColumnsStable, WindowKeyDuplicate,
and the delta composer — each against the recipe emit's sidecar (tracked /
constant / reference / slice_only columns, membership and history tables) or
a literal relation. The end-to-end properties (drip ≡ one-shot, honesty at a
cutoff) are tests/incremental/test_horizon_acceptance.py.
"""

from __future__ import annotations

import pytest
from recipes._recipe_fixture import build_recipe_emit

from fabulexa_forge.config.models import (
    ColumnDecl,
    DerivedSpec,
    DimensionalConfig,
    ElapsedSpec,
    FkClause,
    OrdinalSpec,
    SourceDecl,
    TableDecl,
    TimestampSpec,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.windowing import (
    check_key_columns_stable,
    check_window_key_unique,
    window_delivery_class,
)
from fabulexa_forge.exporters.horizon import compose_window_delta_sql
from fabulexa_forge.reader.emit import Emit, open_emit
from fabulexa_forge.reader.sidecar import Sidecar


@pytest.fixture(scope="module")
def emit(tmp_path_factory: pytest.TempPathFactory) -> Emit:
    emit_dir = tmp_path_factory.mktemp("recipe_emit")
    build_recipe_emit(emit_dir)
    with open_emit(emit_dir) as opened:
        yield opened


@pytest.fixture(scope="module")
def sidecar(emit: Emit) -> Sidecar:
    return emit.sidecar


def _from(name: str, src: str) -> ColumnDecl:
    return ColumnDecl(name=name, **{"from": src})


def _derived(name: str, **spec: object) -> ColumnDecl:
    return ColumnDecl(name=name, derived=DerivedSpec(**spec))


_DIM_DOCTOR = TableDecl(
    name="dim_doctor",
    role="dim",
    scd="type1",
    source=SourceDecl(grain="records", kind="doctor"),
    key=["doctor_id"],
    columns=[_from("doctor_id", "record_id"), _from("name", "prop__name")],
)


def _config(*tables: TableDecl) -> DimensionalConfig:
    return DimensionalConfig(tables=[_DIM_DOCTOR, *tables])


def _records_fact(
    *columns: ColumnDecl, key: list[str] | None = None, **source: object
) -> TableDecl:
    return TableDecl(
        name="fact",
        role="fact",
        source=SourceDecl(grain="records", kind="patient", **source),
        key=key or ["patient_id"],
        columns=[_from("patient_id", "record_id"), *columns],
    )


def _classify(table: TableDecl, sidecar: Sidecar) -> str:
    return window_delivery_class(table, _config(table), sidecar)


# ---------------------------------------------------------------------------
# window_delivery_class — the snapshot conditions
# ---------------------------------------------------------------------------


def test_type1_dim_is_snapshot(sidecar: Sidecar) -> None:
    assert window_delivery_class(_DIM_DOCTOR, _config(), sidecar) == "snapshot"


def test_mutable_filter_is_snapshot(sidecar: Sidecar) -> None:
    """A records filter on a tracked property can shrink the row set."""
    table = _records_fact(filter={"prop__status": "active"})
    assert _classify(table, sidecar) == "snapshot"


def test_constant_filter_is_not_snapshot(sidecar: Sidecar) -> None:
    """The exempt slice_only discriminator is carried verbatim: prefix-monotone."""
    table = TableDecl(
        name="fact",
        role="fact",
        source=SourceDecl(
            grain="records", kind="staff", filter={"prop__staff_type": "nurse"}
        ),
        key=["staff_id"],
        columns=[_from("staff_id", "record_id"), _from("name", "prop__name")],
    )
    assert _classify(table, sidecar) == "append"


# ---------------------------------------------------------------------------
# window_delivery_class — the horizon-invariance reading per column form
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        (_from("name", "prop__name"), "append"),
        (_from("created", "created_sim_time"), "append"),
        (_from("doctor_ref", "prop__doctor_id"), "append"),
        (_from("doctor_idx", "ref_index__doctor_id"), "append"),
        (_from("status", "prop__status"), "upsert"),
        (_from("active", "active"), "upsert"),
        (_from("deactivated_at", "deactivated_at"), "upsert"),
        (_from("mutated_at", "last_mutation_sim_time"), "upsert"),
        (
            _derived("created_at", timestamp=TimestampSpec(source="created_sim_time")),
            "append",
        ),
        (
            _derived(
                "mutated_at", timestamp=TimestampSpec(source="last_mutation_sim_time")
            ),
            "upsert",
        ),
        (ColumnDecl(name="placeholder", null=True), "append"),
    ],
    ids=lambda v: v if isinstance(v, str) else v.name,
)
def test_records_grain_channels(
    sidecar: Sidecar, column: ColumnDecl, expected: str
) -> None:
    assert _classify(_records_fact(column), sidecar) == expected


def test_ordinal_by_grain_time_key_is_invariant(sidecar: Sidecar) -> None:
    table = _records_fact(
        _from("created", "created_sim_time"),
        _derived(
            "seq", ordinal=OrdinalSpec(partition_by="patient_id", order_by="created")
        ),
    )
    assert _classify(table, sidecar) == "append"


def test_ordinal_by_mutable_column_varies(sidecar: Sidecar) -> None:
    table = _records_fact(
        _from("mutated", "last_mutation_sim_time"),
        _derived(
            "seq", ordinal=OrdinalSpec(partition_by="patient_id", order_by="mutated")
        ),
    )
    assert _classify(table, sidecar) == "upsert"


def test_ordinal_partitioned_by_mutable_column_varies(sidecar: Sidecar) -> None:
    table = _records_fact(
        _from("created", "created_sim_time"),
        _from("status", "prop__status"),
        _derived("seq", ordinal=OrdinalSpec(partition_by="status", order_by="created")),
    )
    assert _classify(table, sidecar) == "upsert"


def test_elapsed_varies(sidecar: Sidecar) -> None:
    table = TableDecl(
        name="fact",
        role="fact",
        source=SourceDecl(
            grain="history_point", kind="patient", property="status", value="discharged"
        ),
        key=["patient_id"],
        columns=[
            _from("patient_id", "record_id"),
            _derived(
                "los_hours",
                elapsed=ElapsedSpec(
                    correlate_on="record_id",
                    other_where={"value": "pending"},
                    start_source="sim_time",
                    end_source="sim_time",
                    unit="hours",
                ),
            ),
        ],
    )
    assert _classify(table, sidecar) == "upsert"


def test_history_point_is_append(sidecar: Sidecar) -> None:
    table = TableDecl(
        name="fact",
        role="fact",
        source=SourceDecl(grain="history_point", kind="patient", property="status"),
        key=["patient_id", "at"],
        columns=[
            _from("patient_id", "record_id"),
            _from("at", "sim_time"),
            _from("status", "value"),
        ],
    )
    assert _classify(table, sidecar) == "append"


def test_history_interval_end_varies(sidecar: Sidecar) -> None:
    table = TableDecl(
        name="fact",
        role="fact",
        source=SourceDecl(grain="history_interval", kind="patient", property="status"),
        key=["patient_id", "start"],
        columns=[
            _from("patient_id", "record_id"),
            _from("start", "sim_time"),
            _from("end", "lead_sim_time"),
        ],
    )
    assert _classify(table, sidecar) == "upsert"


def _membership_fact(*columns: ColumnDecl, key: list[str] | None = None) -> TableDecl:
    return TableDecl(
        name="fact",
        role="fact",
        source=SourceDecl(grain="membership", kind="patient", property="visits"),
        key=key or ["patient_id", "joined"],
        columns=[
            _from("patient_id", "record_id"),
            _from("joined", "joined_sim_time"),
            *columns,
        ],
    )


def test_membership_grain_open_end_varies(sidecar: Sidecar) -> None:
    assert (
        _classify(_membership_fact(_from("left", "left_sim_time")), sidecar) == "upsert"
    )
    assert _classify(_membership_fact(_from("slot", "elem__slot")), sidecar) == "append"


def test_membership_fk_on_membership_grain_is_invariant(sidecar: Sidecar) -> None:
    table = _membership_fact(
        ColumnDecl(name="doctor_key", fk=FkClause(to="dim_doctor", via="membership"))
    )
    assert _classify(table, sidecar) == "append"


def test_membership_fk_on_records_grain_varies(sidecar: Sidecar) -> None:
    table = _records_fact(
        ColumnDecl(
            name="doctor_key",
            fk=FkClause(to="dim_doctor", via="membership", property="visits"),
        )
    )
    assert _classify(table, sidecar) == "upsert"


def test_reference_fk_over_constant_hop_is_invariant(sidecar: Sidecar) -> None:
    table = _records_fact(
        ColumnDecl(name="doctor_key", fk=FkClause(to="dim_doctor", via="reference"))
    )
    assert _classify(table, sidecar) == "append"


def test_scd2_dim_tracked_read_is_invariant_valid_to_is_not(sidecar: Sidecar) -> None:
    base = [
        _from("patient_id", "record_id"),
        _from("status", "prop__status"),
        _derived("valid_from", scd_window="valid_from"),
    ]
    without_valid_to = TableDecl(
        name="dim_patient",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="patient"),
        key=["patient_id", "valid_from"],
        columns=base,
    )
    with_valid_to = without_valid_to.model_copy(
        update={"columns": [*base, _derived("valid_to", scd_window="valid_to")]}
    )
    assert _classify(without_valid_to, sidecar) == "append"
    assert _classify(with_valid_to, sidecar) == "upsert"


# ---------------------------------------------------------------------------
# KeyColumnsStable
# ---------------------------------------------------------------------------


def test_key_on_interval_end_refused(sidecar: Sidecar) -> None:
    table = _membership_fact(_from("left", "left_sim_time"), key=["patient_id", "left"])
    with pytest.raises(
        ExportError, match=r"key column 'left'.*interval end left_sim_time"
    ):
        check_key_columns_stable(table, _config(table), sidecar)


def test_key_on_tracked_property_refused(sidecar: Sidecar) -> None:
    table = _records_fact(_from("status", "prop__status"), key=["patient_id", "status"])
    with pytest.raises(
        ExportError, match=r"key column 'status'.*tracked property 'status'"
    ):
        check_key_columns_stable(table, _config(table), sidecar)


def test_stable_key_passes(sidecar: Sidecar) -> None:
    table = _membership_fact(_from("left", "left_sim_time"))
    check_key_columns_stable(table, _config(table), sidecar)


def test_snapshot_table_unstable_key_refused(sidecar: Sidecar) -> None:
    """KeyColumnsStable is always-on: a snapshot table keyed on a mutable
    column refuses too — the delivery class no longer gates the rule."""
    table = _records_fact(
        _from("status", "prop__status"),
        key=["status"],
        filter={"prop__status": "active"},
    )
    with pytest.raises(
        ExportError, match=r"key column 'status'.*tracked property 'status'"
    ):
        check_key_columns_stable(table, _config(table), sidecar)


# ---------------------------------------------------------------------------
# WindowKeyDuplicate, the delta composer
# ---------------------------------------------------------------------------


def _literal(rows: str, cols: str) -> str:
    return f"SELECT * FROM (VALUES {rows}) AS t({cols})"


def test_duplicate_key_at_horizon_refused(emit: Emit) -> None:
    table = _records_fact()
    with pytest.raises(
        ExportError,
        match=r"key \['patient_id'\] is not unique.*\(1 duplicated key values\)",
    ):
        check_window_key_unique(
            emit, table, _literal("('a'), ('a'), ('b')", "patient_id")
        )


def test_null_keys_count_as_equal(emit: Emit) -> None:
    table = _records_fact()
    with pytest.raises(ExportError, match="not unique"):
        check_window_key_unique(emit, table, _literal("(NULL), (NULL)", "patient_id"))


def test_unique_key_passes(emit: Emit) -> None:
    check_window_key_unique(
        emit, _records_fact(), _literal("('a'), ('b')", "patient_id")
    )


def test_delta_is_ordered_multiset_difference(emit: Emit) -> None:
    end = _literal("(2, 'y'), (1, 'x'), (2, 'y'), (3, NULL)", "k, v")
    start = _literal("(2, 'y'), (3, NULL)", "k, v")
    sql = compose_window_delta_sql(end, start, ["k", "v"])
    assert emit.query(sql, ()) == [(1, "x"), (2, "y")]
