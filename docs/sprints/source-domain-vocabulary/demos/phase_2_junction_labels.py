#!/usr/bin/env python
"""
Demo: Kind labels -- validation + junction rendering
Sprint: source-domain-vocabulary
Phase: 2

Builds a minimal emit with a polymorphic junction (`membership__visit__team`,
member kind `actor`) via the existing spanning source-mode test fixture, then
renders the junction table twice: once with no `kind_labels` (today's
verbatim behavior), once with `kind_labels: {actor: clinician}` (the member
kind values recoded). Prints both rendered row sets side by side -- the
owner column, ids, and timestamps stay untouched while the member kind
recodes -- then shows `SourceKindLabelUnknown` and `SourceKindLabelCollision`
firing at plan time.
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
    SourceTableDecl,
)
from fabulexa_forge.errors import (  # noqa: E402
    SourceKindLabelCollision,
    SourceKindLabelUnknown,
)
from fabulexa_forge.exporters.election import resolve_election  # noqa: E402
from fabulexa_forge.exporters.notices import Notice  # noqa: E402
from fabulexa_forge.exporters.source.plan import build_source_plan  # noqa: E402
from fabulexa_forge.exporters.source.renders import (  # noqa: E402
    build_junction_render_sql,
)
from fabulexa_forge.reader.emit import open_emit  # noqa: E402
from tests.exporters.source._source_fixtures import (  # noqa: E402
    build_source_test_emit,
)

_JUNCTION_TABLES = (
    SourceTableDecl(
        name="visit_team", membership=MembershipRef(kind="visit", property="team")
    ),
)


def _discard_notice(notice: Notice) -> None:
    """A no-op NoticeSink -- this demo has nothing to say about notices."""


def _render_junction(
    emit_dir: Path, kind_labels: "dict[str, str] | None"
) -> "list[dict[str, object]]":
    """Build the source plan over `visit_team` under one `kind_labels`
    config and render its rows.

    Raises:
        SourceKindLabelUnknown, SourceKindLabelCollision: propagated from
            `build_source_plan`.
    """
    config = ExportConfig(
        mode="source",
        source=SourceConfig(tables=_JUNCTION_TABLES, kind_labels=kind_labels),
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, None)
        plan = build_source_plan(emit, config, anchor, election, False, _discard_notice)
        table = plan.tables[0]
        sql = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        out_columns = [out for _, out in table.columns]
        return [dict(zip(out_columns, row)) for row in emit.query(sql, ())]


def _print_rows(label: str, rows: "list[dict[str, object]]") -> None:
    print(label)
    for row in rows:
        print(
            f"  visit_id={row['visit_id']} role={row['role_name']}"
            f" actor_kind={row['actor_kind']} actor_id={row['actor_id']}"
            f" joined_at={row['joined_at']}"
        )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = build_source_test_emit(Path(tmp))

        verbatim_rows = _render_junction(emit_dir, None)
        _print_rows("No kind_labels (verbatim):", verbatim_rows)

        labeled_rows = _render_junction(emit_dir, {"actor": "clinician"})
        _print_rows("\nkind_labels={'actor': 'clinician'}:", labeled_rows)

        assert {r["actor_kind"] for r in verbatim_rows} == {"actor"}
        assert {r["actor_kind"] for r in labeled_rows} == {"clinician"}
        for before, after in zip(verbatim_rows, labeled_rows):
            assert before["visit_id"] == after["visit_id"]
            assert before["actor_id"] == after["actor_id"]
            assert before["joined_at"] == after["joined_at"]

        print("\nValidation rules:")
        try:
            _render_junction(emit_dir, {"ghost": "phantom"})
        except SourceKindLabelUnknown as exc:
            print(f"  REJECTED (unknown kind): {exc}")
        else:
            raise AssertionError("expected SourceKindLabelUnknown")

        try:
            _render_junction(emit_dir, {"actor": "location"})
        except SourceKindLabelCollision as exc:
            print(f"  REJECTED (label collides with kind): {exc}")
        else:
            raise AssertionError("expected SourceKindLabelCollision")

    print("\nSUCCESS: kind_labels validate and render junction member-kind values")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
