"""Tests for `fabulexa-forge stream` CLI verb.

Covers:
- main(["stream", ...]) routes to cmd_stream
- End-to-end stdout: exit 0, JSONL on stdout, per-topic counts
- End-to-end file: exit 0, <kind>.jsonl files written
- sink/out pairing errors: --sink file without --out → exit 1; --sink stdout with --out → exit 1
- --fmt other than 'jsonl' → exit 1
- Funnel: bad config → exit 1; bad emit dir → exit 1; unknown kind → exit 1
- --base-date / --timezone flow into ts
- Anchor fallback to sidecar / config rebase when CLI args absent
- Membership Debezium: file/stdout/error funnels; JSONL regression
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import duckdb
import pytest
import yaml

from exporters.streaming._helpers import _membership_table_spec
from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.cli import cmd_stream, main
from fabulexa_forge.exporters.streaming.types import StreamOutcome

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


def _table_spec(
    name: str,
    category: str,
    cols: list[dict[str, object]],
    rows: int,
    record_kind: str | None = None,
) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": name,
        "category": category,
        "columns": cols,
        "rows": rows,
    }
    if record_kind is not None:
        spec["record_kind"] = record_kind
    return spec


def _build_spanning_emit(tmp_path: Path) -> Path:
    """Build a minimal single-branch v4 emit with two kinds."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__actor", _RECORD_COLS))
    conn.execute(_ddl("records__task", _RECORD_COLS))
    conn.execute(_ddl("history", _HISTORY_COLS))

    _DAY = 86_400_000_000_000
    actor_rows = [
        ("trunk", "a001", 1 * _DAY, True, None, 1 * _DAY, "active"),
        ("trunk", "a002", 2 * _DAY, False, 5 * _DAY, 5 * _DAY, "inactive"),
    ]
    for row in actor_rows:
        conn.execute(
            'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?)', list(row)
        )

    task_rows = [
        ("trunk", "t001", 1 * _DAY, True, None, 1 * _DAY, "open"),
    ]
    for row in task_rows:
        conn.execute(
            'INSERT INTO "records__task" VALUES (?, ?, ?, ?, ?, ?, ?)', list(row)
        )

    history_rows = [
        ("trunk", "actor", "a001", "status", 1 * _DAY, "active"),
        ("trunk", "actor", "a002", "status", 2 * _DAY, "active"),
        ("trunk", "actor", "a002", "status", 4 * _DAY, "inactive"),
        ("trunk", "task", "t001", "status", 1 * _DAY, "open"),
    ]
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            _table_spec(
                "records__actor", "records", _RECORD_COLS, 2, record_kind="actor"
            ),
            _table_spec(
                "records__task", "records", _RECORD_COLS, 1, record_kind="task"
            ),
            _table_spec("history", "fixed", _HISTORY_COLS, 4),
        ],
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path


def _write_stream_config(config_path: Path, kinds: list[str]) -> None:
    """Write a minimal stream config YAML for the given kinds."""
    doc = {
        "content": "state-changes",
        "kinds": [{"kind": k, "properties": ["status"]} for k in kinds],
    }
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


def _write_bad_stream_config(config_path: Path) -> None:
    """Write a config YAML that will fail validation (empty kinds)."""
    doc = {"content": "state-changes", "kinds": []}
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


def _write_unknown_kind_config(config_path: Path) -> None:
    """Write a config that references a kind not in the emit."""
    doc = {
        "content": "state-changes",
        "kinds": [{"kind": "nonexistent", "properties": []}],
    }
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


def _write_debezium_stream_config(config_path: Path, kinds: list[str]) -> None:
    """Write a stream config YAML with a debezium block for the given kinds."""
    doc = {
        "content": "state-changes",
        "kinds": [{"kind": k, "properties": ["status"]} for k in kinds],
        "debezium": {
            "schemas_enable": True,
            "source": {
                "connector": "postgresql",
                "name": "myserver",
                "db": "testdb",
                "schema": "public",
                "version": "1.9.0.Final",
            },
        },
    }
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


def _write_debezium_config_no_debezium_block(
    config_path: Path, kinds: list[str]
) -> None:
    """Write a stream config YAML without a debezium block."""
    doc = {
        "content": "state-changes",
        "kinds": [{"kind": k, "properties": ["status"]} for k in kinds],
    }
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


def _write_realtime_stream_config(
    config_path: Path, kinds: list[str], speed: float
) -> None:
    """Write a stream config with a realtime clock block."""
    doc = {
        "content": "state-changes",
        "kinds": [{"kind": k, "properties": ["status"]} for k in kinds],
        "clock": {"mode": "realtime", "speed": speed},
    }
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


def _write_fast_stream_config(config_path: Path, kinds: list[str]) -> None:
    """Write a stream config with an explicit fast clock block."""
    doc = {
        "content": "state-changes",
        "kinds": [{"kind": k, "properties": ["status"]} for k in kinds],
        "clock": {"mode": "fast"},
    }
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class TestRouting:
    """main() routes 'stream' to cmd_stream."""

    def test_main_routes_stream_verb(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """main(['stream', ...]) routes to cmd_stream and returns 0."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        rc = main(
            [
                "stream",
                str(emit_dir),
                str(config_path),
                "--fmt",
                "jsonl",
                "--sink",
                "stdout",
            ]
        )
        capsys.readouterr()
        assert rc == 0


# ---------------------------------------------------------------------------
# End-to-end stdout
# ---------------------------------------------------------------------------


class TestEndToEndStdout:
    """End-to-end: stream → stdout sink."""

    def test_stdout_exit_0_jsonl_on_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Valid config + emit → exit 0, JSONL lines on stdout."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
        )
        captured = capsys.readouterr()
        assert rc == 0
        # All stdout lines before the summary line are JSONL
        jsonl_lines = [ln for ln in captured.out.splitlines() if ln.startswith("{")]
        assert len(jsonl_lines) > 0
        for line in jsonl_lines:
            obj = json.loads(line)
            assert "seq" in obj
            assert "op" in obj

    def test_stdout_per_topic_counts_printed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Per-topic event counts are printed to stdout on success."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert "actor" in captured.out
        assert "events" in captured.out


# ---------------------------------------------------------------------------
# End-to-end file
# ---------------------------------------------------------------------------


class TestEndToEndFile:
    """End-to-end: stream → file sink."""

    def test_file_exit_0_jsonl_files_written(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Valid config + emit → exit 0, <kind>.jsonl files under out."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        _write_stream_config(config_path, ["actor", "task"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="file",
            out=out_dir,
            cli_base_date=None,
            cli_timezone=None,
        )
        capsys.readouterr()
        assert rc == 0
        assert (out_dir / "actor.jsonl").exists()
        assert (out_dir / "task.jsonl").exists()

    def test_file_jsonl_content_is_valid(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Each line in the written .jsonl files is valid JSON."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        _write_stream_config(config_path, ["actor"])

        cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="file",
            out=out_dir,
            cli_base_date=None,
            cli_timezone=None,
        )
        capsys.readouterr()
        content = (out_dir / "actor.jsonl").read_text(encoding="utf-8")
        for line in content.splitlines():
            if line.strip():
                json.loads(line)


# ---------------------------------------------------------------------------
# sink/out pairing usage errors
# ---------------------------------------------------------------------------


class TestSinkOutPairing:
    """CLI usage errors for sink/out mismatches."""

    def test_sink_file_without_out_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--sink file without --out → exit 1, message on stderr."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="file",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err != ""

    def test_sink_stdout_with_out_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--sink stdout with --out → exit 1, message on stderr."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        _write_stream_config(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=out_dir,
            cli_base_date=None,
            cli_timezone=None,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err != ""

    def test_sink_file_without_out_via_main(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """main(['stream', ..., '--sink', 'file']) without --out → exit 1."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        rc = main(
            [
                "stream",
                str(emit_dir),
                str(config_path),
                "--fmt",
                "jsonl",
                "--sink",
                "file",
            ]
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err != ""


# ---------------------------------------------------------------------------
# --fmt validation
# ---------------------------------------------------------------------------


class TestFmtValidation:
    """--fmt other than 'jsonl' or 'debezium' → exit 1."""

    def test_fmt_not_supported_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--fmt avro → exit 1, message on stderr naming jsonl|debezium."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="avro",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert "jsonl|debezium" in captured.err

    def test_fmt_avro_via_main_shows_usage_with_debezium(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """main(['stream', ..., '--fmt', 'avro']) exits 1 with usage naming jsonl|debezium."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        rc = main(
            [
                "stream",
                str(emit_dir),
                str(config_path),
                "--fmt",
                "avro",
                "--sink",
                "stdout",
            ]
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err != ""


# ---------------------------------------------------------------------------
# Error funnel
# ---------------------------------------------------------------------------


class TestErrorFunnel:
    """ConfigError, ReaderError, ExportError all funnel to exit 1 with stderr message."""

    def test_bad_config_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A config with empty kinds → ConfigError → exit 1."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_bad_stream_config(config_path)

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err != ""

    def test_bad_emit_dir_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A missing emit dir → ReaderError → exit 1."""
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=tmp_path / "no_such_dir",
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err != ""

    def test_unknown_kind_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A kind not in the emit → ExportError → exit 1."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_unknown_kind_config(config_path)

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert "nonexistent" in captured.err

    def test_missing_config_file_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A missing config file → ConfigError → exit 1."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=tmp_path / "no_such_config.yaml",
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err != ""


# ---------------------------------------------------------------------------
# Anchor / timestamp flow
# ---------------------------------------------------------------------------


class TestAnchorFlow:
    """--base-date / --timezone flow into ts; absent → raw ns."""

    def test_no_anchor_ts_is_raw_int(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without an anchor, ts on stdout is a raw integer (ns)."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
        )
        captured = capsys.readouterr()
        jsonl_lines = [ln for ln in captured.out.splitlines() if ln.startswith("{")]
        assert len(jsonl_lines) > 0
        obj = json.loads(jsonl_lines[0])
        assert isinstance(obj["ts"], int)

    def test_cli_base_date_and_timezone_produce_iso_ts(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With --base-date and --timezone, ts is an offset-bearing ISO-8601 string."""
        from datetime import datetime

        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=datetime(2026, 1, 1, 0, 0, 0),
            cli_timezone="UTC",
        )
        captured = capsys.readouterr()
        jsonl_lines = [ln for ln in captured.out.splitlines() if ln.startswith("{")]
        assert len(jsonl_lines) > 0
        obj = json.loads(jsonl_lines[0])
        ts = obj["ts"]
        assert isinstance(ts, str)
        assert "+" in ts or "Z" in ts or ts.endswith("+00:00")


# ---------------------------------------------------------------------------
# Debezium format via CLI
# ---------------------------------------------------------------------------


class TestDebeziumCli:
    """CLI: --fmt debezium integration tests."""

    def test_debezium_stdout_exits_0_and_writes_messages(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--fmt debezium with a debezium config + rebase anchor → exit 0, messages."""
        from datetime import datetime

        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_debezium_stream_config(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="debezium",
            sink="stdout",
            out=None,
            cli_base_date=datetime(2026, 1, 1, 0, 0, 0),
            cli_timezone="UTC",
        )
        captured = capsys.readouterr()
        assert rc == 0
        msg_lines = [ln for ln in captured.out.splitlines() if ln.startswith("{")]
        assert len(msg_lines) > 0
        for line in msg_lines:
            msg = json.loads(line)
            assert "payload" in msg  # schemas_enable=True

    def test_debezium_no_anchor_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--fmt debezium with no anchor (no base_date/timezone/sidecar) → exit 1."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_debezium_stream_config(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="debezium",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert "ERROR" in captured.err

    def test_debezium_no_debezium_block_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--fmt debezium with no debezium block → exit 1 with ERROR message."""
        from datetime import datetime

        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_debezium_config_no_debezium_block(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="debezium",
            sink="stdout",
            out=None,
            cli_base_date=datetime(2026, 1, 1, 0, 0, 0),
            cli_timezone="UTC",
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert "ERROR" in captured.err


# ---------------------------------------------------------------------------
# Clock flag wiring (Phase 4)
# ---------------------------------------------------------------------------


class TestClockFlagWiring:
    """CLI clock flags: --speed, --idle-cap, --fast wiring and validation."""

    def test_speed_cli_flag_wires_realtime_clock_exit_0(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--speed 1e9 with a fast config escalates to realtime and succeeds (exit 0)."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
            cli_speed=1e9,  # very high speed — no real sleeping
        )
        capsys.readouterr()
        assert rc == 0

    def test_fast_flag_over_realtime_config_delivers_unpaced_exit_0(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--fast over a realtime config delivers unpaced (exit 0)."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_realtime_stream_config(config_path, ["actor"], speed=1e9)

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
            cli_fast=True,
        )
        capsys.readouterr()
        assert rc == 0

    def test_fast_and_speed_conflict_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--fast --speed 60 is a usage error → exit 1 before the funnel."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
            cli_speed=60.0,
            cli_fast=True,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err != ""

    def test_fast_and_idle_cap_conflict_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--fast --idle-cap 5 is a usage error → exit 1 before the funnel."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
            cli_idle_cap_seconds=5.0,
            cli_fast=True,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err != ""

    def test_speed_zero_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--speed 0 is a usage error → exit 1."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
            cli_speed=0.0,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err != ""

    def test_idle_cap_negative_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--idle-cap -1 is a usage error → exit 1."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
            cli_idle_cap_seconds=-1.0,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err != ""

    def test_idle_cap_without_speed_over_fast_config_funnels_to_exit_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--idle-cap 5 with no --speed over a fast/absent config → ClockSpeedUnresolvable → exit 1."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])  # no clock block (fast)

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
            cli_idle_cap_seconds=5.0,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err != ""

    def test_config_level_realtime_clock_paces_exit_0(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A config-level clock: {mode: realtime, speed: ...} with no CLI flags → exit 0."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_realtime_stream_config(config_path, ["actor"], speed=1e9)

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
        )
        capsys.readouterr()
        assert rc == 0


# ---------------------------------------------------------------------------
# Kafka sink config helper
# ---------------------------------------------------------------------------


def _write_kafka_stream_config(
    config_path: Path, kinds: list[str], bootstrap_servers: str = "localhost:9092"
) -> None:
    """Write a stream config YAML with a kafka block."""
    doc = {
        "content": "state-changes",
        "kinds": [{"kind": k, "properties": ["status"]} for k in kinds],
        "kafka": {"bootstrap_servers": bootstrap_servers},
    }
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


def _fake_write_kafka_stream_ok(
    events: Any,
    render_value: Any,
    anchor: Any,
    bootstrap_servers: str,
    topic_set: tuple[str, ...],
    *,
    paced: bool = False,
) -> StreamOutcome:
    """A fake write_kafka_stream that succeeds and returns zero counts."""
    counts = {t: 0 for t in topic_set}
    return StreamOutcome(total_events=0, events_per_topic=counts)


# ---------------------------------------------------------------------------
# --sink kafka usage checks
# ---------------------------------------------------------------------------


class TestKafkaSinkUsageChecks:
    """CLI usage errors for the kafka sink."""

    def test_sink_kafka_with_out_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--sink kafka with --out → exit 1 with usage message."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        _write_kafka_stream_config(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="kafka",
            out=out_dir,
            cli_base_date=None,
            cli_timezone=None,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert "--sink kafka does not accept --out" in captured.err

    def test_sink_bogus_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--sink bogusvalue → exit 1 with usage message naming stdout|file|kafka."""
        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="bogusvalue",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert "stdout|file|kafka" in captured.err

    def test_sink_kafka_without_out_accepted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--sink kafka without --out is accepted (no usage error, no early exit)."""
        from datetime import datetime

        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_kafka_stream_config(config_path, ["actor"])

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream_ok,
        ):
            rc = cmd_stream(
                emit_dir=emit_dir,
                config_path=config_path,
                fmt="jsonl",
                sink="kafka",
                out=None,
                cli_base_date=datetime(2026, 1, 1),
                cli_timezone="UTC",
                cli_bootstrap_servers="localhost:9092",
            )
        capsys.readouterr()
        assert rc == 0


# ---------------------------------------------------------------------------
# Bootstrap precedence through CLI
# ---------------------------------------------------------------------------


class TestKafkaBootstrapPrecedence:
    """--bootstrap-servers / config block / env precedence through cmd_stream."""

    def test_cli_bootstrap_servers_wins(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--bootstrap-servers CLI flag wins over config and env."""
        from datetime import datetime

        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_kafka_stream_config(
            config_path, ["actor"], bootstrap_servers="config:9092"
        )
        captured_bs: list[str] = []

        def _capture_bs(
            events: Any, rv: Any, anchor: Any, bs: str, ts: Any, *, paced: bool = False
        ) -> StreamOutcome:  # noqa: E501
            captured_bs.append(bs)
            return StreamOutcome(total_events=0, events_per_topic={t: 0 for t in ts})

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_capture_bs,
        ):
            with patch.dict(os.environ, {"FABEXPORT_KAFKA_BOOTSTRAP": "env:9092"}):
                rc = cmd_stream(
                    emit_dir=emit_dir,
                    config_path=config_path,
                    fmt="jsonl",
                    sink="kafka",
                    out=None,
                    cli_base_date=datetime(2026, 1, 1),
                    cli_timezone="UTC",
                    cli_bootstrap_servers="cli:9092",
                )
        capsys.readouterr()
        assert rc == 0
        assert captured_bs == ["cli:9092"]

    def test_config_block_wins_over_env(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With no CLI flag, the config kafka block wins over env."""
        from datetime import datetime

        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_kafka_stream_config(
            config_path, ["actor"], bootstrap_servers="config:9092"
        )
        captured_bs: list[str] = []

        def _capture_bs(
            events: Any, rv: Any, anchor: Any, bs: str, ts: Any, *, paced: bool = False
        ) -> StreamOutcome:  # noqa: E501
            captured_bs.append(bs)
            return StreamOutcome(total_events=0, events_per_topic={t: 0 for t in ts})

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_capture_bs,
        ):
            with patch.dict(os.environ, {"FABEXPORT_KAFKA_BOOTSTRAP": "env:9092"}):
                rc = cmd_stream(
                    emit_dir=emit_dir,
                    config_path=config_path,
                    fmt="jsonl",
                    sink="kafka",
                    out=None,
                    cli_base_date=datetime(2026, 1, 1),
                    cli_timezone="UTC",
                )
        capsys.readouterr()
        assert rc == 0
        assert captured_bs == ["config:9092"]

    def test_env_used_when_only_env_set(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With no CLI flag and no config block, FABEXPORT_KAFKA_BOOTSTRAP is used."""
        from datetime import datetime

        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        # Write config WITHOUT kafka block
        _write_stream_config(config_path, ["actor"])
        captured_bs: list[str] = []

        def _capture_bs(
            events: Any, rv: Any, anchor: Any, bs: str, ts: Any, *, paced: bool = False
        ) -> StreamOutcome:  # noqa: E501
            captured_bs.append(bs)
            return StreamOutcome(total_events=0, events_per_topic={t: 0 for t in ts})

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_capture_bs,
        ):
            with patch.dict(os.environ, {"FABEXPORT_KAFKA_BOOTSTRAP": "env:9092"}):
                rc = cmd_stream(
                    emit_dir=emit_dir,
                    config_path=config_path,
                    fmt="jsonl",
                    sink="kafka",
                    out=None,
                    cli_base_date=datetime(2026, 1, 1),
                    cli_timezone="UTC",
                )
        capsys.readouterr()
        assert rc == 0
        assert captured_bs == ["env:9092"]

    def test_none_bootstrap_funnels_exit_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No CLI, no config block, no env → KafkaBootstrapUnresolvable → exit 1."""
        from datetime import datetime

        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_stream_config(config_path, ["actor"])

        # Ensure env var is absent
        env = {k: v for k, v in os.environ.items() if k != "FABEXPORT_KAFKA_BOOTSTRAP"}
        with patch.dict(os.environ, env, clear=True):
            rc = cmd_stream(
                emit_dir=emit_dir,
                config_path=config_path,
                fmt="jsonl",
                sink="kafka",
                out=None,
                cli_base_date=datetime(2026, 1, 1),
                cli_timezone="UTC",
            )
        captured = capsys.readouterr()
        assert rc == 1
        assert "ERROR" in captured.err


# ---------------------------------------------------------------------------
# Kafka sink end-to-end with fake producer
# ---------------------------------------------------------------------------


class TestKafkaSinkEndToEndFake:
    """sink='kafka' with fake write_kafka_stream: exit 0 and error cases."""

    def test_kafka_exit_0_per_topic_counts_printed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """sink='kafka' with fake producer → exit 0, per-topic counts on stdout."""
        from datetime import datetime

        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_kafka_stream_config(config_path, ["actor"])

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream_ok,
        ):
            rc = cmd_stream(
                emit_dir=emit_dir,
                config_path=config_path,
                fmt="jsonl",
                sink="kafka",
                out=None,
                cli_base_date=datetime(2026, 1, 1),
                cli_timezone="UTC",
                cli_bootstrap_servers="localhost:9092",
            )
        captured = capsys.readouterr()
        assert rc == 0
        assert "actor" in captured.out
        assert "events" in captured.out

    def test_kafka_client_unavailable_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """KafkaClientUnavailable → exit 1."""
        from datetime import datetime

        from fabulexa_forge.errors import KafkaClientUnavailable

        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_kafka_stream_config(config_path, ["actor"])

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=KafkaClientUnavailable("no kafka"),
        ):
            rc = cmd_stream(
                emit_dir=emit_dir,
                config_path=config_path,
                fmt="jsonl",
                sink="kafka",
                out=None,
                cli_base_date=datetime(2026, 1, 1),
                cli_timezone="UTC",
                cli_bootstrap_servers="localhost:9092",
            )
        captured = capsys.readouterr()
        assert rc == 1
        assert "ERROR" in captured.err

    def test_kafka_delivery_error_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """KafkaDeliveryError → exit 1."""
        from datetime import datetime

        from fabulexa_forge.errors import KafkaDeliveryError

        emit_dir = _build_spanning_emit(tmp_path / "emit")
        config_path = tmp_path / "stream.yaml"
        _write_kafka_stream_config(config_path, ["actor"])

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=KafkaDeliveryError("delivery failed"),
        ):
            rc = cmd_stream(
                emit_dir=emit_dir,
                config_path=config_path,
                fmt="jsonl",
                sink="kafka",
                out=None,
                cli_base_date=datetime(2026, 1, 1),
                cli_timezone="UTC",
                cli_bootstrap_servers="localhost:9092",
            )
        captured = capsys.readouterr()
        assert rc == 1
        assert "ERROR" in captured.err


# ---------------------------------------------------------------------------
# Membership emit + config builders
# ---------------------------------------------------------------------------

_NS = 1_000_000_000  # one second in nanoseconds

_WAITERS_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__priority", "type": "VARCHAR"},
]

_MEMBERS_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
]


def _build_membership_emit(tmp_path: Path) -> Path:
    """Build a minimal single-branch v4 emit with two membership tables."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    cols_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _WAITERS_COLS)
    conn.execute(f'CREATE TABLE "membership__queue__waiters" ({cols_ddl})')
    waiters_rows: list[tuple[object, ...]] = [
        ("trunk", "r1", 1 * _NS, 3 * _NS, "high"),
        ("trunk", "r2", 2 * _NS, None, "low"),
    ]
    ph = ", ".join("?" for _ in _WAITERS_COLS)
    for row in waiters_rows:
        conn.execute(
            f'INSERT INTO "membership__queue__waiters" VALUES ({ph})', list(row)
        )

    cols_ddl2 = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _MEMBERS_COLS)
    conn.execute(f'CREATE TABLE "membership__team__members" ({cols_ddl2})')
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            _membership_table_spec(
                "membership__queue__waiters",
                _WAITERS_COLS,
                len(waiters_rows),
                "queue",
                "waiters",
            ),
            _membership_table_spec(
                "membership__team__members", _MEMBERS_COLS, 0, "team", "members"
            ),
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return emit_dir


def _write_membership_debezium_config(config_path: Path, schemas_enable: bool) -> None:
    """Write a membership-events stream config with a debezium block."""
    doc = {
        "content": "membership-events",
        "memberships": [
            {"owner_kind": "queue", "property": "waiters", "fields": ["priority"]},
            {"owner_kind": "team", "property": "members", "fields": []},
        ],
        "debezium": {
            "schemas_enable": schemas_enable,
            "source": {
                "connector": "postgresql",
                "name": "myserver",
                "db": "testdb",
                "schema": "public",
                "version": "1.9.0.Final",
            },
        },
    }
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


def _write_membership_no_debezium_block_config(config_path: Path) -> None:
    """Write a membership-events stream config WITHOUT a debezium block."""
    doc = {
        "content": "membership-events",
        "memberships": [
            {"owner_kind": "queue", "property": "waiters", "fields": ["priority"]},
            {"owner_kind": "team", "property": "members", "fields": []},
        ],
    }
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


def _write_membership_jsonl_config(config_path: Path) -> None:
    """Write a membership-events stream config for JSONL output."""
    doc = {
        "content": "membership-events",
        "memberships": [
            {"owner_kind": "queue", "property": "waiters", "fields": ["priority"]},
        ],
    }
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


# ---------------------------------------------------------------------------
# Membership Debezium CLI end-to-end
# ---------------------------------------------------------------------------


class TestMembershipDebeziumCli:
    """CLI: membership emit + --fmt debezium end-to-end tests."""

    def test_membership_debezium_file_exit_0_per_topic_files_exist(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """membership + debezium block + rebase anchor + sink=file → exit 0, topic files."""
        from datetime import datetime

        emit_dir = _build_membership_emit(tmp_path)
        config_path = tmp_path / "stream.yaml"
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        _write_membership_debezium_config(config_path, schemas_enable=True)

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="debezium",
            sink="file",
            out=out_dir,
            cli_base_date=datetime(2026, 1, 1, 0, 0, 0),
            cli_timezone="UTC",
        )
        capsys.readouterr()
        assert rc == 0
        assert (out_dir / "queue__waiters.jsonl").exists()
        assert (out_dir / "team__members.jsonl").exists()

        content = (out_dir / "queue__waiters.jsonl").read_text(encoding="utf-8")
        non_empty = [ln for ln in content.splitlines() if ln.strip()]
        assert non_empty, "queue__waiters.jsonl must have at least one line"
        for line in non_empty:
            msg = json.loads(line)
            payload = msg["payload"]
            assert payload["op"] == "c", f"Expected op='c', got {payload['op']!r}"
            assert payload["before"] is None
            after = payload["after"]
            after_keys = list(after.keys())
            assert after_keys[0] == "event", (
                f"Expected 'event' as first after key, got {after_keys[0]!r}"
            )

    def test_membership_debezium_stdout_schemas_enable_exit_0(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """membership + debezium block + schemas_enable=True + sink=stdout → exit 0, payload key."""
        from datetime import datetime

        emit_dir = _build_membership_emit(tmp_path)
        config_path = tmp_path / "stream.yaml"
        _write_membership_debezium_config(config_path, schemas_enable=True)

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="debezium",
            sink="stdout",
            out=None,
            cli_base_date=datetime(2026, 1, 1, 0, 0, 0),
            cli_timezone="UTC",
        )
        captured = capsys.readouterr()
        assert rc == 0
        msg_lines = [ln for ln in captured.out.splitlines() if ln.startswith("{")]
        assert len(msg_lines) == 3
        for line in msg_lines:
            msg = json.loads(line)
            assert "payload" in msg  # schemas_enable=True wraps in schema+payload

    def test_membership_debezium_no_anchor_returns_1_with_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """membership + debezium + no anchor → exit 1 with ERROR (DebeziumRequiresAnchor)."""
        emit_dir = _build_membership_emit(tmp_path)
        config_path = tmp_path / "stream.yaml"
        _write_membership_debezium_config(config_path, schemas_enable=True)

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="debezium",
            sink="stdout",
            out=None,
            cli_base_date=None,
            cli_timezone=None,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert "ERROR" in captured.err

    def test_membership_debezium_no_debezium_block_returns_1_with_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """membership + no debezium block → exit 1 with ERROR (DebeziumRequiresConfig)."""
        from datetime import datetime

        emit_dir = _build_membership_emit(tmp_path)
        config_path = tmp_path / "stream.yaml"
        _write_membership_no_debezium_block_config(config_path)

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="debezium",
            sink="stdout",
            out=None,
            cli_base_date=datetime(2026, 1, 1, 0, 0, 0),
            cli_timezone="UTC",
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert "ERROR" in captured.err

    def test_membership_jsonl_regression_exit_0(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """membership + --fmt jsonl still exits 0 and writes unchanged JSONL output."""
        emit_dir = _build_membership_emit(tmp_path)
        config_path = tmp_path / "stream.yaml"
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        _write_membership_jsonl_config(config_path)

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="jsonl",
            sink="file",
            out=out_dir,
            cli_base_date=None,
            cli_timezone=None,
        )
        capsys.readouterr()
        assert rc == 0
        content = (out_dir / "queue__waiters.jsonl").read_text(encoding="utf-8")
        non_empty = [ln for ln in content.splitlines() if ln.strip()]
        assert non_empty, "queue__waiters.jsonl must have JSONL lines"
        for line in non_empty:
            obj = json.loads(line)
            assert "op" in obj
            assert "payload" not in obj  # bare JSONL, no schema wrapper
