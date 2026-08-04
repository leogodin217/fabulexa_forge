# Sprint Review: list-valued-predicates

**Date:** 2026-08-03
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# TODO`/`# Future:` markers added; no `pass`-only bodies; no inert self-renames; the sprint's one new public symbol (`render_predicate_condition`) and three new private helpers (`_reject_malformed_predicate`, `_predicate_elements`, `_unobserved_discriminator_notice_message`) all trace via `find_references` to production callers, not just tests/demos. |
| 2. Consistency / DRY | observations | 2 | See Findings — a duplicate-detection loop is re-implemented rather than reused, and a scalar-or-list normalization idiom is written twice (helper vs. inline) across sibling modules in the same subpackage. |
| 3. Test names | clean | 0 | Spot-checked every renamed/new test (list-IN rendering, notice-matrix rows, subset-FK closure, election-ambiguity) against its body; names match behavior, including the two `_render_typed_literal` fork-test files that were rewritten rather than merely renamed. |
| 4. Test value | clean | 0 | No test-multiplication beyond deliberate, well-labeled `@pytest.mark.parametrize` groups (`tests/config/test_models.py`'s five-surface matrix, `test_sql.py`'s type matrix); the two `is not None` assertions found in the diff are mypy-narrowing guards ahead of real value assertions, not the test's actual pin; row counts and exact SQL/message strings are asserted throughout instead of `len(x) > 0`. |
| 5. Coverage | clean | 0 | Full-suite `--cov-report=term-missing`: `_sql.py` 100%, `populations.py` 100%, `scd.py` 100%, `versioned_intervals.py` 100%; `config/models.py` 99%, `validation.py` 88%, `columns.py` 92%, `fk.py` 94%, `reference_resolution.py` 94%, `reader/relations.py` 98% — every uncovered line traced to pre-existing, sprint-untouched code (e.g. `RenameEntry` validators, the `TableNotFoundError` history fallback, `get_fork_path_from_sidecar`'s branch-count guards). No new file exists this sprint under `src/` (only `tests/test_sql.py` is new, and it is the 100%-covered authority's own test module). 4215 passed, 18 skipped, 0 failed. |
| 6. Type-ignore density | clean | 0 | One `# type: ignore[arg-type]` added (`tests/exporters/dimensional/test_elapsed.py`), mirroring an identical pre-existing ignore on the same line shape one function above it in the same file — not a new pattern, well below the density heuristic. |
| 7. Spec ↔ codebase | observations | 1 | See Findings — 7b (spec→impl) is a clean match across all three phases' contracts, signatures, docstrings, and Raises clauses; 7c surfaces the same normalization duplication as Gate 2, from the spec-authoring side (see Findings for whether the design doc/spec should have called out a shared helper). |
| 8. Workspace | clean | 0 | `git status --porcelain` empty; no untracked files. |
| 9. Lint & typecheck | clean | 0 | `make lint typecheck` — `ruff check`, `ruff format --check`, `mypy src` all pass with zero findings. |
| 10. Demos | clean | 0 | All three demos (`phase_1_rendering_authority.py`, `phase_2_list_predicates_export.py`, `phase_3_dim_population_subset.py`) run twice each, exit 0, byte-identical stdout both runs. |

Severity values:
- **clean** — gate found nothing.
- **observations** — gate found smells worth recording but no blocker.
- **blockers** — must fix before merge.

## Findings

### Gate 2: Consistency / DRY

- **finding 1** (observation): `_reject_malformed_predicate`'s duplicate-element
  detection (`src/fabulexa_forge/config/models.py:96-105`) re-implements, loop
  for loop, the same "walk a sequence, track a `seen` set, collect repeats,
  raise naming them" shape that `RenameEntry`'s existing `columns` validator
  already carries in the same file
  (`src/fabulexa_forge/config/models.py:565-575`, pre-existing, unmodified by
  this sprint). Nothing about the domain differs — both are "duplicate
  elements in a `list[str]`." A small shared `_find_duplicates(items:
  Sequence[str]) -> list[str]` in `models.py` would have served both call
  sites; instead the file now carries the same 8-line pattern twice.
  Severity: observation, not a blocker — each copy is independently
  correct and tested, and the two validators serve different fields with
  different error messages, so nothing is functionally wrong.

- **finding 2** (observation): the "normalize a scalar-or-list predicate
  value to its element list, in config order" idiom is written twice within
  the same subpackage (`exporters/dimensional/`), once as an extracted
  helper and once inline:
  - `validation.py:418-427`: `_predicate_elements(value) -> list[str]`
    (`[value] if isinstance(value, str) else list(value)`)
  - `populations.py:114`: `elements = conjunct if isinstance(conjunct, list) else [conjunct]`
    (inline, same subpackage, added one phase later)

  These are semantically identical (differ only in which side of the
  `isinstance` check they lead with) and answer the same question the spec
  poses independently in two places ("a scalar's singleton, or a list's
  elements in config order") for Phase 2's notice matrix and Phase 3's
  population selection. `validation.py` already imports from `populations.py`
  (for `resolve_dim_source_populations` etc.), so the layering would have
  allowed `populations.py` to own the normalizer and `validation.py` to reuse
  it, or a shared location either could reach. Severity: observation — a
  one-line duplication, each site independently tested and correct, but a
  clean instance of the Gate-7c "the spec itself invited the duplicate"
  pattern (see Gate 7 finding below).

### Gate 7: Spec ↔ codebase

- **finding 1** (observation, 7c — spec-time miss): the design doc / sprint
  spec describes the "scalar's singleton, or list's elements in config
  order" rule twice, once per phase (Phase 2's
  `check_discriminator_value_observed` contract and Phase 3's
  `resolve_dim_source_populations` contract), without noting that both land
  in the same subpackage and could share one normalizer. The implementation
  faithfully built what the spec said in each phase, in isolation — the miss
  is upstream of the code, in the spec not flagging the reuse opportunity
  across its own two phases. This is the same fact as Gate 2 finding 2; the
  calibration point is the spec process (spec review should cross-reference
  contracts introduced in different phases of the same sprint that touch the
  same subpackage), not this sprint's code.

## Recommendation

**APPROVED-WITH-NOTES**

No blockers. Two observations record real-but-minor duplication (an
8-line duplicate-detection loop, and a 1-line scalar-or-list
normalization idiom appearing twice in the same subpackage) — both
independently correct and fully tested, neither a Principle #7 violation
(no config value is defaulted or invented) and neither a contract
mismatch (every Phase 1/2/3 contract in the spec matches its
implementation's signature, docstring, and Raises clause verbatim, per
Gate 7b). Mergeable as-is; fix-vs-accept on the two observations is the
user's call.
