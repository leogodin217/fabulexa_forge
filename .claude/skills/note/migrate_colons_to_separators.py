#!/usr/bin/env python3
"""One-shot migration: remove colons from note frontmatter.

Policy (see SKILL.md / PROCESS.md):
  - Property values use `__` as the domain/topic separator, never `:`.
    Applies to `discovered-in` (e.g. `qa:nhs` -> `qa__nhs`).
  - Tags use `-` only and must not contain `:` (e.g. `planning:nhs` ->
    `planning-nhs`). Tags were already migrated in a prior pass; this handles
    any stragglers idempotently.

Does NOT touch `related-code` `::symbol` (Python node-id convention, kept).
Pure filesystem rewrite — no Obsidian dependency. Idempotent: re-running on a
clean vault changes nothing.

Usage:
    python3 .claude/skills/note/migrate_colons_to_separators.py            # apply
    python3 .claude/skills/note/migrate_colons_to_separators.py --dry-run  # preview
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def vault_path() -> Path:
    vp = os.environ.get("OBSIDIAN_VAULT_PATH")
    if not vp:
        cfg = Path.home() / ".config" / "fabulexa-note.json"
        if cfg.exists():
            vp = json.loads(cfg.read_text()).get("vault_path")
    if not vp:
        vp = "/mnt/c/Users/<user>/OneDrive/projects/fabulexa/Fabulexa"
    p = Path(vp)
    if not p.is_dir():
        sys.exit(f"vault_path not a directory: {p}")
    return p


def split_frontmatter(text: str) -> tuple[str, str] | None:
    m = re.match(r"^---\n(.*?\n)---\n?(.*)$", text, re.DOTALL)
    if not m:
        return None
    return m.group(1), m.group(2)


def migrate_fm(fm: str) -> tuple[str, list[str]]:
    """Return (new_frontmatter, list_of_change_descriptions)."""
    out_lines: list[str] = []
    changes: list[str] = []
    field = None
    in_tags = False
    for line in fm.split("\n"):
        # Track the current top-level field name.
        m = re.match(r"^([A-Za-z][\w-]*):(.*)$", line)
        if m:
            field = m.group(1)
            in_tags = field == "tags"
            val = m.group(2).strip()
            if field == "discovered-in" and ":" in val:
                new_val = val.replace(":", "__")
                line = f"discovered-in: {new_val}"
                changes.append(f"discovered-in: {val} -> {new_val}")
            out_lines.append(line)
            continue
        # List item under the current field.
        li = re.match(r"^(\s*-\s*)(.*)$", line)
        if li and in_tags:
            raw = li.group(2).strip().strip('"').strip("'")
            if ":" in raw:
                new_raw = raw.replace(":", "-")
                line = f"{li.group(1)}{new_raw}"
                changes.append(f"tag: {raw} -> {new_raw}")
        out_lines.append(line)
    return "\n".join(out_lines), changes


def main() -> int:
    dry = "--dry-run" in sys.argv
    root = vault_path()
    total_files = 0
    touched = 0
    for md in sorted(root.rglob("*.md")):
        if "_templates" in md.parts:
            continue
        total_files += 1
        text = md.read_text(encoding="utf-8")
        parts = split_frontmatter(text)
        if not parts:
            continue
        fm, body = parts
        new_fm, changes = migrate_fm(fm)
        if not changes:
            continue
        touched += 1
        rel = md.relative_to(root)
        print(f"{'[dry] ' if dry else ''}{rel}")
        for c in changes:
            print(f"    {c}")
        if not dry:
            new_text = f"---\n{new_fm}---\n{body}"
            tmp = md.with_suffix(md.suffix + ".tmp")
            tmp.write_text(new_text, encoding="utf-8")
            os.replace(tmp, md)
    print(
        f"\n{'[dry-run] ' if dry else ''}Scanned {total_files} notes, "
        f"{touched} {'would change' if dry else 'changed'}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
