# A regional retailer with its own warehouse and delivery fleet: employment, rostering, customer acquisition, orders, fulfillment, support, returns, and replenishment on one shared cast, with workforce, service, and reorder loops closed by mechanism.

## Flow
```mermaid
flowchart LR
    n6bc7962a["journey:genesis"]
    n518ee539["journey:employment"]
    nfdbf06df["journey:shift_cycle"]
    n012e91a3["journey:customer_lifecycle"]
    n400f2a07["journey:fulfillment"]
    n88f82093["journey:support_case"]
    ne1e2c075["journey:replenishment"]
    na4ccc179["arrival:hiring_stream"]
    nd04ba642["arrival:acquisition_stream"]
    na3c729c6["arrival:order_stream"]
    n603bd209["arrival:po_stream"]
    n6bc7962a -->|"trigger"| n518ee539
    n6bc7962a -->|"trigger"| n012e91a3
    n518ee539 -->|"trigger ×2"| nfdbf06df
    n400f2a07 -->|"trigger"| n88f82093
    nfdbf06df -->|"reentry [on actor.stage]"| nfdbf06df
    na4ccc179 -->|"arrival"| n518ee539
    nd04ba642 -->|"arrival"| n012e91a3
    na3c729c6 -->|"arrival"| n400f2a07
    n603bd209 -->|"arrival"| ne1e2c075
```

## Types
- **actor.employment** — One employee's career at the company, from requisition through exit; carries the skill/facility identity that routes their shifts and the burnout state that drives attrition.
- **actor.customer_lifecycle** — One customer's tenure: prospect -> active -> lapsing -> churned, with win-back; owns the customer entity's lifecycle.
- **actor.order** — One customer order from placement to rest; after a failed delivery the same actor also walks the support_case journey.
- **actor.purchase_order** — One supplier order: raised -> in_transit -> received; receipt restocks the targeted stock line.
- **entity.customer** — A live customer account: order-placement candidate (weighted by propensity) and carrier of the support-written satisfaction signal.
- **entity.stock** — One catalog line: category, demand popularity, and the on-hand count that dispatch depletes and receipts replenish.
- **resource.crew_pool** — A facility crew's working capacity (pickers north/south, drivers north/south); each attended shift raises it for the shift's duration.
- **resource.agent_pool** — The central support desk's working capacity; support-skill attendance raises it. Priority discipline lets escalated cases jump the queue.
- **diary.shift_diary** — A facility's roster calendar; booking a slot schedules a shift, and the no-show draw at the opening is the sick day.
- **diary.delivery_slot** — A facility's delivery-window calendar; the no-show draw at the opening is customer-not-home, the delivery-failure mechanism.

## Resources
- **resource.crew_pool** _(pool)_ — A facility crew's working capacity (pickers north/south, drivers north/south); each attended shift raises it for the shift's duration.
  - seized by: fulfillment.out_for_delivery (tick), fulfillment.picking (tick)
- **resource.agent_pool** _(pool)_ — The central support desk's working capacity; support-skill attendance raises it. Priority discipline lets escalated cases jump the queue.
  - seized by: support_case.awaiting_agent (tick), support_case.escalated (tick)
- **diary.shift_diary** _(diary)_ — A facility's roster calendar; booking a slot schedules a shift, and the no-show draw at the opening is the sick day.
  - booked by: shift_cycle.book_shift (tick)
- **diary.delivery_slot** _(diary)_ — A facility's delivery-window calendar; the no-show draw at the opening is customer-not-home, the delivery-failure mechanism.
  - booked by: fulfillment.awaiting_dispatch (tick)

## Journeys
- **genesis** — Kickoff router for the generated starting population: veterans go to the employment journey, established customers to the lifecycle journey.
  - **genesis_start** — Routing point for the generated starting population, before each veteran joins its real journey.
  - **seeded** — Kickoff complete; the actor now lives in its routed journey.
```mermaid
stateDiagram-v2
    [*] --> n50724887
    n50724887 --> nd427d15f : [on actor.stage]
    n50724887 --> nd427d15f : [default]
    nd427d15f --> [*]
    state "genesis_start" as n50724887
    state "seeded" as nd427d15f
```
- **employment** — One career: the hiring pipeline (whose onboarding delay is the workforce loop's inertia), the burnout-weighted attrition roll, and the notice period the hiring aggregate counts.
  - **intake** — Entry gate for a career: new requisitions head into the hiring pipeline, veterans step straight onto the floor.
  - **requisition** — An approved opening awaiting a sourcing effort.
  - **sourcing** — The role is being searched: postings, screens, interviews.
  - **offer** — An offer is out and awaiting acceptance.
  - **onboarding** — Hired but not yet useful: training, equipment, ramp-up.
  - **productive** — A working member of the crew, rostered onto shifts.
  - **notice** — Resignation handed in; still rostered through the notice period.
  - **exited** — The career at this company has ended.
```mermaid
stateDiagram-v2
    [*] --> n4736bc31
    n4736bc31 --> ne18ccb84 : [on actor.stage]
    n4736bc31 --> n60c394d0 : [default]
    ne18ccb84 --> n77f828c6
    n77f828c6 --> n988180bf
    n988180bf --> na4963c49
    na4963c49 --> n60c394d0
    n60c394d0 --> n60c394d0
    n60c394d0 --> n9368a7d2
    n9368a7d2 --> nb251994c
    nb251994c --> [*]
    state "intake" as n4736bc31
    state "requisition" as ne18ccb84
    state "productive" as n60c394d0
    state "sourcing" as n77f828c6
    state "offer" as n988180bf
    state "onboarding" as na4963c49
    state "notice" as n9368a7d2
    state "exited" as nb251994c
```
- **shift_cycle** — The roster loop, one journey instance per shift: book a facility slot, roll the sick-day draw at the opening, and move the matching crew pool's capacity +1/-1 around each attended shift. Re-entry (gated on still being employed) books the next shift.
  - **roster_check** — Between shifts: bind the crew pool and check whether this person is still on the roster.
  - **book_shift** — Holding a booked slot on the facility roster until the shift starts.
  - **on_shift** — Working a shift; the crew pool holds this person's capacity.
  - **recovering** — Off sick after a no-show; recovering before rejoining the roster.
  - **idle** — No bookable shift was available; waiting before trying again.
  - **shift_done** — The shift cycle has closed; re-entry books the next one while employment lasts.
```mermaid
stateDiagram-v2
    [*] --> n1742507c
    n1742507c --> na9d1818a : [on actor.stage]
    n1742507c --> nfaaeb99d : [default]
    na9d1818a --> n78c79c4b
    na9d1818a --> n776477ae
    na9d1818a --> n4fb62348
    n78c79c4b --> nfaaeb99d
    n776477ae --> nfaaeb99d
    n4fb62348 --> nfaaeb99d
    nfaaeb99d --> [*]
    state "roster_check" as n1742507c
    state "book_shift" as na9d1818a
    state "shift_done" as nfaaeb99d
    state "on_shift" as n78c79c4b
    state "recovering" as n776477ae
    state "idle" as n4fb62348
```
- **customer_lifecycle** — One tenure: activation mints the customer entity; the active poll mixes natural (loyalty-weighted) lapse with the scarred-lapse gate on support-written satisfaction; churn tombstones the entity.
  - **intake_c** — Entry gate for a tenure: new prospects enter the funnel, established customers go straight to activation.
  - **prospect** — Evaluating the retailer before a first purchase.
  - **activate_gate** — The account opens: the customer record is minted here.
  - **active** — A live, buying customer.
  - **lapsing** — Engagement has faded; the account is drifting toward churn.
  - **churned** — The tenure has ended and the account is closed.
```mermaid
stateDiagram-v2
    [*] --> n5dd83580
    n5dd83580 --> n40542f7e : [on actor.stage]
    n5dd83580 --> nbe604d68 : [default]
    n40542f7e --> nbe604d68
    nbe604d68 --> n96879611
    n96879611 --> n96879611
    n96879611 --> n9319b25d
    n96879611 --> n9319b25d : [on role.customer.bound, role.customer.satisfaction]
    n9319b25d --> n96879611
    n9319b25d --> n4d912ffa
    n4d912ffa --> [*]
    state "intake_c" as n5dd83580
    state "prospect" as n40542f7e
    state "activate_gate" as nbe604d68
    state "active" as n96879611
    state "lapsing" as n9319b25d
    state "churned" as n4d912ffa
```
- **fulfillment** — One order's flow: stock gate and depletion, picker contention, the delivery-window booking whose no-show draw is customer-not-home, the return window, and the failure edge that opens a support case on this same actor.
  - **placed** — The order exists and its product line is chosen.
  - **awaiting_stock** — Waiting for the chosen line to have units on hand.
  - **picking** — Holding (or queuing for) a picker while the order is picked.
  - **pick_parked** — Picking paused: the facility's picker pool is offline.
  - **packing** — Picked goods being packed for dispatch.
  - **awaiting_dispatch** — Packed and holding a booked delivery window.
  - **out_for_delivery** — On a van with a driver, en route to the customer.
  - **delivered** — Handed over to the customer.
  - **return_window** — Delivered and inside the return window.
  - **returning** — A return is on its way back to the warehouse.
  - **restocked** — The returned unit is back on the shelf; the order is closed.
  - **closed** — The order completed without a return.
  - **failed_delivery** — The delivery failed; a support ticket is being opened.
  - **failed** — The order ended in a failed delivery; support owns the aftermath.
```mermaid
stateDiagram-v2
    [*] --> nf41690bb
    nf41690bb --> n0e784155
    n0e784155 --> n6ac8a2e6 : [on role.stock.bound, role.stock.on_hand]
    n0e784155 --> n0e784155 : [default]
    n6ac8a2e6 --> n7c6e059c
    n6ac8a2e6 --> n60d51d11
    n60d51d11 --> n6ac8a2e6
    n7c6e059c --> ne4e0fc3f
    ne4e0fc3f --> n531f9da8
    ne4e0fc3f --> ncab2bfff : [×2]
    n531f9da8 --> n373e0712
    n373e0712 --> n54858ca3
    n54858ca3 --> nc3eefb58
    n54858ca3 --> n4c5170ac
    n4c5170ac --> n4d393ca4
    ncab2bfff --> n5d28a90f
    n4d393ca4 --> [*]
    nc3eefb58 --> [*]
    n5d28a90f --> [*]
    state "placed" as nf41690bb
    state "awaiting_stock" as n0e784155
    state "picking" as n6ac8a2e6
    state "packing" as n7c6e059c
    state "pick_parked" as n60d51d11
    state "awaiting_dispatch" as ne4e0fc3f
    state "out_for_delivery" as n531f9da8
    state "failed_delivery" as ncab2bfff
    state "delivered" as n373e0712
    state "return_window" as n54858ca3
    state "closed" as nc3eefb58
    state "returning" as n4c5170ac
    state "restocked" as n4d393ca4
    state "failed" as n5d28a90f
```
- **support_case** — The failed-delivery ticket on the same order actor: agent contention, queue-patience expiry escalating a case to senior handling that jumps the queue, and the distress-clearing resolution the satisfaction aggregate reads.
  - **ticket_opened** — A failed-delivery ticket exists and awaits triage.
  - **awaiting_agent** — Queued for (or being assigned) a support agent; a case whose queue wait outlives its patience escalates instead.
  - **resolving** — An agent is working the case.
  - **escalated** — Escalated for senior handling, jumping the agent queue.
  - **esc_resolving** — A senior agent is working the escalated case.
  - **resolved** — The case is closed; the order actor retires.
```mermaid
stateDiagram-v2
    [*] --> n157fe44e
    n157fe44e --> n37cbbbd0
    n37cbbbd0 --> n878f5aa2
    n37cbbbd0 --> nad9ce018
    n878f5aa2 --> ndc676b42
    nad9ce018 --> n9977d767
    n9977d767 --> ndc676b42
    ndc676b42 --> [*]
    state "ticket_opened" as n157fe44e
    state "awaiting_agent" as n37cbbbd0
    state "resolving" as n878f5aa2
    state "escalated" as nad9ce018
    state "resolved" as ndc676b42
    state "esc_resolving" as n9977d767
```
- **replenishment** — One supplier order: target a popular stock line, wait out the supplier lead time, and restock on receipt.
  - **raised** — The purchase order exists and its restock target is chosen.
  - **in_transit** — Goods are with the supplier or on the way in.
  - **received** — Goods received and shelved; the purchase order closes.
```mermaid
stateDiagram-v2
    [*] --> nd9325e09
    nd9325e09 --> n41ecc4aa
    n41ecc4aa --> n318c5ad5
    n318c5ad5 --> [*]
    state "raised" as nd9325e09
    state "in_transit" as n41ecc4aa
    state "received" as n318c5ad5
```

## Influence Rules
- **burnout_spread** — Burnout spreads by contact between productive colleagues at the same facility; the workload feedback scales the per-contact probability.

## Arrival Streams
- **hiring_stream** — Requisitions opening; the attrition feedback scales this rate, so hiring responds to notice counts after the sourcing + onboarding lag.
- **acquisition_stream** — New prospects entering the funnel.
- **order_stream** — Order placement over the live customer base: each order picks its customer weighted by propensity, so demand concentrates on heavy buyers and shrinks as churn tombstones accounts.
- **po_stream** — Purchase orders raised; the backorder feedback scales this rate, closing the reorder loop.

## Events
- **demand_scales_with_base** — Order volume tracks the live customer base: the count of active + lapsing tenures scales the order stream, so churn shrinks demand and acquisition grows it.
- **hiring_responds_to_attrition** — The workforce loop's closing link: staff in notice raise the requisition rate; relief still waits out sourcing + onboarding.
- **workload_drives_burnout** — Understaffing becomes contagion pressure: orders in picking per productive employee scales the burnout rule's per-contact probability.
- **stockout_drives_reordering** — The reorder loop's closing link: orders stuck awaiting stock raise the purchase-order rate.
- **service_failures_scar_customers** — The service loop's customer link: each customer's count of distressed orders maps to a satisfaction value written on their account; resolution clears distress, so satisfaction recovers.
- **spring_peak** — A three-week seasonal demand surge peaking mid-March.
- **friday_promo** — A weekly Friday daytime promotion lifting order volume.
- **north_facility_outage** — A two-and-a-half-day outage takes the north pickers' standing slot offline; the cut drains passively and the revert promotes waiting picks. (The cut is bounded by the pool's static skeleton slot: attendance-driven capacity offers no floor for a deeper fixed delta.)
