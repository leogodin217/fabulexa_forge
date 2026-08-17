# Sprint Review: dataset-equivalence

**Date:** 2026-08-17
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)
**Diff base:** `82d2af42f1e8d65a5e9edd86e4c84db32c0fc755` (`merge-base HEAD prepare_for_classroom_env`)
**Diff head:** `b989e02` (Phase 3 commit)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | Scanned `compare/*.py` for `# TODO`/`# Future:`/bare `pass`; none found. Checked every sprint-added public symbol (`table_is_equal`, all `compare/` exports, `cmd_compare`/`_cmd_compare`) via `find_references`; every chain terminates in a production caller (engine.py, render.py, cli.py, or `VERBS`). |
| 2. Consistency / DRY | observations | 3 | `compare/inputs.py` reimplements two SQL string-utilities and one type-name set that already exist, byte-for-byte, in `src/fabulexa_forge/_sql.py`. |
| 3. Test names | clean | 0 | Read all new test names + bodies in `test_canonical.py`, `test_engine.py`, `test_inputs.py`, `test_render.py`, `test_cli_compare.py`. Determinism/ordering-named tests genuinely call the function twice or assert exact tuple order — none of the "runs once" / "doesn't verify order" lies. |
| 4. Test value | clean | 0 | No ≥3 near-identical bodies outside legitimate `@pytest.mark.parametrize` groups. `is not None` assertions on `rows`/`table_comparison.rows` are narrowing guards always followed by an exact-value assertion in the same test, not a substitute for one. |
| 5. Coverage | observations | 4 | All touched files ≥94% (five at 100%). New `cli.py` compare code is fully covered. A handful of non-trivial branches the docstrings advertise are untested (see Findings). |
| 6. Type-ignore density | observations | 1 | 4 `# type: ignore[misc]` in `tests/compare/test_canonical.py`, all one shape (frozen-dataclass mutation-rejection), above the "review at >1/file" and "centralize at ≥3 same-shape" heuristics. |
| 7. Spec ↔ codebase | observations | 1 | 7a: sprint notes reviewed, no undocumented deviations. 7b: every contract (signatures, docstrings, Raises, dataclass fields, error-message strings, CLI/exit-code contract) matches the spec verbatim — no divergence found. 7c: the spec's `inputs.py` contract should have directed reuse of `_sql.py`'s existing quoting helpers (ties to gate 2's finding). |
| 8. Workspace | clean | 0 | `git status --porcelain` is empty. |
| 9. Pre-commit | clean | 0 | `pre-commit run --all-files`: all 8 hooks passed (whitespace, EOF, yaml, toml, ruff lint, ruff format, mypy --strict, understand-bundles). |
| 10. Demos | observations | 1 | Phase 1 and Phase 2 demos: byte-identical output across two runs, exit 0 both times. Phase 3 demo: exit 0 both times but stdout differs between runs — see Findings. |

Severity values: **clean** — gate found nothing. **observations** — smells worth recording, no blocker (mergeable; user decides fix-vs-accept). **blockers** — must fix before merge.

## Findings

### Gate 2: Consistency / DRY

- **finding 1**: `compare/inputs.py:78-87` defines `quote_identifier(name)` = `'"' + name.replace('"', '""') + '"'`, byte-for-byte identical to `src/fabulexa_forge/_sql.py:12-26::quote_identifier`, whose own docstring states: *"The one identifier-quoting helper: every CREATE TABLE / SELECT / DESCRIBE splice of a name ... must go through this."* `find_references` confirms `_sql.py::quote_identifier` is already the single helper used by `reader/conformance.py`, `writers/duckdb.py`, `corrupters/engine.py`, `corrupters/base_writer.py`, and `corrupters/operations/schema_drift.py`. `compare/` is the one splice-site that does not go through it. Severity: observation (functionally correct and tested; violates the stated single-helper convention).
- **finding 2**: `compare/inputs.py:90-92::_sql_string_literal(text)` = `"'" + text.replace("'", "''") + "'"` is byte-for-byte identical to `_sql.py:29-38::_sql_literal(value)`. Same duplication as finding 1, same file. Severity: observation.
- **finding 3**: `compare/canonical.py:40-53::_INTEGER_TYPES` (the 10-member DuckDB integer-type-name frozenset) is an exact-value duplicate of `_sql.py:42-53::_INTEGER_TYPES`. (`_FLOAT_TYPES` differs — `_sql.py` additionally includes `"REAL"` — so only the integer set is a true duplicate.) Severity: observation.
- Context: `compare/` never imports from `_sql.py`, `reader/`, or `writers/` anywhere in the diff — consistent with the spec's deliberate "compare depends on nothing else in the package" isolation stance for domain/error-hierarchy coupling. That isolation principle is stated for domain error types (`CompareInputError` vs `ExporterError`/`ReaderError`) and the DuckDB session, not for generic string utilities, so it does not obviously extend to excusing this duplication — hence recording as an observation rather than dismissing it.

### Gate 5: Coverage

All new files clear the 85% bar (canonical.py/report.py/errors.py/`__init__.py` 100%, engine.py 99%, render.py 98%, inputs.py 94%; `cli.py`'s new `cmd_compare`/`_cmd_compare` code fully hit). The following specific branches are untested despite being non-trivial and, in one case, explicitly documented as handled:

- **finding 1**: `compare/inputs.py:225-233` (doubled-quote (`""`) escaping and mid-field character accumulation *inside* a quoted CSV field) and `inputs.py:246-260` (`\r`/CRLF handling; a final row with no trailing newline) are never exercised. `_tokenize_csv`'s docstring explicitly claims to "handle... doubled-quote escaping and embedded delimiters/newlines within quoted fields," but no test constructs a quoted field containing a comma, a newline, or an escaped `"`. Severity: observation.
- **finding 2**: `compare/inputs.py:384` — the interval cell's cast-failure fallback (`TRY_CAST` fails and the raw text is returned) is untested; only the writer-form-parses and TRY_CAST-succeeds paths are covered (`test_csv_interval_*`). The general failing-cast rule is tested for integer and blob but not interval. Severity: observation.
- **finding 3**: `compare/engine.py:354` — the CSV-actual-side, zero-compared-column degenerate path (`_materialize_actual_rows`, `columns` empty) is untested; only the DuckDB-actual equivalent (`_materialize_encoded_rows`, `engine.py:325`) is exercised, via `test_every_column_incompatible_degenerates_row_pass_to_count_check`. Severity: observation.
- **finding 4**: `compare/render.py:99` — the `expected_type=...` fragment of `_render_schema_discrepancy` is never rendered in any test; `test_render.py`'s fixtures only carry `column-extra` (actual_type only) and `table-missing` (neither) discrepancies, never a kind with `expected_type` set (e.g. `column-missing`). Severity: observation.

### Gate 6: Type-ignore density

- **finding 1**: `tests/compare/test_canonical.py:250,262,276,282` carry 4 `# type: ignore[misc]`, one per frozen-dataclass "assert `FrozenInstanceError`" test (`SchemaDiscrepancy`, `RowDiscrepancies`, `TableComparison`, `ComparisonResult`). All four are the identical shape: assign to a frozen field, silence the resulting mypy error, assert the runtime exception. No prior pattern for this shape exists elsewhere in the repo (`find_workspace_symbols` for `FrozenInstanceError` returns nothing outside this file), so it isn't inconsistent with an established convention, but it clears this repo's own heuristic (`>1`/file; `≥3` same-shape) for "centralize via a small test helper" (e.g. `tests/compare/_helpers.py::assert_frozen_field(instance, name, value)`). Severity: observation.

### Gate 7: Spec ↔ codebase

- **finding 1 (7c)**: The spec's Phase 2 file table prescribes `compare/inputs.py` as new "input validation + loading (UTC-pinned session, CSV typing)" without directing reuse of `_sql.py`'s pre-existing `quote_identifier` / `_sql_literal`. Per the spec-audit direction (impl → spec), had the spec author read `_sql.py` (whose docstring names itself the package's single identifier-quoting helper), the contract should have prescribed reuse rather than a fresh module-local copy. This is the spec-time counterpart of gate 2's findings 1-2 — the implementer built exactly what the spec asked for; the miss traces to the spec, not the implementation. Severity: observation (calibrate the spec process, per the gate's own guidance).
- 7a: sprint notes (`git notes --ref refs/notes/agent/sprint`) read for all three phase commits (`e26cee8`, `36b187d`, `b989e02`). All `decisions` entries are consistent with the code as written (e.g. Phase 3's stated rationale for extracting `table_is_equal` into `report.py` matches its two production call sites in `engine.py` and `render.py`; Phase 2's stated CSV-NULL-vs-empty-string and interval-parsing rationale matches `inputs.py`'s implementation). No contradicted or unjustified decision found.
- 7b: every contract in spec.md § Contracts was compared field-by-field / line-by-line against the implementation: `CanonicalFamily`, `family_of`, `encode_value` (canonical.py); the four report dataclasses (report.py); `CompareInputError` (errors.py, docstring near-verbatim); `compare_datasets` signature, docstring Args/Returns/Raises (engine.py); `render_comparison_text`/`render_comparison_json` (render.py); the CLI's argument surface, `--format` default, exit codes, and `VERBS` registration (cli.py); and all seven business-rule error message templates (verified verbatim against `inputs.py`/`engine.py`). No divergence found in either direction (no contract weaker than spec'd, no unjustified deviation).

### Gate 10: Demo verification

- **finding 1**: `docs/sprints/dataset-equivalence/demos/phase_3_cli_compare.py` produced different stdout between its two runs (`diff run1 run2`):
  ```
  16c16
  <   stderr: ERROR: expected side must be a DuckDB file: /tmp/tmprei4wqo8/does-not-exist.duckdb
  ---
  >   stderr: ERROR: expected side must be a DuckDB file: /tmp/tmploq2r5f6/does-not-exist.duckdb
  ```
  The only difference is the randomly-generated `tempfile.TemporaryDirectory()` path the demo embeds into its own printed diagnostic line — the compared behavior itself (exit code 2, error type, message template) is identical both runs. This is demo-harness nondeterminism, not a defect in `compare_datasets` or the CLI (which the engine/render/CLI test suites independently pin as deterministic). Severity: observation, recorded per the gate's literal "any demo producing different output between runs" rule. Phase 1 and Phase 2 demos were byte-identical across both runs.

## Recommendation

**APPROVED-WITH-NOTES**

No blockers. Nine observations recorded above (3 DRY/consistency, 4 coverage-gap, 1 type-ignore-density, 1 spec-process, 1 demo-nondeterminism) — all mergeable; fix-vs-accept is the user's call at the ACCEPT/FIX checkpoint. Contract compliance (gate 7b) is exact in both directions; workspace and pre-commit are clean.
