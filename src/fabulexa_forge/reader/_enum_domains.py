"""Shared enum_domains value-object parse floor.

`sidecar.py`'s typed values-only `enum_domains()` routing surface and
`documentation.py`'s glossed `Documentation.enum_options()` view answer the
same declared value set — a value collision the design forbids by
construction. This module holds the one parse pass both derive from, so
they can never drift: a malformed value object drops whole from both views;
a mis-shaped gloss parses as gloss-absent, never dropping its value.

A standalone module (not sidecar.py or documentation.py) because both of
those import each other at runtime (`sidecar.py` constructs `Documentation`;
`documentation.py` reads sidecar internals) and a shared dependency of
neither avoids the cycle.
"""

from __future__ import annotations

from typing import Mapping


def _parse_enum_value_object(raw: object) -> tuple[str, str | None] | None:
    """Parse one enum_domains value object into (value, gloss); None if malformed.

    An entry survives iff it is an object carrying a string 'value'; the
    optional 'description' gloss parses as its own value when a string, else
    gloss-absent — a mis-shaped gloss never drops the value.

    Args:
        raw: One raw enum_domains option entry.

    Returns:
        (value, gloss), or None when the entry lacks a string 'value'.
    """
    if not isinstance(raw, dict):
        return None
    value = raw.get("value")
    if not isinstance(value, str):
        return None
    gloss_raw = raw.get("description")
    gloss: str | None = gloss_raw if isinstance(gloss_raw, str) else None
    return value, gloss


def parse_enum_domains_glossed(
    raw: object,
) -> Mapping[str, Mapping[str, tuple[tuple[str, str | None], ...]]]:
    """Parse enum_domains into (value, gloss) pairs — the shared parse floor.

    Args:
        raw: The raw enum_domains value from the sidecar.

    Returns:
        A nested {kind: {property: ((value, gloss), ...)}} mapping, empty
        when absent.
    """
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, tuple[tuple[str, str | None], ...]]] = {}
    for kind, props in raw.items():
        if isinstance(props, dict):
            inner: dict[str, tuple[tuple[str, str | None], ...]] = {}
            for prop, options in props.items():
                if isinstance(options, list):
                    inner[prop] = tuple(
                        parsed
                        for entry in options
                        if (parsed := _parse_enum_value_object(entry)) is not None
                    )
            result[kind] = inner
    return result
