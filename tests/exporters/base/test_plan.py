"""Tests for build_base_plan: kind enumeration, presentation, exclude/rename
resolution, and collision checks.

Sidecars are built in-memory via Sidecar.from_raw (no DuckDB needed — plan
building reads only the sidecar), keeping each fixture minimal and focused.
"""

from __future__ import annotations

import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import BaseConfig, ExcludeDecl, RenameEntry
from fabulexa_forge.errors import (
    BaseExcludeUnresolved,
    BaseNameCollision,
    BaseRenameSliceOnly,
    BaseRenameUnresolved,
    ExportError,
)
from fabulexa_forge.exporters.base.plan import build_base_plan
from fabulexa_forge.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# Sidecar-building helpers
# ---------------------------------------------------------------------------


def _col(
    name: str,
    type_: str = "VARCHAR",
    history_tracked: bool | None = None,
    temporal_class: str | None = None,
) -> dict[str, object]:
    """Build a raw sidecar column entry."""
    col: dict[str, object] = {"name": name, "type": type_}
    if history_tracked is not None:
        col["history_tracked"] = history_tracked
    if temporal_class is not None:
        col["temporal_class"] = temporal_class
    return col


def _records_table(
    kind: str,
    prop_cols: list[dict[str, object]],
    presentation_id: bool = False,
    rows: int = 1,
) -> dict[str, object]:
    """Build a raw records__<kind> table entry with the contract's structural prefix."""
    cols = [_col("fork_path"), _col("record_id")]
    if presentation_id:
        cols.append(_col("presentation_id"))
    cols += [
        _col("created_sim_time", "BIGINT"),
        _col("active", "BOOLEAN"),
        _col("deactivated_at", "BIGINT"),
        _col("last_mutation_sim_time", "BIGINT"),
    ]
    cols += prop_cols
    return {
        "name": f"records__{kind}",
        "category": "records",
        "record_kind": kind,
        "columns": cols,
        "rows": rows,
    }


def _membership_table(owner_kind: str, prop: str, rows: int = 1) -> dict[str, object]:
    """Build a raw membership__<owner_kind>__<prop> table entry."""
    cols = [
        _col("fork_path"),
        _col("record_id"),
        _col("joined_sim_time", "BIGINT"),
        _col("left_sim_time", "BIGINT"),
    ]
    return {
        "name": f"membership__{owner_kind}__{prop}",
        "category": "membership",
        "record_kind": owner_kind,
        "property": prop,
        "columns": cols,
        "rows": rows,
    }


def _history_table() -> dict[str, object]:
    """Build the fixed-category history table entry."""
    return {
        "name": "history",
        "category": "fixed",
        "columns": [
            _col("fork_path"),
            _col("kind"),
            _col("record_id"),
            _col("property"),
            _col("sim_time", "BIGINT"),
            _col("value"),
        ],
        "rows": 0,
    }


def _sidecar(
    tables: list[dict[str, object]],
    enum_domains: dict[str, object] | None = None,
) -> Sidecar:
    """Build a Sidecar directly from a raw base.json-shaped mapping."""
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "tables": tables,
    }
    if enum_domains is not None:
        raw["enum_domains"] = enum_domains
    return Sidecar.from_raw(raw)


def _spanning_sidecar() -> Sidecar:
    """A sidecar spanning two records kinds plus a membership and a fixed table.

    - records__patient: one tracked, one constant property.
    - records__doctor: one constant property, carries presentation_id.
    - membership__patient__visits: never a plan entry.
    - history: never a plan entry.
    """
    patient_table = _records_table(
        "patient",
        [
            _col("prop__status", history_tracked=True, temporal_class="tracked"),
            _col("prop__name", history_tracked=False, temporal_class="constant"),
        ],
    )
    doctor_table = _records_table(
        "doctor",
        [_col("prop__name", history_tracked=False, temporal_class="constant")],
        presentation_id=True,
    )
    visits_membership = _membership_table("patient", "visits")
    return _sidecar(
        tables=[patient_table, doctor_table, visits_membership, _history_table()]
    )


def _staff_sidecar() -> Sidecar:
    """A sidecar with a sub-typed kind: the discriminator is slice_only but
    exempt (subtype_values non-empty)."""
    staff_table = _records_table(
        "staff",
        [
            _col("prop__name", history_tracked=False, temporal_class="constant"),
            _col(
                "prop__staff_type",
                history_tracked=False,
                temporal_class="slice_only",
            ),
        ],
    )
    return _sidecar(
        tables=[staff_table],
        enum_domains={"staff": {"staff_type": ["nurse", "physician"]}},
    )


def _slice_only_sidecar() -> Sidecar:
    """A sidecar with one non-exempt slice_only property alongside a tracked one."""
    patient_table = _records_table(
        "patient",
        [
            _col("prop__status", history_tracked=True, temporal_class="tracked"),
            _col(
                "prop__loyalty_tier",
                history_tracked=False,
                temporal_class="slice_only",
            ),
        ],
    )
    return _sidecar(tables=[patient_table])


def _degenerate_slice_only_sidecar() -> Sidecar:
    """A sidecar whose sole kind's every property is non-exempt slice_only."""
    member_table = _records_table(
        "member",
        [_col("prop__tier", history_tracked=False, temporal_class="slice_only")],
    )
    return _sidecar(tables=[member_table])


# ---------------------------------------------------------------------------
# Kind enumeration
# ---------------------------------------------------------------------------


def test_one_spec_per_kind_in_sidecar_order() -> None:
    """One BaseTableSpec per records kind, in sidecar kind-declaration order."""
    plan = build_base_plan(_spanning_sidecar(), None, discard_notice_sink)
    assert [t.kind for t in plan.tables] == ["patient", "doctor"]


def test_membership_and_fixed_tables_never_yield_a_spec() -> None:
    """No membership-category or fixed-category table ever produces a spec."""
    plan = build_base_plan(_spanning_sidecar(), None, discard_notice_sink)
    kinds = {t.kind for t in plan.tables}
    assert "visits" not in kinds
    assert "history" not in kinds
    assert len(plan.tables) == 2


def test_table_name_defaults_to_prefix_stripped_kind() -> None:
    """table_name defaults to the prefix-stripped kind."""
    plan = build_base_plan(_spanning_sidecar(), None, discard_notice_sink)
    names = {t.kind: t.table_name for t in plan.tables}
    assert names == {"patient": "patient", "doctor": "doctor"}


def test_column_renames_carries_record_id_to_id_default() -> None:
    """column_renames carries record_id -> id by default."""
    plan = build_base_plan(_spanning_sidecar(), None, discard_notice_sink)
    for spec in plan.tables:
        assert spec.column_renames["record_id"] == "id"


def test_has_presentation_id_reflects_sidecar_per_kind() -> None:
    """has_presentation_id reflects the sidecar per kind."""
    plan = build_base_plan(_spanning_sidecar(), None, discard_notice_sink)
    by_kind = {t.kind: t.has_presentation_id for t in plan.tables}
    assert by_kind == {"patient": False, "doctor": True}


def test_config_none_yields_every_kind_with_defaults() -> None:
    """config=None yields every kind with defaults and emits only slice_only
    notices."""
    sink = RecordingNoticeSink()
    plan = build_base_plan(_spanning_sidecar(), None, sink)
    assert {t.kind for t in plan.tables} == {"patient", "doctor"}
    assert sink.notices == []


# ---------------------------------------------------------------------------
# slice_only omission
# ---------------------------------------------------------------------------


def test_non_exempt_slice_only_property_omitted_with_notice() -> None:
    """A non-exempt slice_only property is absent, with exactly one notice."""
    sink = RecordingNoticeSink()
    plan = build_base_plan(_slice_only_sidecar(), None, sink)
    spec = plan.tables[0]
    assert "loyalty_tier" not in spec.properties
    assert "status" in spec.properties
    assert len(sink.notices) == 1
    assert sink.notices[0].code == "slice-only-column-omitted"
    assert "loyalty_tier" in sink.notices[0].message


def test_exempt_discriminator_retained() -> None:
    """The exempt discriminator is retained even though its class is slice_only."""
    plan = build_base_plan(_staff_sidecar(), None, discard_notice_sink)
    spec = plan.tables[0]
    assert "staff_type" in spec.properties


def test_degenerate_slice_only_kind_still_yields_a_table() -> None:
    """A kind whose every property is non-exempt slice_only still yields a table."""
    sink = RecordingNoticeSink()
    plan = build_base_plan(_degenerate_slice_only_sidecar(), None, sink)
    assert len(plan.tables) == 1
    assert plan.tables[0].properties == frozenset()
    assert len(sink.notices) == 1


# ---------------------------------------------------------------------------
# exclude
# ---------------------------------------------------------------------------


def test_exclude_kinds_drops_a_kind() -> None:
    """exclude.kinds drops a kind."""
    config = BaseConfig(exclude=ExcludeDecl(kinds=["doctor"]))
    plan = build_base_plan(_spanning_sidecar(), config, discard_notice_sink)
    assert [t.kind for t in plan.tables] == ["patient"]


def test_exclude_tables_drops_by_base_output_name() -> None:
    """exclude.tables drops an output table by its base output name."""
    config = BaseConfig(exclude=ExcludeDecl(tables=["doctor"]))
    plan = build_base_plan(_spanning_sidecar(), config, discard_notice_sink)
    assert [t.kind for t in plan.tables] == ["patient"]


def test_exclude_kinds_unresolved_raises() -> None:
    """exclude naming something base does not emit raises BaseExcludeUnresolved."""
    config = BaseConfig(exclude=ExcludeDecl(kinds=["nonexistent"]))
    with pytest.raises(BaseExcludeUnresolved):
        build_base_plan(_spanning_sidecar(), config, discard_notice_sink)


def test_exclude_tables_unresolved_raises() -> None:
    """exclude.tables naming something base does not emit raises
    BaseExcludeUnresolved."""
    config = BaseConfig(exclude=ExcludeDecl(tables=["nonexistent"]))
    with pytest.raises(BaseExcludeUnresolved):
        build_base_plan(_spanning_sidecar(), config, discard_notice_sink)


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


def test_rename_name_overrides_table_name() -> None:
    """rename with name overrides the output table name."""
    config = BaseConfig(rename=[RenameEntry(table="records__doctor", name="dr")])
    plan = build_base_plan(_spanning_sidecar(), config, discard_notice_sink)
    names = {t.kind: t.table_name for t in plan.tables}
    assert names["doctor"] == "dr"


def test_rename_columns_overrides_state_at_identity_keyed_on_record_id() -> None:
    """rename with columns overrides a state-at column identity, keyed on
    record_id, not the defaulted id."""
    config = BaseConfig(
        rename=[
            RenameEntry(table="records__doctor", columns={"record_id": "doctor_id"})
        ]
    )
    plan = build_base_plan(_spanning_sidecar(), config, discard_notice_sink)
    spec = next(t for t in plan.tables if t.kind == "doctor")
    assert spec.column_renames["record_id"] == "doctor_id"


def test_rename_naming_omitted_slice_only_column_raises() -> None:
    """rename naming an omitted slice_only column raises BaseRenameSliceOnly."""
    config = BaseConfig(
        rename=[
            RenameEntry(
                table="records__patient",
                columns={"prop__loyalty_tier": "tier"},
            )
        ]
    )
    with pytest.raises(BaseRenameSliceOnly):
        build_base_plan(_slice_only_sidecar(), config, discard_notice_sink)


def test_rename_table_not_surviving_raises() -> None:
    """rename whose table is not a surviving records__<kind> raises
    BaseRenameUnresolved."""
    config = BaseConfig(rename=[RenameEntry(table="records__nonexistent", name="x")])
    with pytest.raises(BaseRenameUnresolved):
        build_base_plan(_spanning_sidecar(), config, discard_notice_sink)


def test_rename_columns_key_not_state_at_column_raises() -> None:
    """rename whose columns key is not a state-at column identity raises
    BaseRenameUnresolved."""
    config = BaseConfig(
        rename=[
            RenameEntry(table="records__doctor", columns={"prop__nonexistent": "x"})
        ]
    )
    with pytest.raises(BaseRenameUnresolved):
        build_base_plan(_spanning_sidecar(), config, discard_notice_sink)


def test_two_kinds_renamed_to_same_output_name_raises_collision() -> None:
    """Two kinds renamed to the same output name raises BaseNameCollision."""
    config = BaseConfig(
        rename=[
            RenameEntry(table="records__patient", name="people"),
            RenameEntry(table="records__doctor", name="people"),
        ]
    )
    with pytest.raises(BaseNameCollision):
        build_base_plan(_spanning_sidecar(), config, discard_notice_sink)


def test_two_columns_renamed_to_same_name_raises_collision() -> None:
    """Two columns of one table renamed to the same name raises BaseNameCollision."""
    config = BaseConfig(
        rename=[
            RenameEntry(
                table="records__doctor",
                columns={"created_sim_time": "id"},
            )
        ]
    )
    with pytest.raises(BaseNameCollision):
        build_base_plan(_spanning_sidecar(), config, discard_notice_sink)


def test_rename_producing_reserved_table_name_raises_export_error() -> None:
    """A rename producing a reserved table name raises ExportError."""
    config = BaseConfig(
        rename=[RenameEntry(table="records__doctor", name="_export_meta")]
    )
    with pytest.raises(ExportError):
        build_base_plan(_spanning_sidecar(), config, discard_notice_sink)


def test_rename_producing_reserved_rows_suffix_raises_export_error() -> None:
    """A rename producing a `*__rows` output table name raises ExportError."""
    config = BaseConfig(
        rename=[RenameEntry(table="records__doctor", name="doctor__rows")]
    )
    with pytest.raises(ExportError):
        build_base_plan(_spanning_sidecar(), config, discard_notice_sink)


def test_rename_producing_reserved_presentation_column_raises_export_error() -> None:
    """A rename producing the reserved `last_mutation_sim_time` output column
    name raises ExportError, naming the sim-internal presentation posture."""
    config = BaseConfig(
        rename=[
            RenameEntry(
                table="records__doctor",
                columns={"created_sim_time": "last_mutation_sim_time"},
            )
        ]
    )
    with pytest.raises(ExportError) as exc_info:
        build_base_plan(_spanning_sidecar(), config, discard_notice_sink)
    assert str(exc_info.value) == (
        "table 'doctor': column 'last_mutation_sim_time' names the reserved"
        " last_mutation_sim_time column — it is sim-internal bookkeeping and"
        " is never emitted by base"
    )


def test_rename_producing_reserved_column_name_raises_export_error() -> None:
    """A rename producing the reserved `__valid_from_ns` output column name
    raises ExportError, naming the incremental bookkeeping collision."""
    config = BaseConfig(
        rename=[
            RenameEntry(
                table="records__doctor",
                columns={"active": "__valid_from_ns"},
            )
        ]
    )
    with pytest.raises(ExportError) as exc_info:
        build_base_plan(_spanning_sidecar(), config, discard_notice_sink)
    assert str(exc_info.value) == (
        "table 'doctor': column '__valid_from_ns' is reserved under incremental export"
    )
