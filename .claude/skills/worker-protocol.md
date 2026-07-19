# Worker Protocol

Read and follow these rules for the duration of your task.

## Efficiency Rules (MANDATORY)

### File Reading
- Never re-read a file you have already read in this session.
- Use offset/limit for any file over 500 lines. Grep or LSP to find the relevant section first.
- Grep before Read: Before reading a large file, use Grep to locate the specific section you need.

### Code Navigation — cclsp first for any named symbol

For any question about a symbol the language defines (function, class, method,
variable), call `mcp__cclsp__*` first — never grep or read whole files for it.
It's exact (no false hits from comments, strings, tests) and cheaper than
reading. Backend is basedpyright; every tool below works.

Don't know which file the symbol is in? `find_workspace_symbols` (name only) is
the entry point — not grep.

| You want… | Call |
|---|---|
| where a symbol is defined | `find_definition` |
| every use / who references it | `find_references` |
| who calls a function | `get_incoming_calls` |
| what a function calls (trace a chain outward) | `get_outgoing_calls` |
| locate a symbol when you don't know its file | `find_workspace_symbols` |
| implementations of a protocol / ABC | `find_implementation` |
| type / signature / docstring | `get_hover` |
| errors / warnings in a file | `get_diagnostics` |

Call-hierarchy, hover, and implementation take a `line:character` — get it from
`find_definition` or `find_workspace_symbols` output first. Grep/Read are for
non-symbol text only (concepts, strings, YAML, regex). A timeout just after the
server (re)starts means the index is warming — retry once; it is not broken.

cclsp indexes from the directory the session launched in. If you are working in a
worktree but a result points at a path **outside your cwd** (e.g. the main
checkout while you're in `../worktrees/<sprint>`), that path is stale/wrong —
re-resolve it against cwd before you Read or Edit. Never edit a file outside the
worktree you were told to work in.

The shell is not an exemption: `grep`/`rg` run through Bash counts as grep. A
PreToolUse hook (`tools/hooks/grep_guard.py`) hard-blocks symbol-shaped grep/rg
over `.py` files — bare identifiers (or `|`-alternations of them) and `def`/`class`
searches — and points you back here. Genuine text search (a regex metacharacter,
a quoted multi-word string, or a non-`.py` target) passes untouched.

Subagents receive these navigation rules automatically at the start of their run
via the SubagentStart hook (`tools/hooks/inject_nav_rules.py`) — no manual
handoff to spawned agents is needed. That hook mirrors this section; keep the two
in sync.

### Response Style
- No narration ("Let me read...", "Now I'll..."). Just call tools directly.
- No echoing file contents. Be concise.

## After Implementation

1. **Simplify** — Invoke the `Skill` tool with `skill: "simplify"` to review and clean up your changes.
2. **Run unit tests** — Run the project's test suite (check for package.json scripts, Makefile targets, or common commands like `npm test`, `bun test`, `pytest`, `go test`). If tests fail, fix them.
3. **Test end-to-end** — Follow the e2e recipe from the orchestrator's prompt. Skip if told to.
4. **Commit** — Commit all changes in the worktree with a clear, descriptive message. Do NOT push or create a PR.
5. **Report** — End with a single line: `DONE` (or `FAILED — <reason>`).
