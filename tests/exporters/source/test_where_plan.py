"""Tests for `SourceTableDecl.where` plan-time resolution: the
constant-column gate (`_resolve_where_selection`), castability
(`SourceWhereValueUncastable`), and the out-of-domain notice
(`_check_where_values_observed`) — `exporters/source/plan.py`, source-row-
selection sprint §§ Phase 1 (state tables) and Phase 2 (junction tables, the
parent lookup).

Every fixture is a real (DuckDB-backed) emit: the gate-matrix and domain-
notice cases reuse `_source_fixtures.py`'s spanning fixtures (the deep
plan/fixture surface the `source` step's `_build_state_table_plan` delta
already reshapes); the castability cases need a `constant`-class BIGINT
payload property no spanning fixture declares, so they use a small bespoke
bare emit (0 rows — no gate here consults row data), mirroring
`test_election_plan.py`'s own "gate-only" bespoke-emit convention. Phase 2's
parent-lookup cases (owner `where` / `sub_types` through a membership unit)
need a sub-typed owner with both a tracked and a constant payload property
and a membership table carrying a same-named element field, so they use
their own bespoke bare emit for the same reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink
from _support.sidecar_builder import identity_column, prop_column, write_emit

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    ExportConfig,
    MembershipRef,
    SourceConfig,
    SourceEventsDecl,
    SourceEventSourceDecl,
    SourceTableDecl,
)
from fabulexa_forge.errors import (
    SourceEventSourceOverlap,
    SourceSubTypesOnFlatKind,
    SourceTableSubTypeUnknown,
    SourceWhereColumnUnresolved,
    SourceWhereNotConstant,
    SourceWhereOnDiscriminator,
    SourceWhereValueUncastable,
)
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.populations import Population
from fabulexa_forge.exporters.source.plan import (
    SourceJunctionTablePlan,
    SourceStateTablePlan,
    build_source_plan,
)
from fabulexa_forge.reader.emit import open_emit

from ._source_fixtures import build_slice_only_source_emit, build_source_test_emit

if TYPE_CHECKING:
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.source.plan import SourcePlan

# ---------------------------------------------------------------------------
# Config + plan-build helpers
# ---------------------------------------------------------------------------


def _config(
    tables: "tuple[SourceTableDecl, ...]" = (),
    events: "SourceEventsDecl | None" = None,
) -> ExportConfig:
    """Build a `mode: source` ExportConfig from a declared table/events set."""
    return ExportConfig(
        mode="source", source=SourceConfig(tables=tables, events=events)
    )


def _open_plan(
    emit_dir: Path,
    config: ExportConfig,
    *,
    notice_sink: "NoticeSink" = discard_notice_sink,
) -> "SourcePlan":
    """Open `emit_dir` and build a SourcePlan against it, resolving the
    anchor and election the way the engine does."""
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None, "every fixture here declares a runtime block"
        election = resolve_election(emit.sidecar, None)
        return build_source_plan(emit, config, anchor, election, False, notice_sink)


def _state(plan: "SourcePlan", name: str) -> SourceStateTablePlan:
    """The sole `state` unit named `name`."""
    table = next(t for t in plan.tables if t.name == name)
    assert isinstance(table, SourceStateTablePlan)
    return table


def _junction(plan: "SourcePlan", name: str) -> SourceJunctionTablePlan:
    """The sole `junction` unit named `name`."""
    table = next(t for t in plan.tables if t.name == name)
    assert isinstance(table, SourceJunctionTablePlan)
    return table


# ---------------------------------------------------------------------------
# Gate matrix (doc § The constant-column gate, all seven rows)
# ---------------------------------------------------------------------------


def test_where_constant_property_accepted(tmp_path: Path) -> None:
    """A `constant`-class, non-discriminator payload property resolves: one
    `SourceWhereEntry` carrying the source column, sidecar type, verbatim
    value, and its typed cast."""
    tables = (
        SourceTableDecl(name="loc", kind="location", where={"prop__name": "Ward A"}),
    )
    plan = _open_plan(build_source_test_emit(tmp_path), _config(tables))
    table = _state(plan, "loc")
    assert len(table.where) == 1
    entry = table.where[0]
    assert entry.key == "prop__name"
    assert entry.source_column == "prop__name"
    assert entry.sql_type == "VARCHAR"
    assert entry.value == "Ward A"
    assert entry.typed_values == ("Ward A",)


def test_where_tracked_column_refused(tmp_path: Path) -> None:
    """A `tracked`-class column is refused with the tracked message variant."""
    tables = (SourceTableDecl(name="v", kind="visit", where={"prop__status": "open"}),)
    with pytest.raises(SourceWhereNotConstant, match="temporal_class: tracked"):
        _open_plan(build_source_test_emit(tmp_path), _config(tables))


def test_where_slice_only_column_refused(tmp_path: Path) -> None:
    """A `slice_only`-class column is refused with the slice_only message
    variant — `SourceSliceOnlyRead`'s population never extends to `where`."""
    tables = (
        SourceTableDecl(name="p", kind="patient", where={"prop__loyalty_tier": "gold"}),
    )
    with pytest.raises(SourceWhereNotConstant, match="temporal_class: slice_only"):
        _open_plan(build_slice_only_source_emit(tmp_path), _config(tables))


def test_where_on_discriminator_refused(tmp_path: Path) -> None:
    """A `where` key naming the subject kind's discriminator is refused,
    pointing at `sub_types`."""
    tables = (
        SourceTableDecl(name="s", kind="shift", where={"prop__shift_type": "day"}),
    )
    with pytest.raises(SourceWhereOnDiscriminator, match="sub_types, not where"):
        _open_plan(build_source_test_emit(tmp_path), _config(tables))


def test_where_structural_column_unresolved(tmp_path: Path) -> None:
    """A structural column (`record_id`) is not a payload property and is
    refused — `SourceWhereColumnUnresolved`, not any other class."""
    tables = (SourceTableDecl(name="v", kind="visit", where={"record_id": "v001"}),)
    with pytest.raises(SourceWhereColumnUnresolved, match="not a payload property"):
        _open_plan(build_source_test_emit(tmp_path), _config(tables))


def test_where_unknown_column_unresolved(tmp_path: Path) -> None:
    """A key naming no column of the subject kind is refused."""
    tables = (
        SourceTableDecl(name="loc", kind="location", where={"prop__nonexistent": "x"}),
    )
    with pytest.raises(SourceWhereColumnUnresolved, match="not a payload property"):
        _open_plan(build_source_test_emit(tmp_path), _config(tables))


def test_where_bare_name_missing_prefix_unresolved(tmp_path: Path) -> None:
    """A `kind:` table's `where` key must carry the `prop__` prefix; a bare
    name is refused as unresolved, never silently prefixed."""
    tables = (SourceTableDecl(name="loc", kind="location", where={"name": "Ward A"}),)
    with pytest.raises(SourceWhereColumnUnresolved, match="not a payload property"):
        _open_plan(build_source_test_emit(tmp_path), _config(tables))


def _col_ddl(col: "dict[str, object]") -> str:
    """Build a single column DDL fragment (name + type only)."""
    return f'"{col["name"]}" {col["type"]}'


# ---------------------------------------------------------------------------
# Bespoke bare emit: a sub-typed owner ('clinician', day/night) with a
# tracked and a constant payload property, owning a membership table
# ('ward_allocation') whose element field shares a name with the owner
# property — the parent lookup's gate matrix (doc § The parent lookup).
# ---------------------------------------------------------------------------

_CLINICIAN_COLUMNS: "list[dict[str, object]]" = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__clinician_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
    ),
    prop_column(
        "prop__region", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_WARD_ALLOCATION_COLUMNS: "list[dict[str, object]]" = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__shift_note", "type": "VARCHAR"},
    {"name": "elem__region", "type": "VARCHAR"},
]


def _write_clinician_emit(tmp_path: Path) -> Path:
    """A sub-typed `clinician` kind (day/night, 0 rows) owning
    `membership__clinician__ward_allocation`: `prop__region` (constant,
    declared `enum_domains` for the out-of-domain notice), `prop__status`
    (tracked), and an element field (`elem__region`) that collides in name
    with the owner property to prove `where` resolution never touches the
    membership table."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    for table_name, columns in (
        ("records__clinician", _CLINICIAN_COLUMNS),
        ("membership__clinician__ward_allocation", _WARD_ALLOCATION_COLUMNS),
    ):
        col_fragments = ", ".join(_col_ddl(c) for c in columns)
        conn.execute(f'CREATE TABLE "{table_name}" ({col_fragments})')
    conn.close()
    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__clinician",
                "category": "records",
                "record_kind": "clinician",
                "columns": _CLINICIAN_COLUMNS,
                "rows": 0,
            },
            {
                "name": "membership__clinician__ward_allocation",
                "category": "membership",
                "record_kind": "clinician",
                "property": "ward_allocation",
                "columns": _WARD_ALLOCATION_COLUMNS,
                "rows": 0,
            },
        ],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
            "enum_domains": {
                "clinician": {
                    "clinician_type": ["day", "night"],
                    "region": ["east", "west"],
                }
            },
        },
    )
    return tmp_path


def _clinician_junction(
    sub_types: "tuple[str, ...] | None" = None,
    where: "dict[str, str | list[str]] | None" = None,
) -> tuple[SourceTableDecl, ...]:
    """A `watchers` junction over `membership__clinician__ward_allocation`."""
    return (
        SourceTableDecl(
            name="watchers",
            membership=MembershipRef(kind="clinician", property="ward_allocation"),
            sub_types=sub_types,
            where=where,
        ),
    )


# ---------------------------------------------------------------------------
# The parent lookup: owner `where` gate matrix (junction tables)
# ---------------------------------------------------------------------------


def test_junction_where_owner_constant_property_resolves_owner_not_element(
    tmp_path: Path,
) -> None:
    """A bare `where` key matching both an owner property and a same-named
    element field resolves to the owner property — the parent lookup reads
    only the owner's records table, never the membership table."""
    tables = _clinician_junction(where={"region": "east"})
    plan = _open_plan(_write_clinician_emit(tmp_path), _config(tables))
    table = _junction(plan, "watchers")
    assert len(table.where) == 1
    entry = table.where[0]
    assert entry.key == "region"
    assert entry.source_column == "prop__region"
    assert entry.sql_type == "VARCHAR"


def test_junction_where_element_only_field_unresolved(tmp_path: Path) -> None:
    """A bare key matching only an element field (no owner property) is
    refused — the parent lookup never falls through to the membership
    table's own columns."""
    tables = _clinician_junction(where={"shift_note": "urgent"})
    with pytest.raises(SourceWhereColumnUnresolved, match="not a payload property"):
        _open_plan(_write_clinician_emit(tmp_path), _config(tables))


def test_junction_where_owner_tracked_property_refused(tmp_path: Path) -> None:
    """An owner `tracked` property is refused, as for a records-backed table."""
    tables = _clinician_junction(where={"status": "on_duty"})
    with pytest.raises(SourceWhereNotConstant, match="temporal_class: tracked"):
        _open_plan(_write_clinician_emit(tmp_path), _config(tables))


def test_junction_where_owner_discriminator_refused(tmp_path: Path) -> None:
    """The owner discriminator is refused, pointing at `sub_types`."""
    tables = _clinician_junction(where={"clinician_type": "day"})
    with pytest.raises(SourceWhereOnDiscriminator, match="sub_types, not where"):
        _open_plan(_write_clinician_emit(tmp_path), _config(tables))


def test_junction_where_domain_notice_reused(tmp_path: Path) -> None:
    """An out-of-domain owner `where` value draws the same
    `discriminator-value-unobserved` notice a state table's does — never an
    error."""
    tables = _clinician_junction(where={"region": "south"})
    sink = RecordingNoticeSink()
    emit_dir = _write_clinician_emit(tmp_path)
    plan = _open_plan(emit_dir, _config(tables), notice_sink=sink)
    assert _junction(plan, "watchers").where  # the gate passed
    assert len(sink.notices) == 1
    assert sink.notices[0].code == "discriminator-value-unobserved"


# ---------------------------------------------------------------------------
# The parent lookup: owner `sub_types` validation + addressed populations
# ---------------------------------------------------------------------------


def test_junction_sub_types_unknown_on_subtyped_owner_refused(tmp_path: Path) -> None:
    """A `sub_types` entry outside the owner's discriminator domain is
    refused, naming the owner kind."""
    tables = _clinician_junction(sub_types=("evening",))
    with pytest.raises(SourceTableSubTypeUnknown, match="clinician"):
        _open_plan(_write_clinician_emit(tmp_path), _config(tables))


def test_junction_sub_types_on_flat_owner_refused(tmp_path: Path) -> None:
    """`sub_types` on a flat owner (visit, no discriminator) is refused —
    the relaxed grammar now parses, but the flat-owner gate still fires at
    plan time."""
    tables = (
        SourceTableDecl(
            name="watchers",
            membership=MembershipRef(kind="visit", property="team"),
            sub_types=("standard",),
        ),
    )
    with pytest.raises(SourceSubTypesOnFlatKind, match="visit"):
        _open_plan(build_source_test_emit(tmp_path), _config(tables))


def test_junction_sub_types_narrows_owner_populations(tmp_path: Path) -> None:
    """A valid `sub_types` entry narrows `owner_populations` to exactly the
    addressed atoms, declaration-domain order."""
    tables = _clinician_junction(sub_types=("day",))
    plan = _open_plan(_write_clinician_emit(tmp_path), _config(tables))
    table = _junction(plan, "watchers")
    assert table.owner_populations == (Population(kind="clinician", sub_type="day"),)


def test_junction_where_only_addresses_full_owner_domain(tmp_path: Path) -> None:
    """A `where`-only junction (no `sub_types`) addresses the owner's full
    declared population set (doc § The parent lookup) — `where` never
    narrows the addressed set."""
    tables = _clinician_junction(where={"region": "east"})
    plan = _open_plan(_write_clinician_emit(tmp_path), _config(tables))
    table = _junction(plan, "watchers")
    assert table.owner_populations == (
        Population(kind="clinician", sub_type="day"),
        Population(kind="clinician", sub_type="night"),
    )


# ---------------------------------------------------------------------------
# Bespoke bare emit: a `constant`-class BIGINT property no spanning fixture
# declares (castability), plus a non-discriminator enum_domains entry no
# spanning fixture declares (the out-of-domain notice).
# ---------------------------------------------------------------------------

_SENSOR_COLUMNS: "list[dict[str, object]]" = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__reading", "BIGINT", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__category", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]


def _write_sensor_emit(tmp_path: Path) -> Path:
    """A flat `sensor` kind (0 rows): `prop__reading` (BIGINT, constant) for
    castability, `prop__category` (VARCHAR, constant) with a declared
    enum_domains entry for the out-of-domain notice."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    col_fragments = ", ".join(_col_ddl(c) for c in _SENSOR_COLUMNS)
    conn.execute(f'CREATE TABLE "records__sensor" ({col_fragments})')
    conn.close()
    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__sensor",
                "category": "records",
                "record_kind": "sensor",
                "columns": _SENSOR_COLUMNS,
                "rows": 0,
            }
        ],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
            "enum_domains": {"sensor": {"category": ["indoor", "outdoor"]}},
        },
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Castability
# ---------------------------------------------------------------------------


def test_where_uncastable_element_refused(tmp_path: Path) -> None:
    """A non-numeric element on a BIGINT column is refused, naming the
    element — before any write."""
    tables = (SourceTableDecl(name="s", kind="sensor", where={"prop__reading": "abc"}),)
    with pytest.raises(
        SourceWhereValueUncastable, match="'abc'.*does not cast to BIGINT"
    ):
        _open_plan(_write_sensor_emit(tmp_path), _config(tables))


def test_where_castable_element_resolves_typed_value(tmp_path: Path) -> None:
    """A castable BIGINT element resolves to its typed (int) value."""
    tables = (SourceTableDecl(name="s", kind="sensor", where={"prop__reading": "42"}),)
    plan = _open_plan(_write_sensor_emit(tmp_path), _config(tables))
    table = _state(plan, "s")
    assert table.where[0].typed_values == (42,)


# ---------------------------------------------------------------------------
# Domain notice (discriminator-value-unobserved, never an error)
# ---------------------------------------------------------------------------


def test_where_scalar_observed_emits_no_notice(tmp_path: Path) -> None:
    """A scalar value inside the declared enum_domains entry emits nothing."""
    tables = (
        SourceTableDecl(name="s", kind="sensor", where={"prop__category": "indoor"}),
    )
    sink = RecordingNoticeSink()
    _open_plan(_write_sensor_emit(tmp_path), _config(tables), notice_sink=sink)
    assert sink.notices == []


def test_where_scalar_unobserved_emits_one_notice_no_rows(tmp_path: Path) -> None:
    """A scalar value outside the domain emits one notice stating the unit
    renders no rows — never an error."""
    tables = (
        SourceTableDecl(
            name="s", kind="sensor", where={"prop__category": "underground"}
        ),
    )
    sink = RecordingNoticeSink()
    plan = _open_plan(_write_sensor_emit(tmp_path), _config(tables), notice_sink=sink)
    assert _state(plan, "s").where  # the gate passed; only the value notices
    assert len(sink.notices) == 1
    assert sink.notices[0].code == "discriminator-value-unobserved"
    assert sink.notices[0].message == (
        "table 's': where value 'underground' for 'prop__category' not"
        " observed; the unit renders no rows"
    )


def test_where_list_wholly_unobserved_emits_one_notice_per_element(
    tmp_path: Path,
) -> None:
    """A list with no element observed emits one notice per element, config
    element order, each keeping the "renders no rows" wording."""
    tables = (
        SourceTableDecl(
            name="s",
            kind="sensor",
            where={"prop__category": ["underground", "space"]},
        ),
    )
    sink = RecordingNoticeSink()
    _open_plan(_write_sensor_emit(tmp_path), _config(tables), notice_sink=sink)
    assert [n.code for n in sink.notices] == ["discriminator-value-unobserved"] * 2
    assert "underground" in sink.notices[0].message
    assert "renders no rows" in sink.notices[0].message
    assert "space" in sink.notices[1].message
    assert "renders no rows" in sink.notices[1].message


def test_where_list_partially_observed_emits_only_unobserved_elements(
    tmp_path: Path,
) -> None:
    """A list with some elements observed emits one notice per unobserved
    element only, config order, with the weaker "contributes no rows"
    wording — the unit is not, in fact, empty."""
    tables = (
        SourceTableDecl(
            name="s",
            kind="sensor",
            where={"prop__category": ["indoor", "underground", "outdoor", "space"]},
        ),
    )
    sink = RecordingNoticeSink()
    _open_plan(_write_sensor_emit(tmp_path), _config(tables), notice_sink=sink)
    assert len(sink.notices) == 2
    assert "underground" in sink.notices[0].message
    assert "contributes no rows" in sink.notices[0].message
    assert "space" in sink.notices[1].message
    assert "contributes no rows" in sink.notices[1].message


def test_where_column_absent_from_registry_emits_no_notice(tmp_path: Path) -> None:
    """A `where` column with no `enum_domains` entry is unchecked, whatever
    value it carries."""
    tables = (SourceTableDecl(name="s", kind="sensor", where={"prop__reading": "5"}),)
    sink = RecordingNoticeSink()
    _open_plan(_write_sensor_emit(tmp_path), _config(tables), notice_sink=sink)
    assert sink.notices == []


# ---------------------------------------------------------------------------
# Events sources: `where` gate matrix (bare-key resolution, `events source
# #<n>` labels — source-row-selection sprint § Phase 3)
# ---------------------------------------------------------------------------


def _events(*sources: SourceEventSourceDecl) -> SourceEventsDecl:
    """A one-line `SourceEventsDecl` builder for the disjointness/gate tests."""
    return SourceEventsDecl(name="log", sources=sources)


def test_events_records_source_where_bare_key_resolves(tmp_path: Path) -> None:
    """A records events source's `where` key is bare — the `only` / `ignore`
    addressing convention — never the state table's `prop__<p>` form."""
    events = _events(SourceEventSourceDecl(kind="location", where={"name": "Ward A"}))
    plan = _open_plan(build_source_test_emit(tmp_path), _config(events=events))
    assert plan.events is not None
    entry = plan.events.sources[0].where[0]
    assert entry.key == "name"
    assert entry.source_column == "prop__name"


def test_events_records_source_where_prefixed_key_unresolved(tmp_path: Path) -> None:
    """A `prop__`-prefixed key is refused as unresolved under the bare
    addressing convention, labeled `events source #1`."""
    events = _events(
        SourceEventSourceDecl(kind="location", where={"prop__name": "Ward A"})
    )
    with pytest.raises(SourceWhereColumnUnresolved, match="events source #1"):
        _open_plan(build_source_test_emit(tmp_path), _config(events=events))


def test_events_membership_source_where_resolves_owner_constant(
    tmp_path: Path,
) -> None:
    """A membership events source's `where` resolves owner constants through
    the parent lookup, never the membership table's own columns."""
    events = _events(
        SourceEventSourceDecl(
            membership=MembershipRef(kind="clinician", property="ward_allocation"),
            where={"region": "east"},
        )
    )
    plan = _open_plan(_write_clinician_emit(tmp_path), _config(events=events))
    assert plan.events is not None
    entry = plan.events.sources[0].where[0]
    assert entry.key == "region"
    assert entry.source_column == "prop__region"


def test_events_records_source_where_tracked_refused_with_label(
    tmp_path: Path,
) -> None:
    """A `tracked`-class column is refused, labeled `events source #1` — the
    full constant-column gate applies to events sources."""
    events = _events(SourceEventSourceDecl(kind="visit", where={"status": "open"}))
    with pytest.raises(SourceWhereNotConstant, match="events source #1"):
        _open_plan(build_source_test_emit(tmp_path), _config(events=events))


def test_events_records_source_where_discriminator_refused_with_label(
    tmp_path: Path,
) -> None:
    """A `where` key naming the discriminator is refused, labeled
    `events source #1`, pointing at `sub_types`."""
    events = _events(SourceEventSourceDecl(kind="shift", where={"shift_type": "day"}))
    with pytest.raises(SourceWhereOnDiscriminator, match="events source #1"):
        _open_plan(build_source_test_emit(tmp_path), _config(events=events))


def test_events_records_source_where_uncastable_refused_with_label(
    tmp_path: Path,
) -> None:
    """An uncastable element is refused, labeled `events source #1`, before
    any write."""
    events = _events(SourceEventSourceDecl(kind="sensor", where={"reading": "abc"}))
    with pytest.raises(SourceWhereValueUncastable, match="events source #1"):
        _open_plan(_write_sensor_emit(tmp_path), _config(events=events))


def test_events_second_source_label_uses_its_declaration_index(
    tmp_path: Path,
) -> None:
    """The label tracks declaration position, not just the first source."""
    events = _events(
        SourceEventSourceDecl(kind="location", where={"name": "Ward A"}, item_type="a"),
        SourceEventSourceDecl(kind="location", where={"bogus": "x"}, item_type="b"),
    )
    with pytest.raises(SourceWhereColumnUnresolved, match="events source #2"):
        _open_plan(build_source_test_emit(tmp_path), _config(events=events))


# ---------------------------------------------------------------------------
# Events sources: selection-aware disjointness (doc § Event-source
# disjointness — every row of the table)
# ---------------------------------------------------------------------------


def test_disjointness_common_column_disjoint_typed_values_legal(
    tmp_path: Path,
) -> None:
    """Both sources declare `where` on one common column whose typed value
    sets are disjoint — legal, whatever their other entries do."""
    events = _events(
        SourceEventSourceDecl(kind="sensor", where={"reading": "5"}, item_type="a"),
        SourceEventSourceDecl(kind="sensor", where={"reading": "9"}, item_type="b"),
    )
    plan = _open_plan(_write_sensor_emit(tmp_path), _config(events=events))
    assert plan.events is not None


def test_disjointness_05_and_5_on_bigint_resolve_one_typed_value_refused(
    tmp_path: Path,
) -> None:
    """`'5'` and `'05'` on a BIGINT column are one typed value, never a
    disjoint pair — string comparison would silently license double-logging."""
    events = _events(
        SourceEventSourceDecl(kind="sensor", where={"reading": "5"}, item_type="a"),
        SourceEventSourceDecl(kind="sensor", where={"reading": "05"}, item_type="b"),
    )
    with pytest.raises(
        SourceEventSourceOverlap, match="selections do not establish disjointness"
    ):
        _open_plan(_write_sensor_emit(tmp_path), _config(events=events))


def test_disjointness_no_common_predicated_column_refused(tmp_path: Path) -> None:
    """No column is common to both sources' `where` — nothing to disjoin."""
    events = _events(
        SourceEventSourceDecl(kind="sensor", where={"reading": "5"}, item_type="a"),
        SourceEventSourceDecl(
            kind="sensor", where={"category": "indoor"}, item_type="b"
        ),
    )
    with pytest.raises(
        SourceEventSourceOverlap, match="selections do not establish disjointness"
    ):
        _open_plan(_write_sensor_emit(tmp_path), _config(events=events))


def test_disjointness_only_one_source_selective_refused(tmp_path: Path) -> None:
    """One source declares no `where` at all — the other's selection alone
    cannot establish disjointness."""
    events = _events(
        SourceEventSourceDecl(kind="sensor", where={"reading": "5"}, item_type="a"),
        SourceEventSourceDecl(kind="sensor", item_type="b"),
    )
    with pytest.raises(
        SourceEventSourceOverlap, match="selections do not establish disjointness"
    ):
        _open_plan(_write_sensor_emit(tmp_path), _config(events=events))


def test_disjointness_every_common_column_intersects_refused(tmp_path: Path) -> None:
    """Both common columns' value sets intersect (identical selections) —
    refused, even though two columns are shared."""
    events = _events(
        SourceEventSourceDecl(
            kind="sensor",
            where={"reading": "5", "category": "indoor"},
            item_type="a",
        ),
        SourceEventSourceDecl(
            kind="sensor",
            where={"reading": "5", "category": "indoor"},
            item_type="b",
        ),
    )
    with pytest.raises(SourceEventSourceOverlap):
        _open_plan(_write_sensor_emit(tmp_path), _config(events=events))


def test_disjointness_one_disjoint_common_column_suffices(tmp_path: Path) -> None:
    """`reading` is disjoint (5 vs 9) despite `category` intersecting on both
    (indoor) — legality is existential, one disjoint column suffices."""
    events = _events(
        SourceEventSourceDecl(
            kind="sensor",
            where={"reading": "5", "category": "indoor"},
            item_type="a",
        ),
        SourceEventSourceDecl(
            kind="sensor",
            where={"reading": "9", "category": "indoor"},
            item_type="b",
        ),
    )
    plan = _open_plan(_write_sensor_emit(tmp_path), _config(events=events))
    assert plan.events is not None


def test_disjointness_membership_disjoint_owner_sub_types_legal(
    tmp_path: Path,
) -> None:
    """Membership sources of one `(kind, property)` with both-declared
    disjoint owner `sub_types` sets are legal — the owner sub-type is the
    population axis — and share one resolved item-type by default (the
    sharing exception extends to membership)."""
    events = _events(
        SourceEventSourceDecl(
            membership=MembershipRef(kind="clinician", property="ward_allocation"),
            sub_types=("day",),
        ),
        SourceEventSourceDecl(
            membership=MembershipRef(kind="clinician", property="ward_allocation"),
            sub_types=("night",),
        ),
    )
    plan = _open_plan(_write_clinician_emit(tmp_path), _config(events=events))
    assert plan.events is not None
    assert {s.item_type for s in plan.events.sources} == {"clinician.ward_allocation"}


def test_disjointness_membership_common_where_column_disjoint_legal(
    tmp_path: Path,
) -> None:
    """Membership sources of one `(kind, property)` with a common owner
    `where` column whose typed value sets are disjoint are legal, with no
    `sub_types` narrowing at all."""
    events = _events(
        SourceEventSourceDecl(
            membership=MembershipRef(kind="clinician", property="ward_allocation"),
            where={"region": "east"},
            item_type="a",
        ),
        SourceEventSourceDecl(
            membership=MembershipRef(kind="clinician", property="ward_allocation"),
            where={"region": "west"},
            item_type="b",
        ),
    )
    plan = _open_plan(_write_clinician_emit(tmp_path), _config(events=events))
    assert plan.events is not None


def test_disjointness_population_disjoint_records_legal_regardless_of_where(
    tmp_path: Path,
) -> None:
    """Two records sources already disjoint by `sub_types` are legal
    regardless of their predicates — even identical, intersecting `where`
    values never reach the selection gate."""
    events = _events(
        SourceEventSourceDecl(
            kind="actor", sub_types=("consultant",), where={"name": "Dr. Lee"}
        ),
        SourceEventSourceDecl(
            kind="actor", sub_types=("nurse",), where={"name": "Dr. Lee"}
        ),
    )
    plan = _open_plan(build_source_test_emit(tmp_path), _config(events=events))
    assert plan.events is not None
