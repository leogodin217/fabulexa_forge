## Reading this export

This is a **source** export: a reshaping of the base layer into the messy,
operational shape an application's own database would carry — current-state
tables plus, optionally, an audit trail — rather than a clean OLAP star.
Every table below traces to the base-layer emit this export was built from.

**State tables** carry one row per record, its current values, and a
soft-delete lifecycle pair (`active` / `deactivated_at`) in place of hard
deletes — a record that was removed stays present with `active = false`.

**Junction tables** carry one row per membership interval — an association
between two entities that starts and (optionally) ends. `joined_at` /
`left_at` bound the interval; `left_at` is `NULL` while the membership is
still open. Query a junction table like an application's own many-to-many
link table: join it to its owning and member state tables on their id
columns.

**The event log**, when declared, is one polymorphic audit table covering
every audited table's history: one row per create/update/destroy event,
ordered by `id` (a dense, monotonic position in the log's total order — never
a value-based rank). Its `changes` column is a JSON object mapping each
changed property's output name to an `[old, new]` value pair; a `create`
event's changes carry `[null, value]` for every audited property, a
`destroy` event's carry `[value, null]`. Replaying the log's rows in `id`
order reconstructs every audited table's history from the log alone — the
same mechanism a real application's audit trail would give a downstream
consumer.
