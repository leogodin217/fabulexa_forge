"""Tests for `fabulexa-forge export` CLI verb.

Covers:
- fmt not in {csv,duckdb}: non-zero + usage message on stderr, before emit opens
- --fmt jsonl -> usage error, exit 1, before the emit opens
- valid config + emit: writes star schema, returns 0
- ReaderError (bad emit dir): non-zero + message on stderr
- ExportError (multi-branch emit): non-zero + message on stderr
- main(["export", ...]) dispatches correctly
- --base-date + --timezone rebase end-to-end (exit 0, expected values)
- --base-date / --timezone override a config rebase block
- tz-aware --base-date -> RebaseDateNotNaive (exit 1)
- malformed --base-date -> argparse usage error (exit 1 after catch)
- bogus --timezone -> RebaseUnknownTimezone (exit 1)
- rebase: {} -> ConfigError (exit 1)
- mode: cdc config -> ConfigError (exit 1), rejected at load time
- the remaining incremental CLI error funnel: IncrementalAnchorRequired,
  IncrementalPeriodRegimeMismatch, IncrementalCursorInvalid, and
  IncrementalRangeInvalid each surface through cmd_export as exit 1
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest
import yaml
from _support.sidecar_builder import identity_column as _identity_column
from _support.sidecar_builder import write_emit as _write_sidecar

import fabulexa_forge.cli
from exporters._emit_fixtures import _create_ddl, _table_spec
from exporters.base._base_fixtures import build_base_test_emit
from exporters.source._source_fixtures import build_day_scale_source_emit
from fabulexa_forge.cli import cmd_export, main
from fabulexa_forge.config.models import (
    ColumnDecl,
    DimensionalConfig,
    ExportConfig,
    SourceDecl,
    TableDecl,
)

# ---------------------------------------------------------------------------
# Emit builder helpers
# ---------------------------------------------------------------------------

_ACTOR_COLUMNS: list[dict[str, object]] = [
    _identity_column("fork_path", "VARCHAR"),
    _identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT", "history_tracked": False},
    {"name": "active", "type": "BOOLEAN", "history_tracked": False},
    {"name": "deactivated_at", "type": "BIGINT", "history_tracked": False},
    {"name": "last_mutation_sim_time", "type": "BIGINT", "history_tracked": False},
    _identity_column("record_index", "BIGINT"),
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]


def build_single_branch_emit(tmp_path: Path) -> Path:
    """Build a minimal single-branch emit with one records__actor table."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a001", 50, True, 100, 0, "Alice"],
    )
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor", "records", _ACTOR_COLUMNS, 1, record_kind="actor"
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 200}],
    )
    return tmp_path


def build_two_branch_emit(tmp_path: Path) -> Path:
    """Build a two-branch emit that triggers the SingleBranch business rule."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor", "records", _ACTOR_COLUMNS, 0, record_kind="actor"
            ),
        ],
        branches=[
            {"fork_path": "trunk", "parent": None, "slice_at": 0},
            {"fork_path": "trunk@branch_a", "parent": "trunk", "slice_at": 50},
        ],
    )
    return tmp_path


def write_minimal_config(config_path: Path) -> None:
    """Write a minimal valid export config YAML."""
    config = ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_actor",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(grain="records", kind="actor"),
                    key=["id"],
                    columns=[ColumnDecl(name="id", **{"from": "record_id"})],
                )
            ]
        ),
    )
    config_path.write_text(
        yaml.dump(json.loads(config.model_dump_json()), allow_unicode=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Tests — fmt validation (must happen before emit is opened)
# ---------------------------------------------------------------------------


def test_cmd_export_bad_fmt_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_export with unknown fmt returns non-zero with a usage message on stderr."""
    # The emit dir does not exist — fmt is checked first, so this still fails on fmt
    missing_emit = tmp_path / "no_such_emit"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mode: dimensional\n", encoding="utf-8")
    out = tmp_path / "out"

    exit_code = cmd_export(missing_emit, config_path, out, "parquet")
    captured = capsys.readouterr()
    assert exit_code != 0
    assert "fmt" in captured.err.lower() or "parquet" in captured.err


def test_cmd_export_bad_fmt_missing_emit_still_fails_on_fmt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing emit dir with a bad fmt still fails on fmt first (not on missing dir)."""
    missing_emit = tmp_path / "does_not_exist"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mode: dimensional\n", encoding="utf-8")
    out = tmp_path / "out"

    exit_code = cmd_export(missing_emit, config_path, out, "json")
    captured = capsys.readouterr()
    assert exit_code != 0
    # Error must mention fmt, not the missing directory
    assert "fmt" in captured.err.lower() or "json" in captured.err
    assert "not found" not in captured.err.lower()


# ---------------------------------------------------------------------------
# Tests — valid export
# ---------------------------------------------------------------------------


def test_cmd_export_csv_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_export with valid config + emit writes CSV and returns 0."""
    emit_dir = build_single_branch_emit(tmp_path / "emit")
    config_path = tmp_path / "config.yaml"
    write_minimal_config(config_path)
    out_dir = tmp_path / "out_csv"
    out_dir.mkdir()

    exit_code = cmd_export(emit_dir, config_path, out_dir, "csv")
    assert exit_code == 0
    assert (out_dir / "dim_actor.csv").exists()


def test_cmd_export_duckdb_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_export with valid config + emit writes DuckDB and returns 0."""
    emit_dir = build_single_branch_emit(tmp_path / "emit")
    config_path = tmp_path / "config.yaml"
    write_minimal_config(config_path)
    out_db = tmp_path / "out.duckdb"

    exit_code = cmd_export(emit_dir, config_path, out_db, "duckdb")
    assert exit_code == 0
    assert out_db.exists()


# ---------------------------------------------------------------------------
# Tests — session-zone pin
# ---------------------------------------------------------------------------


def test_cmd_export_pins_session_when_anchor_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cmd_export pins the session zone when a sidecar runtime anchor resolves."""
    calls: list[object] = []
    real_pin = fabulexa_forge.cli.pin_session_timezone

    def _recording_pin(emit: object, anchor: object) -> None:
        calls.append(anchor)
        real_pin(emit, anchor)  # type: ignore[arg-type]

    monkeypatch.setattr(fabulexa_forge.cli, "pin_session_timezone", _recording_pin)

    emit_dir = build_runtime_emit(
        tmp_path / "emit", "2024-01-15T12:00:00+00:00", "America/New_York"
    )
    config_path = tmp_path / "config.yaml"
    write_minimal_config(config_path)
    out_dir = tmp_path / "out_csv"
    out_dir.mkdir()

    exit_code = cmd_export(emit_dir, config_path, out_dir, "csv")
    assert exit_code == 0
    assert len(calls) == 1


def test_cmd_export_does_not_pin_when_anchor_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cmd_export never touches session state when no anchor resolves."""
    calls: list[object] = []
    monkeypatch.setattr(
        fabulexa_forge.cli,
        "pin_session_timezone",
        lambda emit, anchor: calls.append(anchor),
    )

    emit_dir = build_single_branch_emit(tmp_path / "emit")
    config_path = tmp_path / "config.yaml"
    write_minimal_config(config_path)
    out_dir = tmp_path / "out_csv"
    out_dir.mkdir()

    exit_code = cmd_export(emit_dir, config_path, out_dir, "csv")
    assert exit_code == 0
    assert calls == []


# ---------------------------------------------------------------------------
# Tests — error surfaces
# ---------------------------------------------------------------------------


def test_cmd_export_reader_error_surfaces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_export surfaces a ReaderError (bad emit) to stderr with a non-zero exit."""
    missing_emit = tmp_path / "no_emit"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mode: dimensional\n", encoding="utf-8")
    out = tmp_path / "out"

    exit_code = cmd_export(missing_emit, config_path, out, "csv")
    captured = capsys.readouterr()
    assert exit_code != 0
    assert "error" in captured.err.lower() or "not found" in captured.err.lower()


def test_cmd_export_export_error_surfaces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_export surfaces an ExportError (multi-branch emit) to stderr with non-zero exit."""
    emit_dir = build_two_branch_emit(tmp_path / "emit")
    config_path = tmp_path / "config.yaml"
    write_minimal_config(config_path)
    out_dir = tmp_path / "out_csv"
    out_dir.mkdir()

    exit_code = cmd_export(emit_dir, config_path, out_dir, "csv")
    captured = capsys.readouterr()
    assert exit_code != 0
    assert captured.err.strip() != ""


# ---------------------------------------------------------------------------
# Tests — main dispatch
# ---------------------------------------------------------------------------


def test_main_export_dispatches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main(['export', ...]) dispatches to cmd_export correctly."""
    emit_dir = build_single_branch_emit(tmp_path / "emit")
    config_path = tmp_path / "config.yaml"
    write_minimal_config(config_path)
    out_dir = tmp_path / "out_csv"
    out_dir.mkdir()

    exit_code = main(
        ["export", str(emit_dir), str(config_path), str(out_dir), "--fmt", "csv"]
    )
    assert exit_code == 0


def test_main_unknown_verb_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    """main with unknown verb returns non-zero."""
    exit_code = main(["foobar"])
    captured = capsys.readouterr()
    assert exit_code != 0
    assert "unknown" in captured.err.lower() or "foobar" in captured.err


# ---------------------------------------------------------------------------
# Helpers for rebase CLI tests
# ---------------------------------------------------------------------------


def build_runtime_emit(tmp_path: Path, start_datetime: str, timezone_str: str) -> Path:
    """Build a single-branch emit with a runtime anchor.

    Args:
        tmp_path: Directory to write the emit artifacts into.
        start_datetime: ISO-8601 tz-aware start datetime string for the sidecar.
        timezone_str: IANA timezone string for the sidecar.

    Returns:
        tmp_path (the emit directory).
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a001", 0, True, 10_000_000_000, 0, "Alice"],
    )
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor", "records", _ACTOR_COLUMNS, 1, record_kind="actor"
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 200_000_000_000}],
        extra={
            "runtime": {
                "timezone": timezone_str,
                "start_datetime": start_datetime,
            },
        },
    )
    return tmp_path


def write_timestamp_config(
    config_path: Path, rebase_block: dict[str, object] | None = None
) -> None:
    """Write a config with a derived: timestamp column, optionally with a rebase block.

    Args:
        config_path: Path to write the YAML config to.
        rebase_block: Optional rebase dict to include; omitted when None.
    """
    config_dict: dict[str, object] = {
        "mode": "dimensional",
        "dimensional": {
            "tables": [
                {
                    "name": "dim_actor",
                    "role": "dim",
                    "scd": "type1",
                    "source": {"grain": "records", "kind": "actor"},
                    "key": ["id"],
                    "columns": [
                        {"name": "id", "from": "record_id"},
                        {
                            "name": "ts",
                            "derived": {
                                "timestamp": {"source": "last_mutation_sim_time"}
                            },
                        },
                    ],
                }
            ]
        },
    }
    if rebase_block is not None:
        config_dict["rebase"] = rebase_block
    config_path.write_text(yaml.dump(config_dict, allow_unicode=True), encoding="utf-8")


def _read_ts_column(db_path: Path) -> list[object]:
    """Read ts column from dim_actor in a DuckDB output file."""
    conn = duckdb.connect(str(db_path), read_only=True)
    rows = conn.execute('SELECT "ts" FROM "dim_actor" ORDER BY "ts"').fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Tests — --base-date + --timezone rebase end-to-end
# ---------------------------------------------------------------------------


def test_cmd_export_rebase_flags_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--base-date + --timezone rebase end-to-end returns exit 0."""
    emit_dir = build_runtime_emit(
        tmp_path / "emit",
        start_datetime="2024-01-01T00:00:00+00:00",
        timezone_str="UTC",
    )
    config_path = tmp_path / "config.yaml"
    write_timestamp_config(config_path)
    out_db = tmp_path / "out.duckdb"

    exit_code = cmd_export(
        emit_dir,
        config_path,
        out_db,
        "duckdb",
        cli_base_date=datetime(2024, 6, 1, 0, 0, 0),
        cli_timezone="UTC",
    )
    assert exit_code == 0
    assert out_db.exists()


def test_cmd_export_rebase_flags_shift_timestamp(tmp_path: Path) -> None:
    """--base-date shifts the rendered timestamp by exactly the origin delta."""
    emit_dir = build_runtime_emit(
        tmp_path / "emit",
        start_datetime="2024-01-01T00:00:00+00:00",
        timezone_str="UTC",
    )
    config_path = tmp_path / "config.yaml"
    write_timestamp_config(config_path)
    out_identity = tmp_path / "identity.duckdb"
    out_rebased = tmp_path / "rebased.duckdb"

    # Identity run
    exit_code = cmd_export(emit_dir, config_path, out_identity, "duckdb")
    assert exit_code == 0

    # Rebased run: 31 days later
    exit_code = cmd_export(
        emit_dir,
        config_path,
        out_rebased,
        "duckdb",
        cli_base_date=datetime(2024, 2, 1, 0, 0, 0),
        cli_timezone="UTC",
    )
    assert exit_code == 0

    ts_identity = _read_ts_column(out_identity)
    ts_rebased = _read_ts_column(out_rebased)
    assert len(ts_identity) == len(ts_rebased) == 1

    orig_dt = datetime.fromisoformat(str(ts_identity[0]))
    reb_dt = datetime.fromisoformat(str(ts_rebased[0]))
    orig_wall = orig_dt.replace(tzinfo=None)
    reb_wall = reb_dt.replace(tzinfo=None)
    delta = reb_wall - orig_wall
    assert abs(delta - timedelta(days=31)) < timedelta(seconds=1), (
        f"Expected 31-day shift, got {delta}"
    )


def test_cmd_export_cli_overrides_config_rebase(tmp_path: Path) -> None:
    """--base-date / --timezone override a config rebase block."""
    emit_dir = build_runtime_emit(
        tmp_path / "emit",
        start_datetime="2024-01-01T00:00:00+00:00",
        timezone_str="UTC",
    )
    config_path = tmp_path / "config.yaml"
    # Config has a rebase to March 1; CLI overrides to June 1
    write_timestamp_config(
        config_path,
        rebase_block={"base_date": "2024-03-01T00:00:00", "timezone": "UTC"},
    )
    out_cli = tmp_path / "cli_wins.duckdb"

    exit_code = cmd_export(
        emit_dir,
        config_path,
        out_cli,
        "duckdb",
        cli_base_date=datetime(2024, 6, 1, 0, 0, 0),
        cli_timezone="UTC",
    )
    assert exit_code == 0

    ts_rows = _read_ts_column(out_cli)
    assert len(ts_rows) == 1
    ts_str = str(ts_rows[0])
    # Should reflect June origin (sim_time=10s → 2024-06-01T00:00:10), not March
    assert "2024-06" in ts_str, f"Expected June origin (CLI wins), got {ts_str}"


# ---------------------------------------------------------------------------
# Tests — fail-fast error cases
# ---------------------------------------------------------------------------


def test_cmd_export_tz_aware_base_date_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """tz-aware --base-date -> RebaseDateNotNaive (exit 1)."""
    emit_dir = build_runtime_emit(
        tmp_path / "emit",
        start_datetime="2024-01-01T00:00:00+00:00",
        timezone_str="UTC",
    )
    config_path = tmp_path / "config.yaml"
    write_timestamp_config(config_path)
    out_db = tmp_path / "out.duckdb"

    tz_aware_date = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    exit_code = cmd_export(
        emit_dir, config_path, out_db, "duckdb", cli_base_date=tz_aware_date
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert (
        "naive" in captured.err.lower()
        or "tzinfo" in captured.err.lower()
        or "ERROR" in captured.err
    )


def test_main_malformed_base_date_argparse_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """malformed --base-date -> argparse usage error (exit 2)."""
    emit_dir = build_runtime_emit(
        tmp_path / "emit",
        start_datetime="2024-01-01T00:00:00+00:00",
        timezone_str="UTC",
    )
    config_path = tmp_path / "config.yaml"
    write_timestamp_config(config_path)
    out_db = tmp_path / "out.duckdb"

    exit_code = main(
        [
            "export",
            str(emit_dir),
            str(config_path),
            str(out_db),
            "--fmt",
            "duckdb",
            "--base-date",
            "not-a-date",
        ]
    )
    assert exit_code == 2


def test_cmd_export_bogus_timezone_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """bogus --timezone -> RebaseUnknownTimezone (exit 1)."""
    emit_dir = build_runtime_emit(
        tmp_path / "emit",
        start_datetime="2024-01-01T00:00:00+00:00",
        timezone_str="UTC",
    )
    config_path = tmp_path / "config.yaml"
    write_timestamp_config(config_path)
    out_db = tmp_path / "out.duckdb"

    exit_code = cmd_export(
        emit_dir, config_path, out_db, "duckdb", cli_timezone="Not/AReal/Zone"
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR" in captured.err or "timezone" in captured.err.lower()


def test_cmd_export_empty_rebase_block_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """rebase: {} -> ConfigError (exit 1)."""
    emit_dir = build_single_branch_emit(tmp_path / "emit")
    config_path = tmp_path / "config.yaml"
    # Write a YAML config with an empty rebase block
    config_path.write_text(
        "mode: dimensional\n"
        "rebase: {}\n"
        "dimensional:\n"
        "  tables:\n"
        "  - name: dim_actor\n"
        "    role: dim\n"
        "    scd: type1\n"
        "    source:\n"
        "      grain: records\n"
        "      kind: actor\n"
        "    key: [id]\n"
        "    columns:\n"
        "    - name: id\n"
        "      from: record_id\n",
        encoding="utf-8",
    )
    out_db = tmp_path / "out.duckdb"

    exit_code = cmd_export(emit_dir, config_path, out_db, "duckdb")
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR" in captured.err


# ---------------------------------------------------------------------------
# Phase 5: --next, --from/--to, drained exit code
# ---------------------------------------------------------------------------

_INCR_RECORDS_COLUMNS: list[dict[str, object]] = [
    _identity_column("fork_path", "VARCHAR"),
    _identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT", "history_tracked": False},
    {"name": "active", "type": "BOOLEAN", "history_tracked": False},
    {"name": "deactivated_at", "type": "BIGINT", "history_tracked": False},
    {"name": "last_mutation_sim_time", "type": "BIGINT", "history_tracked": False},
    _identity_column("record_index", "BIGINT"),
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_INCR_PERIOD_NS = 100


def build_incremental_emit(tmp_path: Path, slice_at: int = 250) -> Path:
    """Build a minimal emit with three entities at sim_times 10, 110, 210.

    Args:
        tmp_path: Directory for the emit artifacts.
        slice_at: The branch's slice_at value.

    Returns:
        tmp_path (the emit directory).
    """
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    col_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _INCR_RECORDS_COLUMNS)
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
    conn.close()

    _write_sidecar(
        emit_dir,
        tables=[
            {
                "name": "records__entity",
                "category": "records",
                "columns": _INCR_RECORDS_COLUMNS,
                "rows": 3,
                "record_kind": "entity",
            }
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": slice_at}],
    )
    return emit_dir


def write_incremental_config(
    config_path: Path, sim_period_ns: int = _INCR_PERIOD_NS
) -> None:
    """Write a minimal config with an incremental block (sim_period_ns).

    Args:
        config_path: Path to write the YAML config to.
        sim_period_ns: Window size in nanoseconds.
    """
    config_dict: dict[str, object] = {
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
    config_path.write_text(yaml.dump(config_dict, allow_unicode=True), encoding="utf-8")


def write_no_incremental_config(config_path: Path) -> None:
    """Write a minimal config WITHOUT an incremental block.

    Args:
        config_path: Path to write the YAML config to.
    """
    config_dict: dict[str, object] = {
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
    config_path.write_text(yaml.dump(config_dict, allow_unicode=True), encoding="utf-8")


# --next flag combination errors (must not open the emit)


def test_next_with_from_to_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--next combined with --from/--to returns exit 1, usage error on stderr."""
    missing_emit = tmp_path / "no_emit"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mode: dimensional\n", encoding="utf-8")
    out = tmp_path / "out"

    exit_code = cmd_export(
        missing_emit,
        config_path,
        out,
        "duckdb",
        next_window=True,
        range_from="2020-01-01",
        range_to="2020-01-02",
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "usage" in captured.err.lower() or "--next" in captured.err


def test_from_without_to_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--from without --to returns exit 1."""
    missing_emit = tmp_path / "no_emit"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mode: dimensional\n", encoding="utf-8")
    out = tmp_path / "out"

    exit_code = cmd_export(
        missing_emit, config_path, out, "duckdb", range_from="2020-01-01"
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "usage" in captured.err.lower() or "--from" in captured.err


def test_to_without_from_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--to without --from returns exit 1."""
    missing_emit = tmp_path / "no_emit"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mode: dimensional\n", encoding="utf-8")
    out = tmp_path / "out"

    exit_code = cmd_export(
        missing_emit, config_path, out, "duckdb", range_to="2020-01-02"
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "usage" in captured.err.lower() or "--to" in captured.err


# --next without incremental block


def test_next_without_incremental_block_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--next without an incremental block exits 1 with IncrementalConfigMissing on stderr."""
    emit_dir = build_incremental_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    write_no_incremental_config(config_path)
    out = tmp_path / "wh.duckdb"

    exit_code = cmd_export(emit_dir, config_path, out, "duckdb", next_window=True)
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR" in captured.err


# --next drip via cmd_export: windows, labels, drained exit code


def test_next_drip_duckdb_to_drained(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--next drip exits 0 each window (label-prefixed row counts), exits 3 when drained."""
    emit_dir = build_incremental_emit(tmp_path, slice_at=250)
    config_path = tmp_path / "config.yaml"
    write_incremental_config(config_path)
    out = tmp_path / "wh.duckdb"

    exit_codes: list[int] = []
    all_stdout: list[str] = []
    for _ in range(6):  # enough iterations to drain
        code = cmd_export(emit_dir, config_path, out, "duckdb", next_window=True)
        captured = capsys.readouterr()
        exit_codes.append(code)
        all_stdout.append(captured.out)
        if code == 3:
            break

    assert exit_codes[-1] == 3, f"Expected drained (3), got {exit_codes}"
    assert all(c == 0 for c in exit_codes[:-1]), (
        f"Expected 0 before drain, got {exit_codes}"
    )

    # Each 0-exit window printed a label-prefixed row-count line: the
    # windowed manifest's own row_count stays None, but stdout restores the
    # real per-table count (sourced from the writer's WrittenRelation).
    for stdout in all_stdout[:-1]:
        assert "[w" in stdout, f"Expected label-prefix in: {stdout!r}"
        assert " rows" in stdout, f"Expected row count in: {stdout!r}"

    # Drained message on stdout
    last_stdout = all_stdout[-1]
    assert "drained" in last_stdout.lower()


def test_next_drip_csv_to_drained(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--next CSV drip exits 0 per window and 3 when drained."""
    emit_dir = build_incremental_emit(tmp_path, slice_at=150)
    config_path = tmp_path / "config.yaml"
    write_incremental_config(config_path)
    out = tmp_path / "drops"

    exit_codes: list[int] = []
    for _ in range(5):
        code = cmd_export(emit_dir, config_path, out, "csv", next_window=True)
        capsys.readouterr()
        exit_codes.append(code)
        if code == 3:
            break

    assert exit_codes[-1] == 3
    assert all(c == 0 for c in exit_codes[:-1])


# --next via main() (argparse path)


def test_main_next_drip_duckdb(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main(['export', ..., '--next']) drips and drains via the argparse path."""
    emit_dir = build_incremental_emit(tmp_path, slice_at=150)
    config_path = tmp_path / "config.yaml"
    write_incremental_config(config_path)
    out = tmp_path / "wh.duckdb"

    exit_codes: list[int] = []
    for _ in range(5):
        code = main(
            [
                "export",
                str(emit_dir),
                str(config_path),
                str(out),
                "--fmt",
                "duckdb",
                "--next",
            ]
        )
        capsys.readouterr()
        exit_codes.append(code)
        if code == 3:
            break

    assert exit_codes[-1] == 3
    assert all(c == 0 for c in exit_codes[:-1])


# --from/--to range export


def test_from_to_fresh_target_exit_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--from/--to to a fresh target exits 0 with label-prefixed row counts."""
    emit_dir = build_incremental_emit(tmp_path, slice_at=250)
    config_path = tmp_path / "config.yaml"
    write_incremental_config(config_path)
    out = tmp_path / "range_out.duckdb"

    exit_code = cmd_export(
        emit_dir,
        config_path,
        out,
        "duckdb",
        range_from="0",
        range_to="200",
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert out.exists()
    assert "[r_ns0_ns200]" in captured.out
    # A windowed export restores per-table row counts on stdout (the
    # windowed manifest's own row_count stays None; this is presentation
    # only, sourced from the writer's WrittenRelation).
    assert " rows" in captured.out


def test_from_to_existing_target_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--from/--to to an existing target exits 1 (IncrementalRangeTargetExists)."""
    emit_dir = build_incremental_emit(tmp_path, slice_at=250)
    config_path = tmp_path / "config.yaml"
    write_incremental_config(config_path)
    out = tmp_path / "range_out.duckdb"
    out.write_bytes(b"")  # pre-create

    exit_code = cmd_export(
        emit_dir,
        config_path,
        out,
        "duckdb",
        range_from="0",
        range_to="200",
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR" in captured.err


# No new flags: existing full export unchanged


def test_no_incremental_flags_full_export_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --next/--from/--to the existing full export path is unchanged."""
    emit_dir = build_incremental_emit(tmp_path, slice_at=250)
    config_path = tmp_path / "config.yaml"
    write_no_incremental_config(config_path)
    out = tmp_path / "full.duckdb"

    exit_code = cmd_export(emit_dir, config_path, out, "duckdb")
    captured = capsys.readouterr()
    assert exit_code == 0
    assert out.exists()
    # Full export does not print label prefix
    assert "[" not in captured.out


# Remaining incremental error funnel: anchor/regime/cursor/range errors


def write_calendar_incremental_config(config_path: Path) -> None:
    """Write a minimal dimensional config with a calendar incremental block.

    Args:
        config_path: Path to write the YAML config to.
    """
    config_dict: dict[str, object] = {
        "mode": "dimensional",
        "incremental": {"period": "day"},
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
    config_path.write_text(yaml.dump(config_dict, allow_unicode=True), encoding="utf-8")


def test_next_period_without_anchor_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--next with a calendar `period` but no resolvable anchor exits 1
    (IncrementalAnchorRequired via the funnel)."""
    emit_dir = build_incremental_emit(tmp_path)  # no runtime block
    config_path = tmp_path / "config.yaml"
    write_calendar_incremental_config(config_path)
    out = tmp_path / "wh.duckdb"

    exit_code = cmd_export(emit_dir, config_path, out, "duckdb", next_window=True)
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR" in captured.err
    assert "anchor" in captured.err.lower()
    assert not out.exists()


def test_next_sim_period_with_anchor_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--next with `sim_period_ns` while an anchor resolves (CLI --base-date)
    exits 1 (IncrementalPeriodRegimeMismatch via the funnel)."""
    emit_dir = build_incremental_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    write_incremental_config(config_path)  # sim_period_ns regime
    out = tmp_path / "wh.duckdb"

    exit_code = cmd_export(
        emit_dir,
        config_path,
        out,
        "duckdb",
        cli_base_date=datetime(2024, 1, 1, 0, 0, 0),
        cli_timezone="UTC",
        next_window=True,
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR" in captured.err
    assert "sim_period_ns" in captured.err
    assert not out.exists()


def test_next_foreign_warehouse_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--next into a non-empty warehouse without _export_meta exits 1
    (IncrementalCursorInvalid via the funnel)."""
    emit_dir = build_incremental_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    write_incremental_config(config_path)
    out = tmp_path / "wh.duckdb"

    # A warehouse not created by --next: non-empty catalog, no _export_meta.
    conn = duckdb.connect(str(out))
    conn.execute("CREATE TABLE stray (x INTEGER)")
    conn.close()

    exit_code = cmd_export(emit_dir, config_path, out, "duckdb", next_window=True)
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR" in captured.err
    assert "_export_meta" in captured.err


def test_from_to_unparseable_sim_offset_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--from that is not an integer ns offset (no anchor resolves) exits 1
    (IncrementalRangeInvalid via the funnel)."""
    emit_dir = build_incremental_emit(tmp_path)  # no runtime block
    config_path = tmp_path / "config.yaml"
    write_incremental_config(config_path)
    out = tmp_path / "range_out.duckdb"

    exit_code = cmd_export(
        emit_dir,
        config_path,
        out,
        "duckdb",
        range_from="not-a-number",
        range_to="200",
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR" in captured.err
    assert "integer ns offset" in captured.err
    assert not out.exists()


def test_from_to_reversed_order_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--from >= --to exits 1 (IncrementalRangeInvalid via the funnel)."""
    emit_dir = build_incremental_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    write_incremental_config(config_path)
    out = tmp_path / "range_out.duckdb"

    exit_code = cmd_export(
        emit_dir,
        config_path,
        out,
        "duckdb",
        range_from="200",
        range_to="100",
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR" in captured.err
    assert "strictly before" in captured.err
    assert not out.exists()


# ---------------------------------------------------------------------------
# CDC/JSONL rejection tests
# ---------------------------------------------------------------------------


def test_fmt_jsonl_rejected_before_emit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--fmt jsonl -> usage error, exit 1, before the emit opens."""
    missing_emit = tmp_path / "no_emit"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mode: dimensional\n", encoding="utf-8")
    out = tmp_path / "out"

    exit_code = cmd_export(missing_emit, config_path, out, "jsonl")
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "jsonl" in captured.err or "fmt" in captured.err.lower()
    assert "not found" not in captured.err.lower()


def test_mode_cdc_config_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """mode: cdc config is rejected at load time (exit 1, ERROR on stderr)."""
    emit_dir = build_single_branch_emit(tmp_path / "emit")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "mode: cdc\ncdc:\n  table: change_events\n", encoding="utf-8"
    )
    out = tmp_path / "out"

    exit_code = cmd_export(emit_dir, config_path, out, "csv")
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR" in captured.err


# ---------------------------------------------------------------------------
# Phase 4: mode: source dispatch + incremental-flag guard
# ---------------------------------------------------------------------------

_SOURCE_LOCATION_COLUMNS: list[dict[str, object]] = [
    _identity_column("fork_path", "VARCHAR"),
    _identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT", "history_tracked": False},
    {"name": "active", "type": "BOOLEAN", "history_tracked": False},
    {"name": "deactivated_at", "type": "BIGINT", "history_tracked": False},
    {"name": "last_mutation_sim_time", "type": "BIGINT", "history_tracked": False},
    _identity_column("record_index", "BIGINT"),
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]


def build_source_emit(tmp_path: Path, with_runtime: bool = True) -> Path:
    """Build a minimal single-kind emit valid for `mode: source`.

    An untracked `location` kind with a `dimension` role — genre 'reference',
    default output table `location`.

    Args:
        tmp_path: Directory to write the emit artifacts into.
        with_runtime: Whether the sidecar carries a `runtime` anchor block
            (False builds the SourceAnchorRequired fixture).

    Returns:
        tmp_path (the emit directory).
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__location", _SOURCE_LOCATION_COLUMNS))
    conn.execute(
        'INSERT INTO "records__location" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "loc001", 10, True, 10, 0, "Ward A"],
    )
    conn.close()

    extra: dict[str, object] = {"record_roles": {"location": "dimension"}}
    if with_runtime:
        extra["runtime"] = {
            "timezone": "UTC",
            "start_datetime": "2024-01-01T00:00:00+00:00",
        }
    _write_sidecar(
        tmp_path,
        tables=[
            _table_spec(
                "records__location",
                "records",
                _SOURCE_LOCATION_COLUMNS,
                1,
                record_kind="location",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 200}],
        extra=extra,
    )
    return tmp_path


def write_source_config(config_path: Path) -> None:
    """Write a `mode: source` export config declaring one `state` table over
    the `location` kind.

    Args:
        config_path: Path to write the YAML config to.
    """
    config_path.write_text(
        "mode: source\nsource:\n  tables:\n  - name: location\n    kind: location\n",
        encoding="utf-8",
    )


def test_cmd_export_source_mode_csv_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """mode: source dispatches to export_source and writes CSV."""
    emit_dir = build_source_emit(tmp_path / "emit")
    config_path = tmp_path / "config.yaml"
    write_source_config(config_path)
    out_dir = tmp_path / "out_csv"
    out_dir.mkdir()

    exit_code = cmd_export(emit_dir, config_path, out_dir, "csv")
    assert exit_code == 0
    assert (out_dir / "location.csv").exists()


def test_cmd_export_source_mode_duckdb_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """mode: source dispatches to export_source and writes DuckDB."""
    emit_dir = build_source_emit(tmp_path / "emit")
    config_path = tmp_path / "config.yaml"
    write_source_config(config_path)
    out_db = tmp_path / "out.duckdb"

    exit_code = cmd_export(emit_dir, config_path, out_db, "duckdb")
    assert exit_code == 0
    assert out_db.exists()


def write_source_incremental_config(config_path: Path, kind: str = "location") -> None:
    """Write a `mode: source` config with a calendar-day incremental block,
    declaring one `state` table over `kind`.

    Source mode always requires a resolved anchor, so its incremental block
    must use the calendar regime (`period`), not `sim_period_ns`.

    Args:
        config_path: Path to write the YAML config to.
        kind: The records kind to declare the sole `state` table over.
    """
    config_path.write_text(
        "mode: source\nincremental:\n  period: day\nsource:\n"
        f"  tables:\n  - name: {kind}\n    kind: {kind}\n",
        encoding="utf-8",
    )


def test_cmd_export_source_mode_next_supported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """mode: source + --next is supported (the incremental flags are no
    longer source-mode-rejected)."""
    emit_dir = build_source_emit(tmp_path / "emit")
    config_path = tmp_path / "config.yaml"
    write_source_incremental_config(config_path)
    out = tmp_path / "wh.duckdb"

    exit_code = cmd_export(emit_dir, config_path, out, "duckdb", next_window=True)
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "[w" in captured.out


def test_cmd_export_source_mode_from_to_supported(tmp_path: Path) -> None:
    """mode: source + --from/--to is supported (the incremental flags are no
    longer source-mode-rejected)."""
    emit_dir = build_source_emit(tmp_path / "emit")
    config_path = tmp_path / "config.yaml"
    write_source_config(config_path)
    out = tmp_path / "range.duckdb"

    exit_code = cmd_export(
        emit_dir,
        config_path,
        out,
        "duckdb",
        range_from="2024-01-01",
        range_to="2024-01-02",
    )
    assert exit_code == 0
    assert out.exists()


def write_base_config(config_path: Path) -> None:
    """Write a bare `mode: base` export config (no exclude/rename/slice_at).

    Args:
        config_path: Path to write the YAML config to.
    """
    config_path.write_text("mode: base\n", encoding="utf-8")


def test_cmd_export_base_mode_duckdb_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """mode: base dispatches to export_base and prints per-table counts."""
    emit_subdir = tmp_path / "emit"
    emit_subdir.mkdir()
    emit_dir = build_base_test_emit(emit_subdir)
    config_path = tmp_path / "config.yaml"
    write_base_config(config_path)
    out_db = tmp_path / "out.duckdb"

    exit_code = cmd_export(emit_dir, config_path, out_db, "duckdb")
    captured = capsys.readouterr()
    assert exit_code == 0
    assert out_db.exists()
    assert "patient: 3 rows" in captured.out


def test_cmd_export_base_mode_from_to_supported(tmp_path: Path) -> None:
    """mode: base + --from/--to writes a standalone range export."""
    emit_subdir = tmp_path / "emit"
    emit_subdir.mkdir()
    emit_dir = build_base_test_emit(emit_subdir)
    config_path = tmp_path / "config.yaml"
    write_base_config(config_path)
    out = tmp_path / "range.duckdb"

    exit_code = cmd_export(
        emit_dir,
        config_path,
        out,
        "duckdb",
        range_from="2024-01-01",
        range_to="2024-01-03",
    )
    assert exit_code == 0
    assert out.exists()


def test_cmd_export_source_mode_no_anchor_surfaces_source_anchor_required(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """mode: source with no resolvable anchor -> SourceAnchorRequired, exit 1."""
    emit_dir = build_source_emit(tmp_path / "emit", with_runtime=False)
    config_path = tmp_path / "config.yaml"
    write_source_config(config_path)
    out = tmp_path / "out"

    exit_code = cmd_export(emit_dir, config_path, out, "csv")
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR" in captured.err
    assert "anchor" in captured.err.lower()


def test_cmd_export_source_mode_cli_flags_reach_anchor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--base-date/--timezone reach the source anchor with no sidecar runtime."""
    emit_dir = build_source_emit(tmp_path / "emit", with_runtime=False)
    config_path = tmp_path / "config.yaml"
    write_source_config(config_path)
    out_dir = tmp_path / "out_csv"
    out_dir.mkdir()

    exit_code = cmd_export(
        emit_dir,
        config_path,
        out_dir,
        "csv",
        cli_base_date=datetime(2024, 6, 1, 0, 0, 0),
        cli_timezone="UTC",
    )
    assert exit_code == 0
    assert (out_dir / "location.csv").exists()


def test_cmd_export_source_mode_next_drip_to_drained(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A multi-window --next drip over mode: source exits 0 per calendar-day
    window (label-prefixed counts), then exits 3 once drained."""
    emit_dir = build_day_scale_source_emit(tmp_path / "emit")
    config_path = tmp_path / "config.yaml"
    write_source_incremental_config(config_path, kind="widget")
    out = tmp_path / "wh.duckdb"

    exit_codes: list[int] = []
    all_stdout: list[str] = []
    for _ in range(6):  # enough iterations to drain
        code = cmd_export(emit_dir, config_path, out, "duckdb", next_window=True)
        captured = capsys.readouterr()
        exit_codes.append(code)
        all_stdout.append(captured.out)
        if code == 3:
            break

    assert exit_codes[-1] == 3, f"Expected drained (3), got {exit_codes}"
    assert all(c == 0 for c in exit_codes[:-1]), (
        f"Expected 0 before drain, got {exit_codes}"
    )
    assert len(exit_codes) == 5, f"Expected 4 windows then drained, got {exit_codes}"

    for stdout in all_stdout[:-1]:
        assert "[w" in stdout, f"Expected label-prefix in: {stdout!r}"
        assert "widget" in stdout

    assert "drained" in all_stdout[-1].lower()


def test_main_export_source_mode_dispatches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main(['export', ...]) dispatches mode: source through the argparse path."""
    emit_dir = build_source_emit(tmp_path / "emit")
    config_path = tmp_path / "config.yaml"
    write_source_config(config_path)
    out_dir = tmp_path / "out_csv"
    out_dir.mkdir()

    exit_code = main(
        ["export", str(emit_dir), str(config_path), str(out_dir), "--fmt", "csv"]
    )
    assert exit_code == 0
    assert (out_dir / "location.csv").exists()
