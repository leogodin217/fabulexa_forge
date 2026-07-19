#!/usr/bin/env python3
"""SubagentStart hook: inject cclsp-first code-navigation rules into subagents.

Subagents inherit neither CLAUDE.md, the system prompt, nor worker-protocol.md.
A spawned agent (especially a built-in like Explore) therefore starts blind to
this repo's cclsp-first navigation rule and defaults to grep. This hook injects
those rules as `additionalContext` at the start of every subagent's run (before
its first prompt), so the rule rides along regardless of how the agent was
spawned or what its spawn prompt says.

Opt-out by design: a few agents explicitly do NOT navigate source code — they
read program OUTPUT, not implementation (e.g. data-analyst inspects simulation
results; ops-gate runs given commands and never judges code). Injecting code-nav
rules into them is noise, so they are listed in EXCLUDED_AGENTS and skipped.

SubagentStart is context-only (it cannot block). On a matched, non-excluded
agent this prints a hookSpecificOutput.additionalContext JSON object and exits 0;
otherwise it exits 0 with no output. Register with matcher "*".

This mirrors `.claude/skills/worker-protocol.md` § Dispatching Explore agents and
§ Code Navigation; that doc remains the canonical statement of the rule.
"""

from __future__ import annotations

import json
import sys

# Agents whose job is to read program OUTPUT, not source code. They get no
# code-navigation rules. Edit this list to add/remove opt-outs.
EXCLUDED_AGENTS = frozenset(
    {
        "data-analyst",  # inspects simulation output, never the implementation
        "ops-gate",  # runs given commands / mechanical edits; never reads code
    }
)

NAV_RULES = (
    "Code navigation in this repository is cclsp-first. For ANY named symbol "
    "(function, class, method, variable) call `mcp__cclsp__*` — never grep or "
    "read whole files for it:\n"
    "  - find_definition        — where a symbol is defined\n"
    "  - find_references        — every use / who references it\n"
    "  - get_incoming_calls /\n"
    "    get_outgoing_calls     — trace a call chain\n"
    "  - find_workspace_symbols — locate a symbol when you don't know its file "
    "(the entry point, not grep)\n"
    "  - get_hover              — type / signature / docstring\n"
    "  - find_implementation    — implementations of a protocol / ABC\n"
    "cclsp is exact (no hits in comments, strings, or tests) and cheaper than "
    "grep+read. Use Grep/Read ONLY for non-symbol text (concepts, strings, "
    "YAML, regex).\n"
    "This is ENFORCED, not advisory: a PreToolUse hook hard-blocks symbol-shaped "
    "`grep`/`rg` over `.py` files — bare identifiers, `|`-alternations of them, "
    "and `def`/`class` searches — whether run via the Grep tool or shelled "
    "through Bash. The shell is not an exemption. Genuine text search (a regex "
    "metacharacter, a quoted multi-word string, or a non-.py target) passes.\n"
    "A cclsp timeout right after the server starts means the index is warming — "
    "retry once; it is not broken.\n"
    "cclsp indexes from the session's launch directory. If a result points at a "
    "path OUTSIDE your working directory (e.g. the main checkout while you work in "
    "a worktree), that path is stale/wrong — re-resolve it against your working "
    "directory before you Read or Edit, and never edit a file outside the worktree "
    "you were given."
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    if payload.get("agent_type") in EXCLUDED_AGENTS:
        return 0

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SubagentStart",
                    "additionalContext": NAV_RULES,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
