"""Plan-time tests for the three new value-rendering elections (`decimal`,
`instant`, `json_precision`) on source declared tables
(`docs/architecture/pending/value-rendering-elections.md` § Validation
Rules).

Structural-instant shorthand and `date_parse` plan-time behavior is already
covered by `test_plan.py`'s migrated `render` suite — this module tests only
what the unified map's three new typed forms add: the `RenderKeyResolves`
form-domain gate over a typed election, each election's own source-type
gate (`DecimalSourceIsDouble` / `InstantSourceIsBigint` /
`JsonPrecisionSourceIsVarchar`), the junction `elem__<f>` / member-pair-column
key domain, and an elected source joining the `slice_only` refusal surface.
Every fixture is `_source_fixtures.build_source_test_emit` (already carrying
a DOUBLE, a BIGINT, and several VARCHAR payload columns) or
`build_slice_only_source_emit` for the slice_only composition case.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from _support.notices import discard_notice_sink

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    DecimalElection,
    ExportConfig,
    InstantElection,
    JsonPrecisionElection,
    MembershipRef,
    SourceConfig,
    SourceTableDecl,
)
from fabulexa_forge.errors import (
    DecimalSourceIsDouble,
    InstantSourceIsBigint,
    JsonPrecisionSourceIsVarchar,
    RenderKeyResolves,
    SourceColumnUnresolved,
    SourceSliceOnlyRead,
)
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.source.plan import (
    SourceJunctionTablePlan,
    SourceStateTablePlan,
    build_source_plan,
)
from fabulexa_forge.reader.emit import open_emit

from ._source_fixtures import build_slice_only_source_emit, build_source_test_emit

if TYPE_CHECKING:
    from fabulexa_forge.exporters.source.plan import SourcePlan

# ---------------------------------------------------------------------------
# Config + plan-build helpers
# ---------------------------------------------------------------------------


def _config(tables: "tuple[SourceTableDecl, ...]") -> ExportConfig:
    """Build a `mode: source` ExportConfig from a declared table set."""
    return ExportConfig(mode="source", source=SourceConfig(tables=tables))


def _open_plan(emit_dir: Path, config: ExportConfig) -> "SourcePlan":
    """Open `emit_dir` and build a SourcePlan, resolving the anchor and
    election the way the engine does."""
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None, "every fixture here declares a runtime anchor"
        election = resolve_election(emit.sidecar, config.keys)
        return build_source_plan(
            emit, config, anchor, election, False, discard_notice_sink
        )


# ---------------------------------------------------------------------------
# `RenderKeyResolves`: a typed election naming a structural column
# ---------------------------------------------------------------------------


def test_typed_election_on_structural_column_refused(tmp_path: Path) -> None:
    """A typed election naming an instant-carrying structural column raises
    RenderKeyResolves — a typed election's key domain is payload columns
    only, so no rendering ever has two spellings."""
    with pytest.raises(RenderKeyResolves):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                (
                    SourceTableDecl(
                        name="visits",
                        kind="visit",
                        render={
                            "created_sim_time": InstantElection(instant="timestamp")
                        },
                    ),
                )
            ),
        )


# ---------------------------------------------------------------------------
# Source-type gates: `decimal` / `instant` / `json_precision`
# ---------------------------------------------------------------------------


def test_decimal_on_double_source_resolves(tmp_path: Path) -> None:
    """`decimal` on a declared DOUBLE payload column resolves onto
    `table.render`."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            (
                SourceTableDecl(
                    name="orders",
                    kind="order",
                    render={"prop__amount": DecimalElection(decimal=(6, 2))},
                ),
            )
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert table.render == (("prop__amount", DecimalElection(decimal=(6, 2))),)


def test_decimal_on_non_double_source_refused(tmp_path: Path) -> None:
    """`decimal` on a declared VARCHAR payload column raises
    DecimalSourceIsDouble."""
    with pytest.raises(DecimalSourceIsDouble):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                (
                    SourceTableDecl(
                        name="orders",
                        kind="order",
                        render={"prop__location_id": DecimalElection(decimal=(6, 2))},
                    ),
                )
            ),
        )


def test_instant_on_bigint_source_resolves(tmp_path: Path) -> None:
    """`instant` on a declared BIGINT payload column resolves onto
    `table.render`."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            (
                SourceTableDecl(
                    name="visits",
                    kind="visit",
                    render={"prop__priority": InstantElection(instant="timestamp")},
                ),
            )
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert table.render == (("prop__priority", InstantElection(instant="timestamp")),)


def test_instant_on_non_bigint_source_refused(tmp_path: Path) -> None:
    """`instant` on a declared DOUBLE payload column raises
    InstantSourceIsBigint."""
    with pytest.raises(InstantSourceIsBigint):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                (
                    SourceTableDecl(
                        name="orders",
                        kind="order",
                        render={"prop__amount": InstantElection(instant="timestamp")},
                    ),
                )
            ),
        )


def test_json_precision_on_varchar_source_resolves(tmp_path: Path) -> None:
    """`json_precision` on a declared VARCHAR payload column resolves onto
    `table.render`."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            (
                SourceTableDecl(
                    name="locs",
                    kind="location",
                    render={
                        "prop__name": JsonPrecisionElection(json_precision={"x": 2})
                    },
                ),
            )
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert table.render == (
        ("prop__name", JsonPrecisionElection(json_precision={"x": 2})),
    )


def test_json_precision_on_non_varchar_source_refused(tmp_path: Path) -> None:
    """`json_precision` on a declared DOUBLE payload column raises
    JsonPrecisionSourceIsVarchar."""
    with pytest.raises(JsonPrecisionSourceIsVarchar):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                (
                    SourceTableDecl(
                        name="orders",
                        kind="order",
                        render={
                            "prop__amount": JsonPrecisionElection(
                                json_precision={"x": 2}
                            )
                        },
                    ),
                )
            ),
        )


# ---------------------------------------------------------------------------
# Key resolution: omitted / non-existent columns (typed form)
# ---------------------------------------------------------------------------


def test_typed_election_key_on_columns_omitted_column_refused(tmp_path: Path) -> None:
    """A typed election's key naming a real column the table's `columns`
    selection omits is refused — the shared two-stage gate composing for a
    typed form exactly as it does for the shorthand form."""
    with pytest.raises(
        SourceColumnUnresolved, match="not among this table's projected columns"
    ):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                (
                    SourceTableDecl(
                        name="orders",
                        kind="order",
                        columns=("prop__amount",),
                        render={"prop__location_id": DecimalElection(decimal=(4, 2))},
                    ),
                )
            ),
        )


def test_typed_election_key_naming_non_existent_column_refused(
    tmp_path: Path,
) -> None:
    """A typed election's key naming no real column of the source at all is
    refused, distinctly from the omitted-column case."""
    with pytest.raises(SourceColumnUnresolved, match="not a column of its source"):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                (
                    SourceTableDecl(
                        name="orders",
                        kind="order",
                        render={
                            "prop__does_not_exist": DecimalElection(decimal=(4, 2))
                        },
                    ),
                )
            ),
        )


# ---------------------------------------------------------------------------
# Junction: `elem__<f>` typed-key domain, member pair columns refused
# ---------------------------------------------------------------------------


def test_junction_typed_election_addresses_elem_field(tmp_path: Path) -> None:
    """A typed election keys the junction's `elem__<f>` element column — the
    junction's own source identity, exactly as `columns`/`rename` and
    `date_parse` address it."""
    plan = _open_plan(
        build_source_test_emit(tmp_path),
        _config(
            (
                SourceTableDecl(
                    name="visit_team",
                    membership=MembershipRef(kind="visit", property="team"),
                    render={
                        "elem__role_name": JsonPrecisionElection(
                            json_precision={"x": 2}
                        )
                    },
                ),
            )
        ),
    )
    table = plan.tables[0]
    assert isinstance(table, SourceJunctionTablePlan)
    assert table.render == (
        ("elem__role_name", JsonPrecisionElection(json_precision={"x": 2})),
    )


def test_junction_typed_election_on_member_pair_column_refused(
    tmp_path: Path,
) -> None:
    """A typed election naming a `member__<f>__kind`/`__id` pair column is
    refused — reference identity is key election's surface, outside the
    typed-election key domain."""
    with pytest.raises(RenderKeyResolves):
        _open_plan(
            build_source_test_emit(tmp_path),
            _config(
                (
                    SourceTableDecl(
                        name="visit_team",
                        membership=MembershipRef(kind="visit", property="team"),
                        render={
                            "member__actor__kind": JsonPrecisionElection(
                                json_precision={"x": 2}
                            )
                        },
                    ),
                )
            ),
        )


# ---------------------------------------------------------------------------
# An elected source joins the `slice_only` refusal surface
# ---------------------------------------------------------------------------


def test_json_precision_on_slice_only_source_refused(tmp_path: Path) -> None:
    """A `json_precision` election naming a non-exempt slice_only column
    raises SourceSliceOnlyRead — the mode's omission posture composing with
    the new election's own refusal, exactly as `date_parse` already does."""
    with pytest.raises(SourceSliceOnlyRead):
        _open_plan(
            build_slice_only_source_emit(tmp_path),
            _config(
                (
                    SourceTableDecl(
                        name="patients",
                        kind="patient",
                        render={
                            "prop__loyalty_tier": JsonPrecisionElection(
                                json_precision={"x": 2}
                            )
                        },
                    ),
                )
            ),
        )
