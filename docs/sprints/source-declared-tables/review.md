# Sprint Review: source-declared-tables

**Date:** 2026-07-31
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)
**Diff base:** `dcd6e1d` (`state.yaml:parent_branch` = `enable_better_ids`)
**Scope:** 71 files across 4 commits (Phases 1-4)

Gates were split across three parallel reviewers to keep tier-2 context loadable:
gates 1/2/7 (dead code, DRY, spec↔codebase), gates 3/4/6 (test names, test value,
type-ignore density), gates 5/8/9/10 (coverage, workspace, lint, demos).

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# Future:`/`# TODO`/bare-pass scaffolding; every sprint-added public symbol traced via `find_references` to a real production caller — no orphaned exports. |
| 2. Consistency / DRY | observations | 1 | Phase 2's admitted temporary duplicate is genuinely retired; `keys_init.py` is real reuse from both init sites. One stale module docstring. |
| 3. Test names | observations | 1 | Every ordering/determinism/error-type test name checked against its body; all honest. One docstring overstates what its terminal assertions can catch. |
| 4. Test value | **blockers** | 3 | One assertion weakened against the pre-sprint version so a swap bug would escape; two parametrize/existence-check observations. |
| 5. Coverage | clean | 0 | New src/ files: `populations.py` 100%, `events.py` 99%, `keys_init.py` 100%, `source/init.py` 97%. No uncovered line is an error path. |
| 6. Type-ignore density | observations | 2 | 4 ignores added across 2 test files; one file exceeds the >1/file heuristic. |
| 7. Spec ↔ codebase | observations | 1 | Contracts 4/7/8 match the spec verbatim; six retired errors genuinely gone; the plan-time guard move is spec-documented, not an undocumented deviation. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty; no stray `.duckdb`/temp/probe/codemod artifacts. |
| 9. Lint & typecheck | clean | 0 | `make lint typecheck`: ruff check + `325 files already formatted` + mypy `no issues found in 119 source files`. |
| 10. Demos | clean | 0 | All 4 demos ran twice, exit 0, stdout byte-for-byte identical between runs (determinism invariant holds). |

## Findings

### Gate 2: Consistency / DRY

- **[observation]** `src/fabulexa_forge/exporters/source/events.py:6-8` — Module docstring is
  stale: "`SourceEventSourcePlan`/`SourceEventLogPlan` are hand-constructed in tests during this
  phase; a later phase wires `plan.py` to produce them and the engine to compile them." Phase 3
  already wired this (plan.py and engine.py both construct/consume these types in production) —
  the docstring still describes Phase 2's interim state as if it were current, which will mislead
  a future reader into thinking the wiring is still pending.

Verified clean, for the record: `_population_case_expr` no longer exists workspace-wide; both
`renders.py` and `events.py` call the single `build_identity_translation_sql` in `election.py`.
`keys_init.py`'s four extracted helpers (`domains_for_kinds`, `natural_expanded_surfaces`,
`build_keys_config`, `write_keys_block`) are each referenced from **both** `dimensional/init.py`
and `source/init.py` — genuine reuse, not re-implementation.

### Gate 3: Test names

- **[observation]** `tests/exporters/source/test_election_renders.py:180-201`
  `test_edge_mixed_population_resolution_reads_spine_deactivated_target_resolves` — docstring
  claims the mixed election "resolves" per-row and is "digit-rendered", but the terminal
  assertions (`day_row is not None` / `night_row is not None`) are dead code: `day_row`/`night_row`
  come from `next(...)` generator expressions that already raise `StopIteration` if the predicate
  never matches, so `is not None` can never be False. See also Gate 4 (same test, real behavior loss).

### Gate 4: Test value

- **[blocker]** `tests/exporters/source/test_election_renders.py:192-201` — regression vs. the
  pre-sprint version (`assert by_id["ord_a"]["device_id"] == "DAY_001"` /
  `by_id["ord_b"]["device_id"] == "1"`). The new form
  (`next(r for r in rows if r["device_id"] == "DAY_001")`) only checks that *some* row has each
  device_id value, decoupled from which order produced it — a swap bug (order A gets device B's
  value and vice versa) would no longer be caught. Fix: keep the by-order-id lookup and assert
  `by_id["ord_a"]["device_id"] == "DAY_001"` / `by_id["ord_b"]["device_id"] == "1"` directly.

- **[observation]** `tests/config/test_source_config.py:208-241`
  `test_declare_keys_composes_with_tables` / `_with_events` / `_with_both_tables_and_events` —
  three tests differing only in which of `tables`/`events` is present; two of the three assert only
  `config.events is not None` (existence, not the known content `name == "versions"`).
  Parametrize-candidate per the Gate 4 multiplication rule; strengthen the existence checks to
  value checks while consolidating.

- **[observation]** `tests/config/test_source_decls.py` — 16 validation tests across
  `MembershipRef`/`SourceTableDecl`/`SourceEventSourceDecl`/`SourceEventsDecl` share the identical
  shape (`with pytest.raises(ValidationError, match=...)`) differing only in the field name and
  match string (e.g. `*_empty_rejected` x4, `*_duplicate_rejected` x5). Real per-field coverage,
  but the four "empty" tests and five "duplicate" tests are structurally identical and are strong
  `@pytest.mark.parametrize` candidates (field name, kwargs, match string as params) rather than a
  blocker — no assertion is weak, this is pure duplication of scaffolding.

### Gate 6: Type-ignore density

- **[observation]** `tests/exporters/source/test_events_render.py:115,132,137` — 3 ignores in one
  file, exceeding the >1/file heuristic. `_eval_scalar` (line 112-117) returns `object` with
  `# type: ignore[index]` on `.fetchone()[0]`, then two call sites do
  `json.loads(_eval_scalar(expr))  # type: ignore[arg-type]`. Centralize: type `_eval_scalar` (or
  add a thin `_eval_scalar_str` wrapper) to return `str` once inside the helper so the two
  call-site ignores disappear. Only 2 of the 3 share the same shape, short of the >=3-same-shape
  trigger, but still worth folding into the helper.

- **[observation]** `tests/exporters/source/test_renders.py:126` — single
  `# type: ignore[arg-type]` on `builder(...)` where `builder` is chosen at runtime between
  `build_junction_render_sql` / `build_state_render_sql` (different `table` param types). One
  occurrence, under threshold; legitimate polymorphic-dispatch ignore, not a fixture-literal
  ignore — no action needed.

### Gate 7: Spec ↔ codebase

- **[observation]** `src/fabulexa_forge/exporters/source/events.py:6-8` — same stale-docstring
  finding as Gate 2, viewed from the spec-fidelity direction: the implementation now
  exceeds/completes what the spec's Phase 2 contract described, but the module docstring wasn't
  updated post-cutover. A spec-fidelity nit, not a behavior bug.

7c (impl → spec reverse audit) surfaced no case where the spec prescribed a new helper, constant,
or fixture builder that duplicated something the codebase already had. The one deviation checked
in depth — moving the elected-key uniqueness guard from engine.py to plan time — is documented in
the spec itself (spec.md:63/115), so it is not an undocumented deviation.

## Recommendation

**REVISIONS NEEDED** — one blocker (Gate 4, weakened identity-translation assertion in
`test_election_renders.py`). Everything else is observations, which do not gate merge and are
surfaced for the user's per-item decision at the ACCEPT/FIX checkpoint.
