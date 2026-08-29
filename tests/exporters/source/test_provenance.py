"""Tests for source-mode provenance stamping (`exporters/source/plan.py`).

Covers `ColumnProvenance` on `SourceStateTablePlan` / `SourceJunctionTablePlan`
and `KindValueEntry` on `SourceEventLogPlan.kind_values`, per the
documentation-channel sprint spec § Phase 4: a state table's carried, renamed,
elected-identity, and rendered columns all stamp; a junction table's carried
columns stamp keyed against its `membership__<K>__<p>` source; the event
log's `item_type` kind_values entries carry post-`kind_labels` labels and
raw source kinds in source-declaration order, while the log's own
`provenance` stays permanently empty (every log column is computed); and
plan units default `provenance` to empty when constructed directly,
bypassing their builder.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from _support.notices import discard_notice_sink

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    ExportConfig,
    MembershipRef,
    SourceConfig,
    SourceEventsDecl,
    SourceEventSourceDecl,
    SourceTableDecl,
)
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.query_spec import ColumnProvenance, KindValueEntry
from fabulexa_forge.exporters.source.engine import build_source_query_specs
from fabulexa_forge.exporters.source.plan import (
    SourceJunctionTablePlan,
    SourceStateTablePlan,
    build_source_plan,
)
from fabulexa_forge.reader.emit import open_emit

from ._source_fixtures import build_source_keys_emit, build_source_test_emit

if TYPE_CHECKING:
    from fabulexa_forge.exporters.source.plan import SourcePlan

# ---------------------------------------------------------------------------
# Plan-build helper
# ---------------------------------------------------------------------------


def _build_plan(
    emit_dir: Path,
    tables: "tuple[SourceTableDecl, ...]" = (),
    *,
    events: "SourceEventsDecl | None" = None,
    keys: "dict[str, object] | None" = None,
    kind_labels: "dict[str, str] | None" = None,
) -> "SourcePlan":
    """Build a full-export SourcePlan, resolving the anchor and election the
    way `export_source` does."""
    config = ExportConfig(
        mode="source",
        source=SourceConfig(tables=tables, events=events, kind_labels=kind_labels),
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None, "the fixture declares a runtime block"
        election = resolve_election(emit.sidecar, keys)
        return build_source_plan(
            emit, config, anchor, election, False, discard_notice_sink
        )


# ---------------------------------------------------------------------------
# State table
# ---------------------------------------------------------------------------


def test_state_table_carried_column_keyed_post_rename(tmp_path: Path) -> None:
    """A `rename`d state-table column's entry keys on the output name."""
    emit_dir = build_source_test_emit(tmp_path)
    tables = (
        SourceTableDecl(
            name="visit", kind="visit", rename={"prop__status": "current_status"}
        ),
    )
    plan = _build_plan(emit_dir, tables)

    assert plan.tables[0].provenance["current_status"] == ColumnProvenance(
        source_table="records__visit", source_column="prop__status"
    )


def test_state_table_temporal_rendered_column_keeps_provenance_entry(
    tmp_path: Path,
) -> None:
    """A `render`-elected structural instant still stamps its source column,
    under its default lifecycle output name (`created_sim_time -> created_at`)."""
    emit_dir = build_source_test_emit(tmp_path)
    tables = (
        SourceTableDecl(
            name="visit", kind="visit", render={"created_sim_time": "date"}
        ),
    )
    plan = _build_plan(emit_dir, tables)

    assert plan.tables[0].provenance["created_at"] == ColumnProvenance(
        source_table="records__visit", source_column="created_sim_time"
    )


def test_state_table_elected_identity_column_projected_as_stored(
    tmp_path: Path,
) -> None:
    """Under a `presentation_id` election, the identity slot's entry names
    the stored `presentation_id` source column, not `record_id`."""
    emit_dir = build_source_keys_emit(tmp_path)
    tables = (SourceTableDecl(name="visit", kind="visit"),)
    plan = _build_plan(emit_dir, tables, keys={"visit": "presentation_id"})

    assert plan.tables[0].provenance["id"] == ColumnProvenance(
        source_table="records__visit", source_column="presentation_id"
    )


def test_state_table_provenance_covers_exactly_its_projected_columns(
    tmp_path: Path,
) -> None:
    """Every state-table column is a faithful carry (no `derived`/computed
    mode exists on `state`): the provenance map's key set equals the unit's
    final projected output columns exactly, neither more nor fewer."""
    emit_dir = build_source_test_emit(tmp_path)
    tables = (SourceTableDecl(name="location", kind="location"),)
    plan = _build_plan(emit_dir, tables)
    unit = plan.tables[0]
    assert isinstance(unit, SourceStateTablePlan)

    assert set(unit.provenance) == {out for _src, out in unit.columns}


# ---------------------------------------------------------------------------
# author_descriptions: source-identity keys translated through `rename`
# ---------------------------------------------------------------------------


def test_state_table_description_key_lands_under_renamed_output_name(
    tmp_path: Path,
) -> None:
    """A `descriptions` key addressed by source identity lands under its
    post-`rename` output name."""
    emit_dir = build_source_test_emit(tmp_path)
    tables = (
        SourceTableDecl(
            name="visit",
            kind="visit",
            rename={"prop__status": "current_status"},
            descriptions={"prop__status": "The visit's current status."},
        ),
    )
    plan = _build_plan(emit_dir, tables)

    assert plan.tables[0].author_descriptions == {
        "current_status": "The visit's current status."
    }


def test_state_table_description_key_on_unrenamed_column_lands_under_own_name(
    tmp_path: Path,
) -> None:
    """A `descriptions` key on an un-renamed column lands under its own
    (already output-equal) name."""
    emit_dir = build_source_test_emit(tmp_path)
    tables = (
        SourceTableDecl(
            name="visit",
            kind="visit",
            descriptions={"prop__priority": "How urgent the visit is."},
        ),
    )
    plan = _build_plan(emit_dir, tables)

    assert plan.tables[0].author_descriptions == {
        "priority": "How urgent the visit is."
    }


# ---------------------------------------------------------------------------
# Junction table
# ---------------------------------------------------------------------------


def test_junction_table_carried_columns_keyed_against_membership_table(
    tmp_path: Path,
) -> None:
    """Every junction column's entry names the `membership__<K>__<p>`
    source table and its own pre-rename source column."""
    emit_dir = build_source_test_emit(tmp_path)
    tables = (
        SourceTableDecl(
            name="visit_team", membership=MembershipRef(kind="visit", property="team")
        ),
    )
    plan = _build_plan(emit_dir, tables)
    unit = plan.tables[0]
    assert isinstance(unit, SourceJunctionTablePlan)

    source_table = "membership__visit__team"
    assert unit.provenance["visit_id"] == ColumnProvenance(
        source_table=source_table, source_column="record_id"
    )
    assert unit.provenance["role_name"] == ColumnProvenance(
        source_table=source_table, source_column="elem__role_name"
    )
    assert unit.provenance["actor_kind"] == ColumnProvenance(
        source_table=source_table, source_column="member__actor__kind"
    )
    assert unit.provenance["actor_id"] == ColumnProvenance(
        source_table=source_table, source_column="member__actor__id"
    )


def test_junction_table_description_key_lands_under_renamed_output_name(
    tmp_path: Path,
) -> None:
    """A junction table's `descriptions` key translates through `rename`
    the same way a state table's does."""
    emit_dir = build_source_test_emit(tmp_path)
    tables = (
        SourceTableDecl(
            name="visit_team",
            membership=MembershipRef(kind="visit", property="team"),
            rename={"elem__role_name": "role"},
            descriptions={"elem__role_name": "The member's role on the team."},
        ),
    )
    plan = _build_plan(emit_dir, tables)
    unit = plan.tables[0]
    assert isinstance(unit, SourceJunctionTablePlan)

    assert unit.author_descriptions == {"role": "The member's role on the team."}


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


def test_event_log_kind_values_ordered_labels_and_source_kind(tmp_path: Path) -> None:
    """`item_type` kind_values entries follow event-source compile order,
    the label post-`kind_labels` (identity fall-through for an unlabeled
    kind), and `source_kind` the raw kind."""
    emit_dir = build_source_test_emit(tmp_path)
    events = SourceEventsDecl(
        name="audit",
        sources=(
            SourceEventSourceDecl(kind="visit"),
            SourceEventSourceDecl(kind="shift"),
        ),
    )
    plan = _build_plan(emit_dir, (), events=events, kind_labels={"visit": "VisitLog"})
    assert plan.events is not None

    assert plan.events.kind_values["item_type"] == (
        KindValueEntry(label="VisitLog", source_kind="visit"),
        KindValueEntry(label="shift", source_kind="shift"),
    )


def test_event_log_provenance_always_empty(tmp_path: Path) -> None:
    """Every log column (`id`, `item_type`, `item_id`, `event`, `occurred_at`
    / event-time, `changes`) is computed -- the log's own `provenance` stays
    empty regardless of its sources."""
    emit_dir = build_source_test_emit(tmp_path)
    events = SourceEventsDecl(
        name="audit", sources=(SourceEventSourceDecl(kind="visit"),)
    )
    plan = _build_plan(emit_dir, (), events=events)
    assert plan.events is not None

    assert plan.events.provenance == {}
    for column in ("id", "item_type", "item_id", "event", "occurred_at", "changes"):
        assert column not in plan.events.provenance


def test_event_log_spec_stamps_empty_author_descriptions(tmp_path: Path) -> None:
    """The event log declares no `descriptions` surface -- its compiled spec
    stamps an empty `author_descriptions`."""
    emit_dir = build_source_test_emit(tmp_path)
    events = SourceEventsDecl(
        name="audit", sources=(SourceEventSourceDecl(kind="visit"),)
    )
    plan = _build_plan(emit_dir, (), events=events)
    specs = build_source_query_specs(plan, None)
    log_spec = next(s for s in specs if s.table_name == "audit")

    assert log_spec.author_descriptions == {}


# ---------------------------------------------------------------------------
# Determinism + builder-only construction
# ---------------------------------------------------------------------------


def test_provenance_deterministic_across_plan_builds(tmp_path: Path) -> None:
    """Two builds of the same plan against the same emit yield equal
    provenance maps, state/junction/event-log units alike."""
    emit_dir = build_source_test_emit(tmp_path)
    tables = (
        SourceTableDecl(name="visit", kind="visit"),
        SourceTableDecl(
            name="visit_team", membership=MembershipRef(kind="visit", property="team")
        ),
    )
    events = SourceEventsDecl(
        name="audit", sources=(SourceEventSourceDecl(kind="visit"),)
    )

    first = _build_plan(emit_dir, tables, events=events)
    second = _build_plan(emit_dir, tables, events=events)

    assert [t.provenance for t in first.tables] == [t.provenance for t in second.tables]
    assert first.events is not None
    assert second.events is not None
    assert first.events.kind_values == second.events.kind_values


def test_state_table_plan_provenance_defaults_to_empty_when_hand_constructed() -> None:
    """A `SourceStateTablePlan` built directly (bypassing
    `_build_state_table_plan`) defaults `provenance` to empty -- the
    absence-detection default the builder always overrides in practice."""
    unit = SourceStateTablePlan(
        name="t",
        kind="k",
        populations=(),
        columns=(),
        identity_surface="record_id",
        edge_surfaces=(),
        keys=None,
    )
    assert unit.provenance == {}
