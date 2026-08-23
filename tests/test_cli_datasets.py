"""Tests for the `fabulexa-forge datasets` verb (list, get).

Covers:
- `datasets list` against the shipped (empty) manifest: text and json, exit 0
- `datasets list` performs no network I/O (the transport seam is never called)
- Bare `datasets` / unknown sub-verb: argparse usage error, exit 2
- `datasets --help` / `-h`: usage on stdout, exit 0
- `datasets get nope`: unknown-name DatasetError to stderr, exit 1
- `datasets get <name>` success path (manifest + transport monkeypatched)
- `datasets get` DatasetError mapping (a mismatched sha256)
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from typing import BinaryIO

import pytest

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge import cli as cli_mod
from fabulexa_forge.cli import main
from fabulexa_forge.datasets.models import DatasetEntry, DatasetManifest

_PACK_CONTENT = {
    "bundle/run.duckdb": b"duckdb-bytes",
    "bundle/base.json": b'{"tables": []}',
    "bundle/ATLAS.md": b"# Atlas\n",
    "dimensional.yaml": b"grain: event\n",
}


def _make_archive(content: dict[str, bytes]) -> bytes:
    """Build gzip-compressed tar bytes containing the given relative-path files."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in content.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _make_entry(archive_bytes: bytes) -> DatasetEntry:
    """Build a manifest entry pinning the given archive's own sha256/size."""
    return DatasetEntry.model_validate(
        {
            "name": "demo-pack",
            "description": "A demo dataset pack.",
            "url": "https://example.com/demo-pack.tar.gz",
            "sha256": hashlib.sha256(archive_bytes).hexdigest(),
            "size_bytes": len(archive_bytes),
            "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
            "configs": ["dimensional.yaml"],
            "commands": ["fabulexa-forge export {dir}/dimensional.yaml --out out/"],
        }
    )


def test_datasets_list_text_empty_catalog_stdout_only_exit_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`datasets list` on the shipped empty manifest prints the no-datasets line."""
    exit_code = main(["datasets", "list"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "no datasets published for this version\n"
    assert captured.err == ""


def test_datasets_list_json_empty_catalog(capsys: pytest.CaptureFixture[str]) -> None:
    """`datasets list --format json` prints the empty-catalog JSON document."""
    exit_code = main(["datasets", "list", "--format", "json"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == '{"datasets":[]}\n'
    assert captured.err == ""


def test_datasets_list_never_invokes_transport(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`datasets list` performs no network I/O: the transport is never called."""

    def _boom(url: str) -> BinaryIO:
        raise AssertionError("transport must not be invoked on the list path")

    monkeypatch.setattr(cli_mod, "_urllib_transport", _boom)
    exit_code = main(["datasets", "list"])
    capsys.readouterr()
    assert exit_code == 0


def test_datasets_bare_invocation_is_usage_error_exit_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bare `datasets` (missing sub-verb) is an argparse usage error, exit 2."""
    exit_code = main(["datasets"])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.err != ""
    assert captured.out == ""


def test_datasets_unknown_subverb_is_usage_error_exit_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown sub-verb is an argparse usage error, exit 2."""
    exit_code = main(["datasets", "frobnicate"])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.err != ""
    assert captured.out == ""


def test_datasets_help_flag_prints_usage_to_stdout_exit_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`datasets --help` prints argparse's usage to stdout and exits 0."""
    exit_code = main(["datasets", "--help"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.startswith("usage: fabulexa-forge datasets")
    assert captured.err == ""


def test_datasets_get_unknown_name_names_it_and_valid_names_exit_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`datasets get nope` reports the unknown name to stderr, exit 1, empty stdout."""
    exit_code = main(["datasets", "get", "nope"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "nope" in captured.err
    assert captured.out == ""


def test_datasets_get_success_prints_commands_and_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`datasets get <name>` extracts a locally built pack and prints its
    substituted commands to stdout, with progress on stderr."""
    archive_bytes = _make_archive(_PACK_CONTENT)
    entry = _make_entry(archive_bytes)
    manifest = DatasetManifest.model_validate({"datasets": [entry.model_dump()]})
    target = tmp_path / "extracted"

    monkeypatch.setattr(cli_mod, "load_manifest", lambda: manifest)
    monkeypatch.setattr(
        cli_mod, "_urllib_transport", lambda url: io.BytesIO(archive_bytes)
    )

    exit_code = main(["datasets", "get", entry.name, "--dir", str(target)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert (
        captured.out
        == "fabulexa-forge export {}/dimensional.yaml --out out/\n".format(target)
    )
    assert captured.err != ""
    assert (target / "dimensional.yaml").read_bytes() == b"grain: event\n"


def test_datasets_get_dataset_error_mapped_to_stderr_exit_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `DatasetError` from `get_dataset` (e.g. a sha256 mismatch) is mapped to
    its message on stderr, exit 1."""
    archive_bytes = _make_archive(_PACK_CONTENT)
    entry = _make_entry(archive_bytes)
    manifest = DatasetManifest.model_validate({"datasets": [entry.model_dump()]})
    tampered_bytes = bytes([archive_bytes[0] ^ 0xFF]) + archive_bytes[1:]
    target = tmp_path / "extracted"

    monkeypatch.setattr(cli_mod, "load_manifest", lambda: manifest)
    monkeypatch.setattr(
        cli_mod, "_urllib_transport", lambda url: io.BytesIO(tampered_bytes)
    )

    exit_code = main(["datasets", "get", entry.name, "--dir", str(target)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "sha256 mismatch" in captured.err
    assert captured.out == ""
