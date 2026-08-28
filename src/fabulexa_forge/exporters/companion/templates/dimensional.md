## Reading this export

This is a **dimensional** (star-schema) export: one or more fact tables at a
declared grain, surrounded by dimension tables the facts key into. Every
dimension and fact table below traces to the base-layer emit this export was
built from — nothing here is fabricated.

**Dimensions** carry one row per entity (a `record_id`-keyed identity, unless
key election renders a different surface) plus its projected attributes.
A dimension declared `scd: type2` instead carries **one row per version**:
its history-tracked columns change value across versions, and two columns —
`valid_from` / `valid_to` — bound the sim-time interval each row is current
for. `valid_to` is `NULL` on a version's current (most recent) row; joining a
fact's timestamp against `[valid_from, valid_to)` reconstructs the dimension's
state as of that fact, the standard SCD-2 point-in-time join. A `scd: type1`
dimension carries the current value only, no version rows, no validity
columns.

**Fact tables** carry one row per event at the table's declared grain, with
foreign keys into the surrounding dimensions and any declared measure
columns. A fact's foreign key into an SCD-2 dimension is the dimension's
surrogate key as of the fact's own timestamp — resolved once at build time,
not re-derived by the consumer.

To query this warehouse: join facts to dimensions on the declared key
columns; for an SCD-2 dimension, either join on its current-row surrogate key
(if you only need current attributes) or perform the validity-interval join
above (if you need attribute-as-of-the-fact).
