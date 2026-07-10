"""Vendored v4 base-format JSON Schema loader.

Resolves the single canonical file at contract/base-format.schema.json.  The
file is bundled as package data via a hatch force-include so it resolves
identically after a wheel install.  In an editable (in-tree) install
importlib.resources resolves to the src/ tree where no contract/ subdirectory
exists, so we fall back to a __file__-relative path that reaches the canonical
file directly.

The parsed schema is cached and treated as immutable.
"""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path
from typing import Mapping

_cached_schema: Mapping[str, object] | None = None


def _load_vendored_schema() -> Mapping[str, object]:
    """Load the vendored v4 base-format JSON Schema bundled with this package.

    Resolves contract/base-format.schema.json from package data (post-install)
    or from the vendored contract/ directory (in-tree editable install), so it
    resolves identically either way.  The parsed schema is cached and treated as
    immutable: C1 must never mutate the returned object (it makes a shallow
    copy when relaxing the top-level additionalProperties for the unknown-
    top-level-field carve-out).

    Returns:
        The parsed JSON Schema mapping for base_format_version 4.

    Raises:
        FileNotFoundError: the vendored schema is missing — an
            installation/packaging defect, surfaced loudly.  This is not an
            emit problem and is never reported as a conformance failure.
        json.JSONDecodeError: the vendored schema is unparseable — an
            installation/packaging defect, surfaced loudly.
    """
    global _cached_schema
    if _cached_schema is not None:
        return _cached_schema

    schema_text = _read_schema_text()
    result: Mapping[str, object] = json.loads(schema_text)
    _cached_schema = result
    return result


def _read_schema_text() -> str:
    """Return the raw text of the vendored schema, trying two resolution paths.

    1. importlib.resources — correct for a wheel / non-editable install where
       force-include places the file inside the fabulexa_forge package.
    2. __file__-relative path — correct for an editable (in-tree) install
       where importlib.resources resolves to src/fabulexa_forge/ and there
       is no contract/ subdirectory there; the canonical file lives three
       directories up, at the repo root's contract/.
    """
    # Path 1: post-install wheel layout (force-include puts it here).
    pkg_ref = importlib.resources.files("fabulexa_forge")
    schema_ref = pkg_ref / "contract" / "base-format.schema.json"
    try:
        return schema_ref.read_text(encoding="utf-8")
    except (FileNotFoundError, TypeError):
        pass

    # Path 2: editable in-tree layout — resolve from this file's location.
    # _schema.py lives at src/fabulexa_forge/reader/_schema.py
    # canonical schema is at   <repo root>/contract/base-format.schema.json
    # relative path:           ../../../contract/base-format.schema.json
    fallback = (
        Path(__file__).parent.parent.parent.parent
        / "contract"
        / "base-format.schema.json"
    )
    if not fallback.exists():
        raise FileNotFoundError(
            "Vendored schema not found via importlib.resources or fallback path "
            f"{fallback}. This is a packaging defect."
        )
    return fallback.read_text(encoding="utf-8")
