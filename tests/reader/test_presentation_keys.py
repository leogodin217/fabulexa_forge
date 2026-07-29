"""Tests for the presentation_keys typed view, strict accessor, and the
union-safety algebra (KeySpace, PartitionKey, WholeColumnClaim,
PresentationKeys, Sidecar.presentation_keys(), union_safe, combined_claim).

Raw-sidecar-dict helpers in the test_sidecar.py style: Sidecar.from_raw is
exercised directly against hand-built dicts, bypassing the vendored JSON
Schema (that is C1's job) so a fixture can express exactly the one
coherence defect a test targets.
"""

from __future__ import annotations

import pytest

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.reader.errors import PresentationKeysInvalidError
from fabulexa_forge.reader.sidecar import (
    KeySpace,
    PartitionKey,
    Sidecar,
    WholeColumnClaim,
    combined_claim,
    union_safe,
)

# ---------------------------------------------------------------------------
# Raw-dict helpers (sidecar JSON shape, for Sidecar.from_raw fixtures)
# ---------------------------------------------------------------------------

_TRUNK_BRANCH: dict[str, object] = {"fork_path": "trunk", "parent": None, "slice_at": 0}


def _records_table(kind: str, *, presentation_id: bool) -> dict[str, object]:
    """A minimal records__<kind> table, with or without a presentation_id column."""
    columns: list[dict[str, object]] = [{"name": "record_id", "type": "VARCHAR"}]
    if presentation_id:
        columns.append({"name": "presentation_id", "type": "VARCHAR"})
    return {
        "name": f"records__{kind}",
        "category": "records",
        "record_kind": kind,
        "columns": columns,
        "rows": 1,
    }


def _raw_sidecar(
    tables: list[dict[str, object]],
    *,
    presentation_keys: dict[str, object] | None = None,
    enum_domains: dict[str, object] | None = None,
) -> dict[str, object]:
    """A minimal base.json mapping carrying the given tables/blocks."""
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [_TRUNK_BRANCH],
        "tables": tables,
    }
    if presentation_keys is not None:
        raw["presentation_keys"] = presentation_keys
    if enum_domains is not None:
        raw["enum_domains"] = enum_domains
    return raw


def _raw_key_space(
    space_class: str, *, prefix: str | None = None, width: int | None = None
) -> dict[str, object]:
    """A raw key_space object, presence of prefix/width controlled by the caller."""
    raw: dict[str, object] = {"class": space_class}
    if prefix is not None:
        raw["prefix"] = prefix
    if width is not None:
        raw["width"] = width
    return raw


def _raw_partition_key(
    unique_within: str,
    branch_stable: bool,
    slice_stable: bool,
    key_space: dict[str, object],
) -> dict[str, object]:
    """A raw partition_key object."""
    return {
        "unique_within": unique_within,
        "branch_stable": branch_stable,
        "slice_stable": slice_stable,
        "key_space": key_space,
    }


def _raw_counter_key(prefix: str = "", width: int = 0) -> dict[str, object]:
    """A conformant counter-class raw partition_key (emit/false/false)."""
    return _raw_partition_key(
        "emit", False, False, _raw_key_space("counter", prefix=prefix, width=width)
    )


def _raw_record_index_key(prefix: str = "", width: int = 0) -> dict[str, object]:
    """A conformant record_index-class raw partition_key (branch/true/true)."""
    return _raw_partition_key(
        "branch", True, True, _raw_key_space("record_index", prefix=prefix, width=width)
    )


def _raw_uuid_key() -> dict[str, object]:
    """A conformant uuid-class raw partition_key (branch/true/true)."""
    return _raw_partition_key("branch", True, True, _raw_key_space("uuid"))


def _raw_record_id_key() -> dict[str, object]:
    """A conformant record_id-class raw partition_key (branch/true/true)."""
    return _raw_partition_key("branch", True, True, _raw_key_space("record_id"))


# ---------------------------------------------------------------------------
# Typed-object helpers (for union_safe / combined_claim, which take dataclasses)
# ---------------------------------------------------------------------------


def _ks(
    space_class: str, *, prefix: str | None = None, width: int | None = None
) -> KeySpace:
    return KeySpace(space_class=space_class, prefix=prefix, width=width)  # type: ignore[arg-type]


def _pk(
    unique_within: str, branch_stable: bool, slice_stable: bool, key_space: KeySpace
) -> PartitionKey:
    return PartitionKey(
        unique_within=unique_within,  # type: ignore[arg-type]
        branch_stable=branch_stable,
        slice_stable=slice_stable,
        key_space=key_space,
    )


# ---------------------------------------------------------------------------
# Absence
# ---------------------------------------------------------------------------


def test_presentation_keys_absent_returns_none() -> None:
    """No presentation_keys key in the sidecar -> presentation_keys() is None."""
    sidecar = Sidecar.from_raw(
        _raw_sidecar([_records_table("ward", presentation_id=True)])
    )
    assert sidecar.presentation_keys() is None


# ---------------------------------------------------------------------------
# Coherent flat kind
# ---------------------------------------------------------------------------


def test_coherent_flat_kind() -> None:
    """A flat kind: sidecar order, not partitioned, key() verbatim, no sub_types."""
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=True)],
        presentation_keys={
            "ward": {"key": _raw_counter_key(prefix="WARD_", width=3)},
        },
    )
    pk = Sidecar.from_raw(raw).presentation_keys()
    assert pk is not None
    assert pk.kinds() == ("ward",)
    assert pk.is_partitioned("ward") is False
    assert pk.key("ward") == PartitionKey(
        unique_within="emit",
        branch_stable=False,
        slice_stable=False,
        key_space=KeySpace(space_class="counter", prefix="WARD_", width=3),
    )
    with pytest.raises(ValueError):
        pk.sub_types("ward")
    with pytest.raises(ValueError):
        pk.key_for("ward", "anything")


def test_kinds_order_is_sidecar_order() -> None:
    """kinds() reflects the block's own (insertion) order, never re-sorted."""
    raw = _raw_sidecar(
        [
            _records_table("zebra", presentation_id=True),
            _records_table("apple", presentation_id=True),
        ],
        presentation_keys={
            "zebra": {"key": _raw_counter_key()},
            "apple": {"key": _raw_counter_key()},
        },
    )
    pk = Sidecar.from_raw(raw).presentation_keys()
    assert pk is not None
    assert pk.kinds() == ("zebra", "apple")


# ---------------------------------------------------------------------------
# Coherent partitioned kind
# ---------------------------------------------------------------------------


def _actor_raw(extra_sub_types: dict[str, object] | None = None) -> dict[str, object]:
    """A coherent partitioned 'actor' kind: two safe, stable sub-types."""
    sub_types: dict[str, object] = {
        "patient": _raw_record_index_key(prefix="PAT_", width=4),
        "staff": _raw_record_index_key(prefix="STAFF_", width=4),
    }
    if extra_sub_types:
        sub_types.update(extra_sub_types)
    return _raw_sidecar(
        [_records_table("actor", presentation_id=True)],
        presentation_keys={
            "actor": {
                "sub_types": sub_types,
                "unique_within": "branch",
                "branch_stable": True,
                "slice_stable": True,
            }
        },
        enum_domains={"actor": {"actor_type": ["patient", "staff"]}},
    )


def test_coherent_partitioned_kind() -> None:
    """A partitioned kind: is_partitioned True, sub_types verbatim, key_for per sub-type."""
    pk = Sidecar.from_raw(_actor_raw()).presentation_keys()
    assert pk is not None
    assert pk.is_partitioned("actor") is True
    assert pk.sub_types("actor") == ("patient", "staff")
    assert pk.key_for("actor", "patient") == PartitionKey(
        unique_within="branch",
        branch_stable=True,
        slice_stable=True,
        key_space=KeySpace(space_class="record_index", prefix="PAT_", width=4),
    )
    with pytest.raises(KeyError):
        pk.key_for("actor", "undeclared_sub_type")
    with pytest.raises(ValueError):
        pk.key("actor")


def test_partitioned_kind_retains_zero_row_sub_types() -> None:
    """sub_types() never narrows to sub-types with surviving rows — it is declared,
    not observed, so every minting declaration (including a hypothetical zero-row
    partition) survives verbatim."""
    pk = Sidecar.from_raw(_actor_raw()).presentation_keys()
    assert pk is not None
    # Both declared sub-types are present regardless of row counts, which this
    # view never consults (the table's `rows` total is unrelated to per-sub-type
    # presence in the block).
    assert set(pk.sub_types("actor")) == {"patient", "staff"}


# ---------------------------------------------------------------------------
# whole_table_claim
# ---------------------------------------------------------------------------


def test_whole_table_claim_flat_kind_equals_key_scalars() -> None:
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=True)],
        presentation_keys={"ward": {"key": _raw_counter_key(prefix="WARD_", width=3)}},
    )
    pk = Sidecar.from_raw(raw).presentation_keys()
    assert pk is not None
    assert pk.whole_table_claim("ward") == WholeColumnClaim(
        unique_within="emit", branch_stable=False, slice_stable=False
    )


def test_whole_table_claim_partitioned_kind_equals_rollup() -> None:
    pk = Sidecar.from_raw(_actor_raw()).presentation_keys()
    assert pk is not None
    assert pk.whole_table_claim("actor") == WholeColumnClaim(
        unique_within="branch", branch_stable=True, slice_stable=True
    )


def test_whole_table_claim_rollup_omitted_unique_within_is_none() -> None:
    """A rollup with no unique_within (the algebra derives no claim) -> None,
    never a defaulted value."""
    raw = _raw_sidecar(
        [_records_table("asset", presentation_id=True)],
        presentation_keys={
            "asset": {
                "sub_types": {
                    "a": _raw_counter_key(prefix="", width=3),
                    "b": _raw_counter_key(prefix="", width=3),
                },
                # unique_within omitted: the two counters share prefix "" and are
                # not pairwise union-safe.
                "branch_stable": False,
                "slice_stable": False,
            }
        },
        enum_domains={"asset": {"asset_type": ["a", "b"]}},
    )
    pk = Sidecar.from_raw(raw).presentation_keys()
    assert pk is not None
    claim = pk.whole_table_claim("asset")
    assert claim.unique_within is None
    assert claim.branch_stable is False
    assert claim.slice_stable is False


# ---------------------------------------------------------------------------
# Unknown kind -> KeyError from every kind-taking method
# ---------------------------------------------------------------------------


def test_unknown_kind_raises_key_error() -> None:
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=True)],
        presentation_keys={"ward": {"key": _raw_counter_key()}},
    )
    pk = Sidecar.from_raw(raw).presentation_keys()
    assert pk is not None
    with pytest.raises(KeyError):
        pk.is_partitioned("nonexistent")
    with pytest.raises(KeyError):
        pk.key("nonexistent")
    with pytest.raises(KeyError):
        pk.sub_types("nonexistent")
    with pytest.raises(KeyError):
        pk.key_for("nonexistent", "sub")
    with pytest.raises(KeyError):
        pk.whole_table_claim("nonexistent")


# ---------------------------------------------------------------------------
# The six coherence clauses, each violated in isolation
# ---------------------------------------------------------------------------


def test_clause_a_kind_membership_entry_without_presentation_id_column() -> None:
    """(a) A kind carries a block entry, but its records__<kind> table has no
    presentation_id column."""
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=False)],
        presentation_keys={"ward": {"key": _raw_counter_key()}},
    )
    with pytest.raises(PresentationKeysInvalidError, match="ward"):
        Sidecar.from_raw(raw).presentation_keys()


def test_clause_b_kind_membership_column_without_block_entry() -> None:
    """(b) A records__<kind> table carries presentation_id, but the block has
    no entry for it."""
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=True)],
        presentation_keys={},
    )
    with pytest.raises(PresentationKeysInvalidError, match="ward"):
        Sidecar.from_raw(raw).presentation_keys()


def test_clause_c_key_entry_on_discriminator_bearing_kind() -> None:
    """(c) A discriminator-bearing kind carries a flat `key` entry instead of
    `sub_types`."""
    raw = _raw_sidecar(
        [_records_table("actor", presentation_id=True)],
        presentation_keys={"actor": {"key": _raw_counter_key()}},
        enum_domains={"actor": {"actor_type": ["patient", "staff"]}},
    )
    with pytest.raises(PresentationKeysInvalidError, match="actor"):
        Sidecar.from_raw(raw).presentation_keys()


def test_clause_c_sub_types_entry_on_flat_kind() -> None:
    """(c) A flat (non-discriminated) kind carries a `sub_types` entry instead
    of `key`."""
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=True)],
        presentation_keys={
            "ward": {
                "sub_types": {"x": _raw_counter_key()},
                "branch_stable": False,
                "slice_stable": False,
            }
        },
    )
    with pytest.raises(PresentationKeysInvalidError, match="ward"):
        Sidecar.from_raw(raw).presentation_keys()


def test_clause_d_sub_type_outside_discriminator_domain() -> None:
    """(d) A sub_types key names a sub-type outside the kind's discriminator
    domain."""
    raw = _actor_raw(
        extra_sub_types={"ghost": _raw_record_index_key(prefix="G_", width=4)}
    )
    with pytest.raises(PresentationKeysInvalidError, match="ghost"):
        Sidecar.from_raw(raw).presentation_keys()


@pytest.mark.parametrize(
    "key_space",
    [
        _raw_key_space("counter", prefix="WARD_", width=3),  # counter claiming branch
        _raw_key_space("uuid"),  # uuid claiming emit
    ],
    ids=["counter_claiming_branch", "uuid_claiming_emit"],
)
def test_clause_e_scalars_inconsistent_with_key_space_class(
    key_space: dict[str, object],
) -> None:
    """(e) unique_within/branch_stable/slice_stable disagree with what
    key_space.class determines."""
    # Swap the scalar triple relative to what the class demands.
    wrong_scalars = (
        ("branch", True, True)
        if key_space["class"] == "counter"
        else ("emit", False, False)
    )
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=True)],
        presentation_keys={
            "ward": {"key": _raw_partition_key(*wrong_scalars, key_space)},
        },
    )
    with pytest.raises(PresentationKeysInvalidError, match="ward"):
        Sidecar.from_raw(raw).presentation_keys()


def test_clause_f_prefix_width_present_on_uuid() -> None:
    """(f) prefix/width present on a non-digit-rendered class (uuid)."""
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=True)],
        presentation_keys={
            "ward": {
                "key": _raw_partition_key(
                    "branch", True, True, _raw_key_space("uuid", prefix="", width=0)
                )
            }
        },
    )
    with pytest.raises(PresentationKeysInvalidError, match="ward"):
        Sidecar.from_raw(raw).presentation_keys()


def test_clause_f_prefix_width_absent_on_counter() -> None:
    """(f) prefix/width absent on a digit-rendered class (counter)."""
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=True)],
        presentation_keys={
            "ward": {
                "key": _raw_partition_key(
                    "emit", False, False, _raw_key_space("counter")
                )
            }
        },
    )
    with pytest.raises(PresentationKeysInvalidError, match="ward"):
        Sidecar.from_raw(raw).presentation_keys()


def test_clause_g_rollup_wrong_scalar() -> None:
    """(g) The rollup's unique_within disagrees with combined_claim's derived scalar."""
    raw = _raw_sidecar(
        [_records_table("actor", presentation_id=True)],
        presentation_keys={
            "actor": {
                "sub_types": {
                    "patient": _raw_record_index_key(prefix="PAT_", width=4),
                    "staff": _raw_record_index_key(prefix="STAFF_", width=4),
                },
                # combined_claim derives "branch" for two safe, stable entries.
                "unique_within": "emit",
                "branch_stable": True,
                "slice_stable": True,
            }
        },
        enum_domains={"actor": {"actor_type": ["patient", "staff"]}},
    )
    with pytest.raises(PresentationKeysInvalidError, match="actor"):
        Sidecar.from_raw(raw).presentation_keys()


def test_clause_g_rollup_wrongly_present_unique_within() -> None:
    """(g) The rollup carries a unique_within where combined_claim derives none
    (the pair is not union-safe)."""
    raw = _raw_sidecar(
        [_records_table("asset", presentation_id=True)],
        presentation_keys={
            "asset": {
                "sub_types": {
                    "a": _raw_counter_key(prefix="", width=3),
                    "b": _raw_counter_key(prefix="", width=3),
                },
                # Wrongly asserts a claim the algebra derives none for.
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
            }
        },
        enum_domains={"asset": {"asset_type": ["a", "b"]}},
    )
    with pytest.raises(PresentationKeysInvalidError, match="asset"):
        Sidecar.from_raw(raw).presentation_keys()


# ---------------------------------------------------------------------------
# Defensive shape guards (JSON-Schema-invalid raw reaching the strict parser)
# ---------------------------------------------------------------------------


def test_guard_kind_entry_not_an_object() -> None:
    """A block entry that is not an object (bypassing JSON Schema)."""
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=True)],
        presentation_keys={"ward": "not-an-object"},
    )
    with pytest.raises(PresentationKeysInvalidError, match="entry is not an object"):
        Sidecar.from_raw(raw).presentation_keys()


def test_guard_key_space_not_an_object() -> None:
    """key_space is not an object."""
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=True)],
        presentation_keys={
            "ward": {
                "key": {
                    "unique_within": "emit",
                    "branch_stable": False,
                    "slice_stable": False,
                    "key_space": "not-an-object",
                }
            }
        },
    )
    with pytest.raises(
        PresentationKeysInvalidError, match="key_space is missing or not an object"
    ):
        Sidecar.from_raw(raw).presentation_keys()


def test_guard_key_space_class_outside_enum() -> None:
    """key_space.class outside the four-member enum."""
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=True)],
        presentation_keys={
            "ward": {
                "key": _raw_partition_key("emit", False, False, _raw_key_space("bogus"))
            }
        },
    )
    with pytest.raises(PresentationKeysInvalidError, match="is not one of"):
        Sidecar.from_raw(raw).presentation_keys()


def test_guard_key_space_prefix_width_wrong_type() -> None:
    """prefix/width present but wrong type on a digit-rendered class."""
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=True)],
        presentation_keys={
            "ward": {
                "key": _raw_partition_key(
                    "emit",
                    False,
                    False,
                    {"class": "counter", "prefix": 123, "width": "abc"},
                )
            }
        },
    )
    with pytest.raises(
        PresentationKeysInvalidError, match="prefix/width must be a string/integer"
    ):
        Sidecar.from_raw(raw).presentation_keys()


def test_guard_partition_key_entry_not_an_object() -> None:
    """A `key`/sub_types member value that is not an object."""
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=True)],
        presentation_keys={"ward": {"key": "not-an-object"}},
    )
    with pytest.raises(PresentationKeysInvalidError, match="entry is not an object"):
        Sidecar.from_raw(raw).presentation_keys()


def test_guard_partition_key_scalars_missing_or_mistyped() -> None:
    """unique_within/branch_stable/slice_stable missing or mistyped."""
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=True)],
        presentation_keys={
            "ward": {
                "key": {
                    "unique_within": "emit",
                    "branch_stable": "not-a-bool",
                    "slice_stable": False,
                    "key_space": _raw_key_space("counter", prefix="", width=0),
                }
            }
        },
    )
    with pytest.raises(
        PresentationKeysInvalidError,
        match="unique_within/branch_stable/slice_stable missing or mistyped",
    ):
        Sidecar.from_raw(raw).presentation_keys()


def test_guard_rollup_unique_within_outside_enum() -> None:
    """rollup.unique_within present but outside {emit, branch}."""
    raw = _raw_sidecar(
        [_records_table("actor", presentation_id=True)],
        presentation_keys={
            "actor": {
                "sub_types": {
                    "patient": _raw_record_index_key(prefix="PAT_", width=4),
                    "staff": _raw_record_index_key(prefix="STAFF_", width=4),
                },
                "unique_within": "bogus",
                "branch_stable": True,
                "slice_stable": True,
            }
        },
        enum_domains={"actor": {"actor_type": ["patient", "staff"]}},
    )
    with pytest.raises(PresentationKeysInvalidError, match="rollup unique_within"):
        Sidecar.from_raw(raw).presentation_keys()


def test_guard_rollup_stability_missing_or_mistyped() -> None:
    """rollup.branch_stable/slice_stable missing or mistyped."""
    raw = _raw_sidecar(
        [_records_table("actor", presentation_id=True)],
        presentation_keys={
            "actor": {
                "sub_types": {
                    "patient": _raw_record_index_key(prefix="PAT_", width=4),
                    "staff": _raw_record_index_key(prefix="STAFF_", width=4),
                },
                "unique_within": "branch",
                "branch_stable": "not-a-bool",
                "slice_stable": True,
            }
        },
        enum_domains={"actor": {"actor_type": ["patient", "staff"]}},
    )
    with pytest.raises(
        PresentationKeysInvalidError, match="rollup branch_stable/slice_stable"
    ):
        Sidecar.from_raw(raw).presentation_keys()


def test_guard_sub_types_not_an_object() -> None:
    """sub_types present but not an object."""
    raw = _raw_sidecar(
        [_records_table("actor", presentation_id=True)],
        presentation_keys={
            "actor": {
                "sub_types": "not-an-object",
                "unique_within": "branch",
                "branch_stable": True,
                "slice_stable": True,
            }
        },
        enum_domains={"actor": {"actor_type": ["patient", "staff"]}},
    )
    with pytest.raises(
        PresentationKeysInvalidError, match="sub_types must be a non-empty object"
    ):
        Sidecar.from_raw(raw).presentation_keys()


def test_guard_sub_types_empty_object() -> None:
    """sub_types present but empty."""
    raw = _raw_sidecar(
        [_records_table("actor", presentation_id=True)],
        presentation_keys={
            "actor": {
                "sub_types": {},
                "unique_within": "branch",
                "branch_stable": True,
                "slice_stable": True,
            }
        },
        enum_domains={"actor": {"actor_type": ["patient", "staff"]}},
    )
    with pytest.raises(
        PresentationKeysInvalidError, match="sub_types must be a non-empty object"
    ):
        Sidecar.from_raw(raw).presentation_keys()


# ---------------------------------------------------------------------------
# Laziness
# ---------------------------------------------------------------------------


def test_incoherent_block_parses_at_construction_without_raising() -> None:
    """An incoherent block never raises until presentation_keys() is called."""
    raw = _raw_sidecar(
        [_records_table("ward", presentation_id=False)],
        presentation_keys={"ward": {"key": _raw_counter_key()}},
    )
    sidecar = Sidecar.from_raw(raw)  # must not raise
    with pytest.raises(PresentationKeysInvalidError):
        sidecar.presentation_keys()


# ---------------------------------------------------------------------------
# union_safe: the contract's normative pairwise table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        pytest.param(
            _ks("record_index", prefix="WARD_", width=3),
            _ks("record_index", prefix="WARD_", width=3),
            True,
            id="identical_record_index",
        ),
        pytest.param(
            _ks("record_index", prefix="WARD_", width=3),
            _ks("record_index", prefix="WARD_", width=4),
            False,
            id="differing_width_record_index",
        ),
        pytest.param(_ks("uuid"), _ks("uuid"), True, id="uuid_x_uuid"),
        pytest.param(
            _ks("record_id"), _ks("record_id"), True, id="record_id_x_record_id"
        ),
        pytest.param(
            _ks("counter", prefix="WARD_", width=0),
            _ks("counter", prefix="THTR_", width=0),
            True,
            id="incomparable_prefixes_ward_thtr",
        ),
        pytest.param(
            _ks("counter", prefix="A-", width=0),
            _ks("counter", prefix="A-1", width=0),
            False,
            id="comparable_prefixes_a_dash",
        ),
        pytest.param(
            _ks("counter", prefix="", width=0),
            _ks("counter", prefix="1", width=0),
            False,
            id="comparable_prefixes_empty_vs_digit",
        ),
        pytest.param(
            _ks("counter", prefix="", width=0),
            _ks("counter", prefix="X_", width=0),
            True,
            id="incomparable_prefixes_empty_vs_letter",
        ),
        pytest.param(
            _ks("counter", prefix="A", width=3),
            _ks("counter", prefix="A", width=3),
            False,
            id="equal_prefix_counters",
        ),
        pytest.param(
            _ks("uuid"),
            _ks("counter", prefix="WARD_", width=3),
            False,
            id="uuid_x_digit_rendered",
        ),
        pytest.param(
            _ks("record_id"),
            _ks("counter", prefix="WARD_", width=3),
            False,
            id="record_id_x_digit_rendered",
        ),
        pytest.param(
            _ks("counter", prefix="A-1", width=0),
            _ks("counter", prefix="A-", width=0),
            False,
            id="comparable_prefixes_longer_first",
        ),
        pytest.param(
            _ks("counter", prefix="X_", width=0),
            _ks("counter", prefix="", width=0),
            True,
            id="incomparable_prefixes_longer_first",
        ),
    ],
)
def test_union_safe_pairwise_table(a: KeySpace, b: KeySpace, expected: bool) -> None:
    assert union_safe(a, b) is expected


# ---------------------------------------------------------------------------
# combined_claim
# ---------------------------------------------------------------------------


def test_combined_claim_singleton_equals_entry_scalars() -> None:
    entry = _pk("emit", False, False, _ks("counter", prefix="WARD_", width=3))
    assert combined_claim([entry]) == WholeColumnClaim(
        unique_within="emit", branch_stable=False, slice_stable=False
    )


def test_combined_claim_all_counter() -> None:
    entries = [
        _pk("emit", False, False, _ks("counter", prefix="WARD_", width=0)),
        _pk("emit", False, False, _ks("counter", prefix="THTR_", width=0)),
    ]
    assert combined_claim(entries) == WholeColumnClaim(
        unique_within="emit", branch_stable=False, slice_stable=False
    )


def test_combined_claim_all_stable() -> None:
    entries = [
        _pk("branch", True, True, _ks("uuid")),
        _pk("branch", True, True, _ks("uuid")),
    ]
    assert combined_claim(entries) == WholeColumnClaim(
        unique_within="branch", branch_stable=True, slice_stable=True
    )


def test_combined_claim_mixed() -> None:
    entries = [
        _pk("emit", False, False, _ks("counter", prefix="A", width=0)),
        _pk("branch", True, True, _ks("record_index", prefix="B", width=0)),
    ]
    assert combined_claim(entries) == WholeColumnClaim(
        unique_within="branch", branch_stable=False, slice_stable=False
    )


def test_combined_claim_any_pair_unsafe_all_stable() -> None:
    """Pairwise-unsafe union of stable-class entries -> no uniqueness claim,
    stability true/true (every member is stable-class)."""
    entries = [
        _pk("branch", True, True, _ks("record_index", prefix="", width=3)),
        _pk("branch", True, True, _ks("record_index", prefix="", width=4)),
    ]
    claim = combined_claim(entries)
    assert claim.unique_within is None
    assert claim.branch_stable is True
    assert claim.slice_stable is True


def test_combined_claim_any_pair_unsafe_not_all_stable() -> None:
    """Pairwise-unsafe union with a counter-class member -> stability false/false."""
    entries = [
        _pk("emit", False, False, _ks("counter", prefix="", width=3)),
        _pk("emit", False, False, _ks("counter", prefix="", width=3)),
    ]
    claim = combined_claim(entries)
    assert claim.unique_within is None
    assert claim.branch_stable is False
    assert claim.slice_stable is False


def test_combined_claim_empty_raises_value_error() -> None:
    with pytest.raises(ValueError):
        combined_claim([])
