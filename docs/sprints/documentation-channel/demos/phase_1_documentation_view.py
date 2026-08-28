#!/usr/bin/env python
"""
Demo: The reader's documentation view — one typed surface over an emit's
scenario narrative, table prose, per-column description/unit, and glossed
enum options.

Sprint: documentation-channel
Phase: 1

Builds a documented fixture sidecar in-process (no run.duckdb needed —
Sidecar.documentation() only reads base.json) and prints the resolved
dictionary: a structural column's contract string with its placeholder
bound, a payload column's sidecar prose, an undocumented column's silence,
the glossed enum options, and the scenario narrative.
"""

from __future__ import annotations

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.reader import Sidecar

# Embedded fixture sidecar — a customer-accounts scenario with a documented
# balance, an undocumented note, a self-reference (exercising the
# ref_index__<name> placeholder), and a glossed status domain.
SAMPLE_SIDECAR: dict[str, object] = {
    "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
    "scenario_description": (
        "A retail loyalty-program simulation tracking customer accounts and balances."
    ),
    "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1_000}],
    "enum_domains": {
        "customer": {
            "status": [
                {
                    "value": "active",
                    "description": "Account is open and in good standing.",
                },
                {"value": "closed", "description": "Account has been closed."},
            ]
        }
    },
    "tables": [
        {
            "name": "history",
            "category": "fixed",
            "rows": 0,
            "columns": [
                {"name": "fork_path", "type": "VARCHAR"},
                {"name": "kind", "type": "VARCHAR"},
                {"name": "record_id", "type": "VARCHAR"},
                {"name": "property", "type": "VARCHAR"},
                {"name": "sim_time", "type": "BIGINT"},
                {"name": "value", "type": "VARCHAR"},
            ],
        },
        {
            "name": "records__customer",
            "category": "records",
            "record_kind": "customer",
            "description": "Customer accounts opened during the simulation.",
            "rows": 1,
            "columns": [
                {"name": "fork_path", "type": "VARCHAR"},
                {"name": "record_id", "type": "VARCHAR"},
                {"name": "record_index", "type": "BIGINT"},
                {"name": "created_sim_time", "type": "BIGINT"},
                {"name": "active", "type": "BOOLEAN"},
                {"name": "deactivated_at", "type": "BIGINT"},
                {"name": "last_mutation_sim_time", "type": "BIGINT"},
                {
                    "name": "prop__balance",
                    "type": "DOUBLE",
                    "history_tracked": True,
                    "temporal_class": "tracked",
                    "description": "Current account balance.",
                    "unit": "GBP",
                },
                {
                    "name": "prop__notes",
                    "type": "VARCHAR",
                    "history_tracked": False,
                    "temporal_class": "slice_only",
                },
                {
                    "name": "prop__status",
                    "type": "VARCHAR",
                    "history_tracked": False,
                    "temporal_class": "constant",
                },
                {
                    "name": "prop__referred_by",
                    "type": "VARCHAR",
                    "history_tracked": False,
                    "temporal_class": "constant",
                    "references": "customer",
                },
                {"name": "ref_index__referred_by", "type": "BIGINT"},
            ],
        },
    ],
}


def main() -> int:
    sidecar = Sidecar.from_raw(SAMPLE_SIDECAR)
    docs = sidecar.documentation()

    print("=== Scenario narrative ===")
    print(docs.scenario_description())

    print("\n=== Table prose (records__customer) ===")
    print(docs.table_description("records__customer"))

    print(
        "\n=== Structural column: ref_index__referred_by (contract, <name> bound) ==="
    )
    ref_doc = docs.column_doc("records__customer", "ref_index__referred_by")
    assert ref_doc is not None
    print(f"origin={ref_doc.origin!r} description={ref_doc.description!r}")

    print("\n=== Payload column with docs: prop__balance (sidecar) ===")
    balance_doc = docs.column_doc("records__customer", "prop__balance")
    assert balance_doc is not None
    print(
        f"origin={balance_doc.origin!r} description={balance_doc.description!r} "
        f"unit={balance_doc.unit!r}"
    )

    print("\n=== Undocumented payload column: prop__notes ===")
    notes_doc = docs.column_doc("records__customer", "prop__notes")
    print(notes_doc)

    print("\n=== Glossed enum options: customer.status ===")
    for option in docs.enum_options("customer", "status"):
        print(f"  {option.value}: {option.description}")

    if docs.scenario_description() is None:
        return 1
    if ref_doc.origin != "contract" or "referred_by" not in (ref_doc.description or ""):
        return 1
    if balance_doc.origin != "sidecar":
        return 1
    if notes_doc is not None:
        return 1

    print("\nSUCCESS: documentation view resolved all five surfaces correctly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
