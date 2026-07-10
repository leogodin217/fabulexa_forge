"""Corrupter engine + defect manifest subsystem.

Reads a conformant base-layer emit and writes a structurally-conformant,
semantically-broken base emit plus `defects.json` — the deterministic,
label-grade ground-truth manifest of every injected defect. See
`docs/architecture/pending/corrupter-engine-and-manifest.md` (normative).
"""

from __future__ import annotations

from fabulexa_forge.corrupters.base_writer import write_base_emit
from fabulexa_forge.corrupters.engine import corrupt_emit
from fabulexa_forge.corrupters.fingerprint import fingerprint_config
from fabulexa_forge.corrupters.manifest import (
    DEFECT_MANIFEST_VERSION,
    CellLocator,
    ColumnLocator,
    DefectCounts,
    DefectManifest,
    DefectRecord,
    DefectSource,
    ImpactCode,
    Locator,
    ManifestDefect,
    RowCategory,
    RowLocator,
    RowRef,
)
from fabulexa_forge.corrupters.manifest_build import (
    build_defect_manifest,
    derive_defect_id,
    write_defect_manifest,
)
from fabulexa_forge.corrupters.operations import CORRUPTER_REGISTRY, Corrupter
from fabulexa_forge.corrupters.selection import draw_sample
from fabulexa_forge.corrupters.state import (
    CorruptReport,
    CorruptState,
    OperationOutcome,
    WorkingTable,
)
from fabulexa_forge.corrupters.validate import validate_corrupt_config

__all__ = [
    "CORRUPTER_REGISTRY",
    "DEFECT_MANIFEST_VERSION",
    "CellLocator",
    "ColumnLocator",
    "Corrupter",
    "CorruptReport",
    "CorruptState",
    "DefectCounts",
    "DefectManifest",
    "DefectRecord",
    "DefectSource",
    "ImpactCode",
    "Locator",
    "ManifestDefect",
    "OperationOutcome",
    "RowCategory",
    "RowLocator",
    "RowRef",
    "WorkingTable",
    "build_defect_manifest",
    "corrupt_emit",
    "derive_defect_id",
    "draw_sample",
    "fingerprint_config",
    "validate_corrupt_config",
    "write_base_emit",
    "write_defect_manifest",
]
