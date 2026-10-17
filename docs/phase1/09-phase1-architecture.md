# Phase 1 architecture (short version, for review)

Status: **proposal; "Proposed" parts are not built.** Updated 2026-10-02. Older docs [02](02-scope.md) and [03](03-architecture.md) describe an earlier, stricter design (verified posting dates, Tavily first); this page replaces them where they differ. Background: [07 provider findings](07-search-provider-findings.md), [06 local model](06-local-model-findings.md).

## 1. Scope

Phase 1 is **one service and one SQLite database.** You give it search inputs. It finds jobs, screens and ranks them, and saves each job's description and application links. You read a ranked list.

A useful result: top jobs fit your target roles, each keeps its description and links, and exclusions and unsearched queries are visible.

Out of scope: résumé tailoring (Phase 2), applying, other providers, fallback. Only SerpApi is used. Phase 2 will read only what Phase 1 saves: description, links, provenance and the match assessment.

## 2. What exists today

`scripts/run_search.py` is a hand-run, synchronous script.

1. It builds a search plan from your roles, locations and `config/search_terms.json` (`src/nemo/queryplan.py`).
2. It sends them to **SerpApi** (Google Jobs and Google Search). Tavily is used only in probe scripts.
3. It keeps every Google Jobs result, with or without a LinkedIn link, removes duplicates, and saves them (`src/nemo/discovery.py`, `store.py`, `data/nemo.sqlite`, `runs/<id>/`).

**Where data comes from:** title, company, location and description come from *Google's listing*, not from LinkedIn, and each job records which site supplied the description. No code fetches a LinkedIn page. Dates are Google's claims. All application options are kept; an employer/ATS link is preferred when established; job boards are labelled as such and uncertain links as unknown. LinkedIn links are consistency-checked against the listing, not verified; a clearly mismatched one is rejected and the job is kept without it. Jobs with no application link and no Google job ID are skipped and counted.

**Gaps:** screening is a separate script (below), not part of the run; a crashed run loses work.

**Known limitation (discovery coverage):** Nemo finds useful jobs but can miss suitable postings even when they are publicly accessible. Known miss: Safe Security, "Software Development Engineer I", Bengaluru (LinkedIn 4473364096; the posting and Safe's official Lever listing were both open when checked). It is absent from every saved response, including a full-title search on pages 1 and 2 and company and ID controls. The cause is unresolved (page depth, ranking or source coverage), and a miss does not show the posting is absent from Google Jobs. Paid diagnostics aimed at it are stopped. Treat results as best-effort, not complete.

## 3. Proposed flow

```mermaid
flowchart LR
  U([You]) -->|search inputs| API["Run API<br/>(proposed)"]
  subgraph SVC [One service]
    API --> Q["Narrow role queries<br/>(exists, to be improved)"]
    Q --> P["SerpApi search<br/>(exists)"]
    P --> N["Normalize + dedupe<br/>(exists)"]
    N --> R["Rules: screen + rank<br/>(proposed)"]
    R -.->|only if evaluation shows benefit| L["Local LLM<br/>(optional, later)"]
    R --> S["Save + retrieve<br/>(partly exists)"]
    L --> S
  end
  P -.-> E[(SerpApi / Google Jobs)]
  S --> D[("SQLite<br/>data/nemo.sqlite")]
  S -->|ranked jobs| U
  classDef exists fill:#e6f4ea,stroke:#2e7d32,color:#000;
  classDef proposed fill:#fff4e5,stroke:#ef6c00,stroke-dasharray:4 3,color:#000;
  class Q,P,N,E,D exists;
  class API,R,L,S proposed;
```
Green exists; orange dashed is proposed.

**Queries (exists):** SDE and backend engineering only. `config/search_terms.json` has two families (SDE / software engineer; backend) with natural aliases ("SDE I", "Software Development Engineer I", "Python Backend Engineer"), a few junior or "0-2 years" variants, and plain-role searches. DevOps, SRE, platform, cloud, QA/testing and dedicated AI/ML roles are no longer searched. Cities: Bengaluru and Hyderabad. `--offset N` runs the next batch, `pages: 2` in a query file follows a second page (each page is one request), and `--dry-run` shows the order, request cap and which searches fit.

**Screening (exists as `scripts/screen_results.py`, rules only):**
- **Experience:** excluded when the *required* minimum is above 2 years; a range counts by its minimum (2-4 borderline, 3-5 excluded); exactly 2 is borderline; gaps are shown against your 14 full-time and 21 total months. Years come from applicant sentences only. Preferred years never exclude, an unstated requirement stays eligible and flagged, and an alternative route ("fresh graduates … encouraged") or a mixed-level title ("SDE I, II, III") goes to review.
- **Role, from responsibilities as well as the title:** backend work (APIs, microservices, databases) is prioritised; software roles that use cloud or AI products stay in. Primarily DevOps, SRE, infrastructure-operations, QA and testing roles are excluded from the default shortlist (kept with the reason); mixed or unclear ones are flagged for review.
- **Labels:** each job gets a discipline, a match level (strong / possible / adjacent / weak: a title alone is never strong), a required-stack fit shown separately from role fit (Python-compatible or language-flexible roles rank first; other stacks stay visible as gaps and are never strong), the employment type as Google reports it (or "not stated"), and a description check from content (responsibilities and requirements present; a heuristic, not proof of completeness; partial or thin descriptions are never strong). Fit and confidence are separate, and unknowns are not penalised. Links are checked for structure only; reported age is shown, never used to exclude. The local LLM is added only if evaluation (§6) shows the rules miss something it catches.

**Duplicates:** postings are merged only when they share a posting identifier (the same application URL including its job-id parameter, or a LinkedIn job id). Same company, title and city is shown as a possible duplicate and never merged.

**Visibility (a goal, not a guarantee):** excluded and skipped jobs are counted, excluded jobs keep their reason, and each run reports searches completed versus planned. Coverage stays partial.

## 4. Collection contract and service (exists)

**Input:** target titles (default: the profile's desired roles), chosen cities, the job profile file (experience, skills, preferences; server-side), **maximum posting age (default 24 hours)** and a **request budget** (default 12). The existing dollar guard stays a safety net; its per-search price is an assumption, not verified billing.

**Behaviour:** broad searches (plain roles plus a few junior variants); result pages are followed, breadth-first, until the request budget, the end of results, or a repeated page, and the run reports **why it stopped**; jobs with unstated experience are kept and seniority is assessed afterwards; the actual location is screened against the chosen cities (other cities excluded with a reason, a missing city flagged); each job is bucketed as **confirmed within the max age** (Google's reported age, unverified), **age unconfirmed** (no age, or too coarse such as "1 day ago") or **older** (excluded from the fresh shortlist, record kept). Source, age evidence and all application links are retained.

**Service (`scripts/serve.py`, standard library, localhost, one run at a time):** `POST /runs` {titles?, cities?, max_age_hours?, request_budget?} returns a run ID; `GET /runs/<id>` returns state, stop reason, requests used, freshness counts and the screened jobs. Optional bearer token; the profile is never part of a request.

**What the source cannot guarantee:** a 24-hour window (Google Jobs' own date filter offers Yesterday / Last 3 days / Last week / Last month, applied through a Google-issued token, and its listings carry only relative ages such as "3 hours ago", missing for about a third of jobs); exhaustive results (10 per page, ranking changes between runs); or LinkedIn coverage (pages are Google Jobs result pages aggregating many sources; LinkedIn appears only as one possible apply link and is never fetched).

## 5. Minimum database

| Table | Holds | Status |
|---|---|---|
| `runs`, `run_queries` | run status, each search and outcome | exists; add `running` state and crash recovery |
| `jobs` | one row per job. Key: LinkedIn job ID when a consistency-checked link exists, else a hash of the application URL (job-id query parameters kept, known tracking parameters removed), else Google's job ID; never title alone. Description, its source, links, first/last seen | exists |
| `job_links` | every application link, labelled | proposed (today stored inside the job record) |
| `run_jobs` | jobs found per run, raw listing | exists |
| `assessments` | per run and job: fit, confidence, reasons, exclusions, rule version | proposed |

A repeat sighting updates the one `jobs` row and adds a `run_jobs` row, so history is kept. Jobs are never merged by title alone.

## 6. Security and evaluation

- Keys only in `.env`/environment, never in git, logs or the database; `.env` and the database readable only by you; localhost only; bound query parameters; logs hold IDs and counts, not descriptions. Descriptions are untrusted text for any future LLM. Back up by copying the database file.
- **Evaluation:** [`runs/review-batch.md`](../../runs/review-batch.md) (local, gitignored) holds 10 deduplicated saved jobs. You label each relevant / maybe / not relevant. We then check whether the rules keep your "relevant" jobs, rank them high, and exclude none by mistake.

## 7. Next steps

1. Run the four-search benchmark and review the returned jobs.
2. Add `assessments`, run state and recovery.
3. Expose the two-call service.
4. One end-to-end run, reviewed together.

## 8. Needed from you

1. **Target roles and relevant experience** (to narrow queries and define fit).
2. **Acceptable locations and work arrangements, and genuine dealbreakers.**
