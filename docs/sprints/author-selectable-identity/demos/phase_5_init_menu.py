#!/usr/bin/env python
"""
Demo: The `init` election menu — uniform `record_index` + commented alternatives.

Builds a fixture sidecar with two populations:

  - `patient` (flat kind): registry-declared (a `presentation_keys.key` entry),
    so its menu offers both `record_id` and `presentation_id` alternatives.
  - `actor` (partitioned kind, sub-types `clinician` / `staff`): only
    `clinician` is registry-declared, so the active election renders as a
    per-sub-type map (shape follows the alternatives, not the uniformly
    `record_index` active values) — `clinician` offers both alternatives,
    `staff` only `record_id`.

`propose_key_election` + `render_keys_block` are the one shared module the
dimensional, source, and streaming `init` engines all splice verbatim — this
demo exercises the module directly. It then writes a candidate config with
one alternative uncommented *alongside* its active line (rather than
replacing it) and shows the Phase-1 duplicate-key refusal catching the
mistake at load time.

Sprint: author-selectable-identity
Phase: 5
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.errors import ConfigError
from fabulexa_forge.exporters.keys_init import propose_key_election, render_keys_block
from fabulexa_forge.reader.sidecar import Sidecar

_UUID_KEY = {
    "key_space": {"class": "uuid"},
    "unique_within": "branch",
    "branch_stable": True,
    "slice_stable": True,
}

_RAW_SIDECAR: dict[str, object] = {
    "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
    "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    "tables": [
        {
            "name": "records__patient",
            "category": "records",
            "record_kind": "patient",
            "rows": 1,
            "columns": [
                {"name": "fork_path", "type": "VARCHAR"},
                {"name": "record_id", "type": "VARCHAR"},
                {"name": "presentation_id", "type": "VARCHAR"},
                {"name": "record_index", "type": "BIGINT"},
                {"name": "prop__status", "type": "VARCHAR"},
            ],
        },
        {
            "name": "records__actor",
            "category": "records",
            "record_kind": "actor",
            "rows": 2,
            "columns": [
                {"name": "fork_path", "type": "VARCHAR"},
                {"name": "record_id", "type": "VARCHAR"},
                {"name": "presentation_id", "type": "VARCHAR"},
                {"name": "record_index", "type": "BIGINT"},
                {"name": "prop__actor_type", "type": "VARCHAR"},
                {"name": "prop__name", "type": "VARCHAR"},
            ],
        },
    ],
    "enum_domains": {"actor": {"actor_type": ["clinician", "staff"]}},
    "presentation_keys": {
        "patient": {"key": _UUID_KEY},
        "actor": {
            "sub_types": {"clinician": _UUID_KEY},
            "unique_within": "branch",
            "branch_stable": True,
            "slice_stable": True,
        },
    },
}


def _uncomment_duplicate(lines: list[str]) -> str:
    """Splice a menu block that "uncomments" `patient`'s presentation_id
    alternative alongside its active `record_index` line — the wrong-way
    activation the Phase-1 duplicate-key loader must catch.
    """
    out: list[str] = []
    for line in lines:
        out.append(line)
        if line == "  patient: record_index":
            out.append("  patient: presentation_id")
    return "\n".join(out)


def main() -> int:
    sidecar = Sidecar.from_raw(_RAW_SIDECAR)

    proposal = propose_key_election(sidecar)
    print("active:", dict(proposal.active))
    print("alternatives:", {k: list(v) for k, v in proposal.alternatives.items()})

    errors: list[str] = []
    if proposal.active.get("patient") != "record_index":
        errors.append("patient: active election is not uniform record_index")
    if list(proposal.alternatives.get("patient", ())) != [
        "record_id",
        "presentation_id",
    ]:
        errors.append("patient: expected both alternatives (registry-declared)")
    if proposal.active.get("actor") != {
        "clinician": "record_index",
        "staff": "record_index",
    }:
        errors.append("actor: expected a per-sub-type map, uniformly record_index")
    if list(proposal.alternatives.get("actor.clinician", ())) != [
        "record_id",
        "presentation_id",
    ]:
        errors.append("actor.clinician: expected both alternatives (registry-declared)")
    if list(proposal.alternatives.get("actor.staff", ())) != ["record_id"]:
        errors.append("actor.staff: expected only record_id (undeclared)")

    lines = render_keys_block(proposal)
    print("\nrendered keys: block:")
    for line in lines:
        print(f"  {line}")

    if not any("SWAPS the active" in line for line in lines):
        errors.append("rendered block missing the swap-not-uncomment header")
    if lines[0] != "keys:":
        errors.append("rendered block does not lead with 'keys:'")

    for error in errors:
        print(f"FAILURE: {error}")
    if errors:
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "candidate.yaml"
        config_path.write_text(
            "mode: dimensional\n\n" + _uncomment_duplicate(lines) + "\n",
            encoding="utf-8",
        )
        try:
            load_export_config(config_path)
        except ConfigError as exc:
            print(f"\nDuplicate-key activation refused: {exc}")
            if "patient" not in str(exc) or str(config_path) not in str(exc):
                print("FAILURE: error message missing key name or file path")
                return 1
        else:
            print("FAILURE: activating an alternative alongside the active line loaded")
            return 1

    print(
        "\nSUCCESS: uniform record_index active for every population; "
        "record_id/presentation_id offered as swap-not-join comments; "
        "activating one alongside (not instead of) the active line fails fast"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
