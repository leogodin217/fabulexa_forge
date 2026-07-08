"""Tests for build_source_plan: classification, sub-type split, presentation,
exclude/rename resolution, and collision checks.

Sidecars are built in-memory via Sidecar.from_raw (no DuckDB needed — plan
building reads only the sidecar), keeping each fixture minimal and focused.
"""

from __future__ import annotations

import pytest

from fabulexa_export import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_export.config.models import ExcludeDecl, RenameEntry, SourceConfig
from fabulexa_export.errors import (
    ExportError,
    SourceExcludeUnresolved,
    SourceHistoryTrackedRequired,
    SourceNameCollision,
    SourceRecordRolesRequired,
    SourceRenameUnresolved,
    SourceRoleUnknown,
    SourceSubtypesUndeclared,
)
from fabulexa_export.exporters.source.plan import build_source_plan
from fabulexa_export.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# Sidecar-building helpers
# ---------------------------------------------------------------------------


def _col(
    name: str,
    type_: str = "VARCHAR",
    history_tracked: bool | None = None,
) -> dict[str, object]:
    """Build a raw sidecar column entry."""
    col: dict[str, object] = {"name": name, "type": type_}
    if history_tracked is not None:
        col["history_tracked"] = history_tracked
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


def _membership_table(
    owner_kind: str,
    prop: str,
    extra_cols: list[dict[str, object]],
    rows: int = 1,
) -> dict[str, object]:
    """Build a raw membership__<owner_kind>__<prop> table entry."""
    cols = [
        _col("fork_path"),
        _col("record_id"),
        _col("joined_sim_time", "BIGINT"),
        _col("left_sim_time", "BIGINT"),
    ]
    cols += extra_cols
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
    record_roles: dict[str, object] | None = None,
    enum_domains: dict[str, object] | None = None,
) -> Sidecar:
    """Build a Sidecar directly from a raw base.json-shaped mapping."""
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "tables": tables,
    }
    if record_roles is not None:
        raw["record_roles"] = record_roles
    if enum_domains is not None:
        raw["enum_domains"] = enum_domains
    return Sidecar.from_raw(raw)


def _spanning_sidecar() -> Sidecar:
    """A sidecar spanning all four genres: changelog, reference, transaction, junction.

    - records__visit: tracked (prop__status) -> changelog; owns a junction table.
    - records__location: untracked, dimension role -> reference.
    - records__order: untracked, fact role -> transaction.
    - records__actor: untracked, object-registry role -> splits (consultant/nurse).
    - membership__visit__team: -> junction.
    """
    visit_table = _records_table(
        "visit",
        [
            _col("prop__status", history_tracked=True),
            _col("prop__notes", history_tracked=False),
        ],
    )
    location_table = _records_table(
        "location",
        [
            _col("prop__name", history_tracked=False),
            _col("prop__region", history_tracked=False),
        ],
    )
    order_table = _records_table(
        "order",
        [_col("prop__amount", "DOUBLE", history_tracked=False)],
    )
    actor_table = _records_table(
        "actor",
        [
            _col("prop__actor_type", history_tracked=False),
            _col("prop__name", history_tracked=False),
        ],
    )
    team_membership = _membership_table(
        "visit",
        "team",
        [
            _col("elem__role_name"),
            _col("member__actor__kind"),
            _col("member__actor__id"),
        ],
    )
    return _sidecar(
        tables=[
            visit_table,
            location_table,
            order_table,
            actor_table,
            team_membership,
            _history_table(),
        ],
        record_roles={
            "location": "dimension",
            "order": "fact",
            "actor": {"consultant": "dimension", "nurse": "fact"},
        },
        enum_domains={"actor": {"actor_type": ["consultant", "nurse"]}},
    )


def _tracked_sidecar(presentation_id: bool = False) -> Sidecar:
    """A sidecar with a single tracked kind (changelog genre): one tracked and
    one untracked scalar property, for snapshot-delivery column-shape tests.
    """
    widget_table = _records_table(
        "widget",
        [
            _col("prop__status", history_tracked=True),
            _col("prop__notes", history_tracked=False),
        ],
        presentation_id=presentation_id,
    )
    return _sidecar(tables=[widget_table], record_roles={})


# ---------------------------------------------------------------------------
# Classification precedence
# ---------------------------------------------------------------------------


def test_tracked_kind_classifies_changelog_regardless_of_role() -> None:
    """A kind with a history_tracked=True column classifies as changelog."""
    plan = build_source_plan(_spanning_sidecar(), None)
    spec = next(s for s in plan if s.source_table == "records__visit")
    assert spec.genre == "changelog"


def test_untracked_dimension_role_classifies_reference() -> None:
    """An untracked kind with a 'dimension' role classifies as reference."""
    plan = build_source_plan(_spanning_sidecar(), None)
    spec = next(s for s in plan if s.source_table == "records__location")
    assert spec.genre == "reference"


def test_untracked_fact_role_classifies_transaction() -> None:
    """An untracked kind with a 'fact' role classifies as transaction."""
    plan = build_source_plan(_spanning_sidecar(), None)
    spec = next(s for s in plan if s.source_table == "records__order")
    assert spec.genre == "transaction"


def test_history_tracked_none_treated_as_untracked() -> None:
    """A column with an absent history_tracked flag (None) is untracked (`is True`)."""
    location_no_flag = {
        "name": "records__location",
        "category": "records",
        "record_kind": "location",
        "columns": [
            _col("fork_path"),
            _col("record_id"),
            _col("created_sim_time", "BIGINT"),
            _col("active", "BOOLEAN"),
            _col("deactivated_at", "BIGINT"),
            _col("last_mutation_sim_time", "BIGINT"),
            {"name": "prop__name", "type": "VARCHAR"},  # no history_tracked key at all
        ],
        "rows": 1,
    }
    sidecar = _sidecar(
        tables=[
            _records_table("visit", [_col("prop__status", history_tracked=True)]),
            location_no_flag,
        ],
        record_roles={"location": "dimension"},
    )
    plan = build_source_plan(sidecar, None)
    spec = next(s for s in plan if s.source_table == "records__location")
    assert spec.genre == "reference"


def test_membership_table_classifies_junction() -> None:
    """A membership table always classifies as junction."""
    plan = build_source_plan(_spanning_sidecar(), None)
    spec = next(s for s in plan if s.source_table == "membership__visit__team")
    assert spec.genre == "junction"


def test_history_table_never_a_plan_entry() -> None:
    """The fixed-category history table never yields a plan entry."""
    plan = build_source_plan(_spanning_sidecar(), None)
    assert all(s.source_table != "history" for s in plan)


# ---------------------------------------------------------------------------
# The sub-type split
# ---------------------------------------------------------------------------


def test_untracked_object_registry_kind_splits_per_declared_sub_type() -> None:
    """An untracked object-registry kind yields one unit per declared sub-type."""
    plan = build_source_plan(_spanning_sidecar(), None)
    actor_specs = [s for s in plan if s.source_table == "records__actor"]
    assert [s.sub_type for s in actor_specs] == ["consultant", "nurse"]
    consultant = next(s for s in actor_specs if s.sub_type == "consultant")
    nurse = next(s for s in actor_specs if s.sub_type == "nurse")
    assert consultant.genre == "reference"
    assert nurse.genre == "transaction"


def test_split_unit_drops_discriminator_column() -> None:
    """A split unit's own <kind>_type discriminator is dropped, not renamed."""
    plan = build_source_plan(_spanning_sidecar(), None)
    consultant = next(
        s
        for s in plan
        if s.source_table == "records__actor" and s.sub_type == "consultant"
    )
    assert all(src != "prop__actor_type" for src, _ in consultant.columns)


def test_tracked_subtyped_kind_single_changelog_with_discriminator_retained() -> None:
    """A tracked sub-typed kind is one changelog table; discriminator retained."""
    shift_table = _records_table(
        "shift",
        [
            _col("prop__shift_type", history_tracked=False),
            _col("prop__status", history_tracked=True),
        ],
    )
    sidecar = _sidecar(
        tables=[shift_table],
        record_roles={},  # present (registry required unconditionally) but unused
        enum_domains={"shift": {"shift_type": ["day", "night"]}},
    )
    plan = build_source_plan(sidecar, None)
    assert len(plan) == 1
    spec = plan[0]
    assert spec.genre == "changelog"
    assert spec.sub_type is None
    assert ("prop__shift_type", "shift_type") in spec.columns


def test_bare_role_subtyped_kind_single_spec_with_discriminator_retained() -> None:
    """A bare-role sub-typed untracked kind is one table; discriminator retained."""
    entity_table = _records_table(
        "entity",
        [
            _col("prop__entity_type", history_tracked=False),
            _col("prop__name", history_tracked=False),
        ],
    )
    sidecar = _sidecar(
        tables=[entity_table],
        record_roles={"entity": "dimension"},
        enum_domains={"entity": {"entity_type": ["consultant", "nurse"]}},
    )
    plan = build_source_plan(sidecar, None)
    assert len(plan) == 1
    spec = plan[0]
    assert spec.genre == "reference"
    assert spec.sub_type is None
    assert ("prop__entity_type", "entity_type") in spec.columns


def test_declared_subtype_gets_spec_even_with_zero_rows() -> None:
    """A declared sub-type materializing zero rows still gets a spec."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "actor",
                [
                    _col("prop__actor_type", history_tracked=False),
                    _col("prop__name", history_tracked=False),
                ],
                rows=0,
            )
        ],
        record_roles={"actor": {"consultant": "dimension", "nurse": "fact"}},
        enum_domains={"actor": {"actor_type": ["consultant", "nurse"]}},
    )
    plan = build_source_plan(sidecar, None)
    assert {s.sub_type for s in plan} == {"consultant", "nurse"}


def test_untracked_kind_with_no_role_raises_source_role_unknown() -> None:
    """An untracked kind absent from record_roles raises SourceRoleUnknown."""
    sidecar = _sidecar(
        tables=[_records_table("widget", [_col("prop__name", history_tracked=False)])],
        record_roles={},
    )
    with pytest.raises(SourceRoleUnknown):
        build_source_plan(sidecar, None)


def test_declared_subtype_absent_from_registry_raises_source_role_unknown() -> None:
    """A sub-type in the enum domain but absent from the registry object raises."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "actor",
                [
                    _col("prop__actor_type", history_tracked=False),
                    _col("prop__name", history_tracked=False),
                ],
            )
        ],
        record_roles={"actor": {"consultant": "dimension"}},  # nurse missing
        enum_domains={"actor": {"actor_type": ["consultant", "nurse"]}},
    )
    with pytest.raises(SourceRoleUnknown):
        build_source_plan(sidecar, None)


def test_object_registry_kind_without_enum_domain_raises_subtypes_undeclared() -> None:
    """An object-registry kind with no <kind>_type enum domain raises."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "actor",
                [
                    _col("prop__actor_type", history_tracked=False),
                    _col("prop__name", history_tracked=False),
                ],
            )
        ],
        record_roles={"actor": {"consultant": "dimension", "nurse": "fact"}},
    )
    with pytest.raises(SourceSubtypesUndeclared):
        build_source_plan(sidecar, None)


def test_sidecar_without_record_roles_raises() -> None:
    """A sidecar with no record_roles registry raises SourceRecordRolesRequired."""
    sidecar = _sidecar(
        tables=[
            _records_table("location", [_col("prop__name", history_tracked=False)])
        ],
    )
    with pytest.raises(SourceRecordRolesRequired):
        build_source_plan(sidecar, None)


def test_sidecar_without_history_tracked_flags_raises() -> None:
    """A sidecar with no history_tracked flags anywhere raises."""
    location_no_flag = {
        "name": "records__location",
        "category": "records",
        "record_kind": "location",
        "columns": [
            _col("fork_path"),
            _col("record_id"),
            _col("created_sim_time", "BIGINT"),
            _col("active", "BOOLEAN"),
            _col("deactivated_at", "BIGINT"),
            _col("last_mutation_sim_time", "BIGINT"),
            {"name": "prop__name", "type": "VARCHAR"},
        ],
        "rows": 1,
    }
    sidecar = _sidecar(
        tables=[location_no_flag],
        record_roles={"location": "dimension"},
    )
    with pytest.raises(SourceHistoryTrackedRequired):
        build_source_plan(sidecar, None)


# ---------------------------------------------------------------------------
# Presentation defaults
# ---------------------------------------------------------------------------


def test_presentation_defaults_reference_genre() -> None:
    """fork_path dropped; record_id->id; lifecycle renamed; prop__ stripped."""
    plan = build_source_plan(_spanning_sidecar(), None)
    spec = next(s for s in plan if s.source_table == "records__location")
    col_map = dict(spec.columns)
    assert "fork_path" not in col_map
    assert col_map["record_id"] == "id"
    assert col_map["created_sim_time"] == "created_at"
    assert col_map["last_mutation_sim_time"] == "updated_at"
    assert col_map["active"] == "active"
    assert col_map["prop__name"] == "name"
    assert col_map["prop__region"] == "region"


def test_presentation_defaults_junction_genre() -> None:
    """fork_path dropped; record_id-><K>_id; elem__/member__ prefix mapping."""
    plan = build_source_plan(_spanning_sidecar(), None)
    spec = next(s for s in plan if s.source_table == "membership__visit__team")
    col_map = dict(spec.columns)
    assert "fork_path" not in col_map
    assert col_map["record_id"] == "visit_id"
    assert col_map["joined_sim_time"] == "joined_at"
    assert col_map["left_sim_time"] == "left_at"
    assert col_map["elem__role_name"] == "role_name"
    assert col_map["member__actor__kind"] == "actor_kind"
    assert col_map["member__actor__id"] == "actor_id"


def test_presentation_defaults_changelog_genre() -> None:
    """op/changed_at/id/prop__ fold column defaults."""
    plan = build_source_plan(_spanning_sidecar(), None)
    spec = next(s for s in plan if s.source_table == "records__visit")
    col_map = dict(spec.columns)
    assert col_map["op"] == "op"
    assert col_map["event_sim_time"] == "changed_at"
    assert col_map["record_id"] == "id"
    assert col_map["prop__status"] == "status"
    assert col_map["prop__notes"] == "notes"
    assert "fork_path" not in col_map


def test_presentation_id_kept_unprefixed() -> None:
    """presentation_id keeps its name verbatim in the reference/transaction render."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "widget",
                [_col("prop__name", history_tracked=False)],
                presentation_id=True,
            )
        ],
        record_roles={"widget": "dimension"},
    )
    plan = build_source_plan(sidecar, None)
    col_map = dict(plan[0].columns)
    assert col_map["presentation_id"] == "presentation_id"


def test_default_table_names() -> None:
    """Unsplit kind -> <kind>; split unit -> <sub_type>; junction -> <K>_<p>."""
    plan = build_source_plan(_spanning_sidecar(), None)
    unsplit_names = {s.source_table: s.name for s in plan if s.sub_type is None}
    assert unsplit_names["records__location"] == "location"
    assert unsplit_names["records__order"] == "order"
    assert unsplit_names["records__visit"] == "visit"
    assert unsplit_names["membership__visit__team"] == "visit_team"
    split_names = {
        s.sub_type: s.name for s in plan if s.source_table == "records__actor"
    }
    assert split_names == {"consultant": "consultant", "nurse": "nurse"}


# ---------------------------------------------------------------------------
# exclude resolution
# ---------------------------------------------------------------------------


def test_exclude_kinds_drops_units_and_owned_membership() -> None:
    """exclude.kinds drops the kind's units and the membership tables it owns."""
    config = SourceConfig(exclude=ExcludeDecl(kinds=["visit"]))
    plan = build_source_plan(_spanning_sidecar(), config)
    assert all(s.source_table != "records__visit" for s in plan)
    assert all(s.source_table != "membership__visit__team" for s in plan)


def test_exclude_tables_drops_named_sidecar_table_only() -> None:
    """exclude.tables on a membership entry drops that junction alone."""
    config = SourceConfig(exclude=ExcludeDecl(tables=["membership__visit__team"]))
    plan = build_source_plan(_spanning_sidecar(), config)
    assert all(s.source_table != "membership__visit__team" for s in plan)
    assert any(s.source_table == "records__visit" for s in plan)


def test_exclude_tables_records_prefix_equivalent_to_kind_exclude() -> None:
    """exclude.tables on a records__<kind> entry behaves like exclude.kinds."""
    config = SourceConfig(exclude=ExcludeDecl(tables=["records__visit"]))
    plan = build_source_plan(_spanning_sidecar(), config)
    assert all(s.source_table != "records__visit" for s in plan)
    assert all(s.source_table != "membership__visit__team" for s in plan)


def test_exclude_kind_unresolved_raises() -> None:
    """An exclude.kinds entry matching nothing raises SourceExcludeUnresolved."""
    config = SourceConfig(exclude=ExcludeDecl(kinds=["nonexistent"]))
    with pytest.raises(SourceExcludeUnresolved):
        build_source_plan(_spanning_sidecar(), config)


def test_exclude_table_unresolved_raises() -> None:
    """An exclude.tables entry matching nothing raises SourceExcludeUnresolved."""
    config = SourceConfig(exclude=ExcludeDecl(tables=["records__nonexistent"]))
    with pytest.raises(SourceExcludeUnresolved):
        build_source_plan(_spanning_sidecar(), config)


# ---------------------------------------------------------------------------
# rename resolution
# ---------------------------------------------------------------------------


def test_rename_table_name_override() -> None:
    """A rename entry's name overrides the default table name."""
    config = SourceConfig(
        rename=[RenameEntry(table="membership__visit__team", name="team_roster")]
    )
    plan = build_source_plan(_spanning_sidecar(), config)
    spec = next(s for s in plan if s.source_table == "membership__visit__team")
    assert spec.name == "team_roster"


def test_rename_column_override_keyed_by_source_name() -> None:
    """A rename entry's columns map overrides a column's output name."""
    config = SourceConfig(
        rename=[
            RenameEntry(table="records__location", columns={"record_id": "location_id"})
        ]
    )
    plan = build_source_plan(_spanning_sidecar(), config)
    spec = next(s for s in plan if s.source_table == "records__location")
    assert dict(spec.columns)["record_id"] == "location_id"


def test_rename_column_override_changelog_fold_name() -> None:
    """A rename entry's columns key may name a canonical-fold column."""
    config = SourceConfig(
        rename=[
            RenameEntry(table="records__visit", columns={"event_sim_time": "event_at"})
        ]
    )
    plan = build_source_plan(_spanning_sidecar(), config)
    spec = next(s for s in plan if s.source_table == "records__visit")
    assert dict(spec.columns)["event_sim_time"] == "event_at"


def test_rename_sub_type_selects_split_unit() -> None:
    """A rename entry's sub_type selects one split unit, leaving the other default."""
    config = SourceConfig(
        rename=[RenameEntry(table="records__actor", sub_type="nurse", name="nurses")]
    )
    plan = build_source_plan(_spanning_sidecar(), config)
    names = {s.sub_type: s.name for s in plan if s.source_table == "records__actor"}
    assert names["nurse"] == "nurses"
    assert names["consultant"] == "consultant"


def test_rename_unresolved_table_raises() -> None:
    """A rename entry naming an unknown table raises SourceRenameUnresolved."""
    config = SourceConfig(rename=[RenameEntry(table="records__nonexistent", name="x")])
    with pytest.raises(SourceRenameUnresolved):
        build_source_plan(_spanning_sidecar(), config)


def test_rename_unresolved_sub_type_raises() -> None:
    """A rename entry naming an undeclared sub_type raises SourceRenameUnresolved."""
    config = SourceConfig(
        rename=[RenameEntry(table="records__actor", sub_type="doctor", name="x")]
    )
    with pytest.raises(SourceRenameUnresolved):
        build_source_plan(_spanning_sidecar(), config)


def test_rename_unresolved_columns_key_raises() -> None:
    """A rename entry's columns key naming an unknown source column raises."""
    config = SourceConfig(
        rename=[
            RenameEntry(table="records__location", columns={"prop__nonexistent": "x"})
        ]
    )
    with pytest.raises(SourceRenameUnresolved):
        build_source_plan(_spanning_sidecar(), config)


# ---------------------------------------------------------------------------
# Collision checks
# ---------------------------------------------------------------------------


def test_two_tables_same_default_name_raises_collision() -> None:
    """A kind named like a junction default collides with the junction table."""
    sidecar = _sidecar(
        tables=[
            _records_table("visit_team", [_col("prop__name", history_tracked=False)]),
            _membership_table("visit", "team", [_col("elem__role_name")]),
        ],
        record_roles={"visit_team": "dimension"},
    )
    with pytest.raises(SourceNameCollision):
        build_source_plan(sidecar, None)


def test_column_collision_prop_id_onto_id_raises() -> None:
    """A prop__id column stripping onto 'id' collides with record_id->id."""
    sidecar = _sidecar(
        tables=[_records_table("widget", [_col("prop__id", history_tracked=False)])],
        record_roles={"widget": "dimension"},
    )
    with pytest.raises(SourceNameCollision):
        build_source_plan(sidecar, None)


def test_column_collision_resolved_by_renaming_source_column() -> None:
    """Renaming the colliding source column resolves the collision."""
    sidecar = _sidecar(
        tables=[_records_table("widget", [_col("prop__id", history_tracked=False)])],
        record_roles={"widget": "dimension"},
    )
    config = SourceConfig(
        rename=[RenameEntry(table="records__widget", columns={"prop__id": "widget_id"})]
    )
    plan = build_source_plan(sidecar, config)
    col_map = dict(plan[0].columns)
    assert col_map["record_id"] == "id"
    assert col_map["prop__id"] == "widget_id"


# ---------------------------------------------------------------------------
# Reserved-name checks
# ---------------------------------------------------------------------------


def _widget_sidecar() -> Sidecar:
    return _sidecar(
        tables=[_records_table("widget", [_col("prop__name", history_tracked=False)])],
        record_roles={"widget": "dimension"},
    )


def test_reserved_table_name_raises() -> None:
    """An output table name equal to a reserved bookkeeping name raises."""
    config = SourceConfig(
        rename=[RenameEntry(table="records__widget", name="_export_meta")]
    )
    with pytest.raises(ExportError):
        build_source_plan(_widget_sidecar(), config)


def test_reserved_table_suffix_raises() -> None:
    """An output table name ending in the reserved suffix raises."""
    config = SourceConfig(
        rename=[RenameEntry(table="records__widget", name="widget__rows")]
    )
    with pytest.raises(ExportError):
        build_source_plan(_widget_sidecar(), config)


def test_reserved_column_name_raises() -> None:
    """An output column named with the reserved bookkeeping column name raises."""
    config = SourceConfig(
        rename=[
            RenameEntry(
                table="records__widget", columns={"record_id": "__valid_from_ns"}
            )
        ]
    )
    with pytest.raises(ExportError):
        build_source_plan(_widget_sidecar(), config)


# ---------------------------------------------------------------------------
# Snapshot delivery (change_delivery: snapshot)
# ---------------------------------------------------------------------------


def test_snapshot_delivery_changelog_columns_are_state_at_shape() -> None:
    """Under snapshot delivery, a changelog kind's columns are the state-at shape;
    genre stays 'changelog'."""
    config = SourceConfig(change_delivery="snapshot")
    plan = build_source_plan(_tracked_sidecar(), config)
    spec = plan[0]
    assert spec.genre == "changelog"
    assert spec.columns == (
        ("record_id", "id"),
        ("created_sim_time", "created_at"),
        ("active", "active"),
        ("deactivated_at", "deactivated_at"),
        ("prop__status", "status"),
        ("prop__notes", "notes"),
    )


def test_snapshot_delivery_omits_fold_columns() -> None:
    """The snapshot shape carries no op / changed_at / updated_at."""
    config = SourceConfig(change_delivery="snapshot")
    plan = build_source_plan(_tracked_sidecar(), config)
    col_map = dict(plan[0].columns)
    assert "op" not in col_map
    assert "event_sim_time" not in col_map
    assert "last_mutation_sim_time" not in col_map


def test_snapshot_delivery_includes_presentation_id_when_carried() -> None:
    """presentation_id is carried, positioned before the payload columns."""
    config = SourceConfig(change_delivery="snapshot")
    plan = build_source_plan(_tracked_sidecar(presentation_id=True), config)
    sources = [src for src, _ in plan[0].columns]
    assert sources == [
        "record_id",
        "created_sim_time",
        "active",
        "deactivated_at",
        "presentation_id",
        "prop__status",
        "prop__notes",
    ]


def test_snapshot_delivery_reference_and_transaction_unaffected() -> None:
    """Reference/transaction genre columns are unchanged under snapshot delivery."""
    config = SourceConfig(change_delivery="snapshot")
    changelog_plan = build_source_plan(_spanning_sidecar(), None)
    snapshot_plan = build_source_plan(_spanning_sidecar(), config)
    for source_table in ("records__location", "records__order"):
        default_cols = next(
            s.columns for s in changelog_plan if s.source_table == source_table
        )
        snapshot_cols = next(
            s.columns for s in snapshot_plan if s.source_table == source_table
        )
        assert default_cols == snapshot_cols


def test_default_changelog_delivery_columns_unchanged() -> None:
    """An explicit change_delivery: changelog yields the same columns as omitting
    the config entirely (Unit 1 behavior unchanged)."""
    implicit_plan = build_source_plan(_tracked_sidecar(), None)
    explicit_config = SourceConfig(change_delivery="changelog")
    explicit_plan = build_source_plan(_tracked_sidecar(), explicit_config)
    assert implicit_plan[0].columns == explicit_plan[0].columns


def test_snapshot_rename_keyed_on_state_at_source_name_resolves() -> None:
    """A rename entry keyed on the state-at source name resolves under snapshot
    delivery."""
    config = SourceConfig(
        change_delivery="snapshot",
        rename=[
            RenameEntry(table="records__widget", columns={"record_id": "widget_id"})
        ],
    )
    plan = build_source_plan(_tracked_sidecar(), config)
    assert dict(plan[0].columns)["record_id"] == "widget_id"


def test_snapshot_rename_keyed_on_fold_name_raises() -> None:
    """A rename entry keyed on a fold name (op) is SourceRenameUnresolved under
    snapshot delivery — the fold names are not this unit's source columns."""
    config = SourceConfig(
        change_delivery="snapshot",
        rename=[RenameEntry(table="records__widget", columns={"op": "operation"})],
    )
    with pytest.raises(SourceRenameUnresolved):
        build_source_plan(_tracked_sidecar(), config)


def test_snapshot_collision_check_runs_over_snapshot_columns() -> None:
    """The collision check runs over the snapshot column set: a prop__id column
    stripping onto 'id' collides with record_id->id."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "widget",
                [
                    _col("prop__status", history_tracked=True),
                    _col("prop__id", history_tracked=False),
                ],
            )
        ],
        record_roles={},
    )
    config = SourceConfig(change_delivery="snapshot")
    with pytest.raises(SourceNameCollision):
        build_source_plan(sidecar, config)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism_repeated_calls_identical() -> None:
    """Repeated calls over the same (sidecar, config) yield an identical result."""
    sidecar = _spanning_sidecar()
    assert build_source_plan(sidecar, None) == build_source_plan(sidecar, None)
