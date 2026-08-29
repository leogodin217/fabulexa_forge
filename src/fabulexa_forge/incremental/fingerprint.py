"""Fingerprint computation for incremental drip identity.

A SHA-256 digest over a canonical JSON document that uniquely identifies a
drip session: config, anchor, emit, branch, output format, and code version.
Any change to any input yields a different fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Literal

from fabulexa_forge.anchor import anchor_to_json

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ExportConfig

#: Config surfaces excluded from the canonical dump — presentation-only or
#: (the three description overrides) authored prose that re-voices but never
#: reshapes an export. Changing any of these mid-drip must not raise a
#: fingerprint mismatch. Nested dict form per Pydantic's `model_dump(exclude=)`.
_FINGERPRINT_EXCLUDE: "dict[str, Any]" = {
    "readme_overlay": True,
    "dimensional": {"tables": {"__all__": {"columns": {"__all__": {"description"}}}}},
    "source": {"tables": {"__all__": {"descriptions"}}},
    "base": {"rename": {"__all__": {"descriptions"}}},
}


def compute_fingerprint(
    config: "ExportConfig",
    anchor: "EffectiveAnchor | None",
    sidecar_sha256: str,
    fork_path: str,
    fmt: Literal["csv", "duckdb"],
    package_version: str,
) -> str:
    """SHA-256 hex over the canonical JSON of every drip-identity input.

    Canonical JSON: UTF-8 bytes, keys sorted, compact (',', ':') separators,
    no NaN/Infinity — over the parsed config (model dump, `readme_overlay`
    and the three description-override surfaces excluded — presentation-only
    fields an author may add, change, or remove mid-drip without it counting
    as a drip-identity change), the resolved anchor (start_instant ISO + IANA
    key, or null), the base.json digest, the sole branch's fork_path, the
    fmt, and the package version. Any change to any other input yields a new
    fingerprint, halting --next rather than splicing inconsistent windows.

    Args:
        config: The parsed export config.
        anchor: Resolved anchor, or None (serialized as null).
        sidecar_sha256: Hex digest of base.json's bytes.
        fork_path: The sole branch's fork path.
        fmt: Output format.
        package_version: The installed fabulexa_forge version string.

    Returns:
        64-char lowercase hex digest.
    """
    document: dict[str, Any] = {
        "anchor": anchor_to_json(anchor),
        "config": config.model_dump(mode="json", exclude=_FINGERPRINT_EXCLUDE),
        "fmt": fmt,
        "fork_path": fork_path,
        "package_version": package_version,
        "sidecar_sha256": sidecar_sha256,
    }
    canonical = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
