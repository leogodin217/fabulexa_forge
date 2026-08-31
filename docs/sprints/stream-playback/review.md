# Sprint Review: stream-playback

Date: 2026-08-31
Reviewer: Claude (fresh eyes, tier-2 context loaded)

Diff base: `ddec86392b1f01a57d4e0c6d1ba4711183ba9c2e` (merge-base of `HEAD` and
`prepare_for_loom`). HEAD reviewed (cycle 2): `4659621` (Sprint stream-playback -
review cleanup), which fixes cycle 1's sole blocker on top of `8dee67c0` (Phase 4:
The verb re-seam).

## Cycle 2 note

Cycle 1's single blocker (finding 3 below: mixer CLI double notice emission) was
fixed by commit `4659621` ("Sprint stream-playback - review cleanup"). `cmd_mixer`
now threads a new discarding sink, `_discard_mixer_render_notice` (`cli.py:685`),
into its `resolve_stream_render` call, while `seed_mixer_run`'s call keeps
`render_notice_stderr` — restoring one stderr line per notice for the mixer path.
The fix is pinned by a new regression test,
`test_mixer_emits_out_of_domain_notice_once` (`tests/test_cli_mixer.py:589`), which
builds an emit with a `status` enum domain, a `where` config value outside that
domain, runs `cmd_mixer` end-to-end, and asserts `captured.err` contains exactly one
`"notice:"` line. Verified genuine (not merely asserted): read the diff, confirmed
`_discard_mixer_render_notice` has exactly one call site (`find_references`), traced
`resolve_stream_render`'s unconditional `resolve_streams` call against
`seed_mixer_run`'s independent `iter_stream_events` → `resolve_streams` call — the
two-pass structure is unchanged (still runs twice internally) but only one pass now
renders to stderr — and ran `uv run pytest tests/test_cli_mixer.py` (33 passed,
including the new regression test). The new sink is a plain no-op callback (no
closure state, no config value touched) and introduces no config-boundary issue;
`find_workspace_symbols` for "discard"/"NoticeSink" shows the same per-module
discarding-sink idiom already used by all four sprint demos (`_discard_notice`) and
by `tests/_support/notices.py` (`discard_notice_sink`, test-only, not importable
from production code) — no duplication, just the established convention repeated at
its natural site.

## Summary

| Gate | Severity | Notes |
|---|---|---|
| 1. Dead code scan | observations | Carried forward unchanged: `write_jsonl_stream` / `write_debezium_stream` (pre-existing public API) still have zero production callers post-fix (re-checked via `find_references`) — only `__init__.py`'s re-export and their own tests. |
| 2. Consistency / DRY | clean | No new structural duplication from the fix; `_discard_mixer_render_notice` matches the existing per-module discarding-sink idiom (demos' `_discard_notice`, tests' `discard_notice_sink`) rather than introducing a competing abstraction. |
| 3. Test name audit | clean | `test_mixer_emits_out_of_domain_notice_once` accurately describes its assertion (exactly one `"notice:"` line in stderr after a full `cmd_mixer` run with a deliberately out-of-domain `where` value). |
| 4. Test value audit | observations | Carried forward unchanged: `stream_render.py`'s forbidden-import rule (`never driver, kafka_sink, pacer`) remains unverified by a dedicated test, asymmetric with `stream.py`'s `test_stream_playback_imports_only_pure_streaming_surfaces`; not touched by the fix. |
| 5. Coverage | clean | Fix commit only touches `cli.py` (+17/-3) and its test file; `tests/test_cli_mixer.py` full run: 33 passed. |
| 6. Type-ignore density | clean | Fix diff adds zero `# type: ignore`. |
| 7. Spec ↔ codebase (both directions) | clean (blocker resolved) | Cycle 1's blocker (mixer CLI double notice emission, undeclared/untested regression) is fixed by commit `4659621`: `cmd_mixer`'s `resolve_stream_render` call now passes `_discard_mixer_render_notice` instead of `render_notice_stderr`, so only `seed_mixer_run`'s pass reaches stderr — restoring the spec's "both mixer surfaces" unaffected guarantee, now pinned by `test_mixer_emits_out_of_domain_notice_once`. No new spec deviation introduced by the fix. |
| 8. Workspace | clean | `git status --porcelain` empty at review time (post-fix). |
| 9. Pre-commit | clean | `pre-commit run --files src/fabulexa_forge/cli.py tests/test_cli_mixer.py docs/sprints/stream-playback/review.md` — all 8 hooks passed with zero modifications; `git status --porcelain` empty afterward. |
| 10. Demos | clean | Fix commit does not touch any demo file; cycle 1's demo runs stand. |

## Findings

### Gate 1 — Dead code scan (observation, carried forward unchanged)

1. **`write_jsonl_stream` / `write_debezium_stream` now have zero production callers.**
   File: `src/fabulexa_forge/exporters/streaming/jsonl.py:129` (def), `src/fabulexa_forge/exporters/streaming/debezium.py:422` (def).
   Severity: observation.
   Unchanged from cycle 1. `find_references` on both symbols still shows only `__init__.py`'s re-export and their own test suites as callers; the fix commit does not touch either file. Disclosed scoping decision from Phase 4; not addressed by this sprint.

### Gate 4 — Test value audit (observation, carried forward unchanged)

2. **Architecture-guard test asymmetry: `stream_render.py`'s forbidden imports are unverified by a test.**
   File: `tests/playback/test_selection.py:632-651`.
   Severity: observation.
   Unchanged from cycle 1. The fix commit does not touch `stream_render.py` or `test_selection.py`. Manual inspection of `stream_render.py`'s imports remains compliant with the spec's forbidden-import rule (`debezium`, `encoding`, `engine`, `jsonl`, `presentation`, `routing` only), but no test enforces it the way `test_stream_playback_imports_only_pure_streaming_surfaces` does for `stream.py`.

### Gate 7 — Spec ↔ codebase (RESOLVED)

3. **Undeclared, untested double notice emission in the mixer CLI path — FIXED.**
   File: `src/fabulexa_forge/cli.py:685` (new `_discard_mixer_render_notice`), `:859-861` (`resolve_stream_render` call now uses the discarding sink).
   Severity: was blocker in cycle 1; resolved in cycle 2 by commit `4659621`.
   `cmd_mixer` now passes `_discard_mixer_render_notice` (a no-op `NoticeSink`) to `resolve_stream_render`, while `seed_mixer_run`'s independent call retains `render_notice_stderr`. The eager business-rule pass still runs twice internally (unchanged architecture — `resolve_stream_render` unconditionally calls `resolve_streams`; `seed_mixer_run`'s `iter_stream_events` calls it again), but only one of the two passes now writes to stderr, restoring one line per notice for every `fabulexa-forge mixer` invocation, any format — matching the spec's "both mixer surfaces" unaffected guarantee. Regression test `test_mixer_emits_out_of_domain_notice_once` (`tests/test_cli_mixer.py:589`) pins this by building an emit with a declared `status` enum domain, a `where` config value outside it, running `cmd_mixer` end-to-end with a stubbed `serve_mixer`, and asserting exactly one `"notice:"` line in captured stderr. `uv run pytest tests/test_cli_mixer.py` → 33 passed.

## Recommendation

**APPROVED-WITH-NOTES** — cycle 1's sole blocker (finding 3) is resolved and verified.
Findings 1 and 2 remain as carried-forward observations, unaffected by the fix.
