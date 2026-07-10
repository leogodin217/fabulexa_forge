"""Canned Debezium-envelope fixtures for the Kafka rig.

Hand-authored to conform to the pinned contract (decision note
`streaming-debezium-envelope-contract`). They stand in for the deferred Debezium
renderer (streaming S2): the renderer does not exist yet, so the rig exercises the
Kafka mechanics — keying, single-partition global ordering, message timestamp — from
fixed envelopes that already match the contract field-for-field. When the renderer
lands, these double as its golden outputs.

Shape per change (the value payload, pre-`schemas.enable` wrapping):

    before: null            (always — after-only upsert log)
    after:  full-row reconstruction (record_id + typed prop__ cols)
    source: masquerade block + derived ts_ms / lsn / sequence
    op:     "c" (genesis at created_sim_time) | "u" (coalesced later change)
    ts_ms:  rebased event time (== source.ts_ms)
    transaction: null       (no transaction grain in the sanitised subset)

The list is ordered by the derived total order (`seq` ascending). `seq` interleaves
across records, so the global-monotonicity assertion is non-trivial: r1.c, r2.c,
r1.u, r2.u.
"""

from __future__ import annotations

from typing import Any

# Arbitrary fixed rebased-event-time origin (ms). The rig asserts the Kafka message
# timestamp *equals* the payload value, not its absolute magnitude.
_T0_MS = 1_718_600_000_000
_KIND = "entity"


def _value(
    *, op: str, record_id: str, name: str, seq: int, ts_ms: int
) -> dict[str, Any]:
    """One Debezium value payload conforming to the envelope contract."""
    return {
        "before": None,
        "after": {"record_id": record_id, "prop__name": name},
        "source": {
            # Configurable masquerade block — fixed here to a Postgres identity so
            # downstream connector-pattern-matching tooling works unchanged.
            "version": "2.5.0.Final",
            "connector": "postgresql",
            "name": "fabulexa-forge",
            "db": "fabulexa",
            "schema": "public",
            "table": _KIND,
            # Derived / dynamic source fields.
            "ts_ms": ts_ms,
            "snapshot": "false",
            "txId": None,
            "lsn": seq,
            "sequence": f'[null,"{seq}"]',
        },
        "op": op,
        "ts_ms": ts_ms,
        "transaction": None,
    }


def _key(record_id: str) -> dict[str, Any]:
    """The Debezium key payload — partition key + per-entity ordering."""
    return {"record_id": record_id}


# (key_payload, value_payload) pairs in derived total order (`seq` ascending).
CANNED_ENVELOPES: list[tuple[dict[str, Any], dict[str, Any]]] = [
    (_key("r1"), _value(op="c", record_id="r1", name="alpha", seq=1, ts_ms=_T0_MS)),
    (
        _key("r2"),
        _value(op="c", record_id="r2", name="bravo", seq=2, ts_ms=_T0_MS + 1_000),
    ),
    (
        _key("r1"),
        _value(op="u", record_id="r1", name="alpha-2", seq=3, ts_ms=_T0_MS + 2_000),
    ),
    (
        _key("r2"),
        _value(op="u", record_id="r2", name="bravo-2", seq=4, ts_ms=_T0_MS + 3_000),
    ),
]
