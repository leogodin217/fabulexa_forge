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
    DateParseElection,
    ExportConfig,
    MembershipRef,
    SourceConfig,
    SourceEventsDecl,
    SourceEventSourceDecl,
    SourceTableDecl,
)
from fabulexa_forge.errors import (
    DateParseSourceColumn,
    ExportError,
    RenderKeyResolves,
    SourceColumnNotAddressable,
    SourceColumnUnresolved,
    SourceEventSourceOverlap,
    SourceHistoryTrackedRequired,
    SourceItemTypeCollision,
    SourceKindLabelCollision,
    SourceKindLabelUnknown,
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
    kind_labels: "dict[str, str] | None" = None,
) -> ExportConfig:
    """Build a `mode: source` ExportConfig from a declared table/events set."""
    return ExportConfig(
        mode="source",
        source=SourceConfig(
            tables=tables,
            events=events,
            declare_keys=declare_keys,
            kind_labels=kind_labels,
        ),
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
    assert plan.events.sources[0].audited_properties == (("status", "status"),)


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
    assert plan.events.sources[0].audited_properties == (("status", "status"),)


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
    assert plan.events.sources[0].audited_properties == (
        ("role_name", "role_name"),
        ("actor", "actor"),
    )


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


# ---------------------------------------------------------------------------
# kind_labels: resolution + validation (Phase 2 — junction rendering only;
# labels do not yet reach the event log, Phase 3)
# ---------------------------------------------------------------------------


def test_kind_labels_absent_resolves_empty_on_every_junction_unit(
    tmp_path: Path,
) -> None:
    """No `kind_labels` declared -> every junction unit carries the empty
    tuple."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(
                    name="visit_team",
                    membership=MembershipRef(kind="visit", property="team"),
                ),
            ),
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceJunctionTablePlan)
    assert table.kind_labels == ()


def test_kind_labels_resolve_onto_every_junction_unit_in_declaration_order(
    tmp_path: Path,
) -> None:
    """`kind_labels` resolves onto every junction unit, declaration order."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(
                    name="visit_team",
                    membership=MembershipRef(kind="visit", property="team"),
                ),
            ),
            kind_labels={"actor": "clinician", "visit": "encounter"},
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceJunctionTablePlan)
    assert table.kind_labels == (("actor", "clinician"), ("visit", "encounter"))


def test_kind_labels_unknown_kind_raises(tmp_path: Path) -> None:
    """A `kind_labels` key naming no records kind raises SourceKindLabelUnknown
    with the design doc message."""
    with pytest.raises(
        SourceKindLabelUnknown, match="kind_labels: kind 'ghost' not in this emit"
    ):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                tables=(SourceTableDecl(name="locs", kind="location"),),
                kind_labels={"ghost": "phantom"},
            ),
        )


def test_kind_labels_label_equals_unlabeled_kind_name_raises(tmp_path: Path) -> None:
    """A label equal to an *unlabeled* kind's own name raises
    SourceKindLabelCollision, even when that kind names no declared `tables[]`
    entry — the whole-kind-universe range."""
    with pytest.raises(
        SourceKindLabelCollision,
        match="kind_labels: label 'location' collides with kind 'location'",
    ):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                tables=(SourceTableDecl(name="visits", kind="visit"),),
                kind_labels={"actor": "location"},
            ),
        )


# ---------------------------------------------------------------------------
# events: item-type resolution (design doc § Item-type resolution, one test
# per row)
# ---------------------------------------------------------------------------


def _events_plan(
    tmp_path: Path,
    sources: "tuple[SourceEventSourceDecl, ...]",
    *,
    kind_labels: "dict[str, str] | None" = None,
) -> "SourcePlan":
    """Build a `versions` events-only plan over `build_source_test_emit`."""
    return _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            events=SourceEventsDecl(name="versions", sources=sources),
            kind_labels=kind_labels,
        ),
    )


def test_item_type_declared_override_wins(tmp_path: Path) -> None:
    """A declared `item_type` wins over the kind-label / verbatim default."""
    plan = _events_plan(
        tmp_path, (SourceEventSourceDecl(kind="visit", item_type="episode"),)
    )
    assert plan.events is not None
    assert plan.events.sources[0].item_type == "episode"


def test_item_type_records_label(tmp_path: Path) -> None:
    """A records source's labeled kind resolves to the label."""
    plan = _events_plan(
        tmp_path,
        (SourceEventSourceDecl(kind="visit"),),
        kind_labels={"visit": "encounter"},
    )
    assert plan.events is not None
    assert plan.events.sources[0].item_type == "encounter"


def test_item_type_records_verbatim(tmp_path: Path) -> None:
    """A records source's unlabeled kind resolves verbatim (today's
    behavior)."""
    plan = _events_plan(tmp_path, (SourceEventSourceDecl(kind="visit"),))
    assert plan.events is not None
    assert plan.events.sources[0].item_type == "visit"


def test_item_type_membership_owner_labeled(tmp_path: Path) -> None:
    """A membership source's owner-kind label produces `<label(K)>.<property>`."""
    plan = _events_plan(
        tmp_path,
        (
            SourceEventSourceDecl(
                membership=MembershipRef(kind="visit", property="team")
            ),
        ),
        kind_labels={"visit": "encounter"},
    )
    assert plan.events is not None
    assert plan.events.sources[0].item_type == "encounter.team"


def test_item_type_membership_owner_verbatim(tmp_path: Path) -> None:
    """A membership source's unlabeled owner kind produces `<K>.<property>`
    (today's behavior)."""
    plan = _events_plan(
        tmp_path,
        (
            SourceEventSourceDecl(
                membership=MembershipRef(kind="visit", property="team")
            ),
        ),
    )
    assert plan.events is not None
    assert plan.events.sources[0].item_type == "visit.team"


# ---------------------------------------------------------------------------
# events: item-type distinctness (design doc § Item-type distinctness, one
# test per row)
# ---------------------------------------------------------------------------


def test_item_type_two_records_sources_one_kind_sharing_is_legal(
    tmp_path: Path,
) -> None:
    """Two records sources of one kind resolving one item-type is legal —
    the joint union-safety-gate group, today's shape for a kind split across
    sources."""
    plan = _events_plan(
        tmp_path,
        (
            SourceEventSourceDecl(kind="actor", sub_types=("consultant",)),
            SourceEventSourceDecl(kind="actor", sub_types=("nurse",)),
        ),
    )
    assert plan.events is not None
    assert plan.events.sources[0].item_type == "actor"
    assert plan.events.sources[1].item_type == "actor"


def test_item_type_two_records_sources_different_kinds_sharing_refused(
    tmp_path: Path,
) -> None:
    """Two records sources of different kinds resolving one item-type is
    refused."""
    with pytest.raises(
        SourceItemTypeCollision,
        match=(
            "events: sources #1 and #2 resolve one item_type 'shared' over"
            " two audited item spaces"
        ),
    ):
        _events_plan(
            tmp_path,
            (
                SourceEventSourceDecl(kind="visit", item_type="shared"),
                SourceEventSourceDecl(kind="location", item_type="shared"),
            ),
        )


def test_item_type_membership_sharing_any_source_refused(tmp_path: Path) -> None:
    """A membership source resolving the same item-type as any other source
    (its own owner's included) is refused."""
    with pytest.raises(SourceItemTypeCollision, match="sources #1 and #2"):
        _events_plan(
            tmp_path,
            (
                SourceEventSourceDecl(kind="visit"),
                SourceEventSourceDecl(
                    membership=MembershipRef(kind="visit", property="team"),
                    item_type="visit",
                ),
            ),
        )


def test_item_type_two_records_sources_one_kind_differing_is_legal(
    tmp_path: Path,
) -> None:
    """Two records sources of one kind resolving *different* item-types is
    legal — the union-safety gate re-partitions and runs per resolved
    item-type separately."""
    plan = _events_plan(
        tmp_path,
        (
            SourceEventSourceDecl(
                kind="actor", sub_types=("consultant",), item_type="clinician"
            ),
            SourceEventSourceDecl(kind="actor", sub_types=("nurse",)),
        ),
    )
    assert plan.events is not None
    assert plan.events.sources[0].item_type == "clinician"
    assert plan.events.sources[1].item_type == "actor"


def test_item_type_records_equals_another_kinds_rendered_name_refused(
    tmp_path: Path,
) -> None:
    """A records source's item-type equal to another kind's rendered name is
    refused — including an unaudited, undeclared kind (whole-universe
    range)."""
    with pytest.raises(
        SourceItemTypeCollision,
        match="events source #1: item_type 'shift' collides with kind 'shift'",
    ):
        _events_plan(
            tmp_path, (SourceEventSourceDecl(kind="visit", item_type="shift"),)
        )


def test_item_type_membership_equals_any_kinds_rendered_name_refused(
    tmp_path: Path,
) -> None:
    """A membership source's item-type equal to the rendered name of any
    kind (its owner's included) is refused."""
    with pytest.raises(
        SourceItemTypeCollision,
        match="events source #1: item_type 'visit' collides with kind 'visit'",
    ):
        _events_plan(
            tmp_path,
            (
                SourceEventSourceDecl(
                    membership=MembershipRef(kind="visit", property="team"),
                    item_type="visit",
                ),
            ),
        )


# ---------------------------------------------------------------------------
# events: `changes` key resolution via `rename` (design doc § `changes` key
# resolution, one test per row)
# ---------------------------------------------------------------------------


def test_rename_key_resolves_to_output_key(tmp_path: Path) -> None:
    """A renamed audited property's pair uses the entry's value as the
    `changes` output key."""
    plan = _events_plan(
        tmp_path,
        (SourceEventSourceDecl(kind="visit", rename={"status": "state"}),),
    )
    assert plan.events is not None
    assert ("status", "state") in plan.events.sources[0].audited_properties


def test_unrenamed_property_keeps_its_bare_name(tmp_path: Path) -> None:
    """An audited property without a `rename` entry keeps its bare name as
    the output key."""
    plan = _events_plan(
        tmp_path,
        (SourceEventSourceDecl(kind="visit", rename={"status": "state"}),),
    )
    assert plan.events is not None
    assert ("priority", "priority") in plan.events.sources[0].audited_properties


def test_membership_reference_field_rename_addresses_bare_name(
    tmp_path: Path,
) -> None:
    """A membership reference field's `rename` entry (and its `only`) key on
    the bare field name `f`, resolving the pair's output key."""
    plan = _events_plan(
        tmp_path,
        (
            SourceEventSourceDecl(
                membership=MembershipRef(kind="visit", property="team"),
                only=("role_name", "actor"),
                rename={"actor": "member"},
            ),
        ),
    )
    assert plan.events is not None
    assert plan.events.sources[0].audited_properties == (
        ("role_name", "role_name"),
        ("actor", "member"),
    )


def test_rename_value_colliding_with_unrenamed_bare_name_refused(
    tmp_path: Path,
) -> None:
    """A `rename` value colliding with another property's unrenamed bare
    name raises SourceNameCollision with the "changes key collision"
    message."""
    with pytest.raises(SourceNameCollision, match="changes key collision"):
        _events_plan(
            tmp_path,
            (SourceEventSourceDecl(kind="visit", rename={"status": "priority"}),),
        )


def test_rename_value_colliding_with_membership_pair_expansion_refused(
    tmp_path: Path,
) -> None:
    """A `rename` value colliding with a membership reference pair's
    expanded `_kind` / `_id` name raises SourceNameCollision."""
    with pytest.raises(SourceNameCollision, match="changes key collision"):
        _events_plan(
            tmp_path,
            (
                SourceEventSourceDecl(
                    membership=MembershipRef(kind="visit", property="team"),
                    rename={"role_name": "actor_kind"},
                ),
            ),
        )


def test_rename_key_not_a_property_of_its_source_refused(tmp_path: Path) -> None:
    """A `rename` key naming no property of its source raises
    SourceColumnUnresolved."""
    with pytest.raises(SourceColumnUnresolved, match="not a column of its source"):
        _events_plan(
            tmp_path,
            (SourceEventSourceDecl(kind="visit", rename={"bogus": "x"}),),
        )


def test_rename_key_excluded_by_only_refused(tmp_path: Path) -> None:
    """A `rename` key naming a property excluded by `only` raises
    SourceColumnUnresolved — the entry is unsatisfiable, never a silent
    ignore."""
    with pytest.raises(SourceColumnUnresolved, match="not a column of its source"):
        _events_plan(
            tmp_path,
            (
                SourceEventSourceDecl(
                    kind="visit", only=("status",), rename={"priority": "prio"}
                ),
            ),
        )


def test_rename_key_naming_non_exempt_slice_only_refused(tmp_path: Path) -> None:
    """A `rename` key naming a non-exempt `slice_only` property raises
    SourceSliceOnlyRead."""
    with pytest.raises(SourceSliceOnlyRead):
        _open_plan(
            build_slice_only_source_emit(tmp_path),
            _config(
                events=SourceEventsDecl(
                    name="versions",
                    sources=(
                        SourceEventSourceDecl(
                            kind="patient", rename={"loyalty_tier": "tier"}
                        ),
                    ),
                ),
            ),
        )


# ---------------------------------------------------------------------------
# events: `kind_labels` threading
# ---------------------------------------------------------------------------


def test_kind_labels_thread_onto_every_event_source_plan(tmp_path: Path) -> None:
    """The resolved `kind_labels` map threads onto every event-source plan,
    declaration order."""
    plan = _events_plan(
        tmp_path,
        (
            SourceEventSourceDecl(kind="visit"),
            SourceEventSourceDecl(
                membership=MembershipRef(kind="visit", property="team")
            ),
        ),
        kind_labels={"actor": "clinician", "visit": "encounter"},
    )
    assert plan.events is not None
    expected = (("actor", "clinician"), ("visit", "encounter"))
    assert plan.events.sources[0].kind_labels == expected
    assert plan.events.sources[1].kind_labels == expected


def test_kind_labels_absent_resolves_empty_on_every_event_source_plan(
    tmp_path: Path,
) -> None:
    """No `kind_labels` declared -> every event-source plan carries the
    empty tuple."""
    plan = _events_plan(tmp_path, (SourceEventSourceDecl(kind="visit"),))
    assert plan.events is not None
    assert plan.events.sources[0].kind_labels == ()


# ---------------------------------------------------------------------------
# `render`: structural-instant rendering elections (state + junction)
# ---------------------------------------------------------------------------


def test_render_state_table_elects_types_on_every_instant_column(
    tmp_path: Path,
) -> None:
    """A `render` map covering every one of a records table's instant-carrying
    structural columns (`structural_instant_columns('records')`) resolves
    onto `table.render`, declaration order."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(
                    name="visits",
                    kind="visit",
                    render={
                        "created_sim_time": "date",
                        "deactivated_at": "timestamptz",
                        "last_mutation_sim_time": "time",
                    },
                ),
            ),
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert table.render == (
        ("created_sim_time", "date"),
        ("deactivated_at", "timestamptz"),
        ("last_mutation_sim_time", "time"),
    )


def test_render_junction_table_elects_types_on_interval_columns(
    tmp_path: Path,
) -> None:
    """A `render` map on a junction unit's interval columns
    (`joined_sim_time` / `left_sim_time`, `structural_instant_columns
    ('membership')`) resolves the same way — the state render's twin."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(
                    name="visit_team",
                    membership=MembershipRef(kind="visit", property="team"),
                    render={"joined_sim_time": "date", "left_sim_time": "date"},
                ),
            ),
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceJunctionTablePlan)
    assert table.render == (("joined_sim_time", "date"), ("left_sim_time", "date"))


def test_render_key_on_state_payload_column_refused(tmp_path: Path) -> None:
    """A `render` key naming a payload (non-instant) column on a `state`
    table raises RenderKeyResolves."""
    with pytest.raises(RenderKeyResolves):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                tables=(
                    SourceTableDecl(
                        name="visits", kind="visit", render={"prop__status": "date"}
                    ),
                ),
            ),
        )


def test_render_key_on_junction_non_interval_column_refused(tmp_path: Path) -> None:
    """A `render` key naming a non-interval column on a `junction` table
    raises RenderKeyResolves — the membership category's own instant
    set."""
    with pytest.raises(RenderKeyResolves):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                tables=(
                    SourceTableDecl(
                        name="visit_team",
                        membership=MembershipRef(kind="visit", property="team"),
                        render={"elem__role_name": "date"},
                    ),
                ),
            ),
        )


def test_render_key_on_columns_omitted_column_refused(tmp_path: Path) -> None:
    """A `render` key naming a column the table's `columns` selection omits
    is refused — the existing omitted-declaration posture `rename` already
    carries."""
    with pytest.raises(SourceColumnUnresolved):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                tables=(
                    SourceTableDecl(
                        name="visits",
                        kind="visit",
                        columns=("prop__status",),
                        render={"created_sim_time": "date"},
                    ),
                ),
            ),
        )


def test_render_key_on_windowed_omitted_column_refused(tmp_path: Path) -> None:
    """Under a windowed plan the state render omits `updated_at`
    (`last_mutation_sim_time`), so a `render` key naming it is unsatisfiable —
    the windowed omitted-column posture composes for free through the shared
    two-stage gate."""
    with pytest.raises(SourceColumnUnresolved):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                tables=(
                    SourceTableDecl(
                        name="visits",
                        kind="visit",
                        render={"last_mutation_sim_time": "date"},
                    ),
                ),
            ),
            windowed=True,
        )


def test_render_key_composes_with_rename(tmp_path: Path) -> None:
    """`render` keys are source identities: a renamed column stays
    addressable by its source name in `render`, and the renamed output name
    still lands in `table.columns`."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(
                    name="visits",
                    kind="visit",
                    rename={"created_sim_time": "made_at"},
                    render={"created_sim_time": "date"},
                ),
            ),
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert ("created_sim_time", "made_at") in table.columns
    assert table.render == (("created_sim_time", "date"),)


# ---------------------------------------------------------------------------
# `render`: declared date parses (state + junction) — the `date_parse`
# typed election, folded into the unified `render` map
# ---------------------------------------------------------------------------


def test_date_parse_resolves_on_state_table_payload_varchar(tmp_path: Path) -> None:
    """A `date_parse` election on a `state` table's payload VARCHAR resolves
    onto `table.render`."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(
                    name="locs",
                    kind="location",
                    render={"prop__name": DateParseElection(date_parse="%Y-%m-%d")},
                ),
            ),
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert table.render == (("prop__name", DateParseElection(date_parse="%Y-%m-%d")),)


def test_date_parse_resolves_on_junction_elem_field(tmp_path: Path) -> None:
    """A `date_parse` election on a junction table's `elem__<f>` scalar
    payload column resolves onto `table.render`."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(
                    name="visit_team",
                    membership=MembershipRef(kind="visit", property="team"),
                    render={
                        "elem__role_name": DateParseElection(date_parse="%Y-%m-%d")
                    },
                ),
            ),
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceJunctionTablePlan)
    assert table.render == (
        ("elem__role_name", DateParseElection(date_parse="%Y-%m-%d")),
    )


def test_date_parse_non_varchar_source_refused(tmp_path: Path) -> None:
    """A `date_parse` election on a non-VARCHAR declared column raises
    DateParseSourceColumn."""
    with pytest.raises(DateParseSourceColumn):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                tables=(
                    SourceTableDecl(
                        name="ords",
                        kind="order",
                        render={
                            "prop__amount": DateParseElection(date_parse="%Y-%m-%d")
                        },
                    ),
                ),
            ),
        )


def test_date_parse_on_slice_only_source_refused(tmp_path: Path) -> None:
    """A `date_parse` election naming a non-exempt slice_only column raises
    SourceSliceOnlyRead — the mode's omission posture composing with the
    parse's own refusal (surface-list growth)."""
    with pytest.raises(SourceSliceOnlyRead):
        _open_plan(
            build_slice_only_source_emit(tmp_path),
            _config(
                tables=(
                    SourceTableDecl(
                        name="patients",
                        kind="patient",
                        render={
                            "prop__loyalty_tier": DateParseElection(
                                date_parse="%Y-%m-%d"
                            )
                        },
                    ),
                ),
            ),
        )


def test_date_parse_key_composes_with_rename(tmp_path: Path) -> None:
    """`date_parse` keys are source identities, composing with `rename` the
    same way other `render` keys do."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            tables=(
                SourceTableDecl(
                    name="locs",
                    kind="location",
                    rename={"prop__name": "site_name"},
                    render={"prop__name": DateParseElection(date_parse="%Y-%m-%d")},
                ),
            ),
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert ("prop__name", "site_name") in table.columns
    assert table.render == (("prop__name", DateParseElection(date_parse="%Y-%m-%d")),)


# ---------------------------------------------------------------------------
# events: `render` on the log's one legal key (`event_sim_time`,
# mode-definitional)
# ---------------------------------------------------------------------------


def test_events_render_absent_defaults_to_timestamp(tmp_path: Path) -> None:
    """Absent `events.render`, `plan.events.render` is the mode-definitional
    default `timestamp`."""
    plan = _events_plan(tmp_path, (SourceEventSourceDecl(kind="visit"),))
    assert plan.events is not None
    assert plan.events.render == "timestamp"


def test_events_render_elects_event_sim_time_rendering(tmp_path: Path) -> None:
    """`events.render` keyed on `event_sim_time` resolves onto
    `plan.events.render`."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            events=SourceEventsDecl(
                name="versions",
                sources=(SourceEventSourceDecl(kind="visit"),),
                render={"event_sim_time": "date"},
            ),
        ),
    )
    assert plan.events is not None
    assert plan.events.render == "date"


def test_events_render_key_other_than_event_sim_time_refused(tmp_path: Path) -> None:
    """An `events.render` key other than `event_sim_time` raises
    RenderKeyResolves, naming the log's one legal key."""
    with pytest.raises(
        RenderKeyResolves, match="the log's one legal key is 'event_sim_time'"
    ):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                events=SourceEventsDecl(
                    name="versions",
                    sources=(SourceEventSourceDecl(kind="visit"),),
                    render={"occurred_at": "date"},
                ),
            ),
        )
