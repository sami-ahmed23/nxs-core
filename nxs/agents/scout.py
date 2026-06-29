from __future__ import annotations

import re

from nxs.logger import get_logger
from nxs.schemas.models import JobListing

log = get_logger("Scout")

# Known technologies to scan for in job posting text.
_TECH_KEYWORDS: list[str] = [
    "Python", "SQL", "Java", "Scala", "C++", "C#", "Golang", "Rust", "Julia",
    "JavaScript", "TypeScript", "MATLAB",
    "PyTorch", "TensorFlow", "Keras", "scikit-learn", "XGBoost", "LightGBM",
    "pandas", "NumPy", "SciPy", "Matplotlib", "Seaborn", "Plotly",
    "Spark", "Hadoop", "Kafka", "Airflow", "dbt", "Databricks",
    "AWS", "GCP", "Azure", "Docker", "Kubernetes",
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "Snowflake", "BigQuery",
    "Git", "GitHub", "GitLab", "Linux", "Bash",
    "React", "Next.js", "Node.js", "FastAPI", "Flask", "Django",
    "LangChain", "LangGraph", "CrewAI", "Pydantic", "Supabase",
    "Tableau", "Power BI", "Looker", "Excel",
    "Stan", "JAGS", "MLflow", "Weights & Biases",
]

# Salary patterns: £30,000 | $120k | £25,000 – £35,000 | $80,000-$100,000
# Requires at least 4 digits to avoid false positives like £0 or £1.
_SALARY_RE = re.compile(
    r"[£$€]\s?\d{1,3}(?:,\d{3})+(?:\s?[kK])?"
    r"(?:\s?[-–—to]+\s?[£$€]?\s?\d{1,3}(?:,\d{3})*(?:\s?[kK])?)?"
    r"|[£$€]\s?\d{2,3}[kK]",
    re.IGNORECASE,
)


def _extract_tech_stack(text: str) -> list[str]:
    """Case-sensitive keyword scan against a known tech list."""
    found = []
    for tech in _TECH_KEYWORDS:
        # Word-boundary match to avoid partial hits (e.g. "R" inside "Radar").
        pattern = rf"\b{re.escape(tech)}\b"
        if re.search(pattern, text, re.IGNORECASE):
            found.append(tech)
    return found


def _extract_salary(text: str) -> str | None:
    """Regex scan for salary/compensation figures."""
    match = _SALARY_RE.search(text)
    return match.group(0).strip() if match else None


async def run_scout(job: JobListing) -> dict:
    """
    Parses raw_html via keyword and regex extraction to enrich JobListing fields.
    Deterministic, zero-hallucination – aligned with NXS Protocol.
    """
    plain = re.sub(r"<[^>]+>", " ", job.raw_html or "")
    plain = re.sub(r"\s+", " ", plain).strip()

    # Anchor to "About the job" – the LinkedIn section that contains the actual
    # job description. Take 10k chars from there to avoid sidebar noise.
    anchor = plain.find("About the job")
    if anchor != -1:
        plain = plain[anchor:anchor + 10000]
    else:
        # Fallback: use the company name as the anchor.
        anchor = plain.find(job.company)
        plain = plain[anchor:anchor + 10000] if anchor != -1 else plain[:10000]

    log.info(f"Scanning {len(plain)} chars of text for {job.company} – {job.role}")

    tech_stack = _extract_tech_stack(plain)
    salary = _extract_salary(plain)

    log.info(f"Scout extracted – tech_stack: {tech_stack}, salary: {salary}")

    return {
        "company": job.company,
        "role": job.role,
        "tech_stack": tech_stack,
        "salary": salary,
    }
