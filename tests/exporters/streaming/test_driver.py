"""Tests for stream_export in driver.py.

Covers the ExportRuntimeError precondition raises, the ExportError business
rules (DebeziumRequiresConfig, DebeziumRequiresAnchor), the debezium dispatch
path (schemas_enable true/false, file/stdout), the empty-file creation
path for a zero-event kind, paced vs unpaced byte-identity, and membership-events
end-to-end (JSONL file sink and Debezium stdout/file/kafka).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import duckdb
import pytest
from _support.sidecar_builder import prop_column, write_emit

from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import (
    DebeziumConfig,
    DebeziumSourceIdentity,
    MembershipSelection,
    RoutingConfig,
    StreamConfig,
    StreamKindSelection,
)
from fabulexa_forge.errors import (
    ExportError,
    ExportRuntimeError,
)
from fabulexa_forge.exporters.streaming.driver import (
    build_kafka_render_value,
    stream_export,
)
from fabulexa_forge.exporters.streaming.pacer import ResolvedClock
from fabulexa_forge.exporters.streaming.types import StreamEvent, StreamOutcome
from fabulexa_forge.reader.emit import open_emit

from ._helpers import _ddl, _membership_table_spec, make_anchor

# ---------------------------------------------------------------------------
# Column / sidecar helpers (minimal, mirrors test_engine.py patterns)
# ---------------------------------------------------------------------------

_RECORD_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
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


def _build_emit(
    tmp_path: Path,
    kind: str,
    record_rows: list[tuple[Any, ...]],
    history_rows: list[tuple[Any, ...]],
) -> Path:
    """Build a minimal v5 emit with one kind."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(f"records__{kind}", _RECORD_COLS))
    conn.execute(_ddl("history", _HISTORY_COLS))

    ph = ", ".join("?" for _ in _RECORD_COLS)
    for row in record_rows:
        conn.execute(f'INSERT INTO "records__{kind}" VALUES ({ph})', list(row))
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                f"records__{kind}",
                "records",
                _RECORD_COLS,
                len(record_rows),
                record_kind=kind,
            ),
            _table_spec("history", "fixed", _HISTORY_COLS, len(history_rows)),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path


def _make_config(kind: str, properties: list[str] | None = None) -> StreamConfig:
    """Build a minimal StreamConfig for one kind."""
    return StreamConfig(
        content="state-changes",
        kinds=[StreamKindSelection(kind=kind, properties=properties or [])],
    )


def _make_debezium_source() -> DebeziumSourceIdentity:
    """Build a minimal DebeziumSourceIdentity for tests."""
    return DebeziumSourceIdentity(
        connector="postgresql",
        name="myserver",
        db="testdb",
        **{"schema": "public"},
        version="1.9.0.Final",
    )


def _make_debezium_config(schemas_enable: bool = True) -> DebeziumConfig:
    """Build a DebeziumConfig for tests."""
    return DebeziumConfig(source=_make_debezium_source(), schemas_enable=schemas_enable)


def _make_debezium_stream_config(
    kind: str,
    properties: list[str] | None = None,
    schemas_enable: bool = True,
) -> StreamConfig:
    """Build a StreamConfig with a debezium block for one kind."""
    return StreamConfig(
        content="state-changes",
        kinds=[StreamKindSelection(kind=kind, properties=properties or ["status"])],
        debezium=_make_debezium_config(schemas_enable=schemas_enable),
    )


def _make_anchor() -> EffectiveAnchor:
    """Build a fixed EffectiveAnchor for tests."""
    return make_anchor()


# ---------------------------------------------------------------------------
# Precondition raises — three ExportRuntimeError guards in stream_export
# ---------------------------------------------------------------------------


class TestStreamExportPreconditions:
    """stream_export raises ExportRuntimeError for invalid fmt/sink/out combos."""

    def test_unsupported_fmt_raises_with_message(self, tmp_path: Path) -> None:
        """An unsupported fmt value raises ExportRuntimeError naming the bad value."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportRuntimeError, match="unsupported format"):
                stream_export(
                    emit,
                    config,
                    fmt="csv",  # type: ignore[arg-type]
                    sink="stdout",
                    out=None,
                    anchor=None,
                )

    def test_file_sink_with_out_none_raises_with_message(self, tmp_path: Path) -> None:
        """sink='file' and out=None raises ExportRuntimeError naming the requirement."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportRuntimeError, match="output directory"):
                stream_export(
                    emit,
                    config,
                    fmt="jsonl",
                    sink="file",
                    out=None,
                    anchor=None,
                )

    def test_stdout_sink_with_out_set_raises_with_message(self, tmp_path: Path) -> None:
        """sink='stdout' and out set raises ExportRuntimeError naming the constraint."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportRuntimeError, match="out=None"):
                stream_export(
                    emit,
                    config,
                    fmt="jsonl",
                    sink="stdout",
                    out=out_dir,
                    anchor=None,
                )


# ---------------------------------------------------------------------------
# Empty-file creation path — zero-event kind produces an empty <kind>.jsonl
# ---------------------------------------------------------------------------


class TestStreamExportEmptyFile:
    """A selected kind with zero records produces an empty <kind>.jsonl file."""

    def test_zero_record_kind_produces_empty_jsonl(self, tmp_path: Path) -> None:
        """stream_export creates an empty <kind>.jsonl for a kind with no records."""
        emit_dir = _build_emit(tmp_path, "item", record_rows=[], history_rows=[])
        config = _make_config("item")
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="file",
                out=out_dir,
                anchor=None,
            )

        empty_file = out_dir / "item.jsonl"
        assert empty_file.exists(), "empty item.jsonl must be created"
        assert empty_file.read_text(encoding="utf-8") == "", "empty file must be empty"
        assert outcome.events_per_topic == {"item": 0}
        assert outcome.total_events == 0


# ---------------------------------------------------------------------------
# Business rules — DebeziumRequiresConfig and DebeziumRequiresAnchor
# ---------------------------------------------------------------------------

_DAY = 86_400_000_000_000


class TestDebeziumBusinessRules:
    """stream_export raises ExportError for missing debezium config or anchor."""

    def test_debezium_requires_config_raises(self, tmp_path: Path) -> None:
        """fmt='debezium' with no debezium block raises ExportError."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")  # no debezium block
        anchor = _make_anchor()
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="debezium.*config block"):
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="stdout",
                    out=None,
                    anchor=anchor,
                )

    def test_debezium_requires_anchor_raises(self, tmp_path: Path) -> None:
        """fmt='debezium' with anchor=None raises ExportError."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_debezium_stream_config("item")
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="resolved effective anchor"):
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="stdout",
                    out=None,
                    anchor=None,
                )

    def test_debezium_requires_config_checked_before_anchor(
        self, tmp_path: Path
    ) -> None:
        """DebeziumRequiresConfig is checked before DebeziumRequiresAnchor."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")  # no debezium block
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="debezium.*config block"):
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="stdout",
                    out=None,
                    anchor=None,  # also missing, but config checked first
                )


# ---------------------------------------------------------------------------
# Debezium stdout delivery
# ---------------------------------------------------------------------------


class TestDebeziumStdoutDelivery:
    """stream_export dispatches to write_debezium_stream for fmt='debezium'."""

    def test_debezium_stdout_delivers_messages(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """fmt='debezium' with stdout sink writes Debezium messages to stdout."""
        record_rows = [
            ("trunk", "r001", 1 * _DAY, True, None, 1 * _DAY, "active"),
        ]
        history_rows = [
            ("trunk", "item", "r001", "status", 1 * _DAY, "active"),
        ]
        emit_dir = _build_emit(tmp_path, "item", record_rows, history_rows)
        config = _make_debezium_stream_config("item", schemas_enable=True)
        anchor = _make_anchor()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="debezium", sink="stdout", out=None, anchor=anchor
            )

        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert len(lines) == 1
        for line in lines:
            msg = json.loads(line)
            assert "payload" in msg  # schemas_enable=True wraps {schema, payload}
        assert outcome.events_per_topic["item"] == len(lines)

    def test_debezium_stdout_bare_payload_when_schemas_disabled(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """schemas_enable=false produces bare envelope payloads (no schema wrapper)."""
        record_rows = [
            ("trunk", "r001", 1 * _DAY, True, None, 1 * _DAY, "active"),
        ]
        history_rows = [
            ("trunk", "item", "r001", "status", 1 * _DAY, "active"),
        ]
        emit_dir = _build_emit(tmp_path, "item", record_rows, history_rows)
        config = _make_debezium_stream_config("item", schemas_enable=False)
        anchor = _make_anchor()

        with open_emit(emit_dir) as emit:
            stream_export(
                emit, config, fmt="debezium", sink="stdout", out=None, anchor=anchor
            )

        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert len(lines) == 1
        for line in lines:
            msg = json.loads(line)
            # bare payload has "op", "before", "after" keys — not wrapped
            assert "schema" not in msg
            assert "op" in msg


# ---------------------------------------------------------------------------
# Debezium file delivery
# ---------------------------------------------------------------------------


class TestDebeziumFileDelivery:
    """stream_export file sink writes one <kind>.jsonl per selected kind."""

    def test_debezium_file_writes_kind_jsonl(self, tmp_path: Path) -> None:
        """fmt='debezium' with file sink writes <kind>.jsonl."""
        record_rows = [
            ("trunk", "r001", 1 * _DAY, True, None, 1 * _DAY, "active"),
        ]
        history_rows = [
            ("trunk", "item", "r001", "status", 1 * _DAY, "active"),
        ]
        emit_dir = _build_emit(tmp_path, "item", record_rows, history_rows)
        config = _make_debezium_stream_config("item", schemas_enable=True)
        anchor = _make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="debezium", sink="file", out=out_dir, anchor=anchor
            )

        out_file = out_dir / "item.jsonl"
        assert out_file.exists()
        lines = [
            ln for ln in out_file.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        assert len(lines) == 1
        assert outcome.events_per_topic["item"] == len(lines)

    def test_debezium_file_empty_kind_creates_empty_file(self, tmp_path: Path) -> None:
        """A selected kind with zero events gets an empty <kind>.jsonl."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_debezium_stream_config("item", schemas_enable=True)
        anchor = _make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="debezium", sink="file", out=out_dir, anchor=anchor
            )

        out_file = out_dir / "item.jsonl"
        assert out_file.exists()
        assert out_file.read_text(encoding="utf-8") == ""
        assert outcome.events_per_topic == {"item": 0}
        assert outcome.total_events == 0


# ---------------------------------------------------------------------------
# Paced vs unpaced byte-identity (Phase 4)
# ---------------------------------------------------------------------------


def _make_clock(
    speed: float = 1e9, idle_cap_seconds: float | None = None
) -> ResolvedClock:
    """Build a ResolvedClock with a very high speed so tests don't actually sleep."""
    return ResolvedClock(speed=speed, idle_cap_seconds=idle_cap_seconds)


def _build_emit_with_events(tmp_path: Path, kind: str) -> Path:
    """Build a minimal emit with one record and one history row."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    _DAY = 86_400_000_000_000
    record_rows = [
        ("trunk", "r001", 1 * _DAY, True, None, 2 * _DAY, "active"),
    ]
    history_rows = [
        ("trunk", kind, "r001", "status", 1 * _DAY, "initial"),
        ("trunk", kind, "r001", "status", 2 * _DAY, "active"),
    ]
    return _build_emit(tmp_path, kind, record_rows, history_rows)


class TestStreamExportPacedByteIdentity:
    """Paced and unpaced stream_export produce byte-identical output."""

    def test_jsonl_file_paced_byte_identical_to_unpaced(self, tmp_path: Path) -> None:
        """Paced jsonl file output equals unpaced output byte-for-byte."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")

        out_unpaced = tmp_path / "out_unpaced"
        out_unpaced.mkdir()
        out_paced = tmp_path / "out_paced"
        out_paced.mkdir()

        with open_emit(emit_dir) as emit:
            stream_export(emit, config, "jsonl", "file", out_unpaced, anchor=None)
        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                "jsonl",
                "file",
                out_paced,
                anchor=None,
                clock=_make_clock(),
            )

        unpaced_text = (out_unpaced / "item.jsonl").read_text(encoding="utf-8")
        paced_text = (out_paced / "item.jsonl").read_text(encoding="utf-8")
        assert paced_text == unpaced_text

    def test_jsonl_stdout_paced_byte_identical_to_unpaced(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Paced jsonl stdout output equals unpaced output byte-for-byte."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")

        with open_emit(emit_dir) as emit:
            stream_export(emit, config, "jsonl", "stdout", None, anchor=None)
        unpaced_out = capsys.readouterr().out

        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                "jsonl",
                "stdout",
                None,
                anchor=None,
                clock=_make_clock(),
            )
        paced_out = capsys.readouterr().out

        assert paced_out == unpaced_out

    def test_debezium_file_paced_byte_identical_to_unpaced(
        self, tmp_path: Path
    ) -> None:
        """Paced debezium file output equals unpaced output byte-for-byte."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_debezium_stream_config("item", schemas_enable=True)
        anchor = _make_anchor()

        out_unpaced = tmp_path / "out_unpaced"
        out_unpaced.mkdir()
        out_paced = tmp_path / "out_paced"
        out_paced.mkdir()

        with open_emit(emit_dir) as emit:
            stream_export(emit, config, "debezium", "file", out_unpaced, anchor=anchor)
        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                "debezium",
                "file",
                out_paced,
                anchor=anchor,
                clock=_make_clock(),
            )

        unpaced_text = (out_unpaced / "item.jsonl").read_text(encoding="utf-8")
        paced_text = (out_paced / "item.jsonl").read_text(encoding="utf-8")
        assert paced_text == unpaced_text

    def test_anchorless_paced_jsonl_is_valid(self, tmp_path: Path) -> None:
        """A realtime clock with anchor=None (raw-ns ts) paces without error."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                "jsonl",
                "file",
                out_dir,
                anchor=None,
                clock=_make_clock(),
            )

        assert outcome.total_events > 0
        lines = (out_dir / "item.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == outcome.total_events
        for line in lines:
            obj = json.loads(line)
            assert isinstance(obj["ts"], int)  # raw ns when no anchor

    def test_clock_none_regression(self, tmp_path: Path) -> None:
        """clock=None delivers unpaced, exactly the original path."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, "jsonl", "file", out_dir, anchor=None, clock=None
            )

        assert outcome.total_events > 0


# ---------------------------------------------------------------------------
# Helpers for kafka sink tests — fake write_kafka_stream
# ---------------------------------------------------------------------------


def _fake_write_kafka_stream(
    captured: list[dict[str, Any]],
) -> Callable[..., StreamOutcome]:
    """Return a fake write_kafka_stream that captures calls."""

    def _fake(
        events: Any,
        render_value: Callable[[StreamEvent], bytes],
        anchor: Any,
        bootstrap_servers: str,
        topic_set: tuple[str, ...],
        *,
        paced: bool = False,
    ) -> StreamOutcome:
        captured.append(
            {
                "render_value": render_value,
                "anchor": anchor,
                "bootstrap_servers": bootstrap_servers,
                "topic_set": topic_set,
                "paced": paced,
                "events": list(events),
            }
        )
        counts = {t: 0 for t in topic_set}
        return StreamOutcome(total_events=0, events_per_topic=counts)

    return _fake


# ---------------------------------------------------------------------------
# Kafka sink: KafkaRequiresAnchor
# ---------------------------------------------------------------------------


class TestKafkaRequiresAnchor:
    """sink='kafka' with anchor=None raises ExportError for both formats."""

    def test_kafka_jsonl_no_anchor_raises(self, tmp_path: Path) -> None:
        """sink='kafka', fmt='jsonl', anchor=None → ExportError."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                with pytest.raises(
                    ExportError, match="kafka.*resolved effective anchor"
                ):
                    stream_export(
                        emit,
                        config,
                        fmt="jsonl",
                        sink="kafka",
                        out=None,
                        anchor=None,
                        bootstrap_servers="localhost:9092",
                    )

        assert len(captured) == 0

    def test_kafka_debezium_no_anchor_raises(self, tmp_path: Path) -> None:
        """sink='kafka', fmt='debezium', anchor=None → ExportError."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_debezium_stream_config("item")
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                with pytest.raises(
                    ExportError, match="kafka.*resolved effective anchor"
                ):
                    stream_export(
                        emit,
                        config,
                        fmt="debezium",
                        sink="kafka",
                        out=None,
                        anchor=None,
                        bootstrap_servers="localhost:9092",
                    )

        assert len(captured) == 0


# ---------------------------------------------------------------------------
# Kafka sink: jsonl render_value produces byte-identical output
# ---------------------------------------------------------------------------


class TestKafkaJsonlRenderValue:
    """sink='kafka', fmt='jsonl': render_value output == jsonl file line minus newline."""

    def test_render_value_byte_identical_to_jsonl_line(self, tmp_path: Path) -> None:
        """write_kafka_stream gets render_value whose bytes == jsonl line without \\n."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                stream_export(
                    emit,
                    config,
                    fmt="jsonl",
                    sink="kafka",
                    out=None,
                    anchor=anchor,
                    bootstrap_servers="localhost:9092",
                )

        assert len(captured) == 1
        call = captured[0]
        events = call["events"]
        render_value: Callable[[StreamEvent], bytes] = call["render_value"]
        # _make_config uses properties=[] → one CREATE event per record (1 record → 1
        # event); history rows are not emitted when no properties are tracked.
        assert len(events) == 1

        # Compare to what the file sink would write (minus the newline)
        from fabulexa_forge.exporters.streaming.encoding import encode_pinned
        from fabulexa_forge.exporters.streaming.jsonl import render_jsonl_object

        for event in events:
            expected = encode_pinned(render_jsonl_object(event)).encode("utf-8")
            assert render_value(event) == expected

    def test_topic_set_matches_build_topic_set(self, tmp_path: Path) -> None:
        """write_kafka_stream receives topic_set == build_topic_set(config)."""
        from fabulexa_forge.exporters.streaming.engine import build_topic_set

        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                expected_topic_set = build_topic_set(config, emit.sidecar)
                stream_export(
                    emit,
                    config,
                    fmt="jsonl",
                    sink="kafka",
                    out=None,
                    anchor=anchor,
                    bootstrap_servers="localhost:9092",
                )

        assert captured[0]["topic_set"] == expected_topic_set


# ---------------------------------------------------------------------------
# Kafka sink: debezium rules still fire
# ---------------------------------------------------------------------------


class TestKafkaDebeziumRules:
    """sink='kafka', fmt='debezium': debezium business rules apply."""

    def test_debezium_requires_config_fires_for_kafka(self, tmp_path: Path) -> None:
        """DebeziumRequiresConfig fires on sink='kafka' when debezium block absent."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")  # no debezium block
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                with pytest.raises(ExportError, match="debezium.*config block"):
                    stream_export(
                        emit,
                        config,
                        fmt="debezium",
                        sink="kafka",
                        out=None,
                        anchor=anchor,
                        bootstrap_servers="localhost:9092",
                    )

        assert len(captured) == 0

    def test_debezium_render_value_byte_identical_to_file_line(
        self, tmp_path: Path
    ) -> None:
        """Debezium render_value output == debezium file line without \\n."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_debezium_stream_config("item", schemas_enable=True)
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="kafka",
                    out=None,
                    anchor=anchor,
                    bootstrap_servers="localhost:9092",
                )

        assert len(captured) == 1
        call = captured[0]
        events = call["events"]
        render_value: Callable[[StreamEvent], bytes] = call["render_value"]
        assert len(events) == 2

        debezium_cfg = config.debezium
        assert debezium_cfg is not None and debezium_cfg.schemas_enable is True

        from fabulexa_forge.config.models import RoutingConfig
        from fabulexa_forge.exporters.streaming.debezium import (
            _serialize_message,
            rebased_epoch_ms,
            render_debezium_message,
        )
        from fabulexa_forge.exporters.streaming.driver import _build_value_schemas

        routing = config.routing if config.routing is not None else RoutingConfig()
        with open_emit(emit_dir) as emit2:
            value_schemas = _build_value_schemas(
                emit2,
                config,
                routing,
                debezium_cfg.source,
                routing.table_identity,
            )

        # Build the expected bytes via the FILE-SINK path (independent of the
        # kafka driver's _build_debezium_render_value closure).
        for event in events:
            ts_ms = rebased_epoch_ms(event.event_sim_time, anchor)
            table = (
                event.topic if routing.table_identity == "topic" else event.route_table
            )
            value_schema = (
                value_schemas.get(table) if value_schemas is not None else None
            )
            msg = render_debezium_message(
                event, ts_ms, debezium_cfg.source, table, value_schema
            )
            # File sink: _serialize_message appends '\n'; strip it to get the
            # byte-identical kafka payload.
            expected = _serialize_message(msg).rstrip("\n").encode("utf-8")
            assert render_value(event) == expected


# ---------------------------------------------------------------------------
# Kafka sink: paced vs unpaced
# ---------------------------------------------------------------------------


class TestKafkaPacedDelivery:
    """Paced sink='kafka' passes paced=True and produces identical results."""

    def test_paced_passes_paced_true_to_write_kafka_stream(
        self, tmp_path: Path
    ) -> None:
        """A non-None clock causes paced=True to be passed to write_kafka_stream."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")
        anchor = _make_anchor()
        clock = _make_clock()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                stream_export(
                    emit,
                    config,
                    fmt="jsonl",
                    sink="kafka",
                    out=None,
                    anchor=anchor,
                    clock=clock,
                    bootstrap_servers="localhost:9092",
                )

        assert captured[0]["paced"] is True

    def test_unpaced_passes_paced_false_to_write_kafka_stream(
        self, tmp_path: Path
    ) -> None:
        """clock=None causes paced=False to be passed to write_kafka_stream."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                stream_export(
                    emit,
                    config,
                    fmt="jsonl",
                    sink="kafka",
                    out=None,
                    anchor=anchor,
                    clock=None,
                    bootstrap_servers="localhost:9092",
                )

        assert captured[0]["paced"] is False


# ---------------------------------------------------------------------------
# Membership emit builder helpers
# ---------------------------------------------------------------------------

_MEMBERSHIP_WAITERS_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__priority", "type": "VARCHAR"},
]

_MEMBERSHIP_MEMBERS_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
]


def _build_membership_emit(
    tmp_path: Path,
    waiters_rows: list[tuple[Any, ...]],
    members_rows: list[tuple[Any, ...]],
) -> Path:
    """Build a minimal v5 emit with two membership tables.

    Table membership__queue__waiters has elem__priority; membership__team__members
    has no element fields. waiters_rows is (fork_path, record_id, joined_sim_time,
    left_sim_time, elem__priority); members_rows is (fork_path, record_id,
    joined_sim_time, left_sim_time).
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_ddl("membership__queue__waiters", _MEMBERSHIP_WAITERS_COLS))
    ph_w = ", ".join("?" for _ in _MEMBERSHIP_WAITERS_COLS)
    for row in waiters_rows:
        conn.execute(
            f'INSERT INTO "membership__queue__waiters" VALUES ({ph_w})', list(row)
        )

    conn.execute(_ddl("membership__team__members", _MEMBERSHIP_MEMBERS_COLS))
    ph_m = ", ".join("?" for _ in _MEMBERSHIP_MEMBERS_COLS)
    for row in members_rows:
        conn.execute(
            f'INSERT INTO "membership__team__members" VALUES ({ph_m})', list(row)
        )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _membership_table_spec(
                "membership__queue__waiters",
                _MEMBERSHIP_WAITERS_COLS,
                len(waiters_rows),
                "queue",
                "waiters",
            ),
            _membership_table_spec(
                "membership__team__members",
                _MEMBERSHIP_MEMBERS_COLS,
                len(members_rows),
                "team",
                "members",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path


def _make_membership_config(
    waiters_fields: list[str] | None = None,
    members_fields: list[str] | None = None,
) -> StreamConfig:
    """Build a StreamConfig for content='membership-events' with two tables."""
    return StreamConfig(
        content="membership-events",
        memberships=[
            MembershipSelection(
                owner_kind="queue",
                property="waiters",
                fields=waiters_fields if waiters_fields is not None else ["priority"],
            ),
            MembershipSelection(
                owner_kind="team",
                property="members",
                fields=members_fields if members_fields is not None else [],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Phase 3: Membership JSONL file sink
# ---------------------------------------------------------------------------

_NS = 1_000_000_000  # one second in nanoseconds


class TestMembershipJsonlFileSink:
    """stream_export content='membership-events', fmt='jsonl', sink='file' end-to-end."""

    def test_membership_jsonl_writes_per_topic_files(self, tmp_path: Path) -> None:
        """stream_export writes one .jsonl file per membership topic."""
        waiters_rows = [
            ("trunk", "r1", 1 * _NS, 3 * _NS, "high"),
            ("trunk", "r2", 2 * _NS, None, "low"),
        ]
        emit_dir = _build_membership_emit(tmp_path / "emit", waiters_rows, [])
        config = _make_membership_config()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="jsonl", sink="file", out=out_dir, anchor=None
            )

        assert (out_dir / "queue__waiters.jsonl").exists()
        assert (out_dir / "team__members.jsonl").exists()
        assert outcome.events_per_topic["team__members"] == 0
        assert outcome.events_per_topic["queue__waiters"] > 0

    def test_membership_jsonl_lines_parse_to_valid_objects(
        self, tmp_path: Path
    ) -> None:
        """Each line in the membership JSONL output parses to a valid event object."""
        waiters_rows = [
            ("trunk", "r1", 1 * _NS, 3 * _NS, "high"),
        ]
        emit_dir = _build_membership_emit(tmp_path / "emit", waiters_rows, [])
        config = _make_membership_config()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            stream_export(
                emit, config, fmt="jsonl", sink="file", out=out_dir, anchor=None
            )

        lines = [
            json.loads(ln)
            for ln in (out_dir / "queue__waiters.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if ln.strip()
        ]
        assert len(lines) >= 1
        for obj in lines:
            assert "seq" in obj
            assert "op" in obj
            assert "ts" in obj
            assert obj["op"] in ("join", "leave")

    def test_membership_jsonl_closed_interval_emits_join_and_leave(
        self, tmp_path: Path
    ) -> None:
        """A closed membership interval (left_sim_time non-null) emits join + leave."""
        waiters_rows = [
            ("trunk", "r1", 1 * _NS, 3 * _NS, "high"),
        ]
        emit_dir = _build_membership_emit(tmp_path / "emit", waiters_rows, [])
        config = _make_membership_config()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            stream_export(
                emit, config, fmt="jsonl", sink="file", out=out_dir, anchor=None
            )

        lines = [
            json.loads(ln)
            for ln in (out_dir / "queue__waiters.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if ln.strip()
        ]
        ops = [obj["op"] for obj in lines]
        assert "join" in ops
        assert "leave" in ops

    def test_membership_jsonl_open_interval_emits_join_only(
        self, tmp_path: Path
    ) -> None:
        """An open membership interval (left_sim_time IS NULL) emits join only."""
        waiters_rows = [
            ("trunk", "r2", 2 * _NS, None, "low"),
        ]
        emit_dir = _build_membership_emit(tmp_path / "emit", waiters_rows, [])
        config = _make_membership_config()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            stream_export(
                emit, config, fmt="jsonl", sink="file", out=out_dir, anchor=None
            )

        lines = [
            json.loads(ln)
            for ln in (out_dir / "queue__waiters.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if ln.strip()
        ]
        ops = [obj["op"] for obj in lines]
        assert ops == ["join"]

    def test_membership_jsonl_outcome_counts(self, tmp_path: Path) -> None:
        """StreamOutcome counts match events written (join+leave + empty topic)."""
        waiters_rows = [
            ("trunk", "r1", 1 * _NS, 3 * _NS, "high"),
            ("trunk", "r2", 2 * _NS, None, "low"),
        ]
        emit_dir = _build_membership_emit(tmp_path / "emit", waiters_rows, [])
        config = _make_membership_config()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="jsonl", sink="file", out=out_dir, anchor=None
            )

        # r1 closed: join + leave = 2; r2 open: join = 1; total = 3
        assert outcome.total_events == 3
        assert outcome.events_per_topic["queue__waiters"] == 3
        assert outcome.events_per_topic["team__members"] == 0

    def test_membership_jsonl_declared_but_empty_topic_creates_empty_file(
        self, tmp_path: Path
    ) -> None:
        """A selected membership table with no rows produces an empty .jsonl file."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_config()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="jsonl", sink="file", out=out_dir, anchor=None
            )

        waiters_file = out_dir / "queue__waiters.jsonl"
        members_file = out_dir / "team__members.jsonl"
        assert waiters_file.exists()
        assert members_file.exists()
        assert waiters_file.read_text(encoding="utf-8") == ""
        assert members_file.read_text(encoding="utf-8") == ""
        assert outcome.total_events == 0
        assert outcome.events_per_topic == {"queue__waiters": 0, "team__members": 0}


# ---------------------------------------------------------------------------
# Membership Debezium helpers
# ---------------------------------------------------------------------------


def _make_membership_debezium_config(
    schemas_enable: bool = True,
    waiters_fields: list[str] | None = None,
    members_fields: list[str] | None = None,
    routing: "RoutingConfig | None" = None,
) -> StreamConfig:
    """Build a StreamConfig for membership-events with a debezium block."""
    return StreamConfig(
        content="membership-events",
        memberships=[
            MembershipSelection(
                owner_kind="queue",
                property="waiters",
                fields=waiters_fields if waiters_fields is not None else ["priority"],
            ),
            MembershipSelection(
                owner_kind="team",
                property="members",
                fields=members_fields if members_fields is not None else [],
            ),
        ],
        debezium=_make_debezium_config(schemas_enable=schemas_enable),
        routing=routing,
    )


def _one_waiter_emit(tmp_path: Path) -> Path:
    """Build a membership emit with one waiter row and no members."""
    waiters_rows: list[tuple[Any, ...]] = [
        ("trunk", "r1", 1 * _NS, 3 * _NS, "high"),
    ]
    return _build_membership_emit(tmp_path / "emit", waiters_rows, [])


# ---------------------------------------------------------------------------
# Membership Debezium: stdout sink
# ---------------------------------------------------------------------------


class TestMembershipDebeziumStdout:
    """membership-events + fmt='debezium' + sink='stdout' renders Debezium envelopes."""

    def test_membership_debezium_stdout_renders_events(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """fmt='debezium' over membership content writes lines to stdout (op='c')."""
        emit_dir = _one_waiter_emit(tmp_path)
        config = _make_membership_debezium_config(schemas_enable=True)
        anchor = _make_anchor()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="debezium", sink="stdout", out=None, anchor=anchor
            )

        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert len(lines) == 2
        for line in lines:
            msg = json.loads(line)
            payload = msg["payload"]
            assert payload["op"] == "c"
            assert "event" in payload["after"]
        assert outcome.total_events == len(lines)

    def test_membership_debezium_stdout_schemas_wrapped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """schemas_enable=True wraps each message in {schema, payload}."""
        emit_dir = _one_waiter_emit(tmp_path)
        config = _make_membership_debezium_config(schemas_enable=True)
        anchor = _make_anchor()

        with open_emit(emit_dir) as emit:
            stream_export(
                emit, config, fmt="debezium", sink="stdout", out=None, anchor=anchor
            )

        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert len(lines) == 2
        for line in lines:
            msg = json.loads(line)
            assert "schema" in msg
            assert "payload" in msg

    def test_membership_debezium_stdout_bare_when_schemas_disabled(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """schemas_enable=False produces bare envelopes without schema wrapper."""
        emit_dir = _one_waiter_emit(tmp_path)
        config = _make_membership_debezium_config(schemas_enable=False)
        anchor = _make_anchor()

        with open_emit(emit_dir) as emit:
            stream_export(
                emit, config, fmt="debezium", sink="stdout", out=None, anchor=anchor
            )

        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert len(lines) >= 1
        for line in lines:
            msg = json.loads(line)
            assert "schema" not in msg
            assert "op" in msg
            assert msg["op"] == "c"


# ---------------------------------------------------------------------------
# Membership Debezium: file sink
# ---------------------------------------------------------------------------


class TestMembershipDebeziumFileSink:
    """membership-events + fmt='debezium' + sink='file' writes per-topic JSONL."""

    def test_membership_debezium_file_writes_per_topic_files(
        self, tmp_path: Path
    ) -> None:
        """fmt='debezium' file sink writes one <topic>.jsonl per membership topic."""
        emit_dir = _one_waiter_emit(tmp_path)
        config = _make_membership_debezium_config(schemas_enable=True)
        anchor = _make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="debezium", sink="file", out=out_dir, anchor=anchor
            )

        waiters_file = out_dir / "queue__waiters.jsonl"
        members_file = out_dir / "team__members.jsonl"
        assert waiters_file.exists()
        assert members_file.exists()
        lines = [
            ln
            for ln in waiters_file.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert len(lines) == 2
        for line in lines:
            msg = json.loads(line)
            assert msg["payload"]["op"] == "c"
            assert "event" in msg["payload"]["after"]
        assert outcome.events_per_topic["queue__waiters"] == len(lines)

    def test_membership_debezium_file_empty_table_creates_empty_file(
        self, tmp_path: Path
    ) -> None:
        """A selected membership table with no rows produces an empty .jsonl file."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_debezium_config(schemas_enable=True)
        anchor = _make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="debezium", sink="file", out=out_dir, anchor=anchor
            )

        assert (out_dir / "queue__waiters.jsonl").read_text(encoding="utf-8") == ""
        assert (out_dir / "team__members.jsonl").read_text(encoding="utf-8") == ""
        assert outcome.total_events == 0


# ---------------------------------------------------------------------------
# Membership Debezium: DebeziumRequiresConfig + DebeziumRequiresAnchor
# ---------------------------------------------------------------------------


class TestMembershipDebeziumBusinessRules:
    """membership-events debezium business rules fire in the correct order."""

    def test_membership_debezium_requires_config_stdout(self, tmp_path: Path) -> None:
        """membership + fmt='debezium' + no debezium block → ExportError (config)."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_config()  # no debezium block
        anchor = _make_anchor()
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="debezium.*config block"):
                stream_export(
                    emit, config, fmt="debezium", sink="stdout", out=None, anchor=anchor
                )

    def test_membership_debezium_requires_config_file(self, tmp_path: Path) -> None:
        """membership + fmt='debezium' + no debezium block fires on file sink too."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_config()
        anchor = _make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="debezium.*config block"):
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="file",
                    out=out_dir,
                    anchor=anchor,
                )

    def test_membership_debezium_requires_anchor_stdout(self, tmp_path: Path) -> None:
        """membership + fmt='debezium' + anchor=None → ExportError (anchor)."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_debezium_config()
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="resolved effective anchor"):
                stream_export(
                    emit, config, fmt="debezium", sink="stdout", out=None, anchor=None
                )

    def test_membership_debezium_config_checked_before_anchor(
        self, tmp_path: Path
    ) -> None:
        """DebeziumRequiresConfig fires before DebeziumRequiresAnchor for membership."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_config()  # no debezium block; anchor also missing
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="debezium.*config block"):
                stream_export(
                    emit, config, fmt="debezium", sink="stdout", out=None, anchor=None
                )


# ---------------------------------------------------------------------------
# Membership Debezium: topic-schema ambiguity rule
# ---------------------------------------------------------------------------


class TestMembershipDebeziumTopicAmbiguity:
    """StreamTopicSchemaUnambiguous fires for membership tables under table_identity='topic'."""

    def test_ambiguous_topic_raises_export_error(self, tmp_path: Path) -> None:
        """table_identity='topic' + topic merging two membership tables → ExportError."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        # A literal topic_template collapses all memberships into one topic
        routing = RoutingConfig(
            topic_template="all_membership",
            table_identity="topic",
        )
        config = _make_membership_debezium_config(schemas_enable=True, routing=routing)
        anchor = _make_anchor()
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="merges membership tables"):
                stream_export(
                    emit, config, fmt="debezium", sink="stdout", out=None, anchor=anchor
                )

    def test_source_table_identity_allows_merge(self, tmp_path: Path) -> None:
        """table_identity='source_table' (default) with shared topic → no raise."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        # Same literal template but source_table identity — legal
        routing = RoutingConfig(
            topic_template="all_membership",
            table_identity="source_table",
        )
        config = _make_membership_debezium_config(schemas_enable=True, routing=routing)
        anchor = _make_anchor()
        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="debezium", sink="stdout", out=None, anchor=anchor
            )
        assert outcome.total_events == 0

    def test_schemas_disabled_skips_ambiguity_check(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """schemas_enable=False skips topic-ambiguity check even under table_identity='topic'."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        routing = RoutingConfig(
            topic_template="all_membership",
            table_identity="topic",
        )
        config = _make_membership_debezium_config(schemas_enable=False, routing=routing)
        anchor = _make_anchor()
        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="debezium", sink="stdout", out=None, anchor=anchor
            )
        assert outcome.total_events == 0

    def test_one_table_per_topic_allowed_under_topic_identity(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """table_identity='topic' with distinct topics per table → no raise."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        # Default {route_table} template gives distinct topics per membership table
        routing = RoutingConfig(table_identity="topic")
        config = _make_membership_debezium_config(schemas_enable=True, routing=routing)
        anchor = _make_anchor()
        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="debezium", sink="stdout", out=None, anchor=anchor
            )
        assert outcome.total_events == 0


# ---------------------------------------------------------------------------
# Membership Debezium: kafka sink
# ---------------------------------------------------------------------------


class TestMembershipDebeziumKafka:
    """membership-events + fmt='debezium' + sink='kafka' dispatches correctly."""

    def test_membership_kafka_requires_anchor_first(self, tmp_path: Path) -> None:
        """sink='kafka' with anchor=None raises KafkaRequiresAnchor before debezium rules."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_debezium_config()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                with pytest.raises(
                    ExportError, match="kafka.*resolved effective anchor"
                ):
                    stream_export(
                        emit,
                        config,
                        fmt="debezium",
                        sink="kafka",
                        out=None,
                        anchor=None,
                        bootstrap_servers="localhost:9092",
                    )

        assert len(captured) == 0

    def test_membership_kafka_requires_config(self, tmp_path: Path) -> None:
        """membership + sink='kafka' + no debezium block → DebeziumRequiresConfig."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_config()  # no debezium block
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                with pytest.raises(ExportError, match="debezium.*config block"):
                    stream_export(
                        emit,
                        config,
                        fmt="debezium",
                        sink="kafka",
                        out=None,
                        anchor=anchor,
                        bootstrap_servers="localhost:9092",
                    )

        assert len(captured) == 0

    def test_membership_kafka_render_value_matches_file_line(
        self, tmp_path: Path
    ) -> None:
        """Debezium render_value for membership kafka == file line without newline."""
        waiters_rows: list[tuple[Any, ...]] = [
            ("trunk", "r1", 1 * _NS, 3 * _NS, "high"),
        ]
        emit_dir = _build_membership_emit(tmp_path / "emit", waiters_rows, [])
        config = _make_membership_debezium_config(schemas_enable=True)
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="kafka",
                    out=None,
                    anchor=anchor,
                    bootstrap_servers="localhost:9092",
                )

        assert len(captured) == 1
        call = captured[0]
        events = call["events"]
        render_value: Callable[[StreamEvent], bytes] = call["render_value"]
        assert len(events) >= 1

        from fabulexa_forge.config.models import RoutingConfig as _RoutingConfig
        from fabulexa_forge.exporters.streaming.debezium import (
            _serialize_message,
            rebased_epoch_ms,
            render_debezium_message,
        )
        from fabulexa_forge.exporters.streaming.driver import _build_value_schemas

        routing = config.routing if config.routing is not None else _RoutingConfig()
        debezium_cfg = config.debezium
        assert debezium_cfg is not None
        with open_emit(emit_dir) as emit2:
            value_schemas = _build_value_schemas(
                emit2,
                config,
                routing,
                debezium_cfg.source,
                routing.table_identity,
            )

        for event in events:
            ts_ms = rebased_epoch_ms(event.event_sim_time, anchor)
            table = (
                event.topic if routing.table_identity == "topic" else event.route_table
            )
            value_schema = (
                value_schemas.get(table) if value_schemas is not None else None
            )
            msg = render_debezium_message(
                event, ts_ms, debezium_cfg.source, table, value_schema
            )
            expected = _serialize_message(msg).rstrip("\n").encode("utf-8")
            assert render_value(event) == expected

    def test_membership_kafka_schemas_disabled_bare_render(
        self, tmp_path: Path
    ) -> None:
        """schemas_enable=False produces bare-envelope render_value bytes."""
        waiters_rows: list[tuple[Any, ...]] = [
            ("trunk", "r1", 1 * _NS, 3 * _NS, "high"),
        ]
        emit_dir = _build_membership_emit(tmp_path / "emit", waiters_rows, [])
        config = _make_membership_debezium_config(schemas_enable=False)
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="kafka",
                    out=None,
                    anchor=anchor,
                    bootstrap_servers="localhost:9092",
                )

        assert len(captured) == 1
        call = captured[0]
        events = call["events"]
        render_value: Callable[[StreamEvent], bytes] = call["render_value"]
        for event in events:
            msg = json.loads(render_value(event).decode("utf-8"))
            assert "schema" not in msg
            assert msg["op"] == "c"


# ---------------------------------------------------------------------------
# build_kafka_render_value: direct tests
# ---------------------------------------------------------------------------


def _make_routing() -> "RoutingConfig":
    """Build a default RoutingConfig."""
    return RoutingConfig()


class TestBuildKafkaRenderValueJsonl:
    """build_kafka_render_value(fmt='jsonl') returns bytes byte-identical to jsonl line."""

    def test_jsonl_bytes_match_file_line_minus_newline(self, tmp_path: Path) -> None:
        """fmt='jsonl' render_value bytes == encode_pinned(render_jsonl_object(event))."""
        from fabulexa_forge.exporters.streaming.encoding import encode_pinned
        from fabulexa_forge.exporters.streaming.jsonl import render_jsonl_object

        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")
        anchor = _make_anchor()

        with open_emit(emit_dir) as emit:
            routing = _make_routing()
            from fabulexa_forge.exporters.streaming.engine import build_topic_set

            topic_set = build_topic_set(config, emit.sidecar)
            render = build_kafka_render_value(
                emit, config, "jsonl", anchor, routing, topic_set
            )
            from fabulexa_forge.exporters.streaming.engine import iter_stream_events

            events = list(iter_stream_events(emit, config, anchor))

        assert len(events) > 0
        for event in events:
            expected = encode_pinned(render_jsonl_object(event)).encode("utf-8")
            assert render(event) == expected
            assert not render(event).endswith(b"\n")


class TestBuildKafkaRenderValueDebezium:
    """build_kafka_render_value(fmt='debezium') enforces rules and returns correct bytes."""

    def test_debezium_requires_config_raises(self, tmp_path: Path) -> None:
        """fmt='debezium' with no debezium block raises ExportError (DebeziumRequiresConfig)."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")  # no debezium block
        anchor = _make_anchor()

        with open_emit(emit_dir) as emit:
            routing = _make_routing()
            from fabulexa_forge.exporters.streaming.engine import build_topic_set

            topic_set = build_topic_set(config, emit.sidecar)
            with pytest.raises(ExportError, match="debezium.*config block"):
                build_kafka_render_value(
                    emit, config, "debezium", anchor, routing, topic_set
                )

    def test_ambiguous_topic_raises_stream_topic_schema_unambiguous(
        self, tmp_path: Path
    ) -> None:
        """table_identity='topic' + ambiguous mapping raises ExportError (StreamTopicSchemaUnambiguous)."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        routing = RoutingConfig(
            topic_template="all_membership",
            table_identity="topic",
        )
        config = _make_membership_debezium_config(schemas_enable=True, routing=routing)
        anchor = _make_anchor()

        with open_emit(emit_dir) as emit:
            from fabulexa_forge.exporters.streaming.engine import build_topic_set

            topic_set = build_topic_set(config, emit.sidecar)
            with pytest.raises(ExportError, match="merges membership tables"):
                build_kafka_render_value(
                    emit, config, "debezium", anchor, routing, topic_set
                )

    def test_debezium_bytes_match_file_line_minus_newline(self, tmp_path: Path) -> None:
        """fmt='debezium' render_value bytes == file line bytes (no trailing newline)."""
        from fabulexa_forge.exporters.streaming.debezium import (
            _serialize_message,
            rebased_epoch_ms,
            render_debezium_message,
        )
        from fabulexa_forge.exporters.streaming.driver import _build_value_schemas

        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_debezium_stream_config("item", schemas_enable=True)
        anchor = _make_anchor()
        routing = _make_routing()

        with open_emit(emit_dir) as emit:
            from fabulexa_forge.exporters.streaming.engine import (
                build_topic_set,
                iter_stream_events,
            )

            topic_set = build_topic_set(config, emit.sidecar)
            render = build_kafka_render_value(
                emit, config, "debezium", anchor, routing, topic_set
            )
            events = list(iter_stream_events(emit, config, anchor))

        debezium_cfg = config.debezium
        assert debezium_cfg is not None
        with open_emit(emit_dir) as emit2:
            value_schemas = _build_value_schemas(
                emit2, config, routing, debezium_cfg.source, routing.table_identity
            )

        assert len(events) > 0
        for event in events:
            ts_ms = rebased_epoch_ms(event.event_sim_time, anchor)
            table = (
                event.topic if routing.table_identity == "topic" else event.route_table
            )
            value_schema = (
                value_schemas.get(table) if value_schemas is not None else None
            )
            msg = render_debezium_message(
                event, ts_ms, debezium_cfg.source, table, value_schema
            )
            expected = _serialize_message(msg).rstrip("\n").encode("utf-8")
            assert render(event) == expected
