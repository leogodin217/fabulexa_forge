"""Tests for incremental/driver.py — export_incremental_next, export_window.

Uses tmp_path for all IO. Builds minimal emits inline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import duckdb
import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink
from _support.sidecar_builder import identity_column, write_emit

from exporters.base._base_fixtures import DAY_NS as _BASE_DAY_NS
from exporters.base._base_fixtures import build_base_test_emit
from exporters.source._source_fixtures import (
    build_day_scale_source_emit,
    build_source_test_emit,
    build_windowed_source_test_emit,
    windowed_test_windows,
)
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import ExportConfig
from fabulexa_forge.errors import (
    ExportRuntimeError,
    IncrementalConfigMissing,
    IncrementalFingerprintMismatch,
    IncrementalRangeTargetExists,
)
from fabulexa_forge.exporters.query_spec import NOTICE_KEYS_NOT_DECLARABLE_CSV
from fabulexa_forge.incremental.cursor import (
    _CURRENT_CURSOR_FORMAT_VERSION,
    Cursor,
    read_cursor,
    write_csv_cursor,
)
from fabulexa_forge.incremental.driver import (
    IncrementalOutcome,
    export_incremental_next,
    export_window,
)
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import Emit, open_emit
from fabulexa_forge.writers.relation import WrittenRelation

# ---------------------------------------------------------------------------
# Emit + config builders
# ---------------------------------------------------------------------------

_RECORDS_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT", "history_tracked": False},
    {"name": "active", "type": "BOOLEAN", "history_tracked": False},
    {"name": "deactivated_at", "type": "BIGINT", "history_tracked": False},
    {"name": "last_mutation_sim_time", "type": "BIGINT", "history_tracked": False},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
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

_PERIOD_NS = 100  # small period for sim-time regime tests


def _build_emit(tmp_path: Path, slice_at: int = 300) -> Path:
    """Build a minimal test emit with entities at sim_times 10, 110, 210.

    Args:
        tmp_path: Directory for the emit artifacts.
        slice_at: The branch's slice_at value.

    Returns:
        tmp_path (the emit directory).
    """
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    col_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _RECORDS_COLUMNS)
    conn.execute(f'CREATE TABLE "records__entity" ({col_ddl})')

    for record_index, (entity_id, name, mutation_time) in enumerate(
        [
            ("e001", "Alice", 10),
            ("e002", "Bob", 110),
            ("e003", "Carol", 210),
        ]
    ):
        conn.execute(
            'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
            [
                "trunk",
                entity_id,
                mutation_time,
                True,
                mutation_time,
                record_index,
                name,
            ],
        )

    hist_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLUMNS)
    conn.execute(f'CREATE TABLE "history" ({hist_ddl})')
    for sim_time, val in [(10, "alpha"), (110, "beta"), (210, "gamma")]:
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "entity", "e001", "state", sim_time, val],
        )
    conn.close()

    write_emit(
        emit_dir,
        tables=[
            {
                "name": "records__entity",
                "category": "records",
                "columns": _RECORDS_COLUMNS,
                "rows": 3,
                "record_kind": "entity",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 3,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": slice_at}],
    )
    return emit_dir


def _simple_config(with_incremental: bool = True) -> ExportConfig:
    """Build a minimal type-1 dim config.

    Args:
        with_incremental: If True, include a sim_period_ns incremental block.

    Returns:
        ExportConfig for a fact-only (records grain) table.
    """
    data: dict[str, object] = {
        "mode": "dimensional",
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
    if with_incremental:
        data["incremental"] = {"sim_period_ns": _PERIOD_NS}
    return ExportConfig.model_validate(data)


def _fact_config(with_incremental: bool = True) -> ExportConfig:
    """Build a config with a facts table (records grain, role=fact)."""
    data: dict[str, object] = {
        "mode": "dimensional",
        "dimensional": {
            "tables": [
                {
                    "name": "fact_history",
                    "role": "fact",
                    "source": {
                        "grain": "history_point",
                        "kind": "entity",
                        "property": "state",
                    },
                    "key": ["id"],
                    "columns": [
                        {"name": "id", "from": "record_id"},
                        {"name": "sim_time", "from": "sim_time"},
                        {"name": "value", "from": "value"},
                    ],
                }
            ]
        },
    }
    if with_incremental:
        data["incremental"] = {"sim_period_ns": _PERIOD_NS}
    return ExportConfig.model_validate(data)


# ---------------------------------------------------------------------------
# IncrementalConfigMissing
# ---------------------------------------------------------------------------


def test_incremental_config_missing_raises(tmp_path: Path) -> None:
    """export_incremental_next raises IncrementalConfigMissing when no incremental block."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config(with_incremental=False)
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        with pytest.raises(IncrementalConfigMissing):
            export_incremental_next(
                emit,
                config,
                out,
                "duckdb",
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )


# ---------------------------------------------------------------------------
# DuckDB drip: fresh → emitted → advance → drained
# ---------------------------------------------------------------------------


def test_duckdb_fresh_target_emits_window_0(tmp_path: Path) -> None:
    """Fresh DuckDB target → status 'emitted', window index 0."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config()
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        outcome = export_incremental_next(
            emit,
            config,
            out,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    assert outcome.status == "emitted"
    assert outcome.window is not None
    assert outcome.window.index == 0
    assert out.exists()


def test_duckdb_repeated_calls_advance_index(tmp_path: Path) -> None:
    """Repeated calls advance window index 0, 1, 2."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config()
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        o0 = export_incremental_next(
            emit,
            config,
            out,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )
        o1 = export_incremental_next(
            emit,
            config,
            out,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )
        o2 = export_incremental_next(
            emit,
            config,
            out,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    assert o0.window is not None and o0.window.index == 0
    assert o1.window is not None and o1.window.index == 1
    assert o2.window is not None and o2.window.index == 2


def test_duckdb_cursor_matches_after_each_window(tmp_path: Path) -> None:
    """After each window, the stored cursor's next_window_index matches."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config()
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        for expected_next in [1, 2, 3]:
            export_incremental_next(
                emit,
                config,
                out,
                "duckdb",
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )
            cursor = read_cursor(out, "duckdb", "w00000_ns0")
            assert cursor is not None
            assert cursor.next_window_index == expected_next


def test_duckdb_drained_when_start_ns_exceeds_slice_at(tmp_path: Path) -> None:
    """Window with start_ns > slice_at → status 'drained', nothing written."""
    # slice_at=50: windows are [0,100), [100,200), [200,300)
    # window 1 starts at 100 > 50, so should drain after window 0
    emit_dir = _build_emit(tmp_path, slice_at=50)
    config = _simple_config()
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        # Window 0: start_ns=0 <= 50 → emitted
        o0 = export_incremental_next(
            emit,
            config,
            out,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )
        assert o0.status == "emitted"

        # Window 1: start_ns=100 > 50 → drained
        o1 = export_incremental_next(
            emit,
            config,
            out,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    assert o1.status == "drained"
    assert o1.window is None
    assert o1.report is None


def test_duckdb_window_containing_slice_at_is_emitted(tmp_path: Path) -> None:
    """A window whose start_ns equals slice_at is still emitted (boundary is start_ns > slice_at)."""
    # slice_at=100: window 1 starts at 100 == 100, NOT > 100 → emitted
    emit_dir = _build_emit(tmp_path, slice_at=100)
    config = _simple_config()
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        export_incremental_next(
            emit,
            config,
            out,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )  # window 0
        o1 = export_incremental_next(
            emit,
            config,
            out,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )  # window 1

    assert o1.status == "emitted"
    assert o1.window is not None and o1.window.index == 1


def test_duckdb_drained_cursor_untouched(tmp_path: Path) -> None:
    """A drained call does not advance the cursor."""
    emit_dir = _build_emit(tmp_path, slice_at=50)
    config = _simple_config()
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        export_incremental_next(
            emit,
            config,
            out,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )  # window 0

        cursor_before = read_cursor(out, "duckdb", "w00000_ns0")
        assert cursor_before is not None
        assert cursor_before.next_window_index == 1

        export_incremental_next(
            emit,
            config,
            out,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )  # drained

        cursor_after = read_cursor(out, "duckdb", "w00000_ns0")
        assert cursor_after is not None
        assert cursor_after.next_window_index == 1  # unchanged


# ---------------------------------------------------------------------------
# CSV drip: fresh → emitted → advance → drained
# ---------------------------------------------------------------------------


def test_csv_fresh_target_emits_window_0(tmp_path: Path) -> None:
    """Fresh CSV target → status 'emitted', window index 0, drop dir created."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config()
    out = tmp_path / "drops"

    with open_emit(emit_dir) as emit:
        outcome = export_incremental_next(
            emit,
            config,
            out,
            "csv",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    assert outcome.status == "emitted"
    assert outcome.window is not None
    assert outcome.window.index == 0
    drop_dir = out / outcome.window.label
    assert drop_dir.exists()


def test_csv_repeated_calls_advance_index(tmp_path: Path) -> None:
    """Repeated CSV calls advance window index 0, 1, 2."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config()
    out = tmp_path / "drops"

    with open_emit(emit_dir) as emit:
        o0 = export_incremental_next(
            emit,
            config,
            out,
            "csv",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )
        o1 = export_incremental_next(
            emit,
            config,
            out,
            "csv",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )
        o2 = export_incremental_next(
            emit,
            config,
            out,
            "csv",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    assert o0.window is not None and o0.window.index == 0
    assert o1.window is not None and o1.window.index == 1
    assert o2.window is not None and o2.window.index == 2


def test_csv_cursor_matches_after_each_window(tmp_path: Path) -> None:
    """After each CSV window, the cursor file has the correct next_window_index."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config()
    out = tmp_path / "drops"

    with open_emit(emit_dir) as emit:
        for expected_next in [1, 2, 3]:
            outcome = export_incremental_next(
                emit,
                config,
                out,
                "csv",
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )
            assert outcome.window is not None
            w0_label = derive_window_zero_label(config)
            cursor = read_cursor(out, "csv", w0_label)
            assert cursor is not None
            assert cursor.next_window_index == expected_next


def test_csv_drained(tmp_path: Path) -> None:
    """CSV: window with start_ns > slice_at → status 'drained'."""
    emit_dir = _build_emit(tmp_path, slice_at=50)
    config = _simple_config()
    out = tmp_path / "drops"

    with open_emit(emit_dir) as emit:
        o0 = export_incremental_next(
            emit,
            config,
            out,
            "csv",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )
        assert o0.status == "emitted"
        o1 = export_incremental_next(
            emit,
            config,
            out,
            "csv",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    assert o1.status == "drained"


def test_csv_leftover_tmp_discarded(tmp_path: Path) -> None:
    """A leftover .tmp_* staging directory is discarded on the next call."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config()
    out = tmp_path / "drops"
    out.mkdir()

    # Simulate a leftover .tmp_ dir (it starts with '.', so hidden — it would be
    # .tmp_w00000_ns0 — but our helper uses the label which starts with 'w').
    # Actually .tmp_ dirs are hidden because they start with '.'.
    # The staging dir is out/.tmp_<label> — starts with '.' so hidden.
    # Simulate a pre-existing staging dir with stale content.
    label = f"w{0:05d}_ns{0 * _PERIOD_NS}"
    leftover = out / f".tmp_{label}"
    leftover.mkdir()
    (leftover / "stale.csv").write_text("stale data")

    with open_emit(emit_dir) as emit:
        outcome = export_incremental_next(
            emit,
            config,
            out,
            "csv",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    assert outcome.status == "emitted"
    # The stale dir was replaced by fresh content
    assert not leftover.exists()
    drop_dir = out / outcome.window.label  # type: ignore[union-attr]
    assert drop_dir.exists()


def test_csv_crash_recovery_restart(tmp_path: Path) -> None:
    """Crash-recovery: one window-0 drop dir, no cursor → re-run emits window 0."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config()
    out = tmp_path / "drops"
    out.mkdir()

    # Simulate: window-0 drop renamed, cursor write lost
    config_incremental = config.incremental
    assert config_incremental is not None
    # Compute window-0 label
    from fabulexa_forge.incremental.windows import derive_window

    w0 = derive_window(0, config_incremental, None)
    (out / w0.label).mkdir()

    with open_emit(emit_dir) as emit:
        outcome = export_incremental_next(
            emit,
            config,
            out,
            "csv",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    assert outcome.status == "emitted"
    assert outcome.window is not None
    assert outcome.window.index == 0


# ---------------------------------------------------------------------------
# Fingerprint mismatch
# ---------------------------------------------------------------------------


def test_duckdb_fingerprint_mismatch_raises(tmp_path: Path) -> None:
    """Stored fingerprint differs from computed → IncrementalFingerprintMismatch."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config()
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        export_incremental_next(
            emit,
            config,
            out,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )  # window 0

    # Change the config (adds a new table) to get a different fingerprint
    altered_config = ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "incremental": {"sim_period_ns": _PERIOD_NS},
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
                            {"name": "name_extra", "from": "prop__name"},
                        ],
                    }
                ]
            },
        }
    )

    with open_emit(emit_dir) as emit:
        with pytest.raises(IncrementalFingerprintMismatch):
            export_incremental_next(
                emit,
                altered_config,
                out,
                "duckdb",
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )


def test_csv_fingerprint_mismatch_fmt_change_raises(tmp_path: Path) -> None:
    """Fmt change mid-drip → IncrementalFingerprintMismatch for CSV."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config()
    out_csv = tmp_path / "drops"

    with open_emit(emit_dir) as emit:
        export_incremental_next(
            emit,
            config,
            out_csv,
            "csv",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )  # window 0

    # Now try to continue with a different fmt (same config, different fmt)
    # We simulate by directly writing a cursor with a fingerprint computed for duckdb
    # Actually, simpler: modify the cursor file directly

    w0_label = "w00000_ns0"
    cursor = read_cursor(out_csv, "csv", w0_label)
    assert cursor is not None

    # Write a cursor with a different fingerprint
    bad_cursor = Cursor(
        cursor_format_version=_CURRENT_CURSOR_FORMAT_VERSION,
        fingerprint="b" * 64,  # wrong fingerprint
        next_window_index=1,
    )
    write_csv_cursor(out_csv, bad_cursor)

    with open_emit(emit_dir) as emit:
        with pytest.raises(IncrementalFingerprintMismatch):
            export_incremental_next(
                emit,
                config,
                out_csv,
                "csv",
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )


# ---------------------------------------------------------------------------
# Empty window: emitted, never skipped
# ---------------------------------------------------------------------------


def test_duckdb_empty_window_is_emitted(tmp_path: Path) -> None:
    """Empty window (no rows in window range) is emitted, not skipped."""
    # Window 2: ns=[200,300) — entity e003 at sim_time=210 is in this window
    # Window 1: ns=[100,200) — entity e002 at sim_time=110 is in this window
    # If we use a fact/history grain, window 0 has sim_time=10, window 1 has 110, window 2 has 210
    # But for type-1 dim (records grain), windowing is on last_mutation_sim_time
    # Let's create an emit where window 1 is empty
    emit_dir = tmp_path / "emit_empty_window"
    emit_dir.mkdir()
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    col_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _RECORDS_COLUMNS)
    conn.execute(f'CREATE TABLE "records__entity" ({col_ddl})')
    # entity at sim_time=10 (in window 0: ns=[0,100))
    # no entity in window 1 (ns=[100,200))
    # entity at sim_time=210 (in window 2: ns=[200,300))
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
                "columns": _RECORDS_COLUMNS,
                "rows": 2,
                "record_kind": "entity",
            }
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 300}],
    )

    config = _simple_config()
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        o0 = export_incremental_next(
            emit,
            config,
            out,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )
        o1 = export_incremental_next(
            emit,
            config,
            out,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    # Window 0: type-1 dim — snapshot (all rows)
    assert o0.status == "emitted"
    # Window 1: type-1 dim — snapshot (still 2 rows: replace mode)
    assert o1.status == "emitted"
    assert o1.window is not None and o1.window.index == 1

    # Verify the window row was logged (empty window still logged)
    conn2 = duckdb.connect(str(out))
    win_count = conn2.execute("SELECT COUNT(*) FROM _export_windows").fetchone()
    conn2.close()
    assert win_count is not None and int(win_count[0]) == 2


# ---------------------------------------------------------------------------
# Range path: export_window
# ---------------------------------------------------------------------------


def test_range_target_exists_raises(tmp_path: Path) -> None:
    """export_window with index=None and existing out → IncrementalRangeTargetExists."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config(with_incremental=False)
    out = tmp_path / "range_out"
    out.mkdir()

    window = Window(index=None, start_ns=0, end_ns=100, label="r_ns0_ns100")

    with open_emit(emit_dir) as emit:
        with pytest.raises(IncrementalRangeTargetExists):
            export_window(
                emit,
                config,
                out,
                "duckdb",
                None,
                window,
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )


def test_range_duckdb_fresh_creates_standalone_artifact(tmp_path: Path) -> None:
    """export_window (index=None, duckdb) writes standalone artifact without bookkeeping."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config(with_incremental=False)
    out = tmp_path / "range.duckdb"

    window = Window(index=None, start_ns=0, end_ns=100, label="r_ns0_ns100")

    with open_emit(emit_dir) as emit:
        report = export_window(
            emit,
            config,
            out,
            "duckdb",
            None,
            window,
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    assert out.exists()
    assert "dim_entity" in {t.name for t in report.tables}

    # No bookkeeping tables
    conn = duckdb.connect(str(out))
    meta = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = '_export_meta'"
    ).fetchone()
    win = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = '_export_windows'"
    ).fetchone()
    conn.close()
    assert meta is not None and int(meta[0]) == 0
    assert win is not None and int(win[0]) == 0


def test_range_csv_fresh_creates_standalone_artifact(tmp_path: Path) -> None:
    """export_window (index=None, csv) writes standalone artifact, no cursor file."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config(with_incremental=False)
    out = tmp_path / "range_drop"

    window = Window(index=None, start_ns=0, end_ns=100, label="r_ns0_ns100")

    with open_emit(emit_dir) as emit:
        report = export_window(
            emit,
            config,
            out,
            "csv",
            None,
            window,
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    assert out.exists()
    assert "dim_entity" in {t.name for t in report.tables}
    # No cursor file
    assert not (out / ".fabulexa-forge-cursor.json").exists()


def test_next_against_range_artifact_raises(tmp_path: Path) -> None:
    """--next pointed at a range artifact (no _export_meta) → IncrementalCursorInvalid."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config(with_incremental=False)
    out = tmp_path / "range.duckdb"

    window = Window(index=None, start_ns=0, end_ns=100, label="r_ns0_ns100")

    with open_emit(emit_dir) as emit:
        export_window(
            emit,
            config,
            out,
            "duckdb",
            None,
            window,
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    # Now try --next on the same artifact
    config_with_inc = _simple_config(with_incremental=True)

    from fabulexa_forge.errors import IncrementalCursorInvalid

    with open_emit(emit_dir) as emit:
        with pytest.raises(IncrementalCursorInvalid, match="_export_meta"):
            export_incremental_next(
                emit,
                config_with_inc,
                out,
                "duckdb",
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )


# ---------------------------------------------------------------------------
# CSV error rollback: a write failure discards the staging dir; a rename
# failure raises ExportRuntimeError — for both --from/--to and --next paths.
# ---------------------------------------------------------------------------


def _patch_write_csv_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every write_csv call fail (the driver imports it at call time)."""

    def _boom(
        emit: Emit, table_name: str, query: str, output_dir: Path
    ) -> WrittenRelation:
        raise ExportRuntimeError("simulated CSV write failure")

    monkeypatch.setattr("fabulexa_forge.writers.csv.write_csv", _boom)


def _patch_staging_rename_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make Path.rename fail for .tmp_* staging directories only."""
    real_rename = Path.rename

    def _fail_rename(self: Path, target: str | Path) -> Path:
        if self.name.startswith(".tmp_"):
            raise OSError("simulated rename failure")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", _fail_rename)


def test_range_csv_write_failure_discards_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--from/--to CSV: a mid-export write failure removes the staging dir and
    the partial output never appears at out."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config(with_incremental=False)
    out = tmp_path / "range_drop"
    window = Window(index=None, start_ns=0, end_ns=100, label="r_ns0_ns100")

    _patch_write_csv_failure(monkeypatch)

    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportRuntimeError, match="simulated CSV write failure"):
            export_window(
                emit,
                config,
                out,
                "csv",
                None,
                window,
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )

    assert not out.exists()
    assert not (tmp_path / ".tmp_r_ns0_ns100").exists()


def test_next_csv_write_failure_discards_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--next CSV: a mid-export write failure removes the staging dir; no drop
    dir appears and no cursor is written."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config()
    out = tmp_path / "drops"

    _patch_write_csv_failure(monkeypatch)

    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportRuntimeError, match="simulated CSV write failure"):
            export_incremental_next(
                emit,
                config,
                out,
                "csv",
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )

    label = "w00000_ns0"
    assert not (out / f".tmp_{label}").exists()
    assert not (out / label).exists()
    assert not (out / ".fabulexa-forge-cursor.json").exists()


def test_range_csv_rename_failure_raises_and_discards_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--from/--to CSV: staging_dir.rename(out) failing raises
    ExportRuntimeError, removes the staging dir, and out never appears."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config(with_incremental=False)
    out = tmp_path / "range_drop"
    window = Window(index=None, start_ns=0, end_ns=100, label="r_ns0_ns100")

    _patch_staging_rename_failure(monkeypatch)

    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportRuntimeError, match="failed to rename staging dir"):
            export_window(
                emit,
                config,
                out,
                "csv",
                None,
                window,
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )

    assert not out.exists()
    assert not (tmp_path / ".tmp_r_ns0_ns100").exists()


def test_next_csv_rename_failure_raises_no_drop_no_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--next CSV: staging_dir.rename(drop_dir) failing raises
    ExportRuntimeError; no drop dir appears, no cursor is written, and the
    leftover .tmp_* staging dir awaits the next staging's discard."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config()
    out = tmp_path / "drops"

    _patch_staging_rename_failure(monkeypatch)

    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportRuntimeError, match="failed to rename staging dir"):
            export_incremental_next(
                emit,
                config,
                out,
                "csv",
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )

    label = "w00000_ns0"
    assert not (out / label).exists()
    assert not (out / ".fabulexa-forge-cursor.json").exists()
    # The leftover staging dir is documented to be discarded at the NEXT staging
    assert (out / f".tmp_{label}").exists()


# ---------------------------------------------------------------------------
# Invariant 2: DuckDB drip ≡ full export (query-equals after draining)
# ---------------------------------------------------------------------------


def test_duckdb_drip_equals_full_export(tmp_path: Path) -> None:
    """After draining, dim_entity in warehouse equals full-export warehouse (type-1 dim)."""
    from fabulexa_forge.exporters.dimensional.engine import export_dimensional

    emit_dir = _build_emit(tmp_path)
    config = _simple_config()
    config_no_inc = _simple_config(with_incremental=False)

    wh = tmp_path / "wh.duckdb"
    full_wh = tmp_path / "full.duckdb"

    # Full export
    with open_emit(emit_dir) as emit:
        export_dimensional(
            emit,
            config_no_inc,
            full_wh,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    # Drip to drained
    with open_emit(emit_dir) as emit:
        while True:
            outcome = export_incremental_next(
                emit,
                config,
                wh,
                "duckdb",
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )
            if outcome.status == "drained":
                break

    # Compare dim_entity rows
    conn_wh = duckdb.connect(str(wh))
    conn_full = duckdb.connect(str(full_wh))

    drip_rows = conn_wh.execute(
        'SELECT id, name FROM "dim_entity" ORDER BY id'
    ).fetchall()
    full_rows = conn_full.execute(
        'SELECT id, name FROM "dim_entity" ORDER BY id'
    ).fetchall()

    conn_wh.close()
    conn_full.close()

    assert drip_rows == full_rows


# ---------------------------------------------------------------------------
# Invariant 3: CSV concatenation ≡ full export (type-1 dim = last window's snapshot)
# ---------------------------------------------------------------------------


def test_csv_drip_equals_full_export(tmp_path: Path) -> None:
    """All CSV drops multiset-equal the full export for a type-1 dim (replace)."""
    import csv

    from fabulexa_forge.exporters.dimensional.engine import export_dimensional

    emit_dir = _build_emit(tmp_path)
    config = _simple_config()
    config_no_inc = _simple_config(with_incremental=False)

    out_csv = tmp_path / "drops"
    full_csv = tmp_path / "full"
    full_csv.mkdir()

    # Full export
    with open_emit(emit_dir) as emit:
        export_dimensional(
            emit,
            config_no_inc,
            full_csv,
            "csv",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    # Drip to drained
    last_label: str | None = None
    with open_emit(emit_dir) as emit:
        while True:
            outcome = export_incremental_next(
                emit,
                config,
                out_csv,
                "csv",
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )
            if outcome.status == "drained":
                break
            assert outcome.window is not None
            last_label = outcome.window.label

    # For type-1 dim, the last window's snapshot is the complete snapshot
    assert last_label is not None
    drip_file = out_csv / last_label / "dim_entity.csv"
    full_file = full_csv / "dim_entity.csv"

    drip_rows = set(
        tuple(row) for row in csv.reader(drip_file.read_text().splitlines())
    )
    full_rows = set(
        tuple(row) for row in csv.reader(full_file.read_text().splitlines())
    )

    assert drip_rows == full_rows


# ---------------------------------------------------------------------------
# Invariant 5: Two identical drips → byte-identical CSV drops and cursor files
# ---------------------------------------------------------------------------


def test_csv_determinism_byte_identical_drops(tmp_path: Path) -> None:
    """Same drip from scratch twice → identical CSV drop contents and cursor."""
    emit_dir = _build_emit(tmp_path)
    config = _simple_config()

    out_a = tmp_path / "drops_a"
    out_b = tmp_path / "drops_b"

    labels_a: list[str] = []
    labels_b: list[str] = []

    with open_emit(emit_dir) as emit:
        for _ in range(2):
            outcome = export_incremental_next(
                emit,
                config,
                out_a,
                "csv",
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )
            assert outcome.window is not None
            labels_a.append(outcome.window.label)

    with open_emit(emit_dir) as emit:
        for _ in range(2):
            outcome = export_incremental_next(
                emit,
                config,
                out_b,
                "csv",
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )
            assert outcome.window is not None
            labels_b.append(outcome.window.label)

    assert labels_a == labels_b

    for label in labels_a:
        files_a = sorted((out_a / label).iterdir())
        files_b = sorted((out_b / label).iterdir())
        assert [f.name for f in files_a] == [f.name for f in files_b]
        for fa, fb in zip(files_a, files_b):
            assert fa.read_bytes() == fb.read_bytes()

    # Cursor files are identical
    cursor_a = (out_a / ".fabulexa-forge-cursor.json").read_bytes()
    cursor_b = (out_b / ".fabulexa-forge-cursor.json").read_bytes()
    assert cursor_a == cursor_b


# ---------------------------------------------------------------------------
# Source-mode dispatch (Unit 2): export_window / export_incremental_next call
# the source engine when config.mode == 'source'. Row-count keys equal to a
# source table name (widget/visit/order/location/...) — never a dimensional
# name such as dim_entity — are the dispatch proof.
#
# Source mode always requires a resolved anchor (SourceAnchorRequired), and
# the sim-time-regime cadence (`sim_period_ns`) forbids one resolving
# (IncrementalPeriodRegimeMismatch) — so a source-mode drip's `incremental`
# block always uses the calendar regime (`period`). `build_day_scale_source_
# emit` (shared from `exporters.source._source_fixtures`) anchors at an exact
# UTC midnight so calendar-day windows land at clean day-multiple sim-time
# offsets, matching the fixture's activity.
# ---------------------------------------------------------------------------


def _source_config(
    source: dict[str, object],
    period: Literal["day", "week", "month"] = "day",
) -> ExportConfig:
    """Build a mode='source' config with a calendar-regime incremental block
    (the only regime compatible with source mode's mandatory anchor).

    Args:
        source: The `source` section dict (`tables` / `events` /
            `declare_keys` — the declared grammar).
        period: incremental.period.

    Returns:
        Validated ExportConfig.
    """
    data: dict[str, object] = {
        "mode": "source",
        "incremental": {"period": period},
        "source": source,
    }
    return ExportConfig.model_validate(data)


def _source_config_no_incremental(source: dict[str, object]) -> ExportConfig:
    """Build a mode='source' config with no incremental block (range export).

    Args:
        source: The `source` section dict (`tables` / `events` /
            `declare_keys` — the declared grammar).
    """
    data: dict[str, object] = {"mode": "source", "source": source}
    return ExportConfig.model_validate(data)


def _drain_source_drip(
    emit: Emit,
    config: ExportConfig,
    out: Path,
    fmt: Literal["csv", "duckdb"],
) -> list[IncrementalOutcome]:
    """Drip a mode='source' config to drained, returning every emitted outcome.

    Args:
        emit: The open emit (must declare a runtime anchor).
        config: A mode='source' config with an incremental block.
        out: Output target per fmt.
        fmt: 'duckdb' or 'csv'.

    Returns:
        The 'emitted' outcomes in window order (the 'drained' call excluded).
    """
    anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
    outcomes: list[IncrementalOutcome] = []
    while True:
        outcome = export_incremental_next(
            emit,
            config,
            out,
            fmt,
            anchor,
            notice_sink=discard_notice_sink,
            overlay=None,
        )
        if outcome.status == "drained":
            return outcomes
        outcomes.append(outcome)


def test_source_mode_duckdb_multi_window_drip_dispatches_to_source_engine(
    tmp_path: Path,
) -> None:
    """--next over mode='source' drips calendar-day windows via
    build_source_query_specs: a declared `state` table's windowed render is
    a full-horizon snapshot (rows accumulate across windows) — window 0
    sees w001 only, window 1 adds w002, windows 2-3 hold steady at 2 (a
    later property change and an empty window neither add nor drop rows),
    window 4 drains."""
    emit_dir = build_day_scale_source_emit(tmp_path)
    config = _source_config(source={"tables": [{"name": "widget", "kind": "widget"}]})
    out = tmp_path / "wh.duckdb"

    outcomes: list[IncrementalOutcome] = []
    row_counts: list[int] = []
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        while True:
            outcome = export_incremental_next(
                emit,
                config,
                out,
                "duckdb",
                anchor,
                notice_sink=discard_notice_sink,
                overlay=None,
            )
            if outcome.status == "drained":
                break
            outcomes.append(outcome)
            conn = duckdb.connect(str(out), read_only=True)
            try:
                count = conn.execute("SELECT COUNT(*) FROM widget").fetchone()
            finally:
                conn.close()
            assert count is not None
            row_counts.append(int(count[0]))

    assert [o.window.index for o in outcomes if o.window is not None] == [0, 1, 2, 3]
    for outcome in outcomes:
        assert outcome.report is not None
        assert {t.name for t in outcome.report.tables} == {"widget"}
    assert row_counts == [1, 2, 2, 2]


def test_source_mode_csv_multi_window_drip_dispatches_to_source_engine(
    tmp_path: Path,
) -> None:
    """--next CSV drip over mode='source' writes one drop dir per calendar-day
    window with the source table name, dispatching through
    build_source_query_specs."""
    emit_dir = build_day_scale_source_emit(tmp_path)
    config = _source_config(source={"tables": [{"name": "widget", "kind": "widget"}]})
    out = tmp_path / "drops"

    with open_emit(emit_dir) as emit:
        outcomes = _drain_source_drip(emit, config, out, "csv")

    assert len(outcomes) == 4
    for outcome in outcomes:
        assert outcome.window is not None
        drop_dir = out / outcome.window.label
        assert {p.stem for p in drop_dir.glob("*.csv")} == {"widget"}


def test_source_mode_export_window_explicit_range_dispatches_to_source_engine(
    tmp_path: Path,
) -> None:
    """--from/--to (export_window, window.index=None) over mode='source' writes
    a standalone artifact via build_source_query_specs, no bookkeeping tables.
    export_window never derives a window from a cadence, so the ms-scale
    spanning fixture and its explicit Windows apply directly."""
    emit_dir = build_windowed_source_test_emit(tmp_path)
    config = _source_config_no_incremental(
        source={
            "tables": [
                {"name": "visit", "kind": "visit"},
                {"name": "order", "kind": "order"},
                {"name": "location", "kind": "location"},
                {
                    "name": "visit_team",
                    "membership": {"kind": "visit", "property": "team"},
                },
            ]
        }
    )
    out = tmp_path / "range.duckdb"
    window, _, _ = windowed_test_windows()
    range_window = Window(
        index=None, start_ns=window.start_ns, end_ns=window.end_ns, label="r_ns0_ns100"
    )

    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        report = export_window(
            emit,
            config,
            out,
            "duckdb",
            anchor,
            range_window,
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    assert {t.name for t in report.tables} == {
        "visit",
        "order",
        "location",
        "visit_team",
    }

    conn = duckdb.connect(str(out))
    meta = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = '_export_meta'"
    ).fetchone()
    conn.close()
    assert meta is not None and int(meta[0]) == 0


def test_source_mode_fingerprint_mismatch_on_source_config_change(
    tmp_path: Path,
) -> None:
    """A source.tables change mid-drip (dropping a declared table) flips the
    drip fingerprint and raises IncrementalFingerprintMismatch, exactly as a
    dimensional.* config change does today."""
    emit_dir = build_source_test_emit(tmp_path)
    config = _source_config(
        source={
            "tables": [
                {"name": "visit", "kind": "visit"},
                {"name": "location", "kind": "location"},
            ]
        }
    )
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        export_incremental_next(
            emit,
            config,
            out,
            "duckdb",
            anchor,
            notice_sink=discard_notice_sink,
            overlay=None,
        )  # window 0

    altered_config = _source_config(
        source={"tables": [{"name": "visit", "kind": "visit"}]}
    )

    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        with pytest.raises(IncrementalFingerprintMismatch):
            export_incremental_next(
                emit,
                altered_config,
                out,
                "duckdb",
                anchor,
                notice_sink=discard_notice_sink,
                overlay=None,
            )


# ---------------------------------------------------------------------------
# Source mode: a `where`-narrowed event log's `id` stays tape-anchored across
# a windowed run (source-row-selection sprint § Phase 3, doc § Row selection)
# ---------------------------------------------------------------------------


def test_source_mode_events_where_narrowed_windowed_ids_match_full_export(
    tmp_path: Path,
) -> None:
    """A windowed export of a `where`-narrowed event log carries the same
    `id` values the full export of the same tape assigns — `id` is
    tape-anchored beneath the window predicate, not renumbered per window."""
    emit_dir = build_windowed_source_test_emit(tmp_path)
    config = _source_config_no_incremental(
        source={
            "events": {
                "name": "audit_log",
                "sources": [{"kind": "order", "where": {"amount": ["100.0", "300.0"]}}],
            }
        }
    )

    w0, w1, w2 = windowed_test_windows()
    full_window = Window(index=None, start_ns=0, end_ns=w2.end_ns, label="r_full")

    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)

        full_out = tmp_path / "full.duckdb"
        export_window(
            emit,
            config,
            full_out,
            "duckdb",
            anchor,
            full_window,
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )
        with duckdb.connect(str(full_out)) as conn:
            full_rows = conn.execute(
                'SELECT "id", "item_id" FROM "audit_log"'
            ).fetchall()
        full_by_item = {item_id: row_id for row_id, item_id in full_rows}
        assert full_by_item, "the narrowed selection retained something to compare"

        for index, window in enumerate((w0, w1, w2)):
            range_window = Window(
                index=None,
                start_ns=window.start_ns,
                end_ns=window.end_ns,
                label=f"r{index}",
            )
            out = tmp_path / f"window{index}.duckdb"
            export_window(
                emit,
                config,
                out,
                "duckdb",
                anchor,
                range_window,
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )
            with duckdb.connect(str(out)) as conn:
                window_rows = conn.execute(
                    'SELECT "id", "item_id" FROM "audit_log"'
                ).fetchall()
            for row_id, item_id in window_rows:
                assert full_by_item[item_id] == row_id


# ---------------------------------------------------------------------------
# Base mode: build_base_query_specs dispatch (the three-way regression fix)
#
# Base mode requires no resolved anchor (anchor=None throughout) and, unlike
# source, supports the sim-time regime (`sim_period_ns`) directly.
# ---------------------------------------------------------------------------


def _base_config(sim_period_ns: int) -> ExportConfig:
    """Build a mode='base' config with a sim-time-regime incremental block.

    Args:
        sim_period_ns: incremental.sim_period_ns.

    Returns:
        Validated ExportConfig.
    """
    return ExportConfig.model_validate(
        {"mode": "base", "incremental": {"sim_period_ns": sim_period_ns}}
    )


def test_base_mode_multi_window_drip_dispatches_to_base_engine(tmp_path: Path) -> None:
    """--next over mode='base' drips full per-window snapshots via
    build_base_query_specs: three non-drained windows over the patient
    fixture's 5*DAY_NS slice_at, each a full 3-row snapshot."""
    emit_dir = build_base_test_emit(tmp_path)
    config = _base_config(sim_period_ns=2 * _BASE_DAY_NS)
    out = tmp_path / "wh.duckdb"

    outcomes: list[IncrementalOutcome] = []
    patient_counts: list[int] = []
    with open_emit(emit_dir) as emit:
        while True:
            outcome = export_incremental_next(
                emit,
                config,
                out,
                "duckdb",
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )
            if outcome.status == "drained":
                break
            outcomes.append(outcome)
            conn = duckdb.connect(str(out), read_only=True)
            try:
                count = conn.execute("SELECT COUNT(*) FROM patient").fetchone()
            finally:
                conn.close()
            assert count is not None
            patient_counts.append(int(count[0]))

    assert [o.window.index for o in outcomes if o.window is not None] == [0, 1, 2]
    for outcome in outcomes:
        assert outcome.report is not None
        assert {t.name for t in outcome.report.tables} == {"patient"}
    assert patient_counts == [3, 3, 3]


def test_base_mode_config_no_longer_reaches_dimensional_branch(tmp_path: Path) -> None:
    """A mode='base' config with no `dimensional` section drips cleanly — the
    regression a two-way (source vs. else) dispatch would hit: `else` used to
    mean 'dimensional', so build_query_specs' `assert config.dimensional is
    not None` would raise for a base-mode config."""
    emit_dir = build_base_test_emit(tmp_path)
    config = _base_config(sim_period_ns=2 * _BASE_DAY_NS)
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        outcome = export_incremental_next(
            emit,
            config,
            out,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    assert outcome.status == "emitted"
    assert outcome.window is not None
    assert outcome.window.index == 0


# ---------------------------------------------------------------------------
# declare_keys: the CSV notice (base and source mode), windowed DuckDB
# constraints across windows, and a falsifying window's atomic rollback.
# ---------------------------------------------------------------------------

_KEYED_PATIENT_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]

#: A flat kind's key claim: unique within the branch — a plain
#: record_index-class declaration (mirrors the phase-3 demo's `patient`).
_KEYED_PATIENT_PRESENTATION_KEYS: dict[str, object] = {
    "key": {
        "unique_within": "branch",
        "branch_stable": True,
        "slice_stable": True,
        "key_space": {"class": "record_index", "prefix": "", "width": 4},
    }
}


def _build_keyed_base_emit(
    tmp_path: Path,
    *,
    rows: list[tuple[str, int, int]],
    slice_at: int,
) -> Path:
    """Build a base-mode emit: one `patient` kind claiming `presentation_id`
    unique-within-branch.

    Args:
        tmp_path: Directory for the emit artifacts.
        rows: (record_id, presentation_id, created_sim_time) triples, in
            record_index order.
        slice_at: The branch's slice_at value.

    Returns:
        The emit directory.
    """
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    col_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _KEYED_PATIENT_COLUMNS)
    conn.execute(f'CREATE TABLE "records__patient" ({col_ddl})')
    for record_index, (record_id, presentation_id, created_sim_time) in enumerate(rows):
        conn.execute(
            'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
            [
                "trunk",
                record_id,
                presentation_id,
                created_sim_time,
                True,
                created_sim_time,
                record_index,
            ],
        )
    history_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLUMNS)
    conn.execute(f'CREATE TABLE "history" ({history_ddl})')
    conn.close()

    write_emit(
        emit_dir,
        tables=[
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": _KEYED_PATIENT_COLUMNS,
                "rows": len(rows),
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 0,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": slice_at}],
        extra={"presentation_keys": {"patient": _KEYED_PATIENT_PRESENTATION_KEYS}},
    )
    return emit_dir


def _base_config_declare_keys(sim_period_ns: int, declare_keys: bool) -> ExportConfig:
    """Build a mode='base' config with `declare_keys` and a sim-time-regime
    incremental block."""
    return ExportConfig.model_validate(
        {
            "mode": "base",
            "base": {"declare_keys": declare_keys},
            "incremental": {"sim_period_ns": sim_period_ns},
        }
    )


def _duckdb_constraint_columns(
    db_path: Path, table_name: str
) -> list[tuple[str, tuple]]:
    """Return (constraint_type, constraint_column_names) pairs for a table."""
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = conn.execute(
            "SELECT constraint_type, constraint_column_names"
            " FROM duckdb_constraints() WHERE table_name = ?",
            [table_name],
        ).fetchall()
    finally:
        conn.close()
    return [(str(r[0]), tuple(r[1])) for r in rows]


def test_declare_keys_csv_notice_base_mode_once_per_invocation(tmp_path: Path) -> None:
    """CSV + base-mode declare_keys: exactly one keys-not-declarable-csv
    notice per --next invocation, re-emitted on the second drip."""
    emit_dir = _build_keyed_base_emit(
        tmp_path, rows=[("p001", 101, 0), ("p002", 102, 0)], slice_at=1000
    )
    config = _base_config_declare_keys(sim_period_ns=500, declare_keys=True)
    out = tmp_path / "drops"

    with open_emit(emit_dir) as emit:
        sink1 = RecordingNoticeSink()
        export_incremental_next(
            emit, config, out, "csv", None, notice_sink=sink1, overlay=None
        )
        codes1 = [n.code for n in sink1.notices]
        assert codes1.count(NOTICE_KEYS_NOT_DECLARABLE_CSV) == 1

        sink2 = RecordingNoticeSink()
        export_incremental_next(
            emit, config, out, "csv", None, notice_sink=sink2, overlay=None
        )
        codes2 = [n.code for n in sink2.notices]
        assert codes2.count(NOTICE_KEYS_NOT_DECLARABLE_CSV) == 1


def test_declare_keys_csv_notice_source_mode_once_per_invocation(
    tmp_path: Path,
) -> None:
    """CSV + source-mode declare_keys: exactly one keys-not-declarable-csv
    notice per --next invocation."""
    emit_dir = build_source_test_emit(tmp_path)
    config = _source_config(
        source={
            "tables": [{"name": "visit", "kind": "visit"}],
            "declare_keys": True,
        }
    )
    out = tmp_path / "drops"

    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        sink = RecordingNoticeSink()
        export_incremental_next(
            emit, config, out, "csv", anchor, notice_sink=sink, overlay=None
        )

    assert [n.code for n in sink.notices].count(NOTICE_KEYS_NOT_DECLARABLE_CSV) == 1


def test_windowed_duckdb_declare_keys_constraints_carried_across_windows(
    tmp_path: Path,
) -> None:
    """Windowed DuckDB + declare_keys: window 1 creates the table with the
    declared constraints (no CSV notice); window 2's replace preserves them,
    with row growth from a patient created between the two horizons."""
    emit_dir = _build_keyed_base_emit(
        tmp_path,
        rows=[("p001", 101, 0), ("p002", 102, 0), ("p003", 103, 150)],
        slice_at=150,
    )
    config = _base_config_declare_keys(sim_period_ns=100, declare_keys=True)
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        sink1 = RecordingNoticeSink()
        outcome1 = export_incremental_next(
            emit, config, out, "duckdb", None, sink1, overlay=None
        )
        assert outcome1.status == "emitted"
        assert outcome1.report is not None
        assert {t.name for t in outcome1.report.tables} == {"patient"}
        conn = duckdb.connect(str(out), read_only=True)
        try:
            count1 = conn.execute("SELECT COUNT(*) FROM patient").fetchone()
        finally:
            conn.close()
        assert count1 is not None and int(count1[0]) == 2
        assert NOTICE_KEYS_NOT_DECLARABLE_CSV not in [n.code for n in sink1.notices]

        constraints_after_1 = _duckdb_constraint_columns(out, "patient")
        assert ("PRIMARY KEY", ("patient_key",)) in constraints_after_1
        assert ("UNIQUE", ("id",)) in constraints_after_1
        assert ("UNIQUE", ("presentation_id",)) in constraints_after_1

        sink2 = RecordingNoticeSink()
        outcome2 = export_incremental_next(
            emit, config, out, "duckdb", None, sink2, overlay=None
        )
        assert outcome2.status == "emitted"
        assert outcome2.report is not None
        assert {t.name for t in outcome2.report.tables} == {"patient"}
        conn = duckdb.connect(str(out), read_only=True)
        try:
            count2 = conn.execute("SELECT COUNT(*) FROM patient").fetchone()
        finally:
            conn.close()
        assert count2 is not None and int(count2[0]) == 3

        constraints_after_2 = _duckdb_constraint_columns(out, "patient")
        assert constraints_after_2 == constraints_after_1


def test_windowed_duckdb_declare_keys_falsifying_window_rolls_back(
    tmp_path: Path,
) -> None:
    """A window whose data falsifies a declared key raises ExportRuntimeError
    and leaves the warehouse (rows, _export_windows, cursor) exactly as
    before."""
    emit_dir = _build_keyed_base_emit(
        tmp_path,
        # p003 duplicates p001's presentation_id, created after window 0's
        # horizon so it only appears in window 1's snapshot.
        rows=[("p001", 101, 0), ("p002", 102, 0), ("p003", 101, 150)],
        slice_at=150,
    )
    config = _base_config_declare_keys(sim_period_ns=100, declare_keys=True)
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        export_incremental_next(
            emit, config, out, "duckdb", None, discard_notice_sink, overlay=None
        )

        with pytest.raises(ExportRuntimeError):
            export_incremental_next(
                emit, config, out, "duckdb", None, discard_notice_sink, overlay=None
            )

    conn = duckdb.connect(str(out), read_only=True)
    try:
        patient_rows = conn.execute("SELECT COUNT(*) FROM patient").fetchone()
        window_rows = conn.execute("SELECT COUNT(*) FROM _export_windows").fetchone()
    finally:
        conn.close()
    assert patient_rows is not None and int(patient_rows[0]) == 2
    assert window_rows is not None and int(window_rows[0]) == 1

    cursor = read_cursor(out, "duckdb", window_zero_label="")
    assert cursor is not None and cursor.next_window_index == 1


def test_declare_keys_fingerprint_mismatch_on_flip(tmp_path: Path) -> None:
    """Flipping `declare_keys` mid-drip changes the config fingerprint and
    refuses per the existing mismatch rule."""
    emit_dir = _build_keyed_base_emit(
        tmp_path, rows=[("p001", 101, 0), ("p002", 102, 0)], slice_at=1000
    )
    config = _base_config_declare_keys(sim_period_ns=500, declare_keys=True)
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        export_incremental_next(
            emit, config, out, "duckdb", None, discard_notice_sink, overlay=None
        )

    flipped = _base_config_declare_keys(sim_period_ns=500, declare_keys=False)
    with open_emit(emit_dir) as emit:
        with pytest.raises(IncrementalFingerprintMismatch):
            export_incremental_next(
                emit, flipped, out, "duckdb", None, discard_notice_sink, overlay=None
            )


# ---------------------------------------------------------------------------
# Helper for tests
# ---------------------------------------------------------------------------


def derive_window_zero_label(config: ExportConfig) -> str:
    """Compute the window-0 label for a sim-time-regime config (no anchor)."""
    from fabulexa_forge.incremental.windows import derive_window

    assert config.incremental is not None
    w0 = derive_window(0, config.incremental, None)
    return w0.label
