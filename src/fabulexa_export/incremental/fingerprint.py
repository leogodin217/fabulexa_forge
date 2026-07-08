"""Fingerprint computation for incremental drip identity.

A SHA-256 digest over a canonical JSON document that uniquely identifies a
drip session: config, anchor, emit, branch, output format, and code version.
Any change to any input yields a different fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from fabulexa_export.anchor import EffectiveAnchor
    from fabulexa_export.config.models import ExportConfig


def _anchor_to_json(anchor: "EffectiveAnchor | None") -> Any:
    """Serialize the anchor to a canonical JSON-compatible value.

    Args:
        anchor: Resolved anchor, or None.

    Returns:
        A dict with "start_instant" (ISO string) and "timezone" (IANA key),
        or None when the anchor is absent.
    """
    if anchor is None:
        return None
    return {
        "start_instant": anchor.start_instant.isoformat(),
        "timezone": str(anchor.timezone),
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
    no NaN/Infinity — over the parsed config (model dump), the resolved
    anchor (start_instant ISO + IANA key, or null), the base.json digest,
    the sole branch's fork_path, the fmt, and the package version. Any
    change to any input yields a new fingerprint, halting --next rather
    than splicing inconsistent windows.

    Args:
        config: The parsed export config.
        anchor: Resolved anchor, or None (serialized as null).
        sidecar_sha256: Hex digest of base.json's bytes.
        fork_path: The sole branch's fork path.
        fmt: Output format.
        package_version: The installed fabulexa_export version string.

    Returns:
        64-char lowercase hex digest.
    """
    document: dict[str, Any] = {
        "anchor": _anchor_to_json(anchor),
        "config": config.model_dump(mode="json"),
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
