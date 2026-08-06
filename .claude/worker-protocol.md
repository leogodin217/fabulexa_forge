# Worker Protocol

Rules for the duration of your task. This is the subagent counterpart to the
main session's system prompt — a subagent inherits neither that prompt nor
`CLAUDE.md`, so everything a spawned agent must know is here.

## Reporting (MANDATORY)

- Report outcomes faithfully. If tests fail, say so and include the output. If
  you skipped a step, say so. Say "done" only when you verified it.
- Don't write scaffolding for functionality that doesn't exist yet — no stub
  methods, no loops that iterate and do nothing, no `# Future:` placeholders.
- Match the surrounding code's style, naming, and comment density.
- Never edit a file outside the working directory you were given.

## The Config Boundary — No Invented Mapping Values

This repo's principle **No invented mapping values** (CLAUDE.md § Core
Principles): a misconfigured or incomplete export/corrupt config must FAIL at
load time — never silently work. If your task writes or judges code, this is
your primary concern. It is violated two ways:

- **Fallback** — author config is missing or invalid, and the code substitutes
  a value and continues instead of raising.
- **Invented value** — the config is silent on a mapping-shaping value (the
  grain of a fact table, a natural or elected key, a target table or column
  name, a source population, an FK path, a slice horizon), and the code picks
  one instead of erroring when the config loads.

### The scope test

Before writing a default, `or`-fallback, `dict.get(key, fallback)`, or None
check — or flagging someone else's — ask:

> Is this value something an export/corrupt author specifies (or should
> specify) in YAML?

- **Yes** → it must come from config, or be a load-time error raising a
  specific exception that names the missing key. Any default on it is a
  violation.
- **No** (internal helper argument, test fixture, demo constant) → it is
  ordinary Python. Write it idiomatically; do not flag it in review.

Two sources answer "No" and are never defaults:

- **The sidecar.** A value read from `base.json` (column lists, types,
  temporal class, references) is *sourced*, not invented — the sidecar is
  authoritative per emit. Shadowing it with a literal is the violation
  (Principle: read the sidecar, never hard-code).
- **A mode's published contract.** Values fixed by what a mode *means* — the
  source event log's dense tape-ordered `id`, the streaming `c`/`u`/`d` op
  codes, operational presentation defaults — are mode-definitional: every
  export of that mode renders them identically, so they are not author knobs.
  Discriminator: *if authors would reasonably differ per export, it must come
  from config; if it is what the mode means, it may be hardcoded.*

`init` is not an exemption but a different surface: it *proposes* config by
writing YAML the author reviews and owns. Proposal there is fine; the same
value silently assumed at export time is the violation.

### Never write, always flag

```python
grain = config.grain or "event"          # invents the fact grain
key = spec.natural_key or "id"           # invents a table's key
fmt: str = "csv"                         # Pydantic default lets a missing YAML key work
except ConfigError: pass                 # swallows a config-validation failure
```

### Fine to write, never flag

```python
def _write_emit(tmp_path, kinds=("person",)): ...  # test/demo helper — no author value
slice_at: int | None = None   # Pydantic optional-field absence detection; the code
                              # must then genuinely take the tape's-end path, not
                              # substitute a horizon
_EVENT_LOG_FIRST_ID = 1       # mode-definitional constant (the log's published contract)
```

When in doubt, trace the value to its source. If it originates from a parsed
`ExportConfig` / `StreamConfig` / `CorruptConfig` and the code papers over its
absence, it is a violation. If it never touches author config, it is not.

## File Reading

- Never re-read a file you have already read in this session.
- Use offset/limit for any file over 500 lines. Grep or LSP to find the relevant
  section first.
- Grep before Read: before reading a large file, use Grep to locate the specific
  section you need.
- Run `tools/mdnav FILE.md` before reading unfamiliar markdown, then Read only
  the section by line range.

## Code Navigation — cclsp first for any named symbol

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

The shell is not an exemption: `grep`/`rg` run through Bash counts as grep. A
PreToolUse hook (`tools/hooks/grep_guard.py`) hard-blocks symbol-shaped grep/rg
over `.py` files — bare identifiers (or `|`-alternations of them) and `def`/`class`
searches — and points you back here. Genuine text search (a regex metacharacter,
a quoted multi-word string, or a non-`.py` target) passes untouched.

**Stale paths across worktrees.** cclsp indexes from the directory the session
launched in. A session launched inside a worktree indexes that worktree and its
results are correct. But if a result points at a path **outside your working
directory** — the main checkout while you are working in `../worktrees/<sprint>`
— that path is wrong; re-resolve it against your working directory before you
Read or Edit.

## Response Style

- No narration ("Let me read...", "Now I'll..."). Just call tools directly.
- No echoing file contents back — whoever spawned you can read them.
- Answer with outcomes, not process.
