"""Deterministic config fingerprinting for corrupter runs.

See `docs/architecture/pending/corrupter-engine-and-manifest.md` § Config
canonicalization and the fingerprint (normative) for the full rationale.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_export.config.models import CorruptConfig


def fingerprint_config(config: "CorruptConfig") -> str:
    """Compute the deterministic SHA-256 fingerprint of a validated corrupter config.

    The sole implementation of the § Config canonicalization and the
    fingerprint contract: serialize the validated model to canonical JSON --
    model_dump(mode="json") with all fields, then
    json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True) --
    and return the SHA-256 hex digest of its UTF-8 bytes. A pure function of
    the config's meaning, not its YAML text: reformatting the YAML leaves the
    fingerprint unchanged, while any change to seed, operation order, a
    column-list order, or any operation field changes it.

    Args:
        config: The validated corrupter config.

    Returns:
        The 64-character lowercase hex SHA-256 digest, used as
        DefectManifest.config_fingerprint.
    """
    dumped = config.model_dump(mode="json")
    canonical = json.dumps(
        dumped, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
