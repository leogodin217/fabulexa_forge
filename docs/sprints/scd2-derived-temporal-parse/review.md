# Sprint Review: scd2-derived-temporal-parse

**Date:** 2026-08-18
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Prior-Round Verification

The prior review round recorded 0 blockers and 5 observations; commit `bbb1bce`
("review cleanup") claimed to fix all five. Independently re-verified against
the current diff and, for claim 5, against an actual constructed export (not
just the new test):

1. **DRY dup** (`scd.py`'s `_resolve_source_column_type` vs. `columns.py`'s
   inline scan) — **RESOLVED**. `resolve_source_column_type` now lives once in
   `columns.py`, carrying the `TableNotFoundError`→`ExportError` guard; the
   unguarded `scd.py` duplicate is deleted; both of `scd.py`'s call sites
   (tracked-column cast, value_map) and `columns.py`'s own value_map branch
   share it (`find_references` confirms 4 non-test call sites).
2. **Test multiplication** (3 near-identical denotation-type tests in
   `test_sql.py`) — **RESOLVED**. Collapsed into one
   `test_render_date_parse_expr_denotation_type` with
   `@pytest.mark.parametrize` over 3 ids; exact-value assertions preserved.
3. **Malformed-`%` branch uncovered** (`_sql.py:301`) — **RESOLVED**. New
   `test_date_parse_spec_malformed_percent_directive_raises` uses
   `"%Y-%m-%d %"` (a trailing bare `%` with no directive char after it);
   traced the `_date_parse_directives` logic by hand — `fmt.count("%")` (4)
   != `accounted_percents` (3) — confirms this format hits exactly the line
   301 raise.
4. **Directive-less catch-all uncovered** (`_sql.py:402`) — **RESOLVED**. New
   `test_date_parse_spec_no_temporal_directive_raises` uses `"%%"` (a literal
   percent, no date/time field); traced `_date_parse_completeness(["%%"])` →
   `(False, False)`, `has_any_date_field` is False so the partial-date branch
   is skipped, landing on the line 402 "must denote a complete date" raise —
   confirmed reachable and confirmed the test's `match=` string is the exact
   text that branch raises.
5. **Column-absent→VARCHAR fallback uncovered** (`resolve_source_column_type`)
   — **RESOLVED, and the reachability claim independently verified true, not
   just asserted.** The new unit test mocks the sidecar directly (reasonable
   for a pure function), but its docstring claims a *production* path also
   hits this branch: a `value_map` column on a `history_interval` grain
   sourcing the virtual `lead_sim_time` column. Built an actual emit +
   `DimensionalConfig` with exactly that shape (history_interval grain,
   `derived.value_map.from = "lead_sim_time"`) end-to-end through
   `build_query_specs` — `check_projection_column_exists` passes (the grain's
   virtual surface includes `lead_sim_time` per
   `validation.py:126-149,209`), `resolve_source_column_type` falls back to
   `"VARCHAR"` (visible in the rendered SQL: `CAST('early' AS VARCHAR)`
   literals, not `CAST('early' AS BIGINT)`), and the query executes and
   returns real rows. The claim is genuine, not just plausible-sounding.

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# TODO`/`# Future:` added; the two added `pass` bodies are both demos' `_discard_notice` no-op notice sink (legitimate demo pattern, matches the test suite's `discard_notice_sink`); no self-rename aliases in the diff; every new production symbol (`validate_date_parse_format`, `date_parse_denoted_type`, `resolve_source_column_type`, `check_scd2_derived_source_untracked`) traced via `find_references` to a production caller, not just tests/demos. |
| 2. Consistency / DRY | observations | 1 | `build_scd2_column_expr_flag`'s `date_parse` branch (`scd.py:94-99`) reimplements `build_date_parse_expr`'s 2-line body (construct `qualified_source`, call `render_date_parse_expr`) inline instead of calling `build_date_parse_expr`, unlike its sibling `timestamp` and `value_map` branches, which do call the shared builders (`build_timestamp_expr`, `build_value_map_expr`) directly — see Findings. |
| 3. Test names | clean | 0 | Spot-checked new/changed test names against bodies across `test_sql.py`, `test_scd.py`, `test_validation.py`, `test_models.py`, `test_columns.py` — every name's claim (denotation, refusal rule, expression identity, constancy across versions, reachability) matches what the body asserts. |
| 4. Test value | clean | 0 | No test group of ≥3 with bodies differing only in literals remains un-parametrized (the one prior-round multiplication case is fixed); no `len()>0` / bare-`is not None` weak assertions in sprint-added test bodies outside legitimate DB-cursor / demo-precondition guards (`row is not None` before indexing, `anchor is not None` demo precondition). |
| 5. Coverage | clean | 0 | Re-ran coverage on all 5 touched source files. `_sql.py` 100%, `scd.py` 100% (both prior-round gaps now closed). `config/models.py` 99% (16 missing lines, all outside this sprint's touched ranges — `DateParseSpec`/`_require_date_parse_map_valid` fully covered). `exporters/dimensional/columns.py` 95% (9 missing lines, all in the pre-existing, untouched `_build_elapsed_column` — `resolve_source_column_type` and its call sites fully covered). `exporters/dimensional/validation.py` 89% (55 missing lines, all outside the sprint's touched hunks — `check_scd2_derived_source_untracked` and the amended `check_scd2_column_mode_supported` fully covered). |
| 6. Type-ignore density | clean | 0 | Zero `# type: ignore` added anywhere in the sprint diff (`tests/` or `src/`). |
| 7. Spec ↔ codebase | observations | 1 | Signatures/docstrings/Raises for all seven spec-listed contracts (`validate_date_parse_format`, `date_parse_denoted_type`, `render_date_parse_expr`, `DateParseSpec.format_denotes_a_temporal`, `build_scd2_column_expr_flag`, `check_scd2_column_mode_supported`, `check_scd2_derived_source_untracked`) match the spec verbatim; both amended error-message templates match the spec's table exactly. One divergence: the spec's prose for `build_scd2_column_expr_flag` says the derived compilation goes through "`build_timestamp_expr` / `build_date_parse_expr` / `build_value_map_expr`"; the implementation's own docstring already self-corrects this to "`build_timestamp_expr` / `render_date_parse_expr` / `build_value_map_expr`" — the code calls the lower-level renderer directly for `date_parse`, not the records-grain wrapper the spec named (see Gate 2; same root cause). 7c: the `sidecar.columns()` linear-scan idiom is the package's pre-existing pattern (used elsewhere in `validation.py` before this sprint), so the spec proposing it isn't a fresh miss. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty. |
| 9. Pre-commit | clean | 0 | `pre-commit run --files <all 18 sprint-changed files>` (trim-whitespace, end-of-file, check-yaml, ruff, ruff-format, mypy --strict, understand-bundles) — all Passed. Also independently ran `mypy --strict src/` standalone: "Success: no issues found in 127 source files." |
| 10. Demos | clean | 0 | Both `phase_1_parse_family.py` and `phase_2_scd2_derived.py` run twice each, exit 0, byte-identical output both runs (`diff` empty). Printed output matches the spec's demo narrative exactly: Phase 1 shows TIMESTAMP/TIME/DATE denotations, the spec-form parse expression, the four pairing/uniqueness/completeness refusals with the correct rule text, and the loud mismatch error naming table+column+value; Phase 2 shows per-version-constant derived columns beside a per-version `tier`, the `Scd2DerivedSourceUntracked` refusal, and the amended `Scd2ColumnModeSupported` refusal naming all three admitted modes. |

Full suite: `4821 passed, 18 skipped` (re-ran during coverage collection).

## Findings

### Gate 2: Consistency / DRY

- **finding 1** [`src/fabulexa_forge/exporters/dimensional/scd.py:94-99`, `src/fabulexa_forge/exporters/dimensional/columns.py:405-431`] — `build_scd2_column_expr_flag`'s `date_parse` branch builds `qualified_source = f'"{records_alias}"."{dp.from_}"'` and calls `render_date_parse_expr(...)` directly — the same two-line body `build_date_parse_expr` (in `columns.py`) already wraps. The `timestamp` and `value_map` derived branches, by contrast, call the real shared builders (`build_timestamp_expr(col_decl, anchor, records_alias)`, `build_value_map_expr(...)`) rather than re-implementing them. Root cause: `build_date_parse_expr(col_decl, table_decl: "TableDecl", grain_alias)` takes a full `TableDecl` (only to read `.name` for the renderer's error message), while `build_scd2_column_expr_flag` only carries `table_label: str` (per its own spec-pinned signature) — so it cannot call `build_date_parse_expr` without either threading a `TableDecl` through or the reverse edit (changing `build_date_parse_expr` to accept `table_label: str`, matching `build_timestamp_expr`'s and `build_value_map_expr`'s grain-alias-only style). Severity: **observation** — behaviorally proven identical by `test_derived_date_parse_expression_identical_to_records_grain_builder`'s direct string-equality assertion (`scd2_expr == records_expr`, calling `build_date_parse_expr(..., grain_alias="_records")`), so there is no correctness gap; this is a missed opportunity to close the loop the other two derived modes already close, and the asymmetry is what should be fixed, not the correctness. Fix (if taken up): change `build_date_parse_expr`'s second parameter from `table_decl: "TableDecl"` to `table_label: str` (mirroring the other two builders), then have `build_scd2_column_expr_flag`'s date_parse branch call it directly.

### Gate 7: Spec ↔ codebase

- **finding 1** [`src/fabulexa_forge/exporters/dimensional/scd.py:52-62` docstring vs. `docs/sprints/scd2-derived-temporal-parse/spec.md:219-229`] — Same root cause as Gate 2 finding 1: the spec's contract docstring for `build_scd2_column_expr_flag` names `build_date_parse_expr` as one of "the same per-column builders the records grain uses"; the shipped docstring already renamed this to `render_date_parse_expr` to match what the code actually calls. This is the implementer correctly documenting the code as written rather than the spec as written — an honest docstring, not a hidden divergence — but it means the spec's own contract text is now stale relative to the implementation. Severity: **observation** — no behavior or test-coverage gap (see Gate 2); flagged so the spec text is corrected (or the code is changed to match the spec's original wording) in a follow-up, not as a merge blocker.

## Recommendation

**APPROVED-WITH-NOTES**

No blockers. All five prior-round observations independently verified as
genuinely resolved (including the reachability claim behind fix #5, confirmed
via an actual constructed export, not just the new test's mock). Two new
observations recorded this round (one DRY asymmetry, one stale spec-doc
consequence of the same root cause) — neither touches the config boundary,
contract compliance, or correctness; both are mergeable as-is per the user's
own accept/fix call.
