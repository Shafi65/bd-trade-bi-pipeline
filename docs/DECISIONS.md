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

## Why the pipeline is shaped the way it is

The whole dataset is a few hundred megabytes, so the infrastructure matches the workload: extraction runs once, locally, as a historical backfill, and the cloud's job starts where durability matters — storage, a queryable catalog, SQL, dashboards — all serverless and effectively free at this scale. Nothing ships to the cloud until the reconciliation gate above says the data deserves it.
