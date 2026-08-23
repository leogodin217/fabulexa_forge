"""Dataset distribution: manifest models, loader, listing renderer, and the
fetch/verify/extract pipeline."""

from __future__ import annotations

from fabulexa_forge.datasets.fetch import (
    DatasetError,
    GetResult,
    Transport,
    get_dataset,
)
from fabulexa_forge.datasets.manifest import load_manifest, render_dataset_listing
from fabulexa_forge.datasets.models import DatasetEntry, DatasetManifest

__all__ = [
    "DatasetEntry",
    "DatasetError",
    "DatasetManifest",
    "GetResult",
    "Transport",
    "get_dataset",
    "load_manifest",
    "render_dataset_listing",
]
