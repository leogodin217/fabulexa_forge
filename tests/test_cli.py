"""Tests for the fabexport CLI (Phase 6).

Covers:
- validate on a conforming emit: exit 0, per-check PASS summary
- validate on a non-conforming emit (C4 wrong history type): non-zero exit, failing check printed
- validate on wrong_version emit: non-zero exit, UnsupportedBaseFormatVersionError
- validate on a missing directory: non-zero exit, EmitNotFoundError
- [project.scripts] fabexport entry resolves to cli:main
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from reader._fixtures_build import (
    build_c4_wrong_history_type,
    build_spanning,
    build_wrong_version,
)

from fabulexa_export.cli import main
from fabulexa_export.reader.errors import (
    UnsupportedBaseFormatVersionError,
)

# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def cli_fixtures(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Build the fixtures needed by CLI tests once per session."""
    root = tmp_path_factory.mktemp("cli_fixtures")
    result: dict[str, Path] = {}
    for name, builder in [
        ("spanning", build_spanning),
        ("c4_wrong_history_type", build_c4_wrong_history_type),
        ("wrong_version", build_wrong_version),
    ]:
        p = root / name
        builder(p)
        result[name] = p
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_validate_conforming_exits_zero(
    cli_fixtures: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """main(['validate', spanning]) returns 0 and prints per-check PASS summary."""
    exit_code = main(["validate", str(cli_fixtures["spanning"])])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "PASS" in captured.out
    # Each of C1–C11 should appear in output
    for i in range(1, 12):
        assert f"C{i}" in captured.out


def test_validate_nonconforming_exits_nonzero(
    cli_fixtures: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """main(['validate', c4_wrong_history_type]) exits non-zero with failing check printed."""
    exit_code = main(["validate", str(cli_fixtures["c4_wrong_history_type"])])
    captured = capsys.readouterr()
    assert exit_code != 0
    # C4 should appear as FAIL
    assert "C4" in captured.out
    assert "FAIL" in captured.out


def test_validate_wrong_version_exits_nonzero(
    cli_fixtures: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """main(['validate', wrong_version]) exits non-zero with UnsupportedBaseFormatVersionError."""
    exit_code = main(["validate", str(cli_fixtures["wrong_version"])])
    captured = capsys.readouterr()
    assert exit_code != 0
    # The error message from UnsupportedBaseFormatVersionError should appear
    exc = UnsupportedBaseFormatVersionError(999)
    assert str(exc) in captured.err or "unsupported" in captured.err.lower()


def test_validate_missing_dir_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main(['validate', missing_dir]) exits non-zero with EmitNotFoundError."""
    missing = tmp_path / "does_not_exist"
    exit_code = main(["validate", str(missing)])
    captured = capsys.readouterr()
    assert exit_code != 0
    # EmitNotFoundError message should appear in stderr
    assert "not found" in captured.err.lower() or "emit" in captured.err.lower()


def test_console_script_entry_resolves() -> None:
    """fabexport console script entry point resolves to cli:main."""
    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)
    scripts = data["project"]["scripts"]
    assert scripts["fabexport"] == "fabulexa_export.cli:main"
