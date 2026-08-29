# A 90-day SIEM log corpus of correlated firewall and authentication records over a segmented network: 117 internal hosts across thirty subnets run role-specific benign session journeys (each role with its own cadence, hours, and retry appetite) against a 42-server service estate behind an authored firewall ruleset, while — after a 30-day clean baseline — four external hosts walk an approach FSM toward intrusion and two ABM rules spread destination range through shared segments and subnets, so the attack signal is a shift in per-host reach and denies rather than any labeled event.

## Flow
```mermaid
flowchart LR
    nfdf51f19["journey:dispatch"]
    n618bd7e9["journey:session_developer"]
    nbb47e08d["journey:session_finance"]
    n07727a6f["journey:session_kiosk"]
    n465e5164["journey:weekend_developer"]
    ndec10d9b["journey:weekend_finance"]
    n30fbf52a["journey:weekend_kiosk"]
    nb4522e12["journey:session_service_account"]
    n8c8e9aa9["journey:approach_anytime"]
    nce971114["journey:approach_burst"]
    nc8640a2b["journey:approach_business_hours"]
    nfdf51f19 -->|"trigger"| n618bd7e9
    nfdf51f19 -->|"trigger"| n465e5164
    nfdf51f19 -->|"trigger"| nbb47e08d
    nfdf51f19 -->|"trigger"| ndec10d9b
    nfdf51f19 -->|"trigger"| n07727a6f
    nfdf51f19 -->|"trigger"| n30fbf52a
    nfdf51f19 -->|"trigger"| nb4522e12
    nfdf51f19 -->|"trigger"| nce971114
    nfdf51f19 -->|"trigger ×2"| n8c8e9aa9
    nfdf51f19 -->|"trigger"| nc8640a2b
    n618bd7e9 -->|"reentry"| n618bd7e9
    nbb47e08d -->|"reentry"| nbb47e08d
    n07727a6f -->|"reentry"| n07727a6f
    n465e5164 -->|"reentry"| n465e5164
    ndec10d9b -->|"reentry"| ndec10d9b
    n30fbf52a -->|"reentry"| n30fbf52a
    nb4522e12 -->|"reentry"| nb4522e12
    n8c8e9aa9 -->|"reentry"| n8c8e9aa9
    nce971114 -->|"reentry"| nce971114
    nc8640a2b -->|"reentry"| nc8640a2b
```

## Types
- **actor.host** — A machine that originates connections — the 117 internal workstations, kiosks, and service boxes plus the four external approach hosts — carrying the zone, subnet, role, habitual service, breadth, and auth reliability that shape every firewall and authentication record it generates.
- **entity.server** — A destination in the service estate — hostname, zone, listening port, and the service it backs; the fixed inventory whose width caps a host's distinct-destinations-per-day, the headline breadth signal.
- **entity.account** — A credential principal — user or service account — owned by one host; the identity that host's authentication records report on every accept or reject.
- **entity.fw_rule** — One entry in the authored firewall ruleset, matching a source-zone / destination-zone / port route to a disposition; routes no rule covers are the denies the intrusion narrative trips over.

## Resources
_None declared._

## Journeys
- **dispatch** — Routes each host to the session journey for its role.
  - **classifying** — The host's role decides which session cadence it runs on.
  - **routed** — The host has been handed to its role's session journey.
```mermaid
stateDiagram-v2
    [*] --> nf05f43af
    nf05f43af --> na45814b0 : [on actor.role ×4]
    nf05f43af --> na45814b0 : [on actor.home_service, actor.role ×4]
    nf05f43af --> na45814b0 : [default]
    na45814b0 --> [*]
    state "classifying" as nf05f43af
    state "routed" as na45814b0
```
- **session_developer** — Developer workstation session — the busiest human cohort.
  - **picking_destination** — The host decides whether this session is routine or off-pattern.
  - **habitual_destination** — The host reaches its own service — the routine case.
  - **exploratory_destination** — The host reaches something outside its habit — stale bookmark, wrong service, curiosity.
  - **stale_destination** — The host reaches the service it has always reached, on a route it no longer has.
  - **resolve_rule** — The firewall matches the connection against its ruleset.
  - **apply_rule** — The matched rule's disposition decides the connection.
  - **auth_challenge** — The account behind the connection is presented to the service.
  - **auth_failed** — The credentials were rejected on this attempt.
  - **session_active** — The connection is established and carrying traffic.
  - **session_closed** — The session ran to completion and closed normally.
  - **abandoned** — Authentication failed and the host stopped retrying.
  - **denied** — A rule explicitly denied the connection.
  - **blocked** — No rule matched; the default-deny posture applied.
```mermaid
stateDiagram-v2
    [*] --> n3b196311
    n3b196311 --> nee37ce0a
    n3b196311 --> nf593be9c
    n3b196311 --> ndee05e07
    nee37ce0a --> n0e6134d8
    nf593be9c --> n0e6134d8
    ndee05e07 --> n0e6134d8
    n0e6134d8 --> n6973dddd : [on role.matched_rule.bound]
    n0e6134d8 --> n37084f36 : [default]
    n37084f36 --> nd721aca5 : [on role.matched_rule.action, role.matched_rule.bound]
    n37084f36 --> n62d6c233 : [default]
    nd721aca5 --> n5d98ea23
    nd721aca5 --> n521d9177
    n521d9177 --> nd721aca5
    n521d9177 --> n20e7f550
    n5d98ea23 --> n9c23bde3 : [×3]
    n9c23bde3 --> [*]
    n20e7f550 --> [*]
    n62d6c233 --> [*]
    n6973dddd --> [*]
    state "picking_destination" as n3b196311
    state "habitual_destination" as nee37ce0a
    state "exploratory_destination" as nf593be9c
    state "stale_destination" as ndee05e07
    state "resolve_rule" as n0e6134d8
    state "blocked" as n6973dddd
    state "apply_rule" as n37084f36
    state "auth_challenge" as nd721aca5
    state "denied" as n62d6c233
    state "session_active" as n5d98ea23
    state "auth_failed" as n521d9177
    state "abandoned" as n20e7f550
    state "session_closed" as n9c23bde3
```
- **session_finance** — Finance workstation session — same body, slower cadence.
  - **picking_destination** — The host decides whether this session is routine or off-pattern.
  - **habitual_destination** — The host reaches its own service — the routine case.
  - **exploratory_destination** — The host reaches something outside its habit — stale bookmark, wrong service, curiosity.
  - **stale_destination** — The host reaches the service it has always reached, on a route it no longer has.
  - **resolve_rule** — The firewall matches the connection against its ruleset.
  - **apply_rule** — The matched rule's disposition decides the connection.
  - **auth_challenge** — The account behind the connection is presented to the service.
  - **auth_failed** — The credentials were rejected on this attempt.
  - **session_active** — The connection is established and carrying traffic.
  - **session_closed** — The session ran to completion and closed normally.
  - **abandoned** — Authentication failed and the host stopped retrying.
  - **denied** — A rule explicitly denied the connection.
  - **blocked** — No rule matched; the default-deny posture applied.
```mermaid
stateDiagram-v2
    [*] --> n3b196311
    n3b196311 --> nee37ce0a
    n3b196311 --> nf593be9c
    n3b196311 --> ndee05e07
    nee37ce0a --> n0e6134d8
    nf593be9c --> n0e6134d8
    ndee05e07 --> n0e6134d8
    n0e6134d8 --> n6973dddd : [on role.matched_rule.bound]
    n0e6134d8 --> n37084f36 : [default]
    n37084f36 --> nd721aca5 : [on role.matched_rule.action, role.matched_rule.bound]
    n37084f36 --> n62d6c233 : [default]
    nd721aca5 --> n5d98ea23
    nd721aca5 --> n521d9177
    n521d9177 --> nd721aca5
    n521d9177 --> n20e7f550
    n5d98ea23 --> n9c23bde3 : [×3]
    n9c23bde3 --> [*]
    n20e7f550 --> [*]
    n62d6c233 --> [*]
    n6973dddd --> [*]
    state "picking_destination" as n3b196311
    state "habitual_destination" as nee37ce0a
    state "exploratory_destination" as nf593be9c
    state "stale_destination" as ndee05e07
    state "resolve_rule" as n0e6134d8
    state "blocked" as n6973dddd
    state "apply_rule" as n37084f36
    state "auth_challenge" as nd721aca5
    state "denied" as n62d6c233
    state "session_active" as n5d98ea23
    state "auth_failed" as n521d9177
    state "abandoned" as n20e7f550
    state "session_closed" as n9c23bde3
```
- **session_kiosk** — Shared kiosk session — the quietest cohort, same working hours.
  - **picking_destination** — The host decides whether this session is routine or off-pattern.
  - **habitual_destination** — The host reaches its own service — the routine case.
  - **exploratory_destination** — The host reaches something outside its habit — stale bookmark, wrong service, curiosity.
  - **stale_destination** — The host reaches the service it has always reached, on a route it no longer has.
  - **resolve_rule** — The firewall matches the connection against its ruleset.
  - **apply_rule** — The matched rule's disposition decides the connection.
  - **auth_challenge** — The account behind the connection is presented to the service.
  - **auth_failed** — The credentials were rejected on this attempt.
  - **session_active** — The connection is established and carrying traffic.
  - **session_closed** — The session ran to completion and closed normally.
  - **abandoned** — Authentication failed and the host stopped retrying.
  - **denied** — A rule explicitly denied the connection.
  - **blocked** — No rule matched; the default-deny posture applied.
```mermaid
stateDiagram-v2
    [*] --> n3b196311
    n3b196311 --> nee37ce0a
    n3b196311 --> nf593be9c
    n3b196311 --> ndee05e07
    nee37ce0a --> n0e6134d8
    nf593be9c --> n0e6134d8
    ndee05e07 --> n0e6134d8
    n0e6134d8 --> n6973dddd : [on role.matched_rule.bound]
    n0e6134d8 --> n37084f36 : [default]
    n37084f36 --> nd721aca5 : [on role.matched_rule.action, role.matched_rule.bound]
    n37084f36 --> n62d6c233 : [default]
    nd721aca5 --> n5d98ea23
    nd721aca5 --> n521d9177
    n521d9177 --> nd721aca5
    n521d9177 --> n20e7f550
    n5d98ea23 --> n9c23bde3 : [×3]
    n9c23bde3 --> [*]
    n20e7f550 --> [*]
    n62d6c233 --> [*]
    n6973dddd --> [*]
    state "picking_destination" as n3b196311
    state "habitual_destination" as nee37ce0a
    state "exploratory_destination" as nf593be9c
    state "stale_destination" as ndee05e07
    state "resolve_rule" as n0e6134d8
    state "blocked" as n6973dddd
    state "apply_rule" as n37084f36
    state "auth_challenge" as nd721aca5
    state "denied" as n62d6c233
    state "session_active" as n5d98ea23
    state "auth_failed" as n521d9177
    state "abandoned" as n20e7f550
    state "session_closed" as n9c23bde3
```
- **weekend_developer** — Developer working at the weekend — same session, sparser regime.
  - **picking_destination** — The host decides whether this session is routine or off-pattern.
  - **habitual_destination** — The host reaches its own service — the routine case.
  - **exploratory_destination** — The host reaches something outside its habit — stale bookmark, wrong service, curiosity.
  - **stale_destination** — The host reaches the service it has always reached, on a route it no longer has.
  - **resolve_rule** — The firewall matches the connection against its ruleset.
  - **apply_rule** — The matched rule's disposition decides the connection.
  - **auth_challenge** — The account behind the connection is presented to the service.
  - **auth_failed** — The credentials were rejected on this attempt.
  - **session_active** — The connection is established and carrying traffic.
  - **session_closed** — The session ran to completion and closed normally.
  - **abandoned** — Authentication failed and the host stopped retrying.
  - **denied** — A rule explicitly denied the connection.
  - **blocked** — No rule matched; the default-deny posture applied.
```mermaid
stateDiagram-v2
    [*] --> n3b196311
    n3b196311 --> nee37ce0a
    n3b196311 --> nf593be9c
    n3b196311 --> ndee05e07
    nee37ce0a --> n0e6134d8
    nf593be9c --> n0e6134d8
    ndee05e07 --> n0e6134d8
    n0e6134d8 --> n6973dddd : [on role.matched_rule.bound]
    n0e6134d8 --> n37084f36 : [default]
    n37084f36 --> nd721aca5 : [on role.matched_rule.action, role.matched_rule.bound]
    n37084f36 --> n62d6c233 : [default]
    nd721aca5 --> n5d98ea23
    nd721aca5 --> n521d9177
    n521d9177 --> nd721aca5
    n521d9177 --> n20e7f550
    n5d98ea23 --> n9c23bde3 : [×3]
    n9c23bde3 --> [*]
    n20e7f550 --> [*]
    n62d6c233 --> [*]
    n6973dddd --> [*]
    state "picking_destination" as n3b196311
    state "habitual_destination" as nee37ce0a
    state "exploratory_destination" as nf593be9c
    state "stale_destination" as ndee05e07
    state "resolve_rule" as n0e6134d8
    state "blocked" as n6973dddd
    state "apply_rule" as n37084f36
    state "auth_challenge" as nd721aca5
    state "denied" as n62d6c233
    state "session_active" as n5d98ea23
    state "auth_failed" as n521d9177
    state "abandoned" as n20e7f550
    state "session_closed" as n9c23bde3
```
- **weekend_finance** — Finance working at the weekend — same session, sparser regime.
  - **picking_destination** — The host decides whether this session is routine or off-pattern.
  - **habitual_destination** — The host reaches its own service — the routine case.
  - **exploratory_destination** — The host reaches something outside its habit — stale bookmark, wrong service, curiosity.
  - **stale_destination** — The host reaches the service it has always reached, on a route it no longer has.
  - **resolve_rule** — The firewall matches the connection against its ruleset.
  - **apply_rule** — The matched rule's disposition decides the connection.
  - **auth_challenge** — The account behind the connection is presented to the service.
  - **auth_failed** — The credentials were rejected on this attempt.
  - **session_active** — The connection is established and carrying traffic.
  - **session_closed** — The session ran to completion and closed normally.
  - **abandoned** — Authentication failed and the host stopped retrying.
  - **denied** — A rule explicitly denied the connection.
  - **blocked** — No rule matched; the default-deny posture applied.
```mermaid
stateDiagram-v2
    [*] --> n3b196311
    n3b196311 --> nee37ce0a
    n3b196311 --> nf593be9c
    n3b196311 --> ndee05e07
    nee37ce0a --> n0e6134d8
    nf593be9c --> n0e6134d8
    ndee05e07 --> n0e6134d8
    n0e6134d8 --> n6973dddd : [on role.matched_rule.bound]
    n0e6134d8 --> n37084f36 : [default]
    n37084f36 --> nd721aca5 : [on role.matched_rule.action, role.matched_rule.bound]
    n37084f36 --> n62d6c233 : [default]
    nd721aca5 --> n5d98ea23
    nd721aca5 --> n521d9177
    n521d9177 --> nd721aca5
    n521d9177 --> n20e7f550
    n5d98ea23 --> n9c23bde3 : [×3]
    n9c23bde3 --> [*]
    n20e7f550 --> [*]
    n62d6c233 --> [*]
    n6973dddd --> [*]
    state "picking_destination" as n3b196311
    state "habitual_destination" as nee37ce0a
    state "exploratory_destination" as nf593be9c
    state "stale_destination" as ndee05e07
    state "resolve_rule" as n0e6134d8
    state "blocked" as n6973dddd
    state "apply_rule" as n37084f36
    state "auth_challenge" as nd721aca5
    state "denied" as n62d6c233
    state "session_active" as n5d98ea23
    state "auth_failed" as n521d9177
    state "abandoned" as n20e7f550
    state "session_closed" as n9c23bde3
```
- **weekend_kiosk** — Kiosk used at the weekend — same session, sparser regime.
  - **picking_destination** — The host decides whether this session is routine or off-pattern.
  - **habitual_destination** — The host reaches its own service — the routine case.
  - **exploratory_destination** — The host reaches something outside its habit — stale bookmark, wrong service, curiosity.
  - **stale_destination** — The host reaches the service it has always reached, on a route it no longer has.
  - **resolve_rule** — The firewall matches the connection against its ruleset.
  - **apply_rule** — The matched rule's disposition decides the connection.
  - **auth_challenge** — The account behind the connection is presented to the service.
  - **auth_failed** — The credentials were rejected on this attempt.
  - **session_active** — The connection is established and carrying traffic.
  - **session_closed** — The session ran to completion and closed normally.
  - **abandoned** — Authentication failed and the host stopped retrying.
  - **denied** — A rule explicitly denied the connection.
  - **blocked** — No rule matched; the default-deny posture applied.
```mermaid
stateDiagram-v2
    [*] --> n3b196311
    n3b196311 --> nee37ce0a
    n3b196311 --> nf593be9c
    n3b196311 --> ndee05e07
    nee37ce0a --> n0e6134d8
    nf593be9c --> n0e6134d8
    ndee05e07 --> n0e6134d8
    n0e6134d8 --> n6973dddd : [on role.matched_rule.bound]
    n0e6134d8 --> n37084f36 : [default]
    n37084f36 --> nd721aca5 : [on role.matched_rule.action, role.matched_rule.bound]
    n37084f36 --> n62d6c233 : [default]
    nd721aca5 --> n5d98ea23
    nd721aca5 --> n521d9177
    n521d9177 --> nd721aca5
    n521d9177 --> n20e7f550
    n5d98ea23 --> n9c23bde3 : [×3]
    n9c23bde3 --> [*]
    n20e7f550 --> [*]
    n62d6c233 --> [*]
    n6973dddd --> [*]
    state "picking_destination" as n3b196311
    state "habitual_destination" as nee37ce0a
    state "exploratory_destination" as nf593be9c
    state "stale_destination" as ndee05e07
    state "resolve_rule" as n0e6134d8
    state "blocked" as n6973dddd
    state "apply_rule" as n37084f36
    state "auth_challenge" as nd721aca5
    state "denied" as n62d6c233
    state "session_active" as n5d98ea23
    state "auth_failed" as n521d9177
    state "abandoned" as n20e7f550
    state "session_closed" as n9c23bde3
```
- **session_service_account** — Batch service account — machine cadence, no working hours.
  - **picking_destination** — The host decides whether this session is routine or off-pattern.
  - **habitual_destination** — The host reaches its own service — the routine case.
  - **exploratory_destination** — The host reaches something outside its habit — stale bookmark, wrong service, curiosity.
  - **stale_destination** — The host reaches the service it has always reached, on a route it no longer has.
  - **resolve_rule** — The firewall matches the connection against its ruleset.
  - **apply_rule** — The matched rule's disposition decides the connection.
  - **auth_challenge** — The account behind the connection is presented to the service.
  - **auth_failed** — The credentials were rejected on this attempt.
  - **session_active** — The connection is established and carrying traffic.
  - **session_closed** — The session ran to completion and closed normally.
  - **abandoned** — Authentication failed and the host stopped retrying.
  - **denied** — A rule explicitly denied the connection.
  - **blocked** — No rule matched; the default-deny posture applied.
```mermaid
stateDiagram-v2
    [*] --> n3b196311
    n3b196311 --> nee37ce0a
    n3b196311 --> nf593be9c
    n3b196311 --> ndee05e07
    nee37ce0a --> n0e6134d8
    nf593be9c --> n0e6134d8
    ndee05e07 --> n0e6134d8
    n0e6134d8 --> n6973dddd : [on role.matched_rule.bound]
    n0e6134d8 --> n37084f36 : [default]
    n37084f36 --> nd721aca5 : [on role.matched_rule.action, role.matched_rule.bound]
    n37084f36 --> n62d6c233 : [default]
    nd721aca5 --> n5d98ea23
    nd721aca5 --> n521d9177
    n521d9177 --> nd721aca5
    n521d9177 --> n20e7f550
    n5d98ea23 --> n9c23bde3 : [×3]
    n9c23bde3 --> [*]
    n20e7f550 --> [*]
    n62d6c233 --> [*]
    n6973dddd --> [*]
    state "picking_destination" as n3b196311
    state "habitual_destination" as nee37ce0a
    state "exploratory_destination" as nf593be9c
    state "stale_destination" as ndee05e07
    state "resolve_rule" as n0e6134d8
    state "blocked" as n6973dddd
    state "apply_rule" as n37084f36
    state "auth_challenge" as nd721aca5
    state "denied" as n62d6c233
    state "session_active" as n5d98ea23
    state "auth_failed" as n521d9177
    state "abandoned" as n20e7f550
    state "session_closed" as n9c23bde3
```
- **approach_anytime** — An external host working through the perimeter, on no calendar.
  - **probing** — The host picks a destination and tries it.
  - **resolve_probe** — The perimeter evaluates the connection, exactly as it does for anyone.
  - **probe_again** — That route was shut; the host decides whether to keep looking.
  - **presenting_credentials** — The host offers credentials for an account it does not own.
  - **attempt_rejected** — The credentials were refused; the counter advances.
  - **persisting** — The host decides whether to try again or come back another time.
  - **locked_out** — The account stopped accepting attempts.
  - **inside** — The credentials were accepted.
  - **staging** — An ordinary session, on an unusual destination for this host.
  - **moving** — A second session, larger, outbound.
  - **away** — The host is not doing anything observable.
```mermaid
stateDiagram-v2
    [*] --> n20b10772
    n20b10772 --> nc31e6866
    nc31e6866 --> nf1491471 : [on role.matched_rule.action, role.matched_rule.bound]
    nc31e6866 --> n7a14bc96 : [default]
    n7a14bc96 --> n20b10772
    n7a14bc96 --> n378ce56a
    nf1491471 --> n106b0862
    nf1491471 --> n61a3adf3
    n61a3adf3 --> nccaba3bb : [on actor.failed_attempts]
    n61a3adf3 --> n2796873b : [default]
    n2796873b --> n20b10772
    n2796873b --> n378ce56a
    nccaba3bb --> n20b10772
    nccaba3bb --> n378ce56a
    n106b0862 --> ne919a753
    ne919a753 --> n6e862940
    n6e862940 --> n378ce56a
    n378ce56a --> [*]
    state "probing" as n20b10772
    state "resolve_probe" as nc31e6866
    state "presenting_credentials" as nf1491471
    state "probe_again" as n7a14bc96
    state "away" as n378ce56a
    state "inside" as n106b0862
    state "attempt_rejected" as n61a3adf3
    state "locked_out" as nccaba3bb
    state "persisting" as n2796873b
    state "staging" as ne919a753
    state "moving" as n6e862940
```
- **approach_burst** — The same approach, worked through in one sitting rather than over weeks.
  - **probing** — The host picks a destination and tries it.
  - **resolve_probe** — The perimeter evaluates the connection, exactly as it does for anyone.
  - **probe_again** — That route was shut; the host decides whether to keep looking.
  - **presenting_credentials** — The host offers credentials for an account it does not own.
  - **attempt_rejected** — The credentials were refused; the counter advances.
  - **persisting** — The host decides whether to try again or come back another time.
  - **locked_out** — The account stopped accepting attempts.
  - **inside** — The credentials were accepted.
  - **staging** — An ordinary session, on an unusual destination for this host.
  - **moving** — A second session, larger, outbound.
  - **away** — The host is not doing anything observable.
```mermaid
stateDiagram-v2
    [*] --> n20b10772
    n20b10772 --> nc31e6866
    nc31e6866 --> nf1491471 : [on role.matched_rule.action, role.matched_rule.bound]
    nc31e6866 --> n7a14bc96 : [default]
    n7a14bc96 --> n20b10772
    n7a14bc96 --> n378ce56a
    nf1491471 --> n106b0862
    nf1491471 --> n61a3adf3
    n61a3adf3 --> nccaba3bb : [on actor.failed_attempts]
    n61a3adf3 --> n2796873b : [default]
    n2796873b --> n20b10772
    n2796873b --> n378ce56a
    nccaba3bb --> n20b10772
    nccaba3bb --> n378ce56a
    n106b0862 --> ne919a753
    ne919a753 --> n6e862940
    n6e862940 --> n378ce56a
    n378ce56a --> [*]
    state "probing" as n20b10772
    state "resolve_probe" as nc31e6866
    state "presenting_credentials" as nf1491471
    state "probe_again" as n7a14bc96
    state "away" as n378ce56a
    state "inside" as n106b0862
    state "attempt_rejected" as n61a3adf3
    state "locked_out" as nccaba3bb
    state "persisting" as n2796873b
    state "staging" as ne919a753
    state "moving" as n6e862940
```
- **approach_business_hours** — The same approach, confined to working hours — it arrives inside the crowd.
  - **probing** — The host picks a destination and tries it.
  - **resolve_probe** — The perimeter evaluates the connection, exactly as it does for anyone.
  - **probe_again** — That route was shut; the host decides whether to keep looking.
  - **presenting_credentials** — The host offers credentials for an account it does not own.
  - **attempt_rejected** — The credentials were refused; the counter advances.
  - **persisting** — The host decides whether to try again or come back another time.
  - **locked_out** — The account stopped accepting attempts.
  - **inside** — The credentials were accepted.
  - **staging** — An ordinary session, on an unusual destination for this host.
  - **moving** — A second session, larger, outbound.
  - **away** — The host is not doing anything observable.
```mermaid
stateDiagram-v2
    [*] --> n20b10772
    n20b10772 --> nc31e6866
    nc31e6866 --> nf1491471 : [on role.matched_rule.action, role.matched_rule.bound]
    nc31e6866 --> n7a14bc96 : [default]
    n7a14bc96 --> n20b10772
    n7a14bc96 --> n378ce56a
    nf1491471 --> n106b0862
    nf1491471 --> n61a3adf3
    n61a3adf3 --> nccaba3bb : [on actor.failed_attempts]
    n61a3adf3 --> n2796873b : [default]
    n2796873b --> n20b10772
    n2796873b --> n378ce56a
    nccaba3bb --> n20b10772
    nccaba3bb --> n378ce56a
    n106b0862 --> ne919a753
    ne919a753 --> n6e862940
    n6e862940 --> n378ce56a
    n378ce56a --> [*]
    state "probing" as n20b10772
    state "resolve_probe" as nc31e6866
    state "presenting_credentials" as nf1491471
    state "probe_again" as n7a14bc96
    state "away" as n378ce56a
    state "inside" as n106b0862
    state "attempt_rejected" as n61a3adf3
    state "locked_out" as nccaba3bb
    state "persisting" as n2796873b
    state "staging" as ne919a753
    state "moving" as n6e862940
```

## Influence Rules
- **segment_reach** — A host working a segment is drawn wider by the hosts already ranging widely there.
- **subnet_spread** — A host widens its range in step with the hosts sharing its subnet.

## Arrival Streams
_None declared._

## Events
- **credential_service_wobble** — A directory service has a bad day and rejects credentials it should accept.
- **mail_maintenance** — The mail tier is taken down for patching; hosts keep reaching for it and are refused.
