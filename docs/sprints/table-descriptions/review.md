# Sprint Review: table-descriptions

**Date:** 2026-08-30
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# TODO`/`# Future:`/bare-`pass` additions; no self-renames in the diff; sprint added no new top-level `def`/`class` symbols in `src/` (only fields, constants, and modified function bodies), so the exported-but-uncalled check has nothing to check. |
| 2. Consistency / DRY | observations | 1 | `_require_nonblank_str` reused (not duplicated) for all three new `description` fields, matching the phase-1 sprint note's stated decision. `_EVENT_LOG_TABLE_DESCRIPTION`/`_EVENT_LOG_COLUMN_DESCRIPTIONS` follow the pre-existing `_LEAD_SIM_TIME_DESCRIPTION` pinned-constant pattern. One doc-drift observation below on `dictionary.py`'s module docstring. |
| 3. Test names | observations | 1 | Names accurately describe bodies in every new test read, with one exception noted below (manifest event-log test claims more than it checks). |
| 4. Test value | observations | 1 | New tests follow the established 4-tests-per-field (parses/absent/empty/whitespace) house style already present pre-sprint — not sprint-introduced multiplication. One weak-assertion finding below. |
| 5. Coverage | clean | 0 | Full-suite run with `--cov` over every touched src package: `query_spec.py`, `fingerprint.py` 100%; `dictionary.py` 99% (one pre-existing uncovered line, `records_kind_from_table`'s `None` branch, untouched by this diff); `documentation.py` 97% (two pre-existing uncovered lines); `driver.py` 99%; `base/engine.py`, `base/plan.py`, `source/engine.py`, `source/plan.py`, `dimensional/engine.py`, `config/models.py` all ≥96%. No sprint-added file is below 85% (only new files are the three demos, which aren't part of `--cov` targets). 5602 passed, 18 skipped, 0 failed. |
| 6. Type-ignore density | clean | 0 | Zero `# type: ignore` added by this sprint's test diff. |
| 7. Spec ↔ codebase | observations | 2 | Every contract in spec.md (config models, carriage, stamping, fingerprint, resolution, origin vocabulary) verified against the implementation line-for-line — signatures, docstrings, and precedence order all match. Sprint notes' decisions verified against the diff; all match. Two 7c-flavored findings folded into gates 2/3/4 above (doc-drift, weak assertion) rather than new spec-vs-codebase divergence. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty at review time. |
| 9. Pre-commit | clean | 0 | `pre-commit run --files <27 changed files>` — all hooks passed (trim whitespace, EOF, yaml, ruff, ruff format, mypy strict). |
| 10. Demos | clean | 0 | All three demos (`phase_1_config_surface.py`, `phase_2_carriage.py`, `phase_3_rendered_companions.py`) ran twice each, exit 0, byte-identical output both runs. |

Severity values:
- **clean** — gate found nothing.
- **observations** — gate found smells worth recording but no blocker.
- **blockers** — must fix before merge.

## Findings

### Gate 2: Consistency / DRY

- **finding 1** (observation): `src/fabulexa_forge/exporters/companion/dictionary.py:31-48` — the module-level docstring's "Table resolution" and "Column resolution" sections describe only the pre-sprint resolution order (single-source-table forward; carried-column inheritance). Phase 3 added an author-first tier and a forge-pinned event-log tier to both `resolve_table_description` and `resolve_column_doc`, and those two functions' own docstrings were updated correctly, but the module docstring — the first thing a reader of this file sees — was left describing the old, narrower resolution order. Not a contract violation (the function docstrings are the source of truth and are correct), but a real doc-drift gap in the file a future reader orients from first.

### Gate 3: Test Name Audit

- **finding 1** (observation): `tests/exporters/companion/test_manifest.py:501-531` — `test_event_log_table_and_columns_carry_pinned_descriptions`'s docstring states it verifies "all six pinned column descriptions ... no units, no enum_options." The body pins the exact value only for `item_type` (lines 526-528); for the other five columns (`id`, `item_id`, `event`, `occurred_at`, `changes`) it asserts only `description is not None` and `unit is None` (lines 529-531), never checking `enum_options` for those five and never pinning their exact prose. The name/docstring promise a stronger check than the body delivers.

### Gate 4: Test Value Audit

- **finding 1** (observation): `tests/exporters/companion/test_manifest.py:529-530` — `assert columns[name]["description"] is not None` for five of the six pinned event-log columns, where the fixture's exact expected text is a known constant (`_EVENT_LOG_COLUMN_DESCRIPTIONS` in `dictionary.py`, and the sibling test `tests/exporters/companion/test_readme.py`'s `test_event_log_section_renders_pinned_table_and_column_descriptions` already pins all six exactly). Low severity — the resolution logic itself is fully pinned by the README test and by `resolve_column_doc`'s own unit tests — but this manifest test could pass even if the manifest-rendering path silently dropped/garbled four of the five column descriptions it claims to verify.

## Recommendation

No blockers were found. Config-boundary compliance is clean throughout: `TableDecl.description` / `SourceTableDecl.description` / `RenameEntry.description` are genuine author-config optional fields validated exactly like the existing column-override string rule (reusing `_require_nonblank_str`, not inventing a new one); `QuerySpec.author_table_description`/`event_log` defaults are internal runtime-carriage fields (matching the existing `provenance`/`kind_values`/`author_descriptions` style), explicitly justified in the spec as not a Principle-#7 conflict; `TableReport` gained the same two fields with no defaults, forcing every call site to state them explicitly. All contracts in spec.md were verified against the implementation and matched exactly (signatures, docstrings, precedence order). Full test suite green (5602 passed), pre-commit clean, all three demos deterministic across two runs.

**APPROVED-WITH-NOTES** — no blockers; three observations recorded (doc-drift in `dictionary.py`'s module docstring; a test whose docstring/name claims more coverage than its body delivers; a corresponding weak assertion on five of six pinned column descriptions in that same test). Mergeable as-is; fix-vs-accept on the observations is the user's call.
