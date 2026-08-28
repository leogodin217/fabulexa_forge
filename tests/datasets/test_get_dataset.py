"""Tests for the get/verify/extract pipeline (fabulexa_forge.datasets.fetch)."""

from __future__ import annotations

import functools
import hashlib
import io
import os
import tarfile
import tempfile
from pathlib import Path
from typing import BinaryIO

import pytest

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.datasets import fetch as fetch_module
from fabulexa_forge.datasets.fetch import DatasetError, get_dataset
from fabulexa_forge.datasets.models import DatasetEntry, DatasetManifest

_PACK_CONTENT = {
    "bundle/run.duckdb": b"duckdb-bytes",
    "bundle/base.json": b'{"tables": []}',
    "config.yaml": b"key: value",
}


class _MidStreamFailingReader:
    """A BinaryIO-like reader that yields good bytes, then raises OSError."""

    def __init__(self, data: bytes, fail_after: int) -> None:
        self._data = data
        self._fail_after = fail_after
        self._sent = 0

    def read(self, size: int = -1) -> bytes:
        if self._sent >= self._fail_after:
            raise OSError("connection reset mid-stream")
        chunk = self._data[self._sent : self._sent + size]
        self._sent += len(chunk)
        return chunk

    def __enter__(self) -> "_MidStreamFailingReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _make_archive(content: dict[str, bytes]) -> bytes:
    """Build gzip-compressed tar bytes containing the given relative-path files."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in content.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _make_unsafe_archive(info: tarfile.TarInfo, data: bytes = b"") -> bytes:
    """Build gzip-compressed tar bytes containing one unsafe member."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data) if data else None)
    return buffer.getvalue()


def _make_entry(
    name: str = "retail-week",
    archive_bytes: bytes | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> DatasetEntry:
    """Build a DatasetEntry pinned to the given (or default) archive bytes."""
    data = archive_bytes if archive_bytes is not None else _make_archive(_PACK_CONTENT)
    return DatasetEntry.model_validate(
        {
            "name": name,
            "description": "Test dataset.",
            "url": f"https://example.com/{name}.tar.gz",
            "sha256": sha256
            if sha256 is not None
            else hashlib.sha256(data).hexdigest(),
            "size_bytes": size_bytes if size_bytes is not None else len(data),
            "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
            "configs": ["config.yaml"],
            "commands": ["fabulexa-forge export {dir}/config.yaml --out out/"],
        }
    )


def _manifest_with(*entries: DatasetEntry) -> DatasetManifest:
    """Build a DatasetManifest from the given entries."""
    return DatasetManifest.model_validate(
        {"datasets": [entry.model_dump() for entry in entries]}
    )


def _open_bytes(data: bytes, url: str) -> BinaryIO:
    """Local-bytes transport: serve `data` regardless of `url`."""
    return io.BytesIO(data)


def _raise_oserror(message: str, url: str) -> BinaryIO:
    """Transport whose open itself fails."""
    raise OSError(message)


def _open_mid_stream_failing(data: bytes, fail_after: int, url: str) -> BinaryIO:
    """Transport whose read fails partway through the stream."""
    return _MidStreamFailingReader(data, fail_after)  # type: ignore[return-value]


def _unreachable_transport(url: str) -> BinaryIO:
    """Transport that fails the test if ever called."""
    raise AssertionError(f"transport should not have been called for {url!r}")


def _record_progress(calls: list[tuple[int, int]], received: int, size: int) -> None:
    """Append a (received, size) progress callback observation."""
    calls.append((received, size))


def _tree_snapshot(directory: Path) -> dict[str, bytes]:
    """Map each file's path (relative to `directory`) to its contents."""
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def temp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect fetch.py's temporary-archive directory so tests can assert
    no residue survives a get_dataset call."""
    archive_temp_dir = tmp_path / "_tmp"
    archive_temp_dir.mkdir()
    monkeypatch.setattr(
        tempfile, "mkstemp", functools.partial(tempfile.mkstemp, dir=archive_temp_dir)
    )
    return archive_temp_dir


def test_success_extracts_into_default_target_and_substitutes_commands(
    tmp_path: Path, temp_dir: Path
) -> None:
    """Pack extracted directly into ./<name>; commands carry the substituted path."""
    archive_bytes = _make_archive(_PACK_CONTENT)
    entry = _make_entry(archive_bytes=archive_bytes)
    manifest = _manifest_with(entry)
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        result = get_dataset(
            manifest,
            entry.name,
            None,
            False,
            functools.partial(_open_bytes, archive_bytes),
            None,
        )
    finally:
        os.chdir(old_cwd)
    assert result.target_dir == Path(entry.name)
    assert result.commands == (
        f"fabulexa-forge export {entry.name}/config.yaml --out out/",
    )
    extracted = _tree_snapshot(tmp_path / entry.name)
    assert extracted == _PACK_CONTENT
    assert not list(temp_dir.iterdir())


def test_success_with_explicit_dir_uses_value_verbatim(
    tmp_path: Path, temp_dir: Path
) -> None:
    """--dir value is used verbatim for both extraction and {dir} substitution."""
    archive_bytes = _make_archive(_PACK_CONTENT)
    entry = _make_entry(archive_bytes=archive_bytes)
    manifest = _manifest_with(entry)
    target = tmp_path / "custom" / "place"
    result = get_dataset(
        manifest,
        entry.name,
        target,
        False,
        functools.partial(_open_bytes, archive_bytes),
        None,
    )
    assert result.target_dir == target
    assert result.commands == (
        f"fabulexa-forge export {target}/config.yaml --out out/",
    )
    assert _tree_snapshot(target) == _PACK_CONTENT
    assert not list(temp_dir.iterdir())


def test_unknown_name_raises_and_never_calls_transport(tmp_path: Path) -> None:
    """Unknown dataset name raises DatasetError naming it; transport untouched."""
    manifest = _manifest_with(_make_entry(name="known"))
    with pytest.raises(DatasetError, match="unknown"):
        get_dataset(
            manifest, "missing", tmp_path / "out", False, _unreachable_transport, None
        )


def test_occupied_nonempty_directory_without_force_refuses(
    tmp_path: Path,
) -> None:
    """Non-empty target directory without --force refuses before download."""
    entry = _make_entry()
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    target.mkdir()
    (target / "existing.txt").write_text("already here")
    with pytest.raises(DatasetError, match=str(target)):
        get_dataset(manifest, entry.name, target, False, _unreachable_transport, None)
    assert (target / "existing.txt").exists()


def test_occupied_non_directory_target_without_force_refuses(tmp_path: Path) -> None:
    """A non-directory target path without --force refuses before download."""
    entry = _make_entry()
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    target.write_text("i am a file")
    with pytest.raises(DatasetError, match=str(target)):
        get_dataset(manifest, entry.name, target, False, _unreachable_transport, None)
    assert target.is_file()


def test_empty_directory_target_proceeds(tmp_path: Path, temp_dir: Path) -> None:
    """An existing but empty target directory proceeds without --force."""
    archive_bytes = _make_archive(_PACK_CONTENT)
    entry = _make_entry(archive_bytes=archive_bytes)
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    target.mkdir()
    result = get_dataset(
        manifest,
        entry.name,
        target,
        False,
        functools.partial(_open_bytes, archive_bytes),
        None,
    )
    assert result.target_dir == target
    assert _tree_snapshot(target) == _PACK_CONTENT


def test_occupied_target_with_force_removed_and_recreated(
    tmp_path: Path, temp_dir: Path
) -> None:
    """--force removes and recreates an occupied target, after verification."""
    archive_bytes = _make_archive(_PACK_CONTENT)
    entry = _make_entry(archive_bytes=archive_bytes)
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    target.mkdir()
    (target / "stale.txt").write_text("old content")
    result = get_dataset(
        manifest,
        entry.name,
        target,
        True,
        functools.partial(_open_bytes, archive_bytes),
        None,
    )
    assert result.target_dir == target
    assert not (target / "stale.txt").exists()
    assert _tree_snapshot(target) == _PACK_CONTENT


def test_occupied_non_directory_target_with_force_removed_and_recreated(
    tmp_path: Path, temp_dir: Path
) -> None:
    """--force removes and recreates a non-directory occupied target."""
    archive_bytes = _make_archive(_PACK_CONTENT)
    entry = _make_entry(archive_bytes=archive_bytes)
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    target.write_text("i am a file")
    result = get_dataset(
        manifest,
        entry.name,
        target,
        True,
        functools.partial(_open_bytes, archive_bytes),
        None,
    )
    assert result.target_dir == target
    assert target.is_dir()
    assert _tree_snapshot(target) == _PACK_CONTENT


def test_occupied_target_with_force_survives_sha_mismatch(
    tmp_path: Path, temp_dir: Path
) -> None:
    """A sha-mismatched archive with --force leaves the occupied target untouched."""
    archive_bytes = _make_archive(_PACK_CONTENT)
    entry = _make_entry(archive_bytes=archive_bytes, sha256="f" * 64)
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    target.mkdir()
    (target / "stale.txt").write_text("old content")
    with pytest.raises(DatasetError, match="sha256"):
        get_dataset(
            manifest,
            entry.name,
            target,
            True,
            functools.partial(_open_bytes, archive_bytes),
            None,
        )
    assert (target / "stale.txt").read_text() == "old content"
    assert not list(temp_dir.iterdir())


def test_transport_open_oserror_maps_to_dataset_error(
    tmp_path: Path, temp_dir: Path
) -> None:
    """A transport open failure maps to DatasetError; target left as found."""
    entry = _make_entry()
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    with pytest.raises(DatasetError, match="download"):
        get_dataset(
            manifest,
            entry.name,
            target,
            False,
            functools.partial(_raise_oserror, "connection refused"),
            None,
        )
    assert not target.exists()
    assert not list(temp_dir.iterdir())


def test_transport_read_mid_stream_oserror_maps_to_dataset_error(
    tmp_path: Path, temp_dir: Path
) -> None:
    """A transport read failure mid-stream maps to DatasetError; no residue."""
    archive_bytes = _make_archive(_PACK_CONTENT)
    entry = _make_entry(archive_bytes=archive_bytes)
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    with pytest.raises(DatasetError, match="download"):
        get_dataset(
            manifest,
            entry.name,
            target,
            False,
            functools.partial(_open_mid_stream_failing, archive_bytes, 4),
            None,
        )
    assert not target.exists()
    assert not list(temp_dir.iterdir())


def test_size_mismatch_raises_naming_counts(tmp_path: Path, temp_dir: Path) -> None:
    """A byte-count mismatch raises DatasetError naming expected/actual counts."""
    archive_bytes = _make_archive(_PACK_CONTENT)
    entry = _make_entry(archive_bytes=archive_bytes, size_bytes=len(archive_bytes) + 5)
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    with pytest.raises(DatasetError, match=str(len(archive_bytes))):
        get_dataset(
            manifest,
            entry.name,
            target,
            False,
            functools.partial(_open_bytes, archive_bytes),
            None,
        )
    assert not target.exists()
    assert not list(temp_dir.iterdir())


def test_sha256_mismatch_raises_naming_digests(tmp_path: Path, temp_dir: Path) -> None:
    """A sha256 mismatch raises DatasetError naming expected/actual digests."""
    archive_bytes = _make_archive(_PACK_CONTENT)
    entry = _make_entry(archive_bytes=archive_bytes, sha256="a" * 64)
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    with pytest.raises(DatasetError, match="a" * 64):
        get_dataset(
            manifest,
            entry.name,
            target,
            False,
            functools.partial(_open_bytes, archive_bytes),
            None,
        )
    assert not target.exists()
    assert not list(temp_dir.iterdir())


def test_unsafe_absolute_path_member(tmp_path: Path, temp_dir: Path) -> None:
    """An absolute-path archive member is refused; nothing written."""
    archive_bytes = _make_unsafe_archive(tarfile.TarInfo(name="/etc/passwd"), b"x")
    entry = _make_entry(archive_bytes=archive_bytes)
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    with pytest.raises(DatasetError, match="unsafe"):
        get_dataset(
            manifest,
            entry.name,
            target,
            False,
            functools.partial(_open_bytes, archive_bytes),
            None,
        )
    assert not target.exists()
    assert not list(temp_dir.iterdir())


def test_unsafe_traversal_member(tmp_path: Path, temp_dir: Path) -> None:
    """A `..`-traversal archive member is refused; nothing written."""
    archive_bytes = _make_unsafe_archive(tarfile.TarInfo(name="../evil.txt"), b"x")
    entry = _make_entry(archive_bytes=archive_bytes)
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    with pytest.raises(DatasetError, match="unsafe"):
        get_dataset(
            manifest,
            entry.name,
            target,
            False,
            functools.partial(_open_bytes, archive_bytes),
            None,
        )
    assert not target.exists()
    assert not list(temp_dir.iterdir())


def test_unsafe_symlink_member(tmp_path: Path, temp_dir: Path) -> None:
    """A symlink archive member is refused; nothing written."""
    info = tarfile.TarInfo(name="link")
    info.type = tarfile.SYMTYPE
    info.linkname = "target"
    archive_bytes = _make_unsafe_archive(info)
    entry = _make_entry(archive_bytes=archive_bytes)
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    with pytest.raises(DatasetError, match="unsafe"):
        get_dataset(
            manifest,
            entry.name,
            target,
            False,
            functools.partial(_open_bytes, archive_bytes),
            None,
        )
    assert not target.exists()
    assert not list(temp_dir.iterdir())


def test_unsafe_fifo_member(tmp_path: Path, temp_dir: Path) -> None:
    """A fifo archive member is refused; nothing written."""
    info = tarfile.TarInfo(name="fifo")
    info.type = tarfile.FIFOTYPE
    archive_bytes = _make_unsafe_archive(info)
    entry = _make_entry(archive_bytes=archive_bytes)
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    with pytest.raises(DatasetError, match="unsafe"):
        get_dataset(
            manifest,
            entry.name,
            target,
            False,
            functools.partial(_open_bytes, archive_bytes),
            None,
        )
    assert not target.exists()
    assert not list(temp_dir.iterdir())


def test_post_verification_oserror_propagates_unwrapped(
    tmp_path: Path, temp_dir: Path
) -> None:
    """An I/O failure after verification propagates unwrapped, not as DatasetError."""
    archive_bytes = _make_archive(_PACK_CONTENT)
    entry = _make_entry(archive_bytes=archive_bytes)
    manifest = _manifest_with(entry)
    readonly_parent = tmp_path / "readonly"
    readonly_parent.mkdir()
    target = readonly_parent / "out"
    readonly_parent.chmod(0o500)
    try:
        with pytest.raises(OSError) as exc_info:
            get_dataset(
                manifest,
                entry.name,
                target,
                False,
                functools.partial(_open_bytes, archive_bytes),
                None,
            )
        assert not isinstance(exc_info.value, DatasetError)
    finally:
        readonly_parent.chmod(0o700)
    assert not list(temp_dir.iterdir())


def test_progress_callback_receives_monotonic_counts(
    tmp_path: Path, temp_dir: Path
) -> None:
    """Progress callback fires with (bytes_received, size_bytes) monotonically."""
    archive_bytes = _make_archive(_PACK_CONTENT)
    entry = _make_entry(archive_bytes=archive_bytes)
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    calls: list[tuple[int, int]] = []
    original_chunk_size = fetch_module._DOWNLOAD_CHUNK_SIZE
    fetch_module._DOWNLOAD_CHUNK_SIZE = 4
    try:
        get_dataset(
            manifest,
            entry.name,
            target,
            False,
            functools.partial(_open_bytes, archive_bytes),
            functools.partial(_record_progress, calls),
        )
    finally:
        fetch_module._DOWNLOAD_CHUNK_SIZE = original_chunk_size
    assert len(calls) > 1
    received_counts = [received for received, _size in calls]
    assert received_counts == sorted(received_counts)
    assert calls[-1] == (len(archive_bytes), entry.size_bytes)


def test_progress_none_works(tmp_path: Path, temp_dir: Path) -> None:
    """progress=None is accepted and the get proceeds normally."""
    archive_bytes = _make_archive(_PACK_CONTENT)
    entry = _make_entry(archive_bytes=archive_bytes)
    manifest = _manifest_with(entry)
    target = tmp_path / "out"
    result = get_dataset(
        manifest,
        entry.name,
        target,
        False,
        functools.partial(_open_bytes, archive_bytes),
        None,
    )
    assert result.target_dir == target


def test_determinism_same_bytes_same_tree(tmp_path: Path, temp_dir: Path) -> None:
    """Same manifest + same bytes produce an identical extracted tree on a fresh get."""
    archive_bytes = _make_archive(_PACK_CONTENT)
    entry = _make_entry(archive_bytes=archive_bytes)
    manifest = _manifest_with(entry)
    first_target = tmp_path / "first"
    second_target = tmp_path / "second"
    transport = functools.partial(_open_bytes, archive_bytes)
    get_dataset(manifest, entry.name, first_target, False, transport, None)
    get_dataset(manifest, entry.name, second_target, False, transport, None)
    assert (
        _tree_snapshot(first_target) == _tree_snapshot(second_target) == _PACK_CONTENT
    )
