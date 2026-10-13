## overview

An acute NHS hospital trust: emergency and elective patients flow from a
single arrivals stream through A&E, inpatient wards, outpatient clinics,
diagnostics, cancer and surgical pathways, then post-discharge follow-up —
competing for consultants, beds, theatres and clinic slots under seasonal
demand pressure.

This warehouse is the clinical substrate for the **NHS performance pack**
teaching track. It carries every *ingredient* of a monthly trust performance
pack — the clinical event streams, the state timelines, the reference
catalogues and tariffs — and none of the computed **metrics**: there is no
monthly A&E percentage, no readmission rate, no occupancy census, no income
summary. Those are the reference query's job, and the spec below is what that
query implements.

The exercises themselves — one canonical layered reference query (the monthly
performance pack) and the maintenance exercises against it — live in
[fabulexa_complex_queries](https://github.com/leogodin217/fabulexa_complex_queries),
which also publishes this warehouse as a ready-made DuckDB file so the track
can be run without forge.

The metric catalog descends from the retired
[nhs_sql_practice_data](https://github.com/leogodin217/nhs_sql_practice_data)
practice track, recalibrated to this warehouse. Three of that track's
documented limitations are lifted here and become material:

- **The true 4-hour standard.** The old data had no ED departure event, so
  time-to-assessment stood in as a proxy. This warehouse carries the full ED
  state timeline — arrival-to-departure is directly computable.
- **True bed occupancy.** The old track could only count assignments per bed
  (throughput). Here a stay's start and its spell's discharge instant both
  exist, so a real daily census is computable.
- **Interval-based consultant utilisation.** Allocation is an interval fact
  with a capacity denominator, not an event count.

### NHS metric catalog — definitions and national targets

Monthly, and by specialty where attribution is defined (see the spec below).
Every derivation names only tables and columns in this export.

**Access and performance targets**

1. **A&E 4-hour standard** —
   [target: 78% by March 2026, 95% constitutional](https://www.england.nhs.uk/statistics/statistical-work-areas/ae-waiting-times-and-activity/).
   Per ED attendance (`fact_pathway_state`, `pathway_type = 'ed_attendance'`):
   the clock runs from the `arrived` state's `entered_at` to the `entered_at`
   of `discharged_home` or `admitted` — arrival to *departure*, the real
   standard. `fact_emergency_care.minutes_from_arrival` is the per-milestone
   clock (triage, assessment, diagnosis); its assessment reading is the old
   track's proxy, **not** the standard's endpoint.
2. **Cancer 28-day Faster Diagnosis Standard** —
   [target: 75%, rising to 80% by March 2026](https://www.england.nhs.uk/statistics/statistical-work-areas/cancer-waiting-times/).
   Per cancer pathway (`fact_cancer_pathway`): `cancer_referral` to
   `cancer_diagnosis_confirmed`; `hours_from_referral` carries the clock
   (FDS = 672 hours). The same column reads the two-week-wait first-seen
   standard (336) and the 62-day treatment standard (1488).
3. **Diagnostic 6-week wait (DM01)** —
   [target: ≥99% within 42 days](https://www.england.nhs.uk/statistics/statistical-work-areas/diagnostics-waiting-times-and-activity/monthly-diagnostics-waiting-times-and-activity/).
   Per diagnostic request (`fact_diagnostics`): `diagnostic_ordered` to
   `diagnostic_performed`; `hours_from_order` carries the clock (6 weeks =
   1008). A request ordered but never performed is still waiting — it stays
   in the denominator.

**Quality and outcome measures**

4. **30-day emergency readmission rate** —
   [benchmark: 12–14%](https://digital.nhs.uk/data-and-information/publications/statistical/compendium-emergency-readmissions/current).
   A discharge is a spell's `inpatient_discharged` entry in
   `fact_pathway_state`; a readmission is the same patient's next `admission`
   milestone in `fact_admitted_care` within 30 days. The care system also
   records its own loop — `fact_post_discharge` checks resolve through the
   `post_discharge_followup` episode's `readmitting` state — and the two
   disagree by design (see the ambiguities).
5. **In-hospital mortality** —
   [context for SHMI / HSMR](https://digital.nhs.uk/data-and-information/publications/statistical/shmi).
   A death is a `fact_patient_status` interval entering `deceased`
   (equivalently the spell's `deceased` state). Crude rate = deaths over
   admissions; `comorbidity_count` and `frailty_band` on `dim_patient` are
   the casemix-stratification material the SHMI discussion needs.
6. **Delayed Transfer of Care (DTOC) delay** —
   [discontinued Feb 2020](https://www.england.nhs.uk/statistics/statistical-work-areas/delayed-transfers-of-care/);
   successor: [Discharge Delays](https://www.england.nhs.uk/statistics/statistical-work-areas/discharge-delays/acute-discharge-situation-report/).
   Per spell: the gap from the `medically_fit` state's `entered_at` to the
   `inpatient_discharged` entry. `dtoc_assessment` milestones in
   `fact_admitted_care` mark the formal assessments along the wait.
7. **Friends and Family Test recommendation rate** —
   [national positive rate typically ~85–90%](https://www.england.nhs.uk/fft/friends-and-family-test-data/).
   Outcomes on `fact_fft_response` (`very_good` … `very_poor`,
   `no_response`); invitations on `fact_fft`. Recommend rate =
   (`very_good` + `good`) over the chosen denominator (see the ambiguities).

**Capacity, income and inequality**

8. **Bed occupancy vs the 85% standard** — NHS guidance treats sustained
   occupancy above 85% as a risk marker (infection, cancellations, handover
   delays). True daily census: a stay runs from `fact_ward_stay.assigned_at`
   to its spell's discharge or death instant — reached via `event_id` →
   `fact_admitted_care.episode_id` → that episode's terminal state in
   `fact_pathway_state`. Count stays overlapping each calendar day per ward
   against `dim_ward.total_beds`. `fact_ward_stay` has **no end column of its
   own**; the end is the spell's, and a spell still open at the extract's end
   has none.
9. **Theatre utilisation** — `fact_theatre_session` per theatre per day
   against `dim_theatre.sessions_per_day`.
10. **Casemix income** — `dim_procedure.tariff` × performed procedures:
    `fact_procedure` joined to `fact_surgical_pathway` on `event_id`,
    keeping `milestone = 'surgery_performed'` only (see the traps).
11. **Bed-day cost** — stay-interval days × `dim_ward.cost_per_bed_day`,
    stays bounded as in the census (8).
12. **Inequalities stratification** — `dim_patient.imd_decile` (1 most
    deprived … 10 least), quintile = `ceil(decile / 2.0)`. The decile is
    constant per patient, but read it through the as-of join like any other
    patient attribute — uniformity is what keeps the join honest.

### Performance pack spec — the reference query's shape

One row per **month × specialty × metric**. The pack is a single `WITH`
chain, strictly layered: base extracts → conforming CTEs → one CTE family
per metric — each independently SELECT-able — → spine assembly → final
presentation. The final SELECT's key (month, specialty, metric) is unique,
so set-diff verification against a known-good pack works row-for-row.

**Conventions.**

- **Month.** An instant belongs to the month of its `*_date_key` row on
  `dim_date`. Take the month spine from months that have activity: the
  calendar runs to 2028-12-31 because 877 confirmed bookings open an
  appointment slot after the extract ends — a spine grouped over all of
  `dim_date` manufactures three empty years.
- **Specialty.** A patient-level event is attributed to the main specialty
  of the consultant allocated to the patient at the event's instant
  (`fact_consultant_allocation` interval join → `dim_consultant.
  main_specialty`), `unattributed` when no allocation covers it. Families
  with a native specialty surface (`dim_procedure`, `dim_diagnostic_test`,
  `dim_ward`, `dim_appointment_book` all carry `main_specialty`) may use
  their own; the reference query documents which rule each family uses.
- **Patient attributes as of an event** are the `dim_patient` version whose
  `[valid_from, valid_to)` window contains the event instant — never the
  current (`valid_to IS NULL`) version.
- **Targets** are literals, each defined once in a header contract comment
  with its national definition and source (a later change request converts
  them to a `VALUES` CTE).
- **Denominators** follow the metric definitions above; every exclusion is
  a stated rule, not a join accident.

### Deliberate ambiguities

The spec picks one reading. The alternatives are real, and a reviewer of a
variant query should be able to say which one it implements.

- **Readmission source of truth** — the computed 30-day window over
  discharges and admissions, or the care system's own recorded loop
  (`post_discharge_followup` reaching `readmitting`). In this extract they
  give 315 vs 333: the recorded loop has no 30-day cutoff.
- **FFT denominator** — all recorded outcomes, outcomes excluding
  `no_response`, or all invitations (`fact_fft`). Three defensible rates.
- **Specialty attribution** — allocation-based vs the family's native
  dimension; they disagree whenever a patient's consultant works a
  different specialty than the ward, test or procedure involved.
- **The 4-hour clock's end for admitted patients** — the `admitted` state
  entry (chosen) vs the first `ward_assignment` milestone; decision-to-admit
  vs arrival-on-ward is a real NHS definitional argument.
- **Census instant** — midnight head-count vs bed-days-over-days ratio;
  they differ for same-day stays.
- **Winter** — grouping by calendar year splits each winter across two
  rows; a winter season (Nov–Mar) belongs to the year it starts
  (`CASE WHEN month >= 11 THEN year ELSE year - 1 END`).

### Known traps

- **`dim_patient` is SCD-2 and versions track the upstream record, not
  visible change.** 56,075 version rows over 14,999 patients (up to 27
  versions of one person). `status` is the only projected column that
  changes, and only 30,176 of the 41,076 version transitions change it —
  the other 10,900 adjacent versions are **identical in every visible
  column** (upstream infection-status and survey bookkeeping re-versioned
  the patient). `COUNT(*)` does not count patients; counting versions does
  not count status changes; `LAG` over `status` does.
- **The window is timestamp-grain; the day is not the version.** 12,693
  patient-days carry more than one `dim_patient` version (up to 3 — a
  patient can be `in_ed` and `admitted` within one afternoon), so
  `(patient_id, valid_from_date_key)` is **not** unique. As-of joins
  compare instants — the event timestamp against
  `[valid_from, valid_to)` — and joining or deduplicating at day grain
  through the `*_date_key` columns collapses real same-day versions and
  fans out events. (Unlike the saas-billing warehouse's date-grained
  windows, there are no zero-length versions here: the instants carry the
  order.)
- **The current version is the tape's end, not "now".** Joining facts to
  `valid_to IS NULL` rows reads every patient's final status (mostly
  `discharged` / `recovered` / `active`, 78 `deceased`) into all of
  history. A patient attribute at event time is the
  `[valid_from, valid_to)` as-of join, always.
- **`status = 'active'` means *between episodes*** — the patient is alive
  and under no active care — not "the row is live" and not "currently an
  inpatient".
- **Event facts are milestone-grain.** `COUNT(*)` on `fact_emergency_care`
  counts milestones (33,868), not attendances (8,467): count distinct
  `episode_id`, or filter one milestone per episode.
- **Procedures bind twice.** `fact_procedure` carries one row per binding —
  3,305 = 1,860 planned (`pre_op_assessment`) + 1,445 performed
  (`surgery_performed`). Income over bridge rows without the milestone
  filter double-counts most operations.
- **Interval facts fan out.** `fact_pathway_state` (183,055 rows),
  `fact_consultant_allocation` and `fact_consultant_caseload` are interval
  tables: joining one to an event without the full three-predicate interval
  join (`key match AND event >= entered AND (exited IS NULL OR event <
  exited)`) multiplies rows.
- **Open intervals are `NULL`-ended, and that is information.** A spell
  with no discharge instant is still open at the extract's end. Dropping
  `NULL` ends from the census understates recent months; readmission and
  FDS/DM01 clocks near the extract edge are right-censored — the pack must
  state its cutoff.
- **`fact_consultant_caseload.caseload` is text.** The history grain
  delivers the value as `VARCHAR`; cast before aggregating.
- **No episode references another episode.** An ED attendance that converts
  and the spell it becomes share only the patient and the clock; so do a
  discharge and its follow-up episode. Linking them is a fuzzy temporal
  join (same patient, admission close to the ED `admitted` instant) — the
  same technique real NHS data linkage uses. This is by construction in
  the emit, deliberately preserved: no foreign key was withheld, and none
  exists to withhold.
- **Bookings and clinic events are different systems that reconcile.**
  `fact_booking` spans all three book types; its `outpatient_clinic` slice
  (4,085 attended / 955 no-show) matches `fact_outpatient`'s
  `appointment_attended` / `appointment_dna` exactly. Comparing whole-table
  counts across the two "systems" is a scope error, not a data gap.
