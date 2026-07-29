# Sprint Review: presentation-keys

**Reviewer:** Fresh-eyes Reviewer (fabulexa-forge)
**Date:** 2026-07-29
**Diff base:** `c3380c6` (merge-base with `data_qa`) → `HEAD` (`51cacf0`)
**Phases reviewed:** 1–5 (all complete per `state.yaml`)
**Spec:** `docs/sprints/presentation-keys/spec.md`
**Design doc:** `docs/architecture/pending/presentation-keys.md`

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | Scaffolding grep (`# Future:`, `# TODO`) empty; every new public symbol (`KeySpace`, `PartitionKey`, `WholeColumnClaim`, `PresentationKeys`, `union_safe`, `combined_claim`, `PresentationKeysInvalidError`, `TableKeys`, `resolve_base_table_keys`, `resolve_source_table_keys`, `keys_not_declarable_csv_notice`) traced via `find_references` to a production caller, not just tests/demos. |
| 2. Consistency / DRY | observations | 1 | `_declare_keys_enabled` (base/engine.py, source/engine.py) and `_declare_keys_active` (incremental/driver.py) are three near-identical 1-line predicate helpers, duplicated across modules — see Findings. `column_renames.get(id, id)` identity-fallback idiom confirmed consistent with pre-existing `renders.py` usage (not a violation). |
| 3. Test name audit | clean | 0 | Sampled `test_presentation_keys.py` (all 6 coherence clauses + laziness + algebra), `test_cli_init.py` advisory-comment tests, `test_duckdb.py`/`test_query_spec.py`/`test_engine.py` (base) additions — every body does what its name/docstring claims. |
| 4. Test value audit | clean | 0 | No test multiplication beyond legitimate `@pytest.mark.parametrize` tables (`union_safe` pairwise, clause-e scalar mismatch); assertions are concrete (`assert pk.kinds() == (...)`, exact `TableKeys` tuples, `duckdb_constraints()` introspection) — no weak `len>0`/`is not None`-only assertions found in sampled files. |
| 5. Coverage | observations | 2 | `--cov=src/fabulexa_forge` run once: overall 97%, every touched production module (query_spec.py, base/plan.py, base/engine.py, source/plan.py, source/engine.py) is 100%; `sidecar.py` is 96% with a handful of untested new-code branches — see Findings. |
| 6. Type-ignore density | observations | 1 | 2 `# type: ignore[arg-type]` added, both in one test-helper file (`tests/reader/test_presentation_keys.py`), same shape (str→Literal narrowing in `_ks`/`_pk` builders) — legitimate test scaffolding, flagged per the ">1/file" threshold but low severity. |
| 7. Spec ↔ codebase | clean | 0 | 7a: git notes read for all 7 sprint commits (2 early/superseded commits carry no note — see Findings, informational only). 7b: every verbatim-contract signature/docstring/raises (`resolve_base_table_keys`, `resolve_source_table_keys`, `keys_not_declarable_csv_notice`, `write_duckdb`, reader types/functions) checked line-by-line against the design doc's § Interface Contracts — matches. 7c: no redundant helper/constant found that the spec re-invented; `presentation_id` renames build on the pre-existing `column_renames.get(id, id)` idiom, not a new invention. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty — no untracked files. |
| 9. Lint & typecheck | clean | 0 | `make lint typecheck` — `ruff check`, `ruff format --check`, `mypy src` all pass with zero findings. |
| 10. Demos | observations | 1 | All 5 demos run twice, exit 0 both times, four are byte-identical across runs; `phase_2_writer_constraints.py`'s captured `ExportRuntimeError` message embeds the OS-assigned `tempfile.mkdtemp()` path, so its two runs differ only in that substring — a demo-scaffolding artifact, not business-logic nondeterminism. |

**Full test suite:** `uv run pytest -q` → 3928 passed, 18 skipped, 0 failed.

## Findings

### Gate 2 — Consistency / DRY

1. **Triplicated `declare_keys`-active predicate** — `src/fabulexa_forge/exporters/base/engine.py:32` (`_declare_keys_enabled`), `src/fabulexa_forge/exporters/source/engine.py:94` (`_declare_keys_enabled`), `src/fabulexa_forge/incremental/driver.py:91` (`_declare_keys_active`).
   Severity: observation.
   All three are ~5-line private helpers computing `config.<mode> is not None and config.<mode>.declare_keys is True`. The phase-5 git note explicitly acknowledges this: *"single mode-dispatch helper `_declare_keys_active` local to driver.py since base/source engines were out of phase scope"* — a deliberate, documented per-phase tradeoff, not an oversight. A shared predicate (e.g. on `ExportConfig`, or in `query_spec.py` beside `TableKeys`) would eliminate the triplication, but each instance is small, private, and correctly scoped to its own module's layer-direction invariant (base/engine.py and source/engine.py explicitly may not import each other). Not a blocker.

### Gate 5 — Coverage

1. **`_prefixes_comparable`'s swapped branch is untested** — `src/fabulexa_forge/reader/sidecar.py:754-756`.
   Severity: observation.
   Every `union_safe` pairwise parametrize case in `tests/reader/test_presentation_keys.py` (lines 524-588) passes a `prefix_a` that is shorter-than-or-equal-to `prefix_b`, so the `len(prefix_a) <= len(prefix_b)` branch (line 755, `_digit_suffix_extends(prefix_a, prefix_b)`) is always taken; the `else` branch (line 756, the reversed call for `prefix_a` longer than `prefix_b`) is never exercised. The function is logically symmetric so this is unlikely to hide a real bug, but it is new sprint code with a missed branch.

2. **Defensive "not an object" / malformed-shape guards in the strict parser are untested** — `src/fabulexa_forge/reader/sidecar.py:868` (`key_space` not a mapping), `873` (`class` outside the enum), `897` (`prefix`/`width` wrong type), `921` (partition-key entry not a mapping), `931` (scalar fields missing/mistyped), `972` (`rollup.unique_within` outside `{emit, branch}`), `980` (`rollup.branch_stable`/`slice_stable` missing/mistyped), `1019`/`1032` (kind entry / `sub_types` not a mapping).
   Severity: observation.
   These are belt-and-suspenders checks beyond the six normative coherence clauses the spec's test list enumerates (spec § Phase 1 tests, design doc § Strict-on-read table) — the design doc notes structural JSON shape is C1's job, and these paths defend against a JSON-Schema-invalid block reaching the accessor anyway. Reasonable to have, but currently untested; not required by the spec's explicit test matrix, so not a blocker.

### Gate 6 — Type-ignore density

1. **2× `# type: ignore[arg-type]` in one test file** — `tests/reader/test_presentation_keys.py:125,132` (`_ks`/`_pk` typed-object test builders, narrowing a `str` parameter into `KeySpace.space_class` / `PartitionKey.unique_within`'s `Literal` types).
   Severity: observation.
   Both are same-shape, both are pure test scaffolding (parametrize-table builders accepting a plain `str` for ergonomics), not production code. Meets the skill's ">1/file" flag threshold mechanically but is the documented "test-helper remediation" case the skill treats as low severity.

### Gate 7a — Git notes

1. **Two early sprint commits carry no note** — `00f66e5` ("presentation-keys phase 1: reader typed view...") and `35fee6c` ("Sprint presentation-keys - Phase 3: Base mode declare_keys") have no entry under `refs/notes/agent/sprint`.
   Severity: observation (informational).
   Both are immediately followed by a re-committed, noted version of the same phase (`a8a3b0d` for phase 1, `e926909` for phase 3) that supersedes them in the final tree. This looks like a restarted/rewritten attempt rather than a hidden change — the final `HEAD` tree and its notes are complete and consistent for phases 1–5. No code-level impact.

### Gate 10 — Demos

1. **`phase_2_writer_constraints.py` output differs between runs by tempdir path only** — `docs/sprints/presentation-keys/demos/phase_2_writer_constraints.py:127` (`tempfile.mkdtemp(prefix=...)`).
   Severity: observation.
   The demo prints the caught `ExportRuntimeError`'s message verbatim, which embeds `output_path` — built from the OS-assigned random tempdir. Two runs' stdout therefore differ only in that substring; the semantic content (constraint types shown, which table is named, that a `ExportRuntimeError` is raised) is identical both runs. Not a defect in the reviewed writer/engine code — the message-building code itself is deterministic given a fixed path.

## Recommendation

**APPROVED-WITH-NOTES**

No Principle #7 violations found (no defaulted/invented scenario values; `declare_keys` absence maps to a documented semantic "off" mirroring the existing `slice_at` pattern; `column_renames.get(id, id)` identity-fallback is a pre-existing, consistently-applied rename idiom, not an invented mapping value). No blockers on any of the 10 gates — lint, typecheck, and the full test suite (3928 passed) are clean, coverage on every touched production module is 100% except `sidecar.py` at 96% (untested branches are defensive/symmetric, not spec-required), and all 5 demos run successfully twice. The findings above are minor, non-blocking observations for the implementer's awareness.
