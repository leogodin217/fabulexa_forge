## overview

A two-base airline's quarter (2026-03-02 to 2026-05-31) with its run-up:
2,737 out-and-back legs publish from the CVG and SLC bases, each drawing a
5-minute-grid departure slot in its bank and assembling a tail, a captain,
and a first officer from its base's pools. Crew duty days (with the
1-rest-day-in-7 legality counter), rostered days off, sickness, semi-monthly
paydays, hires and resignations drive crew supply; faults ground tails on
the turn; a March flu wave lifts the sickness draw across both bases. The
data opens 2026-01-05 — eight weeks of ordinary operating history before the
quarter — so nothing starts cold at the quarter boundary: members already
carry sickness records, paydays, and tenure.

The schedule stamps each leg's **published departure**
(`dim_scheduled_leg.scheduled_departure_at`) — the drawn slot **plus a fixed
45-minute ops allowance**. Ops release starts at the slot, so a cleanly
assembled leg pushes *ahead* of the published time: **negative pushback
delay is normal data** (61.7% of flown legs push early this quarter), and
the delay distribution has no authored floor. No column stamps delay
minutes: pushback delay, duty overrun and cancellation pressure all have to
be *derived* against the plan from the state timeline and the wait and
assignment intervals, which is this warehouse's point.

The headline derivations:

- **Pushback delay per leg** — wheels-up against the plan (negative =
  early):

  ```sql
  select l.leg_code,
         epoch(f.entered_at - l.scheduled_departure_at) / 60.0 as delay_min
  from dim_scheduled_leg l
  join fact_lifecycle_state f
    on f.leg_code = l.leg_code
   and f.process = 'flight' and f.state = 'enroute_out'
  ```

  Most of the mass sits in the slot-to-push gate work (boarding, taxi); the
  late tail of the distribution is the scarce-resource queue time in
  `fact_leg_assembly_wait`.

- **Arrival vs plan** — scheduled arrival is a join, not a column: the
  published departure plus the destination's outbound block minutes *from
  the leg's own base* (`dim_airport.sched_block_from_cvg` /
  `sched_block_from_slc`), against the `outstation_turn` entry in
  `fact_lifecycle_state`.

- **Tail utilisation** — `fact_tail_assignment` spans (block + turnaround +
  repair) summed per `tail_number` against the quarter's span.

- **Crew supply vs. the flu wave** — `fact_crew_presence` (`presence =
  'sick'`) counted by week against `fact_leg_assembly_wait` rows with
  `waiting_for in ('captain', 'first_officer', 'reserve_captain',
  'reserve_fo')`: sickness stands crew members down, and the seat-wait queues
  lengthen.

- **Duty overrun** — `fact_duty_release_wait` spans: a crew member whose duty
  day ended while their leg was still airborne queues to stand down.

- **Crew legality pressure** — `dim_crew_member.consecutive_duty_days`
  as-of any instant: mustering requires five or fewer, so members at 5 are
  one duty day from a forced rest.

Things to know before you query:

- **All timestamps are the ops clock.** Every timestamp is zone-aware
  (TIMESTAMP WITH TIME ZONE) on the carrier's operations clock
  (America/New_York), so each value carries its real UTC offset: -05:00
  (EST) until the DST transition on 2026-03-08, -04:00 (EDT) after it. Date
  keys are the ops clock's calendar day. For **station-local** time, join
  the base's IANA zone from `dim_operating_base` (CVG is Eastern, SLC is
  America/Denver) and convert in SQL. Timestamps are recorded at
  whole-minute resolution: two events inside the same minute show the same
  timestamp, and sequence within a minute is not recoverable from the
  timestamps alone.
- **Reason codes are now fact-keyed.** A scrubbed leg carries its typed
  cause on `dim_scheduled_leg.cancel_reason` and its DOT category on
  `cancellation_code` → `dim_cancellation_reason` (weather scrubs code B;
  the six crew-supply causes code A). Every assembly wait carries an IATA
  delay code on `fact_leg_assembly_wait.delay_code` → `dim_delay_reason`
  (93 tail rotation, 95 awaiting flight deck crew, 64 crew
  shortage/standby). The unkeyed catalogue rows are categories this
  quarter's events never produced.
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
- **The schedule runs past the quarter.** Legs publish weeks ahead, so 611
  legs are still `published` (and 11 `scheduled`) when the data ends — their
  `scheduled_departure_at` is NULL (the slot draws at the bank) and they
  have no operational rows yet. Filter on `fact_flight_leg.status` when you
  mean flown work.
- **Paydays are events, not states.** The pay packet lands instantaneously,
  so it never shows up as an interval in `fact_lifecycle_state`; the pay
  cadence is `fact_payday` (one row per payslip — a semi-monthly payday,
  each member's packet landing at its own minute through the morning).
- **Haul class and block time are facts of the city pair.** `dim_airport`
  carries `route_class_from_cvg` / `route_class_from_slc`, a demand weight
  per base, and the four published block-minute columns: SEA is long-haul
  from CVG but medium from SLC. A leg's class (and block) is its
  destination's column *from the leg's own `home_base`* — join
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
- **The SCD-2 dim versions on every tracked write — including the duty
  counter.** `dim_crew_member` versions on employment status, the episode
  counter, *and* `consecutive_duty_days`, whose daily writes make it 8,867
  versions over 102 members — read it like the HR system's audit of the
  crew record. Joining a leg's crew at block time takes the usual
  `[valid_from, valid_to)` as-of join, not the current version.
- **Cancellations release their holds.** A cancelled leg (119 this tape,
  4.3%) may still appear in `fact_tail_assignment` / `fact_leg_crew` (holds
  it acquired before scrubbing) and in the wait facts (queue positions it
  abandoned). Join to `fact_flight_leg.status` to separate flown from
  scrubbed work; the *why* is `dim_scheduled_leg.cancel_reason`. Weather
  scrubs (code B) cancel **before** the published time, crew scrubs (code A)
  after it.

## table: fact_lifecycle_state

State occupancies, not events: each row is an interval `[entered_at,
exited_at)`, and `exited_at` is NULL only on a subject's last observed state.
A state entered and left again at the same recorded instant never lands a
row, so the flight timeline shows `dispatching` only for the 253 legs that
actually waited at the gate for a tail (a 12-tail fleet works harder than
round 2's twenty) and `crewing_captain` / `crewing_fo` only for legs that
waited for a seat (111 and 87); `committing` appears only on the 38 weather
scrubs administered there — a leg granted its resources without waiting
passes straight to `boarding`, the pushback-and-taxi interval every flown
leg has. The same collapse is why the instantaneous `payday` never appears
here — see `fact_payday`. Separately, a short real wait can render as
`entered_at = exited_at` at the clock's minute resolution — that is a
sub-minute interval, not a missing one.

## table: fact_payday

One row per payslip issued (859 this tape): the member's pay packet landing,
at its instant — semi-monthly paydays, each member's packet jittered to its
own minute through the morning rather than a single synchronized stamp. This
is the only payday surface in the star: the payday state is instantaneous,
so the lifecycle timeline cannot carry it.

## table: fact_leg_crew

One row per seat hold, not per leg: the captain seat and the first-officer
seat each get a row from boarding (or reserve callout) through release at the
end of the block. `assignment_type = 'reserve_callout'` marks a seat refilled
mid-block after a sick pull. A hold whose `released_at` is NULL was still
aboard at the end of the quarter's data.

## table: fact_leg_assembly_wait

The scarce-resource component of pushback delay, now delay-coded: each row
carries `delay_code` into `dim_delay_reason`. One leg can wait more than
once (a tail wait, then a captain wait, then a first-officer wait, then — if
a seat was lost mid-block — a reserve-callout wait), so sum per `leg_code`;
most legs assemble without queuing at all (456 waits across 2,737 legs), so
the everyday push-early/push-late spread is gate work, not this table. A
wait row whose `granted_at` is NULL was still queued at the end of the
quarter's data; a cancelled leg's wait ends when the leg scrubs, not with a
grant.

## table: dim_crew_member

Versions window on the member's whole tracked history: a sickness episode
increments `sickness_episodes_to_date`, and every duty day's
`consecutive_duty_days` write, cut a version even when `employment_status`
is unchanged — 8,867 versions over 102 members. Counting rows is not
counting people — there are 102 members (84 on the payroll when the data
opens, 18 hired during it); a member's attribute at an event instant is the
as-of join on `[valid_from, valid_to)`.
