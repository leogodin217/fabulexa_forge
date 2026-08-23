"""Tests for the companion README renderer: the ordering contract
(render_readme) and a per-mode template smoke assertion."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from exporters.companion._fixtures import write_minimal_emit
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.exporters.companion import readme as readme_module
from fabulexa_forge.exporters.companion.overlay import ReadmeOverlay
from fabulexa_forge.exporters.companion.readme import render_readme
from fabulexa_forge.exporters.query_spec import ExportReport, TableKeys, TableReport
from fabulexa_forge.reader.emit import open_emit

# ---------------------------------------------------------------------------
# Fakes -- mirrors tests/reader/test_schema_loader.py's convention
# ---------------------------------------------------------------------------


class _UnreadableRef:
    """A traversable-like ref whose read_text always raises."""

    def __init__(self, exc_type: type[Exception]) -> None:
        self._exc_type = exc_type

    def __truediv__(self, other: str) -> "_UnreadableRef":
        return self

    def read_text(self, encoding: str = "utf-8") -> str:
        raise self._exc_type("resource not readable")


class _NowherePath:
    """A Path-like whose parent/joins resolve to itself and which never exists."""

    def __init__(self, _path: str) -> None:
        pass

    @property
    def parent(self) -> "_NowherePath":
        return self

    def __truediv__(self, other: str) -> "_NowherePath":
        return self

    def exists(self) -> bool:
        return False


_ANCHOR = EffectiveAnchor(
    start_instant=datetime(2024, 1, 1, tzinfo=timezone.utc), timezone=ZoneInfo("UTC")
)


def _two_table_report(*, row_count: int | None) -> ExportReport:
    """A keyed 'patients' table and a keyless 'visits' table, in this order."""
    return ExportReport(
        tables=(
            TableReport(
                name="patients",
                columns=(("id", "BIGINT"), ("mrn", "VARCHAR")),
                row_count=row_count,
                keys=TableKeys(primary_key=("id",), unique=(("mrn",),)),
            ),
            TableReport(
                name="visits",
                columns=(("visit_id", "BIGINT"),),
                row_count=row_count,
                keys=None,
            ),
        )
    )


def _render(
    tmp_path: Path,
    *,
    overlay: ReadmeOverlay | None,
    anchor: EffectiveAnchor | None,
    row_count: int | None = 1,
) -> str:
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)
    with open_emit(emit_dir) as emit:
        return render_readme(
            mode="base",
            emit=emit,
            report=_two_table_report(row_count=row_count),
            overlay=overlay,
            anchor=anchor,
            manifest_filename="base-manifest.json",
        )


# ---------------------------------------------------------------------------
# Ordering contract
# ---------------------------------------------------------------------------


def test_ordering_contract_full_sequence(tmp_path: Path) -> None:
    """Title+marker -> overview -> template prose -> per-table sections in
    report order -> anchor facts -> emit identity, in that order."""
    overlay = ReadmeOverlay(
        overview="Nightly extract.", table_notes={"patients": "One row per patient."}
    )
    text = _render(tmp_path, overlay=overlay, anchor=_ANCHOR)

    title_index = text.index("# Base Export")
    marker_index = text.index("base-manifest.json")
    overview_index = text.index("## Overview")
    template_index = text.index("## Reading this export")
    patients_index = text.index("### patients")
    visits_index = text.index("### visits")
    anchor_index = text.index("## Anchor")
    identity_index = text.index("## Emit Identity")

    assert (
        title_index
        < marker_index
        < overview_index
        < template_index
        < patients_index
        < visits_index
        < anchor_index
        < identity_index
    )


def test_table_with_overlay_note_renders_it(tmp_path: Path) -> None:
    """A table naming an overlay slot renders that slot's note in its section."""
    overlay = ReadmeOverlay(
        overview=None, table_notes={"patients": "One row per patient."}
    )
    text = _render(tmp_path, overlay=overlay, anchor=None)

    patients_section = text[text.index("### patients") : text.index("### visits")]
    assert "One row per patient." in patients_section


def test_table_without_overlay_slot_has_no_placeholder(tmp_path: Path) -> None:
    """A table naming no overlay slot renders derived facts only -- no
    placeholder text stands in for the absent note."""
    overlay = ReadmeOverlay(
        overview=None, table_notes={"patients": "One row per patient."}
    )
    text = _render(tmp_path, overlay=overlay, anchor=None)

    visits_section = text[text.index("### visits") :]
    assert "Columns:" in visits_section
    assert "visit_id" in visits_section
    assert "One row per patient." not in visits_section
    assert "None" not in visits_section


def test_column_key_markings(tmp_path: Path) -> None:
    """A primary-key column is marked '[primary key]'; a unique column
    '[unique]'; an undeclared column carries neither marking."""
    text = _render(tmp_path, overlay=None, anchor=None)

    patients_section = text[text.index("### patients") : text.index("### visits")]
    assert "`id` (BIGINT) [primary key]" in patients_section
    assert "`mrn` (VARCHAR) [unique]" in patients_section

    visits_section = text[text.index("### visits") :]
    assert (
        "`visit_id` (BIGINT)\n" in visits_section
        or visits_section.rstrip().endswith("`visit_id` (BIGINT)")
    )


def test_row_count_rendered_on_full_export(tmp_path: Path) -> None:
    """A full export's non-null row_count renders a 'Row count:' line."""
    text = _render(tmp_path, overlay=None, anchor=None, row_count=42)
    assert "Row count: 42" in text


def test_row_count_absent_on_windowed_export(tmp_path: Path) -> None:
    """A windowed export's null row_count renders no 'Row count:' line."""
    text = _render(tmp_path, overlay=None, anchor=None, row_count=None)
    assert "Row count:" not in text


def test_anchor_present_renders_start_instant_and_timezone(tmp_path: Path) -> None:
    """A resolved anchor renders its start instant and timezone."""
    text = _render(tmp_path, overlay=None, anchor=_ANCHOR)
    assert "Start instant: 2024-01-01T00:00:00+00:00" in text
    assert "Timezone: UTC" in text


def test_anchor_absent_renders_no_anchor_notice(tmp_path: Path) -> None:
    """No resolved anchor renders the 'no wallclock anchor' notice."""
    text = _render(tmp_path, overlay=None, anchor=None)
    assert "No wallclock anchor was resolved" in text


def test_overview_absent_when_overlay_has_none(tmp_path: Path) -> None:
    """An overlay with no overview renders no '## Overview' section."""
    overlay = ReadmeOverlay(overview=None, table_notes={})
    text = _render(tmp_path, overlay=overlay, anchor=None)
    assert "## Overview" not in text


def test_overview_absent_when_overlay_is_none(tmp_path: Path) -> None:
    """No overlay at all renders no '## Overview' section."""
    text = _render(tmp_path, overlay=None, anchor=None)
    assert "## Overview" not in text


# ---------------------------------------------------------------------------
# _load_mode_template resolution paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_type",
    [
        pytest.param(FileNotFoundError, id="file-not-found"),
        pytest.param(TypeError, id="type-error"),
    ],
)
def test_load_mode_template_falls_back_when_package_data_unreadable(
    monkeypatch: pytest.MonkeyPatch, exc_type: type[Exception]
) -> None:
    """When importlib.resources fails, the __file__-relative fallback resolves.

    Both documented failure modes of the package-data read (FileNotFoundError
    and TypeError) route to the fallback, which finds the in-tree templates/
    copy in this editable checkout.
    """
    monkeypatch.setattr(
        "importlib.resources.files", lambda package: _UnreadableRef(exc_type)
    )
    text = readme_module._load_mode_template("base")
    assert "State-at-horizon" in text


def test_load_mode_template_both_paths_fail_raises_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When package data AND the fallback path fail, FileNotFoundError surfaces.

    This is the explicit packaging-defect signal: never swallowed, never
    reported as an export failure.
    """
    monkeypatch.setattr(
        "importlib.resources.files",
        lambda package: _UnreadableRef(FileNotFoundError),
    )
    monkeypatch.setattr(readme_module, "Path", _NowherePath)
    with pytest.raises(FileNotFoundError, match="packaging defect"):
        readme_module._load_mode_template("base")


# ---------------------------------------------------------------------------
# Per-mode template smoke assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected_terms"),
    [
        ("dimensional", ("star-schema", "SCD-2")),
        ("source", ("Junction tables", "changes")),
        ("base", ("State-at-horizon", "Record-index")),
    ],
)
def test_each_template_mentions_its_mode_semantics(
    tmp_path: Path, mode: str, expected_terms: tuple[str, str]
) -> None:
    """Each mode's rendered README mentions its shape's defining semantics."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)
    with open_emit(emit_dir) as emit:
        text = render_readme(
            mode=mode,
            emit=emit,
            report=ExportReport(tables=()),
            overlay=None,
            anchor=None,
            manifest_filename=f"{mode}-manifest.json",
        )
    for term in expected_terms:
        assert term in text
