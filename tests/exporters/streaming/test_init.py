"""Tests for `exporters.streaming.init.generate_stream_init_config`.

One case per design-doc proposal-table row (docs/architecture/pending/
streaming-declared-streams.md § `init --mode streaming` inference contract):
- >= 1 records kind -> live `content: state-changes`, membership alternative
  fully commented.
- Flat kind -> one live stream, `properties` = payload-role `prop__` columns
  bare (ref_index__* and presentation_id never selected).
- Sub-typed kind with a `sub_type_columns` partition -> one live stream per
  declared sub-type, properties pruned to that sub-type's owned columns,
  discriminator never proposed.
- Sub-typed kind, sidecar omits the partition -> union-set fallback with a
  comment, per sub-type.
- A population with no tracked property -> live under an advisory
  lifecycle-only comment.
- Name collision -- a kind name vs. a sub-type value, and the membership
  `<K>_<p>` underscore ambiguity -- later entry commented, config parses.
- Topic-illegal sub-type value -> commented, naming the rule and the value.
- `keys:` proposal: presentation_id for registry-declared, record_index
  otherwise; a gate failure degrades the implicated kind with a comment.
- Each membership table -> one commented stream in the membership-events
  alternative, `fields` bare.
- No records kind, and all-names-illegal -> `StreamInitNothingToStream`.
- An emit predating per-column temporal classes -> `TemporalClassUnavailableError`.
- Non-exempt `slice_only` columns are never proposed; one notice each.
- The `rebase`/`debezium`/`clock`/`kafka` blocks are never proposed, named in
  a trailing comment.
- Round-trip: the emitted text parses into a `StreamConfig` and
  `iter_stream_events` runs clean, including the membership alternative
  uncommented wholesale.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink
from _support.sidecar_builder import identity_column, prop_column, write_emit

from exporters.source._source_fixtures import build_source_test_emit
from exporters.streaming._election_fixtures import (
    CREATURE_UNSAFE_REGISTRY,
    FULL_REGISTRY,
    build_election_emit,
)
from fabulexa_forge.config.loader import load_stream_config
from fabulexa_forge.errors import StreamInitNothingToStream
from fabulexa_forge.exporters.streaming.engine import iter_stream_events
from fabulexa_forge.exporters.streaming.init import generate_stream_init_config
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import TemporalClassUnavailableError

from ._helpers import _ddl, _membership_table_spec

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _generate(emit_dir: Path) -> str:
    """Generate the candidate config, discarding notices."""
    with open_emit(emit_dir) as emit:
        return generate_stream_init_config(emit, discard_notice_sink)


def _generate_recording_notices(emit_dir: Path) -> tuple[str, RecordingNoticeSink]:
    """Generate the candidate config, recording every notice."""
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        content = generate_stream_init_config(emit, sink)
    return content, sink


def _assert_round_trip_streams_clean(
    emit_dir: Path, content: str, tmp_path: Path
) -> None:
    """Load the generated YAML and drain `iter_stream_events` -- must not raise."""
    cfg_path = tmp_path / "candidate.yaml"
    cfg_path.write_text(content, encoding="utf-8")
    config = load_stream_config(cfg_path)
    with open_emit(emit_dir) as emit:
        list(iter_stream_events(emit, config, None, notice_sink=discard_notice_sink))


def _uncomment_membership_alternative(content: str) -> str:
    """Turn the fully-commented membership-events block into a live config.

    Strips exactly one leading '#' (and one following space, when present)
    from every line starting at `# content: membership-events`, and pairs it
    with the original `keys:` block (unaffected by content type) -- the
    "uncomment wholesale" the design doc guarantees parses and streams clean.
    """
    marker = "# content: membership-events\n"
    alt_tail = content[content.index(marker) :]
    uncommented = "\n".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in alt_tail.splitlines()
    )
    keys_start = content.index("keys:")
    keys_end = content.index("\n# rebase:")
    keys_block = content[keys_start:keys_end]
    return f"{uncommented}\n\n{keys_block}"


def _table_spec(
    name: str,
    category: str,
    cols: list[dict[str, object]],
    rows: int,
    record_kind: str | None = None,
) -> dict[str, object]:
    """Build a records/fixed-category table spec dict for the sidecar."""
    spec: dict[str, object] = {
        "name": name,
        "category": category,
        "columns": cols,
        "rows": rows,
    }
    if record_kind is not None:
        spec["record_kind"] = record_kind
    return spec


_IDENTITY_PREFIX: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]

#: A `history` table is only queried when a live stream selects a tracked
#: property (an 'u' event source) -- round-tripped fixtures with >= 1 tracked
#: property carry this (possibly empty) sidecar declaration so that read
#: succeeds; fixtures never round-tripped omit it.
_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


# ---------------------------------------------------------------------------
# >= 1 records kind -> live state-changes, membership alternative commented
# ---------------------------------------------------------------------------


def test_records_kind_proposes_live_state_changes_with_commented_alternative(
    tmp_path: Path,
) -> None:
    """>= 1 records kind -> `content: state-changes` live, the membership
    alternative fully commented below it."""
    build_source_test_emit(tmp_path)
    content = _generate(tmp_path)
    assert "content: state-changes\n" in content
    assert "\n# content: membership-events\n" in content
    assert "\n# streams:\n" in content


# ---------------------------------------------------------------------------
# Flat kind -> one live stream, payload-role prop__ columns bare
# ---------------------------------------------------------------------------


def test_flat_kind_properties_are_bare_prop_columns_only(tmp_path: Path) -> None:
    """A flat kind's stream selects its payload-role `prop__` columns bare,
    never `ref_index__*` (identity-role)."""
    build_source_test_emit(tmp_path)
    content = _generate(tmp_path)
    assert (
        "    - name: order\n      kind: order\n      properties: [location_id, amount]\n"
        in content
    )
    assert "ref_index" not in content


# ---------------------------------------------------------------------------
# Sub-typed kind with a sub_type_columns partition
# ---------------------------------------------------------------------------

_GIZMO_COLUMNS: list[dict[str, object]] = [
    *_IDENTITY_PREFIX,
    {"name": "prop__gizmo_type", "type": "VARCHAR"},
    prop_column(
        "prop__weight", "BIGINT", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__color", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]


def _build_gizmo_partition_emit(tmp_path: Path) -> Path:
    """A sub-typed `gizmo` kind (alpha/beta) with a `sub_type_columns`
    partition: alpha owns `prop__weight`, beta owns `prop__color`."""
    conn = duckdb.connect(str(tmp_path / "run.duckdb"))
    conn.execute(_ddl("records__gizmo", _GIZMO_COLUMNS))
    conn.execute(
        'INSERT INTO "records__gizmo" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "g1", 10, True, 10, 0, "alpha", 5, "red"],
    )
    conn.close()
    write_emit(
        tmp_path,
        tables=[_table_spec("records__gizmo", "records", _GIZMO_COLUMNS, 1, "gizmo")],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={
            "enum_domains": {"gizmo": {"gizmo_type": ["alpha", "beta"]}},
            "sub_type_columns": {
                "gizmo": {"alpha": ["prop__weight"], "beta": ["prop__color"]}
            },
        },
    )
    return tmp_path


def test_subtyped_kind_partition_prunes_to_owned_columns(tmp_path: Path) -> None:
    """One live stream per declared sub-type, pruned to its own
    `sub_type_columns` partition; the discriminator is never proposed."""
    _build_gizmo_partition_emit(tmp_path)
    content = _generate(tmp_path)
    assert (
        "    # kind 'gizmo' declares sub-types: alpha, beta"
        " (one stream per sub-type below)\n"
        "    - name: alpha\n"
        "      kind: gizmo\n"
        "      sub_types: [alpha]\n"
        "      properties: [weight]\n" in content
    )
    assert (
        "    - name: beta\n"
        "      kind: gizmo\n"
        "      sub_types: [beta]\n"
        "      properties: [color]\n" in content
    )
    assert "gizmo_type" not in content
    assert "sidecar carries no sub_type_columns partition" not in content


def test_subtyped_kind_lifecycle_only_subtype_gets_comment(tmp_path: Path) -> None:
    """beta's sole property (`color`, constant) carries no tracked property
    -- its stream still proposes live, headed by the lifecycle-only comment."""
    _build_gizmo_partition_emit(tmp_path)
    content = _generate(tmp_path)
    assert (
        "    # NOTE: this population carries no tracked property; the feed is"
        " lifecycle-only (c/d events only) -- delete to opt out\n"
        "    - name: beta\n" in content
    )


# ---------------------------------------------------------------------------
# Sub-typed kind, sidecar omits sub_type_columns -> union fallback
# ---------------------------------------------------------------------------


def test_subtyped_kind_missing_partition_falls_back_to_union_with_comment(
    tmp_path: Path,
) -> None:
    """No `sub_type_columns` partition at all -> every sub-type stream
    proposes the kind's full payload-role set, with a fallback comment, in
    declared-domain order."""
    build_source_test_emit(tmp_path)
    content = _generate(tmp_path)
    assert (
        "    # kind 'actor' declares sub-types: consultant, nurse"
        " (one stream per sub-type below)\n" in content
    )
    assert (
        "    # NOTE: the sidecar carries no sub_type_columns partition for kind"
        " 'actor'; proposing the full column union for this sub-type\n"
        "    # NOTE: this population carries no tracked property; the feed is"
        " lifecycle-only (c/d events only) -- delete to opt out\n"
        "    - name: consultant\n"
        "      kind: actor\n"
        "      sub_types: [consultant]\n"
        "      properties: [name]\n" in content
    )
    assert (
        "    # NOTE: the sidecar carries no sub_type_columns partition for kind"
        " 'actor'; proposing the full column union for this sub-type\n"
        "    # NOTE: this population carries no tracked property; the feed is"
        " lifecycle-only (c/d events only) -- delete to opt out\n"
        "    - name: nurse\n"
        "      kind: actor\n"
        "      sub_types: [nurse]\n"
        "      properties: [name]\n" in content
    )


# ---------------------------------------------------------------------------
# Lifecycle-only population -> live under an advisory comment
# ---------------------------------------------------------------------------


def test_lifecycle_only_flat_population_live_under_comment(tmp_path: Path) -> None:
    """A flat kind with no tracked property proposes live, headed by the
    lifecycle-only comment -- deleting it opts out."""
    build_source_test_emit(tmp_path)
    content = _generate(tmp_path)
    assert (
        "    # NOTE: this population carries no tracked property; the feed is"
        " lifecycle-only (c/d events only) -- delete to opt out\n"
        "    - name: location\n"
        "      kind: location\n"
        "      properties: [name, region]\n" in content
    )


# ---------------------------------------------------------------------------
# Name collisions
# ---------------------------------------------------------------------------

_PRODUCT_COLUMNS: list[dict[str, object]] = [
    *_IDENTITY_PREFIX,
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_CATALOG_COLUMNS: list[dict[str, object]] = [
    *_IDENTITY_PREFIX,
    {"name": "prop__catalog_type", "type": "VARCHAR"},
    prop_column(
        "prop__level", "BIGINT", history_tracked=True, temporal_class="tracked"
    ),
]


def _build_kind_subtype_collision_emit(tmp_path: Path) -> Path:
    """Flat kind `product` (declared first) collides with sub-typed kind
    `catalog`'s sole declared sub-type value `product` (declared second)."""
    conn = duckdb.connect(str(tmp_path / "run.duckdb"))
    conn.execute(_ddl("records__product", _PRODUCT_COLUMNS))
    conn.execute(_ddl("records__catalog", _CATALOG_COLUMNS))
    conn.execute(_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        'INSERT INTO "records__product" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "p1", 10, True, 10, 0, "live"],
    )
    conn.execute(
        'INSERT INTO "records__catalog" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "c1", 10, True, 10, 0, "product", 7],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "product", "p1", "status", 10, "live"],
    )
    conn.close()
    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__product", "records", _PRODUCT_COLUMNS, 1, "product"),
            _table_spec("records__catalog", "records", _CATALOG_COLUMNS, 1, "catalog"),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 1),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={"enum_domains": {"catalog": {"catalog_type": ["product"]}}},
    )
    return tmp_path


def test_kind_and_subtype_name_collision_comments_out_later_proposal(
    tmp_path: Path,
) -> None:
    """The later `catalog`->`product` sub-type proposal is commented, naming
    the collision; the earlier flat `product` stream stays live; the config
    still parses and streams clean."""
    _build_kind_subtype_collision_emit(tmp_path)
    content = _generate(tmp_path)
    assert (
        "    - name: product\n      kind: product\n      properties: [status]\n"
        in content
    )
    assert (
        "    # NOTE: name 'product' collides with an earlier proposal above;"
        " rename one before uncommenting\n"
        "    # - name: product\n"
        "    #   kind: catalog\n"
        "    #   sub_types: [product]\n"
        "    #   properties: [level]\n" in content
    )
    _assert_round_trip_streams_clean(tmp_path, content, tmp_path)


_AB_MEMBER_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__seat", "type": "VARCHAR"},
]

_A_MEMBER_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__desk", "type": "VARCHAR"},
]


def _build_membership_underscore_ambiguity_emit(tmp_path: Path) -> Path:
    """Kind `a_b` owns `membership__a_b__c` (auto-name `a_b_c`); kind `a`
    owns `membership__a__b_c` (auto-name `a_b_c` too) -- the underscore-
    ambiguous collision, declared in that order."""
    conn = duckdb.connect(str(tmp_path / "run.duckdb"))
    conn.execute(_ddl("records__a_b", _PRODUCT_COLUMNS))
    conn.execute(_ddl("membership__a_b__c", _AB_MEMBER_COLUMNS))
    conn.execute(_ddl("records__a", _PRODUCT_COLUMNS))
    conn.execute(_ddl("membership__a__b_c", _A_MEMBER_COLUMNS))
    conn.execute(_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        'INSERT INTO "records__a_b" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "ab1", 10, True, 10, 0, "live"],
    )
    conn.execute(
        'INSERT INTO "membership__a_b__c" VALUES (?, ?, ?, NULL, ?)',
        ["trunk", "ab1", 10, "window"],
    )
    conn.execute(
        'INSERT INTO "records__a" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a1", 10, True, 10, 0, "live"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "a_b", "ab1", "status", 10, "live"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "a", "a1", "status", 10, "live"],
    )
    conn.execute(
        'INSERT INTO "membership__a__b_c" VALUES (?, ?, ?, NULL, ?)',
        ["trunk", "a1", 10, "corner"],
    )
    conn.close()
    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__a_b", "records", _PRODUCT_COLUMNS, 1, "a_b"),
            _membership_table_spec(
                "membership__a_b__c", _AB_MEMBER_COLUMNS, 1, "a_b", "c"
            ),
            _table_spec("records__a", "records", _PRODUCT_COLUMNS, 1, "a"),
            _membership_table_spec(
                "membership__a__b_c", _A_MEMBER_COLUMNS, 1, "a", "b_c"
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 2),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return tmp_path


def test_membership_underscore_ambiguity_collision(tmp_path: Path) -> None:
    """`a_b`.`c` and `a`.`b_c` both auto-name `a_b_c`: the later entry is
    excluded from the uncommentable alternative body and carried as a
    collision comment; the config still parses and streams clean."""
    _build_membership_underscore_ambiguity_emit(tmp_path)
    content = _generate(tmp_path)
    assert (
        "#   - name: a_b_c\n"
        "#     membership: {kind: a_b, property: c}\n"
        "#     fields: [seat]\n" in content
    )
    assert (
        "#   # NOTE: name 'a_b_c' collides with an earlier proposal; excluded"
        " here -- rename before including it\n" in content
    )
    assert "membership: {kind: a, property: b_c}" not in content
    _assert_round_trip_streams_clean(tmp_path, content, tmp_path)


# ---------------------------------------------------------------------------
# Topic-illegal sub-type value
# ---------------------------------------------------------------------------

_WIDGET2_COLUMNS: list[dict[str, object]] = [
    *_IDENTITY_PREFIX,
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_GADGET2_COLUMNS: list[dict[str, object]] = [
    *_IDENTITY_PREFIX,
    {"name": "prop__gadget2_type", "type": "VARCHAR"},
    prop_column(
        "prop__weight", "BIGINT", history_tracked=True, temporal_class="tracked"
    ),
]


def _build_topic_illegal_subtype_emit(tmp_path: Path) -> Path:
    """A flat kind (`widget2`, kept live) plus a sub-typed kind (`gadget2`)
    whose sole declared sub-type value ('bad type') fails the topic-name
    rule."""
    conn = duckdb.connect(str(tmp_path / "run.duckdb"))
    conn.execute(_ddl("records__widget2", _WIDGET2_COLUMNS))
    conn.execute(_ddl("records__gadget2", _GADGET2_COLUMNS))
    conn.execute(_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        'INSERT INTO "records__widget2" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "w1", 10, True, 10, 0, "active"],
    )
    conn.execute(
        'INSERT INTO "records__gadget2" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "gd1", 10, True, 10, 0, "bad type", 3],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "widget2", "w1", "status", 10, "active"],
    )
    conn.close()
    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__widget2", "records", _WIDGET2_COLUMNS, 1, "widget2"),
            _table_spec("records__gadget2", "records", _GADGET2_COLUMNS, 1, "gadget2"),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 1),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={"enum_domains": {"gadget2": {"gadget2_type": ["bad type"]}}},
    )
    return tmp_path


def test_topic_illegal_subtype_value_commented_never_sanitized(tmp_path: Path) -> None:
    """`gadget2`'s illegal sub-type value is commented out, naming the rule
    and the offending value verbatim -- never sanitized; `widget2` stays live."""
    _build_topic_illegal_subtype_emit(tmp_path)
    content = _generate(tmp_path)
    assert (
        "    - name: widget2\n      kind: widget2\n      properties: [status]\n"
        in content
    )
    assert (
        "    # NOTE: sub-type value 'bad type' of kind 'gadget2' is not a legal"
        " topic name (must match ^[A-Za-z0-9._-]+$, and not '.' or '..'); rename"
        " before uncommenting\n"
        "    # - name: bad type\n"
        "    #   kind: gadget2\n"
        "    #   sub_types: [bad type]\n"
        "    #   properties: [weight]\n" in content
    )
    _assert_round_trip_streams_clean(tmp_path, content, tmp_path)


# ---------------------------------------------------------------------------
# `keys:` proposal, self-gated
# ---------------------------------------------------------------------------


def test_keys_registry_declared_proposes_presentation_id(tmp_path: Path) -> None:
    """A registry-declared, gate-safe population proposes `presentation_id`;
    an undeclared population proposes `record_index`."""
    build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
    content = _generate(tmp_path)
    assert "  widget: presentation_id\n" in content
    assert "  gadget: record_index\n" in content
    assert "  person: presentation_id\n" in content
    assert "  pet: presentation_id\n" in content


def test_keys_gate_failure_degrades_to_record_index_with_comment(
    tmp_path: Path,
) -> None:
    """`creature`'s declared-but-pairwise-unsafe election, gated against
    `trainer.prop__pet_id`'s reference edge, degrades to uniform
    `record_index` with a comment naming the forcing gate."""
    build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
    content = _generate(tmp_path)
    assert "  creature: record_index  # NOTE: ElectionUnionUnsafe" in content


def test_keys_undeclared_registry_proposes_record_index(tmp_path: Path) -> None:
    """No `presentation_keys` block at all -> every population proposes the
    `record_index` scalar."""
    build_election_emit(tmp_path, presentation_keys=None)
    content = _generate(tmp_path)
    assert "  widget: record_index\n" in content
    assert "  person: record_index\n" in content
    assert "  pet: record_index\n" in content
    assert "  creature: record_index\n" in content


_MIN_PERSON_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]

_MIN_CREATURE_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__creature_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="slice_only",
    ),
]

_MIN_PETS_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__pet__kind", "type": "VARCHAR"},
    {"name": "member__pet__id", "type": "VARCHAR"},
]


def _build_membership_member_field_degrade_emit(tmp_path: Path) -> Path:
    """`person` (no reference properties) + sub-typed, pairwise-unsafe
    `creature` (no reference properties either), tied together only by
    `membership__person__pets`' reference-valued `pet` member field.

    No kind-shaped stream (`person`, `cat`, `dog`) references `creature`, so
    the only path that can admit it to the edge gate is the membership
    member-field loop (design doc: "per member kind, since a membership
    member field's target kind is per-row").
    """
    conn = duckdb.connect(str(tmp_path / "run.duckdb"))
    conn.execute(_ddl("records__person", _MIN_PERSON_COLS))
    conn.execute(
        'INSERT INTO "records__person" VALUES (?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "p1", 0, True, 0, 0],
    )
    conn.execute(_ddl("records__creature", _MIN_CREATURE_COLS))
    conn.execute(
        'INSERT INTO "records__creature" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "c_cat1", "C1", 0, True, 0, 0, "cat"],
    )
    conn.execute(
        'INSERT INTO "records__creature" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "c_dog1", "D1", 0, True, 0, 1, "dog"],
    )
    conn.execute(_ddl("membership__person__pets", _MIN_PETS_COLS))
    conn.execute(
        'INSERT INTO "membership__person__pets" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "p1", 0, 300, "creature", "c_cat1"],
    )
    conn.close()
    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__person", "records", _MIN_PERSON_COLS, 1, "person"),
            _table_spec(
                "records__creature", "records", _MIN_CREATURE_COLS, 2, "creature"
            ),
            _membership_table_spec(
                "membership__person__pets", _MIN_PETS_COLS, 1, "person", "pets"
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={
            "enum_domains": {"creature": {"creature_type": ["cat", "dog"]}},
            "presentation_keys": CREATURE_UNSAFE_REGISTRY,
        },
    )
    return tmp_path


def test_keys_membership_member_field_reference_degrades_admitted_kind(
    tmp_path: Path,
) -> None:
    """A membership stream's reference-valued member field alone admits
    `creature` to the edge gate -- its pairwise-unsafe presentation_id claims
    degrade it to `record_index`, with no kind-shaped stream reference
    involved at all."""
    _build_membership_member_field_degrade_emit(tmp_path)
    content = _generate(tmp_path)
    assert "  creature: record_index  # NOTE: ElectionUnionUnsafe" in content


# ---------------------------------------------------------------------------
# Membership tables -> one commented stream per table
# ---------------------------------------------------------------------------


def test_membership_table_proposes_commented_stream_with_bare_fields(
    tmp_path: Path,
) -> None:
    """Each `membership__<K>__<p>` table proposes one commented stream in the
    membership-events alternative: `name: <K>_<p>`, `membership: {kind,
    property}`, `fields` = every element-schema field, bare."""
    build_source_test_emit(tmp_path)
    content = _generate(tmp_path)
    assert (
        "#   - name: visit_team\n"
        "#     membership: {kind: visit, property: team}\n"
        "#     fields: [role_name, actor]\n" in content
    )


# ---------------------------------------------------------------------------
# Refusals: no records kind, all-names-illegal, predating temporal classes
# ---------------------------------------------------------------------------


def _build_no_records_kind_emit(tmp_path: Path) -> Path:
    """An emit carrying no `records__<kind>` table at all -- only a `fixed`
    table."""
    conn = duckdb.connect(str(tmp_path / "run.duckdb"))
    conn.execute(_ddl("history", _HISTORY_COLUMNS))
    conn.close()
    write_emit(
        tmp_path,
        tables=[_table_spec("history", "fixed", _HISTORY_COLUMNS, 0)],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return tmp_path


def test_no_records_kind_raises_nothing_to_stream(tmp_path: Path) -> None:
    """No records kind -> `StreamInitNothingToStream`; a recordless emit has
    nothing to stream."""
    _build_no_records_kind_emit(tmp_path)
    with pytest.raises(StreamInitNothingToStream):
        _generate(tmp_path)


_ONLYBAD_COLUMNS: list[dict[str, object]] = [
    *_IDENTITY_PREFIX,
    {"name": "prop__onlybad_type", "type": "VARCHAR"},
    prop_column(
        "prop__weight", "BIGINT", history_tracked=True, temporal_class="tracked"
    ),
]


def _build_all_names_illegal_emit(tmp_path: Path) -> Path:
    """The sole records kind's every declared sub-type value fails the
    topic-name rule -- no proposal survives live at all."""
    conn = duckdb.connect(str(tmp_path / "run.duckdb"))
    conn.execute(_ddl("records__onlybad", _ONLYBAD_COLUMNS))
    conn.close()
    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__onlybad", "records", _ONLYBAD_COLUMNS, 0, "onlybad")
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={"enum_domains": {"onlybad": {"onlybad_type": ["bad one", "bad/two"]}}},
    )
    return tmp_path


def test_all_names_illegal_raises_nothing_to_stream(tmp_path: Path) -> None:
    """Every sidecar-derived stream name topic-illegal -> no proposal
    survives live -> `StreamInitNothingToStream`."""
    _build_all_names_illegal_emit(tmp_path)
    with pytest.raises(StreamInitNothingToStream):
        _generate(tmp_path)


_LEGACY_COLUMNS: list[dict[str, object]] = [
    *_IDENTITY_PREFIX,
    {"name": "prop__name", "type": "VARCHAR"},
]


def _build_temporal_class_unavailable_emit(tmp_path: Path) -> Path:
    """A flat kind whose sole `prop__` column carries neither
    `history_tracked` nor `temporal_class` -- an emit predating the flags."""
    conn = duckdb.connect(str(tmp_path / "run.duckdb"))
    conn.execute(_ddl("records__legacy", _LEGACY_COLUMNS))
    conn.execute(
        'INSERT INTO "records__legacy" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "l1", 10, True, 10, 0, "Widget"],
    )
    conn.close()
    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__legacy", "records", _LEGACY_COLUMNS, 1, "legacy")
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return tmp_path


def test_emit_predating_temporal_classes_raises(tmp_path: Path) -> None:
    """An emit predating per-column temporal classes ->
    `TemporalClassUnavailableError` propagates from the reader."""
    _build_temporal_class_unavailable_emit(tmp_path)
    with pytest.raises(TemporalClassUnavailableError):
        _generate(tmp_path)


# ---------------------------------------------------------------------------
# Non-exempt slice_only columns
# ---------------------------------------------------------------------------

_SENSOR2_COLUMNS: list[dict[str, object]] = [
    *_IDENTITY_PREFIX,
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__zone_note", "VARCHAR", history_tracked=False, temporal_class="slice_only"
    ),
]


def _build_slice_only_notice_emit(tmp_path: Path) -> Path:
    """A flat kind carrying one tracked property and one non-exempt
    `slice_only` property."""
    conn = duckdb.connect(str(tmp_path / "run.duckdb"))
    conn.execute(_ddl("records__sensor2", _SENSOR2_COLUMNS))
    conn.execute(
        'INSERT INTO "records__sensor2" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "s1", 10, True, 10, 0, "online", "north"],
    )
    conn.close()
    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__sensor2", "records", _SENSOR2_COLUMNS, 1, "sensor2")
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return tmp_path


def test_slice_only_column_never_proposed_one_notice(tmp_path: Path) -> None:
    """A non-exempt `slice_only` column is never proposed; exactly one
    'slice-only-column-omitted' notice fires for it."""
    _build_slice_only_notice_emit(tmp_path)
    content, sink = _generate_recording_notices(tmp_path)
    assert "zone_note" not in content
    assert "properties: [status]" in content
    slice_only_notices = [
        n for n in sink.notices if n.code == "slice-only-column-omitted"
    ]
    assert len(slice_only_notices) == 1
    assert "prop__zone_note" in slice_only_notices[0].message
    assert "sensor2" in slice_only_notices[0].message


# ---------------------------------------------------------------------------
# rebase / debezium / clock / kafka -- never proposed
# ---------------------------------------------------------------------------


def test_delivery_blocks_never_proposed_trailing_comment(tmp_path: Path) -> None:
    """`rebase`/`debezium`/`clock`/`kafka` are never proposed; one trailing
    comment names them."""
    build_source_test_emit(tmp_path)
    content = _generate(tmp_path)
    assert "rebase:\n" not in content
    assert "debezium:\n" not in content
    assert "clock:\n" not in content
    assert "kafka:\n" not in content
    assert (
        "# rebase: / debezium: / clock: / kafka: -- never proposed; delivery and"
        " environment\n"
        "# knobs, not emit-derived. Add them yourself, e.g. debezium:"
        " {table_identity: source_table, ...}\n" in content
    )


def test_authoring_fields_never_proposed_trailing_comment(tmp_path: Path) -> None:
    """`rename`/`kind_label`/`kind_labels`/`where`/`only`/`ignore`/membership
    `sub_types` are never proposed; the trailing comment names them alongside
    the delivery blocks, and the proposal is otherwise unchanged and
    parse-clean."""
    build_source_test_emit(tmp_path)
    content = _generate(tmp_path)
    live_lines = [
        line for line in content.splitlines() if not line.strip().startswith("#")
    ]
    for field in (
        "rename:",
        "kind_label:",
        "kind_labels:",
        "where:",
        "only:",
        "ignore:",
    ):
        assert not any(line.strip().startswith(field) for line in live_lines)
    membership_block = content.split("# Alternative")[1]
    assert "sub_types:" not in membership_block
    assert (
        "# rename: / kind_label: / kind_labels: / where: / only: / ignore: /"
        " sub_types: (membership) --\n"
        "# never proposed either; each is author intent with no sidecar-derived"
        " value (proposing one would be invention). Add them yourself.\n" in content
    )
    _assert_round_trip_streams_clean(tmp_path, content, tmp_path)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_parses_and_streams_clean(tmp_path: Path) -> None:
    """The full candidate -- flat, sub-typed (fallback), lifecycle-only, and
    a membership table -- loads and streams clean."""
    build_source_test_emit(tmp_path)
    content = _generate(tmp_path)
    _assert_round_trip_streams_clean(tmp_path, content, tmp_path)


def test_membership_alternative_uncommented_wholesale_streams_clean(
    tmp_path: Path,
) -> None:
    """Uncommenting the membership-events alternative block wholesale yields
    a config that parses and streams clean."""
    build_election_emit(tmp_path, presentation_keys=None)
    content = _generate(tmp_path)
    uncommented = _uncomment_membership_alternative(content)
    assert "content: membership-events" in uncommented
    _assert_round_trip_streams_clean(tmp_path, uncommented, tmp_path)
