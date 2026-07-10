"""Tests for `fabulexa-forge mixer` CLI verb.

Covers:
- Flag-level usage checks (exit 1, before any emit opens):
  --fmt invalid; --speed out-of-range; --tick <= 0; --port out-of-range
- main(["mixer", ...]) dispatches to cmd_mixer with parsed flags
- --play / --paused mutual exclusion collapsing to cli_playing
- Setup-phase errors land in the (ReaderError, ExporterError) funnel as exit 1
- Serving-phase errors (serve_mixer raising KafkaDeliveryError / KafkaConsumeError)
  land in the second (ReaderError, ExporterError) funnel as exit 1
- MixerExtraUnavailable: FastAPI not importable -> exit 1 with the install hint
- --join happy path: 'fact:dim' parses to ('fact', 'dim') through main()
- Happy path: serve_mixer patched to a no-op coroutine, exit 0
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import duckdb
import pytest
import yaml

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.cli import _parse_join_flag, cmd_mixer, main
from fabulexa_forge.errors import KafkaConsumeError, KafkaDeliveryError

# ---------------------------------------------------------------------------
# Emit / config builder helpers
# ---------------------------------------------------------------------------

_RECORD_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__status", "type": "VARCHAR", "history_tracked": True},
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _build_minimal_emit(tmp_path: Path) -> Path:
    """Build a minimal single-branch v4 emit with one kind and a rebase anchor."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__actor", _RECORD_COLS))
    conn.execute(_ddl("history", _HISTORY_COLS))

    _DAY = 86_400_000_000_000
    actor_rows: list[tuple[Any, ...]] = [
        ("trunk", "a001", 1 * _DAY, True, None, 1 * _DAY, "active"),
    ]
    for row in actor_rows:
        conn.execute(
            'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?)', list(row)
        )
    history_rows: list[tuple[Any, ...]] = [
        ("trunk", "actor", "a001", "status", 1 * _DAY, "active"),
    ]
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "runtime": {
            "start_datetime": "2026-01-01T00:00:00+00:00",
            "timezone": "UTC",
        },
        "tables": [
            {
                "name": "records__actor",
                "category": "records",
                "columns": _RECORD_COLS,
                "rows": 1,
                "record_kind": "actor",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLS,
                "rows": 1,
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return emit_dir


def _write_stream_config(config_path: Path) -> None:
    """Write a minimal stream config YAML with a kafka block."""
    doc: dict[str, object] = {
        "content": "state-changes",
        "kinds": [{"kind": "actor", "properties": ["status"]}],
        "kafka": {"bootstrap_servers": "localhost:9092"},
    }
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


def _write_bad_config(config_path: Path) -> None:
    """Write a config that fails Pydantic validation (missing required content)."""
    doc: dict[str, object] = {"kinds": []}
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


def _write_stream_config_no_bootstrap(config_path: Path) -> None:
    """Write a stream config without any kafka block (bootstrap unresolvable)."""
    doc: dict[str, object] = {
        "content": "state-changes",
        "kinds": [{"kind": "actor", "properties": ["status"]}],
    }
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


def _write_debezium_config_no_block(config_path: Path) -> None:
    """Write a stream config for debezium without a debezium block."""
    doc: dict[str, object] = {
        "content": "state-changes",
        "kinds": [{"kind": "actor", "properties": ["status"]}],
        "kafka": {"bootstrap_servers": "localhost:9092"},
    }
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


# ---------------------------------------------------------------------------
# Flag-level usage checks (exit 1 before open_emit)
# ---------------------------------------------------------------------------


def test_fmt_invalid_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--fmt with an unsupported value exits 1 before any emit opens."""
    emit_dir = tmp_path / "emit"
    config_path = tmp_path / "config.yaml"
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="parquet",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers=None,
        host="127.0.0.1",
        port=8765,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "Usage error" in captured.err
    assert "fmt" in captured.err


def test_speed_too_low_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--speed below 0.1 exits 1 before any emit opens."""
    emit_dir = tmp_path / "emit"
    config_path = tmp_path / "config.yaml"
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=0.05,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers=None,
        host="127.0.0.1",
        port=8765,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "Usage error" in captured.err
    assert "speed" in captured.err


def test_speed_too_high_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--speed above 1000 exits 1 before any emit opens."""
    emit_dir = tmp_path / "emit"
    config_path = tmp_path / "config.yaml"
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1001.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers=None,
        host="127.0.0.1",
        port=8765,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "Usage error" in captured.err
    assert "speed" in captured.err


def test_tick_zero_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--tick <= 0 exits 1 before any emit opens."""
    emit_dir = tmp_path / "emit"
    config_path = tmp_path / "config.yaml"
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.0,
        cli_bootstrap_servers=None,
        host="127.0.0.1",
        port=8765,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "Usage error" in captured.err
    assert "tick" in captured.err


def test_tick_negative_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--tick < 0 exits 1 before any emit opens."""
    emit_dir = tmp_path / "emit"
    config_path = tmp_path / "config.yaml"
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=-1.0,
        cli_bootstrap_servers=None,
        host="127.0.0.1",
        port=8765,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "Usage error" in captured.err
    assert "tick" in captured.err


def test_port_too_low_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--port 0 exits 1 before any emit opens."""
    emit_dir = tmp_path / "emit"
    config_path = tmp_path / "config.yaml"
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers=None,
        host="127.0.0.1",
        port=0,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "Usage error" in captured.err
    assert "port" in captured.err


def test_port_too_high_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--port 65536 exits 1 before any emit opens."""
    emit_dir = tmp_path / "emit"
    config_path = tmp_path / "config.yaml"
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers=None,
        host="127.0.0.1",
        port=65536,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "Usage error" in captured.err
    assert "port" in captured.err


# ---------------------------------------------------------------------------
# Setup-phase funnel errors — exit 1, no server binds
# ---------------------------------------------------------------------------


def test_bad_config_exits_1_via_funnel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ConfigError from a bad config file lands in the funnel as exit 1."""
    emit_dir = _build_minimal_emit(tmp_path)
    config_path = tmp_path / "bad.yaml"
    _write_bad_config(config_path)

    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers="localhost:9092",
        host="127.0.0.1",
        port=8765,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "ERROR" in captured.err


def test_no_bootstrap_exits_1_via_funnel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """KafkaBootstrapUnresolvable lands in the funnel as exit 1."""
    emit_dir = _build_minimal_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_stream_config_no_bootstrap(config_path)

    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers=None,
        host="127.0.0.1",
        port=8765,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "ERROR" in captured.err


def test_debezium_requires_config_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--fmt debezium with no debezium block raises DebeziumRequiresConfig via funnel."""
    emit_dir = _build_minimal_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_debezium_config_no_block(config_path)

    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="debezium",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers="localhost:9092",
        host="127.0.0.1",
        port=8765,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "ERROR" in captured.err


def test_no_resolvable_anchor_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """KafkaRequiresAnchor (no resolvable anchor) exits 1 via funnel before server binds."""
    # Build emit WITHOUT a sidecar runtime anchor so no anchor resolves.
    emit_dir = tmp_path / "emit_no_anchor"
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__actor", _RECORD_COLS))
    conn.execute(_ddl("history", _HISTORY_COLS))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            {
                "name": "records__actor",
                "category": "records",
                "columns": _RECORD_COLS,
                "rows": 0,
                "record_kind": "actor",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLS,
                "rows": 0,
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    _write_stream_config(config_path)

    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers="localhost:9092",
        host="127.0.0.1",
        port=8765,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "ERROR" in captured.err


# ---------------------------------------------------------------------------
# Happy path: serve_mixer patched to a no-op coroutine
# ---------------------------------------------------------------------------


def test_happy_path_exit_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With serve_mixer patched to a no-op, cmd_mixer completes setup and exits 0."""
    emit_dir = _build_minimal_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_stream_config(config_path)

    import fabulexa_forge.exporters.streaming.mixer.serve as serve_mod

    async def _noop_serve(**kwargs: Any) -> None:
        pass

    monkeypatch.setattr(serve_mod, "serve_mixer", _noop_serve)
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers="localhost:9092",
        host="127.0.0.1",
        port=8765,
    )

    assert code == 0


# ---------------------------------------------------------------------------
# main() dispatch to cmd_mixer
# ---------------------------------------------------------------------------


def test_main_dispatches_to_mixer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main(["mixer", ...]) dispatches to cmd_mixer (via _cmd_mixer)."""
    emit_dir = _build_minimal_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_stream_config(config_path)

    import fabulexa_forge.exporters.streaming.mixer.serve as serve_mod

    monkeypatch.setattr(serve_mod, "serve_mixer", AsyncMock(return_value=None))
    code = main(
        [
            "mixer",
            str(emit_dir),
            str(config_path),
            "--fmt",
            "jsonl",
            "--bootstrap-servers",
            "localhost:9092",
        ]
    )

    assert code == 0


def test_main_play_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--play sets cli_playing=True; --paused sets cli_playing=False (default)."""
    emit_dir = _build_minimal_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_stream_config(config_path)

    captured_state: dict[str, Any] = {}

    import fabulexa_forge.exporters.streaming.mixer.serve as serve_mod

    async def _capture_serve(**kwargs: Any) -> None:
        captured_state.update(kwargs)

    monkeypatch.setattr(serve_mod, "serve_mixer", _capture_serve)
    code = main(
        [
            "mixer",
            str(emit_dir),
            str(config_path),
            "--fmt",
            "jsonl",
            "--bootstrap-servers",
            "localhost:9092",
            "--play",
        ]
    )

    assert code == 0
    # When launched playing, state.control.transport.playing is True
    state = captured_state.get("state")
    assert state is not None
    assert state.control.transport.playing is True


def test_main_paused_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--paused sets cli_playing=False (the default)."""
    emit_dir = _build_minimal_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_stream_config(config_path)

    captured_state: dict[str, Any] = {}

    import fabulexa_forge.exporters.streaming.mixer.serve as serve_mod

    async def _capture_serve(**kwargs: Any) -> None:
        captured_state.update(kwargs)

    monkeypatch.setattr(serve_mod, "serve_mixer", _capture_serve)
    code = main(
        [
            "mixer",
            str(emit_dir),
            str(config_path),
            "--fmt",
            "jsonl",
            "--bootstrap-servers",
            "localhost:9092",
            "--paused",
        ]
    )

    assert code == 0
    state = captured_state.get("state")
    assert state is not None
    assert state.control.transport.playing is False


def test_main_unknown_verb_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    """main(["mixer"]) with missing required args prints usage and exits non-zero."""
    code = main(["mixer"])
    # Argparse signals a usage error (code 2 from argparse or 1 from our wrapper)
    assert code != 0


# ---------------------------------------------------------------------------
# Consumer flag usage checks (exit 1, before the funnel)
# ---------------------------------------------------------------------------


def test_window_without_consumer_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--window without --consumer is a usage error."""
    emit_dir = tmp_path / "emit"
    config_path = tmp_path / "config.yaml"
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers=None,
        host="127.0.0.1",
        port=8765,
        cli_consumer=False,
        cli_windows=(60000,),
        cli_joins=(),
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "Usage error" in captured.err
    assert "window" in captured.err


def test_join_without_consumer_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--join without --consumer is a usage error."""
    emit_dir = tmp_path / "emit"
    config_path = tmp_path / "config.yaml"
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers=None,
        host="127.0.0.1",
        port=8765,
        cli_consumer=False,
        cli_windows=(),
        cli_joins=(("orders", "customers"),),
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "Usage error" in captured.err
    assert "join" in captured.err


def test_window_zero_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--window 0 is a usage error."""
    emit_dir = tmp_path / "emit"
    config_path = tmp_path / "config.yaml"
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers=None,
        host="127.0.0.1",
        port=8765,
        cli_consumer=True,
        cli_windows=(0,),
        cli_joins=(),
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "Usage error" in captured.err
    assert "window" in captured.err


def test_consumer_offset_invalid_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--consumer-offset with an invalid value is a usage error."""
    emit_dir = tmp_path / "emit"
    config_path = tmp_path / "config.yaml"
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers=None,
        host="127.0.0.1",
        port=8765,
        cli_consumer=True,
        cli_windows=(),
        cli_joins=(),
        cli_consumer_offset="beginning",
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "Usage error" in captured.err
    assert "consumer-offset" in captured.err


def test_join_bad_topic_exits_1_via_funnel(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A --join naming a topic absent from topic_set raises ExportError via funnel."""
    emit_dir = _build_minimal_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_stream_config(config_path)

    import fabulexa_forge.exporters.streaming.mixer.serve as serve_mod

    async def _noop_serve(**kwargs: Any) -> None:
        pass

    monkeypatch.setattr(serve_mod, "serve_mixer", _noop_serve)
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers="localhost:9092",
        host="127.0.0.1",
        port=8765,
        cli_consumer=True,
        cli_windows=(),
        cli_joins=(("no_such_topic", "also_absent"),),
    )

    assert code == 1
    captured = capsys.readouterr()
    assert "ERROR" in captured.err


# ---------------------------------------------------------------------------
# Consumer wiring: state.consumer and consumer_launch
# ---------------------------------------------------------------------------


def test_consumer_sets_state_and_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--consumer wires ConsumerRunState onto state and passes a ConsumerLaunch."""
    emit_dir = _build_minimal_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_stream_config(config_path)

    captured: dict[str, Any] = {}

    import fabulexa_forge.exporters.streaming.mixer.serve as serve_mod

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(serve_mod, "serve_mixer", _capture)
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers="localhost:9092",
        host="127.0.0.1",
        port=8765,
        cli_consumer=True,
        cli_windows=(60000,),
        cli_joins=(),
    )

    assert code == 0
    state = captured.get("state")
    assert state is not None
    assert state.consumer is not None
    consumer_launch = captured.get("consumer_launch")
    assert consumer_launch is not None
    assert consumer_launch.offset_reset == "earliest"
    # A fresh unique group id was generated
    assert consumer_launch.group_id


def test_consumer_explicit_group_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit --consumer-group is honored."""
    emit_dir = _build_minimal_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_stream_config(config_path)

    captured: dict[str, Any] = {}

    import fabulexa_forge.exporters.streaming.mixer.serve as serve_mod

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(serve_mod, "serve_mixer", _capture)
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers="localhost:9092",
        host="127.0.0.1",
        port=8765,
        cli_consumer=True,
        cli_windows=(),
        cli_joins=(),
        cli_consumer_group="my-group",
        cli_consumer_offset="latest",
    )

    assert code == 0
    consumer_launch = captured.get("consumer_launch")
    assert consumer_launch is not None
    assert consumer_launch.group_id == "my-group"
    assert consumer_launch.offset_reset == "latest"


def test_producer_only_state_consumer_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --consumer, state.consumer is None and consumer_launch is None."""
    emit_dir = _build_minimal_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_stream_config(config_path)

    captured: dict[str, Any] = {}

    import fabulexa_forge.exporters.streaming.mixer.serve as serve_mod

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(serve_mod, "serve_mixer", _capture)
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers="localhost:9092",
        host="127.0.0.1",
        port=8765,
    )

    assert code == 0
    state = captured.get("state")
    assert state is not None
    assert state.consumer is None
    assert captured.get("consumer_launch") is None


# ---------------------------------------------------------------------------
# _cmd_mixer: new flag parsing
# ---------------------------------------------------------------------------


def test_cmd_mixer_consumer_flags_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_mixer parses --consumer, --window, --join, --consumer-group, --consumer-offset."""
    emit_dir = _build_minimal_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_stream_config(config_path)

    captured: dict[str, Any] = {}

    import fabulexa_forge.exporters.streaming.mixer.serve as serve_mod

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(serve_mod, "serve_mixer", _capture)
    code = main(
        [
            "mixer",
            str(emit_dir),
            str(config_path),
            "--fmt",
            "jsonl",
            "--bootstrap-servers",
            "localhost:9092",
            "--consumer",
            "--window",
            "60000",
            "--consumer-group",
            "test-group",
            "--consumer-offset",
            "latest",
        ]
    )

    assert code == 0
    consumer_launch = captured.get("consumer_launch")
    assert consumer_launch is not None
    assert consumer_launch.group_id == "test-group"
    assert consumer_launch.offset_reset == "latest"


def test_cmd_mixer_join_malformed_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_cmd_mixer: malformed --join (not 'fact:dim') prints usage and exits 1."""
    code = main(
        [
            "mixer",
            "/no/emit",
            "/no/config",
            "--fmt",
            "jsonl",
            "--consumer",
            "--join",
            "no-colon",
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "Usage" in captured.err


def test_cmd_mixer_usage_string_contains_consumer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_cmd_mixer usage string mentions --consumer when a parse error occurs."""
    code = main(["mixer"])
    assert code != 0
    captured = capsys.readouterr()
    assert "consumer" in captured.err


# ---------------------------------------------------------------------------
# --join happy path: _parse_join_flag through main()
# ---------------------------------------------------------------------------


def test_parse_join_flag_happy_path() -> None:
    """_parse_join_flag splits 'fact:dim' into ('fact', 'dim')."""
    assert _parse_join_flag("orders:customers") == ("orders", "customers")


def test_main_join_happy_path_reaches_cmd_mixer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() parses each --join 'fact:dim' into a (fact, dim) pair, in order."""
    import fabulexa_forge.cli as cli_mod

    captured: dict[str, Any] = {}

    def _fake_cmd_mixer(*args: Any, **kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "cmd_mixer", _fake_cmd_mixer)
    code = main(
        [
            "mixer",
            "/no/emit",
            "/no/config",
            "--fmt",
            "jsonl",
            "--consumer",
            "--join",
            "orders:customers",
            "--join",
            "shipments:carriers",
        ]
    )

    assert code == 0
    assert captured["cli_joins"] == (
        ("orders", "customers"),
        ("shipments", "carriers"),
    )


# ---------------------------------------------------------------------------
# Serving-phase errors: the second (ReaderError, ExporterError) funnel
# ---------------------------------------------------------------------------


def test_serve_mixer_delivery_error_exits_1_via_second_funnel(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A KafkaDeliveryError raised mid-run by serve_mixer lands in the second
    funnel as exit 1 (setup succeeded; the failure surfaces after asyncio.run)."""
    emit_dir = _build_minimal_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_stream_config(config_path)

    import fabulexa_forge.exporters.streaming.mixer.serve as serve_mod

    async def _raise_delivery(**kwargs: Any) -> None:
        raise KafkaDeliveryError("mid-run delivery failure: broker unreachable")

    monkeypatch.setattr(serve_mod, "serve_mixer", _raise_delivery)
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers="localhost:9092",
        host="127.0.0.1",
        port=8765,
    )

    assert code == 1
    captured = capsys.readouterr()
    assert "ERROR" in captured.err
    assert "mid-run delivery failure" in captured.err


def test_serve_mixer_consume_error_exits_1_via_second_funnel(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A KafkaConsumeError from the consumer instrument's failure path lands in
    the second funnel as exit 1."""
    emit_dir = _build_minimal_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_stream_config(config_path)

    import fabulexa_forge.exporters.streaming.mixer.serve as serve_mod

    async def _raise_consume(**kwargs: Any) -> None:
        raise KafkaConsumeError("consumer poll failed: broker unreachable")

    monkeypatch.setattr(serve_mod, "serve_mixer", _raise_consume)
    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers="localhost:9092",
        host="127.0.0.1",
        port=8765,
        cli_consumer=True,
        cli_windows=(60000,),
        cli_joins=(),
    )

    assert code == 1
    captured = capsys.readouterr()
    assert "ERROR" in captured.err
    assert "consumer poll failed" in captured.err


# ---------------------------------------------------------------------------
# MixerExtraUnavailable: FastAPI not importable
# ---------------------------------------------------------------------------


def test_mixer_extra_unavailable_exits_1_with_install_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With FastAPI unimportable, the real serve_mixer raises
    MixerExtraUnavailable, which exits 1 with the install-the-extra message."""
    emit_dir = _build_minimal_emit(tmp_path)
    config_path = tmp_path / "config.yaml"
    _write_stream_config(config_path)

    # A None entry in sys.modules makes `import fastapi` raise ImportError,
    # simulating an absent `mixer` extra; serve_mixer's probe runs before any
    # Kafka connection is attempted, so no broker is needed.
    monkeypatch.setitem(sys.modules, "fastapi", None)

    code = cmd_mixer(
        emit_dir=emit_dir,
        config_path=config_path,
        fmt="jsonl",
        cli_base_date=None,
        cli_timezone=None,
        cli_speed=1.0,
        cli_playing=False,
        cli_tick_seconds=0.05,
        cli_bootstrap_servers="localhost:9092",
        host="127.0.0.1",
        port=8765,
    )

    assert code == 1
    captured = capsys.readouterr()
    assert "ERROR" in captured.err
    assert "fabulexa-export[mixer]" in captured.err
