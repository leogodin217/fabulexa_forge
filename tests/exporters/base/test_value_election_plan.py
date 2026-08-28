"""Plan-time tests for the three new value-rendering elections (`decimal`,
`instant`, `json_precision`) on base-mode `render` declarations
(`docs/architecture/pending/value-rendering-elections.md` § Validation
Rules).

Structural-instant shorthand and `date_parse` plan-time behavior is already
covered by `test_plan.py`'s migrated `render` suite — this module tests only
what the unified map's three new typed forms add: the `RenderKeyResolves`
form-domain gate over a typed election, each election's own source-type gate
(`DecimalSourceIsDouble` / `InstantSourceIsBigint` /
`JsonPrecisionSourceIsVarchar`), `instant`'s anchor-required gate under the
typed form (base's optional anchor), and an elected source joining the
`slice_only` refusal surface. Sidecars are built in-memory via
`Sidecar.from_raw` (plan building reads only the sidecar), matching
`test_plan.py`'s convention.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from _support.notices import discard_notice_sink

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import (
    BaseConfig,
    BaseRenderDecl,
    DecimalElection,
    InstantElection,
    JsonPrecisionElection,
)
from fabulexa_forge.errors import (
    BaseRenameSliceOnly,
    BaseRenameUnresolved,
    DecimalSourceIsDouble,
    InstantSourceIsBigint,
    JsonPrecisionSourceIsVarchar,
    RenderKeyResolves,
    TemporalRenderRequiresAnchor,
)
from fabulexa_forge.exporters.base.plan import build_base_plan
from fabulexa_forge.reader.sidecar import Sidecar

#: A fixed effective anchor for render-election tests — the same shape
#: `resolve_effective_anchor` would produce for a UTC sidecar runtime.
_ANCHOR = EffectiveAnchor(
    start_instant=datetime(2024, 1, 1, tzinfo=timezone.utc), timezone=ZoneInfo("UTC")
)

# ---------------------------------------------------------------------------
# Sidecar-building helpers
# ---------------------------------------------------------------------------


def _col(
    name: str,
    type_: str = "VARCHAR",
    history_tracked: bool | None = None,
    temporal_class: str | None = None,
) -> dict[str, object]:
    """Build a raw sidecar column entry."""
    col: dict[str, object] = {"name": name, "type": type_}
    if history_tracked is not None:
        col["history_tracked"] = history_tracked
    if temporal_class is not None:
        col["temporal_class"] = temporal_class
    return col


def _sidecar(tables: list[dict[str, object]]) -> Sidecar:
    """Build a Sidecar directly from a raw base.json-shaped mapping."""
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "tables": tables,
    }
    return Sidecar.from_raw(raw)


def _value_election_sidecar() -> Sidecar:
    """A `widget` kind carrying one payload column per new election's
    required declared source type (DOUBLE for `decimal`, BIGINT for
    `instant`, VARCHAR for `json_precision`), plus a non-exempt slice_only
    VARCHAR payload for the elected-source/slice_only composition test."""
    widget_table: dict[str, object] = {
        "name": "records__widget",
        "category": "records",
        "record_kind": "widget",
        "columns": [
            _col("fork_path"),
            _col("record_id"),
            _col("created_sim_time", "BIGINT"),
            _col("active", "BOOLEAN"),
            _col("deactivated_at", "BIGINT"),
            _col("last_mutation_sim_time", "BIGINT"),
            _col(
                "prop__error_rate",
                "DOUBLE",
                history_tracked=False,
                temporal_class="constant",
            ),
            _col(
                "prop__requested_offset_ns",
                "BIGINT",
                history_tracked=False,
                temporal_class="constant",
            ),
            _col(
                "prop__context",
                "VARCHAR",
                history_tracked=False,
                temporal_class="constant",
            ),
            _col(
                "prop__notes",
                "VARCHAR",
                history_tracked=False,
                temporal_class="slice_only",
            ),
        ],
        "rows": 1,
    }
    return _sidecar(tables=[widget_table])


# ---------------------------------------------------------------------------
# `RenderKeyResolves`: a typed election naming a structural column
# ---------------------------------------------------------------------------


def test_typed_election_on_structural_column_refused() -> None:
    """A typed election naming an instant-carrying structural column raises
    RenderKeyResolves — a typed election's key domain is payload columns
    only, so no rendering ever has two spellings."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__widget",
                render={"created_sim_time": InstantElection(instant="timestamp")},
            )
        ]
    )
    with pytest.raises(RenderKeyResolves):
        build_base_plan(
            _value_election_sidecar(), config, discard_notice_sink, anchor=_ANCHOR
        )


# ---------------------------------------------------------------------------
# Source-type gates: `decimal` / `instant` / `json_precision`
# ---------------------------------------------------------------------------


def test_decimal_on_double_source_resolves() -> None:
    """`decimal` on a declared DOUBLE payload column resolves onto
    `spec.render`."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__widget",
                render={"prop__error_rate": DecimalElection(decimal=(6, 2))},
            )
        ]
    )
    plan = build_base_plan(
        _value_election_sidecar(), config, discard_notice_sink, anchor=_ANCHOR
    )
    spec = next(t for t in plan.tables if t.kind == "widget")
    assert spec.render == (("prop__error_rate", DecimalElection(decimal=(6, 2))),)


def test_decimal_on_non_double_source_refused() -> None:
    """`decimal` on a declared VARCHAR payload column raises
    DecimalSourceIsDouble."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__widget",
                render={"prop__context": DecimalElection(decimal=(6, 2))},
            )
        ]
    )
    with pytest.raises(DecimalSourceIsDouble):
        build_base_plan(
            _value_election_sidecar(), config, discard_notice_sink, anchor=_ANCHOR
        )


def test_instant_on_bigint_source_resolves() -> None:
    """`instant` on a declared BIGINT payload column resolves onto
    `spec.render`."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__widget",
                render={
                    "prop__requested_offset_ns": InstantElection(instant="timestamp")
                },
            )
        ]
    )
    plan = build_base_plan(
        _value_election_sidecar(), config, discard_notice_sink, anchor=_ANCHOR
    )
    spec = next(t for t in plan.tables if t.kind == "widget")
    assert spec.render == (
        ("prop__requested_offset_ns", InstantElection(instant="timestamp")),
    )


def test_instant_on_non_bigint_source_refused() -> None:
    """`instant` on a declared DOUBLE payload column raises
    InstantSourceIsBigint."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__widget",
                render={"prop__error_rate": InstantElection(instant="timestamp")},
            )
        ]
    )
    with pytest.raises(InstantSourceIsBigint):
        build_base_plan(
            _value_election_sidecar(), config, discard_notice_sink, anchor=_ANCHOR
        )


def test_json_precision_on_varchar_source_resolves() -> None:
    """`json_precision` on a declared VARCHAR payload column resolves onto
    `spec.render`."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__widget",
                render={
                    "prop__context": JsonPrecisionElection(json_precision={"x": 2})
                },
            )
        ]
    )
    plan = build_base_plan(
        _value_election_sidecar(), config, discard_notice_sink, anchor=_ANCHOR
    )
    spec = next(t for t in plan.tables if t.kind == "widget")
    assert spec.render == (
        ("prop__context", JsonPrecisionElection(json_precision={"x": 2})),
    )


def test_json_precision_on_non_varchar_source_refused() -> None:
    """`json_precision` on a declared BIGINT payload column raises
    JsonPrecisionSourceIsVarchar."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__widget",
                render={
                    "prop__requested_offset_ns": JsonPrecisionElection(
                        json_precision={"x": 2}
                    )
                },
            )
        ]
    )
    with pytest.raises(JsonPrecisionSourceIsVarchar):
        build_base_plan(
            _value_election_sidecar(), config, discard_notice_sink, anchor=_ANCHOR
        )


# ---------------------------------------------------------------------------
# Key resolution: non-existent column (typed form)
# ---------------------------------------------------------------------------


def test_typed_election_key_naming_non_existent_column_refused() -> None:
    """A typed election's key naming no column of the kind's table at all
    raises BaseRenameUnresolved."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__widget",
                render={
                    "prop__does_not_exist": DecimalElection(decimal=(4, 2)),
                },
            )
        ]
    )
    with pytest.raises(BaseRenameUnresolved):
        build_base_plan(
            _value_election_sidecar(), config, discard_notice_sink, anchor=_ANCHOR
        )


# ---------------------------------------------------------------------------
# `instant`'s anchor-required gate under the typed form
# ---------------------------------------------------------------------------


def test_instant_with_no_anchor_refused() -> None:
    """A typed `instant` election with no resolved anchor is refused —
    TemporalRenderRequiresAnchor, base's anchor is optional; the bare
    shorthand's identical refusal is covered in `test_plan.py`."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__widget",
                render={
                    "prop__requested_offset_ns": InstantElection(instant="timestamp")
                },
            )
        ]
    )
    with pytest.raises(TemporalRenderRequiresAnchor, match="prop__requested_offset_ns"):
        build_base_plan(_value_election_sidecar(), config, discard_notice_sink)


# ---------------------------------------------------------------------------
# An elected source joins the `slice_only` refusal surface
# ---------------------------------------------------------------------------


def test_json_precision_on_slice_only_source_refused() -> None:
    """A `json_precision` election naming a non-exempt slice_only column
    raises BaseRenameSliceOnly — the mode's omission posture composing with
    the new election's own refusal, exactly as `date_parse` already does."""
    config = BaseConfig(
        render=[
            BaseRenderDecl(
                table="records__widget",
                render={"prop__notes": JsonPrecisionElection(json_precision={"x": 2})},
            )
        ]
    )
    with pytest.raises(BaseRenameSliceOnly):
        build_base_plan(
            _value_election_sidecar(), config, discard_notice_sink, anchor=_ANCHOR
        )
