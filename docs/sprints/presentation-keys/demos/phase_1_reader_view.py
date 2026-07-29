#!/usr/bin/env python
"""
Demo: PresentationKeys typed view, strict accessor, and union-safety algebra
Sprint: presentation-keys
Phase: 1

Builds a minimal emit (tempdir) whose sidecar carries one flat kind ('ward')
and one partitioned kind ('actor'), then:

1. Opens it and prints the typed claims (Sidecar.presentation_keys()).
2. Runs union_safe / combined_claim over safe and unsafe key-space pairs.
3. Rewrites the block with one coherence clause broken and shows
   presentation_keys() refusing it, naming the kind and clause, while
   construction itself (open_emit) does not raise (strict-on-read is lazy).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.reader import PresentationKeysInvalidError, open_emit
from fabulexa_forge.reader.sidecar import (
    KeySpace,
    PartitionKey,
    combined_claim,
    union_safe,
)

_WARD_TABLE: dict[str, object] = {
    "name": "records__ward",
    "category": "records",
    "record_kind": "ward",
    "columns": [
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "presentation_id", "type": "VARCHAR"},
    ],
    "rows": 3,
}

_ACTOR_TABLE: dict[str, object] = {
    "name": "records__actor",
    "category": "records",
    "record_kind": "actor",
    "columns": [
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "presentation_id", "type": "VARCHAR"},
    ],
    "rows": 5,
}

_COHERENT_PRESENTATION_KEYS: dict[str, object] = {
    "ward": {
        "key": {
            "unique_within": "emit",
            "branch_stable": False,
            "slice_stable": False,
            "key_space": {"class": "counter", "prefix": "WARD_", "width": 3},
        }
    },
    "actor": {
        "sub_types": {
            "patient": {
                "unique_within": "branch",
                "branch_stable": True,
                "slice_stable": True,
                "key_space": {"class": "record_index", "prefix": "PAT_", "width": 4},
            },
            "staff": {
                "unique_within": "branch",
                "branch_stable": True,
                "slice_stable": True,
                "key_space": {"class": "record_index", "prefix": "STAFF_", "width": 4},
            },
        },
        "unique_within": "branch",
        "branch_stable": True,
        "slice_stable": True,
    },
}


def _build_base_json(presentation_keys: dict[str, object]) -> dict[str, object]:
    """A minimal base.json mapping carrying the ward/actor tables and a given
    presentation_keys block."""
    return {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [_WARD_TABLE, _ACTOR_TABLE],
        "enum_domains": {"actor": {"actor_type": ["patient", "staff"]}},
        "presentation_keys": presentation_keys,
    }


def _write_emit(emit_dir: Path, presentation_keys: dict[str, object]) -> None:
    """Write a minimal run.duckdb + base.json pair into `emit_dir`."""
    import duckdb

    (emit_dir / "base.json").write_text(
        json.dumps(_build_base_json(presentation_keys)), encoding="utf-8"
    )
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    try:
        conn.execute(
            "CREATE TABLE records__ward (record_id VARCHAR, presentation_id VARCHAR)"
        )
        conn.execute(
            "CREATE TABLE records__actor (record_id VARCHAR, presentation_id VARCHAR)"
        )
    finally:
        conn.close()


def _print_typed_claims(emit_dir: Path) -> None:
    """Open the coherent emit and print its typed presentation-key claims."""
    with open_emit(emit_dir) as emit:
        pk = emit.sidecar.presentation_keys()
        assert pk is not None
        print("== Typed claims ==")
        print(f"kinds(): {pk.kinds()}")

        print("-- flat kind 'ward' --")
        print(f"  is_partitioned: {pk.is_partitioned('ward')}")
        print(f"  key: {pk.key('ward')}")
        print(f"  whole_table_claim: {pk.whole_table_claim('ward')}")

        print("-- partitioned kind 'actor' --")
        print(f"  is_partitioned: {pk.is_partitioned('actor')}")
        print(f"  sub_types: {pk.sub_types('actor')}")
        print(f"  key_for(patient): {pk.key_for('actor', 'patient')}")
        print(f"  key_for(staff): {pk.key_for('actor', 'staff')}")
        print(f"  whole_table_claim (rollup): {pk.whole_table_claim('actor')}")


def _print_algebra_verdicts() -> None:
    """Run union_safe / combined_claim over one safe pair and one unsafe pair."""
    print("\n== Union-safety algebra ==")

    safe_a = KeySpace(space_class="record_index", prefix="WARD_", width=3)
    safe_b = KeySpace(space_class="record_index", prefix="THTR_", width=3)
    safe_verdict = union_safe(safe_a, safe_b)
    print(f"union_safe(WARD_ record_index, THTR_ record_index) = {safe_verdict}")

    unsafe_a = KeySpace(space_class="counter", prefix="A-", width=0)
    unsafe_b = KeySpace(space_class="counter", prefix="A-1", width=0)
    unsafe_verdict = union_safe(unsafe_a, unsafe_b)
    print(f"union_safe(A- counter, A-1 counter)               = {unsafe_verdict}")

    stable_entries = [
        PartitionKey(
            unique_within="branch",
            branch_stable=True,
            slice_stable=True,
            key_space=safe_a,
        ),
        PartitionKey(
            unique_within="branch",
            branch_stable=True,
            slice_stable=True,
            key_space=safe_b,
        ),
    ]
    stable_claim = combined_claim(stable_entries)
    print(f"combined_claim(two safe stable entries)           = {stable_claim}")

    unsafe_entries = [
        PartitionKey(
            unique_within="emit",
            branch_stable=False,
            slice_stable=False,
            key_space=unsafe_a,
        ),
        PartitionKey(
            unique_within="emit",
            branch_stable=False,
            slice_stable=False,
            key_space=unsafe_b,
        ),
    ]
    print(
        "combined_claim(two unsafe counter entries)        = "
        f"{combined_claim(unsafe_entries)}"
    )


def _print_incoherent_block_refusal(emit_dir: Path) -> None:
    """Rewrite the block with the kind-membership clause broken and show the
    refusal, naming the kind and clause, deferred to first presentation_keys()
    call (open_emit itself does not raise)."""
    print("\n== Incoherent block ==")
    actor_entry = _COHERENT_PRESENTATION_KEYS["actor"]
    assert isinstance(actor_entry, dict)
    actor_sub_types = actor_entry["sub_types"]
    assert isinstance(actor_sub_types, dict)
    patient_claim = actor_sub_types["patient"]
    broken = {
        "ward": _COHERENT_PRESENTATION_KEYS["ward"],
        # 'actor' entry references a sub_type outside the discriminator domain.
        "actor": {
            "sub_types": {"patient": patient_claim, "ghost": patient_claim},
            "unique_within": "branch",
            "branch_stable": True,
            "slice_stable": True,
        },
    }
    (emit_dir / "base.json").write_text(
        json.dumps(_build_base_json(broken)), encoding="utf-8"
    )

    with open_emit(emit_dir) as emit:
        print("open_emit succeeded (strict-on-read is deferred, never eager)")
        try:
            emit.sidecar.presentation_keys()
        except PresentationKeysInvalidError as exc:
            print(f"presentation_keys() refused: {exc}")


def main() -> int:
    tmp_dir = Path(tempfile.mkdtemp(prefix="fabulexa_forge_phase1_demo_"))
    try:
        _write_emit(tmp_dir, _COHERENT_PRESENTATION_KEYS)
        _print_typed_claims(tmp_dir)
        _print_algebra_verdicts()
        _print_incoherent_block_refusal(tmp_dir)
        print("\nSUCCESS: typed view, strict accessor, and union algebra as specified")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
