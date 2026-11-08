"""Tests for `fabulexa-forge compare` CLI verb.

Covers:
- equal pair -> exit 0, text report on stdout, empty stderr
- unequal pair -> exit 1, report still on stdout
- input error (missing expected file) -> exit 2, message on stderr, no report
- --tables narrows the comparison
- --max-row-diffs 0 accepted; listings empty, totals present
- --format json -> parseable JSON on stdout; --format text is the default
- --manifests: row-count diff of two companion manifests -> exit 0 on deltas,
  1 when a table went to zero, 2 on unreadable input; --tables is rejected
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from compare._helpers import build_duckdb, write_csv_dir

from fabulexa_forge.cli import main

_EXPECTED_SQL = (
    "CREATE TABLE people (id BIGINT, name VARCHAR)",
    "INSERT INTO people VALUES (1, 'Ada'), (2, 'Bea')",
)


def test_equal_pair_exits_zero_with_report_on_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An equal actual side exits 0 with the report on stdout and empty stderr."""
    expected = build_duckdb(tmp_path / "expected.duckdb", _EXPECTED_SQL)
    actual = build_duckdb(tmp_path / "actual.duckdb", _EXPECTED_SQL)

    exit_code = main(["compare", str(expected), str(actual)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "EQUAL" in captured.out
    assert captured.err == ""


def test_unequal_pair_exits_one_with_report_on_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A differing actual side exits 1 with the discrepancy report on stdout."""
    expected = build_duckdb(tmp_path / "expected.duckdb", _EXPECTED_SQL)
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        (
            "CREATE TABLE people (id BIGINT, name VARCHAR)",
            "INSERT INTO people VALUES (1, 'Ada')",
        ),
    )

    exit_code = main(["compare", str(expected), str(actual)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "NOT EQUAL" in captured.out


def test_missing_expected_file_exits_two_with_message_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing expected path is a CompareInputError: exit 2, message on
    stderr, no report on stdout."""
    missing = tmp_path / "does-not-exist.duckdb"
    actual = build_duckdb(tmp_path / "actual.duckdb", _EXPECTED_SQL)

    exit_code = main(["compare", str(missing), str(actual)])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "ERROR" in captured.err
    assert captured.out == ""


def test_tables_flag_narrows_the_comparison(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An extra actual-side table outside the --tables selection no longer
    fails the verdict."""
    expected = build_duckdb(tmp_path / "expected.duckdb", _EXPECTED_SQL)
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        (
            *_EXPECTED_SQL,
            "CREATE TABLE extra (id BIGINT)",
        ),
    )

    exit_code = main(["compare", str(expected), str(actual), "--tables", "people"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "EQUAL" in captured.out


def test_max_row_diffs_zero_is_accepted_listings_empty_totals_present(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--max-row-diffs 0 is accepted; listings are empty but totals still show."""
    expected = build_duckdb(tmp_path / "expected.duckdb", _EXPECTED_SQL)
    actual = build_duckdb(
        tmp_path / "actual.duckdb",
        (
            "CREATE TABLE people (id BIGINT, name VARCHAR)",
            "INSERT INTO people VALUES (1, 'Ada')",
        ),
    )

    exit_code = main(["compare", str(expected), str(actual), "--max-row-diffs", "0"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "missing_total=1" in captured.out
    assert "missing: " not in captured.out


def test_format_json_produces_parseable_json_on_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--format json emits parseable JSON on stdout."""
    expected = build_duckdb(tmp_path / "expected.duckdb", _EXPECTED_SQL)
    actual = build_duckdb(tmp_path / "actual.duckdb", _EXPECTED_SQL)

    exit_code = main(["compare", str(expected), str(actual), "--format", "json"])
    captured = capsys.readouterr()

    assert exit_code == 0
    parsed = json.loads(captured.out)
    assert parsed["equal"] is True


def test_format_text_is_the_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Omitting --format renders the text report."""
    expected = build_duckdb(tmp_path / "expected.duckdb", _EXPECTED_SQL)
    actual = write_csv_dir(
        tmp_path / "actual_csv", {"people.csv": "id,name\n1,Ada\n2,Bea\n"}
    )

    exit_code = main(["compare", str(expected), str(actual)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out.startswith("EQUAL")


# --- `--manifests`: the row-count diff mode ---------------------------------


def _write_manifest(path: Path, counts: dict[str, int]) -> Path:
    path.write_text(
        json.dumps({"tables": [{"name": n, "row_count": c} for n, c in counts.items()]})
    )
    return path


def test_manifests_no_table_zeroed_exits_zero_with_deltas_on_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Row-count deltas are informational: a changed count exits 0, the report
    still lists the delta."""
    previous = _write_manifest(tmp_path / "prev.json", {"dim_a": 10})
    fresh = _write_manifest(tmp_path / "fresh.json", {"dim_a": 8})

    exit_code = main(["compare", "--manifests", str(previous), str(fresh)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "NO TABLE WENT TO ZERO" in captured.out
    assert "dim_a: 10 -> 8 (-2) changed" in captured.out
    assert captured.err == ""


def test_manifests_table_went_to_zero_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    previous = _write_manifest(tmp_path / "prev.json", {"fact_leg_crew": 40})
    fresh = _write_manifest(tmp_path / "fresh.json", {"fact_leg_crew": 0})

    exit_code = main(["compare", "--manifests", str(previous), str(fresh)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out.startswith("ZEROED")
    assert "fact_leg_crew: 40 -> 0 (-40) zeroed" in captured.out


def test_manifests_unreadable_input_exits_two_with_message_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fresh = _write_manifest(tmp_path / "fresh.json", {"dim_a": 1})

    exit_code = main(
        ["compare", "--manifests", str(tmp_path / "nope.json"), str(fresh)]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "ERROR" in captured.err
    assert captured.out == ""


def test_manifests_format_json_is_parseable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    previous = _write_manifest(tmp_path / "prev.json", {"dim_a": 1})
    fresh = _write_manifest(tmp_path / "fresh.json", {"dim_a": 1, "dim_b": 2})

    exit_code = main(
        ["compare", "--manifests", str(previous), str(fresh), "--format", "json"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    parsed = json.loads(captured.out)
    assert parsed["zeroed"] is False
    assert [t["status"] for t in parsed["tables"]] == ["unchanged", "appeared"]


def test_manifests_rejects_tables_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--tables` is a dataset-comparison selection; under `--manifests` it is a
    usage error (argparse exit 2)."""
    previous = _write_manifest(tmp_path / "prev.json", {"dim_a": 1})
    fresh = _write_manifest(tmp_path / "fresh.json", {"dim_a": 1})

    exit_code = main(
        ["compare", "--manifests", str(previous), str(fresh), "--tables", "dim_a"]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "--tables does not apply to --manifests" in captured.err
