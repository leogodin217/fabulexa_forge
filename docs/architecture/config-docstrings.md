# Config field-documentation convention

How the export-config Pydantic models (`src/fabulexa_export/config/models.py`) carry
their documentation. A **developer** convention — the audience for every channel below
is a developer reading the code, never the YAML author. Authors learn the config from
**recipes** (worked example configs); the models are the developer reference.

Design record: note `recipe-convention-for-export-config-authoring` (vault, area
`export`). This doc is the canonical, enforceable statement; the note is the rationale
trail.

> **Scope.** Applies to author-facing config models — today the classes in
> `config/models.py`. A future mode's config (e.g. streaming) adopts the same
> convention and is added to the enforcement test's module list.

---

## The three channels

Documentation splits into three channels with no overlap. Every documented fact lives
in exactly one.

| Channel | Physical location | Owns |
|---|---|---|
| **Class docstring** | `"""…"""` directly under `class X:` | one line — *what this config block represents* |
| **Attribute docstring** | a string literal on the line(s) **immediately after** the field assignment, same indent | per-field *meaning / intent* |
| **Validator docstring** | `"""…"""` under the `@model_validator` method | the *cross-field rule* it enforces — single source of truth |

`Field(description=...)` is **not** a channel: a description flows into the
author-facing JSON schema, and field docs are developer-only (see Rationale).
`# comments` are **not** a channel for field docs — the doc must be a string literal so
the `use_attribute_docstrings` flag could promote it verbatim later at zero cost.

---

## Attribute docstrings

```python
to: str
"""The kind whose dimension row this FK resolves to."""
```

- A string literal on the line directly under the field, **before** the next field.
- Descriptive mood, present tense — a noun phrase or one declarative sentence. It must
  read as a standalone field description (it may one day *become*
  `Field(description=...)` if the `use_attribute_docstrings` flag is flipped), so no
  "see above" / "as in the class docstring".
- **One sentence by default, two maximum.** If it needs more, the explanation belongs
  in a recipe, or the fact is a rule that belongs in a validator.

**Allowed**

- The field's role/intent — what it selects, names, or controls in the reshape.
- The base-layer concept it maps to (kind, grain, `sim_time`, membership, reference
  hop) — contract vocabulary, used exactly.
- What a non-obvious enum member or value *means* when the name alone doesn't carry it.
- Naming sibling fields to explain *this* field's role — but never the cross-field
  *rule* (that is the validator's).

**Not allowed**

- **Restating the type.** `to: str` never gets "A string naming…"; the annotation
  already says it.
- **Restating a constraint the code encodes** — required-with-X, exactly-one,
  non-empty, allowed-only-on-grain-Y, or the default value. The docstring states what
  the field *means*; the validator (or annotation) owns the *rule*. Canonical split:
  `as_of`'s docstring says what `as_of` is; "`as_of` requires `member_path`" stays in
  `membership_fk_shape`.
- Sample YAML or examples — those are recipes and tests.
- Invented behavior not in code — undocumented is honest (Principle #7).

**The self-evident test → omit the docstring entirely**

> Given the class one-liner + the field's name + its type, could a competent reader
> write correct YAML, with nothing non-obvious left to say?

If yes, write no docstring. We never invent prose to hit coverage. (`name: str` on
`TableDecl`/`ColumnDecl` is self-evident; `to: str` on `FkClause` is not — "which kind,
the target dimension" is real information.)

**Naming a field in prose** — refer to a field by its **author-facing YAML key**, i.e.
the alias when one exists (`from`, not `from_`; `map`, `where`). The docstring
describes the config contract surface, which is what the author writes.

---

## Class docstrings

One line: what the block represents to a developer. **Not** author "when to use"
guidance — that is the deferred `author_doc` model-level channel, not this one. If a
class docstring today carries per-field prose, relocate it to attribute docstrings; if
it carries a cross-field rule, that is the validator's (usually already there — dedup,
do not duplicate).

## Validator docstrings

State the cross-field invariant in plain terms; this is the single source of truth for
that rule. Most validators already carry one — consolidate any rule that leaked into a
class docstring here, but do not rewrite working validator prose gratuitously.

---

## Templates

```python
class <Name>(StrictBaseModel):
    """<One line: what this config block is.>"""

    <field>: <type>
    """<Field's role/meaning — semantics, not type, not rule. Omit if self-evident.>"""

    @model_validator(mode="after")
    def <rule_name>(self) -> Self:
        """<The cross-field invariant this enforces — the single source of truth.>"""
        ...
```

## Worked example — `FkClause`

The end-state after the refactor: the class docstring drops to one line; per-field
prose moves down; the rule stays in the validator.

```python
class FkClause(StrictBaseModel):
    """A dimension foreign key resolved by a labeled-edge pathfind."""

    to: str
    """The kind whose dimension row this FK resolves to."""
    via: Literal["reference", "membership"]
    """Which edge to pathfind along — a declared reference, or a membership interval."""
    ...
    target_key: Literal["record_id", "presentation_id"] = "record_id"
    """Which identity to write into the fact FK — the natural record_id, or the warehouse surrogate presentation_id."""
    as_of: str | None = None
    """For a point-in-time membership FK, the grain column holding the firing time T at which membership is resolved."""
    member_path: list[str] | None = None
    """The reference-hop chain from the grain kind to the member identity, resolved as of T."""
```

What did **not** become a docstring: "`member_path` requires `as_of`", "valid only with
`via='membership'`" — those are rules and stay in `membership_fk_shape`.

---

## Enforcement

`tests/config/test_docstring_convention.py` locks the **structural** invariants. It
reads attribute docstrings by parsing the model source with `ast` — because
`use_attribute_docstrings` is deliberately **off**, the docstrings do not appear in
`model_fields[...].description`, so the source AST is the only place to read them.

The test asserts, over every `BaseModel` subclass defined in `config/models.py`:

1. **No author-facing descriptions leak.** Every field's `model_fields[name].description
   is None` — catches both a stray `Field(description=...)` and an accidental
   `use_attribute_docstrings` flip in one check.
2. **The flag stays off.** Every model's `model_config.get("use_attribute_docstrings")
   is not True` — documents the intent with a clear failure message.
3. **Class docstrings are one line.** The class docstring, stripped, contains no
   newline.
4. **Attribute docstrings stay short.** Each attribute docstring (string literal
   immediately following an annotated field) is ≤ `ATTR_DOCSTRING_MAX_CHARS` (≈ two
   sentences) — a tripwire against paragraph-length field prose.

What the test **does not** assert — these are review-time judgments, not mechanically
decidable, and pretending otherwise would produce false confidence:

- **Presence** of an attribute docstring (self-evident fields legitimately omit it).
- Prose quality: semantics-not-type, descriptive-not-imperative, YAML-key naming.

Those belong on the review checklist when the docstrings land, not in the test.

---

## Rationale / what we ruled out

- **Field docs are developer-only; no field-level author docs.** Authors learn fields
  through recipes; duplicating field prose for two audiences is the drift the
  three-channel split exists to prevent.
- **Attribute docstrings, not `Field(description=...)`.** Description prose would flow
  into an author-facing schema, crossing the developer/author line. Attribute
  docstrings keep the prose developer-side while staying one flag-flip away from
  becoming descriptions *if* an author-facing validation schema is ever shipped.
- **`use_attribute_docstrings` stays off.** Pydantic ≥ 2.7 (we run 2.13.4) could
  promote these docstrings to schema descriptions today; we don't, for the same
  developer/author-line reason. The format is kept flip-ready at zero cost.
- **Locality over a central field reference.** Per-field prose lives on the field, so a
  rename faces its own doc and the doc cannot silently drift from the field — unlike a
  prose block in the class docstring or a separate reference file.

## Related

| Document | Why |
|---|---|
| note `recipe-convention-for-export-config-authoring` | Design record + the broader two-pillar authoring strategy (recipes + model docs) |
| [`dimensional.md`](dimensional.md) | The config grammar these models express |
| [`../PROCESS.md`](../PROCESS.md) | Code-is-truth, the duplicate-schema-in-prose anti-pattern |
