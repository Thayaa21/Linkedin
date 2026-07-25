"""
drive.py — Fetches the shareable link for {Company}_Resume.pdf from Google Drive.

Resumes must be named:  Thayaa_{Company}.pdf
e.g.  Thayaa_Stripe.pdf, Thayaa_Google.pdf, Thayaa_Acme Corp.pdf

Matching is simple and strict:
  - Case-insensitive
  - Strips spaces, punctuation, and common suffixes (LLC, Inc, Corp, etc.)
  - Must be an EXACT match after cleaning — no fuzzy, no partial, no token tricks

The Drive folder is set via DRIVE_FOLDER_ID in .env.
Files must have "Anyone with the link can view" sharing already set.
"""

import re
import logging
from functools import lru_cache
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from config import GOOGLE_CREDS_FILE, DRIVE_FOLDER_ID

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


@lru_cache(maxsize=1)
def _drive_service():
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def _clean(name: str) -> str:
    """
    Normalize a company name for exact comparison:
      - lowercase
      - remove common corporate suffixes
      - remove all punctuation and spaces
    """
    s = name.lower().strip()
    # Remove common suffixes
    for suffix in ('incorporated', 'inc', 'llc', 'ltd', 'limited', 'corp',
                   'corporation', 'co', 'company', 'the'):
        s = re.sub(r'\b' + suffix + r'\b', '', s)
    # Remove all non-alphanumeric characters (spaces, dots, hyphens, underscores, etc.)
    s = re.sub(r'[^a-z0-9]', '', s)
    return s


def get_resume_link(company: str) -> str | None:
    """
    Returns the webViewLink for Thayaa_{Company}.pdf, or None if not found.
    Only matches if the cleaned company name is identical to the cleaned filename.
    """
    service = _drive_service()
    query = (
        f"'{DRIVE_FOLDER_ID}' in parents "
        f"and mimeType='application/pdf' "
        f"and trashed=false"
    )
    results = service.files().list(
        q=query,
        fields="files(id, name, webViewLink)",
        pageSize=200,
    ).execute()

    files = results.get("files", [])
    target = _clean(company)

    if not target:
        logger.warning("Empty company name after cleaning: '%s'", company)
        return None

    for f in files:
        fname = f["name"].replace(".pdf", "").replace(".PDF", "")
        if not fname.lower().startswith("thayaa_"):
            continue
        file_company = fname[7:]  # after "Thayaa_"
        if _clean(file_company) == target:
            link = f.get("webViewLink")
            logger.info("Resume found for '%s' → %s", company, f["name"])
            return link

    logger.warning("No resume found for '%s' (searched %d files)", company, len(files))
    return None
