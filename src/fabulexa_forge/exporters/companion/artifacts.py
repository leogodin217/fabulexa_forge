"""Mode-neutral companion artifact writer: the README + manifest pair every
file-writing export invocation deposits beside its datasets (design doc
`export-companion-artifacts.md` § Artifact names and placement, § Writing
rules).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from fabulexa_forge.errors import ExportRuntimeError
from fabulexa_forge.exporters.companion.manifest import (
    build_manifest_document,
    render_manifest_bytes,
)
from fabulexa_forge.exporters.companion.readme import render_readme

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.exporters.companion.overlay import ReadmeOverlay
    from fabulexa_forge.exporters.query_spec import ExportReport
    from fabulexa_forge.reader.emit import Emit

_FILE_WRITING_MODES: tuple[str, ...] = ("dimensional", "source", "base")

_COMPANION_ARTIFACT_NAMES: frozenset[str] = frozenset(
    f"{mode}-{suffix}"
    for mode in _FILE_WRITING_MODES
    for suffix in ("readme.md", "manifest.json")
)


@dataclass(frozen=True)
class WindowedArtifactState:
    """The windowed facts a companion-artifact rewrite records.

    `regime` is the incremental cadence in force; `label` is the emitting
    invocation's window or range label; `next_window_index` is the cursor's
    next index after a `--next` window, or None for a `--from`/`--to` range
    (stateless: no cursor exists to have a next index).
    """

    regime: Literal["calendar", "sim_time"]
    label: str
    next_window_index: int | None


def _artifact_paths(
    target: "Path", mode: str, fmt: Literal["csv", "duckdb"]
) -> tuple["Path", "Path"]:
    """The (readme_path, manifest_path) pair for one export target.

    A `csv` target is an output directory: the pair lands inside it, named
    `<mode>-*`. A `duckdb` target is the `.duckdb` file path: the pair lands
    beside it, named `<db-stem>-<mode>-*` (design doc § Artifact names and
    placement).

    Args:
        target: The output directory (csv) or `.duckdb` file path (duckdb).
        mode: The export config's mode literal.
        fmt: The resolved output format.

    Returns:
        The (readme_path, manifest_path) pair.
    """
    if fmt == "duckdb":
        prefix = f"{target.stem}-{mode}"
        placement = target.parent
    else:
        prefix = mode
        placement = target
    return placement / f"{prefix}-readme.md", placement / f"{prefix}-manifest.json"


def write_companion_artifacts(
    emit: "Emit",
    config: "ExportConfig",
    fmt: Literal["csv", "duckdb"],
    anchor: "EffectiveAnchor | None",
    report: "ExportReport",
    overlay: "ReadmeOverlay | None",
    target: "Path",
    windowed: "WindowedArtifactState | None",
) -> None:
    """Render and write both companion artifacts for one export invocation.

    Mode-neutral; placement/prefix follow the target (directory -> '<mode>-*'
    inside it; .duckdb file -> '<db-stem>-<mode>-*' beside it). Overwrites
    unconditionally. Called only after all data of the invocation is
    delivered; never on a drained or failed invocation.

    Args:
        emit: The open emit (sidecar identity, base.json bytes for hashing).
        config: The validated export config.
        fmt: The resolved output format.
        anchor: The resolved effective anchor, or None.
        report: The invocation's per-table report.
        overlay: The parsed overlay, or None.
        target: The output directory (csv) or `.duckdb` file path (duckdb).
        windowed: Windowed invocation facts, or None for a full export.

    Raises:
        ExportRuntimeError: An artifact file cannot be written.
    """
    readme_path, manifest_path = _artifact_paths(target, config.mode, fmt)
    manifest_document = build_manifest_document(
        emit=emit,
        config=config,
        fmt=fmt,
        anchor=anchor,
        report=report,
        windowed=windowed,
    )
    manifest_bytes = render_manifest_bytes(manifest_document)
    readme_text = render_readme(
        mode=config.mode,
        emit=emit,
        report=report,
        overlay=overlay,
        anchor=anchor,
        manifest_filename=manifest_path.name,
    )
    try:
        manifest_path.write_bytes(manifest_bytes)
        readme_path.write_text(readme_text, encoding="utf-8")
    except OSError as exc:
        raise ExportRuntimeError(
            f"failed to write companion artifacts under {readme_path.parent}: {exc}"
        ) from exc


def is_companion_artifact_name(name: str) -> bool:
    """Whether a directory entry is a companion artifact of any file-writing mode.

    True for `<mode>-readme.md` / `<mode>-manifest.json` with mode in
    {dimensional, source, base}. Used by the incremental CSV fresh/lost
    census to exclude artifacts from the non-hidden-entry count.

    Args:
        name: A directory entry basename.

    Returns:
        True iff `name` is a companion artifact filename.
    """
    return name in _COMPANION_ARTIFACT_NAMES
