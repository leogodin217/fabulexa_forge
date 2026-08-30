"""Sidecar-driven candidate `mode: source` config generator for `init`.

`generate_source_init_config` is a pure function of `(emit, code version)`
(design doc § `init --mode source` inference contract): one `state` table per
population — one per declared sub-type for a sub-typed kind (`sub_types:
[<sub_type>]`), one for a flat kind — `init`'s default split, matching
dimensional's per-sub-type stubs (both key off `Sidecar.subtype_values`,
independent of `record_roles`, which source never consults). The first
sub-type's stub carries a header comment naming the kind's full declared
domain; the last carries a commented combine-alternative (one shared table
across every sub-type, `sub_types:` omitted) for a kind whose sub-types share
an identical column set. One `junction` table per `membership__<K>__<p>`
table for a flat owner; a sub-typed owner instead proposes one junction stub
per declared sub-type (`sub_types: [<sub_type>]`), the last carrying a
commented combine-alternative — mirroring the owner's own per-sub-type state
stubs. One `events` stub named `versions` — an active source per
tracked-property kind, membership sources and lifecycle-only kinds appended
commented-out (one commented entry per declared sub-type for a sub-typed
owner's membership, carrying `sub_types: [<sub_type>]`); fully commented when
no kind carries a tracked property.
Absent `columns` / `only` / `ignore` propose the full classified default
(source's own absent-selection rule), so no column enumeration is needed
here; non-exempt `slice_only` columns are never proposed and each gets one
'slice-only-column-omitted' notice. Two auto-derived names that collide
(underscore-bearing identifiers) emit the later proposal commented, with a
collision note — the emitted config always parses and plans clean.

The `keys:` proposal shares the key-election `init` contract's cross-mode
menu (`exporters.keys_init.propose_key_election` / `render_keys_block`) with
dimensional and streaming's engines: uniform `record_index` active for every
population, with each population's resolvable alternatives (`record_id`
always, `presentation_id` where the registry declares the population)
offered as swap-not-join comments.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from fabulexa_forge.errors import SourceHistoryTrackedRequired
from fabulexa_forge.exporters.init_annotations import (
    scenario_comment_lines,
    sub_type_line_suffix,
    table_description,
)
from fabulexa_forge.exporters.keys_init import propose_key_election, render_keys_block
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.slice_only import is_non_exempt_slice_only

if TYPE_CHECKING:
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

#: The `prop__` value-column name prefix.
_PROP_PREFIX = "prop__"


@dataclass(frozen=True)
class _StateUnit:
    """One proposed `state` table: one population of one records kind.

    A flat kind proposes exactly one unit (`sub_type=None`, verbatim kind
    name). A sub-typed kind (`Sidecar.subtype_values` non-empty) proposes one
    unit per declared sub-type (`name` = `<kind>_<sub_type>`, narrowed via
    `sub_types: [<sub_type>]`) — `init`'s default split, matching
    dimensional's per-sub-type stubs; both key off `Sidecar.subtype_values`,
    independent of `record_roles`.
    """

    name: str
    kind: str
    sub_type: str | None


@dataclass(frozen=True)
class _JunctionUnit:
    """One proposed `junction` table: one per membership table for a flat
    owner (`name` = `<K>_<p>`), one per declared sub-type for a sub-typed
    owner (`name` = `<K>_<sub_type>_<p>`, `sub_type` set) — mirroring
    `_StateUnit`'s per-sub-type split, keyed off the owner kind's
    `Sidecar.subtype_values`.
    """

    name: str
    owner_kind: str
    property: str
    sub_type: str | None
    """The owner sub-type this stub addresses, or None for a flat owner /
    whole junction."""


def _proposed_units(sidecar: "Sidecar") -> "tuple[_StateUnit | _JunctionUnit, ...]":
    """One unit per population: one per sub-type of a sub-typed kind, one per
    flat kind, one per `membership__<K>__<p>` table.

    Args:
        sidecar: The open emit's sidecar.

    Returns:
        The proposed units, sidecar table-declaration order (sub-types in
        declared-domain order within a kind).
    """
    units: "list[_StateUnit | _JunctionUnit]" = []
    for table in sidecar.tables():
        if table.category == "records":
            kind = table.record_kind
            assert kind is not None, "records table must declare record_kind"
            domain = sidecar.subtype_values(kind)
            if domain:
                for sub_type in domain:
                    units.append(
                        _StateUnit(
                            name=f"{kind}_{sub_type}", kind=kind, sub_type=sub_type
                        )
                    )
            else:
                units.append(_StateUnit(name=kind, kind=kind, sub_type=None))
        elif table.category == "membership":
            owner_kind = table.record_kind
            property_name = table.property
            assert owner_kind is not None and property_name is not None, (
                "membership table must declare record_kind and property"
            )
            owner_domain = sidecar.subtype_values(owner_kind)
            if owner_domain:
                for sub_type in owner_domain:
                    units.append(
                        _JunctionUnit(
                            name=f"{owner_kind}_{sub_type}_{property_name}",
                            owner_kind=owner_kind,
                            property=property_name,
                            sub_type=sub_type,
                        )
                    )
            else:
                units.append(
                    _JunctionUnit(
                        name=f"{owner_kind}_{property_name}",
                        owner_kind=owner_kind,
                        property=property_name,
                        sub_type=None,
                    )
                )
    return tuple(units)


def _membership_sources(
    sidecar: "Sidecar",
) -> tuple[tuple[str, str, str | None], ...]:
    """Every `(owner_kind, property, sub_type)` membership triple, sidecar
    order — one triple per declared sub-type of a sub-typed owner, one
    (`sub_type=None`) for a flat owner.

    Args:
        sidecar: The open emit's sidecar.

    Returns:
        `(owner_kind, property, sub_type | None)` triples, sidecar
        table-declaration order (sub-types in declared-domain order within
        an owner).
    """
    return tuple(
        (unit.owner_kind, unit.property, unit.sub_type)
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
    sidecar: "Sidecar",
) -> None:
    """Write one proposed `state` table entry.

    A flat kind (`unit.sub_type` is None) proposes one whole-kind stub. A
    sub-typed kind proposes one stub per declared sub-type — `init`'s
    default split, narrowed via `sub_types: [<sub_type>]`, matching
    dimensional's per-sub-type stubs (both key off `Sidecar.subtype_values`,
    independent of `record_roles`). The first sub-type's stub carries a
    header comment naming the kind's full declared domain; the last carries
    a commented combine-alternative — one shared table across every declared
    sub-type (`sub_types:` omitted) — the shape a kind whose sub-types
    declare an identical column set may prefer instead (the `KEPT ...
    CONFORMED` judgment the nhs/retail example configs make for kinds like
    `diary`/`appointment_book`). A name collision comments out the whole
    entry instead, with a collision note.

    Args:
        w: Line-writing callable.
        unit: The proposed unit.
        domain: The kind's declared sub-type domain, `()` for a flat kind.
        commented: True when a same-named proposal was already emitted.
        sidecar: The open emit's sidecar.
    """
    description = table_description(sidecar, f"records__{unit.kind}")
    if commented:
        _write_collision_note(w, unit.name)
        if description is not None:
            w(f"    # {description}")
        w(f"    # - name: {unit.name}")
        w(f"    #   kind: {unit.kind}")
        if unit.sub_type is not None:
            suffix = sub_type_line_suffix(sidecar, unit.kind, unit.sub_type)
            w(f"    #   sub_types: [{unit.sub_type}]{suffix}")
        return
    if unit.sub_type is not None and unit.sub_type == domain[0]:
        w(
            f"    # kind '{unit.kind}' declares sub-types: {', '.join(domain)}"
            " (one table per sub-type below)"
        )
    if description is not None:
        w(f"    # {description}")
    w(f"    - name: {unit.name}")
    w(f"      kind: {unit.kind}")
    if unit.sub_type is not None:
        suffix = sub_type_line_suffix(sidecar, unit.kind, unit.sub_type)
        w(f"      sub_types: [{unit.sub_type}]{suffix}")
    if unit.sub_type is not None and unit.sub_type == domain[-1]:
        w(
            "    # Combine alternative: one shared table across every"
            " declared sub-type instead of the per-sub-type split above"
            " (valid when the sub-types share an identical column set)"
        )
        if description is not None:
            w(f"    # {description}")
        w(f"    # - name: {unit.kind}")
        w(f"    #   kind: {unit.kind}")


def _write_junction_unit(
    w: Callable[[str], None],
    unit: _JunctionUnit,
    domain: tuple[str, ...],
    commented: bool,
    sidecar: "Sidecar",
) -> None:
    """Write one proposed `junction` table entry; a per-sub-type stub
    carries `sub_types: [<sub_type>]`, and the last stub of a sub-typed
    owner's set carries the commented combine-alternative (one whole
    junction, `sub_types:` omitted), mirroring `_write_state_unit`.
    No `where` is ever proposed.

    Args:
        w: Line-writing callable.
        unit: The proposed unit.
        domain: The owner kind's declared discriminator domain (empty for a
            flat owner) — last-stub detection, as `_write_state_unit`'s.
        commented: True when a same-named proposal was already emitted.
        sidecar: The open emit's sidecar.
    """
    description = table_description(
        sidecar, f"membership__{unit.owner_kind}__{unit.property}"
    )
    if commented:
        _write_collision_note(w, unit.name)
        if description is not None:
            w(f"    # {description}")
        w(f"    # - name: {unit.name}")
        w(f"    #   membership: {{kind: {unit.owner_kind}, property: {unit.property}}}")
        if unit.sub_type is not None:
            suffix = sub_type_line_suffix(sidecar, unit.owner_kind, unit.sub_type)
            w(f"    #   sub_types: [{unit.sub_type}]{suffix}")
        return
    if description is not None:
        w(f"    # {description}")
    w(f"    - name: {unit.name}")
    w(f"      membership: {{kind: {unit.owner_kind}, property: {unit.property}}}")
    if unit.sub_type is not None:
        suffix = sub_type_line_suffix(sidecar, unit.owner_kind, unit.sub_type)
        w(f"      sub_types: [{unit.sub_type}]{suffix}")
    if unit.sub_type is not None and unit.sub_type == domain[-1]:
        w(
            "    # Combine alternative: one shared junction across every"
            " declared sub-type instead of the per-sub-type split above"
            " (valid when the sub-types share an identical column set)"
        )
        if description is not None:
            w(f"    # {description}")
        w(f"    # - name: {unit.owner_kind}_{unit.property}")
        w(f"    #   membership: {{kind: {unit.owner_kind}, property: {unit.property}}}")


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
            _write_state_unit(w, unit, domain, commented, sidecar)
        else:
            domain = sidecar.subtype_values(unit.owner_kind)
            _write_junction_unit(w, unit, domain, commented, sidecar)


# ---------------------------------------------------------------------------
# `source.events` proposal
# ---------------------------------------------------------------------------


def _write_events_block(
    w: Callable[[str], None], sidecar: "Sidecar", known_kinds: tuple[str, ...]
) -> None:
    """Write the `source.events` stub, named `versions`.

    An active source per tracked-property kind; membership sources and
    lifecycle-only kinds (no tracked property) appended commented-out — one
    commented entry per declared sub-type of a sub-typed owner, carrying
    `sub_types: [<sub_type>]`, one (no `sub_types`) for a flat owner. When
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
        for owner_kind, prop, sub_type in memberships:
            w(f"  #     - membership: {{kind: {owner_kind}, property: {prop}}}")
            if sub_type is not None:
                suffix = sub_type_line_suffix(sidecar, owner_kind, sub_type)
                w(f"  #       sub_types: [{sub_type}]{suffix}")
        return

    w("  events:")
    w("    name: versions")
    w("    sources:")
    for kind in tracked:
        w(f"      - kind: {kind}")
    for kind in lifecycle_only:
        w(f"      # - kind: {kind}  # lifecycle-only: no tracked property")
    for owner_kind, prop, sub_type in memberships:
        w(f"      # - membership: {{kind: {owner_kind}, property: {prop}}}")
        if sub_type is not None:
            suffix = sub_type_line_suffix(sidecar, owner_kind, sub_type)
            w(f"      #   sub_types: [{sub_type}]{suffix}")


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
    known_kinds = sidecar.record_kinds()
    proposal = propose_key_election(sidecar)

    buf = io.StringIO()

    def w(line: str = "") -> None:
        buf.write(line + "\n")

    scenario_lines = scenario_comment_lines(sidecar)
    if scenario_lines:
        for line in scenario_lines:
            w(line)
        w("")
    w("# Candidate source export config — generated by `fabulexa-forge init`")
    w("# This is a starting point. Review every table / junction / events proposal.")
    w(
        "# Table, junction, and events declarations are AUTHOR-AUTHORITATIVE —"
        " confirm, split, or rename each."
    )
    w("")
    w("mode: source")
    w("")
    for line in render_keys_block(proposal):
        w(line)
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

    Also annotates the output through the emit's documentation view
    (`Sidecar.documentation()`, shared with the other two `init` engines via
    `exporters.init_annotations`): a scenario comment block at the top when
    `scenario_description` is declared, each `state`/`junction` stub's source
    table's `tables[].description`, and each `sub_types: [<v>]` line's
    discriminator gloss — inside commented alternatives too. Comments only;
    undocumented items get no comment.

    Args:
        emit: The open emit.
        notice_sink: Receiver for proposal notices.

    Returns:
        A YAML string: a commented candidate `mode: source` config, with a
        proposed `keys:` block, one `state` table stub per population (per
        declared sub-type for a sub-typed kind, carrying a commented
        combine-alternative after the last one) / one `junction` stub per
        membership table (per declared sub-type of a sub-typed owner,
        likewise carrying a commented combine-alternative after the last
        one), and a `versions` events stub (fully commented when no kind
        carries a tracked property).

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
