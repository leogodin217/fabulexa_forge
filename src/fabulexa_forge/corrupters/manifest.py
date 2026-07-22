"""Defect manifest value and model types.

The manifest side of the corrupter engine's one seam: an operation emits
`DefectRecord`s; `manifest_build.build_defect_manifest` canonicalises and
id-assigns them into a `DefectManifest`. This module owns the frozen,
extra-forbidding models and their parse-time validators — see
`docs/architecture/pending/corrupter-engine-and-manifest.md` § Manifest Value
Types / Manifest Model Types (normative).
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator
from typing_extensions import Self

from fabulexa_forge.config.models import StrictBaseModel

# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------

ImpactCode = Literal[
    "C6",
    "C7",
    "C9",
    "C10",
    "C11",
    "C12",
    "C13",
    "beyond-c1-c12",
]
"""A semantic conformance check id, or the sentinel for defects outside those codes.

Only the semantic checks a corrupter can break appear (C6, C7, C9-C13); a
corrupter preserves structural conformance (C1-C5, C8) and the sidecar-only
sub-type check (C14) by construction, so the vocabulary excludes them. C13's
genesis clause is breakable -- `insert_rows` (a phantom carries no history),
`schema_drift` (a renamed tracked column strands its history under the old
property name), and `shift_sim_time` / `drop_events` (moving or dropping a
series' genesis tick). The `beyond-c1-c12` sentinel keeps its historical spelling
for defects that break none of these codes; it is mutually exclusive with the
real codes -- normalize_impact rejects a mix."""

RowCategory = Literal["records", "history", "membership"]
"""The row-identity scheme a RowRef uses -- a RowRef tag, not a base table
category: `history` tags the fixed-category `history` table's five-column tick
identity, while `records` and `membership` tag those categories' identity
prefixes."""

DEFECT_MANIFEST_VERSION = 1
"""The manifest's own schema version (independent of base_format_version), a
code constant stamped by build_defect_manifest -- never a caller input."""

# A non-empty lower_snake_case identifier: a leading letter, single-underscore-
# separated segments, no leading/trailing/double underscore. Shared by
# defect_class (below) and, via manifest_build, the locator table-name segments
# (the design doc's "same single-underscore snake_case ... unambiguous
# delimiter").
IDENTIFIER_PATTERN = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
_IDENTIFIER_RE = re.compile(rf"^{IDENTIFIER_PATTERN}$")

_ROW_REF_PREFIXES: dict[RowCategory, tuple[str, ...]] = {
    "records": ("fork_path", "record_id"),
    "history": ("fork_path", "kind", "record_id", "property", "sim_time"),
    "membership": ("fork_path", "record_id", "joined_sim_time"),
}


def _normalize_impact(v: object) -> tuple[str, ...]:
    """Sort + dedup an impact set; reject empty and the sentinel/real-code mix.

    Args:
        v: The raw `impact` value (expected list/tuple of ImpactCode strings).

    Returns:
        A sorted, deduplicated tuple of the impact codes.

    Raises:
        ValueError: v is not a list/tuple, is empty, or mixes 'beyond-c1-c12'
            with a real code.
    """
    if not isinstance(v, (list, tuple)):
        raise ValueError("impact must be a list of ImpactCode values")
    codes = sorted(set(v))
    if not codes:
        raise ValueError("impact must be non-empty")
    if "beyond-c1-c12" in codes and len(codes) > 1:
        raise ValueError(
            "impact cannot mix 'beyond-c1-c12' (no C1-C12 code fired) with a "
            f"real code; got {codes}"
        )
    return tuple(codes)


# ---------------------------------------------------------------------------
# RowRef and Locator
# ---------------------------------------------------------------------------


class RowRef(StrictBaseModel):
    """A base row's structural identity, as codec-text key/value pairs."""

    model_config = ConfigDict(frozen=True)
    category: RowCategory
    keys: tuple[tuple[str, str], ...]

    @model_validator(mode="after")
    def check_rowref_matches_category(self) -> Self:
        """RowRef.keys column names equal the category's identity prefix, in
        order: records -> (fork_path, record_id); history -> (fork_path,
        kind, record_id, property, sim_time); membership -> (fork_path,
        record_id, joined_sim_time)."""
        expected = _ROW_REF_PREFIXES[self.category]
        actual = tuple(name for name, _ in self.keys)
        if actual != expected:
            raise ValueError(
                f"RowRef.keys column names {actual} do not match category "
                f"{self.category!r} prefix {expected}"
            )
        return self


class ColumnLocator(StrictBaseModel):
    """Locates a defect at whole-column granularity (dropped/renamed/retyped)."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["column"]
    table: str
    column: str


class RowLocator(StrictBaseModel):
    """Locates a defect at whole-row granularity (an injected duplicate row)."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["row"]
    table: str
    row: RowRef


class CellLocator(StrictBaseModel):
    """Locates a defect at single-cell granularity (a nulled/overwritten cell)."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["cell"]
    table: str
    row: RowRef
    column: str


Locator = Annotated[
    ColumnLocator | RowLocator | CellLocator,
    Field(discriminator="kind"),
]
"""A base coordinate at one of three granularities: column, row, or cell."""


# ---------------------------------------------------------------------------
# DefectRecord / ManifestDefect
# ---------------------------------------------------------------------------


class DefectRecord(StrictBaseModel):
    """One declared defect, as emitted by an operation (before id assignment).

    Fields:
        defect_class: The class tag (non-empty lower_snake_case identifier).
            Serialized on disk as the JSON key `class` via a pydantic field
            alias (alias="class", populate_by_name=True): both spellings
            validate on read-back, and `model_dump(by_alias=True)` /
            `model_json_schema(by_alias=True)` key on `class`.
        rule: The config operation's label that requested this injection (its
            `name`, or the "{kind}#{index}" fallback).
        impact: The complete set of guarantees this defect breaks; one or
            more ImpactCode, non-empty.
        location: The base coordinate where the defect was injected.
    """

    model_config = ConfigDict(frozen=True)
    defect_class: str = Field(alias="class")
    rule: str
    impact: tuple[ImpactCode, ...]
    location: Locator

    @field_validator("impact", mode="before")
    @classmethod
    def normalize_impact(cls, v: object) -> tuple[str, ...]:
        """impact is a set of codes: require at least one ImpactCode, reject
        any mix of `beyond-c1-c12` with a real code, and normalize to a
        sorted, deduplicated tuple."""
        return _normalize_impact(v)

    @model_validator(mode="after")
    def check_class_tag_shape(self) -> Self:
        """defect_class is a non-empty lower_snake_case identifier matching
        `^[a-z][a-z0-9]*(_[a-z0-9]+)*$` -- a leading letter, single-
        underscore-separated segments, no leading/trailing/double
        underscore."""
        if not _IDENTIFIER_RE.match(self.defect_class):
            raise ValueError(
                f"defect_class {self.defect_class!r} is not a non-empty "
                "lower_snake_case identifier"
            )
        return self


class ManifestDefect(DefectRecord):
    """A DefectRecord after the engine assigns its deterministic id."""

    defect_id: str


# ---------------------------------------------------------------------------
# DefectManifest
# ---------------------------------------------------------------------------


class DefectSource(StrictBaseModel):
    """Stable identity of the input base emit this manifest describes."""

    model_config = ConfigDict(frozen=True)
    sidecar_sha256: str
    base_format_version: int


class DefectCounts(StrictBaseModel):
    """Derived summary; must equal the aggregation of `defects`.

    by_class partitions defects -- each defect has exactly one class, so
    sum(by_class.values()) == len(defects). by_impact counts code
    occurrences: a defect with a multi-code impact (e.g. ["C6", "C7"]) adds +1
    to each code it names, so sum(by_impact.values()) >= len(defects).
    """

    model_config = ConfigDict(frozen=True)
    by_class: dict[str, int]
    by_impact: dict[str, int]


class DefectManifest(StrictBaseModel):
    """The full defect manifest serialized to defects.json.

    Fields:
        defect_manifest_version: Our schema version (independent of the base
            format version), supplied by build_defect_manifest from the
            module constant DEFECT_MANIFEST_VERSION -- a code constant, not a
            caller input.
        source: Identity of the input base emit.
        config_fingerprint: SHA-256 of the canonicalized corrupter config.
        code_version: The fabulexa-export package version string.
        counts: Derived per-class and per-impact totals.
        defects: The canonically ordered, id-assigned defect records.
    """

    model_config = ConfigDict(frozen=True)
    defect_manifest_version: int
    source: DefectSource
    config_fingerprint: str
    code_version: str
    counts: DefectCounts
    defects: tuple[ManifestDefect, ...]

    @model_validator(mode="after")
    def check_counts_match(self) -> Self:
        """counts equals the aggregation of defects: by_class partitions the
        records (sum == len(defects)); by_impact tallies each impact-code
        occurrence (sum >= len(defects)). Enforced on every construction
        path, so a hand-assembled or round-tripped manifest cannot carry
        stale counts."""
        expected_by_class: dict[str, int] = {}
        expected_by_impact: dict[str, int] = {}
        for defect in self.defects:
            expected_by_class[defect.defect_class] = (
                expected_by_class.get(defect.defect_class, 0) + 1
            )
            for code in defect.impact:
                expected_by_impact[code] = expected_by_impact.get(code, 0) + 1
        if self.counts.by_class != expected_by_class:
            raise ValueError(
                "counts.by_class does not match the aggregation of defects: "
                f"expected {expected_by_class}, got {self.counts.by_class}"
            )
        if self.counts.by_impact != expected_by_impact:
            raise ValueError(
                "counts.by_impact does not match the aggregation of "
                f"defects: expected {expected_by_impact}, got "
                f"{self.counts.by_impact}"
            )
        return self
