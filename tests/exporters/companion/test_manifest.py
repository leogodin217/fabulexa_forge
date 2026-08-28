"""Tests for the companion manifest builder: field set (build_manifest_document)
and pinned byte serialization (render_manifest_bytes)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from exporters.companion._fixtures import write_minimal_emit
from fabulexa_forge import __version__
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import ExportConfig
from fabulexa_forge.exporters.companion.artifacts import WindowedArtifactState
from fabulexa_forge.exporters.companion.manifest import (
    build_manifest_document,
    render_manifest_bytes,
)
from fabulexa_forge.exporters.query_spec import ExportReport, TableKeys, TableReport
from fabulexa_forge.reader.emit import compute_sidecar_sha256, open_emit

_ANCHOR = EffectiveAnchor(
    start_instant=datetime(2024, 1, 1, tzinfo=timezone.utc), timezone=ZoneInfo("UTC")
)
_RUNTIME_EXTRA: dict[str, object] = {
    "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"}
}


def _two_table_report(*, row_count: int | None) -> ExportReport:
    """Two tables in a fixed, non-alphabetical order: one with declared keys
    and a non-ASCII name, one without keys."""
    return ExportReport(
        tables=(
            TableReport(
                name="café",
                columns=(("id", "BIGINT"), ("name", "VARCHAR")),
                row_count=row_count,
                keys=TableKeys(primary_key=("id",), unique=(("name",),)),
                provenance={},
                kind_values={},
            ),
            TableReport(
                name="visits",
                columns=(("visit_id", "BIGINT"),),
                row_count=row_count,
                keys=None,
                provenance={},
                kind_values={},
            ),
        )
    )


def _top_level_key_index(text: str, key: str) -> int:
    """The text position of `key` as a root-level object key (exactly
    two-space indent) -- distinct from the same name nested deeper (e.g. the
    embedded config's own `mode` / `incremental` fields)."""
    match = re.search(rf'\n  "{re.escape(key)}":', text)
    assert match is not None, f"top-level key {key!r} not found"
    return match.start()


def _build_document(
    emit_dir: Path,
    *,
    anchor: EffectiveAnchor | None,
    windowed: WindowedArtifactState | None,
    readme_overlay: str | None = None,
    row_count: int | None = 1,
) -> dict[str, object]:
    with open_emit(emit_dir) as emit:
        return build_manifest_document(
            emit=emit,
            config=ExportConfig(mode="base", readme_overlay=readme_overlay),
            fmt="csv",
            anchor=anchor,
            report=_two_table_report(row_count=row_count),
            windowed=windowed,
        )


# ---------------------------------------------------------------------------
# Field set — full export
# ---------------------------------------------------------------------------


def test_full_export_field_set(tmp_path: Path) -> None:
    """A full export's manifest carries the complete field set: version,
    mode, format, forge_version, emit identity, anchor, config (incl.
    readme_overlay), null incremental, tables in report order."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir, extra=_RUNTIME_EXTRA)

    document = _build_document(emit_dir, anchor=_ANCHOR, windowed=None)

    assert document["manifest_format_version"] == 1
    assert document["mode"] == "base"
    assert document["format"] == "csv"
    assert document["forge_version"] == __version__
    assert document["incremental"] is None

    emit_block = document["emit"]
    assert isinstance(emit_block, dict)
    with open_emit(emit_dir) as emit:
        assert emit_block["sidecar_sha256"] == compute_sidecar_sha256(emit)
    assert emit_block["fork_path"] == "trunk"
    assert emit_block["slice_at"] == 0
    assert emit_block["runtime"] == {
        "timezone": "UTC",
        "start_datetime": "2024-01-01T00:00:00+00:00",
    }

    assert document["anchor"] is not None

    config_block = document["config"]
    assert isinstance(config_block, dict)
    assert "readme_overlay" in config_block
    assert config_block["readme_overlay"] is None

    tables = document["tables"]
    assert isinstance(tables, list)
    assert [table["name"] for table in tables] == ["café", "visits"]
    assert tables[0]["primary_key"] == ["id"]
    assert tables[0]["unique"] == [["name"]]
    assert tables[0]["row_count"] == 1
    assert tables[1]["primary_key"] is None
    assert tables[1]["unique"] is None
    assert tables[1]["columns"] == [{"name": "visit_id", "type": "BIGINT"}]


def test_readme_overlay_is_embedded_when_present(tmp_path: Path) -> None:
    """The embedded config carries a present readme_overlay value verbatim."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)

    document = _build_document(
        emit_dir, anchor=None, windowed=None, readme_overlay="docs/overlay.md"
    )

    config_block = document["config"]
    assert isinstance(config_block, dict)
    assert config_block["readme_overlay"] == "docs/overlay.md"


def test_anchor_absent_is_null(tmp_path: Path) -> None:
    """No resolved anchor renders the manifest's `anchor` field as null."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)

    document = _build_document(emit_dir, anchor=None, windowed=None)

    assert document["anchor"] is None


def test_runtime_absent_is_null(tmp_path: Path) -> None:
    """An emit declaring no runtime block renders `emit.runtime` as null."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)

    document = _build_document(emit_dir, anchor=None, windowed=None)

    emit_block = document["emit"]
    assert isinstance(emit_block, dict)
    assert emit_block["runtime"] is None


# ---------------------------------------------------------------------------
# Field set — windowed
# ---------------------------------------------------------------------------


def test_windowed_next_carries_int_cursor(tmp_path: Path) -> None:
    """A --next window's incremental block carries regime, label, and an int
    next_window_index; table row_count is null."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)

    document = _build_document(
        emit_dir,
        anchor=None,
        windowed=WindowedArtifactState(
            regime="calendar", label="2024-01", next_window_index=3
        ),
        row_count=None,
    )

    assert document["incremental"] == {
        "regime": "calendar",
        "label": "2024-01",
        "next_window_index": 3,
    }
    tables = document["tables"]
    assert isinstance(tables, list)
    assert all(table["row_count"] is None for table in tables)


def test_windowed_range_carries_null_cursor(tmp_path: Path) -> None:
    """A --from/--to range's incremental block carries a null next_window_index."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)

    document = _build_document(
        emit_dir,
        anchor=None,
        windowed=WindowedArtifactState(
            regime="sim_time", label="0-100", next_window_index=None
        ),
        row_count=None,
    )

    assert document["incremental"] == {
        "regime": "sim_time",
        "label": "0-100",
        "next_window_index": None,
    }


# ---------------------------------------------------------------------------
# Pinned byte form
# ---------------------------------------------------------------------------


def test_byte_form_is_utf8_with_non_ascii_preserved(tmp_path: Path) -> None:
    """Non-ASCII survives as literal UTF-8 bytes, not a \\uXXXX escape."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)

    document = _build_document(emit_dir, anchor=None, windowed=None)
    rendered = render_manifest_bytes(document)

    assert "café".encode("utf-8") in rendered
    assert b"\\u00e9" not in rendered
    assert json.loads(rendered.decode("utf-8")) == document


def test_byte_form_uses_two_space_indent(tmp_path: Path) -> None:
    """Nested keys are indented with two spaces."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)

    document = _build_document(emit_dir, anchor=None, windowed=None)
    text = render_manifest_bytes(document).decode("utf-8")

    assert '\n  "anchor"' in text


def test_byte_form_sorts_top_level_keys(tmp_path: Path) -> None:
    """Top-level object keys render in sorted order."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)

    document = _build_document(emit_dir, anchor=None, windowed=None)
    text = render_manifest_bytes(document).decode("utf-8")

    ordered_keys = [
        "anchor",
        "config",
        "emit",
        "forge_version",
        "format",
        "incremental",
        "manifest_format_version",
        "mode",
        "tables",
    ]
    positions = [_top_level_key_index(text, key) for key in ordered_keys]
    assert positions == sorted(positions)


def test_byte_form_preserves_list_order(tmp_path: Path) -> None:
    """`tables` and `columns` list order is preserved, not sorted."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)

    document = _build_document(emit_dir, anchor=None, windowed=None)
    text = render_manifest_bytes(document).decode("utf-8")

    assert text.index('"name": "café"') < text.index('"name": "visits"')
    assert text.index('"name": "id"') < text.index('"name": "name"')


def test_byte_form_ends_with_single_trailing_newline(tmp_path: Path) -> None:
    """The rendered bytes end in exactly one trailing newline."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)

    document = _build_document(emit_dir, anchor=None, windowed=None)
    rendered = render_manifest_bytes(document)

    assert rendered.endswith(b"}\n")
    assert not rendered.endswith(b"}\n\n")


def test_two_renders_are_byte_identical(tmp_path: Path) -> None:
    """Two renders of the same inputs are byte-identical."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)

    first = render_manifest_bytes(_build_document(emit_dir, anchor=None, windowed=None))
    second = render_manifest_bytes(
        _build_document(emit_dir, anchor=None, windowed=None)
    )

    assert first == second
