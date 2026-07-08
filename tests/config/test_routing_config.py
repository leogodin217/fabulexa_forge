"""Tests for RoutingConfig validator internals (groups_well_formed, template rules)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fabulexa_export.config.models import RoutingConfig

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
