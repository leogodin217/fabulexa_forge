# Sprint Review: documentation-channel

**Date:** 2026-08-28
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No `# TODO`/`# Future:` scaffolding, no bare `pass` bodies, no dead self-renames added; every new public symbol traced to a production caller via `find_references`/`find_workspace_symbols`. |
| 2. Consistency / DRY | observations | 1 | One trivial duplicate helper (see Findings) against an established repo convention of module-private mini-helpers; everything else (shared `build_carried_provenance`, shared `_enum_domains.py` parse floor) was correctly factored to avoid duplication. |
| 3. Test names | clean | 0 | Sampled ~15 of the 104 new test functions across `test_documentation.py`, `test_provenance.py` (x3), and companion tests; each name matches its body's assertions (e.g. `test_windowed_and_full_dimensional_provenance_stamping_identical` genuinely compiles both paths and compares). |
| 4. Test value | clean | 0 | New tests pin exact values (`ColumnDoc(...)`, exact provenance maps, exact YAML text) rather than `len()>0`/`is not None` alone; the few `is not None` hits found are mypy type-narrowing lines immediately followed by an exact-value assertion, or a deliberate presence/absence contrast test (`test_documentation_presence_does_not_reshape_the_table_set`). No ≥3-test near-duplicate groups found. |
| 5. Coverage | clean | 0 | All sprint-added files ≥90%: `dictionary.py` 100%, `documentation.py` 97%, `_enum_domains.py` 100%, `query_spec.py` 100%, `init_annotations.py` 90%, `sidecar.py` 98%. Full touched-package run: 5513 passed, 18 skipped (pre-existing environmental skips: Kafka broker, missing demo bundles — unrelated to this sprint), 97% total. |
| 6. Type-ignore density | clean | 0 | Zero `# type: ignore` markers added by this sprint's test diff. |
| 7. Spec ↔ codebase | observations | 1 | Spot-checked `Sidecar.documentation()`, `Documentation`/`ColumnDoc`/`EnumOption`, `build_grain_sql`'s 5-tuple (every return site updated), `TableReport`/`QuerySpec` field additions (no default on `TableReport`, `default_factory=dict` on `QuerySpec` per spec), corrupter forwarding loop, manifest/README dictionary rendering — all match the spec's contracts and docstrings verbatim. One 7c-class observation on the `records_kind_from_table` duplicate (same finding as gate 2). |
| 8. Workspace | clean | 0 | `git status --porcelain` empty; no untracked files. |
| 9. Pre-commit | clean | 0 | `pre-commit run --all-files` — all 8 hooks passed (trim whitespace, EOF, yaml, toml, ruff, ruff format, mypy strict, understand bundles). |
| 10. Demos | clean | 0 | All 6 phase demos ran twice each, exit 0, byte-identical stdout/stderr between runs (determinism confirmed). |

Severity values:
- **clean** — gate found nothing.
- **observations** — gate found smells worth recording but no blocker.
- **blockers** — must fix before merge.

## Findings

### Gate 2: Consistency / DRY

- **finding 1** (observation): `records_kind_from_table` in
  `src/fabulexa_forge/exporters/companion/dictionary.py:71` is a structurally
  identical duplicate of the pre-existing `_records_kind_from_table` in
  `src/fabulexa_forge/exporters/election.py:170` — same body:
  ```python
  if not table_name.startswith(_RECORDS_TABLE_PREFIX):
      return None
  return table_name[len(_RECORDS_TABLE_PREFIX):]
  ```
  `_RECORDS_TABLE_PREFIX = "records__"` is also independently redefined a
  third time in `source/plan.py:147`. This matches an established repo
  convention (each module keeps its own private one-line constant/helper
  rather than import a private symbol across modules, avoiding cross-module
  coupling for a trivial 3-line function) — `_records_kind_from_table` and
  the constant were *already* duplicated this way before the sprint
  (election.py vs. source/plan.py), so `dictionary.py`'s copy follows the
  codebase's existing pattern rather than introducing a new one. Recorded as
  an observation, not a blocker, because the pattern predates this sprint
  and export-only cross-package sharing would be a new coupling decision
  that the spec did not ask for.

### Gate 7: Spec ↔ codebase

- **finding 1** (observation): Same underlying issue as Gate 2 finding 1,
  viewed from the 7c (impl → spec) direction — the spec's Module Changes
  Summary did not call out that `companion/dictionary.py`'s
  `records_kind_from_table` duplicates logic already present in
  `exporters/election.py`. Not a blocker: the duplication is of a trivial,
  three-line, purely-mechanical string-prefix helper, and the repo already
  tolerates the same duplication pattern between `election.py` and
  `source/plan.py`.

## Recommendation

**APPROVED-WITH-NOTES**

No blockers. The implementation matches the spec's contracts exactly at
every sampled site (field defaults/placement, the `Documentation` view's
resolution rules, per-mode provenance stamping including every
`build_grain_sql` return site, report-forwarding at both assembly sites,
companion README/manifest dictionary rendering, `init` annotations),
pre-commit is clean, coverage is high on every new file, all six demos are
deterministic, and the 104 new tests pin exact values rather than weak
existence checks. The single observation (a trivial duplicate one-line
helper matching an existing repo convention) is left to the user's
ACCEPT/FIX judgment and does not block merge.
