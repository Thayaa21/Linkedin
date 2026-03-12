"""
drive.py — Fetches the shareable link for {Company}_Resume.pdf from Google Drive.

Resumes must be named exactly:  Thayaa_{Company}.pdf
e.g.  Thayaa_Stripe.pdf, Thayaa_Google.pdf, Thayaa_Acme Corp.pdf

The Drive folder is set via DRIVE_FOLDER_ID in .env.
Files must have "Anyone with the link can view" sharing already set —
this script does NOT change sharing permissions.
"""

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


def _normalize(name: str) -> str:
    """Strips extra whitespace and lowercases for comparison."""
    return " ".join(name.lower().split())


def _normalize_for_match(s: str) -> tuple[str, str]:
    """Returns (spaced form, no-space form) for flexible matching."""
    spaced = " ".join(s.lower().split())
    nospace = spaced.replace(" ", "")
    return spaced, nospace


def get_resume_link(company: str) -> str | None:
    """
    Returns the webViewLink for Thayaa_{Company}.pdf, or None if not found.
    Matching is flexible: case-insensitive, ignores extra spaces, and matches
    "Newt Global" to "Thayaa_Newt Global.pdf" or "Thayaa_NewtGlobal.pdf".
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
    company_spaced, company_nospace = _normalize_for_match(company)

    for f in files:
        name = f["name"].replace(".pdf", "").replace(".PDF", "")
        if not name.lower().startswith("thayaa_"):
            continue
        file_company = name[7:].strip()  # after "thayaa_"
        file_spaced, file_nospace = _normalize_for_match(file_company)
        if (company_spaced == file_spaced or company_nospace == file_nospace or
            company_spaced == file_nospace or company_nospace == file_spaced):
            link = f.get("webViewLink")
            logger.info("Found resume for %s: %s", company, link)
            return link

    logger.warning("No resume found for company: %s (searched %d files)", company, len(files))
    return None
