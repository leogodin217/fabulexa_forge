"""Tests for the companion README renderer: the ordering contract
(render_readme) and a per-mode template smoke assertion."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from exporters.companion._fixtures import (
    documented_actor_table_report,
    history_interval_table_report,
    structural_identity_table_report,
    value_mapped_table_report,
    write_documented_emit,
    write_minimal_emit,
)
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
                provenance={},
                kind_values={},
                author_descriptions={},
                author_table_description=None,
                event_log=False,
            ),
            TableReport(
                name="visits",
                columns=(("visit_id", "BIGINT"),),
                row_count=row_count,
                keys=None,
                provenance={},
                kind_values={},
                author_descriptions={},
                author_table_description=None,
                event_log=False,
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
# Data dictionary -- table section order + column/gloss resolution
# ---------------------------------------------------------------------------


def _render_documented(
    tmp_path: Path,
    *,
    overlay: ReadmeOverlay | None,
    row_count: int | None = 1,
    emit_subdir: str = "emit",
) -> str:
    """Render the documented fixture's single `actor_state` table section."""
    emit_dir = tmp_path / emit_subdir
    emit_dir.mkdir()
    write_documented_emit(emit_dir)
    with open_emit(emit_dir) as emit:
        return render_readme(
            mode="source",
            emit=emit,
            report=ExportReport(
                tables=(documented_actor_table_report(row_count=row_count),)
            ),
            overlay=overlay,
            anchor=None,
            manifest_filename="source-manifest.json",
        )


def _column_line(text: str, name: str) -> str:
    """The single column-inventory line for `name` (`` - `<name>` ... ``)."""
    for line in text.splitlines():
        if line.startswith(f"- `{name}`"):
            return line
    raise AssertionError(f"no column inventory line for {name!r} in:\n{text}")


def test_table_section_order_overlay_note_then_description_then_columns_then_glosses_then_row_count(
    tmp_path: Path,
) -> None:
    """One table section orders: overlay note -> table description -> column
    inventory -> declared-value gloss lists -> row count."""
    overlay = ReadmeOverlay(overview=None, table_notes={"actor_state": "A note."})
    text = _render_documented(tmp_path, overlay=overlay)
    section = text[text.index("### actor_state") :]

    note_index = section.index("A note.")
    description_index = section.index("Hospital staff members.")
    columns_index = section.index("Columns:")
    glosses_index = section.index("Declared values:")
    row_count_index = section.index("Row count: 1")

    assert (
        note_index < description_index < columns_index < glosses_index < row_count_index
    )


def test_documented_column_line_shapes(tmp_path: Path) -> None:
    """Column inventory: description-only, description+unit, and
    undocumented (name/type only, no placeholder prose) column shapes."""
    text = _render_documented(tmp_path, overlay=None)

    assert (
        _column_line(text, "full_name")
        == "- `full_name` (VARCHAR): Staff member's full legal name."
    )
    assert (
        _column_line(text, "shift_minutes")
        == "- `shift_minutes` (DECIMAL(10,2)): Length of the current shift. [minutes]"
    )
    assert _column_line(text, "team_id") == "- `team_id` (VARCHAR)"


def test_structural_column_renders_contract_string_with_unit(tmp_path: Path) -> None:
    """A carried structural column renders the pinned contract string, unit
    kept where the rendering still counts as raw nanoseconds (an integer
    type)."""
    text = _render_documented(tmp_path, overlay=None)
    assert _column_line(text, "created_sim_time") == (
        "- `created_sim_time` (BIGINT): Simulation time the record was "
        "created. Set once; never changed by later writes or deactivation. "
        "[ns]"
    )


def test_temporal_rendering_drops_ns_unit_keeps_description(tmp_path: Path) -> None:
    """The same structural source rendered as a TIMESTAMPTZ (a temporal/
    instant rendering) drops the 'ns' unit but keeps the description."""
    text = _render_documented(tmp_path, overlay=None)
    assert _column_line(text, "created_at") == (
        "- `created_at` (TIMESTAMPTZ): Simulation time the record was "
        "created. Set once; never changed by later writes or deactivation."
    )


def test_non_ns_unit_kept_under_a_decimal_rendering(tmp_path: Path) -> None:
    """A non-'ns' unit (minutes) rides its rendering unchanged -- a decimal
    output type keeps both description and unit."""
    text = _render_documented(tmp_path, overlay=None)
    assert "[minutes]" in _column_line(text, "shift_minutes")


def test_closed_domain_column_renders_declared_value_glosses(tmp_path: Path) -> None:
    """A closed-domain column renders its declared values with glosses."""
    text = _render_documented(tmp_path, overlay=None)
    glosses = text[text.index("Declared values:") :]
    status_block = glosses[glosses.index("- `status`:") :]
    assert "- `A`: Active and on duty." in status_block
    assert "- `I`: Inactive; off duty." in status_block


def test_kind_name_as_value_column_glosses_present_and_absent(
    tmp_path: Path,
) -> None:
    """A kind-name-as-value column glosses each label from its source kind's
    table description; a label without prose (an undocumented kind) renders
    bare."""
    text = _render_documented(tmp_path, overlay=None)
    glosses = text[text.index("Declared values:") :]
    kind_block = glosses[glosses.index("- `kind`:") :]
    assert "- `Actor`: Hospital staff members." in kind_block
    assert "- `Team`\n" in kind_block or kind_block.rstrip().endswith("- `Team`")
    assert "- `Team`:" not in kind_block


def _render_report(tmp_path: Path, report: TableReport) -> str:
    """Render one arbitrary table report against the documented fixture emit."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_documented_emit(emit_dir)
    with open_emit(emit_dir) as emit:
        return render_readme(
            mode="source",
            emit=emit,
            report=ExportReport(tables=(report,)),
            overlay=None,
            anchor=None,
            manifest_filename="source-manifest.json",
        )


def test_history_interval_end_column_gets_end_of_validity_description(
    tmp_path: Path,
) -> None:
    """The [start, end) pair carried from history's sim_time /
    lead_sim_time documents each bound in its own words: the start keeps
    the contract's took-effect string, the end gets the forge-authored
    stopped-holding description -- never the start's prose duplicated."""
    text = _render_report(tmp_path, history_interval_table_report())

    assert _column_line(text, "entered_at") == (
        "- `entered_at` (TIMESTAMP): Simulation time the change took effect; "
        "the value holds until the series' next row."
    )
    assert _column_line(text, "exited_at") == (
        "- `exited_at` (TIMESTAMP): Simulation time the value stopped holding "
        "— the instant the series' next change took effect; NULL while the "
        "value is still current at the slice boundary."
    )


def test_structural_identity_strings_rewritten_for_export(tmp_path: Path) -> None:
    """The four pinned identity strings whose prose points at base-layer
    structure (a records__<kind> join target, the record_index column, the
    membership table-name segment, the sidecar) render with that pointer
    clause rewritten out; each string's factual core survives."""
    text = _render_report(tmp_path, structural_identity_table_report())
    section = text[text.index("### actor_identity") :]

    assert _column_line(section, "event_id") == (
        "- `event_id` (VARCHAR): Opaque identifier of the record within its "
        "branch and kind. Not ordered by creation."
    )
    assert _column_line(section, "public_id") == (
        "- `public_id` (VARCHAR): Presentation surrogate identity minted for this kind."
    )
    assert _column_line(section, "owner_id") == (
        "- `owner_id` (VARCHAR): Id of the record that owns the collection."
    )
    assert _column_line(section, "changed_id") == (
        "- `changed_id` (VARCHAR): Id of the record whose property changed. Opaque."
    )
    assert "record_index" not in section
    assert "records__" not in section
    assert "segment" not in section
    assert "sidecar" not in section


def test_structural_string_without_rewrite_stays_contract_verbatim(
    tmp_path: Path,
) -> None:
    """A pinned string outside the rewrite set (created_sim_time) still
    renders contract-verbatim -- the rewrite is an enumerated exception, not
    a general paraphrase pass."""
    text = _render_documented(tmp_path, overlay=None)
    assert _column_line(text, "created_sim_time") == (
        "- `created_sim_time` (BIGINT): Simulation time the record was "
        "created. Set once; never changed by later writes or deactivation. "
        "[ns]"
    )


def test_value_mapped_column_declares_post_map_values(tmp_path: Path) -> None:
    """A derived: value_map column's declared values are the post-map
    rendered values (glosses kept); a source option the map omits renders
    NULL and is dropped from the declared domain."""
    text = _render_report(tmp_path, value_mapped_table_report())
    glosses = text[text.index("Declared values:") :]
    status_block = glosses[glosses.index("- `status`:") :]

    assert "- `active`: Active and on duty." in status_block
    assert "`A`" not in status_block
    assert "Inactive" not in status_block


def test_two_renders_are_byte_identical(tmp_path: Path) -> None:
    """Two renders of the same documented inputs are byte-identical."""
    overlay = ReadmeOverlay(
        overview="Overlay prose.", table_notes={"actor_state": "A note."}
    )
    first = _render_documented(tmp_path, overlay=overlay, emit_subdir="emit1")
    second = _render_documented(tmp_path, overlay=overlay, emit_subdir="emit2")
    assert first == second


# ---------------------------------------------------------------------------
# Data dictionary -- author_descriptions override resolution
# ---------------------------------------------------------------------------


def test_override_on_carried_column_renders_author_prose_unit_still_inherited(
    tmp_path: Path,
) -> None:
    """An override on a carried column renders the author's prose while the
    source unit still inherits under today's unit rules."""
    report = documented_actor_table_report(
        author_descriptions={"shift_minutes": "Custom shift length."}
    )
    text = _render_report(tmp_path, report)
    assert (
        _column_line(text, "shift_minutes")
        == "- `shift_minutes` (DECIMAL(10,2)): Custom shift length. [minutes]"
    )


def test_override_plus_temporal_rendering_still_drops_ns_unit(
    tmp_path: Path,
) -> None:
    """An override on a column whose rendering left the raw-ns form behind
    still drops the unit -- the ns-unit stop is unaffected by the override."""
    report = documented_actor_table_report(
        author_descriptions={"created_at": "Custom creation label."}
    )
    text = _render_report(tmp_path, report)
    assert (
        _column_line(text, "created_at")
        == "- `created_at` (TIMESTAMPTZ): Custom creation label."
    )


def test_override_on_structural_column_wins_over_contract_rewrite(
    tmp_path: Path,
) -> None:
    """An override on a projected structural column wins outright -- the
    contract string and its export rewrite are never consulted."""
    report = structural_identity_table_report(
        author_descriptions={"event_id": "Custom identity note."}
    )
    text = _render_report(tmp_path, report)
    section = text[text.index("### actor_identity") :]
    assert (
        _column_line(section, "event_id")
        == "- `event_id` (VARCHAR): Custom identity note."
    )


def test_override_on_computed_column_with_no_provenance_renders_description_only(
    tmp_path: Path,
) -> None:
    """An override on a column with no carried provenance gives a
    description-only doc where today's resolution returns none."""
    report = TableReport(
        name="computed_facts",
        columns=(("computed_flag", "BOOLEAN"),),
        row_count=1,
        keys=None,
        provenance={},
        kind_values={},
        author_descriptions={"computed_flag": "A derived flag."},
        author_table_description=None,
        event_log=False,
    )
    text = _render_report(tmp_path, report)
    assert (
        _column_line(text, "computed_flag")
        == "- `computed_flag` (BOOLEAN): A derived flag."
    )


def test_override_on_history_interval_end_column_replaces_forge_authored_constant(
    tmp_path: Path,
) -> None:
    """An override on the history-interval end column replaces the
    forge-authored end-of-validity constant; the start column is unaffected."""
    report = history_interval_table_report(
        author_descriptions={"exited_at": "Custom exit description."}
    )
    text = _render_report(tmp_path, report)
    assert (
        _column_line(text, "exited_at")
        == "- `exited_at` (TIMESTAMP): Custom exit description."
    )
    assert _column_line(text, "entered_at") == (
        "- `entered_at` (TIMESTAMP): Simulation time the change took effect; "
        "the value holds until the series' next row."
    )


def test_override_on_value_mapped_column_renders_author_prose_enum_untouched(
    tmp_path: Path,
) -> None:
    """An override on a derived: value_map column renders the author's prose;
    the declared enum options stay the post-map list."""
    report = value_mapped_table_report(
        author_descriptions={"status": "Custom status label."}
    )
    text = _render_report(tmp_path, report)
    assert _column_line(text, "status") == "- `status` (VARCHAR): Custom status label."
    glosses = text[text.index("Declared values:") :]
    assert "- `active`: Active and on duty." in glosses


def test_override_present_while_source_carries_no_sidecar_documentation(
    tmp_path: Path,
) -> None:
    """An override still renders even when its carried column's source
    carries no sidecar documentation of its own."""
    report = documented_actor_table_report(
        author_descriptions={"team_id": "Foreign key to team."}
    )
    text = _render_report(tmp_path, report)
    assert (
        _column_line(text, "team_id") == "- `team_id` (VARCHAR): Foreign key to team."
    )


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
