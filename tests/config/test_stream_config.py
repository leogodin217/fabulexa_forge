"""Tests for StreamKindSelection, StreamConfig, and load_stream_config."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from fabulexa_export.config.loader import load_stream_config
from fabulexa_export.config.models import (
    ClockConfig,
    DebeziumConfig,
    DebeziumSourceIdentity,
    KafkaConfig,
    MembershipSelection,
    RebaseConfig,
    RoutingConfig,
    StreamConfig,
    StreamKindSelection,
)
from fabulexa_export.errors import ConfigError

# ---------------------------------------------------------------------------
# StreamKindSelection
# ---------------------------------------------------------------------------


def test_stream_kind_selection_valid() -> None:
    """Valid StreamKindSelection with bare property names parses cleanly."""
    ks = StreamKindSelection.model_validate(
        {"kind": "patient", "properties": ["name", "status"]}
    )
    assert ks.kind == "patient"
    assert ks.properties == ["name", "status"]


def test_stream_kind_selection_empty_properties_accepted() -> None:
    """Empty properties list is accepted (identity + lifecycle only)."""
    ks = StreamKindSelection.model_validate({"kind": "patient", "properties": []})
    assert ks.properties == []


def test_stream_kind_selection_prop_prefix_raises() -> None:
    """A property carrying the prop__ prefix raises ValueError."""
    with pytest.raises(ValidationError, match="prop__"):
        StreamKindSelection.model_validate(
            {"kind": "patient", "properties": ["prop__name"]}
        )


def test_stream_kind_selection_unknown_field_raises() -> None:
    """An unknown field on StreamKindSelection raises (extra='forbid')."""
    with pytest.raises(ValidationError):
        StreamKindSelection.model_validate(
            {"kind": "patient", "properties": [], "extra": "bad"}
        )


# ---------------------------------------------------------------------------
# StreamConfig
# ---------------------------------------------------------------------------


def _make_stream_config(**overrides: object) -> dict[object, object]:
    base: dict[object, object] = {
        "content": "state-changes",
        "kinds": [
            {"kind": "patient", "properties": ["name", "status"]},
            {"kind": "ward", "properties": []},
        ],
    }
    base.update(overrides)
    return base


def test_stream_config_valid_two_kinds() -> None:
    """Valid StreamConfig with two kinds and optional rebase parses correctly."""
    cfg = StreamConfig.model_validate(
        {
            "content": "state-changes",
            "kinds": [
                {"kind": "patient", "properties": ["name", "status"]},
                {"kind": "ward", "properties": []},
            ],
            "rebase": {"timezone": "Europe/London"},
        }
    )
    assert cfg.content == "state-changes"
    assert len(cfg.kinds) == 2
    assert cfg.kinds[0].kind == "patient"
    assert cfg.kinds[1].kind == "ward"
    assert cfg.rebase is not None
    assert cfg.rebase.timezone == "Europe/London"


def test_stream_config_rebase_absent_is_none() -> None:
    """Absent rebase block → rebase is None."""
    cfg = StreamConfig.model_validate(_make_stream_config())
    assert cfg.rebase is None


def test_stream_config_rebase_present_parsed() -> None:
    """Present rebase block is parsed as RebaseConfig."""
    cfg = StreamConfig.model_validate(_make_stream_config(rebase={"timezone": "UTC"}))
    assert isinstance(cfg.rebase, RebaseConfig)
    assert cfg.rebase.timezone == "UTC"


def test_stream_config_invalid_content_raises() -> None:
    """content='snapshots' still rejects (Literal)."""
    with pytest.raises(ValidationError):
        StreamConfig.model_validate(_make_stream_config(content="snapshots"))


def test_stream_config_empty_kinds_raises() -> None:
    """content='state-changes' with empty kinds raises from selection_matches_content."""
    with pytest.raises(ValidationError, match="non-empty"):
        StreamConfig.model_validate(_make_stream_config(kinds=[]))


def test_stream_config_duplicate_kind_raises() -> None:
    """Duplicate kind name raises ValueError from kinds_unique."""
    with pytest.raises(ValidationError, match="duplicate"):
        StreamConfig.model_validate(
            _make_stream_config(
                kinds=[
                    {"kind": "patient", "properties": []},
                    {"kind": "patient", "properties": ["status"]},
                ]
            )
        )


# ---------------------------------------------------------------------------
# StreamConfig — membership-events content
# ---------------------------------------------------------------------------

_MEMBERSHIP_ENTRY = {
    "owner_kind": "queue",
    "property": "waiters",
    "fields": ["priority"],
}


def _make_membership_stream_config(**overrides: object) -> dict[object, object]:
    base: dict[object, object] = {
        "content": "membership-events",
        "memberships": [_MEMBERSHIP_ENTRY],
    }
    base.update(overrides)
    return base


def test_stream_config_membership_events_valid() -> None:
    """content='membership-events' with non-empty memberships and empty kinds is valid."""
    cfg = StreamConfig.model_validate(_make_membership_stream_config())
    assert cfg.content == "membership-events"
    assert len(cfg.memberships) == 1
    assert cfg.memberships[0].owner_kind == "queue"
    assert cfg.memberships[0].property == "waiters"
    assert cfg.memberships[0].fields == ["priority"]
    assert cfg.kinds == []


def test_stream_config_membership_empty_memberships_raises() -> None:
    """content='membership-events' with empty memberships raises selection_matches_content."""
    with pytest.raises(ValidationError, match="non-empty"):
        StreamConfig.model_validate(_make_membership_stream_config(memberships=[]))


def test_stream_config_membership_non_empty_kinds_raises() -> None:
    """content='membership-events' with non-empty kinds raises selection_matches_content."""
    with pytest.raises(ValidationError, match="empty"):
        StreamConfig.model_validate(
            _make_membership_stream_config(
                kinds=[{"kind": "patient", "properties": []}]
            )
        )


def test_stream_config_state_changes_non_empty_memberships_raises() -> None:
    """content='state-changes' with non-empty memberships raises selection_matches_content."""
    with pytest.raises(ValidationError, match="empty"):
        StreamConfig.model_validate(
            _make_stream_config(memberships=[_MEMBERSHIP_ENTRY])
        )


def test_stream_config_memberships_duplicate_pair_raises() -> None:
    """Duplicate (owner_kind, property) pair in memberships raises memberships_unique."""
    with pytest.raises(ValidationError, match="duplicate"):
        StreamConfig.model_validate(
            _make_membership_stream_config(
                memberships=[
                    {"owner_kind": "queue", "property": "waiters", "fields": []},
                    {
                        "owner_kind": "queue",
                        "property": "waiters",
                        "fields": ["priority"],
                    },
                ]
            )
        )


def test_stream_config_memberships_unknown_field_raises() -> None:
    """Unknown field inside a memberships entry raises (extra='forbid')."""
    with pytest.raises(ValidationError):
        StreamConfig.model_validate(
            _make_membership_stream_config(
                memberships=[
                    {
                        "owner_kind": "queue",
                        "property": "waiters",
                        "fields": [],
                        "extra": "bad",
                    }
                ]
            )
        )


# ---------------------------------------------------------------------------
# MembershipSelection
# ---------------------------------------------------------------------------


def test_membership_selection_fields_empty_accepted() -> None:
    """MembershipSelection.fields = [] accepted (owner identity only)."""
    ms = MembershipSelection.model_validate(
        {"owner_kind": "queue", "property": "waiters", "fields": []}
    )
    assert ms.fields == []


def test_membership_selection_fields_are_bare_elem_prefix_raises() -> None:
    """A field name beginning with 'elem__' raises fields_are_bare."""
    with pytest.raises(ValidationError, match="elem__"):
        MembershipSelection.model_validate(
            {"owner_kind": "queue", "property": "waiters", "fields": ["elem__x"]}
        )


def test_membership_selection_fields_are_bare_member_prefix_raises() -> None:
    """A field name beginning with 'member__' raises fields_are_bare."""
    with pytest.raises(ValidationError, match="member__"):
        MembershipSelection.model_validate(
            {"owner_kind": "queue", "property": "waiters", "fields": ["member__y"]}
        )


def test_membership_selection_fields_unique_raises() -> None:
    """A repeated field name in fields raises fields_unique."""
    with pytest.raises(ValidationError, match="duplicate"):
        MembershipSelection.model_validate(
            {
                "owner_kind": "queue",
                "property": "waiters",
                "fields": ["priority", "priority"],
            }
        )


def test_membership_selection_unknown_field_raises() -> None:
    """Unknown field inside a memberships entry raises (extra='forbid')."""
    with pytest.raises(ValidationError):
        MembershipSelection.model_validate(
            {
                "owner_kind": "queue",
                "property": "waiters",
                "fields": [],
                "unknown": "bad",
            }
        )


def test_stream_config_unknown_top_level_field_raises() -> None:
    """Unknown top-level field raises (extra='forbid')."""
    with pytest.raises(ValidationError):
        StreamConfig.model_validate(_make_stream_config(unknown_field="bad"))


def test_stream_config_unknown_kinds_field_raises() -> None:
    """Unknown field inside a kinds entry raises (extra='forbid')."""
    with pytest.raises(ValidationError):
        StreamConfig.model_validate(
            _make_stream_config(
                kinds=[{"kind": "patient", "properties": [], "extra": "bad"}]
            )
        )


def test_stream_config_properties_empty_accepted() -> None:
    """A kind with properties: [] is accepted."""
    cfg = StreamConfig.model_validate(
        _make_stream_config(kinds=[{"kind": "ward", "properties": []}])
    )
    assert cfg.kinds[0].properties == []


# ---------------------------------------------------------------------------
# load_stream_config
# ---------------------------------------------------------------------------


def test_load_stream_config_valid(tmp_path: Path) -> None:
    """load_stream_config parses a valid YAML file correctly."""
    yaml_text = textwrap.dedent("""\
        content: state-changes
        kinds:
          - kind: patient
            properties:
              - name
              - status
          - kind: ward
            properties: []
    """)
    cfg_path = tmp_path / "stream.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    cfg = load_stream_config(cfg_path)
    assert cfg.content == "state-changes"
    assert len(cfg.kinds) == 2


def test_load_stream_config_missing_file_raises(tmp_path: Path) -> None:
    """Missing file raises ConfigError."""
    with pytest.raises(ConfigError, match="not found"):
        load_stream_config(tmp_path / "nonexistent.yaml")


def test_load_stream_config_malformed_yaml_raises(tmp_path: Path) -> None:
    """Malformed YAML raises ConfigError."""
    cfg_path = tmp_path / "bad.yaml"
    cfg_path.write_text("content: [\nbad yaml", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_stream_config(cfg_path)


def test_load_stream_config_pydantic_invalid_raises(tmp_path: Path) -> None:
    """Pydantic-invalid YAML document raises ConfigError."""
    yaml_text = textwrap.dedent("""\
        content: state-changes
        kinds: []
    """)
    cfg_path = tmp_path / "invalid.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigError, match="validation failed"):
        load_stream_config(cfg_path)


def test_load_stream_config_membership_events_round_trip(tmp_path: Path) -> None:
    """load_stream_config parses a content: membership-events YAML file correctly."""
    yaml_text = textwrap.dedent("""\
        content: membership-events
        memberships:
          - owner_kind: queue
            property: waiters
            fields:
              - priority
              - position
    """)
    cfg_path = tmp_path / "stream_membership.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    cfg = load_stream_config(cfg_path)
    assert cfg.content == "membership-events"
    assert len(cfg.memberships) == 1
    assert cfg.memberships[0].owner_kind == "queue"
    assert cfg.memberships[0].property == "waiters"
    assert cfg.memberships[0].fields == ["priority", "position"]
    assert cfg.kinds == []


# ---------------------------------------------------------------------------
# DebeziumSourceIdentity
# ---------------------------------------------------------------------------

_VALID_SOURCE = {
    "connector": "postgresql",
    "name": "myserver",
    "db": "mydb",
    "schema": "public",
    "version": "2.5.0.Final",
}


def _make_debezium_config(**source_overrides: object) -> dict[object, object]:
    source = dict(_VALID_SOURCE)
    source.update(source_overrides)
    return {"source": source}


def test_debezium_source_identity_valid() -> None:
    """All five source fields parse; schema_ populated from wire key 'schema'."""
    src = DebeziumSourceIdentity.model_validate(_VALID_SOURCE)
    assert src.connector == "postgresql"
    assert src.name == "myserver"
    assert src.db == "mydb"
    assert src.schema_ == "public"
    assert src.version == "2.5.0.Final"


def test_debezium_source_identity_schema_alias() -> None:
    """YAML key 'schema' populates schema_ (alias)."""
    src = DebeziumSourceIdentity.model_validate(
        {
            "schema": "myschema",
            **{k: v for k, v in _VALID_SOURCE.items() if k != "schema"},
        }
    )
    assert src.schema_ == "myschema"


def test_debezium_source_identity_unknown_field_raises() -> None:
    """Unknown field under debezium.source raises (extra='forbid')."""
    with pytest.raises(ValidationError):
        DebeziumSourceIdentity.model_validate({**_VALID_SOURCE, "extra_field": "bad"})


@pytest.mark.parametrize(
    "field",
    ["connector", "name", "db", "schema", "version"],
)
def test_debezium_source_identity_empty_field_raises(field: str) -> None:
    """Empty source field raises ValidationError naming the field."""
    with pytest.raises(
        ValidationError, match=f"debezium.source.{field} must be non-empty"
    ):
        DebeziumSourceIdentity.model_validate({**_VALID_SOURCE, field: ""})


def test_debezium_source_identity_missing_version_raises() -> None:
    """Missing version raises (required field)."""
    data = {k: v for k, v in _VALID_SOURCE.items() if k != "version"}
    with pytest.raises(ValidationError):
        DebeziumSourceIdentity.model_validate(data)


# ---------------------------------------------------------------------------
# DebeziumConfig
# ---------------------------------------------------------------------------


def test_debezium_config_full_block_parses() -> None:
    """Full debezium block with all source fields and schemas_enable parses."""
    cfg = DebeziumConfig.model_validate(
        {**_make_debezium_config(), "schemas_enable": True}
    )
    assert cfg.schemas_enable is True
    assert cfg.source.connector == "postgresql"


def test_debezium_config_schemas_enable_defaults_true() -> None:
    """schemas_enable defaults to True when omitted."""
    cfg = DebeziumConfig.model_validate(_make_debezium_config())
    assert cfg.schemas_enable is True


def test_debezium_config_schemas_enable_false_parses() -> None:
    """An explicit schemas_enable: false parses (the bare-payload branch —
    unwrapped messages, no {schema, payload} envelope)."""
    cfg = DebeziumConfig.model_validate(
        {**_make_debezium_config(), "schemas_enable": False}
    )
    assert cfg.schemas_enable is False
    assert cfg.source.connector == "postgresql"


def test_debezium_config_missing_source_raises() -> None:
    """Missing source block raises (required field)."""
    with pytest.raises(ValidationError):
        DebeziumConfig.model_validate({"schemas_enable": True})


def test_debezium_config_unknown_field_raises() -> None:
    """Unknown field under debezium raises (extra='forbid')."""
    with pytest.raises(ValidationError):
        DebeziumConfig.model_validate({**_make_debezium_config(), "unknown": "bad"})


# ---------------------------------------------------------------------------
# StreamConfig.debezium
# ---------------------------------------------------------------------------


def test_stream_config_debezium_absent_is_none() -> None:
    """Absent debezium block → debezium is None."""
    cfg = StreamConfig.model_validate(_make_stream_config())
    assert cfg.debezium is None


def test_stream_config_debezium_present_parsed() -> None:
    """Present debezium block is parsed as DebeziumConfig."""
    cfg = StreamConfig.model_validate(
        _make_stream_config(debezium=_make_debezium_config())
    )
    assert isinstance(cfg.debezium, DebeziumConfig)
    assert cfg.debezium.source.schema_ == "public"


def test_stream_config_debezium_empty_source_field_raises() -> None:
    """Empty string in source.* raises ValueError."""
    with pytest.raises(
        ValidationError, match="debezium.source.connector must be non-empty"
    ):
        StreamConfig.model_validate(
            _make_stream_config(debezium={"source": {**_VALID_SOURCE, "connector": ""}})
        )


def test_stream_config_debezium_unknown_field_raises() -> None:
    """Unknown field under debezium raises (extra='forbid')."""
    with pytest.raises(ValidationError):
        StreamConfig.model_validate(
            _make_stream_config(debezium={**_make_debezium_config(), "bogus": "field"})
        )


def test_load_stream_config_prop_prefix_raises(tmp_path: Path) -> None:
    """A prop__-prefixed property name in YAML raises ConfigError."""
    yaml_text = textwrap.dedent("""\
        content: state-changes
        kinds:
          - kind: patient
            properties:
              - prop__name
    """)
    cfg_path = tmp_path / "bad_props.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigError, match="validation failed"):
        load_stream_config(cfg_path)


# ---------------------------------------------------------------------------
# StreamKindSelection — types field (at StreamConfig level)
# ---------------------------------------------------------------------------


def test_stream_kind_selection_types_default_empty() -> None:
    """StreamKindSelection.types defaults to empty list."""
    ks = StreamKindSelection.model_validate({"kind": "patient", "properties": []})
    assert ks.types == []


def test_stream_kind_selection_types_non_empty() -> None:
    """A non-empty types list parses correctly."""
    ks = StreamKindSelection.model_validate(
        {"kind": "actor", "types": ["doctor", "nurse"], "properties": []}
    )
    assert ks.types == ["doctor", "nurse"]


def test_stream_kind_selection_types_prop_prefix_raises() -> None:
    """A types value with prop__ prefix raises ValueError."""
    with pytest.raises(ValidationError, match="prop__"):
        StreamKindSelection.model_validate(
            {"kind": "actor", "types": ["prop__role"], "properties": []}
        )


# ---------------------------------------------------------------------------
# StreamConfig — routing field
# ---------------------------------------------------------------------------


def test_stream_config_routing_absent_is_none() -> None:
    """StreamConfig.routing omitted resolves to None."""
    cfg = StreamConfig.model_validate(
        {
            "content": "state-changes",
            "kinds": [{"kind": "patient", "properties": []}],
        }
    )
    assert cfg.routing is None


def test_stream_config_routing_present_parses() -> None:
    """A present routing block parses to RoutingConfig."""
    cfg = StreamConfig.model_validate(
        {
            "content": "state-changes",
            "routing": {"topic_template": "{kind}.{route_table}"},
            "kinds": [{"kind": "patient", "properties": []}],
        }
    )
    assert cfg.routing is not None
    assert isinstance(cfg.routing, RoutingConfig)
    assert cfg.routing.topic_template == "{kind}.{route_table}"
    assert cfg.routing.groups == {}
    assert cfg.routing.table_identity == "source_table"


# ---------------------------------------------------------------------------
# StreamConfig — clock field
# ---------------------------------------------------------------------------


def test_stream_config_clock_absent_is_none() -> None:
    """Absent clock block → clock is None."""
    cfg = StreamConfig.model_validate(_make_stream_config())
    assert cfg.clock is None


def test_stream_config_clock_realtime_present_parsed() -> None:
    """Present clock block with realtime mode is parsed as ClockConfig."""
    cfg = StreamConfig.model_validate(
        _make_stream_config(clock={"mode": "realtime", "speed": 60.0})
    )
    assert isinstance(cfg.clock, ClockConfig)
    assert cfg.clock.mode == "realtime"
    assert cfg.clock.speed == 60.0


def test_stream_config_clock_fast_present_parsed() -> None:
    """Present clock block with fast mode is parsed as ClockConfig."""
    cfg = StreamConfig.model_validate(_make_stream_config(clock={"mode": "fast"}))
    assert isinstance(cfg.clock, ClockConfig)
    assert cfg.clock.mode == "fast"


def test_stream_config_clock_invalid_propagates() -> None:
    """Invalid nested clock block propagates the ValueError through StreamConfig."""
    with pytest.raises(ValidationError):
        StreamConfig.model_validate(_make_stream_config(clock={"mode": "realtime"}))


# ---------------------------------------------------------------------------
# StreamConfig.kafka
# ---------------------------------------------------------------------------


def test_stream_config_kafka_absent_is_none() -> None:
    """Absent kafka block → kafka is None."""
    cfg = StreamConfig.model_validate(_make_stream_config())
    assert cfg.kafka is None


def test_stream_config_kafka_present_parsed() -> None:
    """Present kafka block parses into KafkaConfig."""
    cfg = StreamConfig.model_validate(
        _make_stream_config(kafka={"bootstrap_servers": "localhost:9092"})
    )
    assert isinstance(cfg.kafka, KafkaConfig)
    assert cfg.kafka.bootstrap_servers == "localhost:9092"


def test_stream_config_kafka_empty_bootstrap_servers_raises() -> None:
    """bootstrap_servers: '' raises ValidationError."""
    with pytest.raises(ValidationError, match="non-empty"):
        StreamConfig.model_validate(
            _make_stream_config(kafka={"bootstrap_servers": ""})
        )


def test_stream_config_kafka_unknown_field_raises() -> None:
    """An unknown field under kafka raises ValidationError (extra='forbid')."""
    with pytest.raises(ValidationError):
        StreamConfig.model_validate(
            _make_stream_config(
                kafka={"bootstrap_servers": "localhost:9092", "unknown": "val"}
            )
        )
