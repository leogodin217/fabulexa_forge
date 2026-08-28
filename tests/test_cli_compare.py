"""Tests for `fabulexa-forge compare` CLI verb.

Covers:
- equal pair -> exit 0, text report on stdout, empty stderr
- unequal pair -> exit 1, report still on stdout
- input error (missing expected file) -> exit 2, message on stderr, no report
- --tables narrows the comparison
- --max-row-diffs 0 accepted; listings empty, totals present
- --format json -> parseable JSON on stdout; --format text is the default
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
