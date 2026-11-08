"""Tests for the compare surface's manifest row-count diff
(`fabulexa_forge.compare.manifests`).

Covers:
- every status: unchanged, changed (signed delta), appeared, disappeared, zeroed
- `zeroed` verdict is True iff some table went from rows to none
- a table appearing with zero rows is `appeared`, not `zeroed`
- tables are ordered by name across the union of both sides
- JSON rendering is byte-stable and mirrors the dataclass shape
- text rendering carries the verdict line and one line per table
- malformed input (unreadable file, no `tables`, bad entry) -> CompareInputError
- only `tables[].name` / `tables[].row_count` are read
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabulexa_forge.compare import (
    CompareInputError,
    diff_manifest_row_counts,
    render_row_count_diff_json,
    render_row_count_diff_text,
)


def _manifest(path: Path, counts: dict[str, int]) -> Path:
    document = {
        "manifest_format_version": 5,
        "mode": "dimensional",
        "tables": [{"name": name, "row_count": n} for name, n in counts.items()],
    }
    path.write_text(json.dumps(document))
    return path


def test_every_status_and_ordering(tmp_path: Path) -> None:
    previous = _manifest(
        tmp_path / "prev.json",
        {"dim_a": 10, "fact_b": 100, "fact_crew": 40, "old": 7},
    )
    fresh = _manifest(
        tmp_path / "fresh.json",
        {"dim_a": 10, "fact_b": 90, "fact_crew": 0, "new": 0},
    )

    diff = diff_manifest_row_counts(previous, fresh)

    assert [t.table for t in diff.tables] == [
        "dim_a",
        "fact_b",
        "fact_crew",
        "new",
        "old",
    ]
    by_name = {t.table: t for t in diff.tables}
    assert by_name["dim_a"].status == "unchanged" and by_name["dim_a"].delta == 0
    assert by_name["fact_b"].status == "changed" and by_name["fact_b"].delta == -10
    assert by_name["fact_crew"].status == "zeroed" and by_name["fact_crew"].delta == -40
    assert by_name["new"].status == "appeared"
    assert by_name["new"].previous is None and by_name["new"].delta is None
    assert by_name["old"].status == "disappeared"
    assert by_name["old"].fresh is None and by_name["old"].delta is None
    assert diff.zeroed is True


def test_no_zeroed_table_means_no_verdict_failure(tmp_path: Path) -> None:
    previous = _manifest(tmp_path / "prev.json", {"dim_a": 10, "fact_b": 100})
    fresh = _manifest(tmp_path / "fresh.json", {"dim_a": 12, "fact_b": 100, "new": 0})

    diff = diff_manifest_row_counts(previous, fresh)

    assert diff.zeroed is False
    statuses = {t.table: t.status for t in diff.tables}
    assert statuses == {"dim_a": "changed", "fact_b": "unchanged", "new": "appeared"}


def test_json_render_is_byte_stable_and_mirrors_shape(tmp_path: Path) -> None:
    previous = _manifest(tmp_path / "prev.json", {"fact_b": 100})
    fresh = _manifest(tmp_path / "fresh.json", {"fact_b": 0})

    diff = diff_manifest_row_counts(previous, fresh)
    rendered = render_row_count_diff_json(diff)

    assert rendered == render_row_count_diff_json(
        diff_manifest_row_counts(previous, fresh)
    )
    parsed = json.loads(rendered)
    assert parsed["zeroed"] is True
    assert parsed["tables"] == [
        {
            "table": "fact_b",
            "previous": 100,
            "fresh": 0,
            "delta": -100,
            "status": "zeroed",
        }
    ]
    assert parsed["previous"] == str(previous) and parsed["fresh"] == str(fresh)


def test_text_render_verdict_and_table_lines(tmp_path: Path) -> None:
    previous = _manifest(tmp_path / "prev.json", {"dim_a": 10, "old": 1})
    fresh = _manifest(tmp_path / "fresh.json", {"dim_a": 13})

    text = render_row_count_diff_text(diff_manifest_row_counts(previous, fresh))
    lines = text.splitlines()

    assert lines[0] == "NO TABLE WENT TO ZERO"
    assert lines[1] == "  dim_a: 10 -> 13 (+3) changed"
    assert lines[2] == "  old: 1 -> - disappeared"


def test_zeroed_text_verdict(tmp_path: Path) -> None:
    previous = _manifest(tmp_path / "prev.json", {"fact_b": 5})
    fresh = _manifest(tmp_path / "fresh.json", {"fact_b": 0})

    text = render_row_count_diff_text(diff_manifest_row_counts(previous, fresh))

    assert text.splitlines()[0] == "ZEROED"


def test_missing_file_is_a_compare_input_error(tmp_path: Path) -> None:
    fresh = _manifest(tmp_path / "fresh.json", {"fact_b": 5})
    with pytest.raises(CompareInputError, match="previous manifest is not readable"):
        diff_manifest_row_counts(tmp_path / "absent.json", fresh)


def test_manifest_without_tables_is_a_compare_input_error(tmp_path: Path) -> None:
    previous = _manifest(tmp_path / "prev.json", {"fact_b": 5})
    fresh = tmp_path / "fresh.json"
    fresh.write_text(json.dumps({"mode": "dimensional"}))
    with pytest.raises(CompareInputError, match="fresh manifest has no `tables` list"):
        diff_manifest_row_counts(previous, fresh)


def test_entry_without_integer_row_count_is_a_compare_input_error(
    tmp_path: Path,
) -> None:
    previous = _manifest(tmp_path / "prev.json", {"fact_b": 5})
    fresh = tmp_path / "fresh.json"
    fresh.write_text(json.dumps({"tables": [{"name": "fact_b", "row_count": "5"}]}))
    with pytest.raises(CompareInputError, match="integer `row_count`"):
        diff_manifest_row_counts(previous, fresh)


def test_only_name_and_row_count_are_read(tmp_path: Path) -> None:
    """A manifest with extra per-table fields and no other top-level fields diffs
    cleanly: the diff depends on nothing but the two fields it names."""
    previous = tmp_path / "prev.json"
    previous.write_text(
        json.dumps(
            {"tables": [{"name": "t", "row_count": 1, "columns": [], "extra": 1}]}
        )
    )
    fresh = tmp_path / "fresh.json"
    fresh.write_text(json.dumps({"tables": [{"name": "t", "row_count": 1}]}))

    diff = diff_manifest_row_counts(previous, fresh)

    assert diff.tables[0].status == "unchanged"
