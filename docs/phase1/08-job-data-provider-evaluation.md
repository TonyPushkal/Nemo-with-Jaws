# Job-data provider evaluation and integration note (2026-10-01)

Goal: on-demand search for Indian jobs returning **correctly associated source URLs and complete job descriptions**.
Problem with the search collector (doc 07): Google Jobs descriptions often belong to another site's listing, not the LinkedIn URL.
Everything below was read from official pages on 2026-10-01; **no live API call has been made** (no Bright Data key is configured).

## Providers compared

| | Bright Data LinkedIn Jobs | Apify `labrat011/linkedin-jobs-scraper` | Apify `bebity/linkedin-jobs-scraper` |
|---|---|---|---|
| Discovers jobs? | **Yes**: dataset `gd_lpfll7v5hcqtkxl6l` "discover by keyword" (`keyword`, `location`, `country`, `time_range`, …); also collect/discover by URL | Yes (`keywords`/`location`, `searchUrls`) | Yes (`titles`/`locations`, `startUrls`) |
| Full description | Documented field `job_description_formatted` (plus `job_summary`) in the same record | Only with `fetchJobDetails: true` (`description`, `descriptionHtml`) | `description` in default output |
| Search vs detail charged separately? | No: per-record, documented | Actor page: no separate charge for details; per result | Per result; optional paid enrichment add-ons |
| India | `location` + `country: "IN"` documented generically | "India supported" (8 cities for `splitByCity`) | **Not stated** |
| Published price | 5,000 free credits/month (1 credit/record), then $1.50/1,000; failed records not charged | $0.50/1,000 results | from $1.00/1,000 results |
| Free-tier limit | Hard stop when credits exhausted unless funds deposited; card optional | Apify free plan $5/month, hard stop; **actor page says free users: max 25 results/run and no `fetchJobDetails`** (so no full JDs on free) | $5 Apify credit applies; actor-specific free limits unknown |
| Spend control | `limit_per_input`; no per-run $ cap parameter | `maxTotalChargeUsd`, `maxItems` on run API | same |
| Maturity signal | Vendor product, documented API | 137 users, 10 monthly (page) | 40k users, 4.31★, rebuilt Sept 2026 |
| Login/cookies from us | None | None | None |

Sources: brightdata.com/products/web-scraper/linkedin/jobs; docs.brightdata.com (`linkedin-jobs-discover-by-keyword`, `rest-api/scraper/asynchronous-requests`, `products/scrapers/error-codes`, `general/account/billing-and-pricing/free-tier`); apify.com/pricing; the two actor pages; docs.apify.com `act-runs-post`.
Note the Bright Data *marketing* page says it "accepts job URLs"; the docs show keyword discovery exists. Trust the docs, verify live.

## Cost estimates

| Scenario | Bright Data | Apify labrat011 | Apify bebity |
|---|---|---|---|
| 50 test jobs | **Published:** 50 credits = $0 inside free 5,000; $0.075 PAYG. | Published: $0.025, but free plan caps at 25/run and no details (**blocks the goal on free**) | $0.05 published |
| 3,000 jobs/month | $0 inside free 5,000 credits; $4.50 PAYG | $1.50 (needs paid plan for details, unverified) | $3.00 |

Assumptions / unknowns (not verified): whether discovery records over `limit_per_input` or duplicates are billed; whether Bright Data bills a discovery "search" separately (docs say per record only); Apify compute/platform fees on top of per-result events; Bright Data free credits are shared across its products, so other usage reduces them.

## Recommendation: Bright Data (discover by keyword)
Only option whose **documented free tier includes complete descriptions with India-capable discovery** and a single per-record charge, with failed records free and a hard stop (no overage) when unfunded. Apify labrat011 is cheaper and India-explicit, but its free tier appears unable to fetch details; bebity doesn't state India support. Re-evaluate Apify if Bright Data's real output fails the checks below.

Platform-access caveat unchanged: a third party scrapes LinkedIn on our behalf; whether that suits your risk tolerance/terms is your call (see handoff §3). Our code never contacts LinkedIn.

## What was built (branch `prototype`)
| File | Role |
|---|---|
| `src/nemo/jobsource.py` | `CollectRequest`, `JobRecord`, `JobProvider` protocol (launch / status / fetch / normalize) |
| `src/nemo/providers/brightdata.py` | the one adapter (urllib, typed `ProviderError`, key redaction) |
| `src/nemo/collect.py` | `collect()`, `resume()`, `recover_stale()`, `get_run()` |
| `src/nemo/collect_store.py` | `collect_runs`, `collect_jobs`, `collect_run_jobs` in `data/nemo.sqlite` |
| `scripts/collect.py` | CLI: `run`, `get`, `resume`, `recover` |
| `scripts/live_smoke_brightdata.py` | opt-in live test, ≤50 records |
| `tests/test_brightdata.py`, `tests/test_collect.py` | offline, synthetic data |

Reused unchanged: `budget.py`/`ledger.py` (reservation per launch), `providers/base.ProviderError`, `linkedin.py` (id/URL rule), `consistency.py` (URL-slug vs listing check), `envfile.py`, SQLite file and `.gitignore`. The search collector (`scripts/run_search.py`, `discovery.py`, `store.py`) is untouched and still works.

Behaviour: run row written before launch; **launch never retried** (a timeout ⇒ `launch_unknown`, check the dashboard); op id stored separately from our run id; bounded polling with transient-read retries; jobs saved one transaction each; `timed_out`/`interrupted` runs keep the op id and are resumable; `no_results` (provider succeeded, 0 jobs) is distinct from `failed`; dedup key `(source, source_job_id)`; descriptions flagged `missing|short|truncated`; every job `verification: provider_reported`; raw provider record kept per run. Spend: reservation = `max_records × $0.0015` (published worst case) against the existing ceiling; `est_billed_usd` is records × rate, `measured_usd` stays NULL until read from the dashboard.

## Unverified until the first live run
Real record shape (adapter follows the documented fields); snapshot download shape/pagination and behaviour when not ready; whether `limit_per_input` is honoured and what it bills; whether `country: "IN"`/`location: "India"` returns Indian jobs; cancel endpoint (not implemented, abandoned collections are not cancelled); per-record scrape time (we record our own fetch time as `retrieved_at`); the truncation heuristic (ellipsis / <400 chars).
