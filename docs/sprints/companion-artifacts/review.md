# Sprint Review: companion-artifacts

**Date:** 2026-08-23
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | Scanned for `# Future:`/`# TODO`/bare `pass`; the one `pass` (readme.py's importlib.resources fallback) is a legitimate try/fallback, mirroring `reader/_schema.py`. All new public symbols (`TableReport`, `ExportReport`, `WrittenRelation`, `describe_arrow_columns`, `describe_arrow_table`, `ReadmeOverlay`, `load_readme_overlay`, `validate_overlay_tables`, `WindowedArtifactState`, `write_companion_artifacts`, `is_companion_artifact_name`, `compute_sidecar_sha256`, `anchor_to_json`, `_require_nonblank_str`, manifest/readme helpers) traced via `find_references`/`find_workspace_symbols` to a production caller. |
| 2. Consistency / DRY | observations | 1 | See Findings. `compute_sidecar_sha256`/`anchor_to_json` promotions are correct DRY moves (fingerprint.py's private duplicates removed); `corrupters/engine.py::_sidecar_sha256` is a separate, out-of-scope pre-existing duplicate the sprint did not touch. |
| 3. Test names | clean | 0 | Read every new/changed test file's name, docstring, and body in `tests/exporters/companion/`, `tests/config/test_readme_overlay.py`, `tests/test_cli_readme_overlay.py`, `tests/incremental/test_companion_artifacts.py`, and the migration diffs in `tests/incremental/test_driver.py`. Names match bodies; `test_..._deterministic`/`..._byte_identical` tests genuinely render twice and compare. |
| 4. Test value | observations | 1 | See Findings — three near-identical one-liner tests in `test_fingerprint.py`. Otherwise exact-value assertions throughout (manifest/readme tests pin literal fields; migrated `test_driver.py` row-count assertions were rebuilt via direct SQL `COUNT(*)` queries rather than dropped, since `TableReport.row_count` is `None` on windowed reports by design). |
| 5. Coverage | observations | 1 | Full sprint coverage run (`pytest --cov` over every touched src dir): 5180 passed, 18 skipped, 97% aggregate; `companion/artifacts.py`, `manifest.py`, `overlay.py`, `query_spec.py`, `writers/relation.py` all 100%. `companion/readme.py` 89% (lines 54-63, the packaging-defect fallback branch of `_load_mode_template`) — see Findings. `errors.py`, `cli.py`, `anchor.py`, `reader/emit.py` are 100%/99% once measured via dotted module paths (the first `--cov=src/...` invocation warned "never imported" for these single-file-per-path targets — a `--cov` invocation quirk, not a real gap). |
| 6. Type-ignore density | clean | 0 | Zero new `# type: ignore` markers added to any test file this sprint. |
| 7. Spec ↔ codebase | observations | 1 | 7a: sprint notes for all 4 phase commits read; every stated decision (helper promotions, artifact-write timing split between `export_window`/`export_incremental_next`, `_build_windowed_report` sharing) matches the diff, `review_cycles: 0` throughout, no undisclosed deviations. 7b: every contract in spec.md (`ReadmeOverlay`, `load_readme_overlay`, `validate_overlay_tables`, `TableReport`/`ExportReport`, `WrittenRelation`, `describe_arrow_columns`/`describe_arrow_table`, `write_companion_artifacts`, `is_companion_artifact_name`, `WindowedArtifactState`, all four changed engine/driver signatures, `compute_fingerprint`'s exclusion, the census exclusion) verified present with matching signature, docstring Raises, and behavior. 7c: see Findings for one CLI stdout behavior change implied-but-not-stated by the spec. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty — no untracked files. |
| 9. Pre-commit | clean | 0 | `pre-commit run --files <60 sprint-changed files>` — trim-trailing-whitespace, end-of-file-fixer, check-yaml, ruff (legacy alias), ruff format, mypy (strict, src), understand-bundles(--check) all Passed. |
| 10. Demos | observations | 1 | All four demos run twice, exit 0 both times. Phases 2-4 byte-identical across runs. Phase 1's two runs differ only in embedded `tempfile.TemporaryDirectory()` absolute paths inside printed exception messages — an incidental demo-hygiene issue, not a determinism defect in the parser itself (see Findings). |

Severity values:
- **clean** — gate found nothing.
- **observations** — gate found smells worth recording but no blocker.
- **blockers** — must fix before merge.

## Findings

### Gate 2: Consistency / DRY

- **finding 1** (observation): `src/fabulexa_forge/exporters/companion/readme.py:32-63` (`_load_mode_template`) reimplements the exact two-path `importlib.resources` → `__file__`-relative-fallback → `FileNotFoundError` pattern already established in `src/fabulexa_forge/reader/_schema.py:53-82` (`_read_schema_text`), same call order, same caught exception types, same "packaging defect" message shape. Two occurrences of an identical shape is below the ≥3 threshold the skill's DRY heuristic uses for hard duplication, and the two call sites resolve resources from different roots (package-relative template vs. repo-root `contract/`), so a shared helper isn't a slam dunk — but it is a real candidate a spec author could have proposed (gate 7c overlap). Not a blocker; worth a shared `_load_packaged_text(package, relative_path, fallback_path)` helper if a third call site appears.

### Gate 4: Test value

- **finding 1** (observation): `tests/incremental/test_fingerprint.py` — `test_fingerprint_unaffected_by_readme_overlay_added`, `..._changed`, `..._removed` (new in this sprint) are three tests whose bodies are identical modulo the two `readme_overlay` literal values passed to `_config(...)`. This is the ≥3-near-identical-body shape the skill flags as a parametrization candidate (`@pytest.mark.parametrize` with ids `added`/`changed`/`removed`). Low-harm — each is a one-line assertion with a clear, distinct docstring — but textbook test multiplication.

### Gate 5: Coverage

- **finding 1** (observation): `src/fabulexa_forge/exporters/companion/readme.py:54-63` — the `_load_mode_template` packaging-defect fallback path (both the `except (FileNotFoundError, TypeError): pass` fallthrough and the final `raise FileNotFoundError(...)` when the fallback path also doesn't exist) is uncovered (89% file coverage, above the 85% floor but the miss is exactly an error-condition branch). The sibling `reader/_schema.py::_read_schema_text` implements the identical two-path pattern and achieves 100% coverage in `tests/reader/test_schema_loader.py` by monkeypatching `importlib.resources.files` to force the fallback and by monkeypatching `Path` to force the terminal raise — an established, directly-reusable testing convention that `tests/exporters/companion/test_readme.py` does not apply to `_load_mode_template`.

### Gate 7: Spec ↔ codebase

- **finding 1** (observation, 7c-adjacent): `TableReport.row_count` is `None` on windowed invocations (spec-mandated, design decision explicitly documented). A direct, spec-driven consequence is that the CLI's windowed stdout print changed from `f"  [{label}] {name}: {count} rows"` (pre-sprint, real per-table counts) to `f"  [{label}] {name}"` (no counts at all) in `src/fabulexa_forge/cli.py` (`_print_windowed_report`). This is a real, user-visible stdout regression for every `--next`/`--from`/`--to` invocation. The spec's Success Criteria and Breaking Changes table both pin stdout-unchanged only for *full* exports (`tests/test_cli_readme_overlay.py::test_readme_overlay_present_does_not_change_stdout_row_counts` only exercises the full-export path); no test or spec line anywhere states or pins the windowed stdout format change, and no existing test broke from it (the one existing assertion, `tests/test_cli_export.py::test_next_drip_duckdb_to_drained`, only checks for the `"[w"` label prefix, not row counts). Not a bug relative to the letter of the spec's contracts, but a user-facing behavior change the spec doesn't call out — worth a one-line addition to a future spec's Breaking Changes table.

### Gate 10: Demos

- **finding 1** (observation): `docs/sprints/companion-artifacts/demos/phase_1_overlay.py` printed output differs between two runs only in the absolute `tempfile.TemporaryDirectory()` path embedded in printed `ReadmeOverlayInvalid`/duplicate-slot exception messages (e.g. `/tmp/tmpv10erynz/...` vs `/tmp/tmp2lk8zeny/...`). The underlying parser behavior is fully deterministic (grammar rejections, byte-identical artifact renders elsewhere in phases 2-4 all matched exactly); this is solely an artifact of the demo printing absolute exception-message paths rather than a repo-root-relative or basename-only path. No production code is affected.

## Recommendation

**APPROVED-WITH-NOTES**

No blockers. Five observations recorded across gates 2, 4, 5, 7, and 10, none of which affect correctness, determinism, the config boundary, or the reader-first/faithful-reshaping principles. Implementation is a faithful, thorough realization of the spec: all four phases' contracts verified present with matching signatures/docstrings/raises, sprint-notes decisions match the diff with zero review cycles, full test suite green (5180 passed / 18 skipped) with 97-100% coverage across every touched module, pre-commit clean on all 60 sprint-changed files, and all four demos deterministic and byte-identical (module output) across repeated runs.
