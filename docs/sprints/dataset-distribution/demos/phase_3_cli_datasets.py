#!/usr/bin/env python
"""
Demo: CLI verb `datasets` — sub-verbs `list` and `get`
Sprint: dataset-distribution
Phase: 3

Drives `main()` in-process, exactly as a shell invocation would: `datasets
list` (text and `--format json`) against the shipped (empty) manifest;
`datasets get nope` naming the unknown name; bare `datasets` and an unknown
sub-verb, both usage errors; `datasets --help`; and finally a full
end-to-end `get` against a locally built pack, with the module's manifest
and transport seams monkeypatched — no network I/O anywhere in this demo.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
from pathlib import Path
from typing import BinaryIO

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge import cli as cli_mod
from fabulexa_forge.cli import main as cli_main
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


def build_manifest(archive_bytes: bytes) -> DatasetManifest:
    """Build a one-entry manifest pinning the given archive's own sha256/size."""
    entry = DatasetEntry.model_validate(
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
    return DatasetManifest.model_validate({"datasets": [entry.model_dump()]})


def demo_list_and_usage_surfaces() -> None:
    """`datasets list` (text, json), bare/unknown sub-verb usage errors,
    `--help`, and `get nope` — all against the shipped empty manifest."""
    print("--- datasets list (text) ---")
    assert cli_main(["datasets", "list"]) == 0

    print("--- datasets list --format json ---")
    assert cli_main(["datasets", "list", "--format", "json"]) == 0

    print("--- bare `datasets` (usage error, exit 2) ---")
    assert cli_main(["datasets"]) == 2

    print("--- `datasets frobnicate` (unknown sub-verb, exit 2) ---")
    assert cli_main(["datasets", "frobnicate"]) == 2

    print("--- `datasets --help` (exit 0) ---")
    assert cli_main(["datasets", "--help"]) == 0

    print("--- `datasets get nope` (unknown name, exit 1) ---")
    assert cli_main(["datasets", "get", "nope"]) == 1


def demo_end_to_end_get(archive_bytes: bytes, tmp_root: Path) -> None:
    """Monkeypatch the module's manifest + transport seams to a locally built
    pack and run a full `datasets get` end to end."""
    manifest = build_manifest(archive_bytes)
    target = tmp_root / "demo-pack"

    original_load_manifest = cli_mod.load_manifest
    original_transport = cli_mod._urllib_transport
    cli_mod.load_manifest = lambda: manifest

    def local_bytes_transport(url: str) -> BinaryIO:
        return io.BytesIO(archive_bytes)

    cli_mod._urllib_transport = local_bytes_transport
    try:
        print("--- datasets get demo-pack --dir <tmp> ---")
        exit_code = cli_main(["datasets", "get", "demo-pack", "--dir", str(target)])
        assert exit_code == 0, f"expected exit 0, got {exit_code}"
    finally:
        cli_mod.load_manifest = original_load_manifest
        cli_mod._urllib_transport = original_transport

    extracted = sorted(
        p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()
    )
    assert extracted == sorted(PACK_CONTENT), f"unexpected extracted files: {extracted}"


def main() -> int:
    demo_list_and_usage_surfaces()

    archive_bytes = build_archive(PACK_CONTENT)
    with tempfile.TemporaryDirectory() as tmp_root_str:
        demo_end_to_end_get(archive_bytes, Path(tmp_root_str))

    print(
        "SUCCESS: `datasets` verb dispatches list/get, enforces its usage-error "
        "and exit-code contract, and drives a full get through injectable seams"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
