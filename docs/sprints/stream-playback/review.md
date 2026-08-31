# Sprint Review: stream-playback

Date: 2026-08-31
Reviewer: Claude (fresh eyes, tier-2 context loaded)

Diff base: `ddec86392b1f01a57d4e0c6d1ba4711183ba9c2e` (merge-base of `HEAD` and
`prepare_for_loom`). HEAD reviewed: `8dee67c0` (Phase 4: The verb re-seam).

## Summary

| Gate | Severity | Notes |
|---|---|---|
| 1. Dead code scan | observations | No sprint-added scaffolding/TODOs found; `write_jsonl_stream` / `write_debezium_stream` (pre-existing public API) lost their last production caller when Phase 4 re-seamed `stream_export` over `write_line_stream`, and are now reachable only from `__init__.py`'s re-export and tests — checked via `find_references` on both symbols. |
| 2. Consistency / DRY | clean | Compared every new top-level def (`StreamResolution`, `resolve_streams`, `iter_resolved_stream_events`, `iter_resolved_snapshot_events`, `StreamPlayback`, `StreamRender`, `resolve_stream_render`, `write_line_stream`) against tier-1/tier-2 siblings (`shaped.py`, `head.py`, `snapshot.py`, driver's deleted schema-map family); no structural duplicates beyond the spec-declared, deliberate `_stream_route_tables` duplication (stream_render.py owns a copy since tier-2 may not import `driver`; the driver's old copy was deleted in Phase 4, confirmed via `find_workspace_symbols`). |
| 3. Test name audit | clean | Read `test_stream_head.py`, `test_stream_seek.py`, `test_stream_render.py`, and the `test_engine.py` additions in full; every test name accurately describes its assertion body (e.g. `test_bound_past_tape_is_a_data_condition_not_an_error` genuinely asserts no raise, `test_coincident_update_and_delete_at_t_absent_from_phase` genuinely checks absence). |
| 4. Test value audit | observations | No weak assertions or literal-multiplication candidates found in the new suites; one architecture-guard test gap noted below (`stream_render.py`'s forbidden-import rule is undeclared by any test, unlike `stream.py`'s). |
| 5. Coverage | clean | `pytest tests/playback tests/exporters/streaming --cov=...` → 952 passed, overall 99% (2866 stmts / 26 miss); every sprint-new file (`playback/stream.py`, `playback/stream_render.py`) is 100%; the handful of uncovered lines in `driver.py` (156, 280, 284) and `kafka_sink.py` (94-96, 112-115, 120) are pre-existing defensive/environment-check patterns unrelated to this sprint's diff. |
| 6. Type-ignore density | clean | Diff adds exactly one `# type: ignore[arg-type]` in a test (`test_debezium.py`); well under the >1/file or ≥3-same-shape threshold. |
| 7. Spec ↔ codebase (both directions) | blockers | 7a/7b: contracts (`StreamResolution`, `resolve_streams`, `iter_resolved_stream_events`, `iter_resolved_snapshot_events`, `write_kafka_stream`, `stream_export` internals, seam placement) all implemented verbatim against the spec's signatures/docstrings/Raises. **However**, Phase 4's mechanical fix to `cli.py`'s `cmd_mixer` (replacing `build_kafka_render_value` with `resolve_stream_render`) introduces an undeclared, untested regression: the mixer CLI path now runs the eager business-rule pass twice against the same `notice_sink` (once in `resolve_stream_render`, once inside `seed_mixer_run`'s `iter_stream_events`), doubling any out-of-domain `where` notice printed to stderr — for *every* mixer run, both formats — where previously (via `_build_value_schemas`, which never threaded a `notice_sink`) it emitted once. The spec's "What Doesn't Change" section explicitly lists "both mixer surfaces" as unchanged and only declares the double-notice cost for the re-seamed `stream_export` verb; this mixer-side doubling is not one of the two declared "Observable" changes and has no corresponding test (`test_cli_mixer.py` was not touched this sprint and has no stderr/notice assertions). 7c: no case found where the spec prescribes something the codebase already had. |
| 8. Workspace | clean | `git status --porcelain` empty at review time. |
| 9. Pre-commit | clean | `pre-commit run --all-files` — all 8 hooks (trailing-whitespace, end-of-file, yaml/toml checks, ruff, ruff-format, mypy-strict, understand-bundles) passed with zero modifications; `git status --porcelain` still empty afterward. |
| 10. Demos | clean | All four `docs/sprints/stream-playback/demos/phase_[1-4]_*.py` ran twice each via `uv run python`; all exited 0 and produced byte-identical stdout across both runs (diffed pairwise). |

## Findings

### Gate 1 — Dead code scan (observations)

1. **`write_jsonl_stream` / `write_debezium_stream` now have zero production callers.**
   File: `src/fabulexa_forge/exporters/streaming/jsonl.py:129` (def), `src/fabulexa_forge/exporters/streaming/debezium.py:422` (def).
   Severity: observation.
   Before this sprint, `driver.py`'s `stream_export` called these two functions directly for the stdout/file sinks. Phase 4 collapsed that dispatch to the new `write_line_stream` (`driver.py:241`), driven by `StreamRender.render_bytes` instead. `find_references` on both symbols shows the only remaining call sites are their own `__init__.py` re-export (`exporters/streaming/__init__.py:38,46,98,99`) and their own test suites (`test_jsonl.py`, `test_debezium.py`) — no production module calls either function anymore (confirmed the mixer's `sink.py` uses its own async produce loop, not these). This was a scoping decision recorded in the Phase 4 git note ("write_jsonl_stream/write_debezium_stream left untouched — not in Phase 4's file table"), so it is disclosed, but it leaves genuinely orphaned public API surface per the end-state test in the gate-1 checklist (must terminate in a production caller outside tests/demos/`__init__` re-exports).

### Gate 4 — Test value audit (observations)

2. **Architecture-guard test asymmetry: `stream_render.py`'s forbidden imports are unverified by a test.**
   File: `tests/playback/test_selection.py:632-651`.
   Severity: observation.
   The spec's "Seam-side placement" section says of *both* `stream.py` and `stream_render.py`: "never `driver`, `kafka_sink`, `pacer`." The sprint added `test_stream_playback_imports_only_pure_streaming_surfaces`, which enforces this rule for `stream.py` only; no parallel test enforces it for `stream_render.py`. Manual inspection of `stream_render.py`'s imports (`debezium`, `encoding`, `engine`, `jsonl`, `presentation`, `routing` — no `driver`/`kafka_sink`/`pacer`) confirms the code itself is compliant today, so this is a guard-rail gap rather than a live violation.

### Gate 7 — Spec ↔ codebase (blocker)

3. **Undeclared, untested double notice emission in the mixer CLI path.**
   File: `src/fabulexa_forge/cli.py:846-849` (new `resolve_stream_render` call) and `:853-860` (existing `seed_mixer_run` call), both passed the same `render_notice_stderr` sink.
   Severity: blocker.
   Phase 4 replaced `cmd_mixer`'s use of the deleted `build_kafka_render_value` with `resolve_stream_render(emit, config, fmt_lit, anchor, render_notice_stderr)`. `resolve_stream_render` unconditionally calls `resolve_streams` (the eager, notice-emitting business-rule pass) regardless of `fmt`. `seed_mixer_run` (unchanged, `mixer/scheduler.py:157`) independently calls `iter_stream_events(..., notice_sink)`, which also runs `resolve_streams` internally. Pre-sprint, the mixer's render-value builder (`_build_value_schemas`, only invoked for `fmt='debezium'` with `schemas_enable=True`) called `resolve_stream_surfaces`/`resolve_stream_identities` directly — neither of which accepts a `notice_sink` — so the mixer only ever emitted a stream's out-of-domain notices once (from `seed_mixer_run`). Post-sprint, every `fabulexa-forge mixer` invocation (any format) prints each such notice twice to stderr. The spec's "What Doesn't Change" section lists "both mixer surfaces" as unchanged, and its only two declared "Observable" changes are the two schema-identity fixes and the `stream_export` verb's declared double-pass cost — the mixer is not among them. `tests/test_cli_mixer.py` was not touched this sprint and asserts nothing about stderr/notice output, so this regression has no test coverage. This should be fixed (dedupe the eager pass for the mixer path, or explicitly document/test the new cost) before merge.

## Recommendation

**REVISIONS NEEDED** — one blocker (finding 3: undeclared/untested double notice emission on the mixer CLI path). Findings 1 and 2 are recorded observations only.
