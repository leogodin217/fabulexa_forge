"""Tests for the H2-slot README overlay grammar: load_readme_overlay and
validate_overlay_tables."""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulexa_forge.errors import ReadmeOverlayInvalid, ReadmeOverlayUnknownTable
from fabulexa_forge.exporters.companion.overlay import (
    ReadmeOverlay,
    load_readme_overlay,
    validate_overlay_tables,
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "overlay.md"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# load_readme_overlay — happy path
# ---------------------------------------------------------------------------


def test_overview_and_two_table_slots_parse(tmp_path: Path) -> None:
    """Overview + two table: slots parse into the right ReadmeOverlay; bodies
    are verbatim with leading/trailing blank lines trimmed."""
    path = _write(
        tmp_path,
        "## overview\n"
        "\n"
        "Nightly extract of the clinic's database.\n"
        "\n"
        "## table: patients\n"
        "One row per patient.\n"
        "\n"
        "## table: ward_events\n"
        "The polymorphic event log.\n",
    )
    overlay = load_readme_overlay(path)
    assert overlay == ReadmeOverlay(
        overview="Nightly extract of the clinic's database.",
        table_notes={
            "patients": "One row per patient.",
            "ward_events": "The polymorphic event log.",
        },
    )


def test_body_keeps_interior_blank_lines(tmp_path: Path) -> None:
    """Interior blank lines in a slot body are kept verbatim."""
    path = _write(
        tmp_path,
        "## overview\n\nFirst paragraph.\n\nSecond paragraph.\n",
    )
    overlay = load_readme_overlay(path)
    assert overlay.overview == "First paragraph.\n\nSecond paragraph."


def test_h3_heading_inside_body_stays_in_body(tmp_path: Path) -> None:
    """An H3+ heading inside a slot body does not start a new slot."""
    path = _write(
        tmp_path,
        "## table: patients\n"
        "Some prose.\n"
        "\n"
        "### Caveats\n"
        "Only active patients.\n"
        "\n"
        "## table: ward_events\n"
        "Log prose.\n",
    )
    overlay = load_readme_overlay(path)
    assert overlay.table_notes["patients"] == (
        "Some prose.\n\n### Caveats\nOnly active patients."
    )
    assert overlay.table_notes["ward_events"] == "Log prose."


def test_empty_body_is_legal(tmp_path: Path) -> None:
    """A slot with an empty body parses without error."""
    path = _write(tmp_path, "## table: patients\n")
    overlay = load_readme_overlay(path)
    assert overlay.table_notes["patients"] == ""


# ---------------------------------------------------------------------------
# load_readme_overlay — grammar rejections
# ---------------------------------------------------------------------------


def test_content_before_first_heading_rejected(tmp_path: Path) -> None:
    """Free-floating prose before the first H2 is rejected."""
    path = _write(tmp_path, "Some intro text.\n\n## overview\nBody.\n")
    with pytest.raises(ReadmeOverlayInvalid, match="content before"):
        load_readme_overlay(path)


@pytest.mark.parametrize(
    "heading",
    ["## Overview", "## table:x", "## table:  x", "## table: "],
)
def test_malformed_heading_rejected(tmp_path: Path, heading: str) -> None:
    """A heading matching neither slot form is rejected, naming the heading."""
    path = _write(tmp_path, f"{heading}\nBody.\n")
    with pytest.raises(ReadmeOverlayInvalid, match="heading"):
        load_readme_overlay(path)


def test_duplicate_overview_slot_rejected(tmp_path: Path) -> None:
    """A second 'overview' slot is a duplicate-key error."""
    path = _write(tmp_path, "## overview\nA.\n\n## overview\nB.\n")
    with pytest.raises(ReadmeOverlayInvalid, match="duplicate slot 'overview'"):
        load_readme_overlay(path)


def test_duplicate_table_slot_rejected(tmp_path: Path) -> None:
    """A second 'table: <name>' slot with the same name is a duplicate-key error."""
    path = _write(tmp_path, "## table: patients\nA.\n\n## table: patients\nB.\n")
    with pytest.raises(ReadmeOverlayInvalid, match="duplicate slot 'table: patients'"):
        load_readme_overlay(path)


def test_missing_file_rejected(tmp_path: Path) -> None:
    """A nonexistent overlay path is rejected."""
    with pytest.raises(ReadmeOverlayInvalid):
        load_readme_overlay(tmp_path / "does-not-exist.md")


def test_non_utf8_bytes_rejected(tmp_path: Path) -> None:
    """Non-UTF-8 bytes are rejected."""
    path = tmp_path / "overlay.md"
    path.write_bytes(b"## overview\n\xff\xfe not utf-8\n")
    with pytest.raises(ReadmeOverlayInvalid, match="UTF-8"):
        load_readme_overlay(path)


# ---------------------------------------------------------------------------
# validate_overlay_tables
# ---------------------------------------------------------------------------


def test_unknown_table_slot_rejected() -> None:
    """A table: slot naming an absent table is rejected, naming the slot and
    listing the plan's tables."""
    overlay = ReadmeOverlay(overview=None, table_notes={"ghosts": "prose"})
    with pytest.raises(ReadmeOverlayUnknownTable, match="ghosts") as excinfo:
        validate_overlay_tables(overlay, ["patients", "ward_events"])
    assert "patients" in str(excinfo.value)
    assert "ward_events" in str(excinfo.value)


def test_all_known_slots_pass() -> None:
    """An overlay whose table slots all name plan tables passes."""
    overlay = ReadmeOverlay(overview="Intro.", table_notes={"patients": "prose"})
    validate_overlay_tables(overlay, ["patients", "ward_events"])


def test_overlay_with_no_table_notes_passes_any_plan() -> None:
    """An overlay with no table notes passes against any plan."""
    overlay = ReadmeOverlay(overview="Intro.", table_notes={})
    validate_overlay_tables(overlay, [])
