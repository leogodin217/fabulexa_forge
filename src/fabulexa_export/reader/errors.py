"""Reader error hierarchy for fabulexa_export.reader.

All errors are operational/structural problems that prevent opening or querying.
Conformance failures (C1-C11) are reported as failing CheckResults, never raised.
"""

from __future__ import annotations


class ReaderError(Exception):
    """Base for all reader errors."""


class EmitNotFoundError(ReaderError):
    """emit_dir, run.duckdb, or base.json is absent."""


class SidecarParseError(ReaderError):
    """base.json is not valid JSON."""


class UnsupportedBaseFormatVersionError(ReaderError):
    """base_format_version is a present integer other than the supported version.

    Carries the offending version as `found_version`.
    """

    def __init__(self, found_version: int) -> None:
        self.found_version = found_version
        super().__init__(
            f"unsupported base_format_version {found_version}; no auto-upgrade"
        )


class SidecarStructureError(ReaderError):
    """base.json is the supported version but lacks the required top-level structure."""


class RunDatabaseError(ReaderError):
    """run.duckdb is unreadable, or a query failed to execute against it."""


class TableNotFoundError(ReaderError):
    """A table name was requested that the sidecar does not declare."""
