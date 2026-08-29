"""Tests for the companion manifest builder: field set (build_manifest_document)
and pinned byte serialization (render_manifest_bytes)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from exporters.companion._fixtures import (
    ACTOR_TABLE_DESCRIPTION,
    SCENARIO_DESCRIPTION,
    documented_actor_table_report,
    write_documented_emit,
    write_minimal_emit,
)
from fabulexa_forge import __version__
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import ExportConfig
from fabulexa_forge.exporters.companion.artifacts import WindowedArtifactState
from fabulexa_forge.exporters.companion.manifest import (
    build_manifest_document,
    render_manifest_bytes,
)
from fabulexa_forge.exporters.query_spec import (
    ColumnProvenance,
    ExportReport,
    TableKeys,
    TableReport,
)
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

    assert document["manifest_format_version"] == 2
    assert document["mode"] == "base"
    assert document["format"] == "csv"
    assert document["forge_version"] == __version__
    assert document["incremental"] is None
    assert document["scenario_description"] is None

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
    assert tables[0]["description"] is None
    assert tables[1]["primary_key"] is None
    assert tables[1]["unique"] is None
    assert tables[1]["description"] is None
    assert tables[1]["columns"] == [
        {
            "name": "visit_id",
            "type": "BIGINT",
            "description": None,
            "unit": None,
            "enum_options": None,
        }
    ]


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
# Data dictionary resolution
# ---------------------------------------------------------------------------


def _build_documented_document(
    emit_dir: Path,
    *,
    windowed: WindowedArtifactState | None = None,
    row_count: int | None = 1,
) -> dict[str, object]:
    """Build a manifest document over `write_documented_emit`'s fixture, one
    `actor_state` table carrying every dictionary resolution rule."""
    with open_emit(emit_dir) as emit:
        return build_manifest_document(
            emit=emit,
            config=ExportConfig(mode="base"),
            fmt="csv",
            anchor=None,
            report=ExportReport(
                tables=(documented_actor_table_report(row_count=row_count),)
            ),
            windowed=windowed,
        )


def _documented_columns(document: dict[str, object]) -> dict[str, dict[str, object]]:
    """The documented fixture's single table's columns, keyed by name."""
    tables = document["tables"]
    assert isinstance(tables, list)
    columns = tables[0]["columns"]
    assert isinstance(columns, list)
    return {column["name"]: column for column in columns}


def test_top_level_scenario_description_forwarded(tmp_path: Path) -> None:
    """The manifest's top-level scenario_description carries the sidecar's
    narrative verbatim."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_documented_emit(emit_dir)

    document = _build_documented_document(emit_dir)

    assert document["scenario_description"] == SCENARIO_DESCRIPTION


def test_table_description_forwarded(tmp_path: Path) -> None:
    """A table whose carried columns agree on one source table forwards that
    source's description."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_documented_emit(emit_dir)

    document = _build_documented_document(emit_dir)

    tables = document["tables"]
    assert isinstance(tables, list)
    assert tables[0]["description"] == ACTOR_TABLE_DESCRIPTION


def test_column_documentation_mirror_matches_readme_resolution(
    tmp_path: Path,
) -> None:
    """Per-column description/unit/enum_options mirror the same resolution
    rules the README renders: description-only, description+unit, an
    undocumented carried column, and the ns-unit temporal-drop pair."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_documented_emit(emit_dir)

    columns = _documented_columns(_build_documented_document(emit_dir))

    assert columns["full_name"]["description"] == "Staff member's full legal name."
    assert columns["full_name"]["unit"] is None
    assert columns["shift_minutes"]["description"] == "Length of the current shift."
    assert columns["shift_minutes"]["unit"] == "minutes"
    assert columns["team_id"]["description"] is None
    assert columns["team_id"]["unit"] is None
    assert columns["created_sim_time"]["unit"] == "ns"
    assert columns["created_at"]["unit"] is None
    assert (
        columns["created_at"]["description"]
        == columns["created_sim_time"]["description"]
    )


def test_closed_domain_column_enum_options(tmp_path: Path) -> None:
    """A closed-domain column's enum_options carries its declared values,
    glosses verbatim, in sidecar order."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_documented_emit(emit_dir)

    columns = _documented_columns(_build_documented_document(emit_dir))

    assert columns["status"]["enum_options"] == [
        {"value": "A", "description": "Active and on duty."},
        {"value": "I", "description": "Inactive; off duty."},
    ]
    assert columns["full_name"]["enum_options"] is None


def test_table_spanning_multiple_source_tables_forwards_no_description(
    tmp_path: Path,
) -> None:
    """A table whose carried columns span more than one source table forwards
    no description -- there is no single subject to attribute it to."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_documented_emit(emit_dir)

    with open_emit(emit_dir) as emit:
        report = ExportReport(
            tables=(
                TableReport(
                    name="mixed",
                    columns=(("a", "VARCHAR"), ("b", "VARCHAR")),
                    row_count=1,
                    keys=None,
                    provenance={
                        "a": ColumnProvenance("records__actor", "prop__full_name"),
                        "b": ColumnProvenance("records__team", "prop__team_name"),
                    },
                    kind_values={},
                ),
            )
        )
        document = build_manifest_document(
            emit=emit,
            config=ExportConfig(mode="base"),
            fmt="csv",
            anchor=None,
            report=report,
            windowed=None,
        )

    tables = document["tables"]
    assert isinstance(tables, list)
    assert tables[0]["description"] is None


def test_documentation_identical_across_windows(tmp_path: Path) -> None:
    """A windowed incremental run renders identical documentation-bearing
    fields regardless of the window -- only `incremental` and `row_count`
    vary."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_documented_emit(emit_dir)

    first = _build_documented_document(
        emit_dir,
        windowed=WindowedArtifactState(
            regime="calendar", label="2024-01", next_window_index=1
        ),
        row_count=None,
    )
    second = _build_documented_document(
        emit_dir,
        windowed=WindowedArtifactState(
            regime="calendar", label="2024-02", next_window_index=2
        ),
        row_count=None,
    )

    assert first["scenario_description"] == second["scenario_description"]
    first_tables = first["tables"]
    second_tables = second["tables"]
    assert isinstance(first_tables, list)
    assert isinstance(second_tables, list)
    assert first_tables[0]["description"] == second_tables[0]["description"]
    assert first_tables[0]["columns"] == second_tables[0]["columns"]


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
        "scenario_description",
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
