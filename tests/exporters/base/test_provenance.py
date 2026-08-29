"""Tests for base-mode provenance stamping (`exporters/base/plan.py`).

Covers `ColumnProvenance` on `BaseTableSpec`, per the documentation-channel
sprint spec § Phase 4: every projected payload and structural (state-at)
column stamps, rename included; the re-derived edge keys (`<kind>_key` /
`<p>_key`) get no entry at any horizon, since `build_base_plan` is itself
horizon-agnostic (the horizon is a render-time concern); and a `BaseTableSpec`
built directly, bypassing its builder, defaults `provenance` to empty.
"""

from __future__ import annotations

from pathlib import Path

from _support.notices import discard_notice_sink

from fabulexa_forge.config.models import BaseConfig, RenameEntry
from fabulexa_forge.exporters.base.plan import BaseTableSpec, build_base_plan
from fabulexa_forge.exporters.query_spec import ColumnProvenance
from fabulexa_forge.reader.emit import open_emit

from ._base_fixtures import DAY_NS, build_reference_edge_emit


def _actor_spec(tmp_path: Path, config: "BaseConfig | None" = None) -> BaseTableSpec:
    """Build the reference-edge fixture's plan and return the `actor` spec."""
    emit_dir = build_reference_edge_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        plan = build_base_plan(emit.sidecar, config, notice_sink=discard_notice_sink)
    return next(t for t in plan.tables if t.kind == "actor")


# ---------------------------------------------------------------------------
# Carried columns: payload, structural, rename
# ---------------------------------------------------------------------------


def test_projected_structural_column_carries_source(tmp_path: Path) -> None:
    """The self identity's state-at entry names its `record_id` source
    column under the default `id` output name."""
    spec = _actor_spec(tmp_path)

    assert spec.provenance["id"] == ColumnProvenance(
        source_table="records__actor", source_column="record_id"
    )
    assert spec.provenance["created_sim_time"] == ColumnProvenance(
        source_table="records__actor", source_column="created_sim_time"
    )


def test_projected_payload_column_carries_source(tmp_path: Path) -> None:
    """A reference-valued `prop__<p>` payload column stamps under its
    unrenamed default output name (base does not strip `prop__` by default)."""
    spec = _actor_spec(tmp_path)

    assert spec.provenance["prop__lead_id"] == ColumnProvenance(
        source_table="records__actor", source_column="prop__lead_id"
    )
    assert spec.provenance["prop__backup_id"] == ColumnProvenance(
        source_table="records__actor", source_column="prop__backup_id"
    )


def test_renamed_column_keyed_by_output_name(tmp_path: Path) -> None:
    """A `rename.columns` entry's provenance keys on the new output name,
    still naming the same source column (rename never re-sources a value)."""
    config = BaseConfig(
        rename=[
            RenameEntry(table="records__actor", columns={"prop__lead_id": "leader"})
        ]
    )
    spec = _actor_spec(tmp_path, config)

    assert spec.provenance["leader"] == ColumnProvenance(
        source_table="records__actor", source_column="prop__lead_id"
    )
    assert "prop__lead_id" not in spec.provenance


# ---------------------------------------------------------------------------
# author_descriptions: translated through the matched entry's `columns`
# ---------------------------------------------------------------------------


def test_descriptions_only_rename_entry_compiles_and_stamps(tmp_path: Path) -> None:
    """A descriptions-only rename entry (no `name`, no `columns`) compiles,
    stamping its descriptions keyed on the unrenamed default output name."""
    config = BaseConfig(
        rename=[
            RenameEntry(
                table="records__actor",
                descriptions={"prop__lead_id": "The lead's identity."},
            )
        ]
    )
    spec = _actor_spec(tmp_path, config)

    assert spec.author_descriptions == {"prop__lead_id": "The lead's identity."}


def test_descriptions_key_translates_through_columns_rename(tmp_path: Path) -> None:
    """A `descriptions` key translates through the entry's `columns` renames
    to the resulting output name."""
    config = BaseConfig(
        rename=[
            RenameEntry(
                table="records__actor",
                columns={"prop__lead_id": "leader"},
                descriptions={"prop__lead_id": "The lead's identity."},
            )
        ]
    )
    spec = _actor_spec(tmp_path, config)

    assert spec.author_descriptions == {"leader": "The lead's identity."}
    assert "prop__lead_id" not in spec.author_descriptions


def test_descriptions_absent_when_no_rename_entry_matches(tmp_path: Path) -> None:
    """No matching rename entry -- `author_descriptions` stays empty."""
    spec = _actor_spec(tmp_path)

    assert spec.author_descriptions == {}


# ---------------------------------------------------------------------------
# Re-derived edge keys: no entry, any horizon
# ---------------------------------------------------------------------------


def test_edge_key_columns_absent_from_provenance(tmp_path: Path) -> None:
    """The self `<kind>_key` and per-edge `<p>_key` columns are computed
    (`record_index` / `ref_index__<p>`, re-derived at render), never carried
    -- no provenance entry."""
    spec = _actor_spec(tmp_path)

    assert "actor_key" not in spec.provenance
    assert "lead_id_key" not in spec.provenance
    assert "backup_id_key" not in spec.provenance


def test_edge_key_columns_absent_under_a_slice_at_horizon(tmp_path: Path) -> None:
    """`build_base_plan` is horizon-agnostic (the horizon is supplied at
    render, never at plan build): a `slice_at`-bearing config yields the
    identical provenance map, key columns still absent."""
    unsliced_dir = tmp_path / "unsliced"
    unsliced_dir.mkdir()
    sliced_dir = tmp_path / "sliced"
    sliced_dir.mkdir()
    unsliced = _actor_spec(unsliced_dir)
    sliced = _actor_spec(sliced_dir, BaseConfig(slice_at=2 * DAY_NS))

    assert sliced.provenance == unsliced.provenance
    assert "actor_key" not in sliced.provenance
    assert "lead_id_key" not in sliced.provenance
    assert "backup_id_key" not in sliced.provenance


# ---------------------------------------------------------------------------
# Determinism + builder-only construction
# ---------------------------------------------------------------------------


def test_provenance_deterministic_across_plan_builds(tmp_path: Path) -> None:
    """Two plan builds against the same emit yield an equal provenance map."""
    emit_dir = build_reference_edge_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        first = build_base_plan(emit.sidecar, None, notice_sink=discard_notice_sink)
        second = build_base_plan(emit.sidecar, None, notice_sink=discard_notice_sink)

    assert [t.provenance for t in first.tables] == [t.provenance for t in second.tables]


def test_base_table_spec_provenance_defaults_to_empty_when_hand_constructed() -> None:
    """A `BaseTableSpec` built directly (bypassing `_resolve_specs`) defaults
    `provenance` to empty -- the absence-detection default the builder
    always overrides in practice."""
    spec = BaseTableSpec(
        kind="k",
        table_name="k",
        properties=frozenset(),
        has_presentation_id=False,
        identity_surface="record_id",
        reference_keys=(),
        column_renames={},
    )
    assert spec.provenance == {}
