"""Tests for build_source_plan: classification, sub-type split, presentation,
exclude/rename resolution, and collision checks.

Sidecars are built in-memory via Sidecar.from_raw (no DuckDB needed — plan
building reads only the sidecar), keeping each fixture minimal and focused.
"""

from __future__ import annotations

import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink
from _support.sidecar_builder import prop_column

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import ExcludeDecl, RenameEntry, SourceConfig
from fabulexa_forge.errors import (
    ExportError,
    SourceExcludeUnresolved,
    SourceHistoryTrackedRequired,
    SourceNameCollision,
    SourceRecordRolesRequired,
    SourceRenameSliceOnly,
    SourceRenameUnresolved,
    SourceRoleUnknown,
    SourceSubtypesUndeclared,
    SourceUnclassifiedColumn,
)
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.reader.errors import TemporalClassUnavailableError
from fabulexa_forge.reader.sidecar import ColumnSpec, Sidecar

# ---------------------------------------------------------------------------
# Sidecar-building helpers
# ---------------------------------------------------------------------------


def _col(
    name: str,
    type_: str = "VARCHAR",
    history_tracked: bool | None = None,
    temporal_class: str | None = None,
) -> dict[str, object]:
    """Build a raw sidecar column entry.

    Unlike `_support.sidecar_builder.prop_column`, this builder does not validate
    the (history_tracked, temporal_class) pairing — test_plan.py's negative
    fixtures deliberately construct the broken pairings C13 exists to catch.
    """
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
            _col("prop__status", history_tracked=True, temporal_class="tracked"),
            _col("prop__notes", history_tracked=False, temporal_class="constant"),
        ],
    )
    location_table = _records_table(
        "location",
        [
            _col("prop__name", history_tracked=False, temporal_class="constant"),
            _col("prop__region", history_tracked=False, temporal_class="constant"),
        ],
    )
    order_table = _records_table(
        "order",
        [
            _col(
                "prop__amount",
                "DOUBLE",
                history_tracked=False,
                temporal_class="constant",
            )
        ],
    )
    actor_table = _records_table(
        "actor",
        [
            _col("prop__actor_type", history_tracked=False, temporal_class="constant"),
            _col("prop__name", history_tracked=False, temporal_class="constant"),
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
            _col("prop__status", history_tracked=True, temporal_class="tracked"),
            _col("prop__notes", history_tracked=False, temporal_class="constant"),
        ],
        presentation_id=presentation_id,
    )
    return _sidecar(tables=[widget_table], record_roles={})


# ---------------------------------------------------------------------------
# Classification precedence
# ---------------------------------------------------------------------------


def test_tracked_kind_classifies_changelog_regardless_of_role() -> None:
    """A kind with a history_tracked=True column classifies as changelog."""
    plan = build_source_plan(_spanning_sidecar(), None, notice_sink=discard_notice_sink)
    spec = next(s for s in plan if s.source_table == "records__visit")
    assert spec.genre == "changelog"


def test_untracked_dimension_role_classifies_reference() -> None:
    """An untracked kind with a 'dimension' role classifies as reference."""
    plan = build_source_plan(_spanning_sidecar(), None, notice_sink=discard_notice_sink)
    spec = next(s for s in plan if s.source_table == "records__location")
    assert spec.genre == "reference"


def test_untracked_fact_role_classifies_transaction() -> None:
    """An untracked kind with a 'fact' role classifies as transaction."""
    plan = build_source_plan(_spanning_sidecar(), None, notice_sink=discard_notice_sink)
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
            {
                "name": "prop__name",
                "type": "VARCHAR",
                "temporal_class": "constant",
            },  # no history_tracked key at all
        ],
        "rows": 1,
    }
    sidecar = _sidecar(
        tables=[
            _records_table(
                "visit",
                [_col("prop__status", history_tracked=True, temporal_class="tracked")],
            ),
            location_no_flag,
        ],
        record_roles={"location": "dimension"},
    )
    plan = build_source_plan(sidecar, None, notice_sink=discard_notice_sink)
    spec = next(s for s in plan if s.source_table == "records__location")
    assert spec.genre == "reference"


def test_membership_table_classifies_junction() -> None:
    """A membership table always classifies as junction."""
    plan = build_source_plan(_spanning_sidecar(), None, notice_sink=discard_notice_sink)
    spec = next(s for s in plan if s.source_table == "membership__visit__team")
    assert spec.genre == "junction"


def test_history_table_never_a_plan_entry() -> None:
    """The fixed-category history table never yields a plan entry."""
    plan = build_source_plan(_spanning_sidecar(), None, notice_sink=discard_notice_sink)
    assert all(s.source_table != "history" for s in plan)


# ---------------------------------------------------------------------------
# The genre trichotomy keys on the class
# ---------------------------------------------------------------------------


def test_tracked_presentation_column_reclassifies_to_changelog() -> None:
    """A kind whose only history_tracked column is a class 'tracked' presentation
    value reclassifies from its role's genre to change-log genre — a name that
    genuinely changes over time *is* a change log."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "venue",
                [_col("prop__name", history_tracked=True, temporal_class="tracked")],
            )
        ],
        record_roles={"venue": "dimension"},
    )
    plan = build_source_plan(sidecar, None, notice_sink=discard_notice_sink)
    spec = next(s for s in plan if s.source_table == "records__venue")
    assert spec.genre == "changelog"


def test_constant_presentation_column_does_not_reclassify() -> None:
    """The same kind shape, presentation column class 'constant': no
    reclassification — genre stays reference/transaction by role. The class,
    not the history_tracked bit, decides."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "venue",
                [_col("prop__name", history_tracked=True, temporal_class="constant")],
            )
        ],
        record_roles={"venue": "dimension"},
    )
    plan = build_source_plan(sidecar, None, notice_sink=discard_notice_sink)
    spec = next(s for s in plan if s.source_table == "records__venue")
    assert spec.genre == "reference"


def test_no_flagged_column_skips_class_consultation_entirely() -> None:
    """A kind with no history_tracked=True prop__ column is untracked without
    `_is_kind_tracked` itself consulting any class — the standalone skip
    guard, exercised here with a column carrying no `history_tracked` key at
    all (Phase 3's separate omission scan still needs its class, so it is
    given one). A sibling column carries history_tracked so the sidecar-wide
    availability flag is present."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "venue",
                [
                    _col("prop__name", temporal_class="constant"),
                    _col(
                        "prop__region", history_tracked=False, temporal_class="constant"
                    ),
                ],
            )
        ],
        record_roles={"venue": "dimension"},
    )
    plan = build_source_plan(sidecar, None, notice_sink=discard_notice_sink)
    spec = next(s for s in plan if s.source_table == "records__venue")
    assert spec.genre == "reference"


def test_flagged_column_with_no_class_raises_temporal_class_unavailable() -> None:
    """A prop__ column declaring history_tracked with no paired temporal_class
    raises TemporalClassUnavailableError at plan time, directing to
    `fabulexa-forge validate` (the emit is non-conformant — C13)."""
    sidecar = _sidecar(
        tables=[_records_table("venue", [_col("prop__name", history_tracked=True)])],
        record_roles={"venue": "dimension"},
    )
    with pytest.raises(TemporalClassUnavailableError, match="fabulexa-forge validate"):
        build_source_plan(sidecar, None, notice_sink=discard_notice_sink)


def test_class_with_no_history_tracked_is_never_consulted() -> None:
    """A column declaring a temporal_class with no history_tracked is never
    consulted by the predicate — the refusal is one-directional; a broken
    pairing like this is C13's to report, not the genre trichotomy's. A
    sibling column carries history_tracked so the sidecar-wide availability
    flag is present."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "venue",
                [
                    _col("prop__name", temporal_class="tracked"),
                    _col(
                        "prop__region", history_tracked=False, temporal_class="constant"
                    ),
                ],
            )
        ],
        record_roles={"venue": "dimension"},
    )
    plan = build_source_plan(sidecar, None, notice_sink=discard_notice_sink)
    spec = next(s for s in plan if s.source_table == "records__venue")
    assert spec.genre == "reference"


# ---------------------------------------------------------------------------
# The sub-type split
# ---------------------------------------------------------------------------


def test_untracked_object_registry_kind_splits_per_declared_sub_type() -> None:
    """An untracked object-registry kind yields one unit per declared sub-type."""
    plan = build_source_plan(_spanning_sidecar(), None, notice_sink=discard_notice_sink)
    actor_specs = [s for s in plan if s.source_table == "records__actor"]
    assert [s.sub_type for s in actor_specs] == ["consultant", "nurse"]
    consultant = next(s for s in actor_specs if s.sub_type == "consultant")
    nurse = next(s for s in actor_specs if s.sub_type == "nurse")
    assert consultant.genre == "reference"
    assert nurse.genre == "transaction"


def test_split_unit_drops_discriminator_column() -> None:
    """A split unit's own <kind>_type discriminator is dropped, not renamed."""
    plan = build_source_plan(_spanning_sidecar(), None, notice_sink=discard_notice_sink)
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
            _col("prop__shift_type", history_tracked=False, temporal_class="constant"),
            _col("prop__status", history_tracked=True, temporal_class="tracked"),
        ],
    )
    sidecar = _sidecar(
        tables=[shift_table],
        record_roles={},  # present (registry required unconditionally) but unused
        enum_domains={"shift": {"shift_type": ["day", "night"]}},
    )
    plan = build_source_plan(sidecar, None, notice_sink=discard_notice_sink)
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
            _col("prop__entity_type", history_tracked=False, temporal_class="constant"),
            _col("prop__name", history_tracked=False, temporal_class="constant"),
        ],
    )
    sidecar = _sidecar(
        tables=[entity_table],
        record_roles={"entity": "dimension"},
        enum_domains={"entity": {"entity_type": ["consultant", "nurse"]}},
    )
    plan = build_source_plan(sidecar, None, notice_sink=discard_notice_sink)
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
                    _col(
                        "prop__actor_type",
                        history_tracked=False,
                        temporal_class="constant",
                    ),
                    _col(
                        "prop__name", history_tracked=False, temporal_class="constant"
                    ),
                ],
                rows=0,
            )
        ],
        record_roles={"actor": {"consultant": "dimension", "nurse": "fact"}},
        enum_domains={"actor": {"actor_type": ["consultant", "nurse"]}},
    )
    plan = build_source_plan(sidecar, None, notice_sink=discard_notice_sink)
    assert {s.sub_type for s in plan} == {"consultant", "nurse"}


def test_untracked_kind_with_no_role_raises_source_role_unknown() -> None:
    """An untracked kind absent from record_roles raises SourceRoleUnknown."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "widget",
                [_col("prop__name", history_tracked=False, temporal_class="constant")],
            )
        ],
        record_roles={},
    )
    with pytest.raises(SourceRoleUnknown):
        build_source_plan(sidecar, None, notice_sink=discard_notice_sink)


def test_declared_subtype_absent_from_registry_raises_source_role_unknown() -> None:
    """A sub-type in the enum domain but absent from the registry object raises."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "actor",
                [
                    _col(
                        "prop__actor_type",
                        history_tracked=False,
                        temporal_class="constant",
                    ),
                    _col(
                        "prop__name", history_tracked=False, temporal_class="constant"
                    ),
                ],
            )
        ],
        record_roles={"actor": {"consultant": "dimension"}},  # nurse missing
        enum_domains={"actor": {"actor_type": ["consultant", "nurse"]}},
    )
    with pytest.raises(SourceRoleUnknown):
        build_source_plan(sidecar, None, notice_sink=discard_notice_sink)


def test_object_registry_kind_without_enum_domain_raises_subtypes_undeclared() -> None:
    """An object-registry kind with no <kind>_type enum domain raises."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "actor",
                [
                    _col(
                        "prop__actor_type",
                        history_tracked=False,
                        temporal_class="constant",
                    ),
                    _col(
                        "prop__name", history_tracked=False, temporal_class="constant"
                    ),
                ],
            )
        ],
        record_roles={"actor": {"consultant": "dimension", "nurse": "fact"}},
    )
    with pytest.raises(SourceSubtypesUndeclared):
        build_source_plan(sidecar, None, notice_sink=discard_notice_sink)


def test_sidecar_without_record_roles_raises() -> None:
    """A sidecar with no record_roles registry raises SourceRecordRolesRequired."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "location",
                [_col("prop__name", history_tracked=False, temporal_class="constant")],
            )
        ],
    )
    with pytest.raises(SourceRecordRolesRequired):
        build_source_plan(sidecar, None, notice_sink=discard_notice_sink)


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
        build_source_plan(sidecar, None, notice_sink=discard_notice_sink)


# ---------------------------------------------------------------------------
# Presentation defaults
# ---------------------------------------------------------------------------


def test_presentation_defaults_reference_genre() -> None:
    """fork_path dropped; record_id->id; lifecycle renamed; prop__ stripped."""
    plan = build_source_plan(_spanning_sidecar(), None, notice_sink=discard_notice_sink)
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
    plan = build_source_plan(_spanning_sidecar(), None, notice_sink=discard_notice_sink)
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
    plan = build_source_plan(_spanning_sidecar(), None, notice_sink=discard_notice_sink)
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
                [_col("prop__name", history_tracked=False, temporal_class="constant")],
                presentation_id=True,
            )
        ],
        record_roles={"widget": "dimension"},
    )
    plan = build_source_plan(sidecar, None, notice_sink=discard_notice_sink)
    col_map = dict(plan[0].columns)
    assert col_map["presentation_id"] == "presentation_id"


def test_default_table_names() -> None:
    """Unsplit kind -> <kind>; split unit -> <sub_type>; junction -> <K>_<p>."""
    plan = build_source_plan(_spanning_sidecar(), None, notice_sink=discard_notice_sink)
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
    plan = build_source_plan(
        _spanning_sidecar(), config, notice_sink=discard_notice_sink
    )
    assert all(s.source_table != "records__visit" for s in plan)
    assert all(s.source_table != "membership__visit__team" for s in plan)


def test_exclude_tables_drops_named_sidecar_table_only() -> None:
    """exclude.tables on a membership entry drops that junction alone."""
    config = SourceConfig(exclude=ExcludeDecl(tables=["membership__visit__team"]))
    plan = build_source_plan(
        _spanning_sidecar(), config, notice_sink=discard_notice_sink
    )
    assert all(s.source_table != "membership__visit__team" for s in plan)
    assert any(s.source_table == "records__visit" for s in plan)


def test_exclude_tables_records_prefix_equivalent_to_kind_exclude() -> None:
    """exclude.tables on a records__<kind> entry behaves like exclude.kinds."""
    config = SourceConfig(exclude=ExcludeDecl(tables=["records__visit"]))
    plan = build_source_plan(
        _spanning_sidecar(), config, notice_sink=discard_notice_sink
    )
    assert all(s.source_table != "records__visit" for s in plan)
    assert all(s.source_table != "membership__visit__team" for s in plan)


def test_exclude_kind_unresolved_raises() -> None:
    """An exclude.kinds entry matching nothing raises SourceExcludeUnresolved."""
    config = SourceConfig(exclude=ExcludeDecl(kinds=["nonexistent"]))
    with pytest.raises(SourceExcludeUnresolved):
        build_source_plan(_spanning_sidecar(), config, notice_sink=discard_notice_sink)


def test_exclude_table_unresolved_raises() -> None:
    """An exclude.tables entry matching nothing raises SourceExcludeUnresolved."""
    config = SourceConfig(exclude=ExcludeDecl(tables=["records__nonexistent"]))
    with pytest.raises(SourceExcludeUnresolved):
        build_source_plan(_spanning_sidecar(), config, notice_sink=discard_notice_sink)


# ---------------------------------------------------------------------------
# rename resolution
# ---------------------------------------------------------------------------


def test_rename_table_name_override() -> None:
    """A rename entry's name overrides the default table name."""
    config = SourceConfig(
        rename=[RenameEntry(table="membership__visit__team", name="team_roster")]
    )
    plan = build_source_plan(
        _spanning_sidecar(), config, notice_sink=discard_notice_sink
    )
    spec = next(s for s in plan if s.source_table == "membership__visit__team")
    assert spec.name == "team_roster"


def test_rename_column_override_keyed_by_source_name() -> None:
    """A rename entry's columns map overrides a column's output name."""
    config = SourceConfig(
        rename=[
            RenameEntry(table="records__location", columns={"record_id": "location_id"})
        ]
    )
    plan = build_source_plan(
        _spanning_sidecar(), config, notice_sink=discard_notice_sink
    )
    spec = next(s for s in plan if s.source_table == "records__location")
    assert dict(spec.columns)["record_id"] == "location_id"


def test_rename_column_override_changelog_fold_name() -> None:
    """A rename entry's columns key may name a canonical-fold column."""
    config = SourceConfig(
        rename=[
            RenameEntry(table="records__visit", columns={"event_sim_time": "event_at"})
        ]
    )
    plan = build_source_plan(
        _spanning_sidecar(), config, notice_sink=discard_notice_sink
    )
    spec = next(s for s in plan if s.source_table == "records__visit")
    assert dict(spec.columns)["event_sim_time"] == "event_at"


def test_rename_sub_type_selects_split_unit() -> None:
    """A rename entry's sub_type selects one split unit, leaving the other default."""
    config = SourceConfig(
        rename=[RenameEntry(table="records__actor", sub_type="nurse", name="nurses")]
    )
    plan = build_source_plan(
        _spanning_sidecar(), config, notice_sink=discard_notice_sink
    )
    names = {s.sub_type: s.name for s in plan if s.source_table == "records__actor"}
    assert names["nurse"] == "nurses"
    assert names["consultant"] == "consultant"


def test_rename_unresolved_table_raises() -> None:
    """A rename entry naming an unknown table raises SourceRenameUnresolved."""
    config = SourceConfig(rename=[RenameEntry(table="records__nonexistent", name="x")])
    with pytest.raises(SourceRenameUnresolved):
        build_source_plan(_spanning_sidecar(), config, notice_sink=discard_notice_sink)


def test_rename_unresolved_sub_type_raises() -> None:
    """A rename entry naming an undeclared sub_type raises SourceRenameUnresolved."""
    config = SourceConfig(
        rename=[RenameEntry(table="records__actor", sub_type="doctor", name="x")]
    )
    with pytest.raises(SourceRenameUnresolved):
        build_source_plan(_spanning_sidecar(), config, notice_sink=discard_notice_sink)


def test_rename_unresolved_columns_key_raises() -> None:
    """A rename entry's columns key naming an unknown source column raises."""
    config = SourceConfig(
        rename=[
            RenameEntry(table="records__location", columns={"prop__nonexistent": "x"})
        ]
    )
    with pytest.raises(SourceRenameUnresolved):
        build_source_plan(_spanning_sidecar(), config, notice_sink=discard_notice_sink)


# ---------------------------------------------------------------------------
# Collision checks
# ---------------------------------------------------------------------------


def test_two_tables_same_default_name_raises_collision() -> None:
    """A kind named like a junction default collides with the junction table."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "visit_team",
                [_col("prop__name", history_tracked=False, temporal_class="constant")],
            ),
            _membership_table("visit", "team", [_col("elem__role_name")]),
        ],
        record_roles={"visit_team": "dimension"},
    )
    with pytest.raises(SourceNameCollision):
        build_source_plan(sidecar, None, notice_sink=discard_notice_sink)


def test_column_collision_prop_id_onto_id_raises() -> None:
    """A prop__id column stripping onto 'id' collides with record_id->id."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "widget",
                [_col("prop__id", history_tracked=False, temporal_class="constant")],
            )
        ],
        record_roles={"widget": "dimension"},
    )
    with pytest.raises(SourceNameCollision):
        build_source_plan(sidecar, None, notice_sink=discard_notice_sink)


def test_column_collision_resolved_by_renaming_source_column() -> None:
    """Renaming the colliding source column resolves the collision."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "widget",
                [_col("prop__id", history_tracked=False, temporal_class="constant")],
            )
        ],
        record_roles={"widget": "dimension"},
    )
    config = SourceConfig(
        rename=[RenameEntry(table="records__widget", columns={"prop__id": "widget_id"})]
    )
    plan = build_source_plan(sidecar, config, notice_sink=discard_notice_sink)
    col_map = dict(plan[0].columns)
    assert col_map["record_id"] == "id"
    assert col_map["prop__id"] == "widget_id"


# ---------------------------------------------------------------------------
# Reserved-name checks
# ---------------------------------------------------------------------------


def _widget_sidecar() -> Sidecar:
    return _sidecar(
        tables=[
            _records_table(
                "widget",
                [_col("prop__name", history_tracked=False, temporal_class="constant")],
            )
        ],
        record_roles={"widget": "dimension"},
    )


def test_reserved_table_name_raises() -> None:
    """An output table name equal to a reserved bookkeeping name raises."""
    config = SourceConfig(
        rename=[RenameEntry(table="records__widget", name="_export_meta")]
    )
    with pytest.raises(ExportError):
        build_source_plan(_widget_sidecar(), config, notice_sink=discard_notice_sink)


def test_reserved_table_suffix_raises() -> None:
    """An output table name ending in the reserved suffix raises."""
    config = SourceConfig(
        rename=[RenameEntry(table="records__widget", name="widget__rows")]
    )
    with pytest.raises(ExportError):
        build_source_plan(_widget_sidecar(), config, notice_sink=discard_notice_sink)


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
        build_source_plan(_widget_sidecar(), config, notice_sink=discard_notice_sink)


# ---------------------------------------------------------------------------
# Snapshot delivery (change_delivery: snapshot)
# ---------------------------------------------------------------------------


def test_snapshot_delivery_changelog_columns_are_state_at_shape() -> None:
    """Under snapshot delivery, a changelog kind's columns are the state-at shape;
    genre stays 'changelog'."""
    config = SourceConfig(change_delivery="snapshot")
    plan = build_source_plan(
        _tracked_sidecar(), config, notice_sink=discard_notice_sink
    )
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
    plan = build_source_plan(
        _tracked_sidecar(), config, notice_sink=discard_notice_sink
    )
    col_map = dict(plan[0].columns)
    assert "op" not in col_map
    assert "event_sim_time" not in col_map
    assert "last_mutation_sim_time" not in col_map


def test_snapshot_delivery_includes_presentation_id_when_carried() -> None:
    """presentation_id is carried, positioned before the payload columns."""
    config = SourceConfig(change_delivery="snapshot")
    plan = build_source_plan(
        _tracked_sidecar(presentation_id=True), config, notice_sink=discard_notice_sink
    )
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
    changelog_plan = build_source_plan(
        _spanning_sidecar(), None, notice_sink=discard_notice_sink
    )
    snapshot_plan = build_source_plan(
        _spanning_sidecar(), config, notice_sink=discard_notice_sink
    )
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
    implicit_plan = build_source_plan(
        _tracked_sidecar(), None, notice_sink=discard_notice_sink
    )
    explicit_config = SourceConfig(change_delivery="changelog")
    explicit_plan = build_source_plan(
        _tracked_sidecar(), explicit_config, notice_sink=discard_notice_sink
    )
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
    plan = build_source_plan(
        _tracked_sidecar(), config, notice_sink=discard_notice_sink
    )
    assert dict(plan[0].columns)["record_id"] == "widget_id"


def test_snapshot_rename_keyed_on_fold_name_raises() -> None:
    """A rename entry keyed on a fold name (op) is SourceRenameUnresolved under
    snapshot delivery — the fold names are not this unit's source columns."""
    config = SourceConfig(
        change_delivery="snapshot",
        rename=[RenameEntry(table="records__widget", columns={"op": "operation"})],
    )
    with pytest.raises(SourceRenameUnresolved):
        build_source_plan(_tracked_sidecar(), config, notice_sink=discard_notice_sink)


def test_snapshot_collision_check_runs_over_snapshot_columns() -> None:
    """The collision check runs over the snapshot column set: a prop__id column
    stripping onto 'id' collides with record_id->id."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "widget",
                [
                    _col(
                        "prop__status", history_tracked=True, temporal_class="tracked"
                    ),
                    _col("prop__id", history_tracked=False, temporal_class="constant"),
                ],
            )
        ],
        record_roles={},
    )
    config = SourceConfig(change_delivery="snapshot")
    with pytest.raises(SourceNameCollision):
        build_source_plan(sidecar, config, notice_sink=discard_notice_sink)


# ---------------------------------------------------------------------------
# Records-column taxonomy posture
# ---------------------------------------------------------------------------


def test_unclassified_column_on_reference_kind_raises() -> None:
    """A records table carrying a no-role column raises SourceUnclassifiedColumn,
    naming the table and column, before any output is written."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "location",
                [_col("prop__name", history_tracked=False, temporal_class="constant")],
            ),
        ],
        record_roles={"location": "dimension"},
    )
    # Inject a no-role column directly (the builder helpers only ever produce
    # conformant columns).
    table = sidecar.tables()[0]
    assert table.name == "records__location"
    object.__setattr__(
        table,
        "columns",
        table.columns + (ColumnSpec("mystery", "VARCHAR", None, None, None),),
    )
    with pytest.raises(SourceUnclassifiedColumn, match="records__location.*mystery"):
        build_source_plan(sidecar, None, notice_sink=discard_notice_sink)


def test_unclassified_column_on_changelog_kind_raises() -> None:
    """A tracked (changelog-genre) kind carrying a no-role column also raises."""
    sidecar = _tracked_sidecar()
    table = sidecar.tables()[0]
    assert table.name == "records__widget"
    object.__setattr__(
        table,
        "columns",
        table.columns + (ColumnSpec("mystery", "VARCHAR", None, None, None),),
    )
    with pytest.raises(SourceUnclassifiedColumn, match="records__widget.*mystery"):
        build_source_plan(sidecar, None, notice_sink=discard_notice_sink)


def test_reference_genre_drops_no_columns_beyond_fork_path_at_v5() -> None:
    """The identity index families do not occur here; the taxonomy posture
    changes nothing observable -- fork_path is dropped, record_id kept as id,
    exactly as before."""
    plan = build_source_plan(_spanning_sidecar(), None, notice_sink=discard_notice_sink)
    spec = next(s for s in plan if s.source_table == "records__location")
    sources = [src for src, _ in spec.columns]
    assert "fork_path" not in sources
    assert "record_id" in sources
    assert dict(spec.columns)["record_id"] == "id"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism_repeated_calls_identical() -> None:
    """Repeated calls over the same (sidecar, config) yield an identical result."""
    sidecar = _spanning_sidecar()
    first = build_source_plan(sidecar, None, notice_sink=discard_notice_sink)
    second = build_source_plan(sidecar, None, notice_sink=discard_notice_sink)
    assert first == second


# ---------------------------------------------------------------------------
# slice_only column omission (Phase 3)
# ---------------------------------------------------------------------------


def _slice_only_col(name: str, type_: str = "VARCHAR") -> dict[str, object]:
    """Build a non-exempt `temporal_class: slice_only` prop__ column entry."""
    return prop_column(name, type_, history_tracked=False, temporal_class="slice_only")


def test_slice_only_column_omitted_from_reference_defaults() -> None:
    """A non-exempt slice_only column is absent from the reference genre's
    default column set; a sibling constant column is unaffected."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "patient",
                [
                    _col(
                        "prop__name", history_tracked=False, temporal_class="constant"
                    ),
                    _slice_only_col("prop__loyalty_tier"),
                ],
            )
        ],
        record_roles={"patient": "dimension"},
    )
    plan = build_source_plan(sidecar, None, notice_sink=discard_notice_sink)
    sources = [src for src, _ in plan[0].columns]
    assert "prop__loyalty_tier" not in sources
    assert "prop__name" in sources


def test_slice_only_column_omitted_from_changelog_defaults() -> None:
    """A non-exempt slice_only column is absent from the change-log genre's
    default column set; the tracked property is unaffected."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "visit",
                [
                    _col(
                        "prop__status", history_tracked=True, temporal_class="tracked"
                    ),
                    _slice_only_col("prop__loyalty_tier"),
                ],
            )
        ],
        record_roles={},
    )
    plan = build_source_plan(sidecar, None, notice_sink=discard_notice_sink)
    sources = [src for src, _ in plan[0].columns]
    assert "prop__loyalty_tier" not in sources
    assert "prop__status" in sources


def test_slice_only_column_omitted_from_snapshot_defaults() -> None:
    """A non-exempt slice_only column is absent from the snapshot (state-at)
    delivery shape too."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "visit",
                [
                    _col(
                        "prop__status", history_tracked=True, temporal_class="tracked"
                    ),
                    _slice_only_col("prop__loyalty_tier"),
                ],
            )
        ],
        record_roles={},
    )
    config = SourceConfig(change_delivery="snapshot")
    plan = build_source_plan(sidecar, config, notice_sink=discard_notice_sink)
    sources = [src for src, _ in plan[0].columns]
    assert "prop__loyalty_tier" not in sources
    assert "prop__status" in sources


def test_notice_emitted_once_per_omitted_column() -> None:
    """One slice-only-column-omitted notice per omitted column, naming the
    unit and the column."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "patient",
                [
                    _col(
                        "prop__name", history_tracked=False, temporal_class="constant"
                    ),
                    _slice_only_col("prop__loyalty_tier"),
                ],
            )
        ],
        record_roles={"patient": "dimension"},
    )
    sink = RecordingNoticeSink()
    build_source_plan(sidecar, None, notice_sink=sink)
    assert len(sink.notices) == 1
    notice = sink.notices[0]
    assert notice.code == "slice-only-column-omitted"
    assert "records__patient" in notice.message
    assert "prop__loyalty_tier" in notice.message


def test_notice_order_unit_order_then_sidecar_column_order() -> None:
    """Notices emit in unit order (sidecar table order), then sidecar column
    order within a unit — deterministic across repeated calls."""
    patient_table = _records_table(
        "patient",
        [_slice_only_col("prop__tier_a"), _slice_only_col("prop__tier_b")],
    )
    order_table = _records_table("order", [_slice_only_col("prop__flag_a")])
    sidecar = _sidecar(
        tables=[patient_table, order_table],
        record_roles={"patient": "dimension", "order": "fact"},
    )
    sink = RecordingNoticeSink()
    build_source_plan(sidecar, None, notice_sink=sink)
    assert len(sink.notices) == 3
    assert "prop__tier_a" in sink.notices[0].message
    assert "prop__tier_b" in sink.notices[1].message
    assert "prop__flag_a" in sink.notices[2].message

    sink2 = RecordingNoticeSink()
    build_source_plan(sidecar, None, notice_sink=sink2)
    assert [n.message for n in sink.notices] == [n.message for n in sink2.notices]


def test_degenerate_unit_every_property_slice_only_still_renders() -> None:
    """A unit whose every property is non-exempt slice_only still yields a
    spec: identity/lifecycle columns carried, every prop__ column omitted, one
    notice per omitted column — the unit is never suppressed."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "patient",
                [_slice_only_col("prop__tier"), _slice_only_col("prop__note")],
            )
        ],
        record_roles={"patient": "dimension"},
    )
    sink = RecordingNoticeSink()
    plan = build_source_plan(sidecar, None, notice_sink=sink)
    assert len(plan) == 1
    spec = plan[0]
    sources = [src for src, _ in spec.columns]
    assert "prop__tier" not in sources
    assert "prop__note" not in sources
    assert "record_id" in sources
    assert dict(spec.columns)["record_id"] == "id"
    assert len(sink.notices) == 2


def test_junction_unit_untouched_no_notices() -> None:
    """A junction unit reads no class and emits no slice-only-column-omitted
    notice, even when its owning kind carries a non-exempt slice_only column."""
    visit_table = _records_table(
        "visit",
        [
            _col("prop__status", history_tracked=True, temporal_class="tracked"),
            _slice_only_col("prop__loyalty_tier"),
        ],
    )
    team_membership = _membership_table("visit", "team", [_col("elem__role_name")])
    sidecar = _sidecar(tables=[visit_table, team_membership], record_roles={})
    sink = RecordingNoticeSink()
    plan = build_source_plan(sidecar, None, notice_sink=sink)
    junction_spec = next(s for s in plan if s.source_table == "membership__visit__team")
    assert ("elem__role_name", "role_name") in junction_spec.columns
    assert all("membership__visit__team" not in n.message for n in sink.notices)
    assert len(sink.notices) == 1  # the visit unit's one omitted column


def test_split_unit_omission_notice_names_source_table_and_sub_type() -> None:
    """A split (sub-typed) unit's slice-only-column-omitted notice names the
    unit as "unit '<source_table> (sub_type '<sub_type>')'" (`_unit_label`),
    not the bare kind — one notice per split unit sharing the omitted column."""
    actor_table = _records_table(
        "actor",
        [
            _col("prop__actor_type", history_tracked=False, temporal_class="constant"),
            _slice_only_col("prop__loyalty_tier"),
        ],
    )
    sidecar = _sidecar(
        tables=[actor_table],
        record_roles={"actor": {"consultant": "dimension", "nurse": "fact"}},
        enum_domains={"actor": {"actor_type": ["consultant", "nurse"]}},
    )
    sink = RecordingNoticeSink()
    build_source_plan(sidecar, None, notice_sink=sink)
    notices = [n for n in sink.notices if n.code == "slice-only-column-omitted"]
    assert len(notices) == 2
    assert notices[0].code == "slice-only-column-omitted"
    assert notices[0].message == (
        "unit 'records__actor (sub_type 'consultant')': column"
        " 'prop__loyalty_tier' is temporal_class: slice_only; omitted from"
        " the source export"
    )
    assert notices[1].message == (
        "unit 'records__actor (sub_type 'nurse')': column"
        " 'prop__loyalty_tier' is temporal_class: slice_only; omitted from"
        " the source export"
    )


def test_unsplit_subtyped_discriminator_exempt_even_when_slice_only() -> None:
    """A bare-role sub-typed kind's own discriminator is retained even when
    declared slice_only — the discriminator carve-out is exempt regardless of
    class; the existing retain rule is unchanged."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "entity",
                [
                    _slice_only_col("prop__entity_type"),
                    _col(
                        "prop__name", history_tracked=False, temporal_class="constant"
                    ),
                ],
            )
        ],
        record_roles={"entity": "dimension"},
        enum_domains={"entity": {"entity_type": ["consultant", "nurse"]}},
    )
    sink = RecordingNoticeSink()
    plan = build_source_plan(sidecar, None, notice_sink=sink)
    assert len(plan) == 1
    spec = plan[0]
    assert ("prop__entity_type", "entity_type") in spec.columns
    assert len(sink.notices) == 0


def test_rename_omitted_column_raises_source_rename_slice_only() -> None:
    """A rename entry's columns key naming a policy-omitted slice_only column
    raises SourceRenameSliceOnly, naming the entry, the column, and the
    omission reason."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "patient",
                [
                    _col(
                        "prop__name", history_tracked=False, temporal_class="constant"
                    ),
                    _slice_only_col("prop__loyalty_tier"),
                ],
            )
        ],
        record_roles={"patient": "dimension"},
    )
    config = SourceConfig(
        rename=[
            RenameEntry(
                table="records__patient", columns={"prop__loyalty_tier": "tier"}
            )
        ]
    )
    with pytest.raises(
        SourceRenameSliceOnly, match="records__patient.*prop__loyalty_tier"
    ):
        build_source_plan(sidecar, config, notice_sink=discard_notice_sink)


def test_rename_delivered_column_still_works_alongside_omitted() -> None:
    """A rename entry targeting a still-delivered column succeeds even though
    the same table also carries an omitted slice_only column."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "patient",
                [
                    _col(
                        "prop__name", history_tracked=False, temporal_class="constant"
                    ),
                    _slice_only_col("prop__loyalty_tier"),
                ],
            )
        ],
        record_roles={"patient": "dimension"},
    )
    config = SourceConfig(
        rename=[
            RenameEntry(table="records__patient", columns={"prop__name": "full_name"})
        ]
    )
    plan = build_source_plan(sidecar, config, notice_sink=discard_notice_sink)
    col_map = dict(plan[0].columns)
    assert col_map["prop__name"] == "full_name"
    assert "prop__loyalty_tier" not in col_map


def test_collision_check_ignores_omitted_column() -> None:
    """A slice_only column that would collide with another output name if
    delivered raises nothing once omitted — the collision check runs over the
    narrowed set."""
    sidecar = _sidecar(
        tables=[
            _records_table(
                "widget",
                [
                    _slice_only_col("prop__id"),
                    _col(
                        "prop__name", history_tracked=False, temporal_class="constant"
                    ),
                ],
            )
        ],
        record_roles={"widget": "dimension"},
    )
    plan = build_source_plan(sidecar, None, notice_sink=discard_notice_sink)
    col_map = dict(plan[0].columns)
    assert "prop__id" not in col_map
    assert col_map["record_id"] == "id"
