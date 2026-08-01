"""Sidecar-driven candidate `mode: source` config generator for `init`.

`generate_source_init_config` is a pure function of `(emit, code version)`
(design doc § `init --mode source` inference contract): one `state` table
per `records__<kind>` table (a sub-typed kind proposes one combined STI
table, with a comment enumerating its declared sub-types and the
per-sub-type split alternative — commented, since comments are not
grammar), one `junction` table per `membership__<K>__<p>` table, and one
`events` stub named `versions` — an active source per tracked-property
kind, membership sources and lifecycle-only kinds appended commented-out;
fully commented when no kind carries a tracked property. Absent `columns` /
`only` / `ignore` propose the full classified default (source's own
absent-selection rule), so no column enumeration is needed here; non-exempt
`slice_only` columns are never proposed and each gets one
'slice-only-column-omitted' notice. Two auto-derived names that collide
(underscore-bearing identifiers) emit the later proposal commented, with a
collision note — the emitted config always parses and plans clean.

The `keys:` proposal shares the key-election `init` contract's natural rule
(`exporters.keys_init`: declared population -> presentation_id, undeclared
-> record_index) with dimensional's engine, self-gated through
`resolve_election` before a line is written. Source's own table shape (one
state table spanning a kind's *full* declared domain, unlike dimensional's
always-single-population dim stubs) means `check_identity_election` over
each sub-typed kind's full domain is the *only* gate needed: it is strictly
stronger than the edge-union-safety check (it additionally refuses a mixed
election, which the edge gate does not), and every edge into a kind targets
that same kind's full domain (source edges are kind-targeted), so a kind
that passes its own identity gate is edge-safe by the same computation.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from fabulexa_forge.errors import (
    ElectionMixedIdentity,
    ElectionUnionUnsafe,
    SourceHistoryTrackedRequired,
)
from fabulexa_forge.exporters.election import check_identity_election, resolve_election
from fabulexa_forge.exporters.keys_init import (
    build_keys_config,
    domains_for_kinds,
    natural_expanded_surfaces,
    write_keys_block,
)
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.slice_only import is_non_exempt_slice_only

if TYPE_CHECKING:
    from fabulexa_forge.config.models import KeySurface
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

#: The `prop__` value-column name prefix.
_PROP_PREFIX = "prop__"


@dataclass(frozen=True)
class _StateUnit:
    """One proposed `state` table: verbatim kind name, one per records kind."""

    name: str
    kind: str


@dataclass(frozen=True)
class _JunctionUnit:
    """One proposed `junction` table: `<K>_<p>`, one per membership table."""

    name: str
    owner_kind: str
    property: str


# ---------------------------------------------------------------------------
# Sidecar-driven kind/unit enumeration
# ---------------------------------------------------------------------------


def _known_records_kinds(sidecar: "Sidecar") -> tuple[str, ...]:
    """Every kind with a declared `records__<kind>` table, sidecar table order.

    Args:
        sidecar: The open emit's sidecar.

    Returns:
        Record kinds, in sidecar table-declaration order.
    """
    kinds: list[str] = []
    for table in sidecar.tables():
        if table.category == "records":
            kind = table.record_kind
            assert kind is not None, "records table must declare record_kind"
            kinds.append(kind)
    return tuple(kinds)


def _proposed_units(sidecar: "Sidecar") -> "tuple[_StateUnit | _JunctionUnit, ...]":
    """One unit per `records__<kind>` / `membership__<K>__<p>` table.

    Args:
        sidecar: The open emit's sidecar.

    Returns:
        The proposed units, sidecar table-declaration order.
    """
    units: "list[_StateUnit | _JunctionUnit]" = []
    for table in sidecar.tables():
        if table.category == "records":
            kind = table.record_kind
            assert kind is not None, "records table must declare record_kind"
            units.append(_StateUnit(name=kind, kind=kind))
        elif table.category == "membership":
            owner_kind = table.record_kind
            property_name = table.property
            assert owner_kind is not None and property_name is not None, (
                "membership table must declare record_kind and property"
            )
            units.append(
                _JunctionUnit(
                    name=f"{owner_kind}_{property_name}",
                    owner_kind=owner_kind,
                    property=property_name,
                )
            )
    return tuple(units)


def _membership_sources(sidecar: "Sidecar") -> tuple[tuple[str, str], ...]:
    """Every `(owner_kind, property)` membership table, sidecar order.

    Args:
        sidecar: The open emit's sidecar.

    Returns:
        (owner_kind, property) pairs, sidecar table-declaration order.
    """
    return tuple(
        (unit.owner_kind, unit.property)
        for unit in _proposed_units(sidecar)
        if isinstance(unit, _JunctionUnit)
    )


def _kind_has_tracked_property(sidecar: "Sidecar", kind: str) -> bool:
    """Whether any `prop__` column of a kind's records table is class-`tracked`.

    Args:
        sidecar: The open emit's sidecar.
        kind: The records kind.

    Returns:
        True iff some `prop__` column declares `temporal_class: tracked`.

    Raises:
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    table_name = f"records__{kind}"
    for col in sidecar.columns(table_name):
        if not col.name.startswith(_PROP_PREFIX):
            continue
        if sidecar.temporal_class(table_name, col.name) == "tracked":
            return True
    return False


# ---------------------------------------------------------------------------
# `keys:` self-gate
# ---------------------------------------------------------------------------


def _self_gate_keys_proposal(
    sidecar: "Sidecar",
    domains: "dict[str, tuple[str, ...]]",
    expanded: "dict[tuple[str, str | None], KeySurface]",
) -> "tuple[dict[str, KeySurface | dict[str, KeySurface]], dict[str, str]]":
    """Gate the natural proposal through `resolve_election` + identity uniformity.

    Every sub-typed kind proposes exactly one combined `state` table over its
    full declared domain, so `check_identity_election` over that same full
    domain is the mode's own plan-time gate — no separate edge-union-safety
    pass is needed (module docstring). A flat kind (domain length < 1) or a
    kind with exactly one addressed sub-type needs no gate (never mixed, and
    a lone population has no pair to check).

    Args:
        sidecar: The open emit's sidecar.
        domains: Every proposed kind's sub-type domain.
        expanded: The natural per-population proposal, mutated in place with
            any degradations.

    Returns:
        (keys_config, degraded) — the gated `ExportConfig.keys`-shaped
        proposal, and kind -> a one-line reason naming the forcing gate, for
        every kind the gate degraded.
    """
    election = resolve_election(sidecar, build_keys_config(expanded, domains))
    degraded: dict[str, str] = {}
    for kind, domain in domains.items():
        if len(domain) < 2:
            continue
        try:
            check_identity_election(election, kind, domain, kind)
        except (ElectionMixedIdentity, ElectionUnionUnsafe) as exc:
            degraded[kind] = f"{type(exc).__name__}: {exc}"

    if not degraded:
        return build_keys_config(expanded, domains), degraded

    for kind in degraded:
        for sub_type in domains[kind]:
            expanded[(kind, sub_type)] = "record_index"
    return build_keys_config(expanded, domains), degraded


# ---------------------------------------------------------------------------
# `source.tables` proposal
# ---------------------------------------------------------------------------


def _slice_only_notices_for_kind(
    sidecar: "Sidecar", kind: str, notice_sink: "NoticeSink"
) -> None:
    """Emit one 'slice-only-column-omitted' notice per non-exempt slice_only column.

    Args:
        sidecar: The open emit's sidecar.
        kind: The records kind.
        notice_sink: Receiver for the notices.

    Raises:
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    table_name = f"records__{kind}"
    for col in sidecar.columns(table_name):
        if not col.name.startswith(_PROP_PREFIX):
            continue
        if is_non_exempt_slice_only(sidecar, kind, col.name):
            notice_sink(
                Notice(
                    code="slice-only-column-omitted",
                    message=(
                        f"kind '{kind}': column '{col.name}' is temporal_class:"
                        " slice_only; omitted from the source init proposal"
                    ),
                )
            )


def _write_collision_note(w: Callable[[str], None], name: str) -> None:
    """Write the shared name-collision comment above a commented-out proposal.

    Args:
        w: Line-writing callable.
        name: The colliding output name.
    """
    w(
        f"    # NOTE: name '{name}' collides with an earlier proposal above;"
        " rename one before uncommenting"
    )


def _write_state_unit(
    w: Callable[[str], None],
    unit: _StateUnit,
    domain: tuple[str, ...],
    commented: bool,
) -> None:
    """Write one proposed `state` table entry.

    A sub-typed kind (`domain` non-empty) proposes one combined STI table
    over the full domain, with a comment enumerating the declared sub-types
    and the commented per-sub-type split alternative. A name collision
    comments out the whole entry instead, with a collision note.

    Args:
        w: Line-writing callable.
        unit: The proposed unit.
        domain: The kind's declared sub-type domain, `()` for a flat kind.
        commented: True when a same-named proposal was already emitted.
    """
    if commented:
        _write_collision_note(w, unit.name)
        w(f"    # - name: {unit.name}")
        w(f"    #   kind: {unit.kind}")
        return
    if domain:
        w(
            f"    # kind '{unit.kind}' declares sub-types: {', '.join(domain)}"
            " (STI: one combined table below)"
        )
    w(f"    - name: {unit.name}")
    w(f"      kind: {unit.kind}")
    if domain:
        w(
            "    # Split alternative: one table per sub-type instead of the"
            " combined table above"
        )
        for sub_type in domain:
            w(f"    # - name: {unit.name}_{sub_type}")
            w(f"    #   kind: {unit.kind}")
            w(f"    #   sub_types: [{sub_type}]")


def _write_junction_unit(
    w: Callable[[str], None], unit: _JunctionUnit, commented: bool
) -> None:
    """Write one proposed `junction` table entry.

    Args:
        w: Line-writing callable.
        unit: The proposed unit.
        commented: True when a same-named proposal was already emitted.
    """
    if commented:
        _write_collision_note(w, unit.name)
        w(f"    # - name: {unit.name}")
        w(f"    #   membership: {{kind: {unit.owner_kind}, property: {unit.property}}}")
        return
    w(f"    - name: {unit.name}")
    w(f"      membership: {{kind: {unit.owner_kind}, property: {unit.property}}}")


def _write_tables_block(
    w: Callable[[str], None], sidecar: "Sidecar", notice_sink: "NoticeSink"
) -> None:
    """Write the `source.tables` proposal — one entry per sidecar table.

    Args:
        w: Line-writing callable.
        sidecar: The open emit's sidecar.
        notice_sink: Receiver for slice-only-column-omitted notices.
    """
    units = _proposed_units(sidecar)
    if not units:
        return
    w("  tables:")
    seen: set[str] = set()
    for unit in units:
        commented = unit.name in seen
        seen.add(unit.name)
        if isinstance(unit, _StateUnit):
            _slice_only_notices_for_kind(sidecar, unit.kind, notice_sink)
            domain = sidecar.subtype_values(unit.kind)
            _write_state_unit(w, unit, domain, commented)
        else:
            _write_junction_unit(w, unit, commented)


# ---------------------------------------------------------------------------
# `source.events` proposal
# ---------------------------------------------------------------------------


def _write_events_block(
    w: Callable[[str], None], sidecar: "Sidecar", known_kinds: tuple[str, ...]
) -> None:
    """Write the `source.events` stub, named `versions`.

    An active source per tracked-property kind; membership sources and
    lifecycle-only kinds (no tracked property) appended commented-out. When
    no kind carries a tracked property, the whole block — name included — is
    commented out under a note that the emit's history is lifecycle-only.

    Args:
        w: Line-writing callable.
        sidecar: The open emit's sidecar.
        known_kinds: Every kind with a declared records table, sidecar order.

    Raises:
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    tracked = [k for k in known_kinds if _kind_has_tracked_property(sidecar, k)]
    lifecycle_only = [k for k in known_kinds if k not in tracked]
    memberships = _membership_sources(sidecar)

    if not tracked:
        w(
            "  # events:  # this emit's declared history is lifecycle-only"
            " (create/destroy"
        )
        w(
            "  #          # spine events only) -- no kind carries a tracked"
            " property; uncomment to opt in"
        )
        w("  #   name: versions")
        w("  #   sources:")
        for kind in lifecycle_only:
            w(f"  #     - kind: {kind}")
        for owner_kind, prop in memberships:
            w(f"  #     - membership: {{kind: {owner_kind}, property: {prop}}}")
        return

    w("  events:")
    w("    name: versions")
    w("    sources:")
    for kind in tracked:
        w(f"      - kind: {kind}")
    for kind in lifecycle_only:
        w(f"      # - kind: {kind}  # lifecycle-only: no tracked property")
    for owner_kind, prop in memberships:
        w(f"      # - membership: {{kind: {owner_kind}, property: {prop}}}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_candidate_yaml(emit: "Emit", notice_sink: "NoticeSink") -> str:
    """Build a commented candidate `mode: source` YAML config from the sidecar.

    Args:
        emit: The open emit. Its sidecar must carry per-column
            `history_tracked` flags (checked by `generate_source_init_config`
            before this is called).
        notice_sink: Receiver for slice-only-column-omitted notices.

    Returns:
        A YAML string with candidate config and inline comments.
    """
    sidecar = emit.sidecar
    known_kinds = _known_records_kinds(sidecar)
    domains = domains_for_kinds(sidecar, known_kinds)
    presentation_keys = sidecar.presentation_keys()
    expanded = natural_expanded_surfaces(presentation_keys, domains)
    keys_config, degraded = _self_gate_keys_proposal(sidecar, domains, expanded)

    buf = io.StringIO()

    def w(line: str = "") -> None:
        buf.write(line + "\n")

    w("# Candidate source export config — generated by `fabulexa-forge init`")
    w("# This is a starting point. Review every table / junction / events proposal.")
    w(
        "# Table, junction, and events declarations are AUTHOR-AUTHORITATIVE —"
        " confirm, split, or rename each."
    )
    w("")
    w("mode: source")
    w("")
    write_keys_block(w, keys_config, degraded)
    w("source:")
    _write_tables_block(w, sidecar, notice_sink)
    _write_events_block(w, sidecar, known_kinds)

    return buf.getvalue()


def generate_source_init_config(emit: "Emit", notice_sink: "NoticeSink") -> str:
    """Generate a commented candidate `mode: source` export config from an emit.

    A pure function of `(emit, code version)`: consumes kinds, discriminator
    domains, membership tables, per-column temporal classes, and the
    `presentation_keys` registry — never `record_roles`. Proposal order
    follows the sidecar's table declaration order; two auto-derived names
    that collide emit the later proposal commented, with a collision note —
    the emitted config always parses and plans clean (design doc § `init
    --mode source` inference contract).

    Args:
        emit: The open emit.
        notice_sink: Receiver for proposal notices.

    Returns:
        A YAML string: a commented candidate `mode: source` config, with a
        proposed `keys:` block, one `state` / `junction` table stub per
        sidecar table (a sub-typed kind's combined STI stub carries the
        per-sub-type split alternative in comments), and a `versions` events
        stub (fully commented when no kind carries a tracked property).

    Raises:
        SourceHistoryTrackedRequired: The sidecar predates per-column
            `history_tracked` flags — a candidate config that cannot export
            is not proposed.
        PresentationKeysInvalidError: The sidecar's `presentation_keys` block
            is present and incoherent.
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    if not emit.sidecar.history_tracked_available():
        raise SourceHistoryTrackedRequired(
            "source export requires per-column history_tracked flags; this"
            " emit predates them"
        )
    return _build_candidate_yaml(emit, notice_sink)
