"""Download, verify, and extract a published dataset pack.

`get_dataset` is the full pipeline behind `datasets get`: resolve the
manifest entry, check the target path, download to a temporary file, verify
(byte count, sha256, then archive member safety), prepare the target
directory, extract, delete the temporary archive, and return the entry's
example commands with `{dir}` substituted. The target path is never touched
before the downloaded archive fully verifies, and the temporary archive never
survives — deleted on success and on every failure path alike.
"""

from __future__ import annotations

import hashlib
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO, Callable

if TYPE_CHECKING:
    from fabulexa_forge.datasets.models import DatasetEntry, DatasetManifest

Transport = Callable[[str], BinaryIO]
"""Opens a URL for streamed reading. The CLI wires the stdlib HTTPS opener
with a network timeout (value is CLI presentation, not contract); tests wire
local-bytes openers. The seam that keeps the suite offline.

Failure contract: a failed open or read — timeout included — raises OSError
(which subsumes urllib's URLError/HTTPError). get_dataset maps OSError by
phase, not source: any OSError during download-and-verify (transport open,
transport read, temporary-file write — one streaming loop) becomes
DatasetError; nothing broader is caught, and post-verification OSError
propagates unwrapped."""

_DOWNLOAD_CHUNK_SIZE = 65536


class DatasetError(Exception):
    """A dataset operation failed: unknown name, occupied target, download
    failure, verification mismatch, or unsafe archive. The message is the
    user-facing diagnostic; the CLI maps it to stderr + exit 1."""


@dataclass(frozen=True)
class GetResult:
    """Outcome of a successful dataset get.

    Attributes:
        target_dir: Directory the pack was extracted into.
        commands: The entry's example commands with {dir} substituted.
    """

    target_dir: Path
    commands: tuple[str, ...]


def get_dataset(
    manifest: "DatasetManifest",
    name: str,
    target_dir: Path | None,
    force: bool,
    transport: Transport,
    progress: Callable[[int, int], None] | None,
) -> GetResult:
    """Download, verify, and extract one published dataset pack.

    Pipeline (design doc § `datasets get <name>`): resolve entry → check
    target path → download to a temporary file → verify (byte count, sha256,
    then every archive member's safety) → prepare target directory (--force
    removal only after all verification passes) → extract → delete the
    temporary archive → return next steps. The target path is never touched
    before the downloaded archive fully verifies; the temporary archive never
    survives — deleted on success and on every failure path alike.

    Args:
        manifest: The loaded manifest.
        name: Dataset to fetch; must match an entry's name.
        target_dir: Extraction directory; None means ./<name>. Created
            (parents included) when absent; the path as given here is what
            {dir} substitutes to — never absolutized.
        force: If True, an occupied target path (non-empty directory or
            non-directory) is removed and the directory recreated — only
            after the downloaded archive fully verifies (size, sha256,
            member safety), never before; if False, that condition refuses
            up front, before any download.
        transport: URL opener (HTTPS in the CLI, local bytes in tests).
        progress: Optional callback (bytes_received, size_bytes) for
            presentation-only progress reporting.

    Returns:
        GetResult with the target directory and substituted example commands.

    Raises:
        DatasetError: Unknown name; occupied target without force; download
            failure (any OSError during the download-and-verify phase —
            transport open/read and temporary-file write alike — mapped);
            size or sha256 mismatch; unsafe archive member (path escaping
            the target directory, or a non-regular member). All
            pre-extraction failures leave the target path exactly as found
            and the temporary archive deleted.
        OSError: An I/O failure after verification (target preparation or
            extraction), propagated unwrapped — environmental, not a
            dataset-contract failure; the target may hold a partial tree.
    """
    entry = _resolve_entry(manifest, name)
    resolved_target = target_dir if target_dir is not None else Path(entry.name)
    _check_target(resolved_target, force)

    temp_path = _download_to_temp(entry, transport, progress)
    try:
        with tarfile.open(temp_path, mode="r:gz") as archive:
            members = archive.getmembers()
            _check_members_safe(members)
            _prepare_target(resolved_target, force)
            archive.extractall(resolved_target, members=members, filter="data")
    finally:
        temp_path.unlink(missing_ok=True)

    commands = tuple(
        command.replace("{dir}", str(resolved_target)) for command in entry.commands
    )
    return GetResult(target_dir=resolved_target, commands=commands)


def _resolve_entry(manifest: "DatasetManifest", name: str) -> "DatasetEntry":
    """Look up a dataset entry by name, or raise DatasetError naming valid names."""
    for entry in manifest.datasets:
        if entry.name == name:
            return entry
    valid = ", ".join(entry.name for entry in manifest.datasets)
    raise DatasetError(f"unknown dataset {name!r}; valid names: {valid}")


def _check_target(target_dir: Path, force: bool) -> None:
    """Refuse an occupied target path up front when force is False."""
    if force or not target_dir.exists():
        return
    if target_dir.is_dir():
        if not any(target_dir.iterdir()):
            return
        raise DatasetError(f"target directory {target_dir} is not empty; use --force")
    raise DatasetError(f"target path {target_dir} exists and is not a directory")


def _download_to_temp(
    entry: "DatasetEntry",
    transport: Transport,
    progress: Callable[[int, int], None] | None,
) -> Path:
    """Download the entry's URL to a verified temporary archive file.

    Raises:
        DatasetError: Any OSError during the streaming download-and-write
            loop, a byte-count mismatch, or a sha256 mismatch. The temporary
            file is removed before raising.
    """
    fd, temp_name = tempfile.mkstemp(suffix=".tar.gz")
    temp_path = Path(temp_name)
    try:
        digest = hashlib.sha256()
        received = 0
        with open(fd, "wb") as temp_file:
            try:
                with transport(entry.url) as source:
                    while chunk := source.read(_DOWNLOAD_CHUNK_SIZE):
                        temp_file.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                        if progress is not None:
                            progress(received, entry.size_bytes)
            except OSError as exc:
                raise DatasetError(
                    f"failed to download dataset {entry.name!r} from {entry.url}: {exc}"
                ) from exc
        if received != entry.size_bytes:
            raise DatasetError(
                f"dataset {entry.name!r}: expected {entry.size_bytes} bytes, "
                f"got {received}"
            )
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != entry.sha256:
            raise DatasetError(
                f"dataset {entry.name!r}: sha256 mismatch — expected "
                f"{entry.sha256}, got {actual_sha256}"
            )
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def _check_members_safe(members: list[tarfile.TarInfo]) -> None:
    """Refuse an archive containing an unsafe member.

    Unsafe = a path escaping the extraction root (absolute, or containing a
    `..` component), or a member that is not a regular file or directory.

    Raises:
        DatasetError: The first unsafe member found, naming it.
    """
    for member in members:
        if (
            PurePosixPath(member.name).is_absolute()
            or ".." in PurePosixPath(member.name).parts
        ):
            raise DatasetError(f"unsafe archive member path: {member.name!r}")
        if not (member.isfile() or member.isdir()):
            raise DatasetError(f"unsafe archive member type: {member.name!r}")


def _prepare_target(target_dir: Path, force: bool) -> None:
    """Create the target directory, removing an occupied one first if forced."""
    if force and target_dir.exists():
        if target_dir.is_dir():
            shutil.rmtree(target_dir)
        else:
            target_dir.unlink()
    target_dir.mkdir(parents=True, exist_ok=True)
