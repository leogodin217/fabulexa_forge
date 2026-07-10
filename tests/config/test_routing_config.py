"""Tests for RoutingConfig validator internals (groups_well_formed, template rules)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fabulexa_forge.config.models import RoutingConfig

# ---------------------------------------------------------------------------
# RoutingConfig — defaults
# ---------------------------------------------------------------------------


def test_routing_config_defaults() -> None:
    """RoutingConfig parses with no fields and applies documented defaults."""
    rc = RoutingConfig.model_validate({})
    assert rc.topic_template == "{route_table}"
    assert rc.groups == {}
    assert rc.table_identity == "source_table"


# ---------------------------------------------------------------------------
# RoutingConfig — table_identity
# ---------------------------------------------------------------------------


def test_routing_config_table_identity_topic_parses() -> None:
    """The explicit 'topic' table_identity value (resolved topic as Debezium
    source.table) parses — the non-default arm of the Literal."""
    rc = RoutingConfig.model_validate({"table_identity": "topic"})
    assert rc.table_identity == "topic"


def test_routing_config_table_identity_unknown_value_raises() -> None:
    """A table_identity outside Literal['source_table', 'topic'] raises."""
    with pytest.raises(ValidationError, match="table_identity"):
        RoutingConfig.model_validate({"table_identity": "resolved_topic"})


# ---------------------------------------------------------------------------
# RoutingConfig — valid templates
# ---------------------------------------------------------------------------


def test_routing_config_literal_template() -> None:
    """A literal template with no placeholder is accepted."""
    rc = RoutingConfig.model_validate({"topic_template": "all-events"})
    assert rc.topic_template == "all-events"


def test_routing_config_multi_placeholder_template() -> None:
    """A template with multiple placeholders is accepted."""
    rc = RoutingConfig.model_validate({"topic_template": "{kind}.{sub_type}"})
    assert rc.topic_template == "{kind}.{sub_type}"


def test_routing_config_prefix_template() -> None:
    """A prefix template is accepted."""
    rc = RoutingConfig.model_validate({"topic_template": "cdc.{route_table}"})
    assert rc.topic_template == "cdc.{route_table}"


def test_routing_config_groups_map() -> None:
    """A groups map with distinct members is accepted."""
    rc = RoutingConfig.model_validate(
        {
            "groups": {
                "premium": ["vip_customer", "customer"],
                "staff": ["doctor", "nurse"],
            }
        }
    )
    assert rc.groups == {
        "premium": ["vip_customer", "customer"],
        "staff": ["doctor", "nurse"],
    }


def test_routing_config_escaped_braces_accepted() -> None:
    """A template with escaped literal braces is accepted."""
    rc = RoutingConfig.model_validate({"topic_template": "{{literal}}"})
    assert rc.topic_template == "{{literal}}"


# ---------------------------------------------------------------------------
# RoutingConfig — groups_well_formed rejections
# ---------------------------------------------------------------------------


def test_routing_config_empty_template_raises() -> None:
    """An empty topic_template raises ValueError."""
    with pytest.raises(ValidationError, match="non-empty"):
        RoutingConfig.model_validate({"topic_template": ""})


def test_routing_config_unbalanced_brace_raises() -> None:
    """An unbalanced brace in topic_template raises ValueError."""
    with pytest.raises(ValidationError, match="unbalanced"):
        RoutingConfig.model_validate({"topic_template": "{route_table"})


def test_routing_config_format_spec_raises() -> None:
    """A format-spec on a placeholder raises ValueError."""
    with pytest.raises(ValidationError, match="format-spec"):
        RoutingConfig.model_validate({"topic_template": "{route_table:>8}"})


def test_routing_config_conversion_raises() -> None:
    """A conversion on a placeholder raises ValueError."""
    with pytest.raises(ValidationError, match="conversion"):
        RoutingConfig.model_validate({"topic_template": "{kind!r}"})


def test_routing_config_empty_target_raises() -> None:
    """An empty groups target string raises ValueError."""
    with pytest.raises(ValidationError, match="non-empty"):
        RoutingConfig.model_validate({"groups": {"": ["customer"]}})


def test_routing_config_empty_member_raises() -> None:
    """An empty member string in a group raises ValueError."""
    with pytest.raises(ValidationError, match="empty member"):
        RoutingConfig.model_validate({"groups": {"premium": [""]}})


def test_routing_config_shared_member_raises() -> None:
    """A member listed in two groups raises ValueError."""
    with pytest.raises(ValidationError, match="more than one group"):
        RoutingConfig.model_validate(
            {
                "groups": {
                    "groupA": ["customer"],
                    "groupB": ["customer", "staff"],
                }
            }
        )


# ---------------------------------------------------------------------------
# groups targets follow the Kafka topic-name rule (also forecloses jsonl
# path traversal — a target becomes the .jsonl filename stem)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_target",
    [
        "../../etc/cron.d/evil",
        "/etc/cron.d/evil",
        "a/b",
        "topic name",
        ".",
        "..",
    ],
)
def test_routing_config_invalid_group_target_raises(bad_target: str) -> None:
    """A groups target outside ^[A-Za-z0-9._-]+$ (or '.'/'..') raises."""
    with pytest.raises(ValidationError, match="valid topic name"):
        RoutingConfig.model_validate({"groups": {bad_target: ["customer"]}})


def test_routing_config_kafka_convention_group_targets_pass() -> None:
    """Dots, dashes, and underscores are all legal in a topic name."""
    cfg = RoutingConfig.model_validate(
        {"groups": {"cdc.public.orders-v2_x": ["orders"]}}
    )
    assert "cdc.public.orders-v2_x" in cfg.groups
