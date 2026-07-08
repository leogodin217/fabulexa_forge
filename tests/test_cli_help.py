"""Tests for `fabexport --help` behavior (top level and per-verb).

Covers:
- `fabexport --help` / `-h` / `help` -> usage + verb table on stdout, exit 0
- `fabexport <verb> --help` / `-h` -> argparse's full usage on stdout, exit 0
  (parametrized over every verb in VERBS)
- `fabexport` (bare) -> usage on stderr, exit 1
- `fabexport <unknown-verb>` -> "Unknown verb: '<x>'" + usage on stderr, exit 1
- `fabexport <verb> <bad args>` -> argparse error on stderr, exit 2
- `render_usage()` names every verb in VERBS
"""

from __future__ import annotations

import pytest

from fabulexa_export.cli import VERBS, Verb, main, render_usage


def test_top_level_help_flag_prints_usage_to_stdout_exit_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`fabexport --help` prints the usage + verb table to stdout and exits 0."""
    exit_code = main(["--help"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == render_usage() + "\n"
    assert captured.err == ""


def test_top_level_h_flag_prints_usage_to_stdout_exit_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`fabexport -h` prints the usage + verb table to stdout and exits 0."""
    exit_code = main(["-h"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == render_usage() + "\n"
    assert captured.err == ""


def test_top_level_help_word_prints_usage_to_stdout_exit_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`fabexport help` prints the usage + verb table to stdout and exits 0."""
    exit_code = main(["help"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == render_usage() + "\n"
    assert captured.err == ""


def test_bare_invocation_prints_usage_to_stderr_exit_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`fabexport` with no args prints usage to stderr and exits 1."""
    exit_code = main([])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == render_usage() + "\n"
    assert captured.out == ""


def test_unknown_verb_prints_error_and_usage_to_stderr_exit_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`fabexport bogus` prints 'Unknown verb: ...' + usage to stderr and exits 1."""
    exit_code = main(["bogus"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Unknown verb: 'bogus'" in captured.err
    assert render_usage() in captured.err
    assert captured.out == ""


def test_verb_bad_args_argparse_error_exit_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`fabexport export` missing required args -> argparse error to stderr, exit 2."""
    exit_code = main(["export"])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.err != ""
    assert captured.out == ""


def test_render_usage_names_every_verb() -> None:
    """render_usage() mentions every verb registered in VERBS."""
    usage = render_usage()
    for verb in VERBS:
        assert verb.name in usage


@pytest.mark.parametrize("verb", VERBS, ids=lambda v: v.name)
def test_verb_help_flag_prints_argparse_usage_to_stdout_exit_zero(
    verb: Verb, capsys: pytest.CaptureFixture[str]
) -> None:
    """`fabexport <verb> --help` prints argparse's usage to stdout and exits 0."""
    exit_code = main([verb.name, "--help"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.startswith(f"usage: fabexport {verb.name}")
    assert captured.err == ""


@pytest.mark.parametrize("verb", VERBS, ids=lambda v: v.name)
def test_verb_h_flag_prints_argparse_usage_to_stdout_exit_zero(
    verb: Verb, capsys: pytest.CaptureFixture[str]
) -> None:
    """`fabexport <verb> -h` prints argparse's usage to stdout and exits 0."""
    exit_code = main([verb.name, "-h"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.startswith(f"usage: fabexport {verb.name}")
    assert captured.err == ""
