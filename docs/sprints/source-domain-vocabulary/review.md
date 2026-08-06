# Sprint Review: source-domain-vocabulary

**Date:** 2026-08-06
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)
**Diff base:** `06b441b8cf9b7342a0cdf0df024444f020d8ee38` (main tip; per instructions,
not `git merge-base HEAD keys_and_modes_qa`)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | Checked `# TODO`/`# Future:`/bare `pass` in touched src, inert self-renames, and every sprint-added public symbol (`build_kind_label_expr`, `SourceKindLabelUnknown`, `SourceKindLabelCollision`, `SourceItemTypeCollision`) via `find_references` — all have production callers, none dead. |
| 2. Consistency / DRY | observations | 1 | New per-branch helpers (`_apply_records_property_rename`/`_apply_membership_field_rename`, `_membership_changes_keys`) each have exactly one call site, but this mirrors the file's pre-existing convention (`_resolve_records_change_edges`/`_resolve_membership_change_edges` are likewise single-call-site) — not flagged as a violation, but noted. `_sql_literal` and `_require_rename_map_valid` correctly reused rather than reinvented. |
| 3. Test names | clean | 0 | Sampled `test_events_render.py`, `test_plan.py` new classes/functions against bodies — names accurately describe assertions (e.g. `test_aliased_split_orders_by_resolved_names_not_natural_kind` genuinely asserts an order-index comparison tied to resolved names). |
| 4. Test value | clean | 0 | New tests use exact-value assertions (`_changes(row) == {...}`, tuple equality on resolved plans) rather than `len()>0`/`is not None`; no ≥3-test near-duplicate groups found in the diff. |
| 5. Coverage | clean | 0 | `events.py` 99%, `plan.py` 94%, `errors.py` 100%, `columns.py` 91% (one pre-existing uncovered line, not sprint-added); sprint-added lines in `config/models.py` (the new helper + validator) fall outside that file's missing-line ranges — covered. |
| 6. Type-ignore density | clean | 0 | Zero `# type: ignore` added anywhere in the diff. |
| 7. Spec ↔ codebase | clean | 1 (minor) | 7a: all 4 phase-commit notes show `review_cycles: 0`, corroborating a clean per-phase implementation. 7b: every contract (config fields, error classes, `build_kind_label_expr`, plan-type deltas, function-behavior deltas) matches the spec/design-doc verbatim, including exact error message strings, test-pinned. 7c: no spec-prescribed duplicate helper found — `_require_rename_map_valid`/`_sql_literal` reuse was itself a documented decision. Minor: `SourceKindLabelCollision`'s f-string uses `label` in both `{label}`/`{kind}` slots (`plan.py:_resolve_kind_labels`) — functionally correct (kind==label string in that branch) and test-pinned, but reads as a copy/paste artifact. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty; all sprint deliverables are committed. |
| 9. Pre-commit | clean | 0 | `pre-commit run --all-files` — all 8 hooks passed (ruff, ruff-format, mypy strict, understand bundles, etc.), no auto-fixes applied. |
| 10. Demos | clean | 0 | All 4 `docs/sprints/source-domain-vocabulary/demos/phase_*.py` ran twice each, exit 0, byte-identical output both runs. |

## Findings

### Gate 2: Consistency / DRY

- **observation 1**: `src/fabulexa_forge/exporters/source/plan.py` — the new
  `_apply_records_property_rename` (called once, `plan.py:1767`),
  `_apply_membership_field_rename` (called once, `plan.py:1806`), and
  `_membership_changes_keys` (called once, `plan.py:1809`) are each
  single-call-site helpers, which the review checklist's "Premature helpers"
  row would ordinarily flag. Tier-2 comparison shows this mirrors an
  established convention already in the file — `_resolve_records_change_edges`
  / `_resolve_membership_change_edges` (pre-existing, verified via
  `find_references`, also single-call-site) split the records/membership
  branches of `_build_event_source_plan` into named, independently
  documented/testable units. Not a violation; recorded for visibility only.

### Gate 7: Spec ↔ codebase

- **observation 1** (7c, minor): `plan.py::_resolve_kind_labels`'s collision
  branch —
  `raise SourceKindLabelCollision(f"kind_labels: label '{label}' collides with kind '{label}'")`
  — uses the loop variable `label` for both interpolations instead of a
  named `kind` variable, even though the design doc's message template is
  `"... collides with kind '{kind}'"`. This is not a bug: in this branch
  `label` and the colliding kind's name are the same string by construction,
  and `tests/exporters/source/test_plan.py::test_kind_labels_label_equals_unlabeled_kind_name_raises`
  pins the exact (correct) output. Purely a readability nit.

## Recommendation

**APPROVED-WITH-NOTES** — no blockers; two observations recorded (both minor,
neither affects correctness, config-boundary compliance, or contract
fidelity). Fix-vs-accept is the user's call.
