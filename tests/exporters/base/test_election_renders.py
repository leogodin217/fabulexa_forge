"""Tests for base-mode key election at render time: the self id-space value
surface, per-edge target rendering (uniform presentation_id, uniform
record_index, an excluded mixed election's per-row VARCHAR), the elected
edge value condition table, and the engine's render-time uniqueness guard
(exporters/base/renders.py, exporters/base/engine.py).

Renders are built directly via build_base_plan + build_base_render_sql
(bypassing the engine) when only the render SQL matters; the guard section
goes through build_base_query_specs / export_base, since the guard is an
engine-level concern.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _support.notices import discard_notice_sink

from fabulexa_forge.config.models import BaseConfig, ExcludeDecl, ExportConfig
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.errors import ElectedKeyDuplicate
from fabulexa_forge.exporters.base.engine import build_base_query_specs, export_base
from fabulexa_forge.exporters.base.plan import build_base_plan
from fabulexa_forge.exporters.base.renders import build_base_render_sql
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import Emit, open_emit
from fabulexa_forge.reader.errors import RunDatabaseError

from ._base_fixtures import (
    DAY_NS,
    build_base_test_emit,
    build_corrupted_edge_target_emit,
    build_corrupted_presentation_id_patient_emit,
    build_mixed_edge_election_emit,
    build_reference_edge_emit,
)

#: The mid-tape horizon used throughout the condition-table tests: strictly
#: after t002's deactivation (1*DAY), strictly before t003's creation (3*DAY).
_MID_TAPE_HORIZON = 2 * DAY_NS + 1

_PATIENT_PRESENTATION_KEYS: dict[str, object] = {
    "patient": {
        "key": {
            "unique_within": "emit",
            "branch_stable": False,
            "slice_stable": False,
            "key_space": {"class": "counter", "prefix": "", "width": 4},
        }
    }
}

_TARGET_PRESENTATION_KEYS: dict[str, object] = {
    "target": {
        "key": {
            "unique_within": "emit",
            "branch_stable": False,
            "slice_stable": False,
            "key_space": {"class": "counter", "prefix": "T_", "width": 3},
        }
    }
}


def _select_columns(
    emit: Emit, sql: str, id_col: str, value_cols: tuple[str, ...]
) -> dict[object, tuple[object, ...]]:
    """Execute `sql`, projecting `id_col` + `value_cols` by name, indexed by
    `id_col`'s value.

    Args:
        emit: The open emit to query.
        sql: The render SQL to wrap.
        id_col: The output column name to index rows by.
        value_cols: Further output column names to project.

    Returns:
        {id_col value -> (value_cols..., )}, one entry per result row.
    """
    cols = ", ".join(f'"{c}"' for c in (id_col, *value_cols))
    wrapped = f'SELECT {cols} FROM ({sql}) AS "_t"'
    rows = emit.query(wrapped, ())
    return {row[0]: row[1:] for row in rows}


# ---------------------------------------------------------------------------
# Self columns: presentation_id absorption, record_index drop
# ---------------------------------------------------------------------------


def test_presentation_id_self_column_renders_elected_value_absorbs_standalone(
    tmp_path: Path,
) -> None:
    """presentation_id election renders the elected value in the id slot,
    keyed by patient_key; no standalone 'presentation_id' column remains."""
    emit_dir = build_base_test_emit(
        tmp_path, presentation_keys=_PATIENT_PRESENTATION_KEYS
    )
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        election = resolve_election(emit.sidecar, {"patient": "presentation_id"})
        plan = build_base_plan(
            emit.sidecar, None, discard_notice_sink, election=election
        )
        spec = plan.tables[0]
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, None, None)
        id_col = spec.column_renames["presentation_id"]
        key_col = spec.column_renames["record_index"]
        by_key = _select_columns(emit, sql, key_col, (id_col,))
        with pytest.raises(RunDatabaseError):
            emit.query(f'SELECT "presentation_id" FROM ({sql}) AS "_t"', ())
    assert id_col == "id"
    assert by_key[0] == (1001,)  # p001's self key=0, elected id=1001


def test_record_index_self_column_drops_id_slot_keeps_kind_key_only(
    tmp_path: Path,
) -> None:
    """record_index election drops the self id-space slot entirely — only
    patient_key ships."""
    emit_dir = build_base_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        election = resolve_election(emit.sidecar, {"patient": "record_index"})
        plan = build_base_plan(
            emit.sidecar, None, discard_notice_sink, election=election
        )
        spec = plan.tables[0]
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, None, None)
        assert "record_id" not in spec.column_renames
        with pytest.raises(RunDatabaseError):
            emit.query(f'SELECT "id" FROM ({sql}) AS "_t"', ())
        key_col = spec.column_renames["record_index"]
        rows = emit.query(f'SELECT "{key_col}" FROM ({sql}) AS "_t"', ())
    assert sorted(row[0] for row in rows) == [0, 1, 2]


# ---------------------------------------------------------------------------
# Edge columns: uniform presentation_id / uniform record_index / mixed
# ---------------------------------------------------------------------------


def test_uniform_presentation_id_edge_renders_target_codes_key_unaffected(
    tmp_path: Path,
) -> None:
    """A uniform presentation_id target renders the target's codes in
    prop__lead_id; lead_id_key still resolves the target's record_index,
    unaffected by the election."""
    emit_dir = build_reference_edge_emit(
        tmp_path,
        target_presentation_id=True,
        presentation_keys=_TARGET_PRESENTATION_KEYS,
    )
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        election = resolve_election(emit.sidecar, {"target": "presentation_id"})
        plan = build_base_plan(
            emit.sidecar, None, discard_notice_sink, election=election
        )
        spec = next(t for t in plan.tables if t.kind == "actor")
        rk = next(r for r in spec.reference_keys if r.property_name == "lead_id")
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, None, None)
        id_col = spec.column_renames["record_id"]
        value_col = spec.column_renames.get("prop__lead_id", "prop__lead_id")
        key_col = spec.column_renames["ref_index__lead_id"]
        by_id = _select_columns(emit, sql, id_col, (value_col, key_col))
    assert rk.value_column_shipped is True
    assert by_id["a001"] == ("T001", 0)


def test_uniform_record_index_edge_drops_value_column(tmp_path: Path) -> None:
    """An all-record_index target election drops prop__lead_id — only
    lead_id_key ships."""
    emit_dir = build_reference_edge_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        election = resolve_election(emit.sidecar, {"target": "record_index"})
        plan = build_base_plan(
            emit.sidecar, None, discard_notice_sink, election=election
        )
        spec = next(t for t in plan.tables if t.kind == "actor")
        rk = next(r for r in spec.reference_keys if r.property_name == "lead_id")
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, None, None)
        assert rk.value_column_shipped is False
        with pytest.raises(RunDatabaseError):
            emit.query(f'SELECT "prop__lead_id" FROM ({sql}) AS "_t"', ())
        key_col = spec.column_renames["ref_index__lead_id"]
        rows = emit.query(f'SELECT "{key_col}" FROM ({sql}) AS "_t"', ())
    assert 0 in {row[0] for row in rows}  # a001's lead_id_key still resolves


def test_excluded_mixed_election_edge_renders_per_row_varchar(tmp_path: Path) -> None:
    """An excluded target's mixed election renders one VARCHAR column per
    row: presentation_id's code for the alpha population, digit-rendered
    record_index for the beta population."""
    emit_dir = build_mixed_edge_election_emit(tmp_path)
    config = BaseConfig(exclude=ExcludeDecl(kinds=["target"]))
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        election = resolve_election(
            emit.sidecar,
            {"target": {"alpha": "presentation_id", "beta": "record_index"}},
        )
        plan = build_base_plan(
            emit.sidecar, config, discard_notice_sink, election=election
        )
        spec = next(t for t in plan.tables if t.kind == "widget")
        rk = spec.reference_keys[0]
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, None, None)
        id_col = spec.column_renames["record_id"]
        value_col = spec.column_renames.get("prop__target_id", "prop__target_id")
        by_id = _select_columns(emit, sql, id_col, (value_col,))
    assert rk.rendered_type == "VARCHAR"
    assert by_id["g1"] == ("ALPHA_001",)  # alpha population: presentation_id code
    assert by_id["g2"] == ("1",)  # beta population: digit-rendered record_index


# ---------------------------------------------------------------------------
# Elected edge value condition table
# ---------------------------------------------------------------------------


def test_elected_edge_value_condition_table(tmp_path: Path) -> None:
    """The elected edge value's four conditions, under a uniform
    presentation_id election: absent property -> NULL; dangled sentinel ->
    NULL; a pre-horizon (deactivated) target -> resolves; an at-or-after
    horizon target -> NULL."""
    emit_dir = build_reference_edge_emit(
        tmp_path,
        target_presentation_id=True,
        presentation_keys=_TARGET_PRESENTATION_KEYS,
    )
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        election = resolve_election(emit.sidecar, {"target": "presentation_id"})
        plan = build_base_plan(
            emit.sidecar, None, discard_notice_sink, election=election
        )
        spec = next(t for t in plan.tables if t.kind == "actor")
        sql = build_base_render_sql(
            emit.sidecar, fork_path, spec, None, _MID_TAPE_HORIZON
        )
        id_col = spec.column_renames["record_id"]
        lead_col = spec.column_renames.get("prop__lead_id", "prop__lead_id")
        backup_col = spec.column_renames.get("prop__backup_id", "prop__backup_id")
        by_id = _select_columns(emit, sql, id_col, (lead_col, backup_col))
    assert by_id["a003"][0] is None  # absent property
    assert by_id["a002"][0] is None  # dangled sentinel (t999 does not exist)
    assert by_id["a001"][1] == "T002"  # pre-horizon, deactivated target resolves
    assert by_id["a004"][0] is None  # target created at-or-after the horizon


# ---------------------------------------------------------------------------
# Engine: the render-time uniqueness guard
# ---------------------------------------------------------------------------


def test_self_identity_guard_catches_corrupted_presentation_id(tmp_path: Path) -> None:
    """A corrupted self-identity presentation_id (two records sharing one
    value) fails build_base_query_specs before any writer runs."""
    emit_dir = build_corrupted_presentation_id_patient_emit(tmp_path)
    config = ExportConfig(mode="base", keys={"patient": "presentation_id"})
    with open_emit(emit_dir) as emit:
        with pytest.raises(ElectedKeyDuplicate):
            build_base_query_specs(emit, config, None, None, discard_notice_sink)


def test_edge_guard_catches_corrupted_target_presentation_id(tmp_path: Path) -> None:
    """A corrupted edge-target presentation_id fails build_base_query_specs
    before any writer runs."""
    emit_dir = build_corrupted_edge_target_emit(tmp_path)
    config = ExportConfig(mode="base", keys={"target": "presentation_id"})
    with open_emit(emit_dir) as emit:
        with pytest.raises(ElectedKeyDuplicate):
            build_base_query_specs(emit, config, None, None, discard_notice_sink)


def test_mixed_edge_guard_catches_corrupted_alpha_population(tmp_path: Path) -> None:
    """A corrupted alpha population (a proper subset of target's domain)
    fails the edge guard, restricted to that population's spine."""
    emit_dir = build_mixed_edge_election_emit(tmp_path, corrupt_alpha=True)
    config = ExportConfig(
        mode="base",
        base=BaseConfig(exclude=ExcludeDecl(kinds=["target"])),
        keys={"target": {"alpha": "presentation_id", "beta": "record_index"}},
    )
    with open_emit(emit_dir) as emit:
        with pytest.raises(ElectedKeyDuplicate):
            build_base_query_specs(emit, config, None, None, discard_notice_sink)


def test_mixed_edge_guard_passes_on_conformant_data(tmp_path: Path) -> None:
    """The same election over conformant (uncorrupted) data compiles cleanly
    — the guard runs and passes, per admitted surface group."""
    emit_dir = build_mixed_edge_election_emit(tmp_path)
    config = ExportConfig(
        mode="base",
        base=BaseConfig(exclude=ExcludeDecl(kinds=["target"])),
        keys={"target": {"alpha": "presentation_id", "beta": "record_index"}},
    )
    with open_emit(emit_dir) as emit:
        specs = build_base_query_specs(emit, config, None, None, discard_notice_sink)
    assert any(spec.table_name == "widget" for spec in specs)


def test_corrupted_key_fails_before_any_writer_runs(tmp_path: Path) -> None:
    """export_base raises on a corrupted elected key and writes no output at
    all — the guard runs before build_base_query_specs returns."""
    emit_dir = build_corrupted_presentation_id_patient_emit(tmp_path)
    config = ExportConfig(mode="base", keys={"patient": "presentation_id"})
    out_path = tmp_path / "out.duckdb"
    with open_emit(emit_dir) as emit:
        with pytest.raises(ElectedKeyDuplicate):
            export_base(emit, config, out_path, "duckdb", None, discard_notice_sink)
    assert not out_path.exists()


def test_per_window_guard_fires_for_corrupted_key(tmp_path: Path) -> None:
    """An incremental (windowed) invocation still guards the elected key,
    labeling the failure with the window's display label."""
    emit_dir = build_corrupted_presentation_id_patient_emit(tmp_path)
    config = ExportConfig(mode="base", keys={"patient": "presentation_id"})
    window = Window(index=0, start_ns=0, end_ns=DAY_NS, label="w0")
    with open_emit(emit_dir) as emit:
        with pytest.raises(ElectedKeyDuplicate, match=r"\(w0\)"):
            build_base_query_specs(emit, config, None, window, discard_notice_sink)
