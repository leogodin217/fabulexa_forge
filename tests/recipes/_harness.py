"""Recipe test harness: discovery, expectation loading, and output assertion.

Provides:
  - RecipeFolder     — discovered recipe folder metadata
  - RecipeExpectation / TableExpectation — dimensional expectation schema (Pydantic)
  - StreamRecipeExpectation / StreamExpectation — streaming (JSONL) expectation schema
  - CorruptRecipeExpectation — corrupter (defects.json) expectation schema
  - discover_recipes — enumerate a recipe corpus (any folder holding a config.yaml)
  - load_expectation — parse expect.yaml into a RecipeExpectation (dimensional)
  - load_stream_expectation — parse expect.yaml into a StreamRecipeExpectation
  - load_corrupt_expectation — parse expect.yaml into a CorruptRecipeExpectation
  - assert_recipe_output — compare DuckDB output against a RecipeExpectation
  - assert_stream_output — compare <kind>.jsonl output against a StreamRecipeExpectation
  - assert_corrupt_output — compare defects.json against a CorruptRecipeExpectation
  - RecipeCorpusError / RecipeExpectationError — test-only exceptions
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import duckdb
import yaml
from pydantic import BaseModel, ValidationError

# ---------------------------------------------------------------------------
# Test-only exceptions
# ---------------------------------------------------------------------------


class RecipeCorpusError(Exception):
    """Raised when the recipe corpus root is absent or not a directory."""


class RecipeExpectationError(Exception):
    """Raised when an expect.yaml file is missing, invalid YAML, or schema-invalid."""


# ---------------------------------------------------------------------------
# Pydantic expectation models
# ---------------------------------------------------------------------------


class TableExpectation(BaseModel):
    """Expectations for one output table."""

    columns: list[str]
    """Exact set of column names expected in the table (order-insensitive)."""
    row_count: int | None = None
    """When present, assert the table has exactly this many rows."""
    contains_rows: list[dict[str, Any]] = []
    """Each entry must match at least one output row on every specified column.

    A YAML ``null`` value for a key means SQL IS NULL.
    """


class RecipeExpectation(BaseModel):
    """Top-level structure of an expect.yaml file."""

    tables: dict[str, TableExpectation]
    """Mapping of output-table name -> expectations for that table."""


# ---------------------------------------------------------------------------
# RecipeFolder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecipeFolder:
    """A discovered recipe folder."""

    name: str
    """The recipe's folder name (used as the test ID)."""
    config_path: Path
    """Absolute path to config.yaml within this recipe folder."""
    expect_path: Path
    """Absolute path to expect.yaml within this recipe folder."""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_recipes(recipes_root: Path) -> list[RecipeFolder]:
    """Discover all recipe folders under recipes_root.

    Args:
        recipes_root: The root directory containing one sub-directory per recipe.

    Returns:
        A list of RecipeFolder, sorted by name, one per immediate sub-directory
        that holds a config.yaml. A sub-directory without a config.yaml is a
        container (e.g. the ``streaming/`` sub-corpus root), not a recipe, and is
        skipped — so the dimensional and streaming corpora can nest under one tree.

    Raises:
        RecipeCorpusError: recipes_root is absent or not a directory.
    """
    if not recipes_root.exists():
        raise RecipeCorpusError(f"recipe corpus root does not exist: {recipes_root}")
    if not recipes_root.is_dir():
        raise RecipeCorpusError(
            f"recipe corpus root is not a directory: {recipes_root}"
        )
    folders = sorted(
        (
            RecipeFolder(
                name=child.name,
                config_path=child / "config.yaml",
                expect_path=child / "expect.yaml",
            )
            for child in recipes_root.iterdir()
            if child.is_dir() and (child / "config.yaml").exists()
        ),
        key=lambda r: r.name,
    )
    return folders


# ---------------------------------------------------------------------------
# Expectation loading
# ---------------------------------------------------------------------------


def load_expectation(expect_path: Path) -> RecipeExpectation:
    """Parse and validate an expect.yaml file.

    Args:
        expect_path: Path to the expect.yaml file.

    Returns:
        A validated RecipeExpectation.

    Raises:
        RecipeExpectationError: The file is absent, not valid YAML, or fails
            schema validation.
    """
    try:
        raw_text = expect_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RecipeExpectationError(f"expect.yaml not found: {expect_path}") from None

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise RecipeExpectationError(f"invalid YAML in {expect_path}: {exc}") from exc

    try:
        return RecipeExpectation.model_validate(data)
    except ValidationError as exc:
        raise RecipeExpectationError(
            f"expect.yaml schema failure in {expect_path}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Output assertion
# ---------------------------------------------------------------------------


def assert_recipe_output(expectation: RecipeExpectation, duckdb_path: Path) -> None:
    """Assert that a DuckDB output file matches the given expectation.

    Checks (in order):
    1. The set of output tables exactly equals the declared set.
    2. Each table's column set equals the declared columns (order-insensitive).
    3. ``row_count`` when present.
    4. Each ``contains_rows`` entry matches at least one output row on every
       specified column; a YAML ``null`` value means SQL IS NULL.

    Args:
        expectation: The loaded RecipeExpectation.
        duckdb_path: Path to the DuckDB file to inspect (opened read-only).

    Raises:
        AssertionError: Any of the above checks fails; the message names the
            specific mismatch.
    """
    conn = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        actual_tables: set[str] = {
            row[0] for row in conn.execute("SHOW TABLES").fetchall()
        }
        expected_tables = set(expectation.tables.keys())
        if actual_tables != expected_tables:
            missing = expected_tables - actual_tables
            extra = actual_tables - expected_tables
            parts = []
            if missing:
                parts.append(f"missing tables: {sorted(missing)}")
            if extra:
                parts.append(f"unexpected tables: {sorted(extra)}")
            raise AssertionError(f"output table set mismatch — {'; '.join(parts)}")

        for table_name, tbl_exp in expectation.tables.items():
            # Column check
            schema_rows = conn.execute(f'DESCRIBE "{table_name}"').fetchall()
            actual_cols: set[str] = {row[0] for row in schema_rows}
            expected_cols = set(tbl_exp.columns)
            if actual_cols != expected_cols:
                missing_cols = expected_cols - actual_cols
                extra_cols = actual_cols - expected_cols
                parts = []
                if missing_cols:
                    parts.append(f"missing columns: {sorted(missing_cols)}")
                if extra_cols:
                    parts.append(f"unexpected columns: {sorted(extra_cols)}")
                raise AssertionError(
                    f"table '{table_name}' column mismatch — {'; '.join(parts)}"
                )

            # Row count check
            if tbl_exp.row_count is not None:
                (actual_count,) = conn.execute(
                    f'SELECT COUNT(*) FROM "{table_name}"'
                ).fetchone()  # type: ignore[misc]
                if actual_count != tbl_exp.row_count:
                    raise AssertionError(
                        f"table '{table_name}' row_count mismatch:"
                        f" expected {tbl_exp.row_count}, got {actual_count}"
                    )

            # Contains-rows check
            for row_idx, expected_row in enumerate(tbl_exp.contains_rows):
                predicates: list[str] = []
                params: list[object] = []
                for col, val in expected_row.items():
                    if val is None:
                        predicates.append(f'"{col}" IS NULL')
                    else:
                        predicates.append(f'"{col}" = ?')
                        params.append(val)
                where_clause = " AND ".join(predicates) if predicates else "TRUE"
                sql = f'SELECT COUNT(*) FROM "{table_name}" WHERE {where_clause}'
                (match_count,) = conn.execute(sql, params).fetchone()  # type: ignore[misc]
                if match_count == 0:
                    raise AssertionError(
                        f"table '{table_name}' contains_rows[{row_idx}]"
                        f" matched no output rows: {expected_row}"
                    )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Streaming (JSONL) expectation models
# ---------------------------------------------------------------------------


class StreamExpectation(BaseModel):
    """Expectations for one output stream — a ``<kind>.jsonl`` file."""

    event_count: int | None = None
    """When present, assert the stream holds exactly this many events (lines)."""
    contains_events: list[dict[str, Any]] = []
    """Each entry must match at least one event object on every specified field.

    Field language (one flat YAML map per expected event):
      - ``seq`` / ``op`` / ``ts`` / ``kind`` — matched against the top-level field.
      - ``record_id`` — matched against the message key (``key.record_id``).
      - ``after`` — a mapping is a subset match against the after-image; the scalar
        ``null`` asserts the after-image is JSON null (the delete tombstone).
      - ``after.<col>`` — matched against one after-image key (``null`` means JSON
        null); never matches a delete (whose after-image is null).
    """


class StreamRecipeExpectation(BaseModel):
    """Top-level structure of a streaming recipe's expect.yaml."""

    format: Literal["jsonl", "debezium"] = "jsonl"
    """Which renderer the recipe runs through. Defaults to ``jsonl`` (the run is
    streamed with ``--fmt jsonl``); set ``debezium`` to run the recipe through the
    Debezium renderer (``--fmt debezium``), which requires the config to carry a
    ``debezium`` block and a resolvable anchor. The output is still one
    ``<topic>.jsonl`` file per topic; for ``debezium`` each line is a Debezium value
    message, and ``contains_events`` predicates match against the *envelope*
    (``payload`` unwrapped when ``schemas_enable`` is on) — top-level ``op`` /
    ``after`` plus dotted paths into ``source`` / ``before`` / ``after``."""

    streams: dict[str, StreamExpectation]
    """Mapping of stream name (the ``<kind>`` of ``<kind>.jsonl``) -> expectations.

    The key set must equal the set of emitted ``<kind>.jsonl`` stems, which (file
    sink) is exactly the kinds the config selected — including any that emit zero
    events.
    """


def load_stream_expectation(expect_path: Path) -> StreamRecipeExpectation:
    """Parse and validate a streaming recipe's expect.yaml.

    Args:
        expect_path: Path to the expect.yaml file.

    Returns:
        A validated StreamRecipeExpectation.

    Raises:
        RecipeExpectationError: The file is absent, not valid YAML, or fails
            schema validation.
    """
    try:
        raw_text = expect_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RecipeExpectationError(f"expect.yaml not found: {expect_path}") from None

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise RecipeExpectationError(f"invalid YAML in {expect_path}: {exc}") from exc

    try:
        return StreamRecipeExpectation.model_validate(data)
    except ValidationError as exc:
        raise RecipeExpectationError(
            f"expect.yaml schema failure in {expect_path}: {exc}"
        ) from exc


def _dotted_lookup(obj: dict[str, Any], path: str) -> tuple[bool, Any]:
    """Traverse a dotted field path (e.g. ``source.table``) into a nested dict.

    Args:
        obj: The root object (a decoded JSON dict).
        path: A dot-separated key path.

    Returns:
        ``(True, value)`` when every segment resolves to a dict key; ``(False,
        None)`` when a segment is missing or a non-dict is encountered before
        the path is exhausted.
    """
    cursor: Any = obj
    for segment in path.split("."):
        if not isinstance(cursor, dict) or segment not in cursor:
            return False, None
        cursor = cursor[segment]
    return True, cursor


def _event_matches(event: dict[str, Any], predicate: dict[str, Any]) -> bool:
    """Return True iff the event object satisfies every field in the predicate.

    See StreamExpectation.contains_events for the field language.

    Args:
        event: One decoded JSONL event object.
        predicate: A single contains_events entry.

    Returns:
        True iff every predicate field matches.
    """
    for field, expected in predicate.items():
        if field == "record_id":
            if event.get("key", {}).get("record_id") != expected:
                return False
        elif field == "after":
            after = event.get("after")
            if isinstance(expected, dict):
                if not isinstance(after, dict):
                    return False
                if any(after.get(k) != v for k, v in expected.items()):
                    return False
            elif after != expected:  # scalar/null equality (e.g. tombstone)
                return False
        elif field.startswith("after."):
            after = event.get("after")
            if not isinstance(after, dict):
                return False
            if after.get(field[len("after.") :]) != expected:
                return False
        elif "." in field:
            # Generic nested traversal — used by the Debezium envelope to reach
            # source.<col> (e.g. source.table, source.lsn) and before.<col>
            # (the key-only delete before-image). A missing segment, or a scalar
            # where a dict was expected, is a non-match.
            found, value = _dotted_lookup(event, field)
            if not found or value != expected:
                return False
        elif event.get(field) != expected:
            return False
    return True


def assert_stream_output(expectation: StreamRecipeExpectation, out_dir: Path) -> None:
    """Assert that a file-sink stream output matches the given expectation.

    Checks (in order):
    1. The set of emitted ``<kind>.jsonl`` stems exactly equals the declared
       stream set.
    2. Each stream's ``event_count`` when present.
    3. Each ``contains_events`` entry matches at least one event in that stream.

    Args:
        expectation: The loaded StreamRecipeExpectation.
        out_dir: Directory holding the one-file-per-kind JSONL output.

    Raises:
        AssertionError: Any of the above checks fails; the message names the
            specific mismatch.
    """
    actual_streams = {p.stem for p in out_dir.glob("*.jsonl")}
    expected_streams = set(expectation.streams)
    if actual_streams != expected_streams:
        missing = expected_streams - actual_streams
        extra = actual_streams - expected_streams
        parts = []
        if missing:
            parts.append(f"missing streams: {sorted(missing)}")
        if extra:
            parts.append(f"unexpected streams: {sorted(extra)}")
        raise AssertionError(f"output stream set mismatch — {'; '.join(parts)}")

    for stream_name, stream_exp in expectation.streams.items():
        path = out_dir / f"{stream_name}.jsonl"
        events = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        if expectation.format == "debezium":
            # Match against the Debezium envelope: unwrap {schema, payload} when
            # schemas are enabled; a bare-payload run (schemas_enable: false) has
            # no "payload" key and is already the envelope.
            events = [
                evt["payload"] if isinstance(evt, dict) and "payload" in evt else evt
                for evt in events
            ]

        if stream_exp.event_count is not None and len(events) != stream_exp.event_count:
            raise AssertionError(
                f"stream '{stream_name}' event_count mismatch:"
                f" expected {stream_exp.event_count}, got {len(events)}"
            )

        for evt_idx, predicate in enumerate(stream_exp.contains_events):
            if not any(_event_matches(evt, predicate) for evt in events):
                raise AssertionError(
                    f"stream '{stream_name}' contains_events[{evt_idx}]"
                    f" matched no emitted event: {predicate}"
                )


# ---------------------------------------------------------------------------
# Corrupter (defects.json) expectation models
# ---------------------------------------------------------------------------


class CorruptRecipeExpectation(BaseModel):
    """Top-level structure of a corrupt recipe's expect.yaml."""

    defect_counts: dict[str, int]
    """Expected ``counts.by_class`` mapping — exact match against defects.json."""
    impact_union: list[str]
    """Expected non-sentinel impact-code set (order-insensitive): every real
    ImpactCode the manifest's defects union to, excluding 'beyond-c1-c12'."""
    contains_defects: list[dict[str, Any]] = []
    """Each entry must match at least one manifest defect.

    Field language (one flat YAML map per expected defect):
      - ``class`` / ``rule`` — matched against the defect's top-level field.
      - ``impact`` — exact set match (order-insensitive) against the defect's
        impact list.
      - ``location.<field>`` — dotted traversal into the defect's location
        object (e.g. ``location.table``, ``location.column``,
        ``location.row.category``).
    """


def load_corrupt_expectation(expect_path: Path) -> CorruptRecipeExpectation:
    """Parse and validate a corrupt recipe's expect.yaml.

    Args:
        expect_path: Path to the expect.yaml file.

    Returns:
        A validated CorruptRecipeExpectation.

    Raises:
        RecipeExpectationError: The file is absent, not valid YAML, or fails
            schema validation.
    """
    try:
        raw_text = expect_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RecipeExpectationError(f"expect.yaml not found: {expect_path}") from None

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise RecipeExpectationError(f"invalid YAML in {expect_path}: {exc}") from exc

    try:
        return CorruptRecipeExpectation.model_validate(data)
    except ValidationError as exc:
        raise RecipeExpectationError(
            f"expect.yaml schema failure in {expect_path}: {exc}"
        ) from exc


def _defect_matches(defect: dict[str, Any], predicate: dict[str, Any]) -> bool:
    """Return True iff the manifest defect satisfies every field in the predicate.

    See CorruptRecipeExpectation.contains_defects for the field language.

    Args:
        defect: One decoded manifest defect object (an entry of `defects`).
        predicate: A single contains_defects entry.

    Returns:
        True iff every predicate field matches.
    """
    for field, expected in predicate.items():
        if field == "impact":
            if set(defect.get("impact", [])) != set(expected):
                return False
        elif "." in field:
            found, value = _dotted_lookup(defect, field)
            if not found or value != expected:
                return False
        elif defect.get(field) != expected:
            return False
    return True


def assert_corrupt_output(
    expectation: CorruptRecipeExpectation, manifest_path: Path
) -> None:
    """Assert that a defects.json manifest matches the given expectation.

    Checks (in order):
    1. ``counts.by_class`` exactly equals the declared defect_counts.
    2. The non-sentinel impact-code union over all defects exactly equals the
       declared impact_union.
    3. Each ``contains_defects`` entry matches at least one manifest defect.

    Args:
        expectation: The loaded CorruptRecipeExpectation.
        manifest_path: Path to the defects.json file to inspect.

    Raises:
        AssertionError: Any of the above checks fails; the message names the
            specific mismatch.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    actual_by_class = manifest["counts"]["by_class"]
    if actual_by_class != expectation.defect_counts:
        raise AssertionError(
            f"defect_counts mismatch: expected {expectation.defect_counts},"
            f" got {actual_by_class}"
        )

    actual_impact_union = {
        code
        for defect in manifest["defects"]
        for code in defect["impact"]
        if code != "beyond-c1-c12"
    }
    expected_impact_union = set(expectation.impact_union)
    if actual_impact_union != expected_impact_union:
        raise AssertionError(
            f"impact_union mismatch: expected {sorted(expected_impact_union)},"
            f" got {sorted(actual_impact_union)}"
        )

    for defect_idx, predicate in enumerate(expectation.contains_defects):
        if not any(_defect_matches(d, predicate) for d in manifest["defects"]):
            raise AssertionError(
                f"contains_defects[{defect_idx}] matched no manifest defect:"
                f" {predicate}"
            )
