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


# Ladder from most junior to most senior. Extraction reports the highest level stated.
SENIORITY_LADDER: list[str] = [
    "intern", "placement", "graduate", "junior", "mid", "senior", "lead", "principal",
]

# Wording that identifies a level. Written to avoid partial hits such as "internal".
_SENIORITY_PATTERNS: list[tuple[str, str]] = [
    ("intern", r"\b(?:intern|internship|summer intern)\b"),
    ("placement", r"\b(?:placement|industrial placement|year in industry|sandwich (?:year|placement))\b"),
    ("graduate", r"\b(?:graduate (?:scheme|programme|role|developer|engineer|analyst)|new grad|entry[- ]level)\b"),
    ("junior", r"\b(?:junior|associate)\b"),
    ("mid", r"\b(?:mid[- ]level|intermediate)\b"),
    ("senior", r"\bsenior\b"),
    ("lead", r"\b(?:lead|staff)\b"),
    ("principal", r"\b(?:principal|head of|director)\b"),
]

# A stated minimum of years is a seniority signal in its own right.
_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*\d{0,2}\s*years?[^.]{0,30}?experience", re.IGNORECASE)


def _level_from_years(text: str) -> str | None:
    """Map a stated minimum number of years to the lowest level that could satisfy it."""
    matches = [int(match.group(1)) for match in _YEARS_RE.finditer(text)]
    if not matches:
        return None
    years = min(matches)  # The lowest stated minimum is the easiest bar to clear.
    if years <= 1:
        return "junior"
    return "mid" if years <= 4 else "senior"


# Descriptions name colleagues ("work alongside senior engineers"), so only programme
# wording is trusted there. Senior, lead and principal count only in the role title.
_DESCRIPTION_LEVELS = {"intern", "placement", "graduate"}


def _levels_in(text: str, allowed: set[str] | None = None) -> list[str]:
    return [level for level, pattern in _SENIORITY_PATTERNS
            if (allowed is None or level in allowed) and re.search(pattern, text, re.IGNORECASE)]


def _extract_seniority(title: str, description: str = "") -> str | None:
    """Return the level the listing is pitched at, or None when it does not say.

    The role title is authoritative. The description is consulted only when the title is
    silent, and then only for programme wording and a stated minimum of years. A missed
    level lowers the score ceiling; a wrong one would force a Discard, so missing is safer.
    """
    found = _levels_in(title)
    if not found:
        found = _levels_in(description, _DESCRIPTION_LEVELS)
        years_level = _level_from_years(f"{title} {description}")
        if years_level:
            found.append(years_level)
    if not found:
        return None
    # Within the trusted text, the highest stated level sets the bar a candidate must meet.
    return max(found, key=SENIORITY_LADDER.index)


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
    seniority = _extract_seniority(job.role, plain)

    log.info(f"Scout extracted – tech_stack: {tech_stack}, salary: {salary}, seniority: {seniority}")

    return {
        "company": job.company,
        "role": job.role,
        "tech_stack": tech_stack,
        "salary": salary,
        "seniority": seniority,
    }
