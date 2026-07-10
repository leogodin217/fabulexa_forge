"""Tests for `fabulexa-forge corrupt` CLI verb.

Covers:
- happy path: exit 0; run.duckdb + base.json + defects.json written; per-operation
  report (kind, table, units selected/affected) on stdout
- missing emit dir -> ReaderError surfaces, exit 1, no traceback
- bad config (ConfigError) -> exit 1, no traceback
- business-rule failure (CorruptValidationError) -> exit 1, no traceback
- populated out dir -> CorruptValidationError, exit 1
- the manifest is always written -- no flag exists to suppress it (parser surface)
- main(["corrupt", ...]) dispatches correctly
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from reader._fixtures_build import build_spanning

from fabulexa_forge.cli import cmd_corrupt, main
from fabulexa_forge.config.models import Amount, CorruptConfig, NullCells, Target


def _write_config(config_path: Path, config: CorruptConfig) -> None:
    """Write a CorruptConfig to YAML."""
    config_path.write_text(
        yaml.dump(json.loads(config.model_dump_json()), allow_unicode=True),
        encoding="utf-8",
    )


def _null_name_config() -> CorruptConfig:
    """A single null_cells operation over records__actor.prop__name."""
    return CorruptConfig(
        seed=1,
        operations=[
            NullCells(
                kind="null_cells",
                name="null_actor_name",
                target=Target(table="records__actor", columns=["prop__name"]),
                amount=Amount(rate=1.0),
            )
        ],
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_cmd_corrupt_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_corrupt with a valid emit + config writes the corrupted emit + manifest."""
    emit_dir = tmp_path / "emit"
    build_spanning(emit_dir)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, _null_name_config())
    out_dir = tmp_path / "out"

    exit_code = cmd_corrupt(emit_dir, config_path, out_dir)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert (out_dir / "run.duckdb").exists()
    assert (out_dir / "base.json").exists()
    assert (out_dir / "defects.json").exists()
    assert "null_cells" in captured.out
    assert "records__actor" in captured.out
    assert "units_selected" in captured.out
    assert "units_affected" in captured.out


def test_main_corrupt_dispatches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main(['corrupt', ...]) dispatches to cmd_corrupt correctly."""
    emit_dir = tmp_path / "emit"
    build_spanning(emit_dir)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, _null_name_config())
    out_dir = tmp_path / "out"

    exit_code = main(
        [
            "corrupt",
            str(emit_dir),
            "--config",
            str(config_path),
            "--out",
            str(out_dir),
        ]
    )
    assert exit_code == 0
    assert (out_dir / "defects.json").exists()


# ---------------------------------------------------------------------------
# Error surfaces
# ---------------------------------------------------------------------------


def test_cmd_corrupt_missing_emit_dir_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing emit dir surfaces a ReaderError, exit 1, no traceback."""
    missing_emit = tmp_path / "no_such_emit"
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, _null_name_config())
    out_dir = tmp_path / "out"

    exit_code = cmd_corrupt(missing_emit, config_path, out_dir)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "ERROR" in captured.err
    assert "Traceback" not in captured.err


def test_cmd_corrupt_bad_config_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An invalid corrupter config surfaces a ConfigError, exit 1, no traceback."""
    emit_dir = tmp_path / "emit"
    build_spanning(emit_dir)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("seed: 1\noperations: []\n", encoding="utf-8")
    out_dir = tmp_path / "out"

    exit_code = cmd_corrupt(emit_dir, config_path, out_dir)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "ERROR" in captured.err
    assert "Traceback" not in captured.err


def test_cmd_corrupt_business_rule_failure_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A business-rule failure (structural target column) surfaces, exit 1."""
    emit_dir = tmp_path / "emit"
    build_spanning(emit_dir)
    config_path = tmp_path / "config.yaml"
    bad_config = CorruptConfig(
        seed=1,
        operations=[
            NullCells(
                kind="null_cells",
                target=Target(table="records__actor", columns=["record_id"]),
                amount=Amount(rate=1.0),
            )
        ],
    )
    _write_config(config_path, bad_config)
    out_dir = tmp_path / "out"

    exit_code = cmd_corrupt(emit_dir, config_path, out_dir)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "ERROR" in captured.err
    assert "Traceback" not in captured.err


def test_cmd_corrupt_populated_out_dir_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An out_dir that already holds a run.duckdb refuses to overwrite, exit 1."""
    emit_dir = tmp_path / "emit"
    build_spanning(emit_dir)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, _null_name_config())
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "run.duckdb").write_bytes(b"")

    exit_code = cmd_corrupt(emit_dir, config_path, out_dir)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "ERROR" in captured.err
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# Manifest is always written -- no suppress flag exists
# ---------------------------------------------------------------------------


def test_no_manifest_suppress_flag_in_parser_surface(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The corrupt subparser exposes no flag to suppress defects.json: an
    unrecognized `--no-manifest` / `--quiet-manifest` flag is a usage error
    (non-zero exit), and the manifest is never written on that failed run."""
    emit_dir = tmp_path / "emit"
    build_spanning(emit_dir)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, _null_name_config())
    out_dir = tmp_path / "out"

    exit_code = main(
        [
            "corrupt",
            str(emit_dir),
            "--config",
            str(config_path),
            "--out",
            str(out_dir),
            "--no-manifest",
        ]
    )
    capsys.readouterr()

    assert exit_code != 0
    assert not out_dir.exists() or not (out_dir / "defects.json").exists()
