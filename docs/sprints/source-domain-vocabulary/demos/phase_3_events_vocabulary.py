#!/usr/bin/env python
"""
Demo: Events path -- resolved item-types + changes keys
Sprint: source-domain-vocabulary
Phase: 3

Builds the spanning events-fixture emit (`records__ticket` audited by kind,
`membership__ticket__watchers` audited by membership) via the existing
source-mode test fixture, then renders its event log twice:

  - unlabeled: no `kind_labels`, no `rename`, no `item_type` override --
    today's verbatim behavior (`item_type='ticket'` /
    `'ticket.watchers'`, `changes` key `status`, `<f>_kind` values verbatim
    `'agent'`).
  - labeled: `kind_labels={'ticket': 'case', 'agent': 'staff'}`, a
    records-source `rename={'status': 'state'}`, and a membership
    `item_type='attendance'` override -- the declared vocabulary renders
    throughout: `item_type='case'` for ticket rows, the renamed `state`
    changes key, `party_kind` halves labeled `'staff'`, and the overridden
    `item_type='attendance'` for membership rows.

Both variants tie at sim_time=180ms (a ticket destroy and a membership
join land at the same instant); the order key's `item_type` component
tie-breaks them, and the two variants order the pair oppositely --
"ticket" < "ticket.watchers" (unlabeled: ticket first) vs "attendance" <
"case" (labeled: membership first) -- the demo prints and asserts the
`id` flip, the design doc's § Ordering consequence: relabeling is a
config change that renumbers the log like any other.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# The spanning source-mode test fixture lives under tests/ (an implicit
# namespace package) -- reused here rather than duplicated, mirroring
# dev/demo/build_emit.py's sys.path pattern.
_REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (_REPO_ROOT, _REPO_ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fabulexa_forge.anchor import resolve_effective_anchor  # noqa: E402
from fabulexa_forge.config.models import (  # noqa: E402
    ExportConfig,
    MembershipRef,
    SourceConfig,
    SourceEventsDecl,
    SourceEventSourceDecl,
)
from fabulexa_forge.exporters.election import resolve_election  # noqa: E402
from fabulexa_forge.exporters.notices import Notice  # noqa: E402
from fabulexa_forge.exporters.source.events import build_event_log_sql  # noqa: E402
from fabulexa_forge.exporters.source.plan import build_source_plan  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402
from tests.exporters.source._source_fixtures import (  # noqa: E402
    build_events_test_emit,
)

_COLUMNS = ("id", "item_type", "item_id", "event", "occurred_at", "changes")


def _discard_notice(notice: Notice) -> None:
    """A no-op NoticeSink -- this demo has nothing to say about notices."""


def _run_events(
    emit_dir: Path,
    kind_labels: "dict[str, str] | None",
    rename: "dict[str, str] | None",
    item_type_override: "str | None",
) -> "list[dict[str, object]]":
    """Build the source plan over `audit_log` under one vocabulary config
    and render its rows, ordered by `id`.
    """
    config = ExportConfig(
        mode="source",
        source=SourceConfig(
            events=SourceEventsDecl(
                name="audit_log",
                sources=(
                    SourceEventSourceDecl(kind="ticket", rename=rename),
                    SourceEventSourceDecl(
                        membership=MembershipRef(kind="ticket", property="watchers"),
                        item_type=item_type_override,
                    ),
                ),
            ),
            kind_labels=kind_labels,
        ),
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, None)
        plan = build_source_plan(emit, config, anchor, election, False, _discard_notice)
        assert plan.events is not None
        sql = build_event_log_sql(
            plan.sidecar, plan.fork_path, plan.events, plan.anchor, None
        )
        return [dict(zip(_COLUMNS, row)) for row in emit.query(sql, ())]


def _find_row(
    rows: "list[dict[str, object]]",
    item_type: str,
    item_id: str,
    event: str,
    needle: str,
) -> "dict[str, object]":
    """The one row matching (item_type, item_id, event) whose `changes`
    text contains `needle` -- disambiguates the two `watchers` create rows,
    which share one `item_id` (the owner ticket).
    """
    matches = [
        r
        for r in rows
        if r["item_type"] == item_type
        and r["item_id"] == item_id
        and r["event"] == event
        and needle in str(r["changes"])
    ]
    assert len(matches) == 1, f"expected exactly one match, got {len(matches)}: {rows}"
    return matches[0]


def _print_rows(label: str, rows: "list[dict[str, object]]") -> None:
    print(label)
    for row in rows:
        print(
            f"  id={row['id']} {row['item_type']}#{row['item_id']} {row['event']}"
            f" changes={row['changes']}"
        )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = build_events_test_emit(Path(tmp))

        unlabeled = _run_events(emit_dir, None, None, None)
        _print_rows("Unlabeled (verbatim, today's behavior):", unlabeled)

        labeled = _run_events(
            emit_dir,
            kind_labels={"ticket": "case", "agent": "staff"},
            rename={"status": "state"},
            item_type_override="attendance",
        )
        _print_rows(
            "\nLabeled (kind_labels + records rename + membership item_type override):",
            labeled,
        )

        # item_type: records source defaults to the kind's label; membership
        # source's override wins wholesale over its default label(K).property.
        assert {r["item_type"] for r in unlabeled if r["item_id"] == "t001"} == {
            "ticket",
            "ticket.watchers",
        }
        assert {r["item_type"] for r in labeled if r["item_id"] == "t001"} == {
            "case",
            "attendance",
        }

        # changes key resolution: rename relabels the ticket source's
        # `status` property to `state`, in place (order unaffected).
        unlabeled_update = _find_row(unlabeled, "ticket", "t001", "update", '"status"')
        labeled_update = _find_row(labeled, "case", "t001", "update", '"state"')
        assert '"state"' not in str(unlabeled_update["changes"])
        assert '"status"' not in str(labeled_update["changes"])

        # kind-label rendering: the membership reference field `party`'s
        # `_kind` half renders the label in both old and new halves; `_id`
        # is untouched (the member's own identity, not a kind name).
        unlabeled_join = _find_row(
            unlabeled, "ticket.watchers", "t001", "create", '"agent_b"'
        )
        labeled_join = _find_row(labeled, "attendance", "t001", "create", '"agent_b"')
        assert '"party_kind":[null,"agent"]' in str(unlabeled_join["changes"])
        assert '"party_kind":[null,"staff"]' in str(labeled_join["changes"])
        assert '"party_id":[null,"agent_b"]' in str(labeled_join["changes"])

        # Ordering consequence: the tie at sim_time=180ms (t002's destroy,
        # agent_b's join) breaks on the order key's item_type component.
        # "ticket" < "ticket.watchers" orders the destroy first; "attendance"
        # < "case" orders the join first -- relabeling flips the tie and
        # therefore renumbers `id`.
        unlabeled_destroy = _find_row(unlabeled, "ticket", "t002", "destroy", "")
        labeled_destroy = _find_row(labeled, "case", "t002", "destroy", "")
        assert unlabeled_destroy["id"] < unlabeled_join["id"], (
            "unlabeled: expected the ticket destroy to precede the membership join"
        )
        assert labeled_join["id"] < labeled_destroy["id"], (
            "labeled: expected the membership join to precede the ticket destroy"
            " (the relabeled item_type flips the sim_time=180ms tie)"
        )
        print(
            "\nOrdering consequence: unlabeled orders destroy(id="
            f"{unlabeled_destroy['id']}) before join(id={unlabeled_join['id']});"
            " labeled orders join(id="
            f"{labeled_join['id']}) before destroy(id={labeled_destroy['id']})"
            " -- relabeling renumbers the log like any other config change."
        )

    print(
        "\nSUCCESS: events path resolves item-types, changes keys, and"
        " kind-labeled <f>_kind halves as specified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
