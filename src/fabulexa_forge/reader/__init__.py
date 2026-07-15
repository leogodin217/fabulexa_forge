"""Public reader API for fabulexa_forge."""

from __future__ import annotations

from fabulexa_forge.reader.conformance import (
    CheckResult,
    ConformanceReport,
    run_check,
    validate,
)
from fabulexa_forge.reader.emit import Emit, open_emit
from fabulexa_forge.reader.errors import (
    ColumnNotFoundError,
    EmitNotFoundError,
    ReaderError,
    RunDatabaseError,
    SidecarParseError,
    SidecarStructureError,
    TableNotFoundError,
    TemporalClassUnavailableError,
    UnsupportedBaseFormatVersionError,
)
from fabulexa_forge.reader.relations import (
    build_history_relation_sql,
    build_membership_relation_sql,
    build_records_relation_sql,
    distinct_prop_values,
)
from fabulexa_forge.reader.sidecar import (
    BranchEntry,
    ColumnSpec,
    RecordRoles,
    RuntimeAnchor,
    Sidecar,
    TableSpec,
    TemporalClass,
)

__all__ = [
    "BranchEntry",
    "CheckResult",
    "ColumnNotFoundError",
    "ColumnSpec",
    "ConformanceReport",
    "Emit",
    "EmitNotFoundError",
    "ReaderError",
    "RecordRoles",
    "RunDatabaseError",
    "RuntimeAnchor",
    "Sidecar",
    "SidecarParseError",
    "SidecarStructureError",
    "TableNotFoundError",
    "TableSpec",
    "TemporalClass",
    "TemporalClassUnavailableError",
    "UnsupportedBaseFormatVersionError",
    "build_history_relation_sql",
    "build_membership_relation_sql",
    "build_records_relation_sql",
    "distinct_prop_values",
    "open_emit",
    "run_check",
    "validate",
]
