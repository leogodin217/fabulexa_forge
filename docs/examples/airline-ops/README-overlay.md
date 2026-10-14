## overview

A two-base airline's quarter (2026-03-02 to 2026-05-31, UTC): 5,759
out-and-back legs fly from the east and west stations, each assembling a tail
and a crew pairing from its base's pools. Crew duty days, sickness, paydays
and resignations drive crew supply; faults ground tails on the turn; a spring
flu wave lifts the sickness draw across both bases. **Delay is emergent** —
nothing in the data stamps a "delay minutes" column. Pushback delay, duty
overrun and cancellation pressure all have to be *derived* from the wait and
assignment intervals, which is this warehouse's point.

The headline derivations:

- **Pushback delay per leg** — the leg's time queued for a tail and for crew:

  ```sql
  select flight_number, sum(epoch(granted_at - queued_from)) / 60.0 as delay_min
  from fact_leg_assembly_wait
  where granted_at is not null
  group by flight_number
  ```

  or, equivalently, the leg's `dispatching` + `crewing` occupancy in
  `fact_lifecycle_state` (`process = 'flight'`) net of the grant instants.

- **Tail utilisation** — `fact_tail_assignment` spans (block + turnaround +
  repair) summed per `aircraft_key` against the quarter's span.

- **Crew supply vs. the flu wave** — `fact_crew_presence` (`presence =
  'sick'`) counted by week against `fact_leg_assembly_wait` rows with
  `waiting_for in ('crew', 'reserve_crew')`: sickness stands pairings down,
  and the crew-wait queue lengthens.

- **Duty overrun** — `fact_duty_release_wait` spans: a pairing whose duty day
  ended while its leg was still airborne queues to stand down.

Things to know before you query:

- **One timeline table, four processes.** `fact_lifecycle_state` carries the
  flight, roster, sickness and employment timelines in one table; always
  filter on `process` first. `flight_number` is set on flight rows,
  `pairing_code` on the other three — exactly one of the two per row.
- **Crew pairings, not people.** The crew unit is the *pairing* (captain +
  first officer rostered together). The base layer mints no person names or
  staff numbers — one sickness episode standing down the whole pairing is the
  modeled behavior.
- **The SCD-2 dim is small but real.** `dim_crew_pairing` versions on
  employment status *and* the episode counter, so joining a leg's crew at
  block time takes the usual `[valid_from, valid_to)` as-of join, not the
  current version.
- **Cancellations release their holds.** A cancelled leg may still appear in
  `fact_tail_assignment` / `fact_leg_crew` (holds it acquired before
  scrubbing) and in the wait facts (queue positions it abandoned). Join to
  `fact_flight_leg.status` to separate flown from scrubbed work.

## table: fact_lifecycle_state

State occupancies, not events: each row is an interval `[entered_at,
exited_at)`, and `exited_at` is NULL only on a subject's last observed state.
A state entered and exited within one engine instant is coalesced away, so
the flight timeline shows `dispatching` only for legs that actually waited at
the gate (2,244 of 5,759) — an instantly-granted tail leaves no dispatching
interval.

## table: fact_leg_assembly_wait

The pushback-delay fact. One leg can wait more than once (a tail wait, then a
crew wait, then — if its crew was pulled mid-block — a reserve-callout wait),
so sum per `flight_number`. A wait row whose `granted_at` is NULL at quarter
end was still queued when the tape stopped; a cancelled leg's wait ends when
the leg scrubs, not with a grant.

## table: dim_crew_pairing

Versions window on the pairing's whole HR history: a sickness episode
increments `sickness_episodes_to_date` and cuts a version even when
`employment_status` is unchanged. Counting rows is not counting pairings —
there are 34; and a pairing's attribute at an event instant is the as-of join
on `[valid_from, valid_to)`.
