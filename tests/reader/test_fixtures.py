"""Tests for the base-layer emit fixtures.

Dogfoods the reader (open_emit, Sidecar) against all built fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from fabulexa_export.reader import (
    UnsupportedBaseFormatVersionError,
    conformance,
    open_emit,
)

if TYPE_CHECKING:
    from fabulexa_export.reader import Emit


def _records_table(emit: "Emit"):
    """Return the first records-category table from the emit's sidecar."""
    return next(t for t in emit.sidecar.tables() if t.category == "records")


class TestSpanning:
    """spanning exercises every reader surface."""

    def test_opens_without_error(self, base_fixtures: dict[str, Path]) -> None:
        """open_emit succeeds on the spanning fixture."""
        with open_emit(base_fixtures["spanning"]) as emit:
            assert emit.sidecar is not None

    def test_has_history_fixed_category(self, base_fixtures: dict[str, Path]) -> None:
        """Sidecar declares history as a fixed-category table (no firings)."""
        with open_emit(base_fixtures["spanning"]) as emit:
            table_names = {t.name for t in emit.sidecar.tables()}
            categories = {t.name: t.category for t in emit.sidecar.tables()}
        assert "history" in table_names
        assert categories["history"] == "fixed"
        assert "firings" not in table_names

    def test_history_has_six_base_columns_no_provenance(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """history carries exactly 6 base columns; no written_by_* provenance."""
        with open_emit(base_fixtures["spanning"]) as emit:
            col_names = [c.name for c in emit.sidecar.columns("history")]
        written_by_cols = [n for n in col_names if n.startswith("written_by_")]
        assert len(col_names) == 6
        assert len(written_by_cols) == 0

    def test_has_two_records_tables(self, base_fixtures: dict[str, Path]) -> None:
        """Sidecar declares exactly two records-category tables (records__actor, records__doctor)."""
        with open_emit(base_fixtures["spanning"]) as emit:
            records_tables = [
                t for t in emit.sidecar.tables() if t.category == "records"
            ]
        assert len(records_tables) == 2

    def test_has_one_membership_table(self, base_fixtures: dict[str, Path]) -> None:
        """Sidecar declares exactly one membership-category table (membership__actor__appointments)."""
        with open_emit(base_fixtures["spanning"]) as emit:
            membership_tables = [
                t for t in emit.sidecar.tables() if t.category == "membership"
            ]
        assert len(membership_tables) == 1

    def test_records_table_has_references_annotated_column(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """A records table has at least one references-annotated prop__ column."""
        with open_emit(base_fixtures["spanning"]) as emit:
            records_table = _records_table(emit)
            ref_cols = [c for c in records_table.columns if c.references is not None]
        assert len(ref_cols) == 1

    def test_records_table_has_no_provenance_columns(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """Sanitised fixture: records table carries no created_by_* or deactivated_by_* columns."""
        with open_emit(base_fixtures["spanning"]) as emit:
            records_table = _records_table(emit)
            col_names = [c.name for c in records_table.columns]
        created_cols = [n for n in col_names if n.startswith("created_by_")]
        deactivated_cols = [n for n in col_names if n.startswith("deactivated_by_")]
        assert len(created_cols) == 0
        assert len(deactivated_cols) == 0

    def test_membership_table_has_elem_and_member_columns(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """A membership table has elem__* and member__*__kind/id columns."""
        with open_emit(base_fixtures["spanning"]) as emit:
            mem_table = next(
                t for t in emit.sidecar.tables() if t.category == "membership"
            )
            col_names = [c.name for c in mem_table.columns]
        elem_cols = [n for n in col_names if n.startswith("elem__")]
        member_kind_cols = [n for n in col_names if n.endswith("__kind")]
        member_id_cols = [
            n for n in col_names if n.endswith("__id") and "member__" in n
        ]
        assert len(elem_cols) == 1
        assert len(member_kind_cols) == 1
        assert len(member_id_cols) == 1

    def test_pinned_ids_non_empty(self, base_fixtures: dict[str, Path]) -> None:
        """pinned_ids() returns a non-empty mapping."""
        with open_emit(base_fixtures["spanning"]) as emit:
            pinned = emit.sidecar.pinned_ids()
        assert len(pinned) == 1

    def test_runtime_present(self, base_fixtures: dict[str, Path]) -> None:
        """runtime() returns a non-None RuntimeAnchor."""
        with open_emit(base_fixtures["spanning"]) as emit:
            rt = emit.sidecar.runtime()
        assert rt is not None

    def test_enum_domains_non_empty(self, base_fixtures: dict[str, Path]) -> None:
        """enum_domains() returns a non-empty mapping."""
        with open_emit(base_fixtures["spanning"]) as emit:
            domains = emit.sidecar.enum_domains()
        assert len(domains) == 1

    def test_records_table_has_closed_domain_status_column(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """records table has prop__status (the closed-domain column in enum_domains)."""
        with open_emit(base_fixtures["spanning"]) as emit:
            records_table = _records_table(emit)
            col_names = [c.name for c in records_table.columns]
        assert "prop__status" in col_names

    def test_record_roles_is_present(self, base_fixtures: dict[str, Path]) -> None:
        """record_roles() returns a non-None RecordRoles on the spanning fixture."""
        with open_emit(base_fixtures["spanning"]) as emit:
            roles = emit.sidecar.record_roles()
        assert roles is not None
        assert len(roles.kinds()) == 2


class TestHistorySeries:
    """history_series carries multi-event series and is C1-C12 conformant."""

    def test_passes_all_conformance_checks(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """conformance.validate reports every check passing."""
        with open_emit(base_fixtures["history_series"]) as emit:
            report = conformance.validate(emit)
        assert report.ok, [r for r in report.results if not r.passed]

    def test_at_least_two_series_with_at_least_two_events(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """At least two distinct (kind, record_id, property) series each carry
        >= 2 history rows."""
        with open_emit(base_fixtures["history_series"]) as emit:
            rows = emit.query(
                "SELECT kind, record_id, property, COUNT(*) FROM history "
                "GROUP BY kind, record_id, property",
                (),
            )
        multi_event_series = [r for r in rows if r[3] >= 2]
        assert len(multi_event_series) >= 2

    def test_one_series_has_at_least_four_events(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """At least one series carries >= 4 history rows (a real freeze-cut range)."""
        with open_emit(base_fixtures["history_series"]) as emit:
            rows = emit.query(
                "SELECT kind, record_id, property, COUNT(*) FROM history "
                "GROUP BY kind, record_id, property",
                (),
            )
        assert any(r[3] >= 4 for r in rows)

    def test_at_least_one_event_past_slice_at(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """At least one history row's sim_time exceeds the branch's slice_at."""
        with open_emit(base_fixtures["history_series"]) as emit:
            slice_at = emit.sidecar.branches()[0].slice_at
            rows = emit.query(
                "SELECT sim_time FROM history WHERE sim_time > ?", (slice_at,)
            )
        assert len(rows) >= 1

    def test_sidecar_rows_match_table_contents(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """Every sidecar `rows` count matches the table's actual row count."""
        with open_emit(base_fixtures["history_series"]) as emit:
            for table in emit.sidecar.tables():
                actual = emit.query(f'SELECT COUNT(*) FROM "{table.name}"', ())[0][0]
                assert actual == table.rows, table.name


class TestWrongVersion:
    """wrong_version fixture raises UnsupportedBaseFormatVersionError."""

    def test_raises_unsupported_version(self, base_fixtures: dict[str, Path]) -> None:
        """open_emit raises UnsupportedBaseFormatVersionError for wrong version."""
        with pytest.raises(UnsupportedBaseFormatVersionError):
            open_emit(base_fixtures["wrong_version"])


class TestNegativeFixturesOpen:
    """All non-version fixtures open without raising (structural floor is met)."""

    _NEGATIVE_NAMES = [
        "c4_wrong_history_type",
        "c5_prop_missing",
        "c7_half_null_member",
        "c12_missing_kind",
        "c12_missing_subtype",
        "wrong_version",
        "schema_mismatch",
        "history_duplicate_tick",
        "refs_dangling",
    ]

    @pytest.mark.parametrize(
        "name",
        [n for n in _NEGATIVE_NAMES if n != "wrong_version"],
    )
    def test_fixture_opens(self, name: str, base_fixtures: dict[str, Path]) -> None:
        """Each defective fixture opens successfully; validate (not open_emit) catches it."""
        with open_emit(base_fixtures[name]) as emit:
            assert emit.sidecar is not None

    def test_schema_mismatch_sidecar_declares_phantom_column(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """schema_mismatch: sidecar declares prop__phantom_column absent in DuckDB."""
        with open_emit(base_fixtures["schema_mismatch"]) as emit:
            records_table = _records_table(emit)
            col_names = [c.name for c in records_table.columns]
        assert "prop__phantom_column" in col_names

    def test_c5_prop_missing_sidecar_declares_prop_absent_in_db(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """c5_prop_missing: sidecar declares prop__name absent in DuckDB."""
        with open_emit(base_fixtures["c5_prop_missing"]) as emit:
            records_table = _records_table(emit)
            sidecar_cols = [c.name for c in records_table.columns]
            db_rows = emit.query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'records__actor'",
                (),
            )
        db_col_names = [r[0] for r in db_rows]
        assert "prop__name" in sidecar_cols
        assert "prop__name" not in db_col_names


class TestBuilderDeterminism:
    """Re-building fixtures into a second dir yields identical logical content."""

    def test_spanning_sidecar_is_stable(self, tmp_path: Path) -> None:
        """Building spanning twice produces identical sidecar JSON."""
        from ._fixtures_build import build_spanning

        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        build_spanning(dir_a)
        build_spanning(dir_b)

        sidecar_a = json.loads((dir_a / "base.json").read_text(encoding="utf-8"))
        sidecar_b = json.loads((dir_b / "base.json").read_text(encoding="utf-8"))
        assert sidecar_a == sidecar_b

    def test_spanning_row_sets_are_stable(self, tmp_path: Path) -> None:
        """Building spanning twice yields identical row sets via the reader."""
        from ._fixtures_build import build_spanning

        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        build_spanning(dir_a)
        build_spanning(dir_b)

        with open_emit(dir_a) as ea, open_emit(dir_b) as eb:
            rows_a = ea.query("SELECT * FROM history ORDER BY sim_time", ())
            rows_b = eb.query("SELECT * FROM history ORDER BY sim_time", ())
        assert rows_a == rows_b
