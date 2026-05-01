# Huntsville Code Enforcement — Public Records Request

> **Purpose.** Close the Madison-side coverage gap on early-distress code
> violations. Birmingham (Jefferson) exposes housing, inoperable-vehicle,
> environmental, and zoning enforcement cases through the public Accela
> portal. Huntsville (Madison) only exposes the formal *Unsafe Buildings*
> list — the softer violations (tall grass, junk vehicles, IPMC,
> environmental) sit behind the Huntsville Connect submission portal with
> no public read.
>
> This request asks Huntsville's Community Development / Code Enforcement
> office for the same field set we already get from Birmingham, on a
> recurring monthly basis, under the Alabama Open Records Act.
>
> **How to use:** copy the body below into a letter or email, fill in
> the `{{placeholders}}`, and send to the addressees listed in the
> "Routing" section. The request is structured around what Huntsville's
> internal CRM (likely Cityworks or Accela) actually stores, so a single
> SQL/export job should satisfy it.

---

## Routing

**Primary recipient (Open Records custodian)**
City Clerk's Office
City of Huntsville
308 Fountain Circle SW
Huntsville, AL 35801
clerk@huntsvilleal.gov

**Cc — Records owner (do the actual export)**
Community Development — Code Enforcement Division
City of Huntsville
320 Fountain Circle, P.O. Box 308
Huntsville, AL 35804
codeenforcement@huntsvilleal.gov

**Statutory authority**
Alabama Code § 36-12-40 (Alabama Open Records Act). Code Enforcement case
records are public records of a public officer in the conduct of public
business and are presumptively open to inspection and copying.

---

## Letter body — copy from here

```
{{Today's date — e.g. April 28, 2026}}

City Clerk's Office
City of Huntsville
308 Fountain Circle SW
Huntsville, AL 35801

cc: Community Development — Code Enforcement Division
    codeenforcement@huntsvilleal.gov

RE: Alabama Open Records Act request — Code Enforcement case data

Dear Records Custodian:

Pursuant to the Alabama Open Records Act, Alabama Code § 36-12-40, I
request copies of the following public records held by the City of
Huntsville Code Enforcement Division (or its parent department,
Community Development).

1. RECORDS REQUESTED

   All non-exempt code-enforcement case records opened, updated, or
   resolved during the time period {{start date — recommend 12 months
   prior to today}} through {{end date — today, with rolling updates}},
   limited to the following case categories (using your office's
   internal classification):

       a. Property Maintenance Code (IPMC) violations — including but
          not limited to roof, siding, structural, sanitation, and
          electrical violations.
       b. Overgrowth / nuisance vegetation — tall grass, weeds, and
          overgrown lots above the municipal-code threshold.
       c. Inoperable / abandoned vehicles on private property.
       d. Junk, debris, refuse, and bulk-trash violations on private
          property.
       e. Zoning enforcement — illegal use, setback violations,
          unpermitted structures.
       f. Environmental / public-nuisance enforcement — including
          stagnant water, animal-keeping violations, and similar.
       g. Demolition / condemnation / unsafe-building cases NOT
          already published in the City's monthly Unsafe Buildings PDF
          report.

2. FIELDS REQUESTED PER RECORD

   For each case responsive to item (1), please provide the following
   fields where they exist in your case-management system:

       - Case number / record ID
       - Date case opened
       - Last activity / status-change date
       - Current case status (e.g. open, violation verified, in
         compliance, closed)
       - Violation category / record type (item 1a–1g above)
       - Violation description (free-text or code-section reference)
       - Property situs address (street, city, ZIP)
       - Parcel identifier / tax-map ID (if linked)
       - Owner of record on file with the city (name)
       - Owner mailing address on file with the city (where it
         differs from situs)
       - Compliance deadline / re-inspection date (where set)
       - Total fees / civil penalties assessed (cumulative, if any)
       - Outstanding fee balance owed (if any)
       - Inspector or case-officer initials / ID

   I am NOT requesting investigative work-product, attorney-client
   communications, photographs, complainant identities, or any other
   fields that would be exempt from disclosure under Alabama law. If
   any responsive field is exempt, please withhold only that field
   and produce the rest.

3. DELIVERY FORMAT

   I respectfully request electronic delivery, in this order of
   preference:

       a. Direct CSV or Excel export from the case-management system
          (Cityworks, Accela, or equivalent), one row per case;
       b. Structured PDF with a consistent column layout, suitable
          for OCR;
       c. Native database export (SQL dump or JSON) if the office's
          standard practice.

   Please email the response to {{your email}} or upload to a
   shared drive of your choosing — I will provide a Dropbox or Google
   Drive folder on request to avoid email-attachment size limits.

4. ONGOING / RECURRING DELIVERY

   In addition to the one-time backfill in item (1), I request that
   the same export be produced on a RECURRING MONTHLY basis going
   forward, covering the prior calendar month, until I withdraw this
   standing request in writing. Recurring delivery on the first
   business day of each month, by email, would be ideal. If your
   office prefers I re-submit the request monthly, I will do so —
   please advise.

5. FEES

   I am willing to pay reasonable, actual costs of duplication and
   staff time as permitted by Alabama Code § 36-12-40 and the City's
   established fee schedule. Before performing any work that would
   incur a fee in excess of {{fee cap — recommend $50 for the initial
   backfill, $25 per recurring monthly}}, please provide a written
   fee estimate and the basis for the calculation, and I will
   respond promptly with authorization to proceed or with a request
   to narrow the scope.

   Please consider whether a fee waiver is appropriate. The data is
   sought for a public-interest purpose: identifying distressed
   properties in Huntsville neighborhoods so that they can be
   acquired and rehabilitated, thereby reducing blight, restoring
   tax base, and returning vacant homes to occupancy. The records
   are not sought for resale; any data acquired will be used in
   internal property-research workflows only.

6. RESPONSE TIMELINE

   The Alabama Open Records Act requires a reasonable response time.
   Please acknowledge receipt of this request within seven (7)
   business days, and provide a substantive response or fee estimate
   within twenty (20) business days. If you anticipate a longer
   timeline, please advise so we can discuss accommodations.

7. CONTACT

   {{Your full name}}
   {{Your business / DBA, if any}}
   {{Your mailing address}}
   {{Your phone}}
   {{Your email}}

If any portion of this request is unclear, please contact me so we
can narrow or clarify the scope rather than denying the request
outright. Thank you for your time and for the work your office does
to keep Huntsville's neighborhoods healthy.

Sincerely,


{{Your signature}}
{{Your printed name}}
{{Date}}
```

---

## Field cross-reference (why we ask for each one)

The fields in section 2 are deliberately mapped to the `NoticeData`
schema and the Birmingham `BirminghamEnforcementRecord` dataclass so the
Huntsville response slots into the existing pipeline without per-record
manual cleanup:

| FOIA field | Pipeline target | Why we need it |
|---|---|---|
| Case number | `case_number` (Notes column in DataSift) | Dedup across recurring monthly pulls |
| Date case opened | `date_added` / `notice_date` | Marketing window — fresh cases get higher priority in the niche-sequential preset |
| Last activity / status-change date | (recency tag) | Identifies cases where the violation has festered ≥ N months → higher-conversion subset |
| Current case status | (filter) | Drop already-resolved cases from outreach |
| Violation category | `notice_subtype` | Tags `housing_enforcement` / `inoperable_vehicle` / `environmental_enforcement` / `zoning_enforcement`, mirroring Birmingham |
| Violation description | `description` (Notes column) | Lets cold-call agent reference the actual issue ("noticed the tall grass on Jefferson…") |
| Property situs address | `address` / `city` / `zip` | Primary key for Madison `search_by_situs_address()` owner enrichment |
| Parcel identifier | `parcel_id` | Skips the address-search step when present — direct tax-roll join |
| Owner of record | `owner_name` / `tax_owner_name` | DataSift "Property Owner First/Last Name" — primary contact for living-owner outreach |
| Owner mailing address | `mailing_*` fields | DataSift mailing block — used when situs ≠ mailing (absentee owner = stronger lead) |
| Compliance deadline | (filter) | Cases past compliance deadline are about to escalate to fines → motivated-seller signal |
| Total fees assessed | `tax_delinquent_amount` | Same slot Birmingham uses (DataSift schema has no separate code-fee column); fires `tax_high_exposure` tag at ≥ $5K |
| Outstanding balance | (overrides total when present) | Real motivation signal — accumulating municipal liens |
| Inspector / case officer | (extra dict) | Optional; not used for outreach but useful for spot-checking data quality |

If Huntsville's system stores any of these under a different label, ask
the records officer which fields exist and adjust the request — the
goal is parity with what Birmingham Accela produces.

---

## Tags fired by the soft-violation feed (once integrated)

These mirror the Birmingham tag-strategy so a single DataSift filter
preset works across both counties:

| Subtype | Tags |
|---|---|
| `housing_enforcement` | `housing_enforcement, early_distress, code_violation, madison, courthouse_data` |
| `inoperable_vehicle` | `inoperable_vehicle, early_distress, code_violation, madison, courthouse_data` |
| `environmental_enforcement` | `environmental_enforcement, early_distress, code_violation, madison, courthouse_data` |
| `zoning_enforcement` | `zoning_enforcement, early_distress, code_violation, madison, courthouse_data` |
| `unsafe_building` (existing Phase 1) | `unsafe_building, demolish, code_violation, madison, courthouse_data` |
| Cases with `tax_delinquent_amount ≥ 5000` | adds `tax_high_exposure` |

`early_distress` is the bottom-of-funnel signal: owner is still in the
property but slipping on maintenance. Reach-out window is months-to-years
before foreclosure. Outreach framing is "rehab/clean-up offer", **not**
the tear-down framing the `demolish` tag uses.

---

## Pipeline integration plan (post-response)

When Huntsville delivers the data, build a `huntsville_code_violations_api.py`
adapter parallel to `birmingham_code_enforcement_api.py`:

1. **Source loader.** Read the monthly CSV/Excel/PDF from a known Dropbox
   folder (e.g. `TN Public Notice/Madison/code_violation_soft/`) — same
   pattern as `dropbox_watcher.py`.
2. **Record dataclass.** `HuntsvilleCodeViolationRecord` with the same
   shape as `BirminghamEnforcementRecord` (case_number, case_opened,
   address, category, notice_subtype, description, status, owner_name,
   owner_address, fee_total, fee_balance).
3. **`fetch_code_violations()`.** Read latest export, filter by
   `min_age_months` (mirror `min_age_years` from Phase 1).
4. **`enrich_with_owner()`.** Already exists on the Madison adapter —
   `madison_property_api.search_by_situs_address()`. Same ~80% hit-rate
   pattern as Phase 3.
5. **`to_notice_data()`.** Sets `notice_type="code_violation"` and the
   matching subtype, fires `early_distress` (or `demolish` for unsafe-
   building escalations).
6. **Wire into `code_violation_pipeline.py`.** Extend `_fetch_madison()`
   to merge soft violations alongside the existing Unsafe Buildings PDF.

Once the adapter exists and the Huntsville export is flowing, the soft-
violation tags should land in DataSift on the same monthly cadence as
the existing Birmingham early-distress feed.
