"""Deterministic checks on captured evidence, not a claim of employer verification."""

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from nxs.schemas.models import AuditEvidence, AuditReport, PlacementState


MAX_AUDIT_RETRIES = 1


def run_auditor(state: PlacementState) -> AuditReport:
    count = state.get("audit_retry_count", 0)
    if type(count) is not int or count < 0:
        raise ValueError("audit_retry_count must be a non-negative integer")
    job = state["job_listing"]
    profile = state.get("candidate_profile")
    analysis = state.get("analyst_report")
    evidence = []

    def flag(code, severity, field, detail):
        evidence.append(AuditEvidence(code=code, severity=severity, field=field, detail=detail))

    identity_ok = bool(job.company.strip() and job.role.strip())
    if not identity_ok:
        flag("MISSING_IDENTITY", "blocking", "company/role", "Company or role is empty in the captured record.")
    try:
        url = urlsplit(job.url)
        url_ok = (url.scheme in {"http", "https"} and bool(url.hostname)
                  and not re.search(r"\s", job.url) and not url.username and not url.password)
        url.port  # Validate malformed port numbers as well.
    except ValueError:
        url_ok = False
    if not url_ok:
        flag("INVALID_URL", "blocking", "url", "A usable HTTP(S) listing URL is missing.")

    soup = BeautifulSoup(job.raw_html or "", "html.parser")
    for element in soup.select("script, style, nav, aside, footer, template"):
        element.decompose()
    # Prefer a job-specific description element; avoid inspecting unrelated cards.
    section = soup.select_one(".jobs-description, .show-more-less-html__markup, [itemprop='description']")
    # Keep paragraph boundaries while joining inline markup within a sentence.
    root = section if section is not None else soup
    for element in root.select("p, div, h1, h2, h3, li, br"):
        element.insert_before("\n")
        element.insert_after("\n")
    text = root.get_text(" ", strip=False)
    text = re.sub(r"[^\S\n]+", " ", text).strip()
    if section is None:
        anchor = re.search(r"about the job", text, re.IGNORECASE)
        if anchor:
            text = text[anchor.end():].lstrip(" :\n")
        text = re.split(r"\b(?:similar jobs|recommended jobs|people also viewed)\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
    text = text[:10000]
    description_ok = bool(re.search(r"[A-Za-z]{3,}", text))
    if not description_ok:
        flag("MISSING_DESCRIPTION", "blocking", "raw_html", "No readable captured description is available.")

    # Only standalone status statements are blocking, not arbitrary substring hits.
    closed = re.search(
        r"(?:^|[.!?]\s+|\n\s*)(?:this (?:job|role|position) (?:is|has been) closed|"
        r"applications (?:are|have) closed|no longer accepting applications)\s*(?:[.!?]|$|\n)",
        text, re.IGNORECASE,
    )
    if closed:
        flag("CLOSED_LISTING", "blocking", "raw_html", "Captured status statement: " + closed.group(0).strip())
    pool = re.search(r"\b(?:join our talent pool|future opportunities|not an active vacancy)\b", text, re.IGNORECASE)
    if pool:
        flag("TALENT_POOL_WORDING", "warning", "raw_html", "Review possible speculative recruitment wording: " + pool.group(0))

    profile_ok = profile is not None
    if not profile_ok:
        flag("MISSING_PROFILE", "blocking", "candidate_profile", "Scout did not supply a candidate profile.")
    analysis_ok = analysis is not None
    if analysis is None:
        flag("MISSING_ANALYSIS", "blocking", "analyst_report", "Analyst did not supply a report.")
    elif profile is not None:
        skills = {value.strip().casefold() for value in job.tech_stack if value.strip()}
        candidate = {value.strip().casefold() for value in profile.skills if value.strip()}
        matches = {value.strip().casefold() for value in analysis.skill_matches}
        gaps = {value.strip().casefold() for value in analysis.skill_gaps}
        if matches != skills & candidate or gaps != skills - candidate:
            analysis_ok = False
            flag("INCONSISTENT_ANALYSIS", "blocking", "analyst_report", "Analyst skill matches/gaps disagree with the current listing and profile.")
        elif gaps:
            flag("SKILL_GAPS", "warning", "analyst_report.skill_gaps", "Extracted mentions absent from the profile: " + ", ".join(sorted(gaps)) + ". These are not proven mandatory requirements.")
    if not job.salary:
        flag("SALARY_UNKNOWN", "warning", "salary", "The listing salary is unknown; no pay judgement was made.")
    if not job.tech_stack:
        flag("TECHNOLOGIES_UNKNOWN", "warning", "tech_stack", "No technologies were extracted; this does not establish a good match.")
    if analysis and (analysis.market.salary_benchmark.evidence_status == "unknown" or analysis.market.tech_demand.evidence_status == "unknown"):
        flag("MARKET_EVIDENCE_INCOMPLETE", "warning", "analyst_report.market", "At least one market finding is unknown; do not infer missing evidence.")

    blocking = any(item.severity == "blocking" for item in evidence)
    decision = "reject" if closed else "retry" if blocking and count < MAX_AUDIT_RETRIES else "reject" if blocking else "pass"
    if blocking and not closed and count >= MAX_AUDIT_RETRIES:
        flag("RETRY_EXHAUSTED", "blocking", "audit_retry_count", "The single permitted reparse retry is exhausted; processing stops.")
    return AuditReport(
        red_flags=[item.code for item in evidence], evidence=evidence,
        decision=decision, retry_required=decision == "retry",
        confidence_score=sum(map(bool, (identity_ok, url_ok, description_ok, profile_ok, analysis_ok))) / 5,
        notes="Confidence measures input completeness only, not job authenticity. "
              "Captured evidence cannot prove a ghost job. Retry reparses the same snapshot; no live refetch occurs.",
    )
