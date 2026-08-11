"""Tests for the declared-stream grammar: KindStream, MembershipStream,
StreamDeclaration union discrimination, StreamConfig, and load_stream_config."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from fabulexa_forge.config.loader import load_stream_config
from fabulexa_forge.config.models import (
    ClockConfig,
    DebeziumConfig,
    DebeziumSourceIdentity,
    KafkaConfig,
    KindStream,
    MembershipStream,
    RebaseConfig,
    StreamConfig,
)
from fabulexa_forge.errors import ConfigError

# ---------------------------------------------------------------------------
# KindStream
# ---------------------------------------------------------------------------


def _make_kind_stream(**overrides: object) -> dict[object, object]:
    base: dict[object, object] = {
        "name": "patients",
        "kind": "patient",
        "properties": ["name", "status"],
    }
    base.update(overrides)
    return base


def test_kind_stream_valid() -> None:
    """A valid KindStream with bare property names parses cleanly."""
    ks = KindStream.model_validate(_make_kind_stream())
    assert ks.name == "patients"
    assert ks.kind == "patient"
    assert ks.properties == ["name", "status"]
    assert ks.sub_types is None


def test_kind_stream_properties_empty_accepted() -> None:
    """properties: [] is accepted — the explicit notification-feed declaration."""
    ks = KindStream.model_validate(_make_kind_stream(properties=[]))
    assert ks.properties == []


def test_kind_stream_properties_missing_raises() -> None:
    """`properties` has no default; omitting it raises (required field)."""
    entry = _make_kind_stream()
    del entry["properties"]
    with pytest.raises(ValidationError):
        KindStream.model_validate(entry)


def test_kind_stream_properties_prop_prefix_raises() -> None:
    """A property carrying the prop__ prefix raises ValueError."""
    with pytest.raises(ValidationError, match="prop__"):
        KindStream.model_validate(_make_kind_stream(properties=["prop__name"]))


def test_kind_stream_properties_duplicate_raises() -> None:
    """A repeated property name raises ValueError."""
    with pytest.raises(ValidationError, match="duplicate names"):
        KindStream.model_validate(_make_kind_stream(properties=["name", "name"]))


def test_kind_stream_sub_types_absent_is_none() -> None:
    """Absent `sub_types` -> None (full discriminator domain)."""
    ks = KindStream.model_validate(_make_kind_stream())
    assert ks.sub_types is None


def test_kind_stream_sub_types_valid() -> None:
    """A non-empty, duplicate-free sub_types list parses correctly."""
    ks = KindStream.model_validate(
        _make_kind_stream(kind="actor", sub_types=["doctor", "nurse"], properties=[])
    )
    assert ks.sub_types == ["doctor", "nurse"]


def test_kind_stream_sub_types_empty_raises() -> None:
    """`sub_types: []` raises ValueError (omit the field instead)."""
    with pytest.raises(ValidationError, match="non-empty"):
        KindStream.model_validate(_make_kind_stream(sub_types=[]))


def test_kind_stream_sub_types_duplicate_raises() -> None:
    """A repeated sub_types value raises ValueError."""
    with pytest.raises(ValidationError, match="duplicate names"):
        KindStream.model_validate(_make_kind_stream(sub_types=["doctor", "doctor"]))


def test_kind_stream_unknown_field_raises() -> None:
    """An unknown field on KindStream raises (extra='forbid')."""
    with pytest.raises(ValidationError):
        KindStream.model_validate(_make_kind_stream(extra="bad"))


# ---------------------------------------------------------------------------
# MembershipStream
# ---------------------------------------------------------------------------


def _make_membership_stream(**overrides: object) -> dict[object, object]:
    base: dict[object, object] = {
        "name": "queue-waiters",
        "membership": {"kind": "queue", "property": "waiters"},
        "fields": ["priority"],
    }
    base.update(overrides)
    return base


def test_membership_stream_valid() -> None:
    """A valid MembershipStream with bare field names parses cleanly."""
    ms = MembershipStream.model_validate(_make_membership_stream())
    assert ms.name == "queue-waiters"
    assert ms.membership.kind == "queue"
    assert ms.membership.property == "waiters"
    assert ms.fields == ["priority"]


def test_membership_stream_fields_empty_accepted() -> None:
    """fields: [] is accepted — the explicit owner-identity-only declaration."""
    ms = MembershipStream.model_validate(_make_membership_stream(fields=[]))
    assert ms.fields == []


def test_membership_stream_fields_missing_raises() -> None:
    """`fields` has no default; omitting it raises (required field)."""
    entry = _make_membership_stream()
    del entry["fields"]
    with pytest.raises(ValidationError):
        MembershipStream.model_validate(entry)


def test_membership_stream_fields_elem_prefix_raises() -> None:
    """A field beginning with 'elem__' raises ValueError."""
    with pytest.raises(ValidationError, match="elem__"):
        MembershipStream.model_validate(_make_membership_stream(fields=["elem__x"]))


def test_membership_stream_fields_member_prefix_raises() -> None:
    """A field beginning with 'member__' raises ValueError."""
    with pytest.raises(ValidationError, match="member__"):
        MembershipStream.model_validate(_make_membership_stream(fields=["member__y"]))


def test_membership_stream_fields_duplicate_raises() -> None:
    """A repeated field name raises ValueError."""
    with pytest.raises(ValidationError, match="duplicate names"):
        MembershipStream.model_validate(
            _make_membership_stream(fields=["priority", "priority"])
        )


def test_membership_stream_unknown_field_raises() -> None:
    """An unknown field on MembershipStream raises (extra='forbid')."""
    with pytest.raises(ValidationError):
        MembershipStream.model_validate(_make_membership_stream(extra="bad"))


# ---------------------------------------------------------------------------
# Stream name rule (shared by KindStream and MembershipStream)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "../../etc/cron.d/evil",
        "/etc/cron.d/evil",
        "a/b",
        "topic name",
        ".",
        "..",
        "",
    ],
)
def test_kind_stream_invalid_name_raises(bad_name: str) -> None:
    """A name outside ^[A-Za-z0-9._-]+$ (or '.'/'..') raises on KindStream."""
    with pytest.raises(ValidationError, match="valid topic name"):
        KindStream.model_validate(_make_kind_stream(name=bad_name))


def test_membership_stream_invalid_name_raises() -> None:
    """The same name rule is enforced on MembershipStream."""
    with pytest.raises(ValidationError, match="valid topic name"):
        MembershipStream.model_validate(_make_membership_stream(name="a/b"))


def test_kind_stream_kafka_convention_name_passes() -> None:
    """Dots, dashes, and underscores are all legal in a stream name."""
    ks = KindStream.model_validate(_make_kind_stream(name="cdc.public.patients-v2_x"))
    assert ks.name == "cdc.public.patients-v2_x"


# ---------------------------------------------------------------------------
# StreamDeclaration — union discrimination (via StreamConfig.streams)
# ---------------------------------------------------------------------------


def test_stream_declaration_kind_shape_discriminates_to_kindstream() -> None:
    """An entry carrying 'kind' parses as KindStream."""
    cfg = StreamConfig.model_validate(
        {"content": "state-changes", "streams": [_make_kind_stream()]}
    )
    assert isinstance(cfg.streams[0], KindStream)


def test_stream_declaration_membership_shape_discriminates_to_membershipstream() -> (
    None
):
    """An entry carrying 'membership' parses as MembershipStream."""
    cfg = StreamConfig.model_validate(
        {
            "content": "membership-events",
            "streams": [_make_membership_stream()],
        }
    )
    assert isinstance(cfg.streams[0], MembershipStream)


def test_stream_declaration_both_kind_and_membership_raises() -> None:
    """An entry carrying both 'kind' and 'membership' fails naming the two shapes."""
    entry = _make_kind_stream()
    entry["membership"] = {"kind": "queue", "property": "waiters"}
    with pytest.raises(ValidationError, match="both 'kind' and 'membership'"):
        StreamConfig.model_validate({"content": "state-changes", "streams": [entry]})


def test_stream_declaration_neither_kind_nor_membership_raises() -> None:
    """An entry carrying neither 'kind' nor 'membership' fails naming the two shapes."""
    entry = {"name": "orphan", "properties": []}
    with pytest.raises(ValidationError, match="neither 'kind' nor 'membership'"):
        StreamConfig.model_validate({"content": "state-changes", "streams": [entry]})


# ---------------------------------------------------------------------------
# StreamConfig — content/shape match
# ---------------------------------------------------------------------------


def _make_stream_config(**overrides: object) -> dict[object, object]:
    base: dict[object, object] = {
        "content": "state-changes",
        "streams": [_make_kind_stream()],
    }
    base.update(overrides)
    return base


def test_stream_config_state_changes_valid() -> None:
    """content='state-changes' with KindStream entries parses correctly."""
    cfg = StreamConfig.model_validate(
        _make_stream_config(
            streams=[
                _make_kind_stream(name="patients", kind="patient"),
                _make_kind_stream(name="wards", kind="ward", properties=[]),
            ]
        )
    )
    assert cfg.content == "state-changes"
    assert len(cfg.streams) == 2


def test_stream_config_membership_events_valid() -> None:
    """content='membership-events' with MembershipStream entries parses correctly."""
    cfg = StreamConfig.model_validate(
        {
            "content": "membership-events",
            "streams": [_make_membership_stream()],
        }
    )
    assert cfg.content == "membership-events"
    assert len(cfg.streams) == 1
    assert isinstance(cfg.streams[0], MembershipStream)


def test_stream_config_kind_stream_under_membership_content_raises() -> None:
    """A KindStream entry under content='membership-events' fails (shape mismatch)."""
    with pytest.raises(ValidationError, match="do not match"):
        StreamConfig.model_validate(
            {"content": "membership-events", "streams": [_make_kind_stream()]}
        )


def test_stream_config_membership_stream_under_state_changes_content_raises() -> None:
    """A MembershipStream entry under content='state-changes' fails (shape mismatch)."""
    with pytest.raises(ValidationError, match="do not match"):
        StreamConfig.model_validate(
            _make_stream_config(streams=[_make_membership_stream()])
        )


def test_stream_config_invalid_content_raises() -> None:
    """content='snapshots' still rejects (Literal)."""
    with pytest.raises(ValidationError):
        StreamConfig.model_validate(_make_stream_config(content="snapshots"))


def test_stream_config_streams_empty_raises() -> None:
    """An empty `streams` list raises from streams_match_content."""
    with pytest.raises(ValidationError, match="non-empty"):
        StreamConfig.model_validate(_make_stream_config(streams=[]))


def test_stream_config_unknown_top_level_field_raises() -> None:
    """Unknown top-level field raises (extra='forbid')."""
    with pytest.raises(ValidationError):
        StreamConfig.model_validate(_make_stream_config(unknown_field="bad"))


def test_stream_config_routing_field_no_longer_parses() -> None:
    """A `routing:` block on StreamConfig raises — RoutingConfig is retired;
    table_identity now lives under debezium."""
    with pytest.raises(ValidationError):
        StreamConfig.model_validate(
            _make_stream_config(routing={"topic_template": "{kind}"})
        )


# ---------------------------------------------------------------------------
# StreamConfig — stream-name uniqueness
# ---------------------------------------------------------------------------


def test_stream_config_duplicate_stream_name_raises() -> None:
    """Two streams sharing a name raises from stream_names_unique."""
    with pytest.raises(ValidationError, match="duplicate stream names"):
        StreamConfig.model_validate(
            _make_stream_config(
                streams=[
                    _make_kind_stream(name="patients", kind="patient"),
                    _make_kind_stream(name="patients", kind="ward"),
                ]
            )
        )


def test_stream_config_same_kind_two_streams_parses() -> None:
    """The same kind fed to two distinctly-named streams parses correctly
    (identity is the name, not the kind)."""
    cfg = StreamConfig.model_validate(
        _make_stream_config(
            streams=[
                _make_kind_stream(name="patients-all", kind="patient"),
                _make_kind_stream(
                    name="patients-status", kind="patient", properties=["status"]
                ),
            ]
        )
    )
    assert len(cfg.streams) == 2
    assert {s.name for s in cfg.streams} == {"patients-all", "patients-status"}


# ---------------------------------------------------------------------------
# load_stream_config
# ---------------------------------------------------------------------------


def test_load_stream_config_valid(tmp_path: Path) -> None:
    """load_stream_config parses a valid YAML file correctly."""
    yaml_text = textwrap.dedent("""\
        content: state-changes
        streams:
          - name: patients
            kind: patient
            properties:
              - name
              - status
          - name: wards
            kind: ward
            properties: []
    """)
    cfg_path = tmp_path / "stream.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    cfg = load_stream_config(cfg_path)
    assert cfg.content == "state-changes"
    assert len(cfg.streams) == 2


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
        streams: []
    """)
    cfg_path = tmp_path / "invalid.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigError, match="validation failed"):
        load_stream_config(cfg_path)


def test_load_stream_config_membership_events_round_trip(tmp_path: Path) -> None:
    """load_stream_config parses a content: membership-events YAML file correctly."""
    yaml_text = textwrap.dedent("""\
        content: membership-events
        streams:
          - name: queue-waiters
            membership:
              kind: queue
              property: waiters
            fields:
              - priority
              - position
    """)
    cfg_path = tmp_path / "stream_membership.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    cfg = load_stream_config(cfg_path)
    assert cfg.content == "membership-events"
    assert len(cfg.streams) == 1
    stream = cfg.streams[0]
    assert isinstance(stream, MembershipStream)
    assert stream.membership.kind == "queue"
    assert stream.membership.property == "waiters"
    assert stream.fields == ["priority", "position"]


def test_load_stream_config_prop_prefix_raises(tmp_path: Path) -> None:
    """A prop__-prefixed property name in YAML raises ConfigError."""
    yaml_text = textwrap.dedent("""\
        content: state-changes
        streams:
          - name: patients
            kind: patient
            properties:
              - prop__name
    """)
    cfg_path = tmp_path / "bad_props.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigError, match="validation failed"):
        load_stream_config(cfg_path)


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
# DebeziumConfig.table_identity (moved here from the retired RoutingConfig)
# ---------------------------------------------------------------------------


def test_debezium_config_table_identity_defaults_source_table() -> None:
    """table_identity defaults to 'source_table' when omitted."""
    cfg = DebeziumConfig.model_validate(_make_debezium_config())
    assert cfg.table_identity == "source_table"


def test_debezium_config_table_identity_topic_parses() -> None:
    """The explicit 'topic' table_identity value (the declaring stream's name
    as Debezium source.table) parses — the non-default arm of the Literal."""
    cfg = DebeziumConfig.model_validate(
        {**_make_debezium_config(), "table_identity": "topic"}
    )
    assert cfg.table_identity == "topic"


def test_debezium_config_table_identity_unknown_value_raises() -> None:
    """A table_identity outside Literal['source_table', 'topic'] raises."""
    with pytest.raises(ValidationError, match="table_identity"):
        DebeziumConfig.model_validate(
            {**_make_debezium_config(), "table_identity": "resolved_topic"}
        )


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


# ---------------------------------------------------------------------------
# StreamConfig.rebase
# ---------------------------------------------------------------------------


def test_stream_config_rebase_absent_is_none() -> None:
    """Absent rebase block → rebase is None."""
    cfg = StreamConfig.model_validate(_make_stream_config())
    assert cfg.rebase is None


def test_stream_config_rebase_present_parsed() -> None:
    """Present rebase block is parsed as RebaseConfig."""
    cfg = StreamConfig.model_validate(_make_stream_config(rebase={"timezone": "UTC"}))
    assert isinstance(cfg.rebase, RebaseConfig)
    assert cfg.rebase.timezone == "UTC"


# ---------------------------------------------------------------------------
# StreamConfig.clock
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
