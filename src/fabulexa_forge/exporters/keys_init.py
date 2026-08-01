"""Shared `keys:` proposal primitives for the cross-mode `init` engines.

The key-election `init` contract (docs/architecture/key-election.md §
`init` proposals) is one natural rule — declared population -> presentation_id,
undeclared -> record_index — shared verbatim by every mode's proposal engine.
What differs per mode is the *self-gate*: which of `resolve_election`'s
plan-time gates the mode's own table shapes exercise (dimensional's dims are
always single-population, so only the edge gate applies; source's combined
state tables are not, so the identity gate applies too). This module holds
the mode-neutral pieces — natural-surface computation, the
`ExportConfig.keys`-shaped assembly, and the block writer — so each mode's
own `init` module composes them around its own self-gate rather than
reimplementing the shared half.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:
    from fabulexa_forge.config.models import KeySurface
    from fabulexa_forge.reader.sidecar import PresentationKeys, Sidecar


def domains_for_kinds(
    sidecar: "Sidecar", kinds: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """Every named kind's declared sub-type domain, `()` for a flat kind.

    Args:
        sidecar: The open emit's sidecar.
        kinds: The kinds the caller's proposal covers.

    Returns:
        kind -> `sidecar.subtype_values(kind)`, over `kinds`.
    """
    return {kind: sidecar.subtype_values(kind) for kind in kinds}


def population_declared(
    presentation_keys: "PresentationKeys | None", kind: str, sub_type: str | None
) -> bool:
    """Whether one population carries a `presentation_keys` declaration.

    A flat kind's `key` entry, or a partitioned kind's per-sub-type entry
    (`key_for`) — presence alone, independent of the kind's rollup claim
    (a rollup with no claim still leaves each individually-declared
    sub-type's own entry present).

    Args:
        presentation_keys: The open emit's `presentation_keys` view, or None.
        kind: The population's kind.
        sub_type: The population's discriminator value, or None for a flat kind.

    Returns:
        True iff the population has its own registry entry.
    """
    if presentation_keys is None or kind not in presentation_keys.kinds():
        return False
    try:
        if sub_type is None:
            presentation_keys.key(kind)
        else:
            presentation_keys.key_for(kind, sub_type)
    except (KeyError, ValueError):
        return False
    return True


def natural_expanded_surfaces(
    presentation_keys: "PresentationKeys | None",
    domains: "dict[str, tuple[str, ...]]",
) -> "dict[tuple[str, str | None], KeySurface]":
    """The doc's natural per-population proposal: declared -> presentation_id,
    undeclared -> record_index — total over every population `domains` covers.

    Args:
        presentation_keys: The open emit's `presentation_keys` view, or None.
        domains: Every proposed kind's sub-type domain, from `domains_for_kinds`.

    Returns:
        (kind, sub_type) -> the natural election, one entry per population.
    """
    expanded: "dict[tuple[str, str | None], KeySurface]" = {}
    for kind, domain in domains.items():
        sub_types: tuple[str | None, ...] = domain if domain else (None,)
        for sub_type in sub_types:
            expanded[(kind, sub_type)] = (
                "presentation_id"
                if population_declared(presentation_keys, kind, sub_type)
                else "record_index"
            )
    return expanded


def build_keys_config(
    expanded: "dict[tuple[str, str | None], KeySurface]",
    domains: "dict[str, tuple[str, ...]]",
) -> "dict[str, KeySurface | dict[str, KeySurface]]":
    """The config `keys` block shape from an expanded per-population map.

    Mirrors the registry's own shape (doc § `init` proposals): a flat kind
    proposes the scalar; a partitioned kind the per-sub-type map, collapsed
    to the scalar when every sub-type agrees.

    Args:
        expanded: (kind, sub_type) -> elected surface, total over `domains`.
        domains: Every proposed kind's sub-type domain.

    Returns:
        The `ExportConfig.keys`-shaped proposal.
    """
    config: "dict[str, KeySurface | dict[str, KeySurface]]" = {}
    for kind, domain in domains.items():
        if not domain:
            config[kind] = expanded[(kind, None)]
            continue
        sub_map: "dict[str, KeySurface]" = {
            sub_type: expanded[(kind, sub_type)] for sub_type in domain
        }
        values = set(sub_map.values())
        config[kind] = next(iter(values)) if len(values) == 1 else sub_map
    return config


def write_keys_block(
    w: Callable[[str], None],
    keys_config: "dict[str, KeySurface | dict[str, KeySurface]]",
    degraded: dict[str, str],
) -> None:
    """Write the proposed `keys:` block, one line per kind (or per sub-type).

    A degraded kind always renders as a scalar `record_index` (uniform
    election collapses by construction) with a trailing comment naming the
    forcing gate.

    Args:
        w: Line-writing callable.
        keys_config: The gated `ExportConfig.keys`-shaped proposal.
        degraded: kind -> reason, for every kind the self-gate forced.
    """
    w("keys:")
    for kind, election in keys_config.items():
        if isinstance(election, dict):
            w(f"  {kind}:")
            for sub_type, surface in election.items():
                w(f"    {sub_type}: {surface}")
        else:
            reason = f"  # NOTE: {degraded[kind]}" if kind in degraded else ""
            w(f"  {kind}: {election}{reason}")
    w("")
