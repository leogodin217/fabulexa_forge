#!/usr/bin/env python3
"""Deterministic release-archive builder for one published dataset.

Repo-side only — never shipped in the wheel; run through this repo's own
venv (`uv run python tools/build_dataset_pack.py <name> --out DIR`), since it
imports `open_emit`. Builds `<out>/<name>.tar.gz` from
`docs/examples/<name>/`: `bundle/{run.duckdb,base.json,ATLAS.md}` plus the
manifest entry's `configs` YAMLs at the example directory's root.

Print, never edit: the stamped fields (sha256, size_bytes,
base_format_version) are printed as a paste-ready YAML fragment for the
maintainer to commit into the manifest entry; the manifest itself is never
rewritten. Never talks to the network.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fabulexa_forge.datasets.manifest import load_manifest
from fabulexa_forge.reader import ReaderError, open_emit

if TYPE_CHECKING:
    from fabulexa_forge.datasets.models import DatasetEntry

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUNDLE_FILES = ("run.duckdb", "base.json", "ATLAS.md")
_FILE_MODE = 0o644
_HASH_CHUNK_SIZE = 65536


class PackBuildError(Exception):
    """A pack cannot be built: missing bundle file, missing config, or a
    bundle the reader refuses (including a base_format_version other than
    SUPPORTED_BASE_FORMAT_VERSION — the version refusal renders the reader's
    UnsupportedBaseFormatVersionError.found_version). The message is the
    refusal diagnostic; main maps it to stderr + exit 1."""


@dataclass(frozen=True)
class PackStamp:
    """The builder-stamped manifest fields for one built archive.

    Attributes:
        sha256: Digest of the archive bytes.
        size_bytes: Archive size in bytes.
        base_format_version: Stamped from the pack's own base.json, read
            through open_emit — never parsed ad hoc.
    """

    sha256: str
    size_bytes: int
    base_format_version: int


def _resolve_bundle_paths(bundle_dir: Path) -> dict[str, Path]:
    """Locate the bundle triple, naming any missing file.

    Args:
        bundle_dir: The example's `bundle/` directory.

    Returns:
        A {filename: path} mapping covering `_BUNDLE_FILES`.

    Raises:
        PackBuildError: A bundle file is missing, naming it.
    """
    paths: dict[str, Path] = {}
    for filename in _BUNDLE_FILES:
        path = bundle_dir / filename
        if not path.is_file():
            raise PackBuildError(f"missing bundle file: {path}")
        paths[filename] = path
    return paths


def _resolve_config_paths(
    entry: "DatasetEntry", example_dir: Path
) -> list[tuple[str, Path]]:
    """Locate every configs entry in the example directory, naming any absent one.

    Args:
        entry: The authored manifest entry naming the configs.
        example_dir: The dataset's example directory.

    Returns:
        (filename, path) pairs in the entry's authored `configs` order.

    Raises:
        PackBuildError: A configs entry is absent from example_dir, naming it.
    """
    paths: list[tuple[str, Path]] = []
    for filename in entry.configs:
        path = example_dir / filename
        if not path.is_file():
            raise PackBuildError(f"missing configs file: {path}")
        paths.append((filename, path))
    return paths


def _open_bundle_version(bundle_dir: Path) -> int:
    """Open the bundle through open_emit and return its base_format_version.

    Args:
        bundle_dir: The example's `bundle/` directory.

    Returns:
        The opened sidecar's base_format_version.

    Raises:
        PackBuildError: open_emit refuses the bundle (version refusal
            included), rendering the reader's own diagnostic.
    """
    try:
        with open_emit(bundle_dir) as emit:
            return emit.sidecar.base_format_version
    except ReaderError as exc:
        raise PackBuildError(str(exc)) from exc


def _member_tarinfo(archive_path: str, source_path: Path) -> tarfile.TarInfo:
    """Build a normalized TarInfo for one archive member.

    Normalization: mtime 0, uid/gid 0, uname/gname empty, mode 0644 — pinned
    so the same input tree always produces byte-identical archive bytes.
    """
    info = tarfile.TarInfo(name=archive_path)
    info.size = source_path.stat().st_size
    info.mtime = 0
    info.mode = _FILE_MODE
    info.type = tarfile.REGTYPE
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _write_deterministic_archive(
    out_path: Path, members: list[tuple[str, Path]]
) -> None:
    """Write a deterministic gzip-compressed tar archive.

    Members are added in sorted archive-path order; each member is
    normalized (see `_member_tarinfo`), and the gzip stream itself carries
    mtime 0 and an empty original-filename field.

    Args:
        out_path: The archive file to write.
        members: (archive_path, source_path) pairs.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
        for archive_path, source_path in sorted(members, key=lambda pair: pair[0]):
            info = _member_tarinfo(archive_path, source_path)
            with source_path.open("rb") as fileobj:
                archive.addfile(info, fileobj)
    with (
        out_path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
    ):
        gz.write(tar_buffer.getvalue())


def _sha256_file(path: Path) -> str:
    """Digest a file's bytes as it sits on disk."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def build_pack(entry: "DatasetEntry", example_dir: Path, out_path: Path) -> PackStamp:
    """Build one deterministic release archive from an example directory.

    Driven by the entry's authored fields: `configs` names the YAMLs packed;
    the entry's stamped fields (sha256, size_bytes, base_format_version) are
    ignored on read and recomputed. Archive layout: bundle/run.duckdb,
    bundle/base.json, bundle/ATLAS.md, and the configs at the archive root —
    all member paths relative, no wrapper directory.

    Deterministic means byte-identical: members added in sorted-path order;
    member mtime 0, uid/gid 0, uname/gname empty; mode 0644 for files, 0755
    for directories; gzip stream with mtime 0 and an empty original-filename
    field.

    Args:
        entry: The authored manifest entry driving the build.
        example_dir: The dataset's example directory
            (docs/examples/<name>/ in production use).
        out_path: The archive file to write (<out>/<name>.tar.gz in
            production use).

    Returns:
        The stamped fields computed from the written archive and the opened
        bundle.

    Raises:
        PackBuildError: Bundle triple (run.duckdb / base.json / ATLAS.md)
            incomplete, naming the missing file; a configs file absent from
            example_dir, naming it; the bundle refuses to open under
            open_emit (version refusal included).
    """
    bundle_dir = example_dir / "bundle"
    bundle_paths = _resolve_bundle_paths(bundle_dir)
    config_paths = _resolve_config_paths(entry, example_dir)
    base_format_version = _open_bundle_version(bundle_dir)

    members = [(f"bundle/{name}", path) for name, path in bundle_paths.items()]
    members.extend(config_paths)
    _write_deterministic_archive(out_path, members)

    return PackStamp(
        sha256=_sha256_file(out_path),
        size_bytes=out_path.stat().st_size,
        base_format_version=base_format_version,
    )


def render_stamp_fragment(stamp: PackStamp) -> str:
    """Render the stamped fields as a paste-ready YAML fragment for the
    maintainer to commit into the manifest entry, without trailing newline.

    Returns:
        Three lines: sha256, size_bytes, base_format_version.
    """
    return (
        f"sha256: {stamp.sha256}\n"
        f"size_bytes: {stamp.size_bytes}\n"
        f"base_format_version: {stamp.base_format_version}"
    )


def main(argv: list[str]) -> int:
    """Entry point: build_dataset_pack.py <name> --out DIR.

    Locates docs/examples/<name>/ relative to the repo root and the entry by
    name in the shipped manifest (load_manifest); writes <out>/<name>.tar.gz;
    prints the stamp fragment to stdout. Print, never edit — the manifest is
    never rewritten. Refusals (PackBuildError, unknown name) to stderr,
    exit 1. Never talks to the network.
    """
    parser = argparse.ArgumentParser(
        prog="build_dataset_pack.py",
        description="Build a deterministic release archive for one published dataset.",
    )
    parser.add_argument("name", help="Dataset name, matching a shipped manifest entry.")
    parser.add_argument(
        "--out", required=True, type=Path, help="Output directory for the archive."
    )
    args = parser.parse_args(argv)

    manifest = load_manifest()
    entry = next((e for e in manifest.datasets if e.name == args.name), None)
    if entry is None:
        valid = ", ".join(e.name for e in manifest.datasets)
        print(f"unknown dataset {args.name!r}; valid names: {valid}", file=sys.stderr)
        return 1

    example_dir = _REPO_ROOT / "docs" / "examples" / args.name
    out_path = args.out / f"{args.name}.tar.gz"
    try:
        stamp = build_pack(entry, example_dir, out_path)
    except PackBuildError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(render_stamp_fragment(stamp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
