## overview

A two-base airline's quarter (2026-03-02 to 2026-05-31) with its run-up:
2,601 out-and-back legs fly from the CVG and SLC bases, each assembling a
tail, a captain, and a first officer from its base's pools. Crew duty days,
rostered days off, sickness, paydays, hires and resignations drive crew
supply; faults ground tails on the turn; a spring flu wave lifts the
sickness draw across both bases. The data opens 2026-01-05 — eight weeks of
ordinary operating history before the quarter — so nothing starts cold at
the quarter boundary: members already carry sickness records, paydays, and
tenure.

The schedule stamps each leg's **planned departure**
(`dim_scheduled_leg.scheduled_departure_at`), but **no column stamps delay
minutes**: pushback delay, duty overrun and cancellation pressure all have
to be *derived* against the plan from the state timeline and the wait and
assignment intervals, which is this warehouse's point.

The headline derivations:

- **Pushback delay per leg** — wheels-up against the plan:

  ```sql
  select l.leg_code,
         epoch(f.entered_at - l.scheduled_departure_at) / 60.0 as delay_min
  from dim_scheduled_leg l
  join fact_lifecycle_state f
    on f.leg_code = l.leg_code
   and f.process = 'flight' and f.state = 'enroute_out'
  ```

  Most of the mass is the `boarding` interval (pushback and taxi); the tail
  of the distribution is the crew-shortage queue time in
  `fact_leg_assembly_wait`.

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

- **All timestamps are the ops clock.** Every timestamp is zone-aware
  (TIMESTAMP WITH TIME ZONE) on the carrier's operations clock
  (America/New_York), so each value carries its real UTC offset: -05:00
  (EST) until the DST transition on 2026-03-08, -04:00 (EDT) after it. Date
  keys are the ops clock's calendar day; derive a station-local date in SQL
  where you need one. Timestamps are recorded at whole-minute resolution:
  two events inside the same minute show the same timestamp, and sequence
  within a minute is not recoverable from the timestamps alone.
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
  — at minute resolution a subject can pass through two states inside one
  displayed minute, so the state is part of the key.
- **Paydays are events, not states.** The pay packet lands instantaneously,
  so it never shows up as an interval in `fact_lifecycle_state`; the pay
  cadence is `fact_payday` (one row per payslip — a monthly payday, each
  member's packet landing at its own minute through the morning).
- **Haul class is a fact of the city pair.** `dim_airport` carries
  `route_class_from_cvg` and `route_class_from_slc` (and a demand weight per
  base): SEA is long-haul from CVG but medium from SLC. A leg's class is its
  destination's class *from the leg's own `home_base`* — join
  `dim_scheduled_leg.airport_code` to `dim_airport` and pick the column by
  `home_base`.
- **Crew are named individuals, rated for one seat.** Each crew member
  (CRW staff code, with `first_name` / `last_name` / `full_name` on the dim)
  is a captain or a first officer (`seat`), musters early or late
  (`report_shift`), and a leg fills both seats.
- **The crew fact is per seat.** `fact_leg_crew` has one row per seat hold,
  so a fully crewed leg has two rows (plus an extra row for a seat lost
  mid-block and recovered by reserve callout). Group on `leg_code` for
  leg-level questions, filter on `seat` for seat-level ones.
- **The SCD-2 dim is small but real.** `dim_crew_member` versions on
  employment status *and* the episode counter, so joining a leg's crew at
  block time takes the usual `[valid_from, valid_to)` as-of join, not the
  current version.
- **Cancellations release their holds.** A cancelled leg (133 this tape,
  5.1%) may still appear in `fact_tail_assignment` / `fact_leg_crew` (holds
  it acquired before scrubbing) and in the wait facts (queue positions it
  abandoned). Join to `fact_flight_leg.status` to separate flown from
  scrubbed work.

## table: fact_lifecycle_state

State occupancies, not events: each row is an interval `[entered_at,
exited_at)`, and `exited_at` is NULL only on a subject's last observed state.
A state entered and left again at the same recorded instant never lands a
row, so the flight timeline shows `dispatching` only for legs that actually
waited at the gate (10 of 2,601 — tails are rarely the bottleneck) and
`crewing_captain` / `crewing_fo` only for legs that waited for a seat (408
and 78): a leg granted its resources without waiting passes straight to
`boarding`, the pushback-and-taxi interval every flown leg has. The same
collapse is why the instantaneous `payday` never appears here — see
`fact_payday`. Separately, a short real wait can render as `entered_at =
exited_at` at the clock's minute resolution — that is a sub-minute interval,
not a missing one.

## table: fact_payday

One row per payslip issued (408 this tape): the member's pay packet landing,
at its instant — one payday per month, each member's packet jittered to its
own minute through the morning rather than a single synchronized stamp. This
is the only payday surface in the star: the
payday state is instantaneous, so the lifecycle timeline cannot carry it.

## table: fact_leg_crew

One row per seat hold, not per leg: the captain seat and the first-officer
seat each get a row from boarding (or reserve callout) through release at the
end of the block. `assignment_type = 'reserve_callout'` marks a seat refilled
mid-block after a sick pull. A hold whose `released_at` is NULL was still
aboard at the end of the quarter's data.

## table: fact_leg_assembly_wait

The scarce-resource component of pushback delay. One leg can wait more than
once (a tail wait, then a captain wait, then a first-officer wait, then — if
a seat was lost mid-block — a reserve-callout wait), so sum per `leg_code`;
most legs assemble without queuing at all (499 waits across 2,601 legs), so
the everyday delay mass is the `boarding` interval in
`fact_lifecycle_state`, not this table. A wait row whose `granted_at` is
NULL was still queued at the end of the quarter's data; a cancelled leg's
wait ends when the leg scrubs, not with a grant.

## table: dim_crew_member

Versions window on the member's whole HR history: a sickness episode
increments `sickness_episodes_to_date` and cuts a version even when
`employment_status` is unchanged. Counting rows is not counting people —
there are 93 members (80 on the payroll when the data opens, 13 hired during
it); a member's attribute at an event instant is the as-of join on
`[valid_from, valid_to)`.
