#!/usr/bin/env python
"""
Demo: get_dataset — fetch, verify, extract
Sprint: dataset-distribution
Phase: 2

Builds a small pack archive in a temp dir, serves it through a local-bytes
transport (no network), and runs it through get_dataset: successful
extraction with substituted example commands, then a tampered byte tripping
sha256 verification — with the target left untouched and no temporary
residue.
"""

from __future__ import annotations

import functools
import hashlib
import io
import tarfile
import tempfile
from pathlib import Path
from typing import BinaryIO

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.datasets.fetch import DatasetError, get_dataset
from fabulexa_forge.datasets.models import DatasetEntry, DatasetManifest

PACK_CONTENT = {
    "bundle/run.duckdb": b"pretend-duckdb-bytes",
    "bundle/base.json": b'{"tables": []}',
    "bundle/ATLAS.md": b"# Atlas\n",
    "dimensional.yaml": b"grain: event\n",
}


def build_archive(content: dict[str, bytes]) -> bytes:
    """Build gzip-compressed tar bytes containing the given relative-path files."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in content.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def open_bytes(data: bytes, url: str) -> BinaryIO:
    """Local-bytes transport: serve `data` regardless of `url` — no network."""
    return io.BytesIO(data)


def build_entry(archive_bytes: bytes, sha256: str) -> DatasetEntry:
    """Build the manifest entry pinning `sha256` for the built archive."""
    return DatasetEntry.model_validate(
        {
            "name": "demo-pack",
            "description": "A demo dataset pack.",
            "url": "https://example.com/demo-pack.tar.gz",
            "sha256": sha256,
            "size_bytes": len(archive_bytes),
            "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
            "configs": ["dimensional.yaml"],
            "commands": ["fabulexa-forge export {dir}/dimensional.yaml --out out/"],
        }
    )


def demo_successful_get(archive_bytes: bytes, digest: str, tmp_root: Path) -> None:
    """Download, verify, and extract a valid pack; print substituted commands."""
    entry = build_entry(archive_bytes, digest)
    manifest = DatasetManifest.model_validate({"datasets": [entry.model_dump()]})
    target = tmp_root / "success"

    result = get_dataset(
        manifest,
        entry.name,
        target,
        False,
        functools.partial(open_bytes, archive_bytes),
        None,
    )

    extracted = sorted(
        p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()
    )
    assert extracted == sorted(PACK_CONTENT), f"unexpected extracted files: {extracted}"
    print("--- successful get ---")
    print(f"target_dir: {result.target_dir}")
    print(f"commands: {result.commands}")


def demo_tampered_byte_fails_verification(
    archive_bytes: bytes, digest: str, tmp_root: Path
) -> None:
    """A tampered byte trips sha256 verification; an occupied --force target
    survives untouched, and no temp residue is left."""
    tampered_bytes = bytes([archive_bytes[0] ^ 0xFF]) + archive_bytes[1:]
    entry = build_entry(tampered_bytes, digest)
    manifest = DatasetManifest.model_validate({"datasets": [entry.model_dump()]})
    target = tmp_root / "tampered"
    target.mkdir()
    marker = target / "pre-existing.txt"
    marker.write_text("this must survive the failed get")

    temp_before = sorted(Path(tempfile.gettempdir()).glob("tmp*.tar.gz"))
    try:
        get_dataset(
            manifest,
            entry.name,
            target,
            True,
            functools.partial(open_bytes, tampered_bytes),
            None,
        )
    except DatasetError as exc:
        print("--- tampered byte ---")
        print(f"refused: {exc}")
    else:
        raise AssertionError("expected DatasetError from a sha256 mismatch")

    assert marker.exists(), "target was mutated despite failed verification"
    temp_after = sorted(Path(tempfile.gettempdir()).glob("tmp*.tar.gz"))
    assert temp_after == temp_before, "temporary archive residue left behind"


def main() -> int:
    archive_bytes = build_archive(PACK_CONTENT)
    digest = hashlib.sha256(archive_bytes).hexdigest()

    with tempfile.TemporaryDirectory() as tmp_root_str:
        tmp_root = Path(tmp_root_str)
        demo_successful_get(archive_bytes, digest, tmp_root)
        demo_tampered_byte_fails_verification(archive_bytes, digest, tmp_root)

    print(
        "SUCCESS: get_dataset downloads, verifies, and extracts through the "
        "injectable transport, with failure atomicity on a tampered byte"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
