"""Tests for tools/build_dataset_pack.py.

Loaded via importlib.util.spec_from_file_location — tools/ is not a package.
Fixture emits are built through the existing tests/_support and
tests/reader emit helpers; no fixture writes a base.json by hand.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import tarfile
import types
from pathlib import Path

import duckdb
import pytest
from _support.sidecar_builder import UNSUPPORTED_VERSION_SENTINEL
from _support.sidecar_builder import write_emit as write_sidecar

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.datasets.models import DatasetEntry, DatasetManifest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reader._emit_helpers import _minimal_sidecar  # noqa: E402
from reader._emit_helpers import write_emit as write_reader_emit  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_PATH = _REPO_ROOT / "tools" / "build_dataset_pack.py"


def _load_tool() -> types.ModuleType:
    """Load tools/build_dataset_pack.py as a module (tools/ is not a package)."""
    spec = importlib.util.spec_from_file_location("build_dataset_pack", _TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pack_builder = _load_tool()


def _entry(
    configs: list[str] | None = None,
    commands: list[str] | None = None,
    sha256: str = "0" * 64,
) -> DatasetEntry:
    """Build a well-formed manifest entry driving a test build."""
    return DatasetEntry.model_validate(
        {
            "name": "demo-pack",
            "description": "A demo dataset pack.",
            "url": "https://example.com/demo-pack.tar.gz",
            "sha256": sha256,
            "size_bytes": 1,
            "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
            "configs": configs or ["dimensional.yaml"],
            "commands": commands
            or ["fabulexa-forge export {dir}/dimensional.yaml --out out/"],
        }
    )


def _write_full_example(example_dir: Path, configs: list[str] | None = None) -> None:
    """Write a complete, valid example directory: bundle triple + configs."""
    bundle_dir = example_dir / "bundle"
    bundle_dir.mkdir(parents=True)
    write_reader_emit(bundle_dir)
    (bundle_dir / "ATLAS.md").write_text("# Atlas\n", encoding="utf-8")
    for name in configs or ["dimensional.yaml"]:
        (example_dir / name).write_text("grain: event\n", encoding="utf-8")


def _write_unsupported_version_example(
    example_dir: Path, configs: list[str] | None = None
) -> None:
    """Write an example directory whose bundle carries an unsupported version."""
    bundle_dir = example_dir / "bundle"
    bundle_dir.mkdir(parents=True)
    write_sidecar(
        bundle_dir,
        tables=_minimal_sidecar()["tables"],  # type: ignore[arg-type]
        base_format_version=UNSUPPORTED_VERSION_SENTINEL,
        schema_valid=False,
    )
    duckdb.connect(str(bundle_dir / "run.duckdb")).close()
    (bundle_dir / "ATLAS.md").write_text("# Atlas\n", encoding="utf-8")
    for name in configs or ["dimensional.yaml"]:
        (example_dir / name).write_text("grain: event\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Build success + archive contents
# ---------------------------------------------------------------------------


def test_build_success_archive_contents_exact(tmp_path: Path) -> None:
    """The archive contains exactly the bundle triple + configs, no wrapper dir."""
    example_dir = tmp_path / "example"
    _write_full_example(example_dir)
    (example_dir / "exports").mkdir()
    (example_dir / "exports" / "x.txt").write_text("not packed", encoding="utf-8")
    (example_dir / "demo.yaml").write_text("not packed either", encoding="utf-8")

    out_path = tmp_path / "out" / "demo-pack.tar.gz"
    pack_builder.build_pack(_entry(), example_dir, out_path)

    with tarfile.open(out_path, mode="r:gz") as archive:
        names = sorted(m.name for m in archive.getmembers())
    assert names == [
        "bundle/ATLAS.md",
        "bundle/base.json",
        "bundle/run.duckdb",
        "dimensional.yaml",
    ]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism_byte_identical(tmp_path: Path) -> None:
    """Building the same tree twice yields byte-identical archives."""
    example_dir = tmp_path / "example"
    _write_full_example(example_dir)

    first_path = tmp_path / "first.tar.gz"
    second_path = tmp_path / "second.tar.gz"
    stamp_one = pack_builder.build_pack(_entry(), example_dir, first_path)
    stamp_two = pack_builder.build_pack(_entry(), example_dir, second_path)

    assert stamp_one.sha256 == stamp_two.sha256
    assert first_path.read_bytes() == second_path.read_bytes()


def test_determinism_normalization_pinned(tmp_path: Path) -> None:
    """Members are mtime/uid/gid/uname/gname-normalized, mode 0644, sorted order;
    the gzip stream itself carries mtime 0."""
    example_dir = tmp_path / "example"
    _write_full_example(example_dir)
    out_path = tmp_path / "out.tar.gz"
    pack_builder.build_pack(_entry(), example_dir, out_path)

    with tarfile.open(out_path, mode="r:gz") as archive:
        members = archive.getmembers()
    names = [m.name for m in members]
    assert names == sorted(names)
    for member in members:
        assert member.mtime == 0
        assert member.uid == 0
        assert member.gid == 0
        assert member.uname == ""
        assert member.gname == ""
        assert member.mode == 0o644

    # gzip header: bytes 4-7 are the little-endian MTIME field.
    header = out_path.read_bytes()[:10]
    assert header[4:8] == b"\x00\x00\x00\x00"


# ---------------------------------------------------------------------------
# Stamp
# ---------------------------------------------------------------------------


def test_stamp_matches_written_file(tmp_path: Path) -> None:
    """PackStamp.sha256/size_bytes match the written archive file."""
    example_dir = tmp_path / "example"
    _write_full_example(example_dir)
    out_path = tmp_path / "out.tar.gz"
    stamp = pack_builder.build_pack(_entry(), example_dir, out_path)

    assert stamp.sha256 == hashlib.sha256(out_path.read_bytes()).hexdigest()
    assert stamp.size_bytes == out_path.stat().st_size
    assert stamp.base_format_version == SUPPORTED_BASE_FORMAT_VERSION


def test_stamp_ignores_authored_sha256(tmp_path: Path) -> None:
    """A garbage-sha entry still builds; the stamp reflects the computed digest."""
    example_dir = tmp_path / "example"
    _write_full_example(example_dir)
    out_path = tmp_path / "out.tar.gz"
    entry = _entry(sha256="f" * 64)
    stamp = pack_builder.build_pack(entry, example_dir, out_path)

    assert stamp.sha256 != entry.sha256
    assert stamp.sha256 == hashlib.sha256(out_path.read_bytes()).hexdigest()


def test_render_stamp_fragment_paste_ready() -> None:
    """render_stamp_fragment names the three fields on their own lines."""
    stamp = pack_builder.PackStamp(
        sha256="a" * 64,
        size_bytes=42,
        base_format_version=SUPPORTED_BASE_FORMAT_VERSION,
    )
    fragment = pack_builder.render_stamp_fragment(stamp)
    assert fragment == (
        f"sha256: {'a' * 64}\n"
        "size_bytes: 42\n"
        f"base_format_version: {SUPPORTED_BASE_FORMAT_VERSION}"
    )


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_missing_run_duckdb_names_it(tmp_path: Path) -> None:
    example_dir = tmp_path / "example"
    _write_full_example(example_dir)
    (example_dir / "bundle" / "run.duckdb").unlink()

    with pytest.raises(pack_builder.PackBuildError, match="run.duckdb"):
        pack_builder.build_pack(_entry(), example_dir, tmp_path / "out.tar.gz")


def test_missing_base_json_names_it(tmp_path: Path) -> None:
    example_dir = tmp_path / "example"
    _write_full_example(example_dir)
    (example_dir / "bundle" / "base.json").unlink()

    with pytest.raises(pack_builder.PackBuildError, match="base.json"):
        pack_builder.build_pack(_entry(), example_dir, tmp_path / "out.tar.gz")


def test_missing_atlas_md_names_it(tmp_path: Path) -> None:
    example_dir = tmp_path / "example"
    _write_full_example(example_dir)
    (example_dir / "bundle" / "ATLAS.md").unlink()

    with pytest.raises(pack_builder.PackBuildError, match="ATLAS.md"):
        pack_builder.build_pack(_entry(), example_dir, tmp_path / "out.tar.gz")


def test_missing_configs_file_names_it(tmp_path: Path) -> None:
    example_dir = tmp_path / "example"
    _write_full_example(example_dir)
    (example_dir / "dimensional.yaml").unlink()

    with pytest.raises(pack_builder.PackBuildError, match="dimensional.yaml"):
        pack_builder.build_pack(_entry(), example_dir, tmp_path / "out.tar.gz")


def test_unsupported_version_refuses_naming_found_version(tmp_path: Path) -> None:
    """A bundle open_emit refuses (unsupported version) surfaces the reader's
    own diagnostic, naming found_version."""
    example_dir = tmp_path / "example"
    _write_unsupported_version_example(example_dir)

    with pytest.raises(
        pack_builder.PackBuildError, match=str(UNSUPPORTED_VERSION_SENTINEL)
    ):
        pack_builder.build_pack(_entry(), example_dir, tmp_path / "out.tar.gz")


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_unknown_name_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = DatasetManifest.model_validate({"datasets": [_entry().model_dump()]})
    monkeypatch.setattr(pack_builder, "load_manifest", lambda: manifest)
    monkeypatch.setattr(pack_builder, "_REPO_ROOT", tmp_path)

    exit_code = pack_builder.main(["nope", "--out", str(tmp_path / "out")])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "nope" in captured.err
    assert "demo-pack" in captured.err
    assert captured.out == ""


def test_main_success_writes_archive_and_prints_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = _entry()
    manifest = DatasetManifest.model_validate({"datasets": [entry.model_dump()]})
    monkeypatch.setattr(pack_builder, "load_manifest", lambda: manifest)
    monkeypatch.setattr(pack_builder, "_REPO_ROOT", tmp_path)
    _write_full_example(tmp_path / "docs" / "examples" / entry.name)

    out_dir = tmp_path / "out"
    exit_code = pack_builder.main([entry.name, "--out", str(out_dir)])

    captured = capsys.readouterr()
    archive_path = out_dir / f"{entry.name}.tar.gz"
    assert exit_code == 0
    assert archive_path.exists()
    assert "sha256:" in captured.out
    assert "size_bytes:" in captured.out
    assert "base_format_version:" in captured.out
    assert captured.err == ""


# ---------------------------------------------------------------------------
# Offline / non-mutating
# ---------------------------------------------------------------------------


def test_never_rewrites_manifest_file(tmp_path: Path) -> None:
    """build_pack never touches the manifest file it was driven from."""
    manifest_path = _REPO_ROOT / "src" / "fabulexa_forge" / "datasets" / "manifest.yaml"
    before = manifest_path.read_bytes()
    example_dir = tmp_path / "example"
    _write_full_example(example_dir)
    pack_builder.build_pack(_entry(), example_dir, tmp_path / "out.tar.gz")
    assert manifest_path.read_bytes() == before
