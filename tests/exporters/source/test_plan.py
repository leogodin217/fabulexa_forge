"""Tests for `build_source_plan`: declared-table resolution over populations.

Every fixture is a real (DuckDB-backed) emit built via `_source_fixtures.py`'s
`build_*` helpers, or — for the handful of purely structural cases those
helpers cannot express (an unclassified column, a pre-history_tracked emit, a
multi-branch emit, a partial `presentation_keys` claim) — a small bespoke
fixture built the same way (`_support.sidecar_builder.write_emit` + a bare
DuckDB file). `build_source_plan` now takes the open `Emit`, so every test
resolves an `EffectiveAnchor` and an `Election` first, mirroring the engine's
own resolve-then-plan sequencing (`exporters/source/engine.py`).
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
    ExportError,
    SourceColumnNotAddressable,
    SourceColumnUnresolved,
    SourceEventSourceOverlap,
    SourceHistoryTrackedRequired,
    SourceNameCollision,
    SourceSliceOnlyRead,
    SourceUnclassifiedColumn,
)
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.populations import Population
from fabulexa_forge.exporters.query_spec import TableKeys
from fabulexa_forge.exporters.source.plan import (
    SourceJunctionTablePlan,
    SourceStateTablePlan,
    build_source_plan,
)
from fabulexa_forge.reader.emit import open_emit

from ._source_fixtures import (
    build_empty_source_emit,
    build_slice_only_source_emit,
    build_source_election_emit,
    build_source_keys_emit,
    build_source_test_emit,
)

if TYPE_CHECKING:
    from fabulexa_forge.config.models import KeySurface
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.source.plan import SourcePlan

# ---------------------------------------------------------------------------
# Config + plan-build helpers
# ---------------------------------------------------------------------------


def _config(
    tables: tuple[SourceTableDecl, ...] = (),
    events: SourceEventsDecl | None = None,
    declare_keys: bool = False,
    keys: "dict[str, KeySurface | dict[str, KeySurface]] | None" = None,
) -> ExportConfig:
    """Build a `mode: source` ExportConfig from a declared table/events set."""
    return ExportConfig(
        mode="source",
        source=SourceConfig(tables=tables, events=events, declare_keys=declare_keys),
        keys=keys,
    )


def _open_plan(
    emit_dir: Path,
    config: ExportConfig,
    *,
    windowed: bool = False,
    notice_sink: "NoticeSink" = discard_notice_sink,
) -> "SourcePlan":
    """Open `emit_dir` and build a SourcePlan against it, resolving the anchor
    and election the way the engine does."""
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(
            emit.sidecar.runtime(), config.rebase, None, None
        )
        assert anchor is not None, "every fixture here declares a runtime block"
        election = resolve_election(emit.sidecar, config.keys)
        return build_source_plan(emit, config, anchor, election, windowed, notice_sink)


# ---------------------------------------------------------------------------
# Bespoke structural fixtures (no _source_fixtures.py builder expresses these)
# ---------------------------------------------------------------------------

_RUNTIME_EXTRA: dict[str, object] = {
    "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"}
}


def _write_bare_emit(
    tmp_path: Path,
    table: dict[str, object],
    *,
    branches: list[dict[str, object]] | None = None,
    extra: dict[str, object] | None = None,
    records_shape_valid: bool = True,
) -> Path:
    """Write a minimal single-table emit: an empty run.duckdb (never queried
    by these fixtures' plan builds — default election, no declare_keys data
    reads) plus a base.json carrying exactly `table`."""
    db_path = tmp_path / "run.duckdb"
    duckdb.connect(str(db_path)).close()
    merged_extra = dict(_RUNTIME_EXTRA)
    if extra is not None:
        merged_extra.update(extra)
    write_emit(
        tmp_path,
        tables=[table],
        branches=branches,
        extra=merged_extra,
        records_shape_valid=records_shape_valid,
    )
    return tmp_path


_LIFECYCLE_COLUMNS: list[dict[str, object]] = [
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
]


def build_unclassified_column_emit(tmp_path: Path) -> Path:
    """A records kind carrying one column matching no taxonomy role,
    alongside one conformant tracked column (history_tracked_available must
    read True for the unclassified-column check to be reached)."""
    columns = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        *_LIFECYCLE_COLUMNS,
        identity_column("record_index", "BIGINT"),
        prop_column(
            "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
        ),
        {"name": "mystery", "type": "VARCHAR"},
    ]
    table = {
        "name": "records__gizmo",
        "category": "records",
        "record_kind": "gizmo",
        "columns": columns,
        "rows": 0,
    }
    return _write_bare_emit(tmp_path, table, records_shape_valid=False)


def build_no_history_tracked_emit(tmp_path: Path) -> Path:
    """A records kind whose columns carry no history_tracked flag at all —
    predates the flag (SourceHistoryTrackedRequired's unconditional fixture)."""
    columns = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        *_LIFECYCLE_COLUMNS,
        identity_column("record_index", "BIGINT"),
        {"name": "prop__name", "type": "VARCHAR"},
    ]
    table = {
        "name": "records__thing",
        "category": "records",
        "record_kind": "thing",
        "columns": columns,
        "rows": 0,
    }
    return _write_bare_emit(tmp_path, table)


def build_multi_branch_emit(tmp_path: Path) -> Path:
    """A sidecar declaring two branches — the single-branch guard's fixture."""
    columns = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        *_LIFECYCLE_COLUMNS,
        identity_column("record_index", "BIGINT"),
        prop_column(
            "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
        ),
    ]
    table = {
        "name": "records__thing",
        "category": "records",
        "record_kind": "thing",
        "columns": columns,
        "rows": 0,
    }
    branches = [
        {"fork_path": "trunk", "parent": None, "slice_at": 0},
        {"fork_path": "feature", "parent": "trunk", "slice_at": 0},
    ]
    return _write_bare_emit(tmp_path, table, branches=branches)


def build_partial_presentation_claim_emit(tmp_path: Path) -> Path:
    """A 3-sub-type kind (a/b/c) whose `presentation_keys` registry covers
    only a and b, pairwise-safe counter partitions — c is the uncovered
    'collider' the proper-subset-excluding-collider declare_keys case must
    exclude to still be claimed."""
    columns = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "presentation_id", "type": "VARCHAR"},
        *_LIFECYCLE_COLUMNS,
        identity_column("record_index", "BIGINT"),
        prop_column(
            "prop__gizmo_type",
            "VARCHAR",
            history_tracked=False,
            temporal_class="constant",
        ),
    ]
    table = {
        "name": "records__gizmo",
        "category": "records",
        "record_kind": "gizmo",
        "columns": columns,
        "rows": 0,
    }

    def _counter_partition(prefix: str) -> dict[str, object]:
        return {
            "unique_within": "emit",
            "branch_stable": False,
            "slice_stable": False,
            "key_space": {"class": "counter", "prefix": prefix, "width": 3},
        }

    extra = {
        "enum_domains": {"gizmo": {"gizmo_type": ["a", "b", "c"]}},
        "presentation_keys": {
            "gizmo": {
                "sub_types": {
                    "a": _counter_partition("A_"),
                    "b": _counter_partition("B_"),
                },
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
            },
        },
    }
    return _write_bare_emit(tmp_path, table, extra=extra)


# ---------------------------------------------------------------------------
# Ordering, population resolution, omission-as-exclusion, sharing, collisions
# ---------------------------------------------------------------------------


def test_one_unit_per_declaration_in_order(tmp_path: Path) -> None:
    """`plan.tables` mirrors `tables[]` declaration order; the event log
    lives in its own `events` field (compile places it last — § SourcePlan)."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(name="locs", kind="location"),
                SourceTableDecl(name="ords", kind="order"),
                SourceTableDecl(
                    name="visit_team",
                    membership=MembershipRef(kind="visit", property="team"),
                ),
            ),
            events=SourceEventsDecl(
                name="versions", sources=(SourceEventSourceDecl(kind="visit"),)
            ),
        ),
    )
    assert [t.name for t in plan.tables] == ["locs", "ords", "visit_team"]
    assert isinstance(plan.tables[0], SourceStateTablePlan)
    assert isinstance(plan.tables[2], SourceJunctionTablePlan)
    assert plan.events is not None
    assert plan.events.name == "versions"


def test_zero_row_population_still_yields_its_table(tmp_path: Path) -> None:
    """A declared population materializing zero rows still resolves — plan
    resolution is population-set metadata, not row-count-dependent."""
    plan = _open_plan(
        build_empty_source_emit(tmp_path),
        _config(tables=(SourceTableDecl(name="locs", kind="location"),)),
    )
    assert len(plan.tables) == 1
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert table.populations == (Population(kind="location", sub_type=None),)


def test_undeclared_kind_stays_a_legal_reference_target(tmp_path: Path) -> None:
    """`location` is never declared as a `tables[]` output, but `order`'s
    `prop__location_id` edge still resolves it — omission excludes it from
    export, not from being a legal reference target."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(tables=(SourceTableDecl(name="ords", kind="order"),)),
    )
    assert len(plan.tables) == 1
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert len(table.edge_surfaces) == 1
    edge = table.edge_surfaces[0]
    assert edge.source_column == "prop__location_id"
    assert edge.target_kinds == ("location",)


def test_two_tables_sharing_a_population_both_render_it(tmp_path: Path) -> None:
    """Two `tables[]` entries addressing the same population resolve
    independently — no dedup, no error."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(name="loc_a", kind="location"),
                SourceTableDecl(name="loc_b", kind="location"),
            )
        ),
    )
    assert [t.name for t in plan.tables] == ["loc_a", "loc_b"]
    for table in plan.tables:
        assert isinstance(table, SourceStateTablePlan)
        assert table.populations == (Population(kind="location", sub_type=None),)


def test_table_name_colliding_with_events_name_raises(tmp_path: Path) -> None:
    """A `tables[]` name colliding with the `events.name` (a collision
    `SourceConfig`'s own within-`tables[]` duplicate check cannot see)
    raises SourceNameCollision — never a silent suffix."""
    with pytest.raises(SourceNameCollision):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                tables=(SourceTableDecl(name="versions", kind="location"),),
                events=SourceEventsDecl(
                    name="versions", sources=(SourceEventSourceDecl(kind="visit"),)
                ),
            ),
        )


def test_column_name_collision_via_rename_raises(tmp_path: Path) -> None:
    """A `rename` target colliding with another column's un-renamed default
    output name raises SourceNameCollision, naming the table."""
    with pytest.raises(SourceNameCollision):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                tables=(
                    SourceTableDecl(
                        name="visits",
                        kind="visit",
                        rename={"prop__status": "priority"},
                    ),
                ),
            ),
        )


# ---------------------------------------------------------------------------
# `columns` selection: taxonomy-decided representation
# ---------------------------------------------------------------------------


def test_columns_subset_projects_with_taxonomy_decided_representation(
    tmp_path: Path,
) -> None:
    """`columns` narrows the candidate set; the surviving pairs keep the
    taxonomy's default renames and candidate order, not `columns` order —
    the identity slot always survives regardless of selection."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(
                    name="visits",
                    kind="visit",
                    columns=("prop__status", "created_sim_time"),
                ),
            ),
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert table.columns == (
        ("record_id", "id"),
        ("created_sim_time", "created_at"),
        ("prop__status", "status"),
    )


# ---------------------------------------------------------------------------
# Identity column outside `columns`' reach
# ---------------------------------------------------------------------------


def test_columns_naming_the_elected_identity_surface_not_addressable(
    tmp_path: Path,
) -> None:
    """Under the default (record_id) election, `columns` naming `record_id`
    (the elected identity surface) raises SourceColumnNotAddressable —
    identity is election-governed, not selection-governed."""
    with pytest.raises(SourceColumnNotAddressable):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                tables=(
                    SourceTableDecl(
                        name="visits", kind="visit", columns=("record_id",)
                    ),
                ),
            ),
        )


def test_columns_naming_the_unrendered_default_surface_unresolved(
    tmp_path: Path,
) -> None:
    """Under a non-record_id election, `columns` naming `record_id` (an
    unrendered surface) raises SourceColumnUnresolved, naming the election."""
    with pytest.raises(SourceColumnUnresolved):
        _open_plan(
            build_source_keys_emit(tmp_path),
            _config(
                tables=(
                    SourceTableDecl(
                        name="visits", kind="visit", columns=("record_id",)
                    ),
                ),
                keys={"visit": "presentation_id"},
            ),
        )


# ---------------------------------------------------------------------------
# `rename`: source-name keys, identity keyed on the elected surface
# ---------------------------------------------------------------------------


def test_rename_keyed_on_source_column_name(tmp_path: Path) -> None:
    """`rename` keys on source column names."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(
                    name="locs", kind="location", rename={"prop__name": "site_name"}
                ),
            ),
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert ("prop__name", "site_name") in table.columns


def test_identity_rename_keyed_on_elected_surface_contract_name(
    tmp_path: Path,
) -> None:
    """The identity slot's rename key is the elected surface's contract
    column name (`record_id` under the default election), not its output
    default (`id`)."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(
                    name="locs", kind="location", rename={"record_id": "loc_id"}
                ),
            ),
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert ("record_id", "loc_id") in table.columns


# ---------------------------------------------------------------------------
# Mechanism columns: unaddressable
# ---------------------------------------------------------------------------


def test_fork_path_not_addressable(tmp_path: Path) -> None:
    """`fork_path` is never addressable via `columns`."""
    with pytest.raises(SourceColumnNotAddressable):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                tables=(
                    SourceTableDecl(
                        name="visits", kind="visit", columns=("fork_path",)
                    ),
                ),
            ),
        )


def test_ref_index_not_addressable(tmp_path: Path) -> None:
    """A `ref_index__*` mechanism column is never addressable via `columns`."""
    with pytest.raises(SourceColumnNotAddressable):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                tables=(
                    SourceTableDecl(
                        name="ords", kind="order", columns=("ref_index__location_id",)
                    ),
                ),
            ),
        )


# ---------------------------------------------------------------------------
# slice_only: named -> refused; auto-projection -> notice-omitted
# ---------------------------------------------------------------------------


def test_slice_only_column_named_in_columns_refused(tmp_path: Path) -> None:
    """A `columns` entry naming a non-exempt slice_only column raises
    SourceSliceOnlyRead — unsatisfiable, never silently dropped."""
    with pytest.raises(SourceSliceOnlyRead):
        _open_plan(
            build_slice_only_source_emit(tmp_path),
            _config(
                tables=(
                    SourceTableDecl(
                        name="patients",
                        kind="patient",
                        columns=("prop__loyalty_tier",),
                    ),
                ),
            ),
        )


def test_slice_only_column_auto_projection_omits_with_notice(tmp_path: Path) -> None:
    """Absent `columns`, a non-exempt slice_only column is policy-omitted
    with a notice, never included."""
    sink = RecordingNoticeSink()
    plan = _open_plan(
        build_slice_only_source_emit(tmp_path),
        _config(tables=(SourceTableDecl(name="patients", kind="patient"),)),
        notice_sink=sink,
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert all(src != "prop__loyalty_tier" for src, _ in table.columns)
    assert any(n.code == "slice-only-column-omitted" for n in sink.notices)


# ---------------------------------------------------------------------------
# Discriminator: retained at >= 2 populations, dropped at 1 unless listed
# ---------------------------------------------------------------------------


def test_discriminator_retained_at_two_or_more_populations(tmp_path: Path) -> None:
    """`shift` (domain day/night) with no `sub_types` narrowing addresses
    both populations — the discriminator column is retained by default."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(tables=(SourceTableDecl(name="shifts", kind="shift"),)),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert any(src == "prop__shift_type" for src, _ in table.columns)


def test_discriminator_dropped_at_one_population_by_default(tmp_path: Path) -> None:
    """A single-sub_type-narrowed table drops the discriminator by default."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            tables=(SourceTableDecl(name="shifts", kind="shift", sub_types=("day",)),),
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert all(src != "prop__shift_type" for src, _ in table.columns)


def test_discriminator_retained_at_one_population_when_listed(
    tmp_path: Path,
) -> None:
    """`columns` naming the discriminator explicitly overrides the default
    single-population drop rule."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(
                    name="shifts",
                    kind="shift",
                    sub_types=("day",),
                    columns=("prop__shift_type",),
                ),
            ),
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert any(src == "prop__shift_type" for src, _ in table.columns)


# ---------------------------------------------------------------------------
# `events`: source overlap
# ---------------------------------------------------------------------------


def test_events_sources_overlap_raises(tmp_path: Path) -> None:
    """Two `events.sources` entries addressing the same population raise
    SourceEventSourceOverlap."""
    with pytest.raises(SourceEventSourceOverlap):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                events=SourceEventsDecl(
                    name="versions",
                    sources=(
                        SourceEventSourceDecl(kind="visit"),
                        SourceEventSourceDecl(kind="visit", only=("status",)),
                    ),
                ),
            ),
        )


# ---------------------------------------------------------------------------
# `events`: audited-set resolution
# ---------------------------------------------------------------------------


def test_audited_set_only_narrows(tmp_path: Path) -> None:
    """A records events source's `only` narrows the audited property set."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            events=SourceEventsDecl(
                name="versions",
                sources=(SourceEventSourceDecl(kind="visit", only=("status",)),),
            ),
        ),
    )
    assert plan.events is not None
    assert plan.events.sources[0].audited_properties == ("status",)


def test_audited_set_ignore_widens_by_subtraction(tmp_path: Path) -> None:
    """A records events source's `ignore` widens the default set by
    subtraction."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            events=SourceEventsDecl(
                name="versions",
                sources=(SourceEventSourceDecl(kind="visit", ignore=("priority",)),),
            ),
        ),
    )
    assert plan.events is not None
    assert plan.events.sources[0].audited_properties == ("status",)


def test_audited_set_membership_element_fields(tmp_path: Path) -> None:
    """A membership events source's default audited set is every
    element-schema field, bare names, first-seen order."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            events=SourceEventsDecl(
                name="versions",
                sources=(
                    SourceEventSourceDecl(
                        membership=MembershipRef(kind="visit", property="team")
                    ),
                ),
            ),
        ),
    )
    assert plan.events is not None
    assert plan.events.sources[0].audited_properties == ("role_name", "actor")


# ---------------------------------------------------------------------------
# Records-column taxonomy: unclassified column
# ---------------------------------------------------------------------------


def test_unclassified_records_column_raises(tmp_path: Path) -> None:
    """A records column matching no taxonomy role raises
    SourceUnclassifiedColumn."""
    with pytest.raises(SourceUnclassifiedColumn):
        _open_plan(
            build_unclassified_column_emit(tmp_path),
            _config(tables=(SourceTableDecl(name="gizmos", kind="gizmo"),)),
        )


# ---------------------------------------------------------------------------
# Windowed plan: `last_mutation_sim_time` refused
# ---------------------------------------------------------------------------


def test_windowed_plan_refuses_last_mutation_sim_time(tmp_path: Path) -> None:
    """Under a windowed plan the state render omits `updated_at`, so a
    `columns` entry naming `last_mutation_sim_time` is unsatisfiable."""
    with pytest.raises(SourceColumnUnresolved):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                tables=(
                    SourceTableDecl(
                        name="visits",
                        kind="visit",
                        columns=("last_mutation_sim_time",),
                    ),
                ),
            ),
            windowed=True,
        )


# ---------------------------------------------------------------------------
# Reserved-name check: output tables including the log
# ---------------------------------------------------------------------------


def test_reserved_table_name_refused(tmp_path: Path) -> None:
    """A `tables[]` name colliding with an incremental bookkeeping table name
    is refused."""
    with pytest.raises(ExportError):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(tables=(SourceTableDecl(name="_export_meta", kind="location"),)),
        )


def test_reserved_log_name_refused(tmp_path: Path) -> None:
    """The event log's name is checked under the same reserved-name rule."""
    with pytest.raises(ExportError):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                events=SourceEventsDecl(
                    name="_export_windows",
                    sources=(SourceEventSourceDecl(kind="visit"),),
                ),
            ),
        )


# ---------------------------------------------------------------------------
# SourceHistoryTrackedRequired: unconditional
# ---------------------------------------------------------------------------


def test_history_tracked_required_unconditional(tmp_path: Path) -> None:
    """An emit predating per-column history_tracked flags is refused
    regardless of any other declared config."""
    with pytest.raises(SourceHistoryTrackedRequired):
        _open_plan(
            build_no_history_tracked_emit(tmp_path),
            _config(tables=(SourceTableDecl(name="things", kind="thing"),)),
        )


# ---------------------------------------------------------------------------
# Single-branch guard
# ---------------------------------------------------------------------------


def test_single_branch_guard(tmp_path: Path) -> None:
    """A multi-branch emit is refused (trunk-only stage)."""
    with pytest.raises(ExportError):
        _open_plan(
            build_multi_branch_emit(tmp_path),
            _config(tables=(SourceTableDecl(name="things", kind="thing"),)),
        )


# ---------------------------------------------------------------------------
# declare_keys: PK always; UNIQUE(presentation_id) follows combined_claim
# ---------------------------------------------------------------------------


def test_declared_keys_flat_kind_whole_table_claim(tmp_path: Path) -> None:
    """A flat kind reads the whole-table `key` claim: primary key on the
    identity output name, unique on presentation_id when claimed."""
    plan = _open_plan(
        build_source_keys_emit(tmp_path),
        _config(
            tables=(SourceTableDecl(name="visits", kind="visit"),), declare_keys=True
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert table.keys is not None
    assert table.keys.primary_key == ("id",)
    assert table.keys.unique == (("presentation_id",),)


def test_declared_keys_single_population_presence_is_the_claim(
    tmp_path: Path,
) -> None:
    """A single-sub_type-narrowed table reads its sub-type's own partition
    entry — presence is the claim."""
    plan = _open_plan(
        build_source_keys_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(
                    name="consultants", kind="actor", sub_types=("consultant",)
                ),
            ),
            declare_keys=True,
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert table.keys is not None
    assert table.keys.unique == (("presentation_id",),)


def test_declared_keys_addressed_population_without_entry_declares_nothing(
    tmp_path: Path,
) -> None:
    """A sub-type with no partition entry in the registry declares identity
    keys only."""
    plan = _open_plan(
        build_source_keys_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(name="nurses", kind="actor", sub_types=("nurse",)),
            ),
            declare_keys=True,
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert table.keys is not None
    assert table.keys.unique == ()


def test_declared_keys_full_domain_combined_claim(tmp_path: Path) -> None:
    """A table addressing a sub-typed kind's full domain reads the combined
    claim over every sub-type's partition entry (pairwise-safe counter
    classes with distinct prefixes)."""
    plan = _open_plan(
        build_source_election_emit(tmp_path),
        _config(
            tables=(SourceTableDecl(name="devices", kind="device"),), declare_keys=True
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert table.keys is not None
    assert table.keys.unique == (("presentation_id",),)


def test_declared_keys_proper_subset_excluding_collider(tmp_path: Path) -> None:
    """A table addressing a proper subset of a sub-typed kind's domain that
    excludes the one sub-type without a registry entry (the 'collider') is
    still claimed over its own (pairwise-safe) subset."""
    plan = _open_plan(
        build_partial_presentation_claim_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(name="gizmos", kind="gizmo", sub_types=("a", "b")),
            ),
            declare_keys=True,
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert table.keys is not None
    assert table.keys.unique == (("presentation_id",),)


def test_declared_keys_full_domain_including_collider_declares_nothing(
    tmp_path: Path,
) -> None:
    """Addressing the full 3-sub-type domain (including the uncovered
    collider) drops the claim entirely — an uncovered population declares
    nothing."""
    plan = _open_plan(
        build_partial_presentation_claim_emit(tmp_path),
        _config(
            tables=(SourceTableDecl(name="gizmos", kind="gizmo"),), declare_keys=True
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert table.keys is not None
    assert table.keys.unique == ()


def test_declared_keys_junction_declares_nothing(tmp_path: Path) -> None:
    """A junction table's plan unit carries no `keys` field at all."""
    plan = _open_plan(
        build_source_keys_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(
                    name="visit_team",
                    membership=MembershipRef(kind="visit", property="team"),
                ),
            ),
            declare_keys=True,
        ),
    )
    unit = plan.tables[0]
    assert isinstance(unit, SourceJunctionTablePlan)
    assert not hasattr(unit, "keys")


def test_declared_keys_event_log_declares_id_primary_key(tmp_path: Path) -> None:
    """Under `declare_keys`, the event-log plan unit carries `PRIMARY KEY
    (id)` — a constant of the mode, since `id` is true by construction."""
    plan = _open_plan(
        build_source_keys_emit(tmp_path),
        _config(
            events=SourceEventsDecl(
                name="versions", sources=(SourceEventSourceDecl(kind="visit"),)
            ),
            declare_keys=True,
        ),
    )
    assert plan.events is not None
    assert plan.events.keys == TableKeys(primary_key=("id",), unique=())


def test_declared_keys_off_event_log_carries_no_keys(tmp_path: Path) -> None:
    """With `declare_keys` off, the event-log plan unit's `keys` is None."""
    plan = _open_plan(
        build_source_keys_emit(tmp_path),
        _config(
            events=SourceEventsDecl(
                name="versions", sources=(SourceEventSourceDecl(kind="visit"),)
            ),
            declare_keys=False,
        ),
    )
    assert plan.events is not None
    assert plan.events.keys is None
