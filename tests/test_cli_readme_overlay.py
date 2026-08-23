"""CLI `readme_overlay` resolution tests.

Covers:
- `readme_overlay` resolves against the config file's parent directory, not
  the process cwd
- a missing overlay file exits 1 through the existing ConfigError funnel
  (`ReadmeOverlayInvalid`), before any artifact is written
- overlay content violating the slot grammar exits 1 through the same funnel
- an overlay `table:` slot naming a table the plan doesn't produce exits 1
  (`ReadmeOverlayUnknownTable`) and leaves the target empty
- stdout row-count lines are byte-identical whether or not `readme_overlay`
  is set
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exporters._emit_fixtures import build_test_emit
from fabulexa_forge.cli import cmd_export

_DIMENSIONAL_CONFIG_BODY = """\
dimensional:
  tables:
  - name: dim_entity
    role: dim
    scd: type1
    source:
      grain: records
      kind: entity
    key: [id]
    columns:
    - name: id
      from: record_id
"""


def _emit_subdir(parent: Path) -> Path:
    """A freshly created `parent/emit` directory for an emit builder to write
    into (the builders assume an existing directory)."""
    emit_dir = parent / "emit"
    emit_dir.mkdir()
    return emit_dir


def _write_config(config_path: Path, *, readme_overlay: str | None) -> None:
    """Write a minimal `mode: dimensional` export config, optionally carrying
    a `readme_overlay` path (written verbatim, relative to the config file).
    """
    lines = ["mode: dimensional"]
    if readme_overlay is not None:
        lines.append(f"readme_overlay: {readme_overlay}")
    config_path.write_text(
        "\n".join(lines) + "\n" + _DIMENSIONAL_CONFIG_BODY, encoding="utf-8"
    )


def test_readme_overlay_resolves_against_config_parent_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`readme_overlay` resolves relative to the config file's directory even
    when the process cwd is elsewhere."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    emit_dir = build_test_emit(_emit_subdir(project_dir))
    config_path = project_dir / "config.yaml"
    (project_dir / "overlay.md").write_text(
        "## overview\n\nCustom overview text.\n", encoding="utf-8"
    )
    _write_config(config_path, readme_overlay="overlay.md")
    out_dir = project_dir / "out"
    out_dir.mkdir()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    exit_code = cmd_export(emit_dir, config_path, out_dir, "csv")

    assert exit_code == 0
    readme = (out_dir / "dimensional-readme.md").read_text(encoding="utf-8")
    assert "Custom overview text." in readme


def test_readme_overlay_missing_file_exits_1_via_config_error_funnel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `readme_overlay` naming a nonexistent file exits 1
    (`ReadmeOverlayInvalid`, a `ConfigError`) before any artifact is written."""
    emit_dir = build_test_emit(_emit_subdir(tmp_path))
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, readme_overlay="does-not-exist.md")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    exit_code = cmd_export(emit_dir, config_path, out_dir, "csv")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "ERROR" in captured.err
    assert not any(out_dir.iterdir())


def test_readme_overlay_invalid_content_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Overlay content preceding the first H2 slot heading exits 1
    (`ReadmeOverlayInvalid`) via the same funnel."""
    emit_dir = build_test_emit(_emit_subdir(tmp_path))
    config_path = tmp_path / "config.yaml"
    (tmp_path / "overlay.md").write_text(
        "stray content before any heading\n\n## overview\n\nHi.\n", encoding="utf-8"
    )
    _write_config(config_path, readme_overlay="overlay.md")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    exit_code = cmd_export(emit_dir, config_path, out_dir, "csv")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "ERROR" in captured.err


def test_readme_overlay_unknown_table_exits_1_and_leaves_target_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An overlay `table:` slot naming a table the plan doesn't produce exits
    1 (`ReadmeOverlayUnknownTable`) with the target directory left empty --
    no dataset, no companion artifacts."""
    emit_dir = build_test_emit(_emit_subdir(tmp_path))
    config_path = tmp_path / "config.yaml"
    (tmp_path / "overlay.md").write_text(
        "## table: no_such_table\n\nNote.\n", encoding="utf-8"
    )
    _write_config(config_path, readme_overlay="overlay.md")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    exit_code = cmd_export(emit_dir, config_path, out_dir, "csv")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "ERROR" in captured.err
    assert not any(out_dir.iterdir())


def test_readme_overlay_present_does_not_change_stdout_row_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """stdout's per-table row-count lines are byte-identical whether or not
    `readme_overlay` is set."""
    emit_dir = build_test_emit(_emit_subdir(tmp_path))

    config_plain = tmp_path / "config_plain.yaml"
    _write_config(config_plain, readme_overlay=None)
    out_plain = tmp_path / "out_plain"
    out_plain.mkdir()
    exit_code = cmd_export(emit_dir, config_plain, out_plain, "csv")
    assert exit_code == 0
    plain_stdout = capsys.readouterr().out

    config_overlay = tmp_path / "config_overlay.yaml"
    (tmp_path / "overlay.md").write_text("## overview\n\nHello.\n", encoding="utf-8")
    _write_config(config_overlay, readme_overlay="overlay.md")
    out_overlay = tmp_path / "out_overlay"
    out_overlay.mkdir()
    exit_code = cmd_export(emit_dir, config_overlay, out_overlay, "csv")
    assert exit_code == 0
    overlay_stdout = capsys.readouterr().out

    assert plain_stdout == overlay_stdout
    assert "dim_entity: 2 rows" in plain_stdout
