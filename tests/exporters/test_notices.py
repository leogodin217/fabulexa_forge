"""Tests for the mode-neutral notice channel (`exporters/notices.py`) and its
threading through the dimensional compile, the incremental driver, and the
CLI `export` verb.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import FrozenInstanceError
from pathlib import Path

import duckdb
import pytest
import yaml
from _support.notices import RecordingNoticeSink, discard_notice_sink
from _support.sidecar_builder import identity_column, write_emit

from fabulexa_forge.cli import cmd_export
from fabulexa_forge.config.models import ExportConfig
from fabulexa_forge.exporters.dimensional.engine import (
    build_query_specs,
    export_dimensional,
)
from fabulexa_forge.exporters.notices import Notice, render_notice_stderr
from fabulexa_forge.incremental.driver import export_incremental_next, export_window
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit

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
    {
        "name": "prop__entity_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_PERIOD_NS = 100  # small period for incremental drip tests


def _build_notice_emit(tmp_path: Path, slice_at: int = 250) -> Path:
    """Build a minimal emit with three entities and an entity_type discriminator.

    Declared observed values (`enum_domains`) are 'consultant' and 'nurse';
    a filter naming 'admin' is unobserved.

    Args:
        tmp_path: Directory for the emit artifacts.
        slice_at: The branch's slice_at value.

    Returns:
        The emit directory.
    """
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    col_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _RECORDS_COLUMNS)
    conn.execute(f'CREATE TABLE "records__entity" ({col_ddl})')

    for record_index, (entity_id, name, mutation_time) in enumerate(
        [("e001", "Alice", 10), ("e002", "Bob", 110), ("e003", "Carol", 210)]
    ):
        conn.execute(
            'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
            [
                "trunk",
                entity_id,
                mutation_time,
                True,
                mutation_time,
                record_index,
                name,
                "consultant",
            ],
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
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": slice_at}],
        extra={"enum_domains": {"entity": {"entity_type": ["consultant", "nurse"]}}},
    )
    return emit_dir


def _config_with_filter(
    value: str | list[str], *, with_incremental: bool = False
) -> ExportConfig:
    """Build a type-1 dim config filtering entity records on entity_type.

    Args:
        value: The prop__entity_type filter value — a scalar or a list.
        with_incremental: If True, include a sim_period_ns incremental block.

    Returns:
        The ExportConfig.
    """
    data: dict[str, object] = {
        "mode": "dimensional",
        "dimensional": {
            "tables": [
                {
                    "name": "dim_entity",
                    "role": "dim",
                    "scd": "type1",
                    "source": {
                        "grain": "records",
                        "kind": "entity",
                        "filter": {"prop__entity_type": value},
                    },
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


_UNOBSERVED_MESSAGE = (
    "discriminator value 'admin' not observed for"
    " 'entity.prop__entity_type'; table will be empty"
)

# ---------------------------------------------------------------------------
# Notice / NoticeSink / render_notice_stderr
# ---------------------------------------------------------------------------


def test_notice_is_frozen_and_value_equal() -> None:
    """Notice is frozen (mutation raises) and value-equal by fields."""
    first = Notice(code="c", message="m")
    second = Notice(code="c", message="m")
    assert first == second
    with pytest.raises(FrozenInstanceError):
        first.code = "other"  # type: ignore[misc]


def test_render_notice_stderr_writes_exact_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """render_notice_stderr writes exactly 'notice: {message}\\n' to stderr;
    stdout is untouched; it returns None."""
    result = render_notice_stderr(Notice(code="c", message="hello"))
    captured = capsys.readouterr()
    assert result is None
    assert captured.err == "notice: hello\n"
    assert captured.out == ""


# ---------------------------------------------------------------------------
# DiscriminatorValueObserved -> notice channel
# ---------------------------------------------------------------------------


def test_discriminator_unobserved_emits_one_notice_no_warning(tmp_path: Path) -> None:
    """Unobserved filter value -> exactly one notice, verbatim former warning
    text; no Python warning raised (passes under simplefilter('error'))."""
    emit_dir = _build_notice_emit(tmp_path)
    config = _config_with_filter("admin")
    assert config.dimensional is not None
    sink = RecordingNoticeSink()

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with open_emit(emit_dir) as emit:
            build_query_specs(
                emit, config.dimensional, None, None, sink, base_relations=None
            )

    assert len(sink.notices) == 1
    notice = sink.notices[0]
    assert notice.code == "discriminator-value-unobserved"
    assert notice.message == _UNOBSERVED_MESSAGE


def test_discriminator_observed_emits_zero_notices(tmp_path: Path) -> None:
    """Observed filter value -> zero notices."""
    emit_dir = _build_notice_emit(tmp_path)
    config = _config_with_filter("consultant")
    assert config.dimensional is not None
    sink = RecordingNoticeSink()

    with open_emit(emit_dir) as emit:
        build_query_specs(
            emit, config.dimensional, None, None, sink, base_relations=None
        )

    assert sink.notices == []


def test_discriminator_list_wholly_unobserved_emits_one_notice_per_element(
    tmp_path: Path,
) -> None:
    """A list filter with no element observed -> one notice per element,
    end-to-end through the compile, each keeping the verbatim 'table will be
    empty' wording (§ The unobserved-value notice matrix, row 3)."""
    emit_dir = _build_notice_emit(tmp_path)
    config = _config_with_filter(["admin", "guest"])
    assert config.dimensional is not None
    sink = RecordingNoticeSink()

    with open_emit(emit_dir) as emit:
        build_query_specs(
            emit, config.dimensional, None, None, sink, base_relations=None
        )

    assert [n.code for n in sink.notices] == ["discriminator-value-unobserved"] * 2
    assert [n.message for n in sink.notices] == [
        "discriminator value 'admin' not observed for"
        " 'entity.prop__entity_type'; table will be empty",
        "discriminator value 'guest' not observed for"
        " 'entity.prop__entity_type'; table will be empty",
    ]


def test_discriminator_list_partially_observed_emits_weaker_wording(
    tmp_path: Path,
) -> None:
    """A list filter with some elements observed -> one notice per unobserved
    element only, in config element order, end-to-end through the compile,
    with the weaker 'it contributes no rows' wording — the table is not, in
    fact, empty (§ The unobserved-value notice matrix, row 4)."""
    emit_dir = _build_notice_emit(tmp_path)
    config = _config_with_filter(["consultant", "admin"])
    assert config.dimensional is not None
    sink = RecordingNoticeSink()

    with open_emit(emit_dir) as emit:
        build_query_specs(
            emit, config.dimensional, None, None, sink, base_relations=None
        )

    assert len(sink.notices) == 1
    assert sink.notices[0].code == "discriminator-value-unobserved"
    assert sink.notices[0].message == (
        "discriminator value 'admin' not observed for"
        " 'entity.prop__entity_type'; it contributes no rows"
    )


def test_build_query_specs_notice_sequence_deterministic(tmp_path: Path) -> None:
    """Two identical build_query_specs runs -> identical notice sequences."""
    emit_dir = _build_notice_emit(tmp_path)
    config = _config_with_filter("admin")
    assert config.dimensional is not None

    first_sink = RecordingNoticeSink()
    second_sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        build_query_specs(
            emit, config.dimensional, None, None, first_sink, base_relations=None
        )
        build_query_specs(
            emit, config.dimensional, None, None, second_sink, base_relations=None
        )

    assert first_sink.notices == second_sink.notices
    assert len(first_sink.notices) == 1


# ---------------------------------------------------------------------------
# export_dimensional threads the sink; output is sink-choice-independent
# ---------------------------------------------------------------------------


def test_export_dimensional_output_identical_recording_or_discarding(
    tmp_path: Path,
) -> None:
    """export_dimensional threads notice_sink to the compile; output tables
    are byte-identical whether the sink records or discards."""
    emit_dir = _build_notice_emit(tmp_path)
    config = _config_with_filter("admin")

    out_recording = tmp_path / "out_recording"
    out_discard = tmp_path / "out_discard"
    out_recording.mkdir()
    out_discard.mkdir()

    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        export_dimensional(emit, config, out_recording, "csv", None, sink, None)
    with open_emit(emit_dir) as emit:
        export_dimensional(
            emit, config, out_discard, "csv", None, discard_notice_sink, None
        )

    assert len(sink.notices) == 1
    assert (out_recording / "dim_entity.csv").read_bytes() == (
        out_discard / "dim_entity.csv"
    ).read_bytes()


# ---------------------------------------------------------------------------
# export_window / export_incremental_next thread the sink to the compile
# ---------------------------------------------------------------------------


def test_export_window_threads_sink_to_dimensional_compile(tmp_path: Path) -> None:
    """export_window threads notice_sink to build_query_specs for a range export."""
    emit_dir = _build_notice_emit(tmp_path)
    config = _config_with_filter("admin")
    out = tmp_path / "range.duckdb"
    window = Window(index=None, start_ns=0, end_ns=10**12, label="range")
    sink = RecordingNoticeSink()

    with open_emit(emit_dir) as emit:
        export_window(emit, config, out, "duckdb", None, window, None, sink)

    assert len(sink.notices) == 1
    assert sink.notices[0].code == "discriminator-value-unobserved"


def test_export_incremental_next_drip_reemits_notices_each_invocation(
    tmp_path: Path,
) -> None:
    """A --next drip re-emits its compile's notices each invocation."""
    emit_dir = _build_notice_emit(tmp_path)
    config = _config_with_filter("admin", with_incremental=True)
    out = tmp_path / "wh.duckdb"

    first_sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        first_outcome = export_incremental_next(
            emit, config, out, "duckdb", None, first_sink
        )
    assert first_outcome.status == "emitted"

    second_sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        second_outcome = export_incremental_next(
            emit, config, out, "duckdb", None, second_sink
        )
    assert second_outcome.status == "emitted"

    assert first_sink.notices == second_sink.notices
    assert len(first_sink.notices) == 1


# ---------------------------------------------------------------------------
# CLI `export`: notice on stderr before data delivery; exit 0
# ---------------------------------------------------------------------------


def test_cli_export_notice_on_stderr_before_data_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI export: notice rendered to stderr; exit 0; stdout carries only the
    existing output."""
    emit_dir = _build_notice_emit(tmp_path)
    config = _config_with_filter("admin")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump(json.loads(config.model_dump_json()), allow_unicode=True),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    exit_code = cmd_export(emit_dir, config_path, out_dir, "csv")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == f"notice: {_UNOBSERVED_MESSAGE}\n"
    assert "dim_entity: 0 rows" in captured.out
    assert (out_dir / "dim_entity.csv").exists()


def test_cli_export_run_twice_byte_identical_notice_sequence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Running the same export twice produces byte-identical stderr notices."""
    emit_dir = _build_notice_emit(tmp_path)
    config = _config_with_filter("admin")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump(json.loads(config.model_dump_json()), allow_unicode=True),
        encoding="utf-8",
    )

    out_dir_1 = tmp_path / "out1"
    out_dir_1.mkdir()
    cmd_export(emit_dir, config_path, out_dir_1, "csv")
    first_err = capsys.readouterr().err

    out_dir_2 = tmp_path / "out2"
    out_dir_2.mkdir()
    cmd_export(emit_dir, config_path, out_dir_2, "csv")
    second_err = capsys.readouterr().err

    assert first_err == second_err == f"notice: {_UNOBSERVED_MESSAGE}\n"
