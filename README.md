# nxs-core
Placement intelligence pipeline built with LangGraph and Pydantic.

Python 3.12 is required. Install `requirements.txt` in a virtual environment.
Copy `.env.example` to `.env` only on a new installation; preserve existing secrets.
Configure the Supabase URL and service-role key, then run:

```powershell
.venv\Scripts\python.exe main.py
```

Analyst defaults to `NXS_MARKET_SEARCH=off`: local skill matching, no model or
search API calls, and explicit unknowns for market salary and demand. Existing
Supabase plan limits still apply. No OpenAI key or paid model credit is needed.

Optional `NXS_MARKET_SEARCH=serper` uses `SERPER_API_KEY` and two searches per
listing. Serper's advertised 2,500 free queries are a finite allowance, not an
unlimited free service. Do not purchase credits for zero-cost operation; return
to local mode when the allowance ends. Search failure leaves findings unknown.
Retrieved technology mentions are not treated as measured hiring demand.

Run offline tests with:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Scout and local Analyst are implemented. Full market benchmarking, the later
agents and final result writes remain incomplete. Activity and Analyst reports
are saved to `agent_logs`; no final score or verdict is produced yet.
