"""Derivations layer — interpretive shared folds over the reader.

Every derivation is a function of the sidecar plus plain parameters, returning
one complete SELECT over base tables. No I/O, no connection handling; the mode
materializes it through the reader's query surfaces.

Layer-direction invariant: this package imports only the reader,
fabulexa_forge.errors, and stdlib. Modes import derivations; derivations never
import exporters.* or config.

Public surface:
    require_single_branch     — stage-wide single-branch guard; returns fork_path
    VERSIONED_INTERVAL_COLUMNS — canonical versioned-interval column names
    build_versioned_intervals_sql — versioned-intervals SQL builder
    REFERENCE_RESOLUTION_COLUMNS — canonical reference-resolution column names
    build_reference_path_sql  — reference-path derivation SQL builder
    build_membership_edge_sql — membership-edge derivation SQL builder
    ROW_STATE_EVENT_COLUMNS   — canonical row-state-events column prefix
    EVENT_CLASS_CREATE        — event_class value for create events (0)
    EVENT_CLASS_UPDATE        — event_class value for update events (1)
    EVENT_CLASS_DELETE        — event_class value for delete events (2)
    build_row_state_events_sql — row-state-events SQL builder
    MEMBERSHIP_EVENT_COLUMNS  — canonical membership-events column prefix
    EVENT_CLASS_JOIN          — event_class value for join events (0)
    EVENT_CLASS_LEAVE         — event_class value for leave events (1)
    resolve_membership_columns — membership after-image column order resolver
    build_membership_events_sql — membership join/leave event stream SQL builder
"""

from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.derivations.membership_events import (
    EVENT_CLASS_JOIN,
    EVENT_CLASS_LEAVE,
    MEMBERSHIP_EVENT_COLUMNS,
    build_membership_events_sql,
    resolve_membership_columns,
)
from fabulexa_forge.derivations.reference_resolution import (
    REFERENCE_RESOLUTION_COLUMNS,
    build_membership_edge_sql,
    build_reference_path_sql,
)
from fabulexa_forge.derivations.row_state_events import (
    EVENT_CLASS_CREATE,
    EVENT_CLASS_DELETE,
    EVENT_CLASS_UPDATE,
    ROW_STATE_EVENT_COLUMNS,
    build_row_state_events_sql,
)
from fabulexa_forge.derivations.versioned_intervals import (
    VERSIONED_INTERVAL_COLUMNS,
    build_versioned_intervals_sql,
)

__all__ = [
    "require_single_branch",
    "VERSIONED_INTERVAL_COLUMNS",
    "build_versioned_intervals_sql",
    "REFERENCE_RESOLUTION_COLUMNS",
    "build_reference_path_sql",
    "build_membership_edge_sql",
    "ROW_STATE_EVENT_COLUMNS",
    "EVENT_CLASS_CREATE",
    "EVENT_CLASS_UPDATE",
    "EVENT_CLASS_DELETE",
    "build_row_state_events_sql",
    "MEMBERSHIP_EVENT_COLUMNS",
    "EVENT_CLASS_JOIN",
    "EVENT_CLASS_LEAVE",
    "resolve_membership_columns",
    "build_membership_events_sql",
]
