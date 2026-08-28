"""Shared `keys:` election-menu primitives for the cross-mode `init` engines.

The key-election `init` contract (docs/architecture/key-election.md §
`init` proposals) is one natural rule shared verbatim by every mode's
proposal engine: the active election is uniformly `record_index` for every
population of every kind — always present, one shared space per kind, so no
mode-specific gate can ever reject it. Alongside the active line, `init`
offers each population's resolvable alternatives as comments: `record_id`
always, `presentation_id` only where the presentation-key registry declares
that population. Because the active election never varies by population,
`propose_key_election` needs nothing from a specific mode — one proposal,
consulted only against the sidecar, served identically to dimensional,
source, and streaming's `init` engines through `render_keys_block`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Mapping, Sequence

    from fabulexa_forge.config.models import KeySurface
    from fabulexa_forge.reader.sidecar import PresentationKeys, Sidecar


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


@dataclass(frozen=True)
class KeyElectionProposal:
    """An `init` keys proposal: the active election plus its alternatives."""

    active: "Mapping[str, KeySurface | Mapping[str, KeySurface]]"
    """Per kind, the active election — a scalar for a flat kind, or for a
    partitioned kind no population of which carries a presentation_id
    alternative; a per-sub-type map otherwise. Uniformly record_index."""

    alternatives: "Mapping[str, Sequence[KeySurface]]"
    """Population address -> the surfaces offered as commented alternatives,
    in surface order. Address: the kind, or '<kind>.<sub_type>'."""


def propose_key_election(sidecar: "Sidecar") -> KeyElectionProposal:
    """The cross-mode keys proposal: uniform record_index plus per-population
    alternatives.

    Alternatives by resolvability alone: record_id always; presentation_id
    only where the presentation-key registry declares the population.
    Consults the strict registry accessor and shares its refusal behavior.

    Args:
        sidecar: The open emit's sidecar.

    Returns:
        The proposal, one active entry and one alternatives entry per known
        kind (per population for the alternatives).

    Raises:
        PresentationKeysInvalidError: The emit carries an incoherent
            presentation-key block (propagated from `sidecar.presentation_keys()`).
    """
    presentation_keys = sidecar.presentation_keys()
    active: "dict[str, KeySurface | dict[str, KeySurface]]" = {}
    alternatives: "dict[str, list[KeySurface]]" = {}
    for kind in sidecar.record_kinds():
        domain = sidecar.subtype_values(kind)
        sub_types: tuple[str | None, ...] = domain if domain else (None,)
        declared = [
            population_declared(presentation_keys, kind, sub_type)
            for sub_type in sub_types
        ]
        collapse = not domain or not any(declared)
        for sub_type, is_declared in zip(sub_types, declared):
            address = kind if collapse else f"{kind}.{sub_type}"
            surfaces: "list[KeySurface]" = ["record_id"]
            if is_declared:
                surfaces.append("presentation_id")
            alternatives[address] = surfaces
        if collapse:
            active[kind] = "record_index"
        else:
            active[kind] = {sub_type: "record_index" for sub_type in domain}
    return KeyElectionProposal(active=active, alternatives=alternatives)


def _render_population(
    key: str,
    active_surface: "KeySurface",
    alternatives: Sequence["KeySurface"],
    indent: str,
) -> list[str]:
    """Render one population's alternative comments, then its active line.

    Args:
        key: The YAML mapping key this population's active line uses — the
            kind name at top level, the sub-type value inside a per-sub-type map.
        active_surface: The active election (always `record_index`).
        alternatives: The population's offered alternatives, surface order.
        indent: The line-leading whitespace for this population's nesting depth.

    Returns:
        Comment lines (swap-not-join header, then one commented `key:
        surface` line per alternative) followed by the uncommented active line.
    """
    lines = [
        f"{indent}# NOTE: an uncommented alternative below SWAPS the active"
        " line for this population -- delete the active line, don't just"
        " uncomment"
    ]
    lines.extend(f"{indent}# {key}: {surface}" for surface in alternatives)
    lines.append(f"{indent}{key}: {active_surface}")
    return lines


def render_keys_block(proposal: KeyElectionProposal) -> list[str]:
    """Render the keys block: active lines and commented alternatives.

    The single renderer, spliced verbatim by the dimensional, source, and
    streaming init engines. Each population's alternatives precede its active
    line as comments, headed by one line stating an alternative replaces the
    active line rather than joining it.

    Args:
        proposal: The proposal to render.

    Returns:
        YAML lines, `keys:` first, ready to splice into a candidate config.
    """
    lines: list[str] = ["keys:"]
    for kind, election in proposal.active.items():
        if isinstance(election, str):
            lines.extend(
                _render_population(kind, election, proposal.alternatives[kind], "  ")
            )
            continue
        lines.append(f"  {kind}:")
        for sub_type, surface in election.items():
            lines.extend(
                _render_population(
                    sub_type,
                    surface,
                    proposal.alternatives[f"{kind}.{sub_type}"],
                    "    ",
                )
            )
    lines.append("")
    return lines
