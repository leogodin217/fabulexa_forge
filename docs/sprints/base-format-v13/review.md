# Sprint Review: base-format-v13

**Date:** 2026-10-22
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

Diff base: `8c51bcb` (merge-base with `contract-sync/v13-4762bc4b`); 2 phase commits, 55 files.

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | observations | 1 | No scaffolding/TODO/dead aliases; every new public symbol reaches a production caller; `_check_c16_pair` and `_parse_gaps` are single-call-site helpers. |
| 2. Consistency / DRY | observations | 1 | `_DIGIT_RENDERED_CLASSES` is duplicated test-side (documented, deliberate); no other duplicates against sibling helpers. |
| 3. Test names | clean | 0 | Read every new/renamed test name, docstring, body (C16, impact, clause-f, union_safe, constructors, datasets, order pins); each asserts what it claims. |
| 4. Test value | observations | 1 | Parametrization is already used where bodies repeat; one C16 test is vacuous on a zero-row table. |
| 5. Coverage | clean | 0 | Full suite 6365 passed / 23 skipped; all new lines (conformance 2083-2175, sidecar 705+, `_impact.enum_value_undeclared`, `_cell_impact`, conflict impact) covered; no new src file; uncovered lines are all pre-existing. |
| 6. Type-ignore density | observations | 1 | 5 net-new `# type: ignore[arg-type]` of one shape (`str` param into the Literal `space_class`). |
| 7. Spec <-> codebase | observations | 2 | Contracts match the spec exactly; stale "C1-C13" prose left in a recipe test docstring; spec file lists missed docstring/schema files. |
| 8. Workspace | clean | 0 | `git status --porcelain` is empty after all gates ran. |
| 9. Pre-commit | clean | 0 | `pre-commit run --all-files` exits 0 (ruff, ruff format, mypy strict, yaml/toml, understand bundles). |
| 10. Demos | clean | 0 | Both demos exit 0 on two runs each; outputs byte-identical run-to-run. |

Config boundary (primary focus): nothing in the diff defaults, `or`-falls-back, or `.get`s an author-configurable value. `start`/`gaps` are parsed strictly from the sidecar (missing/mistyped raises `PresentationKeysInvalidError`); the synthesized `_RECORD_INDEX_KEY_SPACE` is mode-definitional (decision 1); `sidecar_builder` defaults are test-helper kwargs. No finding.

## Findings

### Gate 1: Dead code

- **finding 1 (observation)**: `_check_c16_pair` (`src/fabulexa_forge/reader/conformance.py:2083`) and `_parse_gaps` (`src/fabulexa_forge/reader/sidecar.py:1053`) are each called from exactly one site. The spec prescribed a single `_check_c16`; the split is consistent with the existing `_check_c5_table` precedent and keeps the functions short, so recorded only. Severity: observation.

Verified: no `# Future:` / `# TODO:` / `pass$` / inert aliases added in src. Exported-but-uncalled end state: `Gaps` (used by `KeySpace`/`_parse_gaps`/`union_safe`), `enum_value_undeclared` (production callers `mutate_cells._cell_impact`, `duplicate_rows._conflict_duplicate_impact`), `_check_c16` (registered in `_CHECKS`) all reach production.

### Gate 2: Consistency / DRY

- **finding 1 (observation)**: `tests/_support/sidecar_builder.py:183` redefines `_DIGIT_RENDERED_CLASSES` identically to `src/fabulexa_forge/reader/sidecar.py:918`. Phase-1 note records this as deliberate (a fixture authority must not mirror a reader bug), and `partition_key` likewise keeps its own class->scalars table. Acceptable; recorded because it is a two-place edit on the next contract bump. Severity: observation.

Verified: `_is_plain_int` is the only non-bool-int helper in the package; `enum_value_undeclared` parallels `actor_subtype_undeclared` in shape/placement but checks a different registry (not a duplicate); `_check_c16` reuses `_catalog_tables`/`_catalog_columns`/`_quote_identifier`.

### Gate 4: Test value

- **finding 1 (observation)**: `tests/reader/test_conformance_data.py:2330` `test_c16_declared_option_without_live_row_is_conformant` builds a zero-row `records__actor`, so it cannot distinguish "declared option, no live row" from "no rows at all". It still pins the pass + no-skip path, but a one-row table with a different declared value would test the stated property. Severity: observation.

### Gate 6: Type-ignore density

- **finding 1 (observation)**: Net +5 `# type: ignore[arg-type]` of one shape: `tests/_support/test_sidecar_builder.py` (3: `key_space(space_class)`, two `key_space(space_class, **texture)`) and `tests/reader/test_presentation_keys.py` (3 added, 1 removed: `_ks` KeySpace construction, `_with(key_space(space_class), gaps=...)`, `_parsed_key_space(key_space(space_class))`). All are a parametrized `str` flowing into the `Literal` class parameter. Per the gate heuristic (>=3 same shape) the remedy is a small typed test helper (or a `Literal`-typed parametrize argument), not a production typing change. Severity: observation.

### Gate 7: Spec <-> codebase

- **finding 1 (observation)**: Stale prose after the vocabulary widened to C16: `tests/recipes/test_corrupt_recipes.py:15` and `:20` still describe the set-equality scope as "the manifest's C1-C13 impact vocabulary" / "The C1-C13 scoping", while `tests/recipes/_harness.py` (updated this sprint) now scopes to C1-C13 plus C16. Behavior is correct; the docstring lags. Severity: observation.
- **finding 2 (observation, spec-time miss)**: The Phase 2 file table omitted files the change necessarily touched: `src/fabulexa_forge/corrupters/defect_manifest.schema.json` (the impact enum lives there too; recorded in the Phase 2 note and required for `defects.json` schema validity), and docstring/prose-only edits in `cli.py`, `errors.py`, `reader/emit.py`, `reader/errors.py`. The spec's "grep the vocabulary's other homes" step would have caught the schema file. Calibrate the spec process; the implementation is correct. Severity: observation.

Contract comparison (spec -> impl): `Gaps`, `KeySpace`, `_parse_key_space` clause-f, `union_safe` four-parameter row, `reader/__init__` export, election constants (`start=0`/`gaps=None`; record_id None/None), `key_space`/`partition_key` constructors (signatures, ValueError arms), `_check_c16` (skips, sorted distinct offenders, messages naming kind/property/value), registry/`_RECOGNIZED_IDS`, `ImpactCode`, `enum_value_undeclared`, both impact sites, engine prose, `_MANIFEST_VOCABULARY`, empty `manifest.yaml` all match. Verified against contract: schema requires `prefix/width/start` iff digit-rendered and `gaps` digit-rendered-only, matching the parser; C16 text matches the check. Success criteria verified: no hand-built `key_space` dict literals remain (remaining `unique_within` dicts are kind-level rollup/partition scalars); only the two named recipes' ground truths moved. Phase-2 notes (schema enum, full-set agreement assertions, tiny-emit helper) are consistent with the code. Spec-listed migrate files `test_populations.py`, `streaming/test_election_stream.py`, `streaming/test_init.py` were not edited; the state.yaml step allows "no edit needed" and the grep above confirms no residual hand-built key spaces.

## Recommendation

**APPROVED-WITH-NOTES** — no blockers; 6 observations recorded across gates 1, 2, 4, 6, 7. Mergeable; fix-vs-accept is the user's call.
