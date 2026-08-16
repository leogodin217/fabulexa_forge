# Sprint Review: temporal-elections

**Date:** 2026-08-16
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# TODO`/`# Future:` markers; every sprint-added public symbol (`render_anchor_temporal_expr`, `render_date_parse_expr`, `scd_window_bound`/`scd_window_render`, `build_date_parse_expr`, `check_temporal_render_requires_anchor`, `check_date_parse_source_column`, `pin_session_timezone`, the three new error classes, `ScdWindowSpec`/`DateParseSpec`/`BaseRenderDecl`) traced via `find_references`/`find_definition` to a production caller. No inert self-renames in the diff. |
| 2. Consistency / DRY | observations | 2 | See below — a duplicate `TemporalRender` literal-type definition, and a repeated `.as_ or "timestamp"` inline idiom that could share `scd_window_render`'s helper pattern. |
| 3. Test names | clean | 0 | Sampled test names against bodies across `test_columns.py`, `test_scd.py`, `test_validation.py`, `test_source_decls.py`, `test_csv.py`, `test_sql.py` — names accurately describe assertions (e.g. `*_negative` tests check sign preservation, `*_unchanged` tests pin byte-identical regression forms). |
| 4. Test value | clean | 0 | New tests use exact-value assertions (`assert row[0] == date(1990, 5, 14)`, `assert _rows(...) == [[...]]`) rather than existence-only checks. `is not None` occurrences are narrowing asserts before an exact-value assertion on the next line, never the terminal assertion. No ≥3-test near-duplicate groups found; per-election test groups (four elections × several surfaces) each assert a distinct type/value, not a parametrization candidate in disguise. |
| 5. Coverage | clean | 0 | `pytest --cov=src/fabulexa_forge`: 97% total; every touched file ≥ 93% (`dimensional/columns.py` 93%, `dimensional/validation.py` 87%, `anchor.py`/`_sql.py`/`errors.py`/`config/models.py` 99-100%). No new file dropped below the 85% floor (no wholly new `src/` files this sprint — only demos and one new test file were added). |
| 6. Type-ignore density | clean | 0 | 1 `# type: ignore` added across the entire test diff — well under the heuristic. |
| 7. Spec ↔ codebase | observations | 1 | 7b (spec→impl): every contract (`render_anchor_temporal_expr`, `render_date_parse_expr`, `pin_session_timezone`, CSV text forms, business rules) matches signature/docstring/Raises exactly; sprint-notes decisions match the code. 7c (impl→spec): the spec itself specifies `TemporalRender` living in `config/models.py` (§ Config models) without acknowledging Phase 1 already placed an identically-named, identically-documented alias in `anchor.py` — a spec-process miss, not an implementation bug (see finding below). |
| 8. Workspace | clean | 0 | `git status --porcelain` empty. |
| 9. Pre-commit | clean | 0 | `pre-commit run --files <49 changed files>` — all hooks passed (ruff, ruff format, mypy --strict, trim-whitespace, end-of-file, yaml). |
| 10. Demos | clean | 0 | All 6 phase demos (`phase_1`..`phase_6`) ran twice, exit 0 both times, byte-identical stdout across runs — confirms determinism and the machine-independence claim (phase 2) and family-identity claims (phase 1, 4). |

Severity mapping: 2 gates carry observations (Gate 2, Gate 7); no blockers. → **APPROVED-WITH-NOTES**.

## Findings

### Gate 2: Consistency / DRY

- **finding 1** (observation): `TemporalRender = Literal["timestamp", "date", "time", "timestamptz"]` is defined twice with an identical docstring — `src/fabulexa_forge/anchor.py:29` and `src/fabulexa_forge/config/models.py:96`. `anchor.py` imports `config.models` only under `TYPE_CHECKING` (no runtime cycle), so `config/models.py` could import the alias from `anchor.py` at runtime instead of redefining it. Sprint notes (Phase 1) record this as a deliberate choice ("TemporalRender literal type lives in anchor.py (the renderer's home); Phase 3 introduces the config surface separately") but the two definitions can silently diverge if only one is ever edited — no test pins that they stay identical. Not a config-boundary issue (both are fixed vocabularies, not author-configurable values), so an observation rather than a blocker.

- **finding 2** (observation): the idiom `ts.as_ or "timestamp"` (the mode-definitional default for an absent `TimestampSpec.as_`) is repeated verbatim at `src/fabulexa_forge/exporters/dimensional/columns.py:147`, `columns.py:271`, and `src/fabulexa_forge/exporters/dimensional/validation.py:1186`. The sprint already factored the equivalent `scd_window` default into shared helpers (`scd_window_bound`/`scd_window_render` in `config/models.py`), but did not add an analogous `timestamp_render(spec: TimestampSpec) -> TemporalRender` helper for the parallel `TimestampSpec.as_` case, leaving three independent inline copies of the same absence-detection default. Not a config-boundary violation — this is the documented, intentional mode-definitional default, correctly implemented at each site — but it is the kind of pre-existing-pattern-not-reused case Gate 2 exists to surface.

### Gate 7: Spec ↔ codebase

- **finding 1** (observation, 7c): spec.md § Config models (line 89-90) specifies `TemporalRender` as living in `config/models.py`, but Phase 1's contract (spec.md § Phase 1, delivered before Phase 3) already required the same alias inside `anchor.py` for `render_anchor_temporal_expr`'s `render: TemporalRender` parameter. The spec never reconciled the two phases into one canonical location, so the implementer (correctly, given the spec text) built both. This is the same duplication as Gate 2 finding 1, filed here as a spec-process observation per the gate's own convention ("the audit caught what the spec didn't").

## Recommendation

**APPROVED-WITH-NOTES**

**Resolution (post-review, user-directed FIX):** all three observations were
addressed in the review-cleanup commit — `TemporalRender` now has a single
canonical definition in `anchor.py` (imported by `config/models.py`), and the
`.as_ or "timestamp"` idiom is factored into a shared
`timestamp_render(spec)` helper used at all three sites. Full suite green and
pre-commit clean after the fix.

No blockers: all ten gates are clean or observation-only. Zero of the 138 new/changed tests are weak or duplicated; all 4594 tests pass; coverage, pre-commit, dead-code, and demo-determinism gates are all clean. The two observations (a duplicate `TemporalRender` alias and a duplicated `.as_ or "timestamp"` idiom, both traceable to a spec-process gap rather than an implementation defect) are surfaced for the user's own accept-or-fix call; neither breaks a config-boundary guarantee, a contract, or a test.
