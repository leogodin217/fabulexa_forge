#!/usr/bin/env python3
"""Deterministic release-archive builder for one published dataset.

Repo-side only — never shipped in the wheel; run through this repo's own
venv (`uv run python tools/build_dataset_pack.py <name> --out DIR`), since it
imports `open_emit`. Builds `<out>/<name>.tar.gz` from
`docs/examples/<name>/`: `bundle/{run.duckdb,base.json,ATLAS.md}` plus the
manifest entry's `configs` YAMLs at the example directory's root, and every
config-relative file those configs name (file supplements, `readme_overlay`).

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

from fabulexa_forge.config.loader import (
    load_export_config,
    load_stream_config,
    load_yaml_mapping,
)
from fabulexa_forge.config.models import ExportConfig
from fabulexa_forge.datasets.manifest import load_manifest
from fabulexa_forge.errors import ConfigError
from fabulexa_forge.exporters.supplements import load_supplements
from fabulexa_forge.reader import ReaderError, open_emit

if TYPE_CHECKING:
    from collections.abc import Callable

    from fabulexa_forge.config.models import StreamConfig
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


def _load_packed_config(
    dataset_name: str, config_name: str, config_path: Path
) -> "ExportConfig | StreamConfig":
    """Load one configs entry through the loader its top-level shape names.

    Peeks the parsed YAML document's top-level keys to choose the loader — a
    `mode` key means `load_export_config`, a `streams` key means
    `load_stream_config` — then re-loads through that loader for its own
    diagnostics (validation, duplicate keys, etc).

    Args:
        dataset_name: The manifest entry's name, for the error prefix.
        config_name: The configs entry's filename, for the error prefix.
        config_path: The config file's path.

    Returns:
        The validated ExportConfig or StreamConfig.

    Raises:
        PackBuildError: The document has neither a `mode` nor a `streams`
            top-level key, or the chosen loader refuses it (the loader's own
            diagnostic, prefixed `"dataset '{name}': config '{cfg}': "`).
    """
    prefix = f"dataset '{dataset_name}': config '{config_name}': "
    raw = config_path.read_text(encoding="utf-8")
    try:
        data = load_yaml_mapping(raw, "config", config_path)
    except ConfigError as exc:
        raise PackBuildError(f"{prefix}{exc}") from exc
    loader: Callable[[Path], ExportConfig | StreamConfig]
    if isinstance(data, dict) and "mode" in data:
        loader = load_export_config
    elif isinstance(data, dict) and "streams" in data:
        loader = load_stream_config
    else:
        raise PackBuildError(f"{prefix}neither an export nor a stream config")
    try:
        return loader(config_path)
    except ConfigError as exc:
        raise PackBuildError(f"{prefix}{exc}") from exc


def _config_file_member(
    dataset_name: str,
    config_name: str,
    label: str,
    source_path: Path,
    example_dir: Path,
) -> tuple[str, Path]:
    """Place one config-named file (a file supplement's source, the
    `readme_overlay`) as an archive member.

    Args:
        dataset_name: The manifest entry's name, for the error prefix.
        config_name: The configs entry's filename, for the error prefix.
        label: What the config named the file as, for the error message
            (`supplement '<name>'`, `readme_overlay`).
        source_path: The file's resolved (absolute) source path.
        example_dir: The dataset's example directory (resolved).

    Returns:
        The (config-relative archive path, source path) pair.

    Raises:
        PackBuildError: source_path does not resolve inside example_dir.
    """
    if not source_path.is_relative_to(example_dir):
        raise PackBuildError(
            f"dataset '{dataset_name}': config '{config_name}':"
            f" {label}: file {source_path} is outside"
            f" the dataset directory {example_dir}"
        )
    return source_path.relative_to(example_dir).as_posix(), source_path


def _packed_config_members(
    entry: "DatasetEntry", example_dir: Path
) -> list[tuple[str, Path]]:
    """Locate every configs entry, load it through the loader its top-level
    shape names, and collect the files it names as members.

    A document with a top-level `mode` key loads through
    `load_export_config`; one with a top-level `streams` key through
    `load_stream_config`; a document with neither is refused. For an export
    config, `load_supplements(config, example_dir)` resolves every file
    supplement and the config's `readme_overlay` resolves beside it, exactly
    as the CLI resolves them at export time; each is a member at its
    config-relative path, so the packed config runs from the extracted pack.

    Args:
        entry: The authored manifest entry naming the configs.
        example_dir: The dataset's example directory.

    Returns:
        (archive path, source path) pairs: each config in the entry's
        authored order, then each of its supplement files in declaration
        order, then its overlay. Determinism is unaffected —
        `_write_deterministic_archive` sorts members by path.

    Raises:
        PackBuildError: A configs entry is absent, naming it; a config is
            neither an export nor a stream config; a config its loader
            refuses (the loader's diagnostic, prefixed `"dataset '{name}':
            config '{cfg}': "`); a supplement file or overlay file that is
            missing or whose resolved path is outside `example_dir`.
    """
    resolved_example_dir = example_dir.resolve()
    members: list[tuple[str, Path]] = []
    for filename in entry.configs:
        config_path = example_dir / filename
        if not config_path.is_file():
            raise PackBuildError(f"missing configs file: {config_path}")
        prefix = f"dataset '{entry.name}': config '{filename}': "
        config = _load_packed_config(entry.name, filename, config_path)
        members.append((filename, config_path))
        if not isinstance(config, ExportConfig):
            continue
        try:
            supplements = load_supplements(config, config_path.parent)
        except ConfigError as exc:
            raise PackBuildError(f"{prefix}{exc}") from exc
        for supplement in supplements:
            if supplement.path is None:
                continue
            members.append(
                _config_file_member(
                    entry.name,
                    filename,
                    f"supplement '{supplement.decl.name}'",
                    supplement.path,
                    resolved_example_dir,
                )
            )
        if config.readme_overlay is not None:
            overlay_path = (config_path.parent / config.readme_overlay).resolve()
            if not overlay_path.is_file():
                raise PackBuildError(
                    f"{prefix}readme_overlay: missing file {overlay_path}"
                )
            members.append(
                _config_file_member(
                    entry.name,
                    filename,
                    "readme_overlay",
                    overlay_path,
                    resolved_example_dir,
                )
            )
    return members


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
    bundle/base.json, bundle/ATLAS.md, the configs at the archive root, and
    (for an export config) any file supplement's source at its
    config-relative path — all member paths relative, no wrapper directory.
    Loaded through the loader its top-level shape names
    (`_packed_config_members`) — a config the loader refuses fails the build.

    Deterministic means byte-identical: members added in sorted-path order;
    member mtime 0, uid/gid 0, uname/gname empty, mode 0644 (files only — the
    archive has no directory members); gzip stream with mtime 0 and an empty
    original-filename field.

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
            example_dir, naming it; a config neither an export nor a stream
            config, or one its loader refuses (see
            `_packed_config_members`); a supplement file missing or outside
            example_dir; the bundle refuses to open under open_emit (version
            refusal included).
    """
    bundle_dir = example_dir / "bundle"
    bundle_paths = _resolve_bundle_paths(bundle_dir)
    config_members = _packed_config_members(entry, example_dir)
    base_format_version = _open_bundle_version(bundle_dir)

    members = [(f"bundle/{name}", path) for name, path in bundle_paths.items()]
    members.extend(config_members)
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
