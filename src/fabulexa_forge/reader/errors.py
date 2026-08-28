"""Reader error hierarchy for fabulexa_forge.reader.

All errors are operational/structural problems that prevent opening or querying.
Conformance failures (C1-C15) are reported as failing CheckResults, never raised.
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


class ColumnNotFoundError(ReaderError):
    """A named column is not declared on the named table."""


class PresentationKeysInvalidError(ReaderError):
    """The sidecar's presentation_keys block is present but incoherent.

    Raised by Sidecar.presentation_keys() naming the kind (and sub-type)
    and the violated clause. Absence of the block never raises.
    """


class TemporalClassUnavailableError(ReaderError):
    """A column whose point-in-time class is required has no usable one.

    Raised for a column carrying neither temporal attribute (a structural or
    identity column — conformant, but it has no temporal semantics to ask about),
    for a declared history_tracked with no paired temporal_class, and for a
    declared class outside the three-value enum (both non-conformant, C13). The
    message distinguishes the cases; the non-conformant ones direct the caller to
    `fabulexa-forge validate`. Raised rather than inferring a class from
    history_tracked: that inference is the fiction the contract's
    `base_format_version 5` bump exists to delete (it introduced the explicit
    `temporal_class` attribute so a reader never has to guess).
    """
