"""Point-in-time playback answers: PlaybackSnapshot, PlaybackPosition.

`build_snapshot` composes the state-at fold (record populations) and the
membership-state-at fold (membership populations) at horizon `at_sim_time +
1`, restricted to the head's selection, and stamps each table with the
sub_type / owner_sub_type discriminator plus `_ts` wallclock siblings when
the head's anchor resolves. `PlaybackPosition` composes a snapshot with the
tail of the event stream — the consistency algebra's two halves.

Layer-direction invariant: imports the reader, the derivations state-at
folds, `fabulexa_forge.anchor`, `fabulexa_forge.playback.*`, and stdlib.
Never imports exporters.* or config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterator

from fabulexa_forge.derivations.membership_state_at import (
    MEMBERSHIP_STATE_AT_COLUMNS,
    build_membership_state_at_sql,
)
from fabulexa_forge.derivations.state_at import STATE_AT_COLUMNS, build_state_at_sql
from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.events import PlaybackEvent, iter_playback_events
from fabulexa_forge.playback.stamp import spine_discriminator_index

if TYPE_CHECKING:
    import pyarrow

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.playback.selection import (
        ResolvedMembershipSelection,
        ResolvedRecordSelection,
        ResolvedSelection,
    )
    from fabulexa_forge.reader.emit import Emit

#: The raw-ns lifecycle columns on a record snapshot table that gain a
#: '_ts' sibling when the head's anchor resolves, in that sibling order.
_RECORD_TS_COLUMNS: tuple[str, ...] = (STATE_AT_COLUMNS[1], STATE_AT_COLUMNS[3])

#: The raw-ns lifecycle column on a membership snapshot table that gains a
#: '_ts' sibling when the head's anchor resolves.
_MEMBERSHIP_TS_COLUMNS: tuple[str, ...] = (MEMBERSHIP_STATE_AT_COLUMNS[1],)


def _find_resolved_record(
    resolved: "ResolvedSelection", kind: str
) -> "ResolvedRecordSelection":
    """Locate kind's ResolvedRecordSelection, or raise PlaybackError."""
    for record_sel in resolved.records:
        if record_sel.kind == kind:
            return record_sel
    raise PlaybackError(f"kind {kind!r} is not in the head's selection")


def _find_resolved_membership(
    resolved: "ResolvedSelection", owner_kind: str, property_name: str
) -> "ResolvedMembershipSelection":
    """Locate the (owner_kind, property_name) ResolvedMembershipSelection, or
    raise PlaybackError."""
    for membership_sel in resolved.memberships:
        if (
            membership_sel.owner_kind == owner_kind
            and membership_sel.property_name == property_name
        ):
            return membership_sel
    raise PlaybackError(
        f"membership {owner_kind!r}.{property_name!r} is not in the head's selection"
    )


def _append_stamp_column(
    table: "pyarrow.Table",
    column_name: str,
    discriminator_index: dict[str, str | None],
) -> "pyarrow.Table":
    """Append a verbatim sub_type / owner_sub_type stamp column, keyed by
    each row's record_id.

    Args:
        table: The state-at fold's materialized table (carries record_id).
        column_name: 'sub_type' or 'owner_sub_type'.
        discriminator_index: record_id -> verbatim discriminator value, from
            spine_discriminator_index (empty when not subtyped / undeclared,
            so every row stamps None uniformly).

    Returns:
        table with column_name appended.
    """
    import pyarrow as pa

    record_ids = table.column("record_id").to_pylist()
    stamp_values = [discriminator_index.get(str(rid)) for rid in record_ids]
    return table.append_column(column_name, pa.array(stamp_values, type=pa.string()))


def _append_ts_siblings(
    table: "pyarrow.Table",
    anchor: "EffectiveAnchor | None",
    raw_columns: tuple[str, ...],
) -> "pyarrow.Table":
    """Append a '<name>_ts' sibling per raw-ns lifecycle column, in order.

    A no-op when the head carries no anchor — no sibling columns exist.

    Args:
        table: The table carrying each raw-ns column in raw_columns.
        anchor: The resolved effective anchor, or None.
        raw_columns: The raw-ns lifecycle column names, in sibling order.

    Returns:
        table with one '<name>_ts' column appended per raw_columns entry,
        unchanged when anchor is None.
    """
    if anchor is None:
        return table

    import pyarrow as pa

    from fabulexa_forge.anchor import render_ts

    for name in raw_columns:
        raw_values = table.column(name).to_pylist()
        ts_values = [
            None if value is None else render_ts(value, anchor) for value in raw_values
        ]
        table = table.append_column(f"{name}_ts", pa.array(ts_values, type=pa.string()))
    return table


def _build_record_state_table(
    emit: "Emit",
    fork_path: str,
    resolved_record: "ResolvedRecordSelection",
    anchor: "EffectiveAnchor | None",
    at_sim_time: int,
) -> "pyarrow.Table":
    """Materialize one kind's record-state snapshot table at horizon T + 1.

    Args:
        emit: The open emit.
        fork_path: The sole branch's fork_path.
        resolved_record: The resolved RecordAtomSelection for this kind.
        anchor: The resolved effective anchor, or None for raw-ns siblings.
        at_sim_time: The inclusive snapshot position T.

    Returns:
        The composed state-at fold's canonical relation, plus sub_type, plus
        _ts siblings when anchor resolves.
    """
    sidecar = emit.sidecar
    kind = resolved_record.kind
    horizon_ns = at_sim_time + 1

    sql = build_state_at_sql(
        sidecar, fork_path, kind, frozenset(resolved_record.properties), horizon_ns
    )
    table = emit.query_arrow(sql, ())

    is_subtyped = bool(sidecar.subtype_values(kind))
    discriminator_index = spine_discriminator_index(
        emit, fork_path, kind, is_subtyped, resolved_record.discriminator_declared
    )
    table = _append_stamp_column(table, "sub_type", discriminator_index)
    table = _append_ts_siblings(table, anchor, _RECORD_TS_COLUMNS)
    return table


def _build_membership_state_table(
    emit: "Emit",
    fork_path: str,
    resolved_membership: "ResolvedMembershipSelection",
    anchor: "EffectiveAnchor | None",
    at_sim_time: int,
) -> "pyarrow.Table":
    """Materialize one membership table's containment snapshot at horizon T + 1.

    Args:
        emit: The open emit.
        fork_path: The sole branch's fork_path.
        resolved_membership: The resolved MembershipAtomSelection.
        anchor: The resolved effective anchor, or None for raw-ns siblings.
        at_sim_time: The inclusive snapshot position T.

    Returns:
        The composed membership-state-at fold's canonical relation, plus
        owner_sub_type, plus joined_sim_time_ts when anchor resolves.
    """
    sidecar = emit.sidecar
    owner_kind = resolved_membership.owner_kind
    horizon_ns = at_sim_time + 1

    sql = build_membership_state_at_sql(
        sidecar,
        fork_path,
        owner_kind,
        resolved_membership.property_name,
        resolved_membership.fields,
        horizon_ns,
    )
    table = emit.query_arrow(sql, ())

    is_owner_subtyped = bool(sidecar.subtype_values(owner_kind))
    discriminator_index = spine_discriminator_index(
        emit,
        fork_path,
        owner_kind,
        is_owner_subtyped,
        resolved_membership.owner_discriminator_declared,
    )
    table = _append_stamp_column(table, "owner_sub_type", discriminator_index)
    table = _append_ts_siblings(table, anchor, _MEMBERSHIP_TS_COLUMNS)
    return table


class PlaybackSnapshot:
    """Lazy point-in-time state at one inclusive position.

    at_sim_time: the inclusive position T this snapshot reflects.
    """

    at_sim_time: int

    def __init__(
        self,
        emit: "Emit",
        resolved: "ResolvedSelection",
        anchor: "EffectiveAnchor | None",
        fork_path: str,
        at_sim_time: int,
    ) -> None:
        self.at_sim_time = at_sim_time
        self._emit = emit
        self._resolved = resolved
        self._anchor = anchor
        self._fork_path = fork_path
        self._record_tables: dict[str, "pyarrow.Table"] = {}
        self._membership_tables: dict[tuple[str, str], "pyarrow.Table"] = {}

    def record_state(self, kind: str) -> "pyarrow.Table":
        """The kind's state table at T.

        Columns: STATE_AT_COLUMNS (record_id; created_sim_time; active;
        deactivated_at), the fold's own presentation_id column when the kind
        carries one, a sub_type stamp (the spine
        discriminator verbatim, undeclared values included; NULL when the
        kind is not sub-typed, the cell is NULL, or the discriminator
        column is undeclared), one prop__<p> per
        selected property, and — when the head's anchor resolves — a
        <name>_ts sibling per raw-ns lifecycle column. Typed at zero rows.
        Column order is contract (§ Snapshot): the fold's canonical
        relation verbatim — properties in sidecar declaration order — then
        sub_type, then the _ts siblings in raw-column order.

        Args:
            kind: A kind named by the head's selection.

        Returns:
            The materialized table; identical on repeated calls.

        Raises:
            PlaybackError: kind is not in the head's selection.
        """
        if kind not in self._record_tables:
            resolved_record = _find_resolved_record(self._resolved, kind)
            self._record_tables[kind] = _build_record_state_table(
                self._emit,
                self._fork_path,
                resolved_record,
                self._anchor,
                self.at_sim_time,
            )
        return self._record_tables[kind]

    def membership_state(
        self,
        owner_kind: str,
        property_name: str,
    ) -> "pyarrow.Table":
        """The membership table's containment rows at T.

        Columns: MEMBERSHIP_STATE_AT_COLUMNS (record_id — the owner;
        joined_sim_time; each selected field's column shape — scalar
        elem__<f> or the reference member__<f>__kind / member__<f>__id
        pair), an owner_sub_type
        stamp (verbatim; NULL when the owner kind is not sub-typed, the owner
        row is an orphan, its discriminator cell is NULL, or the
        discriminator column is undeclared), and — when the
        anchor resolves — joined_sim_time_ts. left_sim_time is never present.
        Typed at zero rows. Column order is contract (§ Snapshot): the
        fold's canonical relation verbatim — fields in sidecar
        element-schema order — then owner_sub_type, then joined_sim_time_ts.

        Args:
            owner_kind: The owner kind of a selected membership table.
            property_name: Its collection property.

        Returns:
            The materialized table; identical on repeated calls.

        Raises:
            PlaybackError: (owner_kind, property_name) is not in the head's
                selection.
        """
        key = (owner_kind, property_name)
        if key not in self._membership_tables:
            resolved_membership = _find_resolved_membership(
                self._resolved, owner_kind, property_name
            )
            self._membership_tables[key] = _build_membership_state_table(
                self._emit,
                self._fork_path,
                resolved_membership,
                self._anchor,
                self.at_sim_time,
            )
        return self._membership_tables[key]


class PlaybackPosition:
    """A seek result: state as of T plus the stream strictly after T.

    at_sim_time: the inclusive position T.
    """

    at_sim_time: int

    def __init__(
        self,
        emit: "Emit",
        resolved: "ResolvedSelection",
        anchor: "EffectiveAnchor | None",
        fork_path: str,
        at_sim_time: int,
    ) -> None:
        self.at_sim_time = at_sim_time
        self._emit = emit
        self._resolved = resolved
        self._anchor = anchor
        self._fork_path = fork_path
        self._snapshot = PlaybackSnapshot(
            emit, resolved, anchor, fork_path, at_sim_time
        )

    def snapshot(self) -> PlaybackSnapshot:
        """The state as of T; equal to Playback.snapshot(at_sim_time).

        Returns:
            The lazy snapshot.
        """
        return self._snapshot

    def events(self) -> Iterator[PlaybackEvent]:
        """The stream strictly after T; equal to
        Playback.events(at_sim_time + 1, None).

        Returns:
            A lazy iterator with entry-point-invariant seq.
        """
        return iter_playback_events(
            self._emit,
            self._resolved,
            self._anchor,
            self._fork_path,
            self.at_sim_time + 1,
            None,
        )
