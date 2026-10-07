# nxs-core
Placement intelligence pipeline built with LangGraph and Pydantic.

Python 3.12 is required. Install `requirements.txt` in a virtual environment.
Copy `.env.example` to `.env` only on a new installation; preserve existing secrets.
Configure the Supabase URL and service-role key, then run:

```powershell
.venv\Scripts\python.exe main.py
```

Analyst defaults to `NXS_MARKET_SEARCH=public`: deterministic skill matching plus
free [IT Jobs Watch](https://www.itjobswatch.co.uk/) UK salary and vacancy tables.
No OpenAI key, Serper allowance or paid model credit is needed. Existing Supabase
plan limits still apply. Set `NXS_MARKET_SEARCH=off` for entirely offline research.

Public research reads one recognised role category and up to five recognised
technologies, caching successful pages per UTC day within the running process.
It retains source links, table text, retrieval times, reporting periods and salary
sample sizes. Vacancy-count changes and market-share changes are reported
separately. Unsupported categories, stale data, changed tables and unavailable
pages produce explicit unknowns. Salary medians require at least five observations.

The figures describe the publisher's UK permanent-job advertisement sample.
Role medians are contextual benchmarks, not internship pay estimates. Graduate
categories are selected only for explicitly graduate roles. Technology demand
covers all seniority levels, with overlapping categories that must not be summed.

Optional `NXS_MARKET_SEARCH=serper` uses `SERPER_API_KEY` and two searches per
listing. Serper's advertised 2,500 free queries are a finite allowance, not an
unlimited free service. Do not purchase credits for zero-cost operation; return
to local mode when the allowance ends. Search failure leaves findings unknown.
Retrieved technology mentions are not treated as measured hiring demand.

Run offline tests with:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Scout and the public-source Analyst are implemented and tested. The later
agents and final result writes remain incomplete. Activity and Analyst reports
are saved to `agent_logs`; no final score or verdict is produced yet.
