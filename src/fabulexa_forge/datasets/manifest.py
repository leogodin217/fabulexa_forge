"""Loader and listing renderer for the dataset distribution manifest.

The manifest ships as package data at
`src/fabulexa_forge/datasets/manifest.yaml`; `importlib.resources` resolves
one path in both the wheel and the in-tree layout (hatchling auto-includes
files inside `packages = ["src/fabulexa_forge"]`).
"""

from __future__ import annotations

import importlib.resources
import json
from typing import Any

import yaml

from fabulexa_forge.datasets.models import DatasetEntry, DatasetManifest

_NO_DATASETS_LINE = "no datasets published for this version"

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def load_manifest() -> DatasetManifest:
    """Load and validate the dataset manifest shipped as package data.

    Resolves `importlib.resources.files("fabulexa_forge") / "datasets" /
    "manifest.yaml"` — one path, valid in both the wheel and in-tree layouts.

    Returns:
        The validated manifest, entries in authored order.

    Raises:
        ValidationError: If the packaged manifest does not satisfy the model.
        yaml.YAMLError: If the packaged document is not parseable YAML,
            propagated from the parser.
        (Both unreachable in a released wheel — the hygiene test loads the
        manifest and gates the build — but loud during development.)
    """
    resource = (
        importlib.resources.files("fabulexa_forge") / "datasets" / "manifest.yaml"
    )
    raw = resource.read_text(encoding="utf-8")
    data: Any = yaml.safe_load(raw)
    return DatasetManifest.model_validate(data)


def render_dataset_listing(manifest: DatasetManifest, fmt: str) -> str:
    """Render the manifest as the `datasets list` payload.

    Args:
        manifest: The loaded manifest.
        fmt: "text" for the human table, "json" for the byte-stable
            document (the manifest's field set verbatim, raw values,
            authored entry order, sorted keys, separators (",", ":")).
            An empty manifest renders the no-datasets line under "text"
            and the model document verbatim under "json".

    Returns:
        The complete stdout payload, without trailing newline.

    Raises:
        ValueError: `fmt` is neither "text" nor "json".
    """
    if fmt == "json":
        return _render_json(manifest)
    if fmt == "text":
        return _render_text(manifest)
    raise ValueError(f"unknown listing format: {fmt!r}")


def _render_json(manifest: DatasetManifest) -> str:
    """Render the manifest as the byte-stable JSON listing document."""
    return json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def _render_text(manifest: DatasetManifest) -> str:
    """Render the manifest as the human-readable text listing."""
    if not manifest.datasets:
        return _NO_DATASETS_LINE
    return "\n\n".join(_render_entry(entry) for entry in manifest.datasets)


def _render_entry(entry: DatasetEntry) -> str:
    """Render one dataset entry's text-listing block."""
    size = _human_size(entry.size_bytes)
    header = f"{entry.name} ({size}, base_format_version={entry.base_format_version})"
    configs = f"  configs: {', '.join(entry.configs)}"
    description = f"  {entry.description}"
    return "\n".join((header, configs, description))


def _human_size(size_bytes: int) -> str:
    """Render a byte count as a human-readable binary-unit size string."""
    size = float(size_bytes)
    unit = _SIZE_UNITS[0]
    for unit in _SIZE_UNITS:
        if size < 1024 or unit == _SIZE_UNITS[-1]:
            break
        size /= 1024
    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.1f} {unit}"
