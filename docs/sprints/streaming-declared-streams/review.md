# Sprint Review: streaming-declared-streams

**Date:** 2026-08-11
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary
| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code scan | Clean | 0 | No `# TODO`/`# Future:` markers, no bare `pass` bodies added; every new public symbol (`build_elected_identity_index`, `elect_after_image_columns`, `resolve_stream_surfaces`, `build_topic_set`, `generate_stream_init_config`, `StreamInitNothingToStream`, `field_resolves`) traces to a production caller via `find_references`. |
| 2. Consistency / DRY | Observations | 1 (3 sub-instances) | Three near-identical private helper pairs duplicated between `engine.py` and `init.py` within this sprint instead of being shared; one pair (`_known_records_kinds`) diverges in *implementation strategy*, not just location. |
| 3. Test name audit | Clean | 0 | Sampled `test_election_stream.py` (23 tests) and `test_init.py` (20 tests) exhaustively; every test name matches its assertions (op/key/gate/ordering names all check what they claim). |
| 4. Test value audit | Clean | 0 | `test_election_stream.py` asserts exact dict literals (`render_jsonl_object(...) == {...}`) and exact key/column lists throughout — no `len(x) > 0` / bare `is not None` laxity found against fixtures with known values. |
| 5. Coverage analysis | Observations | 4 | 98-99% on every new/touched streaming/derivations/config file; 4 specific gaps are genuine untested branches, not noise (see Findings). |
| 6. Type-ignore density | Clean | 0 | 4 new `# type: ignore` lines added this sprint, one each in 4 different test files — well under the 1-per-file / 3-cross-file smell threshold. |
| 7. Spec-implementation comparison | Clean | 0 | Every Phase 1–4 contract (`build_row_state_events_sql`, `iter_stream_events`, `build_topic_set`, `StreamEvent`, `build_elected_identity_index`, `elect_after_image_columns`, `generate_stream_init_config`) matches the spec's signature/docstring/behavior; sprint notes show `review_cycles: 0` and no disputed findings across all 4 phases; the Gate 2 duplication is the only impl→spec-direction observation (not a spec defect — an implementer choice not to reuse the existing `source/plan.py`/`source/init.py` sidecar-category convention). |
| 8. Workspace check | Clean | 0 | `git status --porcelain` empty; no untracked files. |
| 9. Pre-commit compliance | Clean | 0 | `pre-commit run --files <72 changed files>` — all hooks passed (trim whitespace, end-of-file, yaml, ruff, ruff format, mypy strict). |
| 10. Demo verification | Clean | 0 | All 4 demos (`phase_1_fold_split.py` … `phase_4_init_streaming.py`) ran twice each; exit 0 and byte-identical stdout both times. |

## Findings

### Gate 2 — Consistency / DRY

**Finding 1 (observation).** Three helper pairs were introduced independently in `src/fabulexa_forge/exporters/streaming/engine.py` and `src/fabulexa_forge/exporters/streaming/init.py` this sprint, each doing the same computation, instead of sharing one implementation:

- `_known_records_kinds`: `engine.py:188-205` vs `init.py:109-124`. The **implementations differ**, not just the location: `engine.py`'s version derives the kind by string-slicing `table.name[len("records__"):]` after checking `table.name.startswith("records__")`; `init.py`'s version (and the pre-existing siblings `exporters/source/plan.py:272` and `exporters/source/init.py:93`) reads the sidecar's own structured `TableSpec.category == "records"` / `TableSpec.record_kind` fields — the established convention elsewhere in the codebase. `engine.py`'s version is a new, narrower reimplementation that bypasses the sidecar's declared fields in favor of name parsing.
- `_kind_reference_targets` (`engine.py:208-236`) vs `_kind_stream_reference_targets` (`init.py:392-420`): near-identical docstring and body (selected reference-valued properties of a kind, mapped to their target kind, restricted to targets present in the emit).
- `_membership_reference_fields_selected` (`engine.py:239-262`) vs `_membership_reference_fields` (`init.py:423-443`): identical logic (selected membership fields backed by a `member__<f>__kind`/`__id` pair).

None of these is a config-boundary issue — all are internal helper arguments derived from the sidecar, not author config. But it is a real DRY violation: three parallel private implementations of the same three computations, live in the same package, introduced in the same sprint (Phase 2/3 added the `engine.py` versions; Phase 4 added the `init.py` versions with no cross-reference to the earlier ones). A shared module-level helper (e.g., in `routing.py`, which both `engine.py` and `init.py` already import from) would have removed the duplication and the resulting `_known_records_kinds` implementation drift.

### Gate 5 — Coverage analysis

Four genuine untested branches (not just line-count trivia):

1. **`engine.py:328-329`** (`_resolve_membership_stream_surface`, sub-typed-owner branch): a membership stream whose owner kind is itself sub-typed never reaches `check_identity_election` / `election.surface_for(owner_kind, domain[0])` in any test. The design doc explicitly calls out "the owner kind's full declared domain always spans a membership stream" as a distinct case; `tests/exporters/streaming/_election_fixtures.py` only declares `person` (the sole membership owner) as a **flat** kind, so this branch is dead in the test suite.
2. **`engine.py:593-596`** (`_resolve_target_identity`, sub-typed-target branch): reference-property translation to a **sub-typed** target kind (`trainer.prop__pet_id` → `creature`, which the fixture doc marks sub-typed) is only exercised through the `TestGates` tests, which all raise before reaching the render pass (`ElectionMixedIdentity`/`ElectionUnionUnsafe`). `TestReferenceEdgeTranslation` only covers a flat target (`gadget.prop__target_id` → `widget`). The positive-path translation-through-a-sub-typed-target case — explicitly called out in the phase-3 sprint notes ("supporting mixed election across edges") — has no passing test.
3. **`engine.py:508`** (`elect_after_image_columns`, `record_index` branch's `return renamed`): exercised indirectly via `_rekey_after_image` (a different function) in `TestRecordIndexElection`, but never via the Debezium schema-building path (`_build_value_schemas_kinds` → `elect_after_image_columns`) the way `TestPresentationIdElection.test_debezium_value_schema_follows_elect_after_image_columns` does for `presentation_id`. No test proves the Debezium schema and the rendered after-image "stay the same list by construction" (the function's own docstring claim) under `record_index`.
4. **`init.py:523-524`** (`_self_gate_streaming_keys`, membership member-field degrade branch): only one degrade test exists (`test_keys_gate_failure_degrades_to_record_index_with_comment`), and it exercises the **kind-shaped-stream** reference-property loop (`trainer.prop__pet_id`, lines 483-500) — not the **membership** member-field loop (lines 502-524). The membership-triggered degrade path (a distinct loop over `known_kinds` per member field) has no test forcing it.

None of these are config-boundary violations or correctness defects found by inspection — they read as correct given the surrounding gated logic — but they are real gaps in branches the design doc and the sprint's own decisions single out as distinct cases.

## Recommendation
- APPROVED-WITH-NOTES (no blockers; observations recorded — mergeable, user decides on each observation)
