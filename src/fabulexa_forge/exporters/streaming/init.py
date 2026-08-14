"""Sidecar-driven candidate `StreamConfig` generator for `init --mode streaming`.

`generate_stream_init_config` is a pure function of `(emit, code version)`
(design doc § `init --mode streaming` inference contract): one live
kind-shaped stream per population — one per declared sub-type for a
sub-typed kind (`name: <sub_type>` verbatim, `properties` from the
`sub_type_columns` partition's payload-role entries, union-set fallback with
a comment when the sidecar omits the partition), one per flat kind
(`name: <kind>`) — a lifecycle-only population's stream proposed live under
an advisory comment (deleting it opts out). Names are sidecar identity
verbatim; `init` infers no intent and never sanitizes. Every
`membership__<K>__<p>` table proposes one membership-shaped stream
(`name: <K>_<p>`) inside a fully-commented `content: membership-events`
alternative block.

One name-collision namespace spans both content blocks (a kind-shaped and a
membership-shaped proposal can collide, and the membership auto-name
`<K>_<p>` is itself underscore-ambiguous): the later sidecar-order proposal
is emitted commented out, with a comment naming the collision — the emitted
config always parses and streams clean. A sub-type value that fails the
topic-name rule (the only name family that can — kind and membership
`<K>_<p>` names are table-name segments, identifier-safe by construction) is
likewise emitted commented out, naming the rule and the offending value.
`StreamInitNothingToStream` refuses outright when the emit carries no
records kind, or when no proposal survives live at all (every sidecar-
derived name topic-illegal).

The `keys:` proposal shares the key-election `init` contract's natural rule
(`exporters.keys_init`: declared population -> presentation_id, undeclared ->
record_index) and is self-gated through streaming's own gates before a line
is written: edge union safety over every live kind-shaped stream's selected
reference-valued properties, and over every (not excluded-by-collision)
membership-shaped stream's reference-valued fields against every known
kind (a membership member field's target kind is per-row, never fixed —
design doc § Message-key election). Every proposed stream draws from exactly
one population, so the identity-uniformity gate never fires for a proposal
`init` builds itself; a kind implicated in an edge-safety failure degrades to
uniform `record_index`, with a comment naming the forcing gate.

Non-exempt `slice_only` columns are never proposed; one
'slice-only-column-omitted' notice each.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal, Sequence

from fabulexa_forge.config.models import _validate_stream_name
from fabulexa_forge.errors import ElectionUnionUnsafe, StreamInitNothingToStream
from fabulexa_forge.exporters.election import check_edge_union_safety, resolve_election
from fabulexa_forge.exporters.keys_init import (
    build_keys_config,
    domains_for_kinds,
    natural_expanded_surfaces,
    write_keys_block,
)
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.slice_only import is_non_exempt_slice_only
from fabulexa_forge.exporters.streaming.routing import (
    kind_reference_targets,
    known_records_kinds,
    membership_reference_fields,
)

if TYPE_CHECKING:
    from fabulexa_forge.config.models import KeySurface
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

#: The `prop__` value-column name prefix.
_PROP_PREFIX = "prop__"

_UnitStatus = Literal["live", "collision", "topic_illegal"]


@dataclass(frozen=True)
class _KindStreamUnit:
    """One proposed kind-shaped stream: one population of one records kind.

    A flat kind proposes exactly one unit (`sub_type=None`, verbatim kind
    name, `domain=()`). A sub-typed kind proposes one unit per declared
    sub-type (`name` = the sub-type value verbatim), `domain` carrying the
    kind's full declared sub-type domain for the header comment on the
    first one.
    """

    name: str
    kind: str
    sub_type: str | None
    properties: tuple[str, ...]
    domain: tuple[str, ...]
    union_fallback: bool
    lifecycle_only: bool


@dataclass(frozen=True)
class _MembershipStreamUnit:
    """One proposed membership-shaped stream: `<K>_<p>`, one per membership table."""

    name: str
    owner_kind: str
    property: str
    fields: tuple[str, ...]


# ---------------------------------------------------------------------------
# Sidecar-driven unit enumeration
# ---------------------------------------------------------------------------


def _sub_type_properties(
    sidecar: "Sidecar", kind: str, sub_type: str
) -> tuple[tuple[str, ...], bool]:
    """One sub-type's payload-role `prop__` names, and the union-fallback flag.

    Reads the sidecar's `sub_type_columns` partition for `(kind, sub_type)`
    when present; falls back to the kind's full payload-role column set
    (minus the discriminator, constant within a single-sub-type stream) when
    the sidecar omits the partition, or omits this kind/sub-type from it —
    the init engines' union-fallback convention (dimensional's posture).

    Args:
        sidecar: The open emit's sidecar.
        kind: The sub-typed records kind.
        sub_type: The declared sub-type.

    Returns:
        (bare property names in sidecar column order, union_fallback).
    """
    all_cols = sidecar.columns(f"records__{kind}")
    discriminator = f"{_PROP_PREFIX}{kind}_type"
    partition = sidecar.sub_type_columns()
    owned: frozenset[str] | None = None
    fallback = True
    if partition is not None:
        try:
            owned = frozenset(partition.columns_for(kind, sub_type))
            fallback = False
        except KeyError:
            owned = None

    names: list[str] = []
    for col in all_cols:
        if not col.name.startswith(_PROP_PREFIX) or col.name == discriminator:
            continue
        if owned is not None and col.name not in owned:
            continue
        names.append(col.name[len(_PROP_PREFIX) :])
    return tuple(names), fallback


def _flat_kind_properties(sidecar: "Sidecar", kind: str) -> tuple[str, ...]:
    """A flat kind's payload-role `prop__` names, sidecar column order.

    Args:
        sidecar: The open emit's sidecar.
        kind: The flat records kind.

    Returns:
        Bare property names.
    """
    cols = sidecar.columns(f"records__{kind}")
    return tuple(
        col.name[len(_PROP_PREFIX) :]
        for col in cols
        if col.name.startswith(_PROP_PREFIX)
    )


def _population_has_tracked_property(
    sidecar: "Sidecar", kind: str, properties: Sequence[str]
) -> bool:
    """Whether any of a population's selected properties is history-tracked.

    Args:
        sidecar: The open emit's sidecar.
        kind: The records kind.
        properties: The population's selected bare property names.

    Returns:
        True iff some selected `prop__<p>` column declares `history_tracked: true`.
    """
    cols_by_name = {c.name: c for c in sidecar.columns(f"records__{kind}")}
    return any(
        cols_by_name[f"{_PROP_PREFIX}{prop}"].history_tracked is True
        for prop in properties
    )


def _filter_non_exempt_slice_only(
    sidecar: "Sidecar",
    kind: str,
    sub_type: str | None,
    properties: Sequence[str],
    notice_sink: "NoticeSink",
) -> tuple[str, ...]:
    """Drop non-exempt `slice_only` properties, one notice per drop.

    Args:
        sidecar: The open emit's sidecar.
        kind: The records kind.
        sub_type: The population's discriminator value, or None for a flat kind.
        properties: The candidate bare property names.
        notice_sink: Receiver for 'slice-only-column-omitted' notices.

    Returns:
        `properties`, minus every non-exempt `slice_only` entry.

    Raises:
        TemporalClassUnavailableError: Propagated from the reader.
    """
    label = (
        f"kind '{kind}'" if sub_type is None else f"kind '{kind}' sub-type '{sub_type}'"
    )
    kept: list[str] = []
    for prop in properties:
        col_name = f"{_PROP_PREFIX}{prop}"
        if is_non_exempt_slice_only(sidecar, kind, col_name):
            notice_sink(
                Notice(
                    code="slice-only-column-omitted",
                    message=(
                        f"{label}: column '{col_name}' is temporal_class:"
                        " slice_only; omitted from the streaming init proposal"
                    ),
                )
            )
            continue
        kept.append(prop)
    return tuple(kept)


def _membership_fields(
    sidecar: "Sidecar", owner_kind: str, property_name: str
) -> tuple[str, ...]:
    """Every element-schema field of one membership table, bare names, sidecar order.

    A non-reference field contributes its `elem__<f>` name; a reference field
    contributes one bare name from its `member__<f>__kind` half (its
    `member__<f>__id` sibling names the same field).

    Args:
        sidecar: The open emit's sidecar.
        owner_kind: The membership table's owning kind.
        property_name: The membership property name.

    Returns:
        Bare element-schema field names.
    """
    table_name = f"membership__{owner_kind}__{property_name}"
    fields: list[str] = []
    for col in sidecar.columns(table_name):
        if col.name.startswith("elem__"):
            fields.append(col.name[len("elem__") :])
        elif col.name.startswith("member__") and col.name.endswith("__kind"):
            fields.append(col.name[len("member__") : -len("__kind")])
    return tuple(fields)


def _proposed_units(
    sidecar: "Sidecar", notice_sink: "NoticeSink"
) -> "tuple[_KindStreamUnit | _MembershipStreamUnit, ...]":
    """Every proposed unit — kind-shaped and membership-shaped alike — sidecar order.

    One combined pass over `sidecar.tables()`, so a later collision judgment
    (design doc: "spans both content blocks") sees the true interleaved
    declaration order.

    Args:
        sidecar: The open emit's sidecar.
        notice_sink: Receiver for slice-only-column-omitted notices.

    Returns:
        Every proposed unit, sidecar table-declaration order (sub-types in
        declared-domain order within a kind).
    """
    units: "list[_KindStreamUnit | _MembershipStreamUnit]" = []
    for table in sidecar.tables():
        if table.category == "records":
            kind = table.record_kind
            assert kind is not None, "records table must declare record_kind"
            domain = sidecar.subtype_values(kind)
            if domain:
                for sub_type in domain:
                    props, fallback = _sub_type_properties(sidecar, kind, sub_type)
                    props = _filter_non_exempt_slice_only(
                        sidecar, kind, sub_type, props, notice_sink
                    )
                    units.append(
                        _KindStreamUnit(
                            name=sub_type,
                            kind=kind,
                            sub_type=sub_type,
                            properties=props,
                            domain=domain,
                            union_fallback=fallback,
                            lifecycle_only=not _population_has_tracked_property(
                                sidecar, kind, props
                            ),
                        )
                    )
            else:
                props = _flat_kind_properties(sidecar, kind)
                props = _filter_non_exempt_slice_only(
                    sidecar, kind, None, props, notice_sink
                )
                units.append(
                    _KindStreamUnit(
                        name=kind,
                        kind=kind,
                        sub_type=None,
                        properties=props,
                        domain=(),
                        union_fallback=False,
                        lifecycle_only=not _population_has_tracked_property(
                            sidecar, kind, props
                        ),
                    )
                )
        elif table.category == "membership":
            owner_kind = table.record_kind
            property_name = table.property
            assert owner_kind is not None and property_name is not None, (
                "membership table must declare record_kind and property"
            )
            units.append(
                _MembershipStreamUnit(
                    name=f"{owner_kind}_{property_name}",
                    owner_kind=owner_kind,
                    property=property_name,
                    fields=_membership_fields(sidecar, owner_kind, property_name),
                )
            )
    return tuple(units)


def _classify_units(
    units: "Sequence[_KindStreamUnit | _MembershipStreamUnit]",
) -> list[_UnitStatus]:
    """Classify each proposed unit live / collision-loser / topic-illegal, in order.

    One global name-collision namespace spans both content blocks — the
    design doc's collision rule. A topic-illegal name (only a sub-typed
    kind's sub-type value can be) is refused outright and never occupies the
    namespace, so a later, legally-named duplicate is judged only against
    live predecessors.

    Args:
        units: Every proposed unit, sidecar declaration order.

    Returns:
        One status per unit, same order.
    """
    statuses: list[_UnitStatus] = []
    seen: set[str] = set()
    for unit in units:
        if isinstance(unit, _KindStreamUnit) and unit.sub_type is not None:
            try:
                _validate_stream_name(unit.name)
            except ValueError:
                statuses.append("topic_illegal")
                continue
        if unit.name in seen:
            statuses.append("collision")
            continue
        seen.add(unit.name)
        statuses.append("live")
    return statuses


# ---------------------------------------------------------------------------
# Key-election self-gate
# ---------------------------------------------------------------------------


def _self_gate_streaming_keys(
    sidecar: "Sidecar",
    domains: "dict[str, tuple[str, ...]]",
    expanded: "dict[tuple[str, str | None], KeySurface]",
    kind_units: "Sequence[tuple[_KindStreamUnit, _UnitStatus]]",
    membership_units: "Sequence[tuple[_MembershipStreamUnit, _UnitStatus]]",
) -> "tuple[dict[str, KeySurface | dict[str, KeySurface]], dict[str, str]]":
    """Gate the natural proposal through streaming's own edge-safety gate.

    Every proposed stream draws from exactly one population, so the
    identity-uniformity gate (`check_identity_election`) can never fire for a
    proposal `init` builds itself; the one gate needed is edge union safety
    (`check_edge_union_safety`), run over every live kind-shaped stream's
    selected reference-valued properties and every non-excluded
    membership-shaped stream's reference-valued fields — against every known
    kind, since a membership member field's target kind is per-row, never
    fixed. A kind implicated in a failure degrades to uniform `record_index`
    — always passing, by construction; one pass suffices (mirrors
    `exporters.keys_init.self_gate_edge_safety`'s argument).

    Args:
        sidecar: The open emit's sidecar.
        domains: Every known kind's sub-type domain.
        expanded: The natural per-population proposal, mutated in place with
            any degradations.
        kind_units: Every proposed kind-shaped unit with its classification.
        membership_units: Every proposed membership-shaped unit with its
            classification.

    Returns:
        (keys_config, degraded) — the gated `StreamConfig.keys`-shaped
        proposal, and kind -> a one-line reason naming the forcing gate.
    """
    known_kinds = frozenset(domains)
    election = resolve_election(sidecar, build_keys_config(expanded, domains))
    degraded: dict[str, str] = {}

    for kind_unit, kind_status in kind_units:
        if kind_status != "live":
            continue
        targets = kind_reference_targets(
            sidecar, kind_unit.kind, kind_unit.properties, known_kinds
        )
        for prop, target_kind in targets.items():
            if target_kind in degraded:
                continue
            try:
                check_edge_union_safety(
                    election,
                    target_kind,
                    domains[target_kind],
                    f"stream '{kind_unit.name}'.prop__{prop}",
                )
            except ElectionUnionUnsafe as exc:
                degraded[target_kind] = f"ElectionUnionUnsafe: {exc}"

    for membership_unit, membership_status in membership_units:
        if membership_status != "live":
            continue
        ref_fields = membership_reference_fields(
            sidecar,
            membership_unit.owner_kind,
            membership_unit.property,
            membership_unit.fields,
        )
        for field in ref_fields:
            for kind in known_kinds:
                if kind in degraded:
                    continue
                try:
                    check_edge_union_safety(
                        election,
                        kind,
                        domains[kind],
                        f"stream '{membership_unit.name}'.member__{field}"
                        f" (member kind '{kind}')",
                    )
                except ElectionUnionUnsafe as exc:
                    degraded[kind] = f"ElectionUnionUnsafe: {exc}"

    if not degraded:
        return build_keys_config(expanded, domains), degraded

    for kind in degraded:
        sub_types: tuple[str | None, ...] = domains[kind] if domains[kind] else (None,)
        for sub_type in sub_types:
            expanded[(kind, sub_type)] = "record_index"
    return build_keys_config(expanded, domains), degraded


# ---------------------------------------------------------------------------
# `streams:` block
# ---------------------------------------------------------------------------


def _write_kind_unit(
    w: Callable[[str], None], unit: _KindStreamUnit, status: _UnitStatus
) -> None:
    """Write one kind-shaped stream entry to the live `streams:` list.

    Args:
        w: Line-writing callable.
        unit: The proposed unit.
        status: The unit's classification.
    """
    if status != "live":
        note = (
            f"sub-type value {unit.sub_type!r} of kind '{unit.kind}' is not a legal"
            " topic name (must match ^[A-Za-z0-9._-]+$, and not '.' or '..'); rename"
            " before uncommenting"
            if status == "topic_illegal"
            else f"name '{unit.name}' collides with an earlier proposal above;"
            " rename one before uncommenting"
        )
        w(f"    # NOTE: {note}")
        w(f"    # - name: {unit.name}")
        w(f"    #   kind: {unit.kind}")
        if unit.sub_type is not None:
            w(f"    #   sub_types: [{unit.sub_type}]")
        w(f"    #   properties: [{', '.join(unit.properties)}]")
        return
    if unit.sub_type is not None and unit.sub_type == unit.domain[0]:
        w(
            f"    # kind '{unit.kind}' declares sub-types: {', '.join(unit.domain)}"
            " (one stream per sub-type below)"
        )
    if unit.union_fallback:
        w(
            f"    # NOTE: the sidecar carries no sub_type_columns partition for"
            f" kind '{unit.kind}'; proposing the full column union for this sub-type"
        )
    if unit.lifecycle_only:
        w(
            "    # NOTE: this population carries no tracked property; the feed is"
            " lifecycle-only (c/d events only) -- delete to opt out"
        )
    w(f"    - name: {unit.name}")
    w(f"      kind: {unit.kind}")
    if unit.sub_type is not None:
        w(f"      sub_types: [{unit.sub_type}]")
    w(f"      properties: [{', '.join(unit.properties)}]")


def _write_streams_block(
    w: Callable[[str], None],
    kind_units: "Sequence[tuple[_KindStreamUnit, _UnitStatus]]",
) -> None:
    """Write the live `streams:` list — one entry per kind-shaped unit.

    Args:
        w: Line-writing callable.
        kind_units: Every proposed kind-shaped unit with its classification.
    """
    w("streams:")
    for unit, status in kind_units:
        _write_kind_unit(w, unit, status)


def _write_membership_alternative(
    w: Callable[[str], None],
    membership_units: "Sequence[tuple[_MembershipStreamUnit, _UnitStatus]]",
) -> None:
    """Write the fully-commented `content: membership-events` alternative block.

    A collision-loser membership entry is excluded from the uncommentable
    body and carried as a collision comment instead, so uncommenting the
    block wholesale still yields a config that parses and streams clean.

    Args:
        w: Line-writing callable.
        membership_units: Every proposed membership-shaped unit with its
            classification.
    """
    w("")
    w(
        "# Alternative: membership-events -- one stream per membership table."
        " Uncomment this"
    )
    w(
        "# block (and comment out `content:` / `streams:` above) to stream"
        " membership events instead."
    )
    w("#")
    w("# content: membership-events")
    w("#")
    w("# streams:")
    for unit, status in membership_units:
        if status != "live":
            w(
                f"#   # NOTE: name '{unit.name}' collides with an earlier proposal;"
                " excluded here -- rename before including it"
            )
            continue
        w(f"#   - name: {unit.name}")
        w(f"#     membership: {{kind: {unit.owner_kind}, property: {unit.property}}}")
        w(f"#     fields: [{', '.join(unit.fields)}]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_candidate_yaml(emit: "Emit", notice_sink: "NoticeSink") -> str:
    """Build a commented candidate `StreamConfig` YAML from the sidecar.

    Args:
        emit: The open emit. Its sidecar must carry >= 1 records kind
            (checked by `generate_stream_init_config` before this is called).
        notice_sink: Receiver for slice-only-column-omitted notices.

    Returns:
        A YAML string with candidate config and inline comments.

    Raises:
        StreamInitNothingToStream: No kind-shaped unit survives live (every
            sidecar-derived name topic-illegal).
    """
    sidecar = emit.sidecar
    known_kinds = known_records_kinds(sidecar)
    domains = domains_for_kinds(sidecar, known_kinds)
    presentation_keys = sidecar.presentation_keys()
    expanded = natural_expanded_surfaces(presentation_keys, domains)

    units = _proposed_units(sidecar, notice_sink)
    statuses = _classify_units(units)
    kind_units = [
        (unit, status)
        for unit, status in zip(units, statuses)
        if isinstance(unit, _KindStreamUnit)
    ]
    membership_units = [
        (unit, status)
        for unit, status in zip(units, statuses)
        if isinstance(unit, _MembershipStreamUnit)
    ]

    if not any(status == "live" for _, status in kind_units):
        raise StreamInitNothingToStream(
            "every sidecar-derived stream name is topic-illegal; no candidate"
            " streaming config can be proposed"
        )

    keys_config, degraded = _self_gate_streaming_keys(
        sidecar, domains, expanded, kind_units, membership_units
    )

    buf = io.StringIO()

    def w(line: str = "") -> None:
        buf.write(line + "\n")

    w("# Candidate streaming config — generated by `fabulexa-forge init`")
    w("# This is a starting point. Review every stream declaration.")
    w(
        "# Stream declarations are AUTHOR-AUTHORITATIVE — confirm, rename, or"
        " split each."
    )
    w("")
    w("content: state-changes")
    w("")
    _write_streams_block(w, kind_units)
    w("")
    write_keys_block(w, keys_config, degraded)
    w(
        "# rebase: / debezium: / clock: / kafka: -- never proposed; delivery and"
        " environment"
    )
    w(
        "# knobs, not emit-derived. Add them yourself, e.g. debezium:"
        " {table_identity: source_table, ...}"
    )
    _write_membership_alternative(w, membership_units)

    return buf.getvalue()


def generate_stream_init_config(emit: "Emit", notice_sink: "NoticeSink") -> str:
    """Generate a commented candidate `StreamConfig` YAML from an emit.

    A pure function of `(emit, code version)`: one live declared stream per
    population — per declared sub-type for a sub-typed kind (properties from
    the `sub_type_columns` partition's payload-role entries, union-set
    fallback with a comment when the sidecar omits the partition), per kind
    for a flat kind — a lifecycle-only population's stream live under an
    advisory comment, name-collision losers and topic-illegal names emitted
    commented out, the `keys` block proposed and self-gated per the
    key-election init contract, and the membership-events alternative fully
    commented. Names are sidecar identity verbatim; no intent is inferred.
    Non-exempt `slice_only` columns are never proposed, one notice each. The
    emitted text always parses into a valid `StreamConfig` and streams clean
    against this emit (self-gated; every proposal is live except collision
    losers and topic-illegal names, a collision pair's first entry stays
    live, and no live proposal at all is a refusal, not an emitted config).

    Args:
        emit: An open, version-gated emit (trusted as conformant; not
            re-validated).
        notice_sink: Required notice channel; receives one
            'slice-only-column-omitted' notice per skipped column.

    Returns:
        The candidate config YAML, commented, ending in a trailing block
        naming the never-proposed delivery blocks (rebase / debezium / clock
        / kafka) and the fully-commented membership-events alternative.

    Raises:
        StreamInitNothingToStream: The emit carries no records kind, or no
            proposal survives live (every sidecar-derived name
            topic-illegal) — a candidate config that cannot stream is not
            proposed.
        ReaderError: Sidecar access failures surface unchanged — including
            TemporalClassUnavailableError on an emit predating per-column
            temporal classes.
    """
    if not known_records_kinds(emit.sidecar):
        raise StreamInitNothingToStream(
            "this emit carries no records kind; a candidate streaming config"
            " that cannot stream is not proposed"
        )
    return _build_candidate_yaml(emit, notice_sink)
