---
name: architecture
description: Designing interfaces, contracts, and architectural decisions for the exporters and corrupters.
subsystem-docs: docs/architecture/*.md
context: |
  docs/PROCESS.md
  docs/architecture/README.md  outline  #per-subsystem-design-docs
  docs/CAPABILITIES.md  outline  #status-legend
---

Past the subsystem table pinned above, the README outline routes three more ways
— § Staged roadmap for build order, § Status by implementation state, § Inputs
and fixtures for what the test emits carry. Read the range you need.

A design moves through `/arch-design` (writes a doc under
`docs/architecture/pending/`) → `/arch-review` (judges it) → `/create-sprint` →
`/fold-pending` (retires it).

Code navigation is cclsp-first: `.claude/skills/worker-protocol.md` § Code
Navigation. Findings and bugs are tracked in the `note` vault, not GitHub —
`/note list --type finding --status open`.
