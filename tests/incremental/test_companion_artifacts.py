"""Tests for windowed companion-artifact threading (incremental/driver.py
Phase 4): placement, whole-state rewrite, drained untouched, range's null
`next_window_index`, and an SCD-2 dim's view-named manifest entry.

Uses tmp_path for all IO. Builds minimal emits inline (base-mode fixtures
reuse `exporters.base._base_fixtures`, as `tests/incremental/test_driver.py`
already does for its base-mode dispatch tests).
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, write_emit

from exporters.base._base_fixtures import DAY_NS, build_base_test_emit
from fabulexa_forge.config.models import (
    ColumnDecl,
    DerivedSpec,
    DimensionalConfig,
    ExportConfig,
    IncrementalConfig,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.exporters.companion.overlay import ReadmeOverlay
from fabulexa_forge.incremental.driver import export_incremental_next, export_window
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit

# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------


def _base_config(sim_period_ns: int) -> ExportConfig:
    """A mode='base' config with a sim-time-regime incremental block."""
    return ExportConfig.model_validate(
        {"mode": "base", "incremental": {"sim_period_ns": sim_period_ns}}
    )


def _type1_dim_config(sim_period_ns: int) -> ExportConfig:
    """A one-table type-1 dim dimensional config with a sim-time incremental
    block, over `_build_gap_window_emit`'s `entity` kind."""
    return ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "incremental": {"sim_period_ns": sim_period_ns},
            "dimensional": {
                "tables": [
                    {
                        "name": "dim_entity",
                        "role": "dim",
                        "scd": "type1",
                        "source": {"grain": "records", "kind": "entity"},
                        "key": ["id"],
                        "columns": [
                            {"name": "id", "from": "record_id"},
                            {"name": "name", "from": "prop__name"},
                        ],
                    }
                ]
            },
        }
    )


def _scd2_config(sim_period_ns: int) -> ExportConfig:
    """A one-table SCD-2 (with valid_to) dimensional config over
    `_build_scd2_emit`'s `actor` kind."""
    return ExportConfig(
        mode="dimensional",
        incremental=IncrementalConfig(sim_period_ns=sim_period_ns),
        dimensional=DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_actor",
                    role="dim",
                    scd="type2",
                    source=SourceDecl(grain="records", kind="actor"),
                    key=["id", "valid_from"],
                    columns=[
                        ColumnDecl(name="id", **{"from": "record_id"}),
                        ColumnDecl(name="status", **{"from": "prop__status"}),
                        ColumnDecl(
                            name="valid_from",
                            derived=DerivedSpec(scd_window="valid_from"),
                        ),
                        ColumnDecl(
                            name="valid_to",
                            derived=DerivedSpec(scd_window="valid_to"),
                        ),
                    ],
                )
            ]
        ),
    )


# ---------------------------------------------------------------------------
# Emit builders
# ---------------------------------------------------------------------------

_ENTITY_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_ACTOR_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN", "history_tracked": False},
    {"name": "deactivated_at", "type": "BIGINT", "history_tracked": False},
    {"name": "last_mutation_sim_time", "type": "BIGINT", "history_tracked": False},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _build_gap_window_emit(tmp_path: Path) -> Path:
    """Build a type-1-dim emit whose window 1 (`[100, 200)`) carries no
    mutations: e001 mutates at 10, e002 at 210."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    col_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _ENTITY_COLUMNS)
    conn.execute(f'CREATE TABLE "records__entity" ({col_ddl})')
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "e001", 10, True, 10, 0, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "e002", 210, True, 210, 1, "Carol"],
    )
    conn.close()

    write_emit(
        emit_dir,
        tables=[
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "columns": _ENTITY_COLUMNS,
                "rows": 2,
            }
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 300}],
    )
    return emit_dir


def _build_scd2_emit(tmp_path: Path) -> Path:
    """Build an SCD-2 actor emit: 3 status changes (sim_time 10/20/30)."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    col_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _ACTOR_COLUMNS)
    conn.execute(f'CREATE TABLE "records__actor" ({col_ddl})')
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a001", 0, True, None, 30, 0, "discharged"],
    )
    hist_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLUMNS)
    conn.execute(f'CREATE TABLE "history" ({hist_ddl})')
    for sim_time, state in [
        (10, "admitted"),
        (20, "under_treatment"),
        (30, "discharged"),
    ]:
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "actor", "a001", "status", sim_time, state],
        )
    conn.close()

    write_emit(
        emit_dir,
        tables=[
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": _ACTOR_COLUMNS,
                "rows": 1,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 3,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return emit_dir


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, object]:
    """Parse a JSON artifact file."""
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_row_counts_are_none(manifest: dict[str, object]) -> None:
    """Every `tables` entry in a windowed manifest carries `row_count: null`."""
    entries = manifest["tables"]
    assert isinstance(entries, list)
    assert entries
    for entry in entries:
        assert entry["row_count"] is None


# ---------------------------------------------------------------------------
# --next window 0: artifacts at the output root, never in the drop dir
# ---------------------------------------------------------------------------


def test_next_csv_window0_artifacts_at_root_not_in_drop_dir(tmp_path: Path) -> None:
    emit_dir = build_base_test_emit(tmp_path)
    config = _base_config(sim_period_ns=2 * DAY_NS)
    out = tmp_path / "drops"

    with open_emit(emit_dir) as emit:
        outcome = export_incremental_next(
            emit, config, out, "csv", None, discard_notice_sink, overlay=None
        )

    assert outcome.status == "emitted"
    assert outcome.window is not None
    label = outcome.window.label

    assert (out / "base-readme.md").exists()
    assert (out / "base-manifest.json").exists()
    assert not (out / label / "base-readme.md").exists()
    assert not (out / label / "base-manifest.json").exists()

    manifest = _read_json(out / "base-manifest.json")
    assert manifest["incremental"] == {
        "regime": "sim_time",
        "label": label,
        "next_window_index": 1,
    }
    _assert_row_counts_are_none(manifest)


# ---------------------------------------------------------------------------
# Second --next: whole-state rewrite, next_window_index advances
# ---------------------------------------------------------------------------


def test_second_next_rewrites_artifacts_whole_state(tmp_path: Path) -> None:
    """The manifest's `next_window_index` advances, and an overlay note added
    only on the second call proves the README rewrite is whole-state, not
    incremental (the README carries no window-to-window facts on its own)."""
    emit_dir = build_base_test_emit(tmp_path)
    config = _base_config(sim_period_ns=2 * DAY_NS)
    out = tmp_path / "drops"
    overlay = ReadmeOverlay(overview=None, table_notes={"patient": "Added mid-drip."})

    with open_emit(emit_dir) as emit:
        export_incremental_next(
            emit, config, out, "csv", None, discard_notice_sink, overlay=None
        )
        manifest0_bytes = (out / "base-manifest.json").read_bytes()
        readme0_bytes = (out / "base-readme.md").read_bytes()

        outcome1 = export_incremental_next(
            emit, config, out, "csv", None, discard_notice_sink, overlay=overlay
        )

    assert outcome1.status == "emitted"
    manifest1 = _read_json(out / "base-manifest.json")
    assert manifest1["incremental"]["next_window_index"] == 2
    assert (out / "base-manifest.json").read_bytes() != manifest0_bytes

    readme1_bytes = (out / "base-readme.md").read_bytes()
    assert readme1_bytes != readme0_bytes
    assert "Added mid-drip." in readme1_bytes.decode("utf-8")


# ---------------------------------------------------------------------------
# Empty window: artifacts rewritten like any emitting window
# ---------------------------------------------------------------------------


def test_empty_window_artifacts_still_rewritten(tmp_path: Path) -> None:
    """Window 1 ([100, 200)) carries no mutations, yet is emitted and its
    artifacts are rewritten -- the incremental block alone changes."""
    emit_dir = _build_gap_window_emit(tmp_path)
    config = _type1_dim_config(sim_period_ns=100)
    db_path = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        o0 = export_incremental_next(
            emit, config, db_path, "duckdb", None, discard_notice_sink, overlay=None
        )
        manifest0_bytes = (tmp_path / "wh-dimensional-manifest.json").read_bytes()

        o1 = export_incremental_next(
            emit, config, db_path, "duckdb", None, discard_notice_sink, overlay=None
        )

    assert o0.status == "emitted"
    assert o1.status == "emitted"
    assert o1.window is not None and o1.window.index == 1

    manifest1 = _read_json(tmp_path / "wh-dimensional-manifest.json")
    assert manifest1["incremental"]["next_window_index"] == 2
    assert (tmp_path / "wh-dimensional-manifest.json").read_bytes() != manifest0_bytes


# ---------------------------------------------------------------------------
# Drained: exit path untouched
# ---------------------------------------------------------------------------


def test_drained_leaves_both_artifacts_untouched(tmp_path: Path) -> None:
    emit_dir = build_base_test_emit(tmp_path)
    config = _base_config(sim_period_ns=2 * DAY_NS)
    db_path = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        while True:
            outcome = export_incremental_next(
                emit, config, db_path, "duckdb", None, discard_notice_sink, overlay=None
            )
            if outcome.status == "drained":
                break

        manifest_before = (tmp_path / "wh-base-manifest.json").read_bytes()
        readme_before = (tmp_path / "wh-base-readme.md").read_bytes()

        drained_again = export_incremental_next(
            emit, config, db_path, "duckdb", None, discard_notice_sink, overlay=None
        )

    assert drained_again.status == "drained"
    assert drained_again.window is None
    assert drained_again.report is None
    assert (tmp_path / "wh-base-manifest.json").read_bytes() == manifest_before
    assert (tmp_path / "wh-base-readme.md").read_bytes() == readme_before


# ---------------------------------------------------------------------------
# --from/--to range: artifacts written with next_window_index: null
# ---------------------------------------------------------------------------


def test_range_writes_null_next_window_index(tmp_path: Path) -> None:
    emit_dir = build_base_test_emit(tmp_path)
    config = ExportConfig(mode="base")
    out = tmp_path / "range_out"
    window = Window(index=None, start_ns=0, end_ns=DAY_NS, label="r_ns0_dayNS")

    with open_emit(emit_dir) as emit:
        windowed_export = export_window(
            emit,
            config,
            out,
            "csv",
            None,
            window,
            None,
            discard_notice_sink,
            overlay=None,
        )

    assert "patient" in {t.name for t in windowed_export.report.tables}
    manifest = _read_json(out / "base-manifest.json")
    assert manifest["incremental"] == {
        "regime": "sim_time",
        "label": "r_ns0_dayNS",
        "next_window_index": None,
    }
    _assert_row_counts_are_none(manifest)


# ---------------------------------------------------------------------------
# Duckdb windowed: <db-stem>-<mode>-* siblings, rewritten per emitting window
# ---------------------------------------------------------------------------


def test_duckdb_windowed_writes_db_stem_siblings_and_rewrites(tmp_path: Path) -> None:
    emit_dir = build_base_test_emit(tmp_path)
    config = _base_config(sim_period_ns=2 * DAY_NS)
    db_path = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        export_incremental_next(
            emit, config, db_path, "duckdb", None, discard_notice_sink, overlay=None
        )
        manifest0_bytes = (tmp_path / "wh-base-manifest.json").read_bytes()

        outcome1 = export_incremental_next(
            emit, config, db_path, "duckdb", None, discard_notice_sink, overlay=None
        )

    assert (tmp_path / "wh-base-readme.md").exists()
    assert (tmp_path / "wh-base-manifest.json").exists()
    assert outcome1.status == "emitted"
    assert (tmp_path / "wh-base-manifest.json").read_bytes() != manifest0_bytes


# ---------------------------------------------------------------------------
# SCD-2 dim under incremental: one manifest entry under the view name
# ---------------------------------------------------------------------------


def test_scd2_windowed_manifest_entry_uses_view_name_and_physical_columns(
    tmp_path: Path,
) -> None:
    emit_dir = _build_scd2_emit(tmp_path)
    config = _scd2_config(sim_period_ns=15)
    db_path = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        outcome = export_incremental_next(
            emit, config, db_path, "duckdb", None, discard_notice_sink, overlay=None
        )

    assert outcome.status == "emitted"
    assert outcome.report is not None
    assert {t.name for t in outcome.report.tables} == {"dim_actor"}

    manifest = _read_json(tmp_path / "wh-dimensional-manifest.json")
    entries = manifest["tables"]
    assert isinstance(entries, list)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["name"] == "dim_actor"
    column_names = {c["name"] for c in entry["columns"]}
    assert "__valid_from_ns" in column_names
    assert "valid_to" not in column_names
    assert entry["row_count"] is None
