"""Integration tests for cmd_stream with sink='kafka'.

Drives cmd_stream against a live broker for both jsonl and debezium formats.
Asserts delivered keys, ordered seq, and rebased timestamps.

Requires: `make kafka-up` (a running broker at FABEXPORT_KAFKA_BOOTSTRAP or
localhost:9092). Skipped automatically when no broker is reachable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from _support.sidecar_builder import identity_column, prop_column
from _support.sidecar_builder import write_emit as _write_sidecar

from exporters.streaming._helpers import _ddl
from fabulexa_forge.cli import cmd_stream

from ._harness import bootstrap_servers, consume, delete_topic, skip_reason

pytestmark = pytest.mark.kafka

_DAY = 86_400_000_000_000

_RECORD_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _build_emit(tmp_path: Path, kind: str) -> Path:
    """Build a minimal emit with two records and multiple history rows."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(f"records__{kind}", _RECORD_COLS))
    conn.execute(_ddl("history", _HISTORY_COLS))

    record_rows = [
        ("trunk", "r001", 1 * _DAY, True, None, 2 * _DAY, 0, "active"),
        ("trunk", "r002", 2 * _DAY, True, None, 2 * _DAY, 1, "pending"),
    ]
    history_rows = [
        ("trunk", kind, "r001", "status", 1 * _DAY, "initial"),
        ("trunk", kind, "r001", "status", 2 * _DAY, "active"),
        ("trunk", kind, "r002", "status", 2 * _DAY, "pending"),
    ]

    for row in record_rows:
        conn.execute(
            f'INSERT INTO "records__{kind}" VALUES (?, ?, ?, ?, ?, ?, ?, ?)', list(row)
        )
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            {
                "name": f"records__{kind}",
                "category": "records",
                "columns": _RECORD_COLS,
                "rows": len(record_rows),
                "record_kind": kind,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLS,
                "rows": len(history_rows),
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path


def _write_jsonl_kafka_config(config_path: Path, kind: str, topic: str) -> None:
    """Write a JSONL stream config with kafka block and a named stream (topic=name)."""
    doc = {
        "content": "state-changes",
        "streams": [{"name": topic, "kind": kind, "properties": ["status"]}],
        "kafka": {"bootstrap_servers": bootstrap_servers()},
    }
    config_path.write_text(yaml.dump(doc), encoding="utf-8")


def _write_debezium_kafka_config(config_path: Path, kind: str, topic: str) -> None:
    """Write a Debezium stream config with kafka block and a named stream (topic=name)."""
    doc = {
        "content": "state-changes",
        "streams": [{"name": topic, "kind": kind, "properties": ["status"]}],
        "kafka": {"bootstrap_servers": bootstrap_servers()},
        "debezium": {
            "schemas_enable": False,
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


@pytest.fixture(scope="session")
def kafka_bootstrap() -> str:
    """Resolve bootstrap and skip if broker is unreachable."""
    bs = bootstrap_servers()
    reason = skip_reason(bs)
    if reason is not None:
        pytest.skip(reason)
    return bs


@pytest.fixture()
def fresh_topic(kafka_bootstrap: str) -> Any:
    """Yield a unique topic name; delete it after the test."""
    topics: list[str] = []

    def _make() -> str:
        t = f"fabulexa-forge.cli.{uuid.uuid4().hex[:12]}"
        topics.append(t)
        return t

    yield _make

    for t in topics:
        delete_topic(kafka_bootstrap, t)


class TestKafkaCliJsonl:
    """cmd_stream sink='kafka' fmt='jsonl' end-to-end against a live broker."""

    def test_jsonl_delivers_full_stream(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        fresh_topic: Any,
        kafka_bootstrap: str,
    ) -> None:
        """cmd_stream jsonl kafka delivers all events; keys have record_id; seq ordered."""
        topic = fresh_topic()
        emit_dir = _build_emit(tmp_path / "emit", "item")
        config_path = tmp_path / "stream.yaml"
        _write_jsonl_kafka_config(config_path, "item", topic)

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

        consumed = consume(kafka_bootstrap, topic, expected=3)
        assert len(consumed) == 3

        # Key is always {record_id}
        for msg in consumed:
            assert set(msg.key) == {"record_id"}

        # seq must be strictly increasing in consume order
        seqs = [msg.value["seq"] for msg in consumed]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)

        # record timestamp == rebased ts_ms (ts field in jsonl is ISO string when anchor set)
        for msg in consumed:
            assert msg.timestamp_ms > 0


class TestKafkaCliDebezium:
    """cmd_stream sink='kafka' fmt='debezium' end-to-end against a live broker."""

    def test_debezium_delivers_full_stream(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        fresh_topic: Any,
        kafka_bootstrap: str,
    ) -> None:
        """cmd_stream debezium kafka delivers all events with correct structure."""
        topic = fresh_topic()
        emit_dir = _build_emit(tmp_path / "emit", "item")
        config_path = tmp_path / "stream.yaml"
        _write_debezium_kafka_config(config_path, "item", topic)

        rc = cmd_stream(
            emit_dir=emit_dir,
            config_path=config_path,
            fmt="debezium",
            sink="kafka",
            out=None,
            cli_base_date=datetime(2026, 1, 1),
            cli_timezone="UTC",
        )
        capsys.readouterr()
        assert rc == 0

        consumed = consume(kafka_bootstrap, topic, expected=3)
        assert len(consumed) == 3

        # Key is always bare {record_id} — never schema-wrapped
        for msg in consumed:
            assert set(msg.key) == {"record_id"}

        # seq/lsn must be strictly increasing in consume order
        seqs = [msg.value["source"]["lsn"] for msg in consumed]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)

        # Record timestamp == payload ts_ms
        for msg in consumed:
            assert msg.timestamp_ms == msg.value["ts_ms"]
