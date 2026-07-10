"""Tests for the deterministic corrupter-config fingerprint."""

from __future__ import annotations

import re
import textwrap

import yaml

from fabulexa_forge.config.models import CorruptConfig
from fabulexa_forge.corrupters.fingerprint import fingerprint_config

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

_BASE_YAML = textwrap.dedent("""\
    seed: 42
    operations:
      - kind: null_cells
        target:
          table: records__patient
          columns: [prop__email, prop__phone]
          where: { prop__active_status: admitted }
        amount: { rate: 0.05 }
""")

# Same config, reformatted (whitespace / quoting / key order within `where`).
_REFORMATTED_YAML = textwrap.dedent("""\
    seed: 42
    operations:
      -   kind: "null_cells"
          amount:
            rate: 0.05
          target:
            columns:
              - "prop__email"
              - "prop__phone"
            table: "records__patient"
            where:
              prop__active_status: "admitted"
""")


def _config_from_yaml(text: str) -> CorruptConfig:
    return CorruptConfig.model_validate(yaml.safe_load(text))


def test_fingerprint_is_64_char_lowercase_hex() -> None:
    """The fingerprint is a 64-char lowercase hex digest."""
    fp = fingerprint_config(_config_from_yaml(_BASE_YAML))
    assert _HEX64_RE.match(fp)


def test_fingerprint_stable_across_yaml_reformat() -> None:
    """Reformatting the YAML (whitespace, quoting, `where` key order) leaves
    the fingerprint unchanged."""
    fp_a = fingerprint_config(_config_from_yaml(_BASE_YAML))
    fp_b = fingerprint_config(_config_from_yaml(_REFORMATTED_YAML))
    assert fp_a == fp_b


def test_fingerprint_changes_on_seed_change() -> None:
    """A different seed changes the fingerprint."""
    config_a = _config_from_yaml(_BASE_YAML)
    config_b = config_a.model_copy(update={"seed": 43})
    assert fingerprint_config(config_a) != fingerprint_config(config_b)


def test_fingerprint_changes_on_operation_reorder() -> None:
    """Reordering `operations` changes the fingerprint (each operation seeds
    from (seed, index))."""
    two_ops_yaml = textwrap.dedent("""\
        seed: 42
        operations:
          - kind: null_cells
            name: op_a
            target: { table: records__patient, columns: [prop__email] }
            amount: { rate: 0.05 }
          - kind: null_cells
            name: op_b
            target: { table: records__patient, columns: [prop__phone] }
            amount: { rate: 0.05 }
    """)
    reordered_yaml = textwrap.dedent("""\
        seed: 42
        operations:
          - kind: null_cells
            name: op_b
            target: { table: records__patient, columns: [prop__phone] }
            amount: { rate: 0.05 }
          - kind: null_cells
            name: op_a
            target: { table: records__patient, columns: [prop__email] }
            amount: { rate: 0.05 }
    """)
    fp_a = fingerprint_config(_config_from_yaml(two_ops_yaml))
    fp_b = fingerprint_config(_config_from_yaml(reordered_yaml))
    assert fp_a != fp_b


def test_fingerprint_changes_on_target_columns_reorder() -> None:
    """Reordering `target.columns` changes the fingerprint (fixes the cell-
    sampling enumeration order)."""
    columns_ab = textwrap.dedent("""\
        seed: 42
        operations:
          - kind: null_cells
            target: { table: records__patient, columns: [prop__email, prop__phone] }
            amount: { rate: 0.05 }
    """)
    columns_ba = textwrap.dedent("""\
        seed: 42
        operations:
          - kind: null_cells
            target: { table: records__patient, columns: [prop__phone, prop__email] }
            amount: { rate: 0.05 }
    """)
    fp_a = fingerprint_config(_config_from_yaml(columns_ab))
    fp_b = fingerprint_config(_config_from_yaml(columns_ba))
    assert fp_a != fp_b


def test_fingerprint_where_key_order_does_not_change_it() -> None:
    """A `where` map's key order does not affect the fingerprint (an
    unordered conjunction)."""
    where_ab = textwrap.dedent("""\
        seed: 42
        operations:
          - kind: null_cells
            target:
              table: records__patient
              columns: [prop__email]
              where: { prop__active_status: admitted, prop__ward: icu }
            amount: { rate: 0.05 }
    """)
    where_ba = textwrap.dedent("""\
        seed: 42
        operations:
          - kind: null_cells
            target:
              table: records__patient
              columns: [prop__email]
              where: { prop__ward: icu, prop__active_status: admitted }
            amount: { rate: 0.05 }
    """)
    fp_a = fingerprint_config(_config_from_yaml(where_ab))
    fp_b = fingerprint_config(_config_from_yaml(where_ba))
    assert fp_a == fp_b


def test_fingerprint_explicit_name_equal_to_fallback_differs_from_absent() -> None:
    """An explicit `name` equal to the "{kind}#{index}" fallback still
    fingerprints differently from an absent `name` (the fallback is resolved
    at runtime, not in the model)."""
    absent_name_yaml = textwrap.dedent("""\
        seed: 42
        operations:
          - kind: null_cells
            target: { table: records__patient, columns: [prop__email] }
            amount: { rate: 0.05 }
    """)
    explicit_name_yaml = textwrap.dedent("""\
        seed: 42
        operations:
          - kind: null_cells
            name: "null_cells#0"
            target: { table: records__patient, columns: [prop__email] }
            amount: { rate: 0.05 }
    """)
    fp_absent = fingerprint_config(_config_from_yaml(absent_name_yaml))
    fp_explicit = fingerprint_config(_config_from_yaml(explicit_name_yaml))
    assert fp_absent != fp_explicit
