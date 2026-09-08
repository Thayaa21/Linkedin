"""
matcher.py — Extract a connection's current company from their LinkedIn headline
and fuzzy-match it against companies in the Tracker sheet.

Company extraction uses GPT-4o-mini when OPENAI_API_KEY is set (far more reliable
than regex on messy headlines like "Data Scientist | ML & NLP | Open to Work"),
and falls back to a strict regex parser if the API is unavailable or errors.
The fallback returns "" when it can't confidently find an employer — it never
returns the whole headline (that was the old bug that produced garbage matches).
"""

import re
import json
import logging

from rapidfuzz import fuzz, process
from config import FUZZY_THRESHOLD, OPENAI_API_KEY, OPENAI_MODEL

logger = logging.getLogger(__name__)

# Generic words that must NOT drive a company match on their own. "AI" matching
# "Simple AI" is exactly what messaged the wrong people.
_GENERIC_TOKENS = {
    "ai", "ml", "the", "inc", "llc", "ltd", "co", "corp", "corporation", "company",
    "labs", "lab", "technologies", "technology", "tech", "systems", "system",
    "solutions", "solution", "software", "group", "global", "services", "data",
    "digital", "consulting", "studio", "studios", "and", "of",
}


def _tokens(s: str) -> set[str]:
    """Lowercase alphanumeric tokens of a company string."""
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t}


# ─── GPT-4o-mini extraction ───────────────────────────────────────────────────

_openai_client = None
_openai_disabled = False


def _get_openai_client():
    """Lazily build an OpenAI client; return None if unavailable."""
    global _openai_client, _openai_disabled
    if _openai_disabled:
        return None
    if _openai_client is not None:
        return _openai_client
    if not OPENAI_API_KEY:
        _openai_disabled = True
        return None
    try:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
        return _openai_client
    except Exception as e:
        logger.warning("OpenAI client unavailable (%s) — using regex fallback", e)
        _openai_disabled = True
        return None


_SYSTEM_PROMPT = (
    "You extract the person's CURRENT employer (company name only) from a "
    "LinkedIn headline. Rules: return ONLY the company name, nothing else. "
    "If the headline shows a university/school as their current place and no "
    "employer, return the school name. If there is no real current employer or "
    "school (e.g. it only lists skills, titles, or 'Open to Work'), return an "
    "empty string. Do not include role titles, skills, or descriptions. "
    'Respond as JSON: {"company": "<name or empty>"}.'
)


def _extract_company_llm(headline: str) -> str | None:
    """Return company via GPT-4o-mini, or None if the call couldn't run."""
    client = _get_openai_client()
    if client is None:
        return None
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": headline.strip()[:400]},
            ],
            temperature=0,
            max_tokens=30,
            response_format={"type": "json_object"},
        )
        content = (resp.choices[0].message.content or "").strip()
        data = json.loads(content)
        company = (data.get("company") or "").strip()
        return company
    except Exception as e:
        logger.warning("LLM company extraction failed (%s) — regex fallback", e)
        return None


# ─── Regex fallback ────────────────────────────────────────────────────────────

# "at Company" / "@Company" / "Company" after a separator. Whitespace after "at"
# is required (avoids matching inside words like "database").
_AT_PATTERN = re.compile(
    r'(?:\bat\s+|@\s*)([A-Z0-9][^|·•\-–—,\n]{1,60})',
)

# Phrases that clearly are NOT companies (skills / statuses / descriptors).
_NON_COMPANY_HINTS = (
    "open to work", "seeking", "looking for", "aspiring", "enthusiast",
    "ml", "nlp", "ai ", "python", "sql", "developer", "engineer", "scientist",
    "student", "graduate", "grad", "expert", "building", "turning", "passionate",
)


def _extract_company_regex(headline: str) -> str:
    """Strict best-effort. Returns "" if no confident employer is found."""
    if not headline:
        return ""
    m = _AT_PATTERN.search(headline)
    if m:
        candidate = m.group(1).strip().rstrip(".,|-–— ")
        low = candidate.lower()
        # Reject obvious non-company phrases
        if any(h in low for h in _NON_COMPANY_HINTS) and len(candidate.split()) > 3:
            return ""
        return candidate
    return ""


# ─── Public API ────────────────────────────────────────────────────────────────

def extract_company_from_headline(headline: str) -> str:
    """
    Extract the current company from a headline. Prefers GPT-4o-mini; falls back
    to a strict regex. Returns "" when no confident employer is found.
    """
    if not headline or not headline.strip():
        return ""
    llm = _extract_company_llm(headline)
    if llm is not None:          # LLM ran (even if it returned "")
        return llm
    return _extract_company_regex(headline)


def find_matching_row(
    connection_company: str,
    sheet_rows: list[dict],
) -> dict | None:
    """
    Match a connection's company against Tracker rows (each with a 'company' key).
    Returns the best-matching row above FUZZY_THRESHOLD, or None.
    """
    if not connection_company or not connection_company.strip() or not sheet_rows:
        return None

    conn_lower = connection_company.strip().lower()

    # 1) Exact (case-insensitive)
    for row in sheet_rows:
        if row["company"].strip().lower() == conn_lower:
            logger.info("Matched '%s' → '%s' (exact)", connection_company, row["company"])
            return row

    # 2) Full sheet company appears as consecutive whole words in the extracted
    #    text. Skip when the sheet company is only generic tokens (e.g. "AI"),
    #    which would otherwise match almost any AI-flavored headline.
    for row in sheet_rows:
        sheet_company = row["company"].strip()
        if not sheet_company:
            continue
        if _tokens(sheet_company) <= _GENERIC_TOKENS:
            continue
        pattern = r"\b" + re.escape(sheet_company) + r"\b"
        if re.search(pattern, connection_company, re.IGNORECASE):
            logger.info("Matched '%s' → '%s' (contains)", connection_company, sheet_company)
            return row

    # 3) Fuzzy — but guarded against generic single-token false positives.
    #
    #    The classic bug: extracting "AI" from a headline scored token_set_ratio
    #    100 against a sheet company like "Simple AI" (the shared "AI" token is a
    #    subset), so unrelated people were matched. Guards:
    #      - require token_sort_ratio (order-sensitive, not subset-generous),
    #      - require the connection company to share a DISTINCTIVE token with the
    #        sheet company (not just a generic word like "ai"/"the"/"labs").
    conn_tokens = _tokens(connection_company)
    if not conn_tokens or conn_tokens <= _GENERIC_TOKENS:
        # Nothing but generic words ("ai", "inc", ...) — never fuzzy-match on that.
        return None

    choices = {row["company"]: row for row in sheet_rows if row.get("company")}
    if not choices:
        return None

    best_match = None
    best_score = 0.0
    for company in choices:
        sort_score = fuzz.token_sort_ratio(connection_company, company)
        set_score = fuzz.token_set_ratio(connection_company, company)
        # Distinctive (non-generic) token overlap is REQUIRED.
        sheet_tokens = _tokens(company)
        distinctive_overlap = (conn_tokens & sheet_tokens) - _GENERIC_TOKENS
        if not distinctive_overlap:
            continue
        score = min(sort_score, set_score)  # both must be high
        if score > best_score:
            best_score, best_match = score, company

    if best_match and best_score >= FUZZY_THRESHOLD:
        logger.info("Matched '%s' → '%s' (score=%.0f)", connection_company, best_match, best_score)
        return choices[best_match]

    logger.debug(
        "No confident match for '%s' (best '%s' score=%.0f < %d)",
        connection_company, best_match, best_score, FUZZY_THRESHOLD,
    )
    return None
