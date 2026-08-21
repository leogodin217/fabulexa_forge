"""Tests for build_base_plan: kind enumeration, presentation, exclude/rename
resolution, and collision checks.

Sidecars are built in-memory via Sidecar.from_raw (no DuckDB needed — plan
building reads only the sidecar), keeping each fixture minimal and focused.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import (
    BaseConfig,
    BaseRenderDecl,
    DateParseElection,
    ExcludeDecl,
    RenameEntry,
)
from fabulexa_forge.errors import (
    BaseExcludeUnresolved,
    BaseNameCollision,
    BaseRenameSliceOnly,
    BaseRenameUnresolved,
    DateParseSourceColumn,
    ExportError,
    RenderKeyResolves,
    TemporalRenderRequiresAnchor,
)
from fabulexa_forge.exporters.base.plan import (
    NOTICE_REFERENCE_KEY_TARGET_ABSENT,
    BaseTableSpec,
    build_base_plan,
    resolve_base_table_keys,
)
from fabulexa_forge.exporters.query_spec import TableKeys
from fabulexa_forge.reader.errors import PresentationKeysInvalidError
from fabulexa_forge.reader.sidecar import Sidecar

#: A fixed effective anchor for render-election tests — the same shape
#: `resolve_effective_anchor` would produce for a UTC sidecar runtime.
_ANCHOR = EffectiveAnchor(
    start_instant=datetime(2024, 1, 1, tzinfo=timezone.utc), timezone=ZoneInfo("UTC")
)

# ---------------------------------------------------------------------------
# Sidecar-building helpers
# ---------------------------------------------------------------------------


def _col(
    name: str,
    type_: str = "VARCHAR",
    history_tracked: bool | None = None,
    temporal_class: str | None = None,
    references: str | None = None,
) -> dict[str, object]:
    """Build a raw sidecar column entry."""
    col: dict[str, object] = {"name": name, "type": type_}
    if history_tracked is not None:
        col["history_tracked"] = history_tracked
    if temporal_class is not None:
        col["temporal_class"] = temporal_class
    if references is not None:
        col["references"] = references
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
    presentation_keys: dict[str, object] | None = None,
) -> Sidecar:
    """Build a Sidecar directly from a raw base.json-shaped mapping."""
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "tables": tables,
    }
    if enum_domains is not None:
        raw["enum_domains"] = enum_domains
    if presentation_keys is not None:
        raw["presentation_keys"] = presentation_keys
    return Sidecar.from_raw(raw)


def _raw_key_space(
    space_class: str, *, prefix: str = "", width: int = 0
) -> dict[str, object]:
    """A raw key_space object of the given digit-rendered class."""
    return {"class": space_class, "prefix": prefix, "width": width}


def _raw_counter_key(prefix: str = "") -> dict[str, object]:
    """A conformant counter-class raw partition_key (emit/false/false)."""
    return {
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
        "key_space": _raw_key_space("counter", prefix=prefix, width=3),
    }


def _raw_record_index_key() -> dict[str, object]:
    """A conformant record_index-class raw partition_key (branch/true/true)."""
    return {
        "unique_within": "branch",
        "branch_stable": True,
        "slice_stable": True,
        "key_space": _raw_key_space("record_index", width=4),
    }


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


def _target_records_table() -> dict[str, object]:
    """Build a plain `target`-kind records table with no reference properties."""
    return _records_table("target", [])


def _reference_col(name: str, target_kind: str) -> dict[str, object]:
    """Build a non-tracked (constant) reference-annotated prop__ column."""
    return _col(
        name,
        history_tracked=False,
        temporal_class="constant",
        references=target_kind,
    )


def _reference_kind_sidecar() -> Sidecar:
    """`actor` references `target` via two surviving reference properties."""
    actor_table = _records_table(
        "actor",
        [
            _reference_col("prop__lead_id", "target"),
            _col("ref_index__lead_id", "BIGINT"),
            _reference_col("prop__backup_id", "target"),
            _col("ref_index__backup_id", "BIGINT"),
        ],
    )
    return _sidecar(tables=[actor_table, _target_records_table()])


def _absent_target_sidecar() -> Sidecar:
    """`actor` references a `ghost` kind absent from the sidecar."""
    actor_table = _records_table(
        "actor",
        [
            _reference_col("prop__ghost_id", "ghost"),
            _col("ref_index__ghost_id", "BIGINT"),
        ],
    )
    return _sidecar(tables=[actor_table])


def _excluded_target_sidecar() -> Sidecar:
    """`actor` references `target`; `target` itself is excludable from the plan."""
    actor_table = _records_table(
        "actor",
        [
            _reference_col("prop__lead_id", "target"),
            _col("ref_index__lead_id", "BIGINT"),
        ],
    )
    return _sidecar(tables=[actor_table, _target_records_table()])


def _slice_only_reference_sidecar() -> Sidecar:
    """`actor`'s sole reference property is non-exempt slice_only."""
    actor_table = _records_table(
        "actor",
        [
            _col(
                "prop__lead_id",
                history_tracked=False,
                temporal_class="slice_only",
                references="target",
            ),
            _col("ref_index__lead_id", "BIGINT"),
        ],
    )
    return _sidecar(tables=[actor_table, _target_records_table()])


def _render_election_sidecar() -> Sidecar:
    """A `patient` kind carrying a VARCHAR `prop__signup_date` payload (a
    date_parse candidate) and a non-VARCHAR `prop__age` payload (a
    DateParseSourceColumn negative case)."""
    patient_table = _records_table(
        "patient",
        [
            _col(
                "prop__signup_date",
                "VARCHAR",
                history_tracked=False,
                temporal_class="constant",
            ),
            _col(
                "prop__age", "BIGINT", history_tracked=False, temporal_class="constant"
            ),
        ],
    )
    return _sidecar(tables=[patient_table])


def _notice_order_sidecar() -> Sidecar:
    """Two kinds, `alpha` then `beta`, each contributing a slice-only and/or
    an absent-target-reference notice — checks kind-then-column notice
    ordering: `alpha`'s slice-only notice, then its absent-target notice,
    then `beta`'s absent-target notice."""
    alpha_table = _records_table(
        "alpha",
        [
            _col("prop__tier", history_tracked=False, temporal_class="slice_only"),
            _reference_col("prop__ghost_id", "ghost"),
            _col("ref_index__ghost_id", "BIGINT"),
        ],
    )
    beta_table = _records_table(
        "beta",
        [
            _reference_col("prop__ghost2_id", "ghost2"),
            _col("ref_index__ghost2_id", "BIGINT"),
        ],
    )
    return _sidecar(tables=[alpha_table, beta_table])


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


# ---------------------------------------------------------------------------
# reference-key resolution
# ---------------------------------------------------------------------------


def test_reference_keys_resolved_in_column_order() -> None:
    """reference_keys resolves in sidecar column-declaration order, each
    carrying the bare property name and target kind."""
    plan = build_base_plan(_reference_kind_sidecar(), None, discard_notice_sink)
    spec = next(t for t in plan.tables if t.kind == "actor")
    assert [(rk.property_name, rk.target_kind) for rk in spec.reference_keys] == [
        ("lead_id", "target"),
        ("backup_id", "target"),
    ]


def test_kind_with_no_reference_property_yields_empty_reference_keys() -> None:
    """A kind with no reference property resolves reference_keys == ()."""
    plan = build_base_plan(_spanning_sidecar(), None, discard_notice_sink)
    spec = next(t for t in plan.tables if t.kind == "patient")
    assert spec.reference_keys == ()


def test_column_renames_carries_key_defaults() -> None:
    """column_renames carries record_index -> <kind>_key and
    ref_index__<p> -> <p>_key defaults."""
    plan = build_base_plan(_reference_kind_sidecar(), None, discard_notice_sink)
    spec = next(t for t in plan.tables if t.kind == "actor")
    assert spec.column_renames["record_index"] == "actor_key"
    assert spec.column_renames["ref_index__lead_id"] == "lead_id_key"
    assert spec.column_renames["ref_index__backup_id"] == "backup_id_key"


def test_rename_overrides_key_defaults() -> None:
    """A rename.columns entry overrides each key default independently."""
    config = BaseConfig(
        rename=[
            RenameEntry(
                table="records__actor",
                columns={
                    "record_index": "actor_sk",
                    "ref_index__lead_id": "lead_sk",
                },
            )
        ]
    )
    plan = build_base_plan(_reference_kind_sidecar(), config, discard_notice_sink)
    spec = next(t for t in plan.tables if t.kind == "actor")
    assert spec.column_renames["record_index"] == "actor_sk"
    assert spec.column_renames["ref_index__lead_id"] == "lead_sk"
    assert spec.column_renames["ref_index__backup_id"] == "backup_id_key"


def test_rename_ref_index_slice_only_omitted_raises() -> None:
    """Renaming ref_index__<p> where prop__<p> is slice_only-omitted raises
    BaseRenameSliceOnly."""
    config = BaseConfig(
        rename=[
            RenameEntry(
                table="records__actor",
                columns={"ref_index__lead_id": "lead_sk"},
            )
        ]
    )
    with pytest.raises(BaseRenameSliceOnly):
        build_base_plan(_slice_only_reference_sidecar(), config, discard_notice_sink)


def test_rename_ref_index_non_reference_property_raises_unresolved() -> None:
    """Renaming ref_index__<p> where <p> is not a reference property raises
    BaseRenameUnresolved."""
    config = BaseConfig(
        rename=[RenameEntry(table="records__doctor", columns={"ref_index__name": "x"})]
    )
    with pytest.raises(BaseRenameUnresolved):
        build_base_plan(_spanning_sidecar(), config, discard_notice_sink)


def test_rename_ref_index_absent_target_raises_unresolved() -> None:
    """Renaming ref_index__<p> where the target kind has no records table
    raises BaseRenameUnresolved."""
    config = BaseConfig(
        rename=[
            RenameEntry(
                table="records__actor",
                columns={"ref_index__ghost_id": "x"},
            )
        ]
    )
    with pytest.raises(BaseRenameUnresolved):
        build_base_plan(_absent_target_sidecar(), config, discard_notice_sink)


def test_absent_target_yields_no_reference_key_and_one_notice() -> None:
    """A property whose target kind has no records table yields no
    ReferenceKey entry and one reference-key-target-absent notice naming the
    kind, the property, and the absent target kind."""
    sink = RecordingNoticeSink()
    plan = build_base_plan(_absent_target_sidecar(), None, sink)
    spec = plan.tables[0]
    assert spec.reference_keys == ()
    notices = [n for n in sink.notices if n.code == NOTICE_REFERENCE_KEY_TARGET_ABSENT]
    assert len(notices) == 1
    assert "actor" in notices[0].message
    assert "ghost_id" in notices[0].message
    assert "ghost" in notices[0].message


def test_notice_order_follows_table_then_column_order() -> None:
    """Notice order follows sidecar table then column order, alongside the
    slice-only-column-omitted notices: a kind's slice-only notices precede its
    reference-key-target-absent notices, kind order otherwise."""
    sink = RecordingNoticeSink()
    build_base_plan(_notice_order_sidecar(), None, sink)
    codes_and_messages = [(n.code, n.message) for n in sink.notices]
    assert len(codes_and_messages) == 3
    assert codes_and_messages[0][0] == "slice-only-column-omitted"
    assert "alpha" in codes_and_messages[0][1]
    assert codes_and_messages[1][0] == NOTICE_REFERENCE_KEY_TARGET_ABSENT
    assert "alpha" in codes_and_messages[1][1]
    assert "ghost_id" in codes_and_messages[1][1]
    assert codes_and_messages[2][0] == NOTICE_REFERENCE_KEY_TARGET_ABSENT
    assert "beta" in codes_and_messages[2][1]


def test_excluded_target_kind_reference_key_still_present() -> None:
    """An excluded target kind is NOT absent: its records table is still in
    the sidecar, so the edge key is still emitted."""
    config = BaseConfig(exclude=ExcludeDecl(kinds=["target"]))
    plan = build_base_plan(_excluded_target_sidecar(), config, discard_notice_sink)
    assert [t.kind for t in plan.tables] == ["actor"]
    spec = plan.tables[0]
    assert [(rk.property_name, rk.target_kind) for rk in spec.reference_keys] == [
        ("lead_id", "target")
    ]


def test_rename_colliding_with_resolved_key_name_raises_collision() -> None:
    """Renaming another column to the resolved <kind>_key name raises
    BaseNameCollision."""
    config = BaseConfig(
        rename=[
            RenameEntry(
                table="records__doctor",
                columns={"created_sim_time": "doctor_key"},
            )
        ]
    )
    with pytest.raises(BaseNameCollision):
        build_base_plan(_spanning_sidecar(), config, discard_notice_sink)


def test_rename_record_index_to_reserved_name_raises_export_error() -> None:
    """Renaming record_index to a reserved name raises ExportError."""
    config = BaseConfig(
        rename=[
            RenameEntry(
                table="records__doctor",
                columns={"record_index": "__valid_from_ns"},
            )
        ]
    )
    with pytest.raises(ExportError):
        build_base_plan(_spanning_sidecar(), config, discard_notice_sink)


# ---------------------------------------------------------------------------
# resolve_base_table_keys
# ---------------------------------------------------------------------------


def _ward_table(presentation_id: bool = True) -> dict[str, object]:
    """A flat 'ward' kind carrying a constant property and presentation_id."""
    return _records_table(
        "ward",
        [_col("prop__name", history_tracked=False, temporal_class="constant")],
        presentation_id=presentation_id,
    )


def _claimed_flat_sidecar() -> Sidecar:
    """A flat 'ward' kind carrying a whole-column presentation_keys claim."""
    return _sidecar(
        tables=[_ward_table()],
        presentation_keys={"ward": {"key": _raw_counter_key()}},
    )


def _unclaimed_sidecar() -> Sidecar:
    """A flat 'ward' kind with no presentation_keys block at all."""
    return _sidecar(tables=[_ward_table(presentation_id=False)])


def _claimed_partitioned_sidecar(with_unique_within: bool) -> Sidecar:
    """A 'ward' kind carrying a partitioned presentation_keys entry (base
    ignores the sub-type split; the rollup is what resolve_base_table_keys
    consults).

    `with_unique_within=True`: two record_index sub-types sharing a
    prefix/width — pairwise union-safe, the algebra derives a 'branch' claim.
    `with_unique_within=False`: two counter sub-types sharing an empty
    prefix — not pairwise union-safe, the algebra derives no claim
    (unique_within omitted, matching the algebra's None)."""
    if with_unique_within:
        rollup: dict[str, object] = {
            "sub_types": {
                "a": _raw_record_index_key(),
                "b": _raw_record_index_key(),
            },
            "unique_within": "branch",
            "branch_stable": True,
            "slice_stable": True,
        }
    else:
        rollup = {
            "sub_types": {
                "a": _raw_counter_key(),
                "b": _raw_counter_key(),
            },
            "branch_stable": False,
            "slice_stable": False,
        }
    return _sidecar(
        tables=[_ward_table()],
        presentation_keys={"ward": rollup},
        enum_domains={"ward": {"ward_type": ["a", "b"]}},
    )


def _incoherent_sidecar() -> Sidecar:
    """A presentation_keys block naming a kind absent from the sidecar's
    presentation_id-carrying tables — refused at read time."""
    return _sidecar(
        tables=[_ward_table()],
        presentation_keys={"ghost": {"key": _raw_counter_key()}},
    )


def _spec_for(sidecar: Sidecar, config: BaseConfig | None = None) -> BaseTableSpec:
    """Build the sole BaseTableSpec of a single-kind sidecar's plan."""
    plan = build_base_plan(sidecar, config, discard_notice_sink)
    assert len(plan.tables) == 1
    return plan.tables[0]


def test_resolve_base_table_keys_flat_claimed_kind() -> None:
    """A flat claimed kind: PK <kind>_key, unique id + presentation_id."""
    sidecar = _claimed_flat_sidecar()
    spec = _spec_for(sidecar)
    keys = resolve_base_table_keys(sidecar, spec)
    assert keys == TableKeys(
        primary_key=("ward_key",),
        unique=(("id",), ("presentation_id",)),
    )


def test_resolve_base_table_keys_renamed_columns_resolve_to_output_names() -> None:
    """A rename entry's post-rename names appear in the resolved keys."""
    sidecar = _claimed_flat_sidecar()
    config = BaseConfig(
        rename=[
            RenameEntry(
                table="records__ward",
                columns={"record_id": "ward_id", "presentation_id": "code"},
            )
        ]
    )
    spec = _spec_for(sidecar, config)
    keys = resolve_base_table_keys(sidecar, spec)
    assert keys == TableKeys(
        primary_key=("ward_key",),
        unique=(("ward_id",), ("code",)),
    )


def test_resolve_base_table_keys_partitioned_rollup_claim_declares_presentation_id() -> (
    None
):
    """A partitioned kind's rollup with a non-None unique_within declares
    presentation_id."""
    sidecar = _claimed_partitioned_sidecar(with_unique_within=True)
    spec = _spec_for(sidecar)
    keys = resolve_base_table_keys(sidecar, spec)
    assert keys == TableKeys(
        primary_key=("ward_key",),
        unique=(("id",), ("presentation_id",)),
    )


def test_resolve_base_table_keys_rollup_without_unique_within_not_declared() -> None:
    """A rollup with no unique_within (no derivable claim) declares identity
    keys only."""
    sidecar = _claimed_partitioned_sidecar(with_unique_within=False)
    spec = _spec_for(sidecar)
    keys = resolve_base_table_keys(sidecar, spec)
    assert keys == TableKeys(primary_key=("ward_key",), unique=(("id",),))


def test_resolve_base_table_keys_kind_absent_from_block_identity_only() -> None:
    """A kind absent from a present block declares identity keys only."""
    sidecar = _sidecar(
        tables=[_ward_table(), _records_table("bed", [])],
        presentation_keys={"ward": {"key": _raw_counter_key()}},
    )
    plan = build_base_plan(sidecar, None, discard_notice_sink)
    bed_spec = next(t for t in plan.tables if t.kind == "bed")
    keys = resolve_base_table_keys(sidecar, bed_spec)
    assert keys == TableKeys(primary_key=("bed_key",), unique=(("id",),))


def test_resolve_base_table_keys_block_absent_identity_only() -> None:
    """No presentation_keys block at all -> identity keys only."""
    sidecar = _unclaimed_sidecar()
    spec = _spec_for(sidecar)
    keys = resolve_base_table_keys(sidecar, spec)
    assert keys == TableKeys(primary_key=("ward_key",), unique=(("id",),))


def test_resolve_base_table_keys_incoherent_block_raises() -> None:
    """An incoherent presentation_keys block raises at plan time, propagated
    from the strict accessor."""
    sidecar = _incoherent_sidecar()
    spec = _spec_for(sidecar)
    with pytest.raises(PresentationKeysInvalidError):
        resolve_base_table_keys(sidecar, spec)


# ---------------------------------------------------------------------------
# `render` / `date_parse`: temporal rendering elections
# ---------------------------------------------------------------------------


def test_render_election_on_lifecycle_instant_resolves_with_anchor() -> None:
    """A render election on created_sim_time resolves into spec.render with
    an anchor present."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__patient", render={"created_sim_time": "date"}
            )
        ]
    )
    plan = build_base_plan(
        _render_election_sidecar(), config, discard_notice_sink, anchor=_ANCHOR
    )
    spec = next(t for t in plan.tables if t.kind == "patient")
    assert spec.render == (("created_sim_time", "date"),)


def test_render_election_composes_with_rename_keys_stay_pre_default() -> None:
    """render and rename both target one table; render's key stays the
    pre-default column identity regardless of rename's output renaming."""
    config = BaseConfig(
        rename=[
            RenameEntry(
                table="records__patient", columns={"created_sim_time": "joined_at"}
            )
        ],
        render=[
            BaseRenderDecl(
                table="records__patient", render={"created_sim_time": "timestamptz"}
            )
        ],
    )
    plan = build_base_plan(
        _render_election_sidecar(), config, discard_notice_sink, anchor=_ANCHOR
    )
    spec = next(t for t in plan.tables if t.kind == "patient")
    assert spec.render == (("created_sim_time", "timestamptz"),)
    assert spec.column_renames["created_sim_time"] == "joined_at"


def test_render_key_last_mutation_sim_time_refused() -> None:
    """A render key of last_mutation_sim_time is refused — outside the base
    key domain, the mode never emits it."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__patient", render={"last_mutation_sim_time": "date"}
            )
        ]
    )
    with pytest.raises(BaseRenameUnresolved):
        build_base_plan(
            _render_election_sidecar(), config, discard_notice_sink, anchor=_ANCHOR
        )


def test_render_key_not_instant_column_refused() -> None:
    """A render key naming a non-instant column (a prop__ payload) raises
    RenderKeyResolves."""
    config = BaseConfig(
        render=[BaseRenderDecl(table="records__patient", render={"prop__age": "date"})]
    )
    with pytest.raises(RenderKeyResolves):
        build_base_plan(
            _render_election_sidecar(), config, discard_notice_sink, anchor=_ANCHOR
        )


def test_render_key_unresolved_column_refused() -> None:
    """A render key naming no column of the kind's table at all raises
    BaseRenameUnresolved."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__patient", render={"prop__nonexistent": "date"}
            )
        ]
    )
    with pytest.raises(BaseRenameUnresolved):
        build_base_plan(
            _render_election_sidecar(), config, discard_notice_sink, anchor=_ANCHOR
        )


def test_render_election_with_no_anchor_refused() -> None:
    """An election with no resolved anchor is refused —
    TemporalRenderRequiresAnchor, base's anchor is optional."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(table="records__patient", render={"deactivated_at": "date"})
        ]
    )
    with pytest.raises(TemporalRenderRequiresAnchor, match="deactivated_at"):
        build_base_plan(_render_election_sidecar(), config, discard_notice_sink)


def test_render_table_not_surviving_raises() -> None:
    """A render entry whose table is not a surviving records__<kind> raises
    BaseRenameUnresolved."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__nonexistent", render={"created_sim_time": "date"}
            )
        ]
    )
    with pytest.raises(BaseRenameUnresolved):
        build_base_plan(
            _render_election_sidecar(), config, discard_notice_sink, anchor=_ANCHOR
        )


def test_date_parse_on_varchar_prop_resolves_without_anchor() -> None:
    """date_parse on a VARCHAR prop__ column resolves — a parse reads no
    sim_time, so it carries no anchor requirement."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__patient",
                render={"prop__signup_date": {"date_parse": "%Y-%m-%d"}},
            )
        ]
    )
    plan = build_base_plan(_render_election_sidecar(), config, discard_notice_sink)
    spec = next(t for t in plan.tables if t.kind == "patient")
    assert spec.render == (
        ("prop__signup_date", DateParseElection(date_parse="%Y-%m-%d")),
    )


def test_date_parse_on_non_varchar_prop_refused() -> None:
    """date_parse on a non-VARCHAR prop__ column raises DateParseSourceColumn."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__patient",
                render={"prop__age": {"date_parse": "%Y-%m-%d"}},
            )
        ]
    )
    with pytest.raises(DateParseSourceColumn):
        build_base_plan(_render_election_sidecar(), config, discard_notice_sink)


def test_date_parse_on_slice_only_column_refused() -> None:
    """A date_parse source that is slice_only is refused — the mode's
    omission posture composes with the parse's refusal."""
    sidecar = _slice_only_sidecar()
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__patient",
                render={"prop__loyalty_tier": {"date_parse": "%Y-%m-%d"}},
            )
        ]
    )
    with pytest.raises(BaseRenameSliceOnly):
        build_base_plan(sidecar, config, discard_notice_sink)
