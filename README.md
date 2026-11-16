# NXS Core

A deterministic decision pipeline that scores UK tech placement listings. Capture a job on LinkedIn and, about five seconds later, it has a **Nexus Score** (300–850), a **Proceed, Observe or Discard** verdict and an itemised explanation on a live dashboard.

NXS Core is the engine behind [Placement Tracker](https://github.com/sami-ahmed23/placement-tracker), which captures listings through a Chrome extension and displays the results. The two share a Supabase backend.

## How it works

```
Chrome extension → /api/ingest → job_listings (Supabase)
                                      │  polled every 3 s
                                      ▼
   Scout → Analyst → Auditor ──reject──► end
     ▲                 │
     └──retry once─────┤
                       ▼ pass
                    Assessor → Executive → verdict written back to job_listings
```

Five stages run as a [LangGraph](https://github.com/langchain-ai/langgraph) state machine. Each logs to `agent_logs`, which the dashboard streams.

| Stage | What it does |
|---|---|
| **Scout** | Extracts technologies, salary and seniority from the captured page. The role title decides seniority; the description counts only when the title is silent. |
| **Analyst** | Compares the technologies with the candidate profile and retrieves cited UK salary and vacancy statistics from free public tables. |
| **Auditor** | Checks the capture is usable and flags closed or speculative listings. Incomplete input gets one retry; closed listings are rejected. |
| **Assessor** | Calculates the Nexus Score. |
| **Executive** | Decides the verdict, assembles the reasoning and writes the result back to the listing. |

There are no model calls. Every stage is deterministic Python, and every output is a Pydantic model whose validators reject inconsistent results, such as a verdict that disagrees with its own score.

## The scoring model

Points are awarded above a floor of 300:

| Component | Points | Measures |
|---|---|---|
| Skill coverage | 200 | Share of the listing's technologies that appear in the profile |
| Input completeness | 150 | How much of the listing was captured usably |
| Seniority fit | 100 | Whether the listing's level is within reach |
| Salary alignment | 100 | The listing's stated salary against the candidate's target |

**Missing evidence lowers the ceiling instead of being guessed.** If a listing states no salary, salary alignment awards nothing *and* the maximum achievable score drops by 100. Missing weight is never redistributed, so a listing that could not be read cannot score as though it were a perfect match.

## The verdict rule

| Verdict | Condition |
|---|---|
| **Proceed** | Score of 700 or above |
| **Discard** | Score below 600, **and** a ceiling of 600 or above |
| **Observe** | Everything else |

A listing is only discarded when it *could* have cleared the bar and didn't. A low score caused by missing evidence comes out as Observe, so a parsing failure can't throw away a real opportunity. A seniority level beyond the candidate's reach forces Discard whatever the score, because skill overlap doesn't make a role applicable.

## Outputs

- **Dashboard:** score, verdict and per-agent reasoning on each Placement Tracker card.
- **Listing brief:** `python -m nxs.editorial <job_id>` renders one scored listing as Substack-ready Markdown. It's a fit judgement against one profile, so it's for personal use.
- **Weekly market brief:** `python -m nxs.weekly --week-ending YYYY-MM-DD` aggregates a week of captured listings into profile-independent facts (requested skills, seniority mix, salary transparency, listing quality and cited demand) for publication in [The Metric](https://themetric.substack.com). It contains no scores or verdicts, and shares appear only from five listings upwards.

Briefs are written to `briefs/`, which is gitignored.

## Setup

Requires Python 3.12.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env    # then add your Supabase URL and service-role key
```

`NXS_MARKET_SEARCH=public` (the default) uses free [IT Jobs Watch](https://www.itjobswatch.co.uk/) tables, so no paid API is needed. Set it to `off` for fully offline runs.

The candidate profile is in `nxs/profile.py`.

## Running

```powershell
.venv\Scripts\python main.py
```

The engine processes listings captured after it starts. Restart it after changing code.

## Tests

```powershell
.venv\Scripts\python -m pytest
```

124 tests, runnable offline. Use pytest rather than unittest: `tests/conftest.py` stops any test writing a verdict to the live database, and only pytest loads it.

## Project layout

```
nxs/
  agents/      scout, analyst, market, auditor, assessor, executive
  schemas/     Pydantic contracts for every stage's output
  graph/       the LangGraph state machine
  db/          Supabase client, polling listener, result write-back
  editorial.py listing brief
  weekly.py    weekly market brief
tests/         one file per stage, plus briefs
```

## Limitations

- Technologies come from a fixed list of 65, so unlisted tools are not counted.
- Weights are reasoned, not fitted to application outcomes.
- Public statistics describe a sample of UK permanent-job adverts, not placement pay or all hiring.
- Listings captured while the engine is off are not processed, and a listing the Auditor rejects is not written back.
- Extraction depends on LinkedIn's page layout, which changes without notice.

## Licence

MIT
