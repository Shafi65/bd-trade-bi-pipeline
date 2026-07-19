# Why This Project Looks The Way It Does

A short walkthrough of the reasoning behind the major decisions. New sections are added as decisions are actually made.

## The question we're answering

A trade expert posed it plainly: are Bangladesh's exports earning better prices, and where are they eroding? Behind that sit five questions — price trends per product, the biggest improvers, whether erosion concentrates in specific destinations, what's diversifying the export basket, and which new products could break into new markets. Everything below follows from needing **price, per product, per destination, over time**.

## Why this data

Answering at product level requires 8-digit HS-code detail by destination country. Only Bangladesh's own statistics bureau (BBS) publishes that grain — the cleaner international source (UN Comtrade's API) stops at 6 digits for Bangladesh and literally cannot answer the question. So we accepted five years of inconsistent Excel and thousand-page PDFs over a clean API, because the right grain beats the easy format.

## How we deal with the mess

Five fiscal years arrive in three different packagings, but underneath they contain the same tables. We normalize everything into one canonical shape — product × destination × half-year × quantity × value — and treat the originals as immutable source of truth. Trust is earned, not assumed: every extracted year must sum back to the totals BBS itself published (within 0.1%), or it doesn't enter the pipeline. Silent extraction errors are the failure mode; plausible-but-wrong numbers are worse than no numbers.

## Keeping the prices honest

"Price" here is a unit value — value ÷ quantity — and two things can quietly corrupt it. First, values are in taka, and the taka lost roughly 30% against the dollar over the study window: in taka, almost everything looks like a price improvement, so every price is analyzed in both BDT and USD to separate real gains from devaluation. Second, quantities come in different units (kilograms, item counts), so unit prices are only ever compared within the same product and unit — never across.

## Why exports lead and imports follow

The expert's questions are all export-side, so exports are the headline. Import data is still extracted, but as supporting context — chiefly how import-dependent the export machine is (imported inputs relative to export earnings), which frames whether better prices actually translate into value retained.

## When the source contradicts itself

The older PDF years failed the reconciliation gate, and the reasons turned out to be physical defects in the publications themselves: one import volume contains the original print *and* a partially revised re-print of 41 countries (with different numbers); one country's section was interrupted mid-print by its own revision, with the original's tail misplaced 500 pages later; one export volume simply truncates mid-commodity. None of this is guessable — but all of it is decidable, because BBS also publishes the same data at coarser grains (chapter totals, commodity totals, and a country-major mirror table). Every conflict was arbitrated the same way: reconstruct each candidate reading and keep the one that matches the independently published control totals (the winner matched all 97 chapter totals; the loser failed 60). Where a primary table omits detail, it is filled only from BBS's own mirror table, never estimated. The principle: when a source is internally inconsistent, don't average, don't guess, don't drop the year — let the publisher's own control totals pick the truth, and record the surgery explicitly in config so it stays visible.

## Why the pipeline is shaped the way it is

The whole dataset is a few hundred megabytes, so the infrastructure matches the workload: extraction runs once, locally, as a historical backfill, and the cloud's job starts where durability matters — storage, a queryable catalog, SQL, dashboards — all serverless and effectively free at this scale. Nothing ships to the cloud until the reconciliation gate above says the data deserves it.

## From reconciled to modelled: shaping the star schema

Passing reconciliation proves the numbers are right; it does not make them a data model. The published tables are occasionally *finer* than an 8-digit product — a commodity is split into sub-lines, or a section is reprinted — so several source rows can describe the same product-to-destination-in-a-half-year fact. That is legitimate detail, not duplication: each sub-line carries real trade value, and the year still reconciles. So the model **sums** those rows onto one row per fact rather than dropping any — and the safety check is that the per-year totals are identical before and after, to the taka. Deleting a row that looked like a duplicate would have quietly erased real exports; aggregating cannot.

## One product, one code; one country, one code

A dimensional model only works if a key means the same thing everywhere. Two silent inconsistencies threatened that. Product codes were already zero-padded during extraction (so `1012100` and `01012100` are one product). Country codes were not: the spreadsheet year wrote Canada as `16` while the PDF years wrote `016`. Unreconciled, Canada would have appeared as two different destinations and its price history would have split in half exactly at the format boundary — the worst kind of bug, because every total still adds up. The fix is the same principle as the product codes: normalise every numeric country code to a fixed width so the key is canonical. A handful of small territories that the source names but never numbers (e.g. Western Sahara) are given stable surrogate codes, so every fact still points at a short, unique key rather than a free-text name that drifts in spelling year to year.

## Making price comparable across years: the exchange rate is data, not a constant

Because the taka fell by roughly a third over the window, a price quoted only in taka is half devaluation and half signal. Converting to dollars requires an exchange rate *per period*, and the right kind of rate matters: these are flows accumulated over a half-year, so the correct divisor is the **average** rate over that half-year, not the rate on any single day. Those ten rates (one per fiscal half) are treated as reference data — sourced from the central bank, written down explicitly with their provenance, and reviewed as their own decision — rather than hard-coded deep in a script. And the dollar figures themselves are not stored alongside the taka: the fact table stays in the single currency it was measured in, and every derived figure (dollar value, unit price) is computed at query time from the fact and the rate table. That way a single corrected rate fixes every downstream number at once, and there is exactly one definition of each metric rather than a copy frozen into millions of rows.
