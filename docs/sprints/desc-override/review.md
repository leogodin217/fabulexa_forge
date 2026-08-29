# Sprint Review: desc-override

**Date:** 2026-08-29
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# TODO`/`# Future:` markers, no self-rename aliases, no orphaned `pass` bodies in the diff. Every new private helper (`_require_descriptions_map_valid`, `_table_author_descriptions`, `_resolve_state_table_descriptions`, `_resolve_junction_table_descriptions`, `_resolve_descriptions_map`, `_resolve_source_doc`, `_ns_unit_survives`) traces to a production caller via `find_references`. |
| 2. Consistency / DRY | clean | 0 | The sprint reuses pre-existing generic machinery rather than forking it: `_require_nonblank_str` (pre-existing) for `ColumnDecl.description`; `_resolve_temporal_key_map` (pre-existing, shared with `render`) for both source description-key resolvers, passing a no-op `verify` callback since prose carries no render-shape check; `_check_state_column_name`/`_check_junction_column_name` reused with existing `allow_identity`/`allow_owner` flags; `_check_column_domain` reused for base. `_resolve_source_doc` and `_ns_unit_survives` are extractions of pre-existing inline logic in `dictionary.py`, each now called from two sites (author-tier + legacy path) — genuine DRY, not one-use scaffolding. |
| 3. Test names | clean | 0 | Spot-checked test names against bodies across `test_models.py`, `test_provenance.py` (x3), `test_plan.py` (x2), `test_readme.py`, `test_manifest.py`, `test_driver.py`, `test_fingerprint.py`. Each name's claim (parses / raises / stamps / forwards / unaffected) is exercised with an exact-value assertion matching the claim. |
| 4. Test value | clean | 0 | No ≥3-test near-duplicate group found (each new test targets a distinct config shape: from/derived/null column modes, empty/whitespace/absent variants, per-mode stamping, per-error-type gate). Assertions are exact-value throughout (`==` against a fixture-known dict/string), not `len(x) > 0` or bare `is not None` (the two `is not None` hits found are pre-existing Optional-narrowing idiom for mypy, not weak value pins). |
| 5. Coverage | clean | 0 | `pytest --cov` over every sprint-touched src package: `config/models.py` 99%, `exporters/base/engine.py` 97%, `exporters/base/plan.py` 99%, `exporters/companion/dictionary.py` 99%, `exporters/dimensional/engine.py` 100%, `exporters/query_spec.py` 100%, `exporters/source/engine.py` 98%, `exporters/source/plan.py` 96%, `incremental/driver.py` 99%, `incremental/fingerprint.py` 100%, `reader/documentation.py` 97%. All uncovered line numbers were checked against the diff and are pre-existing, sprint-untouched branches (e.g. `RenameEntry.table`/`sub_type`/`name` empty-string branches at models.py:982-1002, predating this diff). No new file added under 85%; no sprint-added line is uncovered. |
| 6. Type-ignore density | clean | 0 | Zero `# type: ignore[...]` markers added by the diff in `tests/`. |
| 7. Spec ↔ codebase | clean | 0 | 7b: every contract in spec.md (`ColumnDecl.description`, `SourceTableDecl.descriptions`, `RenameEntry.descriptions` + 3 validators, `QuerySpec`/`TableReport.author_descriptions`, the plan-time gate table, `ColumnDoc`/`resolve_column_doc`, fingerprint exclusion) matches the implementation signature-for-signature and docstring-for-docstring; the two forwarding call sites named in the spec (`write_query_specs` both arms, `_build_windowed_report`) are both updated. 7c: the sprint deliberately reused generic pre-existing machinery (`_resolve_temporal_key_map`, `_require_nonblank_str`, `_check_column_domain`) rather than the spec prescribing new parallel helpers — no spec-time miss found. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty. |
| 9. Pre-commit | clean | 0 | `pre-commit run --all-files` — all 8 hooks (trailing-whitespace, end-of-file, check-yaml, check-toml, ruff, ruff-format, mypy --strict, understand-bundles --check) passed. |
| 10. Demos | clean | 0 | All three `docs/sprints/desc-override/demos/phase_{1,2,3}_*.py` run twice each; stdout byte-identical across both runs for all three (config parsing / whitespace refusal, per-mode plan stamping + gate-failure messages, README/manifest re-voicing + byte-identical dataset + fingerprint equality/inequality). |

Severity values:
- **clean** — gate found nothing.
- **observations** — gate found smells worth recording but no blocker.
- **blockers** — must fix before merge.

## Findings

No gate returned `observations` or `blockers`.

### Config-boundary check (primary focus)

Traced every default / `.get(key, fallback)` / None-check touching an author-configurable value introduced by this sprint:

- `ColumnDecl.description: str | None = None`, `SourceTableDecl.descriptions: dict[str, str] | None = None`, `RenameEntry.descriptions: dict[str, str] | None = None` — all absence-detection Optionals with a load-time non-blank validator (`description_nonempty` / `table_shape` / `entry_well_formed` via the shared `_require_descriptions_map_valid`). Absence is documented and behaviorally distinct ("inheritance as before"); presence is validated non-empty/non-whitespace. Not a fallback.
- `QuerySpec.author_descriptions: Mapping[str, str] = field(default_factory=dict)` — mirrors the pre-existing `provenance`/`kind_values` pattern the spec explicitly cites; "empty means no overrides" is a real, exercised code path (tested at `tests/exporters/test_query_spec.py` and the event-log stamping test), not a value substitution.
- `TableReport.author_descriptions: Mapping[str, str]` — required, no default (a genuine breaking change per spec), forcing every construction site to state it explicitly.
- `_resolve_descriptions_map`'s `column_renames.get(key, key)` (base/plan.py) and the source-side `_apply_*_rename`'s pre-existing `rename.get(src, out)` idiom — `key`/`src` is only reached after the shared `_check_column_domain` / `_check_state_column_name` / `_check_junction_column_name` gate has already validated it is a real, addressable column identity; the fallback is the identity's own name standing in for "not explicitly renamed," which is the pre-existing, spec-sanctioned output-naming convention for every unrenamed column in the table — not an invented mapping value.
- Test/fixture helper defaults (`documented_actor_table_report(..., author_descriptions: Mapping[str, str] | None = None)` and siblings in `tests/exporters/companion/_fixtures.py`) are internal test-helper arguments, not author config — correctly out of scope per the scope test.

No fallback, default, or swallowed-error was found substituting a value for a genuinely author-specified export/corrupt config field.

## Recommendation

**APPROVED** — no blockers, no observations.
