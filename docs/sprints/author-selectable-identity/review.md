# Sprint Review: author-selectable-identity

**Date:** 2026-08-27
**Reviewer:** Claude (fresh eyes, tier-2 context loaded)

## Summary

| Gate | Severity | Findings | Notes |
|---|---|---|---|
| 1. Dead code | clean | 0 | No scaffolding markers, no bare-`pass` bodies, no inert self-renames; every sprint-added public/private symbol (17 functions/classes checked via `find_references`) traces to a production caller chain, not just tests/demos. |
| 2. Consistency / DRY | observations | 2 | `_known_kinds` (keys_init.py) duplicates a sidecar-scan body that already exists 3x elsewhere in the codebase; several new helpers have exactly one call site but match the file's existing one-call-site-helper decomposition style. |
| 3. Test names | clean | 0 | Sampled `test_keys_init.py` (new file, 30 tests), the identity-parse pairs in `test_stream_config.py`, and `test_election_stream.py`'s 11 new tests — names match bodies and assertions exactly. |
| 4. Test value | clean | 0 | Exact-value assertions throughout; `is not None` occurrences are mypy-narrowing guards immediately followed by exact-value checks (established idiom), not standalone weak assertions; no illegitimate test multiplication. |
| 5. Coverage | clean | 0 | Full-suite coverage on every sprint-touched file: 95–100% (engine.py 100%, presentation.py 100%, types.py 100%, keys_init.py 100%, driver.py 99%, playback/* 97–100%); no new source files; all residual misses trace to pre-existing lines outside the sprint's diff hunks. |
| 6. Type-ignore density | clean | 0 | Exactly one new `# type: ignore[misc]`, the standard frozen-dataclass mutation-test idiom (`test_proposal_is_frozen`). |
| 7. Spec ↔ codebase | observations | 3 | See below — one spec-scope carry-forward (mixer Kafka key), two spec-overstated-file cases (debezium.py, `_election_fixtures.py` needed no change), one docstring/spec `Raises` type mismatch. |
| 8. Workspace | clean | 0 | `git status --porcelain` empty. |
| 9. Pre-commit | clean | 0 | `pre-commit run --files <all 41 touched files>` — every hook passed (trailing whitespace, end-of-file, check yaml, ruff, ruff format, mypy strict). |
| 10. Demos | clean | 0 | All 5 demos run twice each via `uv run python`; deterministic (byte-identical output modulo tempdir paths); all exit 0. One demo docstring is imprecise (noted below), not a functional issue. |

## Findings

### Gate 2: Consistency / DRY

- **finding 1** (observation): `src/fabulexa_forge/exporters/keys_init.py:28` (`_known_kinds`) is structurally identical (same loop, same `category == "records"` check, same `assert kind is not None` message) to `known_records_kinds` in `src/fabulexa_forge/exporters/streaming/routing.py:134`. This is the *fourth* near-identical copy of this sidecar-scan in the codebase (also `_known_records_kinds` in `source/init.py`, `_known_records_kinds` in `source/plan.py`, `_records_kind_from_table` in `exporters/election.py`) — an established pre-existing pattern (each layer keeps its own copy rather than importing across the layer-direction boundary that `keys_init.py`, consulted by dimensional/source/streaming alike, must not cross). Not introduced as a new smell by this sprint; flagged for awareness only, not a blocker.
- **finding 2** (observation): Several new helpers (`_known_kinds`, `_render_population` in keys_init.py; `_resolve_published_surfaces` in engine.py; `_check_properties_disjoint_from_identity`, `_drop_unpublished_presentation_id` in playback) have exactly one call site in production code. Each is independently documented and testable, and matches the file's pre-existing style of decomposing a multi-rule validator into named single-purpose helpers (e.g. `_check_selection_non_empty` / `_check_atoms_unique` in the same `playback/selection.py`, pre-existing). Not flagged as a violation given the established local convention, but noted per the skill's instruction to record what was checked even when not blocking.

### Gate 7: Spec ↔ codebase

- **finding 1** (observation, 7c — spec-time miss): The spec's Module Changes Summary table lists `src/fabulexa_forge/exporters/streaming/debezium.py` as needing a change ("Value schema / `d` before-image consume `OutputEntry` + resolved keys") and Phase 2's Files table lists it as `Modify`. The implementation correctly made **zero** changes to `debezium.py` — it already consumed `event.key_column` / `event.key_value` generically before this sprint, so the resolved-key change flows through automatically. Confirmed by direct inspection (no `presentation_id` or `OutputEntry` reference in the file) and by the diff (`debezium.py` absent from the actual changed-files list). The spec over-predicted scope here; the implementer's git notes for Phase 3 record the analogous "presentation.py needed no change" decision but did not separately flag debezium.py — worth a note back to the spec process, not a code issue.
- **finding 2** (observation, 7c): Phase 2's Files table also lists `tests/exporters/streaming/_election_fixtures.py` as an author-step `Modify` target; it too received zero changes. Inspection confirms the fixture module only builds base-layer DuckDB tables/sidecar rows and never constructs a `StreamEvent`, so the `presentation_id`-field removal and `key_column` reshape don't touch it. Correctly unmodified; another spec over-prediction.
- **finding 3** (observation, 7b): `propose_key_election`'s implemented docstring documents `Raises: PresentationKeysInvalidError` (a `ReaderError` subclass), while the spec's Phase 5 contract text says `Raises: ExportError`. The implementation is correct and consistent with the codebase's established convention — `sidecar.presentation_keys()` already raises `PresentationKeysInvalidError` and existing code (`exporters/election.py`, `exporters/base/plan.py`) propagates it unwrapped the same way. This is spec imprecision, not an implementation bug.
- **finding 4** (carry-forward item, judged): `KafkaSink.deliver` in `src/fabulexa_forge/exporters/streaming/mixer/sink.py:157` still hardcodes `encode_pinned({"record_id": event.record_id})` instead of `event.key_column` / `event.key_value` — unlike `kafka_sink.py:295`, which correctly keys on `{event.key_column: event.key_value}`. **Judgment: correctly out of this sprint's spec scope**, not a blocker. The spec's own Scope section explicitly excludes "corrupter/mixer surfaces beyond the removed `StreamEvent` field," and its "What Doesn't Change" section states "The mixer's semantics — pass-through; only its invariant's field list shrinks with `StreamEvent`." The sprint kept that boundary faithfully. Recorded as an **observation** because the practical effect survives past sprint end: under any non-default election (`record_index`/`presentation_id`) or a `rename`, FabulMixer's live Kafka key now diverges from the batch `kafka_sink.py` / JSONL / Debezium paths for the same run — the exact "default join trap" class of bug this sprint's Phase 2 delivers language calls out as dying, still alive in the one remaining sink. Worth a follow-up ticket/sprint; not a defect in this sprint's delivered scope.

### Gate 10: Demos

- **finding 1** (observation, cosmetic): `docs/sprints/author-selectable-identity/demos/phase_1_duplicate_key_refusal.py`'s docstring says it demonstrates "a duplicate `content` key at the top level," but the actual fixture (`DUPLICATE_KEY_STREAM_CONFIG`) duplicates the `keys` key, not `content`. The demo runs correctly and its assertions are accurate; only the docstring prose is stale.

## Recommendation

**APPROVED-WITH-NOTES**

No blockers. Gates 2, 7, and 10 carry observations only — all traced, judged, and none rise to a config-boundary violation, contract mismatch, dead code, or quality defect that would block merge. The mixer `KafkaSink.deliver` carry-forward item is confirmed correctly out of this sprint's declared scope (spec's own Scope + "What Doesn't Change" sections exclude it) but is surfaced here as a real, user-visible inconsistency worth a follow-up. Full suite green (5401 passed, 18 skipped), pre-commit clean on every touched file, coverage 95–100% across every sprint-touched source file, and all 5 demos deterministic across two runs each.
