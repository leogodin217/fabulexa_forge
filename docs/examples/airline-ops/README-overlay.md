## overview

A two-base airline's quarter (2026-03-02 to 2026-05-31): 5,759 out-and-back
legs fly from the east and west stations, each assembling a tail, a captain,
and a first officer from its base's pools. Crew duty days, sickness, paydays,
hires and resignations drive crew supply; faults ground tails on the turn; a
spring flu wave lifts the sickness draw across both bases. **No column stamps
delay minutes.** Pushback delay, duty overrun and cancellation pressure all
have to be *derived* from the wait and assignment intervals, which is this
warehouse's point.

The headline derivations:

- **Pushback delay per leg** — the leg's time queued for a tail and for its
  two seats:

  ```sql
  select leg_code, sum(epoch(granted_at - queued_from)) / 60.0 as delay_min
  from fact_leg_assembly_wait
  where granted_at is not null
  group by leg_code
  ```

  or, equivalently, the leg's `dispatching` + `crewing_captain` +
  `crewing_fo` occupancy in `fact_lifecycle_state` (`process = 'flight'`) net
  of the grant instants.

- **Tail utilisation** — `fact_tail_assignment` spans (block + turnaround +
  repair) summed per `aircraft_key` against the quarter's span.

- **Crew supply vs. the flu wave** — `fact_crew_presence` (`presence =
  'sick'`) counted by week against `fact_leg_assembly_wait` rows with
  `waiting_for in ('captain', 'first_officer', 'reserve_captain',
  'reserve_fo')`: sickness stands crew members down, and the seat-wait queues
  lengthen.

- **Duty overrun** — `fact_duty_release_wait` spans: a crew member whose duty
  day ended while their leg was still airborne queues to stand down.

Things to know before you query:

- **All timestamps are the ops clock.** Every timestamp is local time on the
  carrier's operations clock (America/Phoenix — no DST, so the daily cadence
  holds all quarter), recorded at whole-minute resolution. Two events inside
  the same minute show the same timestamp; sequence within a minute is not
  recoverable from the timestamps alone.
- **Reason codes are a catalogue, not yet a fact column.** `dim_delay_reason`
  (the delay-code list, IATA numbering) and `dim_cancellation_reason` (DOT
  letter categories) are the carrier's reference catalogues, maintained by
  hand outside the operational system. No fact column carries a code this
  quarter — the events don't record one — so they stand as lookups awaiting
  coded events, as they would in a warehouse whose upstream hasn't started
  coding yet.
- **Leg code vs. flight number.** `leg_code` (LEG-nnnnn) identifies ONE flown
  leg and is the key every fact joins `dim_scheduled_leg` on. `flight_number`
  (FXnnn) is the published service — one number per (departure bank,
  destination) pair, reused day after day — so grouping a quarter by
  `flight_number` aggregates a service, not a leg.
- **One timeline table, four processes.** `fact_lifecycle_state` carries the
  flight, roster, sickness and employment timelines in one table; always
  filter on `process` first. `leg_code` is set on flight rows, `crew_code` on
  the other three — exactly one of the two per row, and
  (`leg_code`/`crew_code`, `process`, `entered_at`, `state`) identifies a row
  — at minute resolution a leg can pass through two states inside one
  displayed minute, so the state is part of the key.
- **Crew are named individuals, rated for one seat.** Each crew member
  (CRW staff code, with `first_name` / `last_name` / `full_name` on the dim)
  is a captain or a first officer (`seat`), and a leg fills both seats.
- **The crew fact is per seat.** `fact_leg_crew` has one row per seat hold,
  so a fully crewed leg has two rows (plus an extra row for a seat lost
  mid-block and recovered by reserve callout). Group on `leg_code` for
  leg-level questions, filter on `seat` for seat-level ones.
- **The SCD-2 dim is small but real.** `dim_crew_member` versions on
  employment status *and* the episode counter, so joining a leg's crew at
  block time takes the usual `[valid_from, valid_to)` as-of join, not the
  current version.
- **Haul class lives on the airport.** The destination is drawn when the leg
  enters the schedule and never changes; `dim_scheduled_leg.airport_code`
  joins `dim_airport`, and a leg's `route_class` is its destination's.
- **Cancellations release their holds.** A cancelled leg may still appear in
  `fact_tail_assignment` / `fact_leg_crew` (holds it acquired before
  scrubbing) and in the wait facts (queue positions it abandoned). Join to
  `fact_flight_leg.status` to separate flown from scrubbed work.

## table: fact_lifecycle_state

State occupancies, not events: each row is an interval `[entered_at,
exited_at)`, and `exited_at` is NULL only on a subject's last observed state.
A state entered and left again at the same recorded instant never lands a
row, so the flight timeline shows `dispatching` only for legs that actually
waited at the gate (2,261 of 5,759) — a leg granted its tail with no wait
leaves no dispatching interval. The same goes for the two crewing states: a
captain or first officer seated with no wait leaves no `crewing_captain` /
`crewing_fo` interval. Separately, a short real wait can render as
`entered_at = exited_at` at the clock's minute resolution — that is a
sub-minute interval, not a missing one.

## table: fact_leg_crew

One row per seat hold, not per leg: the captain seat and the first-officer
seat each get a row from boarding (or reserve callout) through release at the
end of the block. `assignment_type = 'reserve_callout'` marks a seat refilled
mid-block after a sick pull. A hold whose `released_at` is NULL was still
aboard at the end of the quarter's data.

## table: fact_leg_assembly_wait

The pushback-delay fact. One leg can wait more than once (a tail wait, then a
captain wait, then a first-officer wait, then — if a seat was lost mid-block —
a reserve-callout wait), so sum per `leg_code`. A wait row whose `granted_at`
is NULL was still queued at the end of the quarter's data; a cancelled leg's
wait ends when the leg scrubs, not with a grant.

## table: dim_crew_member

Versions window on the member's whole HR history: a sickness episode
increments `sickness_episodes_to_date` and cuts a version even when
`employment_status` is unchanged. Counting rows is not counting people —
there are 66 members (64 on the payroll at the quarter's start, two hired
during it); a member's attribute at an event instant is the as-of join on
`[valid_from, valid_to)`.
