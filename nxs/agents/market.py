"""Free public UK market statistics, with explicit population and period labels."""

from __future__ import annotations

from datetime import date, datetime, timezone
from functools import lru_cache
import re
from urllib.parse import quote

from bs4 import BeautifulSoup
import requests

from nxs.schemas.analysis import Citation, MarketAnalysis, MarketFinding, SearchSource
from nxs.schemas.models import JobListing


# Only public category names are transmitted, never raw listings or profiles.
TECHNOLOGIES = {
    name.casefold(): name for name in (
        "Python", "SQL", "Java", "JavaScript", "TypeScript", "React", "C++", "C#",
        "AWS", "Azure", "Docker", "Kubernetes", "Linux", "Git", "PostgreSQL",
        "MongoDB", "Rust", "Scala", "Spark", "TensorFlow", "PyTorch",
    )
}
ROLE_CATEGORIES = (
    "Software Engineer", "Software Developer", "Data Scientist", "Data Engineer",
    "Data Analyst", "DevOps Engineer", "Machine Learning Engineer",
)


def role_category(role: str) -> str | None:
    for category in ROLE_CATEGORIES:
        if re.search(rf"\b{re.escape(category)}\b", role, re.IGNORECASE):
            return f"Graduate {category}" if re.search(r"\bgraduate\b", role, re.IGNORECASE) else category
    return None


def _number(value: str, *, money: bool = False) -> float | None:
    if value in {"-", "", "N/A"}:
        return None
    pattern = r"£[\d,]+(?:\.\d+)?" if money else r"[\d,]+(?:\.\d+)?%?"
    if not re.fullmatch(pattern, value):
        raise ValueError("Unexpected market statistic format")
    return float(value.replace("£", "").replace(",", "").replace("%", ""))


def parse_table(html: str, category: str, today: date) -> tuple[dict, str]:
    """Read only the category's first summary table; never nearby category totals."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.summary")
    if table is None:
        raise ValueError("Market summary table missing")
    headers = [cell.get_text(" ", strip=True) for cell in table.select("tr:first-child th")]
    match = re.fullmatch(r"6 months to (\d{1,2} [A-Za-z]{3} \d{4})", headers[1] if len(headers) > 1 else "")
    if not match:
        raise ValueError("Market reporting period missing")
    period = datetime.strptime(match[1], "%d %b %Y").date()
    if not 0 <= (today - period).days <= 31:
        raise ValueError("Market data is stale or future-dated")
    if len(headers) < 3 or headers[2] != f"Same period {period.year - 1}":
        raise ValueError("Prior-year period does not match")
    rows = {}
    for row in table.select("tr"):
        cells = [re.sub(r"(\d)\s+(th|st|nd|rd)\b", r"\1\2", cell.get_text(" ", strip=True))
                 for cell in row.find_all("td", recursive=False)]
        if len(cells) >= 3:
            rows[cells[0]] = cells[1:]
    expected = [f"Permanent jobs citing {category}", f"Permanent jobs requiring a {category}", f"Permanent jobs requiring an {category}"]
    count_label = next((label for label in expected if label in rows), None)
    if count_label is None:
        raise ValueError("Page does not describe the requested category")
    counts = rows[count_label]
    share = rows.get("As % of all permanent jobs in the UK", ["-", "-"])
    median_label = "Median annual salary (50th Percentile)"
    metrics = {
        "category": category, "period_end": period.isoformat(),
        "jobs": _number(counts[0]), "previous_jobs": _number(counts[1]),
        "share": _number(share[0]), "previous_share": _number(share[1]),
        "salary_samples": _number(rows.get("Number of salaries quoted", ["-"])[0]),
        "median": _number(rows.get(median_label, ["-"])[0], money=True),
    }
    if any(metrics[key] is not None and not 0 <= metrics[key] <= 100 for key in ("share", "previous_share")):
        raise ValueError("Invalid market share")
    if any(metrics[key] is not None and not metrics[key].is_integer() for key in ("jobs", "previous_jobs", "salary_samples")):
        raise ValueError("Invalid sample count")
    # Preserve the actual source table text as the citation evidence.
    evidence = " | ".join(table.stripped_strings)
    return metrics, evidence


@lru_cache(maxsize=128)
def _fetch(category: str, day: str) -> tuple[dict, str, str, datetime]:
    """Cache successful public pages per UTC day within the engine process."""
    url = f"https://www.itjobswatch.co.uk/jobs/uk/{quote(category.lower(), safe='')}.do"
    response = requests.get(url, timeout=15, headers={"User-Agent": "NXS-Core/1.0 (public market statistics)"}, allow_redirects=False)
    if response.status_code != 200:
        raise ValueError("Public market page unavailable")
    metrics, evidence = parse_table(response.content, category, date.fromisoformat(day))
    return metrics, evidence, url, datetime.now(timezone.utc)


def collect_public_market(job: JobListing) -> tuple[MarketAnalysis, list[SearchSource], list[str]]:
    """Fetch one role benchmark and up to five distinct technology categories."""
    sources, limitations, salaries, demand, salary_citations, demand_citations = [], [], [], [], [], []
    role = role_category(job.role)
    categories = [("salary", role)] if role else []
    if not role:
        limitations.append("No supported public role category matches this listing.")
    skills = list(dict.fromkeys(skill.strip().casefold() for skill in job.tech_stack))
    supported = [TECHNOLOGIES[skill] for skill in skills if skill in TECHNOLOGIES]
    unsupported = [skill for skill in skills if skill not in TECHNOLOGIES]
    if unsupported:
        limitations.append("No configured public statistics category for: " + ", ".join(unsupported))
    if len(supported) > 5:
        limitations.append("Public research is bounded to the first five supported technologies.")
    categories.extend(("demand", skill) for skill in supported[:5])
    day = datetime.now(timezone.utc).date().isoformat()
    for topic, category in categories:
        try:
            metrics, evidence, url, retrieved_at = _fetch(category, day)
        except (requests.RequestException, ValueError, IndexError):
            limitations.append(f"Public statistics unavailable or invalid for {category}; no values inferred.")
            continue
        source_id = f"public-{topic}-{len(sources) + 1}"
        source = SearchSource(source_id=source_id, topic=topic, url=url,
                              title=f"IT Jobs Watch: {category}, UK", snippet=evidence,
                              query=f"Public statistics category: {category}", retrieved_at=retrieved_at)
        sources.append(source)
        citation = Citation(source_id=source_id, quote=evidence)
        scope = f"{category}, UK permanent advertised jobs, six months to {metrics['period_end']}"
        if topic == "salary":
            if metrics["median"] is not None and (metrics["salary_samples"] or 0) >= 5:
                salaries.append(f"{scope}: median annual advertised salary GBP {metrics['median']:,.0f}, "
                                f"based on {metrics['salary_samples']:,.0f} quoted salaries. "
                                "This category benchmark is context, not a validated internship or individual offer valuation.")
                salary_citations.append(citation)
            else:
                limitations.append(f"{category}: fewer than five salary observations or median unavailable.")
        elif metrics["jobs"] is not None:
            line = f"{scope}: {metrics['jobs']:,.0f} adverts."
            if metrics["previous_jobs"] is not None:
                line += f" Same period last year: {metrics['previous_jobs']:,.0f}."
                if metrics["previous_jobs"] > 0:
                    change = (metrics["jobs"] / metrics["previous_jobs"] - 1) * 100
                    line += f" Advert count change: {change:+.1f}%."
            if metrics["share"] is not None:
                line += f" Share of UK permanent adverts: {metrics['share']:g}%."
            if metrics["share"] is not None and metrics["previous_share"] is not None:
                line += f" Share change: {metrics['share'] - metrics['previous_share']:+.3f} percentage points."
            demand.append(line)
            demand_citations.append(citation)
    limitations.append("Public statistics describe this publisher's advertised-job sample, not all UK hiring. "
                       "Technology counts overlap and are not summed. They cover all seniority levels; placement-specific demand is unknown.")
    if re.search(r"\b(intern|internship|placement)\b", job.role, re.IGNORECASE):
        limitations.append("No placement-specific salary benchmark was established; the role median must not be compared directly with placement pay.")
    salary = MarketFinding(evidence_status="supported" if salaries else "unknown",
                          summary=" ".join(salaries) or "No current role salary benchmark with a sufficient sample was available.", citations=salary_citations)
    tech = MarketFinding(evidence_status="supported" if demand else "unknown",
                        summary=" ".join(demand) or "No current vacancy statistics were available for the extracted technologies.", citations=demand_citations)
    return MarketAnalysis(salary_benchmark=salary, tech_demand=tech), sources, limitations
