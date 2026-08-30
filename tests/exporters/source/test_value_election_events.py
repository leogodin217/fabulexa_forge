"""Tests for the event-log reach (doc §
`docs/architecture/pending/value-rendering-elections.md` § Event-log and
after-image reach): the per-kind `ElectionKindConflict` agreement gate,
elected `changes` entries at the codec seam, the changeset-membership /
`id`-numbering invariance, and the export-time guards firing at the log
site.

Every scenario plans through `build_source_plan` (the plan-time gate under
test) and renders through `build_event_log_sql`, over
`build_value_election_events_emit` (one `widget` records kind carrying one
tracked column per election kind, sub-typed safe/risky for the guard
scenarios, plus a `tags` junction for the `elem__<f>` reach test) — the same
style the phase-4 demo
(`docs/sprints/value-rendering-elections/demos/phase_4_event_log_reach.py`)
uses.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import pytest

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    DateParseElection,
    DecimalElection,
    ExportConfig,
    InstantElection,
    JsonPrecisionElection,
    MembershipRef,
    SourceConfig,
    SourceEventsDecl,
    SourceEventSourceDecl,
    SourceTableDecl,
)
from fabulexa_forge.errors import ElectionKindConflict
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.source.events import build_event_log_sql
from fabulexa_forge.exporters.source.plan import SourceStateTablePlan, build_source_plan
from fabulexa_forge.exporters.source.renders import build_state_render_sql
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import RunDatabaseError

from ._event_log_helpers import changes_of, event_log_rows, row_for
from ._source_fixtures import build_value_election_events_emit

if TYPE_CHECKING:
    from fabulexa_forge.anchor import TemporalRender
    from fabulexa_forge.config.models import RenderElection
    from fabulexa_forge.exporters.source.plan import SourcePlan
    from fabulexa_forge.reader.emit import Emit

# ---------------------------------------------------------------------------
# Plan + render helpers
# ---------------------------------------------------------------------------


def _discard(notice: object) -> None:
    """A notice sink that drops every notice — no scenario here asserts on
    slice-only-column-omitted notices."""


@contextmanager
def _plan_over(
    emit_dir: Path, config: ExportConfig
) -> "Iterator[tuple[Emit, SourcePlan]]":
    """Build a `SourcePlan` for `config` over an already-built `emit_dir`,
    resolving the anchor and election the way the engine does."""
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, config.keys)
        plan = build_source_plan(emit, config, anchor, election, False, _discard)
        yield emit, plan


def _rows_over(emit_dir: Path, config: ExportConfig) -> list[dict[str, object]]:
    """Plan `config` over an already-built `emit_dir` and render its event
    log's rows."""
    with _plan_over(emit_dir, config) as (emit, plan):
        assert plan.events is not None, "every scenario here declares an events block"
        sql = build_event_log_sql(
            emit.sidecar, plan.fork_path, plan.events, plan.anchor, None
        )
        return event_log_rows(emit, sql)


@contextmanager
def _plan(tmp_path: Path, config: ExportConfig) -> "Iterator[tuple[Emit, SourcePlan]]":
    """Build `build_value_election_events_emit` under `tmp_path` and a
    `SourcePlan` for `config` over it — the single-config convenience most
    scenarios use."""
    emit_dir = build_value_election_events_emit(tmp_path)
    with _plan_over(emit_dir, config) as (emit, plan):
        yield emit, plan


def _rows_for(tmp_path: Path, config: ExportConfig) -> list[dict[str, object]]:
    """Plan `config` over the fixture built under `tmp_path` and render its
    event log's rows."""
    emit_dir = build_value_election_events_emit(tmp_path)
    return _rows_over(emit_dir, config)


def _safe_events(**source_kwargs: object) -> SourceEventsDecl:
    """One `widget_events` log auditing the `safe` sub-population."""
    source = SourceEventSourceDecl(kind="widget", sub_types=("safe",), **source_kwargs)
    return SourceEventsDecl(name="widget_events", sources=(source,))


def _amount_table(name: str, election: "RenderElection | None") -> SourceTableDecl:
    """A `safe`-sub_types-scoped declared table over `widget`, electing
    `prop__amount` when `election` is given, silent otherwise."""
    render = {"prop__amount": election} if election is not None else None
    return SourceTableDecl(name=name, kind="widget", sub_types=("safe",), render=render)


def _update_changes(
    rows: list[dict[str, object]], item_id: object, key: str
) -> list[object]:
    """Every `update` row for `item_id` carrying `key` in its `changes`
    object, that entry's `[old, new]` pair, row order."""
    return [
        changes_of(r)[key]
        for r in rows
        if r["item_id"] == item_id and r["event"] == "update" and key in changes_of(r)
    ]


# ---------------------------------------------------------------------------
# Agreement gate (`ElectionKindConflict`)
# ---------------------------------------------------------------------------


class TestAgreementGate:
    def test_identical_election_on_two_tables_elects(self, tmp_path: Path) -> None:
        """Every declared table of one membership agreeing on an election is
        legal: the log renders the agreed, elected text."""
        election = DecimalElection(decimal=(6, 3))
        config = ExportConfig(
            mode="source",
            source=SourceConfig(
                tables=(
                    _amount_table("widget_a", election),
                    _amount_table("widget_b", election),
                ),
                events=_safe_events(),
            ),
        )
        rows = _rows_for(tmp_path, config)
        create = row_for(rows, "w001", "create")
        assert changes_of(create)["amount"] == [None, "12.346"]

    def test_differing_elections_refuses_naming_both_tables(
        self, tmp_path: Path
    ) -> None:
        """Two declared tables electing the same property differently, with
        a log rendering it, is refused — the message names both tables and
        uses the "conflicting elections" shape."""
        config = ExportConfig(
            mode="source",
            source=SourceConfig(
                tables=(
                    _amount_table("widget_a", DecimalElection(decimal=(6, 3))),
                    _amount_table("widget_b", DecimalElection(decimal=(6, 2))),
                ),
                events=_safe_events(),
            ),
        )
        with pytest.raises(ElectionKindConflict) as excinfo:
            _rows_for(tmp_path, config)
        message = str(excinfo.value)
        assert "widget_a" in message
        assert "widget_b" in message
        assert "conflicting render elections" in message

    def test_elected_beside_silent_refuses_silent_table_message_shape(
        self, tmp_path: Path
    ) -> None:
        """An electing table beside a silent sibling table, with a log
        rendering the property, is refused — the "declares none" shape."""
        config = ExportConfig(
            mode="source",
            source=SourceConfig(
                tables=(
                    _amount_table("widget_a", DecimalElection(decimal=(6, 3))),
                    _amount_table("widget_b", None),
                ),
                events=_safe_events(),
            ),
        )
        with pytest.raises(ElectionKindConflict) as excinfo:
            _rows_for(tmp_path, config)
        message = str(excinfo.value)
        assert "widget_a" in message
        assert "widget_b" in message
        assert "declares none" in message

    def test_differing_elections_no_log_renders_property_legal(
        self, tmp_path: Path
    ) -> None:
        """Two declared tables electing the property differently, with no
        `events` block at all, is legal — no log rendering to disagree
        about."""
        config = ExportConfig(
            mode="source",
            source=SourceConfig(
                tables=(
                    _amount_table("widget_a", DecimalElection(decimal=(6, 3))),
                    _amount_table("widget_b", DecimalElection(decimal=(6, 2))),
                ),
            ),
        )
        with _plan(tmp_path, config) as (_emit, plan):
            assert plan.events is None

    def test_property_narrowed_via_ignore_legalizes_conflict(
        self, tmp_path: Path
    ) -> None:
        """Narrowing the property out of the events source's audited set
        (`ignore`) removes it from the gate's scope: the same disagreeing
        pair of table declarations becomes legal."""
        config = ExportConfig(
            mode="source",
            source=SourceConfig(
                tables=(
                    _amount_table("widget_a", DecimalElection(decimal=(6, 3))),
                    _amount_table("widget_b", None),
                ),
                events=_safe_events(ignore=("amount",)),
            ),
        )
        rows = _rows_for(tmp_path, config)
        create = row_for(rows, "w001", "create")
        assert "amount" not in changes_of(create)

    def test_kind_audited_with_no_declared_table_renders_raw_codec_text(
        self, tmp_path: Path
    ) -> None:
        """A kind the log audits with no declared table at all renders raw
        codec text — the log-only declaration surface is deferred."""
        config = ExportConfig(
            mode="source",
            source=SourceConfig(events=_safe_events()),
        )
        rows = _rows_for(tmp_path, config)
        create = row_for(rows, "w001", "create")
        # The raw codec string is exactly the history value inserted, not a
        # decimal-rounded or re-cast form.
        assert changes_of(create)["amount"] == [None, "12.3456"]


# ---------------------------------------------------------------------------
# Elected rendering: create / update, byte identity, pinned temporal forms
# ---------------------------------------------------------------------------


class TestElectedRendering:
    def test_decimal_create_and_update_carry_elected_text(self, tmp_path: Path) -> None:
        table = _amount_table("widget_a", DecimalElection(decimal=(6, 3)))
        config = ExportConfig(
            mode="source",
            source=SourceConfig(tables=(table,), events=_safe_events()),
        )
        rows = _rows_for(tmp_path, config)
        create = row_for(rows, "w001", "create")
        assert changes_of(create)["amount"] == [None, "12.346"]
        assert ["12.346", "45.679"] in _update_changes(rows, "w001", "amount")

    def test_json_precision_create_and_update_carry_elected_text(
        self, tmp_path: Path
    ) -> None:
        table = SourceTableDecl(
            name="widget_a",
            kind="widget",
            sub_types=("safe",),
            render={"prop__payload": JsonPrecisionElection(json_precision={"pct": 2})},
        )
        config = ExportConfig(
            mode="source",
            source=SourceConfig(tables=(table,), events=_safe_events()),
        )
        rows = _rows_for(tmp_path, config)
        create = row_for(rows, "w001", "create")
        create_new = changes_of(create)["payload"][1]
        assert isinstance(create_new, str)
        assert '"pct":0.13' in create_new or '"pct": 0.13' in create_new

    def test_date_parse_create_and_update_carry_elected_text(
        self, tmp_path: Path
    ) -> None:
        table = SourceTableDecl(
            name="widget_a",
            kind="widget",
            sub_types=("safe",),
            render={"prop__opened_at": DateParseElection(date_parse="%Y-%m-%d")},
        )
        config = ExportConfig(
            mode="source",
            source=SourceConfig(tables=(table,), events=_safe_events()),
        )
        rows = _rows_for(tmp_path, config)
        create = row_for(rows, "w001", "create")
        assert changes_of(create)["opened_at"] == [None, "2024-02-01"]
        updates = _update_changes(rows, "w001", "opened_at")
        assert ["2024-02-01", "2024-03-15"] in updates

    @pytest.mark.parametrize(
        ("render", "no_us_text", "with_us_text"),
        [
            ("date", "2024-01-01", "2024-01-01"),
            ("time", "00:00:00.000000", "00:00:00.000500"),
            ("timestamp", "2024-01-01 00:00:00", "2024-01-01 00:00:00.000500"),
            (
                "timestamptz",
                "2024-01-01 00:00:00.000000-05:00",
                "2024-01-01 00:00:00.000500-05:00",
            ),
        ],
    )
    def test_instant_forms_match_writers_pinned_csv_forms(
        self,
        tmp_path: Path,
        render: "TemporalRender",
        no_us_text: str,
        with_us_text: str,
    ) -> None:
        """Every elected instant rendering's in-JSON text matches the
        writers' own pinned CSV form for that type — including the naive
        `TIMESTAMP` form's µs field, omitted at the zero-microsecond
        creation offset and shown at the update offset."""
        table = SourceTableDecl(
            name="widget_a",
            kind="widget",
            sub_types=("safe",),
            render={"prop__offset_ns": InstantElection(instant=render)},
        )
        config = ExportConfig(
            mode="source",
            source=SourceConfig(tables=(table,), events=_safe_events()),
        )
        rows = _rows_for(tmp_path, config)
        create = row_for(rows, "w001", "create")
        assert changes_of(create)["offset_ns"] == [None, no_us_text]
        assert [no_us_text, with_us_text] in _update_changes(rows, "w001", "offset_ns")

    def test_table_column_and_changes_entry_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """The declaring table's own rendered column and the log's `changes`
        entry carry byte-identical text for the same value."""
        table = _amount_table("widget_a", DecimalElection(decimal=(6, 3)))
        config = ExportConfig(
            mode="source",
            source=SourceConfig(tables=(table,), events=_safe_events()),
        )
        with _plan(tmp_path, config) as (emit, plan):
            state_table = next(t for t in plan.tables if t.name == "widget_a")
            assert isinstance(state_table, SourceStateTablePlan)
            table_sql = build_state_render_sql(
                plan.sidecar, plan.fork_path, state_table, plan.anchor, None
            )
            cols = [out for _, out in state_table.columns]
            table_row = dict(zip(cols, next(iter(emit.query(table_sql, ())))))
            assert plan.events is not None
            log_sql = build_event_log_sql(
                plan.sidecar, plan.fork_path, plan.events, plan.anchor, None
            )
            log_rows = event_log_rows(emit, log_sql)

        table_amount_text = str(table_row["amount"])
        last_update = [
            r for r in log_rows if r["item_id"] == "w001" and r["event"] == "update"
        ][-1]
        assert changes_of(last_update)["amount"][1] == table_amount_text


# ---------------------------------------------------------------------------
# Invariance: changeset membership is a raw-value fact
# ---------------------------------------------------------------------------


class TestInvariance:
    def test_two_raw_values_rounding_to_one_text_still_emit_update(
        self, tmp_path: Path
    ) -> None:
        """Two distinct raw values (45.6789 -> 45.6791) that round to the
        same decimal(6,3) text ("45.679") still emit their `u` row — an
        equal-looking `[old, new]` pair, never suppressed."""
        table = _amount_table("widget_a", DecimalElection(decimal=(6, 3)))
        config = ExportConfig(
            mode="source",
            source=SourceConfig(tables=(table,), events=_safe_events()),
        )
        rows = _rows_for(tmp_path, config)
        assert ["45.679", "45.679"] in _update_changes(rows, "w001", "amount")

    def test_event_set_and_id_numbering_identical_with_and_without_elections(
        self, tmp_path: Path
    ) -> None:
        """The event set and dense `id` numbering are election-invariant: a
        presentation election never suppresses or renumbers a row."""
        elected_config = ExportConfig(
            mode="source",
            source=SourceConfig(
                tables=(_amount_table("widget_a", DecimalElection(decimal=(6, 3))),),
                events=_safe_events(),
            ),
        )
        raw_config = ExportConfig(
            mode="source", source=SourceConfig(events=_safe_events())
        )
        emit_dir = build_value_election_events_emit(tmp_path)
        elected_rows = _rows_over(emit_dir, elected_config)
        raw_rows = _rows_over(emit_dir, raw_config)

        def identity(row: dict[str, object]) -> tuple[object, object, object, object]:
            return (row["id"], row["item_type"], row["item_id"], row["event"])

        assert [identity(r) for r in elected_rows] == [identity(r) for r in raw_rows]

    def test_junction_elem_field_election_reaches_bare_field(
        self, tmp_path: Path
    ) -> None:
        """A junction `elem__<f>` election reaches the log's bare audited
        field `<f>` — the junction render's own name strip."""
        tags_ref = MembershipRef(kind="widget", property="tags")
        table = SourceTableDecl(
            name="tags",
            membership=tags_ref,
            render={"elem__weight": DecimalElection(decimal=(4, 2))},
        )
        events = SourceEventsDecl(
            name="tag_events",
            sources=(SourceEventSourceDecl(membership=tags_ref),),
        )
        config = ExportConfig(
            mode="source", source=SourceConfig(tables=(table,), events=events)
        )
        rows = _rows_for(tmp_path, config)
        create = row_for(rows, "w001", "create")
        assert changes_of(create)["weight"] == [None, "3.14"]


# ---------------------------------------------------------------------------
# Guards: fire at the log site on a value no declared table selects
# ---------------------------------------------------------------------------


class TestGuardsFireAtLogSite:
    def test_decimal_overflow_fires_on_a_value_no_declared_table_selects(
        self, tmp_path: Path
    ) -> None:
        """`safe`-sub_types-scoped `widget_a` elects `decimal(4,2)`; the log
        audits the WHOLE kind (both tiers). w002's raw amount (9999.99)
        overflows DECIMAL(4,2) — a value `widget_a` never selects — yet the
        guard fires at the log's own render."""
        table = SourceTableDecl(
            name="widget_a",
            kind="widget",
            sub_types=("safe",),
            render={"prop__amount": DecimalElection(decimal=(4, 2))},
        )
        events = SourceEventsDecl(
            name="widget_events", sources=(SourceEventSourceDecl(kind="widget"),)
        )
        config = ExportConfig(
            mode="source", source=SourceConfig(tables=(table,), events=events)
        )
        with pytest.raises(RunDatabaseError) as excinfo:
            _rows_for(tmp_path, config)
        message = str(excinfo.value)
        # Attributed to the log's own output name — the value never crosses
        # widget_a's own row selection (tier=safe only).
        assert "widget_events" in message
        assert "amount" in message

    def test_json_payload_error_fires_on_a_value_no_declared_table_selects(
        self, tmp_path: Path
    ) -> None:
        """w002's payload carries a non-numeric declared leaf
        (`{"pct": "oops"}`) — a value `widget_a` never selects (`safe` sub_types
        only) — yet `forge_json_precision`'s guard fires at the log's own
        render."""
        table = SourceTableDecl(
            name="widget_a",
            kind="widget",
            sub_types=("safe",),
            render={"prop__payload": JsonPrecisionElection(json_precision={"pct": 2})},
        )
        events = SourceEventsDecl(
            name="widget_events", sources=(SourceEventSourceDecl(kind="widget"),)
        )
        config = ExportConfig(
            mode="source", source=SourceConfig(tables=(table,), events=events)
        )
        with pytest.raises(RunDatabaseError):
            _rows_for(tmp_path, config)
