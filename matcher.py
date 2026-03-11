"""
matcher.py — Fuzzy-matches a LinkedIn connection's company to companies in the sheet.

LinkedIn headlines look like:
  "Software Engineer at Stripe"
  "Senior PM @ Google DeepMind"
  "Product Designer | Figma"

We extract the company portion and compare it against all companies in the sheet
using token_set_ratio (handles word-order differences and partial matches well).
"""

import re
import logging
from rapidfuzz import fuzz, process
from config import FUZZY_THRESHOLD

logger = logging.getLogger(__name__)

# Patterns to extract "Company" from a LinkedIn headline.
# Two sub-patterns:
#   1. "at " — word separator, MUST have whitespace after (avoids matching "database")
#   2. @  –  —  |  -  — symbol separators, space is OPTIONAL (handles "@Company" no-space)
_AT_PATTERN = re.compile(
    r'(?:at\s+|[@–—|]\s*|(?<!\w)-\s+)(.+?)(?:\s*[,|]|\s*$)',
    re.IGNORECASE
)


def extract_company_from_headline(headline: str) -> str:
    """
    Best-effort extraction of company name from a LinkedIn headline string.
    Returns the raw extracted string, or the full headline if extraction fails.
    """
    if not headline:
        return ""
    m = _AT_PATTERN.search(headline)
    if m:
        return m.group(1).strip()
    # Fallback: take everything after the last "|" or "–"
    for sep in ["|", "–", "—", "-"]:
        if sep in headline:
            return headline.split(sep)[-1].strip()
    return headline.strip()


def find_matching_row(
    connection_company: str,
    sheet_rows: list[dict],
) -> dict | None:
    """
    Given a company name from a LinkedIn connection's headline and a list of
    sheet row dicts (each having a 'company' key), returns the best-matching
    row above FUZZY_THRESHOLD, or None.

    sheet_rows items must have at minimum: row_index, company, role.
    """
    if not connection_company or not sheet_rows:
        return None

    conn_lower = connection_company.strip().lower()

    # Fast path: case-insensitive exact match (e.g. "Havi" vs "HAVI")
    for row in sheet_rows:
        if row["company"].strip().lower() == conn_lower:
            logger.info("Matched '%s' → '%s' (exact)", connection_company, row["company"])
            return row

    # Build lookup: normalised company name → row
    choices = {row["company"]: row for row in sheet_rows}

    result = process.extractOne(
        connection_company,
        choices.keys(),
        scorer=fuzz.token_set_ratio,
    )

    if result is None:
        return None

    best_match, score, _ = result
    if score >= FUZZY_THRESHOLD:
        logger.info(
            "Matched '%s' → '%s' (score=%d)", connection_company, best_match, score
        )
        return choices[best_match]

    logger.debug(
        "No match for '%s' (best: '%s', score=%d < threshold=%d)",
        connection_company, best_match, score, FUZZY_THRESHOLD,
    )
    return None
