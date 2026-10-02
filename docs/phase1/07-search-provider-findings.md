# Search-provider findings (2026-09-30)

## Tavily: fails the contract
**First probe** (`probe_out/20260930T153504Z`, run by you; 5 generic queries, basic, `include_domains=["linkedin.com"]`, $0.04):
- Our request sent `site:linkedin.com/jobs/view/ <role>` verbatim (adapter unit test confirms the body). Tavily's response echoed the query **without the operator** (e.g. `"software engineer"`). Tavily's docs do not mention `site:` or other operators; the operator was dropped server-side and the restriction fell back to `linkedin.com` as a whole.
- 50 results → **1** individual posting URL; the rest were hiring guides, posts, PDFs and `/jobs/<keyword>-jobs` listing pages. That posting had a 158-char snippet, **no raw content**, no date text.

**Comparison** (`scripts/compare_tavily.py`, `probe_out/compare/20260930T153829Z`; your roles × cities, no `site:`; one guard capped at $0.10; spent **$0.096**, 12 credits):

| Variant | Results | Posting URLs | Usable description | Posting-date evidence |
|---|---|---|---|---|
| A basic, `include_domains=["linkedin.com/jobs/view"]` | 0 | 0 | 0 | — |
| B basic, path + `country=india` | 0 | 0 | 0 | — |
| C advanced, path + `country=india`, `chunks_per_source=3` | 1 | 1 | 1 (5.9k chars raw) | none admissible |

Queries: `Platform Engineer Bengaluru`, `DevOps Engineer Hyderabad`, `Cloud Infrastructure Engineer Visakhapatnam`.
- The path in `include_domains` **is honoured** (only job-view URLs came back), but Tavily's index holds very few such pages: 1 result from 9 requests.
- The one posting: "Azure DevOps Engineer (Only Locals)(14+ years of exp)", header says *Plano, TX*, body says *Hyderabad*; the only time text is **"1 year ago"** (relative, and Tavily reports no retrieval time → `unknown_date`; in any case far outside any lookback). Raw content contains LinkedIn sign-in boilerplate before the description.
- Scanner bug found and fixed: the probe's date pattern lacked the `year`/`second` units, so "1 year ago" was not counted in the first report. Re-scored from saved files: no admissible posting-time evidence in either run.

**Totals: 14 Tavily requests, $0.136 committed, 2 posting URLs, 1 usable description, 0 postings with admissible posting-time evidence.**

## Candidates (docs checked 2026-09-30; none tried; each needs your key)

| Provider | LinkedIn targeting | Descriptions | Posting-time evidence | Cost / billing |
|---|---|---|---|---|
| **SerpApi – Google Search** | `site:` / `inurl:` operators documented as supported | Google snippets only (short) | Snippet text only; any "N days ago" is relative to Google's crawl, not to our request → `unknown_date` under the current rule | 250 free searches/month; $25 for 1,000. Docs: "Cached, errored, and failed searches are not" counted |
| **SerpApi – Google Jobs** | Not a LinkedIn filter: jobs with an `apply_options` link to LinkedIn (`via`) | `description` field (usually full text) | `detected_extensions.posted_at` (e.g. "3 days ago"), which is **Google Jobs' own claim**, rendered at request time; `search_metadata.created_at/processed_at` gives the request time | same as above |
| **Brave Search API** | `site:` documented as supported | `description` + optional `extra_snippets` | `age`/`page_age` are provider dates (never evidence); snippet text rarely has a posting time | $5 per 1,000 requests with $5 free credit/month; failed-request billing not documented |

**The date rule matters most.** No candidate returns LinkedIn's own posting time with a retrieval time we can trust. Only Google Jobs returns a posting time, and it is Google's aggregated value, not text from the LinkedIn page. Accepting it would change the contract rule "provider dates never admit a job", so it needs your decision; it is not adopted.

## SerpApi probe (prepared, not yet run)
`scripts/compare_serpapi.py` runs one Google Search (`site:linkedin.com/jobs/view "<role>" (Bengaluru OR Hyderabad OR Visakhapatnam)`, `gl=in`) and one Google Jobs search (`<role> India`, `gl=in`) per desired role in `job-profile.md`: 16 searches for 8 roles, interleaved. It keeps only individual LinkedIn posting URLs (Google Jobs: only jobs with a LinkedIn `/jobs/view` apply link) and records date information in four separate fields: `aggregator_posted_at` (Google Jobs `posted_at` + raw `extensions` + SerpApi request time, status `aggregator_reported_unverified`), `search_engine_date` (Google result `date`), `returned_text_mentions` (raw phrases from snippet/description), and `verified_source_date` (absolute date-time with offset only). Output: `probe_out/serpapi/<timestamp>/comparison.json`. The production date gate is unchanged.

## SerpApi run 20260930T154858Z (4 of 16 searches; stopped by the $0.10 per-run ceiling)
- **Google Search with `site:linkedin.com/jobs/view`**: 2 searches → **20/20 results are individual posting URLs** (India, relevant titles). Snippets are 125–165 chars (no usable description). Posting-time: no phrases in snippets; Google's own `date` on 2/20 ("29 Aug 2026", "5 days ago") — search-engine dates, not evidence.
- **Google Jobs**: 2 searches, `status: Success`, `google_jobs_url …&udm=8`, and the response carried SerpApi's "hasn't returned any results" error (the only error the adapter passes through). `kept 0, dropped 0` means there was no `jobs_results` array at all (a job without a LinkedIn link would have counted as dropped), so this was a genuinely empty response, not a parser miss. The exact error text and raw responses were not saved in that run; the script now saves redacted raw responses and the error text.

## Archive check and Google Jobs with explicit location (2026-09-30)
- **Archive retrieval is free (measured):** SerpApi's archive docs do not state credit use, so usage was read through the free Account API before and after: `this_month_usage` 4 → 4, `total_searches_left` 246 → 246 across 4 archive retrievals. Redacted archives: `probe_out/serpapi/20260930T154858Z/archive/`.
- **First Google Jobs searches were genuinely empty:** `search_information.jobs_results_state: "Fully empty"`, error "Google hasn't returned any results for this query.", no `jobs_results` key (queries `Platform Engineer India` / `DevOps Engineer India`, `gl=in`, no `location`).
- **With `location="Bengaluru,Karnataka,India"` and the bare role** (2 searches, `probe_out/serpapi/20260930T155427Z`, $0.05 at the assumed $0.025/search; 6 of 250 free searches used this month): 20 jobs, of which **9 have a LinkedIn `/jobs/view` apply link**; all 9 have usable descriptions (1.5k–8.2k chars).
- **Parser gap found and fixed:** SerpApi's docs show `detected_extensions.posted_at`, but these responses had no `detected_extensions`; the time was only in `extensions` (e.g. `["1 day ago", "Full–time"]`). Now read from there and labelled `source: google_jobs.extensions`. Re-parsed offline: **8/9 have an aggregator-reported time** (1–10 days ago). **0 verified source dates**; descriptions contain no date phrases.
- **The aggregator time and description often describe a different listing than the LinkedIn URL:** only 4/9 are `via: LinkedIn`. The others come via eBay careers, Circana careers, SimplyHired, Foundit and Join, with LinkedIn as one of several apply links. Two concrete problems:
  - *eBay "Sr Platform Engineer, Java Fmwk"*: "10 days ago", but the LinkedIn id 4300659931 is ~170M lower than the fresh ids (~447xxxxxxx), so the LinkedIn posting is likely much older than the time shown.
  - *Circana "AI Ops Engineer"*: the LinkedIn apply link is `ai-ops-engineer-at-skit-ai-4344432903`, a different company's posting.
  So an aggregator time is at best a claim about the listing Google chose (`via`), not about the LinkedIn posting, unless `via` is LinkedIn.
