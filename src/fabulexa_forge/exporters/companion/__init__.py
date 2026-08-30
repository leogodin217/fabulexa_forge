"""Companion artifacts: the author README overlay grammar, and the
mode-neutral README + manifest writer every file-writing export invocation
deposits beside its datasets."""

from __future__ import annotations

from fabulexa_forge.exporters.companion.artifacts import (
    WindowedArtifactState,
    is_companion_artifact_name,
    write_companion_artifacts,
)
from fabulexa_forge.exporters.companion.overlay import (
    ReadmeOverlay,
    load_readme_overlay,
    validate_overlay_tables,
)
from fabulexa_forge.exporters.query_spec import ExportReport, TableReport

__all__ = [
    "ExportReport",
    "ReadmeOverlay",
    "TableReport",
    "WindowedArtifactState",
    "is_companion_artifact_name",
    "load_readme_overlay",
    "validate_overlay_tables",
    "write_companion_artifacts",
]
