# An acute NHS hospital trust: emergency and elective patients flow from a single arrivals stream through A&E, inpatient wards, outpatient clinics, diagnostics, cancer and surgical pathways, then post-discharge follow-up — competing for consultants, beds, theatres and clinic slots under seasonal demand pressure.

## Flow
```mermaid
flowchart LR
    n944ec6f7["journey:patient_intake"]
    nd71bba9c["journey:ed_attendance"]
    n321463bb["journey:inpatient_spell"]
    nff9ebeba["journey:outpatient_pathway"]
    nb9ddd2cf["journey:diagnostic_request"]
    n6db4bdd2["journey:cancer_pathway"]
    n5a113ea6["journey:surgical_pathway"]
    n3279af01["journey:post_discharge_followup"]
    n88045176["journey:fft_survey"]
    n23c7e27b["arrival:patient_arrivals"]
    n944ec6f7 -->|"trigger"| nd71bba9c
    n944ec6f7 -->|"trigger"| nff9ebeba
    n944ec6f7 -->|"trigger"| n6db4bdd2
    n944ec6f7 -->|"trigger"| n5a113ea6
    nd71bba9c -->|"trigger"| nb9ddd2cf
    nd71bba9c -->|"trigger"| n321463bb
    n321463bb -->|"trigger ×4"| n3279af01
    nff9ebeba -->|"trigger [on actor.outpatient_diagnostic_required]"| nb9ddd2cf
    nff9ebeba -->|"trigger [on actor.suspicious_finding]"| n6db4bdd2
    n3279af01 -->|"trigger [on actor.fft_invited]"| n88045176
    n3279af01 -->|"trigger"| n321463bb
    n944ec6f7 -->|"reentry [on actor.pathway_type, actor.readmission_risk, actor.suspicious_finding]"| n944ec6f7
    n23c7e27b -->|"arrival"| n944ec6f7
```

## Types
- **actor.default** — A patient of the trust — carries demographics (deprivation decile, ethnicity), clinical attributes (primary condition, frailty, comorbidities) and pathway flags that steer them through the care pathways.
- **resource.consultant** — A senior doctor with finite capacity, deliberately split by pathway: ward admissions gate themselves below full capacity to hold slots back for clinic and theatre work, so an inpatient side that plateaus short of the declared capacity is the specialty running full. Consultants of the same specialty share one priority queue, and a saturated specialty queues its patients by clinical priority.
- **entity.ward** — An inpatient ward with a bed count and a ward type (general, assessment, ICU/HDU, step-down) that stratifies safety-incident risk.
- **entity.theatre** — An operating theatre, with a specialty and a number of operating sessions per day.
- **entity.diagnostic** — A diagnostic test type (X-ray, CT, MRI, bloods, ECG, ultrasound and others) with a typical turnaround and cost.
- **entity.procedure** — A surgical procedure in the catalogue, coded by OPCS-4 with an HRG case-mix bucket, national tariff and complexity band.
- **entity.medication** — A medication in the formulary, grouped by drug class and specialty.
- **diary.outpatient_clinic** — A per-specialty outpatient clinic appointment book. The wait for a routine slot emerges from how fast referrals arrive against a fixed supply of openings.
- **diary.cancer_clinic** — A per-specialty two-week-wait cancer clinic book for urgent suspected-cancer referrals; the 2WW wait is the booking lead-time.
- **diary.operating_list** — A per-specialty elective operating-list book; the wait for surgery emerges from scarce theatre-list openings.

## Resources
- **resource.consultant** _(pool)_ — A senior doctor with finite capacity, deliberately split by pathway: ward admissions gate themselves below full capacity to hold slots back for clinic and theatre work, so an inpatient side that plateaus short of the declared capacity is the specialty running full. Consultants of the same specialty share one priority queue, and a saturated specialty queues its patients by clinical priority.
  - partition: main_specialty — 110, 180, 300, 301, 320, 340
  - seized by: ed_attendance.arrived (tick), inpatient_spell.admission_triage (tick), surgical_pathway.surgery_day (tick)
- **diary.outpatient_clinic** _(diary)_ — A per-specialty outpatient clinic appointment book. The wait for a routine slot emerges from how fast referrals arrive against a fixed supply of openings.
  - booked by: outpatient_pathway.awaiting_clinic_slot (tick)
- **diary.cancer_clinic** _(diary)_ — A per-specialty two-week-wait cancer clinic book for urgent suspected-cancer referrals; the 2WW wait is the booking lead-time.
  - booked by: cancer_pathway.awaiting_first_appt (tick)
- **diary.operating_list** _(diary)_ — A per-specialty elective operating-list book; the wait for surgery emerges from scarce theatre-list openings.
  - booked by: surgical_pathway.awaiting_surgeon (tick)

## Journeys
- **patient_intake** — Front-door triage that routes each new arrival to the right pathway — emergency, elective outpatient, cancer or surgical — by pathway type.
  - **routing** — Newly arrived patient awaiting routing to a care pathway.
  - **routed_emergency** — Routed to the emergency (A&E) pathway.
  - **routed_elective** — Routed to the elective outpatient pathway.
  - **routed_cancer** — Routed to the suspected-cancer pathway.
  - **routed_surgical** — Routed to the elective surgical pathway.
```mermaid
stateDiagram-v2
    [*] --> nfc6672ad
    nfc6672ad --> ne4100ed2 : [on actor.pathway_type]
    nfc6672ad --> naf9eb8a6 : [on actor.pathway_type]
    nfc6672ad --> ncdbb070d : [on actor.pathway_type]
    nfc6672ad --> n020bdce5 : [on actor.pathway_type]
    ne4100ed2 --> [*]
    naf9eb8a6 --> [*]
    ncdbb070d --> [*]
    n020bdce5 --> [*]
    state "routing" as nfc6672ad
    state "routed_emergency" as ne4100ed2
    state "routed_elective" as naf9eb8a6
    state "routed_cancer" as ncdbb070d
    state "routed_surgical" as n020bdce5
```
- **ed_attendance** — An A&E (emergency department) attendance: arrival, triage, assessment, diagnosis, then either discharge home or admission to a ward.
  - **arrived** — Arrived in A&E, waiting for an emergency doctor to triage them.
  - **triaged** — Triaged in A&E; awaiting clinical assessment.
  - **assessed** — Assessed by the emergency team; awaiting a diagnosis and any diagnostic tests.
  - **diagnosed** — Diagnosed in A&E; a decision is made to discharge home or admit.
  - **discharged_home** — Discharged home from A&E.
  - **admitted** — Admitted from A&E to an inpatient bed.
```mermaid
stateDiagram-v2
    [*] --> n4d1109bf
    n4d1109bf --> nc9346726
    nc9346726 --> n1cb9db72
    n1cb9db72 --> nd22b75e3 : [on actor.diagnostic_required ×2]
    nd22b75e3 --> nbd57bd77 : [on actor.admission_disposition]
    nd22b75e3 --> n52563f89 : [on actor.admission_disposition]
    nbd57bd77 --> [*]
    n52563f89 --> [*]
    state "arrived" as n4d1109bf
    state "triaged" as nc9346726
    state "assessed" as n1cb9db72
    state "diagnosed" as nd22b75e3
    state "discharged_home" as nbd57bd77
    state "admitted" as n52563f89
```
- **inpatient_spell** — An inpatient hospital stay from admission through ward care (and possible ICU escalation or death) to medically-fit, any delayed transfer of care, and discharge.
  - **admission_triage** — Admitted patient being booked in and matched to a specialty consultant, waiting if the specialty is full.
  - **ward_assignment** — Allocated to an inpatient ward.
  - **specialty_care** — Receiving specialty inpatient care; length of stay varies by condition, with a daily risk of a safety incident and a frailty-driven risk of death.
  - **icu_care** — Escalated to intensive care for the sickest patients.
  - **medically_fit** — Declared medically fit for discharge; either discharged or held for a delayed transfer of care.
  - **dtoc_assessment** — Medically fit but waiting for community or social care to be arranged (a delayed transfer of care); the wait lengthens with deprivation.
  - **inpatient_discharged** — Discharged from the inpatient ward.
  - **deceased** — Died in hospital during the inpatient stay.
```mermaid
stateDiagram-v2
    [*] --> n6b73d906
    n6b73d906 --> nd7bf8486
    nd7bf8486 --> n1af5242d
    n1af5242d --> n22687031
    n1af5242d --> n8f0e1603 : [on actor.frailty_band, actor.pathway_type]
    n1af5242d --> ne72b5e98 : [on actor.frailty_band, actor.pathway_type, actor.primary_condition ×7]
    n8f0e1603 --> ne72b5e98
    ne72b5e98 --> n9460fbf0
    ne72b5e98 --> nc74c0d97
    nc74c0d97 --> n9460fbf0 : [on actor.imd_decile ×3]
    n9460fbf0 --> [*]
    n22687031 --> [*]
    state "admission_triage" as n6b73d906
    state "ward_assignment" as nd7bf8486
    state "specialty_care" as n1af5242d
    state "deceased" as n22687031
    state "icu_care" as n8f0e1603
    state "medically_fit" as ne72b5e98
    state "inpatient_discharged" as n9460fbf0
    state "dtoc_assessment" as nc74c0d97
```
- **outpatient_pathway** — An elective outpatient referral: wait for a clinic appointment, then either attend or do-not-attend (DNA).
  - **referred** — Referred by a GP to an outpatient clinic.
  - **awaiting_clinic_slot** — On the waiting list for a clinic appointment, with a daily risk of deteriorating while waiting.
  - **attended** — Attended the outpatient appointment.
  - **dna** — Did not attend (DNA) the booked appointment, wasting the slot.
  - **attended_done** — Outpatient episode complete after attending.
  - **dna_done** — Outpatient episode closed after a missed appointment.
```mermaid
stateDiagram-v2
    [*] --> n424f14bc
    n424f14bc --> n474527e0
    n474527e0 --> n70cb5461
    n474527e0 --> nd0331ca0
    n70cb5461 --> n1013413a
    nd0331ca0 --> n6ffb2bfc
    n1013413a --> [*]
    n6ffb2bfc --> [*]
    state "referred" as n424f14bc
    state "awaiting_clinic_slot" as n474527e0
    state "attended" as n70cb5461
    state "dna" as nd0331ca0
    state "attended_done" as n1013413a
    state "dna_done" as n6ffb2bfc
```
- **diagnostic_request** — A diagnostic test order running alongside an A&E or outpatient episode: scheduling, the test being performed, reported, and the result communicated.
  - **scheduling** — Diagnostic test being scheduled.
  - **scheduling_deferred** — Test scheduling deferred into a backlog before being ordered.
  - **requested** — Test ordered; awaiting the test to be performed (turnaround varies by modality).
  - **performed** — Test performed; awaiting the report.
  - **reported** — Test reported; result being communicated.
  - **result_communicated** — Diagnostic result communicated to the requesting team.
```mermaid
stateDiagram-v2
    [*] --> na919b4a6
    na919b4a6 --> nc6a91ee7
    na919b4a6 --> n8059dbfb
    n8059dbfb --> nc6a91ee7
    nc6a91ee7 --> n8b60aa24 : [on actor.diagnostic_test_type ×6]
    n8b60aa24 --> n8e752bb9 : [on actor.diagnostic_test_type ×6]
    n8e752bb9 --> nfbb02ea0
    nfbb02ea0 --> [*]
    state "scheduling" as na919b4a6
    state "requested" as nc6a91ee7
    state "scheduling_deferred" as n8059dbfb
    state "performed" as n8b60aa24
    state "reported" as n8e752bb9
    state "result_communicated" as nfbb02ea0
```
- **cancer_pathway** — A suspected-cancer pathway on the two-week-wait standard: referral, first appointment, diagnosis, treatment decision and start of treatment, tracked against the 62-day target.
  - **referral_intake** — Urgent cancer referral being processed.
  - **referral_deferred** — Cancer referral held in a backlog before proceeding.
  - **two_week_referral** — Two-week-wait referral made; awaiting a first specialist appointment.
  - **awaiting_first_appt** — Waiting for the first two-week-wait clinic appointment.
  - **first_seen** — Seen by the specialist for the first time.
  - **diagnosis_confirmed** — Cancer diagnosis confirmed.
  - **treatment_decision** — Treatment decision made (decision-to-treat).
  - **treatment_started** — Cancer treatment started.
```mermaid
stateDiagram-v2
    [*] --> n5c13b4a1
    n5c13b4a1 --> n54e1a3eb
    n5c13b4a1 --> n9c4f7fc5
    n9c4f7fc5 --> n54e1a3eb
    n54e1a3eb --> n745dc60e
    n745dc60e --> n519e82fe
    n519e82fe --> nb6fb7016
    nb6fb7016 --> na8a1cb05
    na8a1cb05 --> n1f4c451c
    n1f4c451c --> [*]
    state "referral_intake" as n5c13b4a1
    state "two_week_referral" as n54e1a3eb
    state "referral_deferred" as n9c4f7fc5
    state "awaiting_first_appt" as n745dc60e
    state "first_seen" as n519e82fe
    state "diagnosis_confirmed" as nb6fb7016
    state "treatment_decision" as na8a1cb05
    state "treatment_started" as n1f4c451c
```
- **surgical_pathway** — An elective surgical pathway: pre-operative assessment, wait for an operating-list slot, the operation itself, and recovery to post-op review.
  - **pre_op_assessment** — Pre-operative assessment; the procedure is planned and the patient is listed for surgery.
  - **awaiting_surgeon** — On the elective waiting list for an operating-list slot.
  - **surgery_day** — Day of surgery; a specialty surgeon is taken for the operation.
  - **surgery_performed** — Operation performed in theatre.
  - **recovery** — Recovering after the operation.
  - **post_op_review** — Post-operative review; the surgical episode is complete.
```mermaid
stateDiagram-v2
    [*] --> n4e973978
    n4e973978 --> n52211cbd
    n52211cbd --> n78c67553
    n78c67553 --> n9e7f3688
    n9e7f3688 --> n8c585378
    n8c585378 --> nd12df291
    nd12df291 --> [*]
    state "pre_op_assessment" as n4e973978
    state "awaiting_surgeon" as n52211cbd
    state "surgery_day" as n78c67553
    state "surgery_performed" as n9e7f3688
    state "recovery" as n8c585378
    state "post_op_review" as nd12df291
```
- **post_discharge_followup** — Follow-up after an inpatient discharge: a check at home that either resolves to recovery or to an emergency readmission.
  - **at_home** — Recently discharged and recovering at home; an FFT invitation is sent.
  - **readmission_check** — Follow-up check; the patient either recovers or is readmitted, with risk rising by comorbidity burden.
  - **readmitting** — Being readmitted as an emergency, starting a new inpatient spell.
  - **recovered** — Recovered after discharge; the episode is closed.
  - **readmitted_recorded** — Readmission recorded; a fresh inpatient spell is underway.
```mermaid
stateDiagram-v2
    [*] --> nafffae2d
    nafffae2d --> na19bb62d
    na19bb62d --> nf6e09cc8
    na19bb62d --> n446b17f9
    n446b17f9 --> nec8e924e
    nf6e09cc8 --> [*]
    nec8e924e --> [*]
    state "at_home" as nafffae2d
    state "readmission_check" as na19bb62d
    state "recovered" as nf6e09cc8
    state "readmitting" as n446b17f9
    state "readmitted_recorded" as nec8e924e
```
- **fft_survey** — The Friends and Family Test — every discharged patient is invited to rate their care; they either respond with a score or do not respond.
  - **invited** — Invited to complete the Friends and Family Test after discharge.
  - **responded** — Responded to the survey with a satisfaction score.
  - **no_response** — Did not respond to the survey.
```mermaid
stateDiagram-v2
    [*] --> n307128ff
    n307128ff --> n42d72995 : [×5]
    n307128ff --> n594f4fb1
    n42d72995 --> [*]
    n594f4fb1 --> [*]
    state "invited" as n307128ff
    state "responded" as n42d72995
    state "no_response" as n594f4fb1
```

## Influence Rules
- **hai_transmission** — Healthcare-associated infection spread: each day, an uncolonised admitted inpatient can pick up a hospital infection from colonised patients in the same condition cohort on the ward.

## Arrival Streams
- **patient_arrivals** — The stream of patients arriving at the trust, with exponential inter-arrival times shaped by time-of-day and seasonal demand.

## Events
- **winter_pressure** — Recurring seasonal winter surge in arrivals (November to March, every year).
- **worst_winter_shock** — One unusually severe winter that stacks on top of the normal winter pressure.
- **weekend_reduction** — Fewer arrivals on Saturdays and Sundays.
- **monday_surge** — A Monday spike in arrivals as the weekend backlog presents.
- **infection_outbreak** — A sharp three-week respiratory infection outbreak in spring 2024.
- **cancer_staffing_shock** — A 2024–25 cancer-service staffing shortage that pushes more urgent referrals into backlog.
- **diagnostics_capacity_decline** — A sustained decline in diagnostic capacity through 2024–25 that pushes a growing share of test requests into backlog.
