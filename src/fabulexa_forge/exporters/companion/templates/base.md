## Reading this export

This is a **base** export: a flat, unopinionated projection of the base
layer, one table per records-category kind, with no star-schema shaping and
no declared-table grammar. Every table below traces to the base-layer emit
this export was built from.

**State-at-horizon.** Every non-key value is reconstructed **as of one
instant** — the export's horizon (the tape's end, an explicit `slice_at`, or
an incremental window's end). A tracked property carries its most-recent
value at-or-before the horizon; a constant property carries its current
value (properties that never change have no "as of" to reconstruct); a
record created at-or-after the horizon is simply absent from the table,
never present with nulls.

**Record-index key columns.** Every table carries exactly one **self key** —
the record's own dense `record_index`, stable across every horizon and every
export of the same branch — plus one **edge key** per surviving reference
property, named `<property>_key`: the referenced record's `record_index` as
of the same horizon. Each reference property also keeps its plain id-space
column (`<property>`) alongside its edge key — the two are not redundant: the
id column always carries the referenced identity when the property is set,
while the edge key is `NULL` whenever the reference cannot be resolved at
this horizon (the target record did not yet exist, or does not exist at
all). Join two base tables on `<kind>_key = <other_kind>.record_index`-style
key pairs to reconstruct relationships as of the export's horizon.
