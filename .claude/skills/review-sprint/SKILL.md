---
name: review-sprint
description: Post-implementation mechanical audit of sprint deliverables.
---

# Review Sprint

Post-implementation review with fresh eyes. Run after implement-sprint completes.

## Argument

`/review-sprint <sprint-name>` — required when more than one sprint dir exists with phases still in progress. If exactly one is in progress, the name may be omitted.

The sprint's parent branch (the diff base) is read from `docs/sprints/<sprint-name>/state.yaml:parent_branch`. Run from inside the sprint's worktree (`../worktrees/<sprint-name>`) — review-sprint operates on the worktree's checkout, not the main repo.

## Conventions

Throughout this skill:
- `<sprint>` — the sprint name (also the directory under `docs/sprints/` and the suffix on the `sprint/<sprint>` branch)
- `<parent>` — the value of `state.yaml:parent_branch` for this sprint
- `<diff-base>` — `$(git merge-base HEAD <parent>)`. Substitute literally in commands.

## Purpose

Catch issues that slip through when implementer is too close to the code:
- Dead code and scaffolding
- Test names that don't match test behavior
- Spec-implementation drift
- Uncovered error paths

## Context Loading

Two tiers. The first is enough for mechanical checks (dead code, lint,
coverage). The second is required for consistency checks (DRY, helper
duplication, spec-vs-codebase). Skipping tier 2 was the historical reason
review-sprint missed cross-cutting smells.

### Tier 1 — always load

1. `CLAUDE.md` — Principles (especially #7, #8) and the Anti-Patterns table
2. `docs/sprints/<sprint-name>/spec.md` — what was planned
3. Changed files this sprint (`git diff $(git merge-base HEAD <parent_branch>)..HEAD --name-only`, where `<parent_branch>` comes from `state.yaml`)
4. Test files for changed code

### Tier 2 — load before consistency checks (§2, §4, §7)

5. **Sibling source files in every subpackage of `src/fabulexa_forge/` the sprint touched.** Required
   for duplicate-helper detection: a new helper looks fine in the diff
   and identical to a pre-existing helper two files away.
6. **Existing test files for those subpackages.** Required for test-value
   audit: a new test file with 4 near-identical tests is only suspicious
   when you've seen how the rest of the suite expresses similar shapes.

Fresh eyes means deliberately ignorant of *intent* (why this design?), not
of *context* (what already exists?). Without tier 2, the "Consistency / DRY"
gate is unauditable.

## Review Process

### 1. Dead Code Scan

Scaffolding patterns (mechanical):

```bash
grep -rn "# Future:" src/
grep -rn "# TODO:" src/
grep -rn "pass$" src/
```

**Flag:** Any loop body that only contains `pass`, `continue`, or comments.

**Flag:** Any `__init__` that stores data not used by other methods.

Inert-rename patterns (judgment, requires reading the diff):

```bash
# Self-renames: assignment whose RHS is just another local name with no
# subsequent semantic divergence.
git diff <diff-base>..HEAD -- '*.py' | grep -E "^\+\s+(\w+)\s*=\s*(\w+)\s*$"
```

For every `+    foo = bar` line in the diff, ask: does `bar` get reassigned
later? Does `foo` carry a different type than `bar`? If neither, it is a
dead alias (e.g., `full_record_id = record_id` — the historical miss).

**Flag:** Inert assignments where the RHS is the only thing the LHS ever
holds and the rename adds no semantic distinction.

**Flag:** 1- or 2-element collection literals stored in module-level
constants (`_FOO = frozenset({1, 6})`) used in exactly one membership
test. Inline as a tuple literal.

Exported-but-uncalled symbols (Principle #8 end-state check):

The phase-level reviewer intentionally does NOT flag exported symbols
whose only would-be consumer is a later phase of the same sprint — that
is the expected mid-sprint state. At sprint level, the end state is
visible, and any symbol the sprint added that no production code calls
is dead.

```bash
# For each public symbol added by the sprint diff to src/ (not __init__.py
# re-exports), use find_references to check for production callers
# (anything outside tests/ and demos/).
git diff <diff-base>..HEAD -- 'src/**/*.py' | grep -E "^\+def [a-z]"
git diff <diff-base>..HEAD -- 'src/**/*.py' | grep -E "^\+class [A-Z]"
```

**Flag:** Any sprint-added public symbol whose only references are in
tests, demos, `__init__.py` re-exports, or other sprint-added symbols
that are themselves uncalled. The chain must terminate in a production
caller; otherwise the whole chain is dead.

### 2. Consistency / DRY Check

**Requires tier-2 context** (sibling source files in every touched subpackage of `src/fabulexa_forge/`).

For every new top-level definition the sprint added — function, class,
constant, type alias — ask whether something with the same purpose already
exists in the `src/fabulexa_forge/` tree.

```bash
# 1. List new top-level definitions added this sprint.
git diff <diff-base>..HEAD -- 'src/**/*.py' \
  | grep -E "^\+(def |class |[A-Z_][A-Z0-9_]+ = )" \
  | sed 's/^+//'

# 2. List new function names added this sprint.
git diff <diff-base>..HEAD -- 'src/**/*.py' \
  | grep -E "^\+def _?[a-z]" \
  | awk '{print $2}' | cut -d'(' -f1

# For each new function, locate sibling defs with cclsp
# (find_workspace_symbols / find_references) and compare bodies — not grep
# (Agent Instructions §6; a def-grep over *.py is hook-blocked in this repo).
```

For each new helper, read the bodies of pre-existing helpers in the same
module / sibling modules. If two function bodies differ by less than ~30%
of their tokens, flag the duplicate.

Anti-patterns from CLAUDE.md to apply here (not just to scan for
syntactically):

| Anti-pattern | What to check |
|---|---|
| One-use abstractions | Helper called from exactly one site → inline candidate |
| Premature helpers | New helper whose body matches a pre-existing helper → consolidate |
| Defensive copies | `list(x)` / `tuple(x)` / `dict(x)` over already-owned data |
| Reader indirection | Layers that don't add semantic value → call the reader directly |

**Flag:** Any new helper whose body is structurally identical (same call
order, same return shape) to a helper already present in the tree.
Loading the existing helpers is mandatory; a grep for the new name alone
will not surface the pre-existing duplicate.

**Flag:** Any helper called from exactly one site in the diff. Inline.

**Flag:** Cross-context helper reuse — a helper named for one shape (e.g.
`_encode_records_substrate_cell`) called from a writer for a different
shape (e.g. history). Works only by coincidence of argument values.

### 3. Test Name Audit

For each test file changed this sprint:

1. Read test function name
2. Read test docstring
3. Read test body
4. **Verify:** Does the test actually test what the name claims?

Common lies:
- `test_X_order` that doesn't verify ordering
- `test_X_deterministic` that only runs once
- `test_X_error` that doesn't verify the error type/message

### 4. Test Value Audit

Distinct from name-audit: the names may be honest, the bodies may run, and
coverage may report 100% — yet the tests carry no value or pin no behavior.
The Test Name Audit catches lies; this gate catches **multiplication and
weak assertions**.

**Test multiplication.** Group new tests by name prefix or shared fixture.
Inside each group, look for ≥3 tests whose bodies differ only in literal
values or YAML strings. These are parameterization candidates.

```bash
# List new test functions by file.
git diff <diff-base>..HEAD -- 'tests/**/test_*.py' \
  | grep -E "^\+def test_" | awk '{print $2}' | cut -d'(' -f1
```

For each test file, scan for repeated bodies. Concrete shapes to flag:

- Four tests asserting the same `==` against four near-identical inputs →
  one `@pytest.mark.parametrize` with four ids.
- Tests that differ only in a `True`/`False` toggle on the same field.
- Tests in the same file whose bodies are 90%+ overlap by line.

**Weak assertions on deterministic fixtures.** Scan diffed test bodies for
shapes that pass without pinning behavior:

```bash
git diff <diff-base>..HEAD -- 'tests/**/test_*.py' \
  | grep -E "^\+\s+assert (len\(.+\) > 0|.+ is not None|.+ != None|.+ == .+ == )"
```

Specifically flag:

| Shape | Why it's weak |
|---|---|
| `assert len(rows) > 0` | Pins existence, not row count, on a fixture with a known count |
| `assert x is not None` | Pins existence, not value, when the fixture knows the value |
| `assert ... == ... == ...` | Transitive None passes (`None == None == None`) |
| `assert isinstance(x, T)` only | Type without value content |

Each should be replaced with an exact-value assertion against the
fixture's known state.

**Flag:** Any test group of ≥3 with identical bodies modulo literals.

**Flag:** Any `len(x) > 0`-style assertion against a fixture whose row
count is constructible from the test setup.

### 5. Coverage Analysis

```bash
# Get coverage for new files only
uv run pytest --cov=src/fabulexa_forge --cov-report=term-missing

# Check files added this sprint
git diff --name-only --diff-filter=A <diff-base>...HEAD
```

**Flag:** Any new file with < 85% coverage.

**Flag:** Uncovered lines that are error conditions (even if "shouldn't happen").

### 6. Type-ignore Density Check

```bash
# Count type-ignore markers added this sprint, grouped by file.
git diff <diff-base>..HEAD -- 'tests/**/*.py' \
  | grep "^+" | grep -E "type:\s*ignore\[" \
  | wc -l

# Per-file detail.
git diff <diff-base>..HEAD --stat -- 'tests/**/*.py' \
  | awk '{print $1}' | grep '\.py$' \
  | xargs -I {} sh -c 'echo "=== {} ==="; grep -n "type: ignore" {} || true'
```

Each `# type: ignore` is a small acknowledgement that the type system is
fighting the test setup. One or two are normal; **many of the same shape**
across files means the test surface is missing a small helper.

**Heuristics:**

- > 1 `# type: ignore` per file added in the diff: review.
- ≥ 3 `# type: ignore[arg-type]` of the same shape across the diff
  (e.g. all on the same dataclass field): centralize via a test helper
  (e.g. `tests/_helpers.py::prov(*keys)`).
- `# type: ignore` on a test fixture's `frozenset(...)` /
  `cast(...)` / `dict(...)` literal: factor a typed builder.

**Flag:** Density above the heuristic. The remediation is *always* a small
test helper, not a typing change to the production type signature.

### 7. Spec-Implementation Comparison

This gate has **two directions**, not one. The historical version checked
only `spec → impl` (does the implementation match what the spec
prescribed?). That presumed the spec itself was right. A spec that
prescribes a duplicate of an existing helper still passes a one-way audit.
Add the reverse direction.

#### 7a. Load Sprint Notes (if available)

Read implementation decisions attached to phase commits:

```bash
# Find sprint commits and read their notes
for sha in $(git log --oneline --grep="Sprint" | head -10 | awk '{print $1}'); do
    echo "=== $sha ==="
    git notes --ref refs/notes/agent/sprint show "$sha" 2>/dev/null || echo "(no notes)"
done
```

If notes exist, use the `decisions` field to understand **why** the implementer made specific choices. This enables checking intent-vs-implementation, not just spec-vs-implementation.

#### 7b. Compare Contracts (spec → impl)

For each contract in spec.md:
1. Find the implementation
2. Compare signature (params, types, return)
3. Compare docstring (Args, Returns, Raises)
4. Note any improvements or divergences
5. If sprint notes recorded a decision about this contract, verify the stated rationale matches the code

**Document:** If implementation is better than spec, note it for spec update.

**Flag:** If implementation is worse than spec, it's a bug.

**Flag:** If a noted decision contradicts the spec without justification, it's a deviation.

#### 7c. Audit the Spec against the Codebase (impl → spec)

The reverse direction: catch cases where the spec itself prescribed
something the codebase already does, or contradicts established patterns.

For each contract the spec adds:

1. **New helper / function?** Read existing helpers in the same subpackage
   (tier-2 context). Does one already do this? If yes, the spec should
   have prescribed reuse, not a new helper. Flag as a *spec-time miss*
   even if the implementation faithfully built what the spec said.
2. **New constant / type?** Search the `src/fabulexa_forge/` tree for siblings. Is the new
   name redundant with an existing one (e.g. `_FOO_INT_OFFSETS` vs an
   inline tuple in the only call site)?
3. **New test fixture builder?** Does an existing test file already
   have one with the same shape?

**Flag:** Any contract whose existence the spec wouldn't have proposed
had the spec author read the rest of the codebase. The fix lives in the
spec process, not in the sprint code — but the review gate is the
moment to detect it.

**Note in `review.md`:** When this gate fires, the finding is "the
audit caught what the spec didn't" — calibrate the spec process, not
just this sprint.

### 8. Workspace Check

```bash
git status --porcelain
```

**Flag:** Any untracked files that aren't in .gitignore.

### 9. Lint & Typecheck

This repo has no pre-commit hook chain; the static green-bar is the `make` targets
(`make check` = lint + typecheck + tests). This gate runs the static portion:

```bash
make lint typecheck
```

**Flag:** Any failure from `ruff check`, `ruff format --check`, or `mypy`
(formatting, linting, type errors).

### 10. Demo Verification

Run all demo scripts twice to verify determinism and consistency:

```bash
# First run
for demo in docs/sprints/<sprint>/demos/phase_*.py; do
    uv run python "$demo"
done

# Second run
for demo in docs/sprints/<sprint>/demos/phase_*.py; do
    uv run python "$demo"
done
```

**Flag:** Any demo that fails or produces different output between runs.

## Output Format

### 1. Do NOT file `finding` notes

Record every gate result in `review.md` (below) only. `review.md` is the
complete, authoritative report — the downstream fix loop consumes it.

Do **not** auto-file `finding` notes via the `note` skill. Auto-filing
mirrors every style nit into the vault and buries the findings that
actually matter. A `finding` note is created **only** when the human
explicitly asks for one, and only for an item that genuinely cannot be
fixed in this sprint. Until then, findings live in `review.md` and nowhere
else.

### 2. Create review summary in `docs/sprints/<sprint>/review.md`

PASS/FAIL gating was the historical reason cross-cutting smells were
suppressed: a green grep returned PASS even when the reviewer had noticed
something. Replace with a three-level severity per gate **and** a mandatory
"Notes" sub-section, even on green gates.

```markdown
# Sprint Review: [Name]

**Date:** YYYY-MM-DD
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean / observations / blockers | N | one-line summary |
| 2. Consistency / DRY | … | N | … |
| 3. Test names | … | N | … |
| 4. Test value | … | N | … |
| 5. Coverage | … | N | … |
| 6. Type-ignore density | … | N | … |
| 7. Spec ↔ codebase | … | N | … |
| 8. Workspace | … | N | … |
| 9. Lint & typecheck | … | N | … |
| 10. Demos | … | N | … |

Severity values:
- **clean** — gate found nothing.
- **observations** — gate found smells worth recording but no blocker.
  *Always populate the Notes column when this is set; never leave it
  empty just because no issue was filed.*
- **blockers** — must fix before merge.

## Findings

For each gate that returned `observations` or `blockers`, include a
sub-section:

### Gate N: [Name]

- **finding 1**: brief description; pointer to file:line; severity
- **finding 2**: …

The "observations" tier exists specifically so the reviewer has a place
to write down what they noticed even when they can't justify blocking.
Empty observations means the reviewer didn't look hard enough.

## Recommendation

- **APPROVED** — no blockers, no observations.
- **APPROVED-WITH-NOTES** — no blockers; one or more observations recorded.
  **Mergeable.** Observations are surfaced for the user's per-item decision; the
  implement-sprint orchestrator does **not** auto-fix them and does not enter a
  fix loop for them. Do not prescribe a cleanup commit here — fix-vs-accept is
  the user's call at the ACCEPT/FIX checkpoint.
- **REVISIONS NEEDED** — one or more blockers; do not merge until fixed.

The mapping is mechanical: any blocker → REVISIONS NEEDED; else any observation
→ APPROVED-WITH-NOTES; else APPROVED. Observations never force REVISIONS NEEDED,
regardless of count. Per-finding severity (recorded in each Findings entry) is
what the orchestrator reads to feed **only blockers** into its fix loop.
```

## When to Use

Run review-sprint:
- After all phases of implement-sprint complete
- Before `/verify-sprint`
- When you suspect quality issues

## Review Pipeline

This command checks **mechanical** properties. Follow it with `/verify-sprint` for **behavioral** correctness.

```
implement-sprint (per phase)
    └── reviewer agent (quick check)

review-sprint (after all phases)
    └── mechanical audit: coverage, linting, dead code, test names

verify-sprint (after review-sprint passes)
    └── spec-fidelity audit: does code match spec's algorithms step-by-step?
```

`/review-sprint` catches sloppy code. `/verify-sprint` catches correct-looking code that does the wrong thing. Both must pass before merge.

## Agent Instructions

When invoked, use the **reviewer** agent with these specific instructions:

1. Load tier-1 context, then tier-2 before §2, §4, §7. Do NOT skip tier 2
   — duplicate-helper detection is unauditable without it.
2. Run all 10 gates systematically. Do not collapse a gate to "PASS"
   without populating the Notes column.
3. Document ALL findings in `review.md` — observations as well as
   blockers. Empty "observations" usually means the reviewer didn't look
   hard enough at that gate. Do NOT file `finding` notes (see Output
   Format §1) — `review.md` is the only output.
4. Be skeptical — assume issues exist until proven otherwise. Spec
   passing does not imply the spec was right (gate 7c).
5. Produce the review report using the new Severity / Findings / Notes
   schema. Do not use PASS/FAIL.
6. Code navigation is cclsp-first — follow `.claude/skills/worker-protocol.md`
   § Code Navigation (`find_definition` / `find_references` /
   `get_incoming_calls` / `get_outgoing_calls` to trace code, not Grep for
   definitions or call sites).

The reviewer should NOT:
- Assume the implementer was right
- Assume the spec was right (gate 7c is specifically for catching this)
- Skip checks because "it passed CI"
- Accept "it works" as proof of quality
- Skip tier-2 context loading "to stay fresh-eyed" — fresh on intent,
  not on existing code
- Grep for `def foo` or `class Bar` — use `find_definition` instead
- Grep a function name across directories to find callers — use
  `find_references` or `get_incoming_calls`
- Mark a gate `clean` without writing a one-sentence Note explaining
  what was checked — silence on a gate is a smell of its own
