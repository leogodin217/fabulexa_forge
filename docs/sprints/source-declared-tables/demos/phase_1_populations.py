#!/usr/bin/env python
"""
Demo: The population resolver + declaration vocabulary (Phase 1)
Sprint: source-declared-tables
Phase: 1

Resolves declared-table population addresses (whole-kind, sub-type-subset,
flat-kind) against a fixture sidecar via `resolve_populations`, shows the
`membership` address parsing through the standalone decl models
(`MembershipRef` / `SourceTableDecl` — membership *resolution* against the
sidecar lands in Phase 3's plan builder, not here), and fires the three
population-resolution errors with their owner-prefixed messages.

No YAML / on-disk emit involved: `resolve_populations` and the decl models
both operate off in-memory objects (a `Sidecar` built via `Sidecar.from_raw`,
and Pydantic model construction), so the demo embeds its fixture inline.
"""

from __future__ import annotations

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import MembershipRef, SourceTableDecl
from fabulexa_forge.errors import (
    SourceSubTypesOnFlatKind,
    SourceTableKindUnknown,
    SourceTableSubTypeUnknown,
)
from fabulexa_forge.exporters.populations import Population, resolve_populations
from fabulexa_forge.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# Fixture sidecar: a flat kind (trip) and a sub-typed kind (customer)
# ---------------------------------------------------------------------------

_FIXTURE_SIDECAR: dict[str, object] = {
    "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
    "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    "tables": [
        {
            "name": "records__trip",
            "category": "records",
            "record_kind": "trip",
            "columns": [
                {"name": "fork_path", "type": "VARCHAR"},
                {"name": "record_id", "type": "VARCHAR"},
            ],
            "rows": 100,
        },
        {
            "name": "records__customer",
            "category": "records",
            "record_kind": "customer",
            "columns": [
                {"name": "fork_path", "type": "VARCHAR"},
                {"name": "record_id", "type": "VARCHAR"},
                {"name": "prop__customer_type", "type": "VARCHAR"},
            ],
            "rows": 50,
        },
        {
            "name": "membership__trip__drivers",
            "category": "membership",
            "record_kind": "trip",
            "property": "drivers",
            "columns": [
                {"name": "fork_path", "type": "VARCHAR"},
                {"name": "record_id", "type": "VARCHAR"},
            ],
            "rows": 10,
        },
    ],
    "enum_domains": {
        "customer": {"customer_type": ["standard", "vip", "trial"]},
    },
}


def build_sidecar() -> Sidecar:
    """Parse the embedded fixture into a typed Sidecar."""
    return Sidecar.from_raw(_FIXTURE_SIDECAR)


def demo_whole_kind(sidecar: Sidecar) -> tuple[Population, ...]:
    """`kind: customer` with no `sub_types` — shorthand for the full
    discriminator domain, domain declaration order."""
    result = resolve_populations(sidecar, "table 'customers'", "customer", None)
    print(f"whole-kind (customer, no sub_types)      -> {result}")
    return result


def demo_sub_type_subset(sidecar: Sidecar) -> tuple[Population, ...]:
    """`kind: customer, sub_types: [vip]` — an explicit subset."""
    result = resolve_populations(sidecar, "table 'vip_customers'", "customer", ("vip",))
    print(f"sub-type-subset (customer, sub_types=vip) -> {result}")
    return result


def demo_flat_kind(sidecar: Sidecar) -> tuple[Population, ...]:
    """`kind: trip` — a flat kind (no discriminator domain) resolves to the
    single (kind, None) atom."""
    result = resolve_populations(sidecar, "table 'trips'", "trip", None)
    print(f"flat-kind (trip)                          -> {result}")
    return result


def demo_membership_decl() -> SourceTableDecl:
    """`membership: {kind: trip, property: drivers}` — parses through the
    standalone decl model. Membership *resolution* against the sidecar
    (SourceTableMembershipUnknown) is a plan-time concern the Phase 3
    plan builder adds; `resolve_populations` itself only ever addresses
    `kind` populations."""
    decl = SourceTableDecl(
        name="trip_drivers",
        membership=MembershipRef(kind="trip", property="drivers"),
    )
    print(
        f"membership address (decl model)           -> "
        f"name={decl.name!r} membership={decl.membership}"
    )
    return decl


def demo_resolution_errors(sidecar: Sidecar) -> None:
    """Fire the three population-resolution errors, owner-prefixed."""
    try:
        resolve_populations(sidecar, "table 'ghosts'", "ghost", None)
    except SourceTableKindUnknown as exc:
        print(f"SourceTableKindUnknown:      {exc}")

    try:
        resolve_populations(sidecar, "events source #2", "customer", ("enterprise",))
    except SourceTableSubTypeUnknown as exc:
        print(f"SourceTableSubTypeUnknown:   {exc}")

    try:
        resolve_populations(sidecar, "table 'trips'", "trip", ("express",))
    except SourceSubTypesOnFlatKind as exc:
        print(f"SourceSubTypesOnFlatKind:    {exc}")


def main() -> int:
    sidecar = build_sidecar()

    print("--- Population resolution ---")
    demo_whole_kind(sidecar)
    demo_sub_type_subset(sidecar)
    demo_flat_kind(sidecar)
    demo_membership_decl()

    print("\n--- Resolution errors (owner-prefixed) ---")
    demo_resolution_errors(sidecar)

    print("\nSUCCESS: population resolver + declaration vocabulary demonstrated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
