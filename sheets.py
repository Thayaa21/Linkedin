"""
sheets.py — Google Sheets read/write via gspread (service account auth).

Tracker layout (simplified):
  A: Applied Date  B: Company  C: Role  D: Job URL  E: Status

Sent Messages layout (people we've messaged):
  A: Name  B: Company  C: LinkedIn ID  D: Job URL  E: Role  F: Status
"""

import re
import gspread
from google.oauth2.service_account import Credentials
from config import SHEET_ID, SHEET_TAB, SENT_TAB, GOOGLE_CREDS_FILE

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Tracker column indices (0-based) — 5 columns only
COL_APPLIED_DATE = 0
COL_COMPANY     = 1
COL_ROLE        = 2
COL_URL         = 3
COL_STATUS      = 4

STATUS_APPLIED          = "Applied"
STATUS_PENDING          = "Pending Message"
STATUS_SENT             = "Message Sent"
STATUS_NO_RESUME        = "No Resume"
STATUS_ALREADY_MESSAGED = "Already Messaged"

TRACKER_HEADERS = ["Applied Date", "Company", "Role", "Job URL", "Status"]

# Sent sheet columns
SENT_COL_NAME    = 0
SENT_COL_COMPANY = 1
SENT_COL_LI_URL  = 2
SENT_COL_JOB_URL = 3
SENT_COL_ROLE    = 4
SENT_COL_STATUS = 5

SENT_HEADERS = ["Name", "Company", "LinkedIn ID", "Job URL", "Role", "Status"]


def _client():
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def _worksheet():
    return _client().open_by_key(SHEET_ID).worksheet(SHEET_TAB)


def _to_applied_date(ts: str) -> str:
    """Extract YYYY-MM-DD from timestamp like 2026-03-10T07:..."""
    if not ts:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", ts.strip())
    return m.group(1) if m else ts[:10] if len(ts) >= 10 else ts


def ensure_sent_sheet_exists():
    """Create Sent Messages sheet with headers if it doesn't exist."""
    spreadsheet = _client().open_by_key(SHEET_ID)
    try:
        ws = spreadsheet.worksheet(SENT_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SENT_TAB, rows=5000, cols=6)
    rows = ws.get_all_values()
    if not rows:
        ws.update("A1:F1", [SENT_HEADERS])


def _sent_worksheet():
    return _client().open_by_key(SHEET_ID).worksheet(SENT_TAB)


def refine_tracker_sheet():
    """
    Refines the tracker: timestamp→date, removes Source/Name/LI/Resume, 5 cols only.
    Migrates any Pending/Sent rows with person data to Sent sheet first.
    """
    ws = _worksheet()
    rows = ws.get_all_values()
    if len(rows) < 2:
        return

    # Migrate person data to Sent sheet before simplifying
    ensure_sent_sheet_exists()
    sent_ws = _sent_worksheet()
    sent_rows = sent_ws.get_all_values()
    sent_li_urls = {r[SENT_COL_LI_URL].strip() for r in sent_rows[1:] if len(r) > SENT_COL_LI_URL and r[SENT_COL_LI_URL].strip()}

    def _person_col(row: list, name_idx: int, li_idx: int) -> tuple[str, str]:
        """Return (li_name, li_url) from row. Legacy: Name=6, LI=7. New 8-col: Name=5, LI=6."""
        n = row[name_idx].strip() if len(row) > name_idx else ""
        u = row[li_idx].strip() if len(row) > li_idx else ""
        return n, u

    new_rows = [TRACKER_HEADERS]
    for row in rows[1:]:
        if len(row) < 5:
            continue
        is_legacy = len(row) >= 9
        applied_date = _to_applied_date(row[0]) if row else ""
        company = row[1] if len(row) > 1 else ""
        role = row[2] if len(row) > 2 else ""
        job_url = row[3] if len(row) > 3 else ""
        status = row[4] if len(row) > 4 else ""

        # Migrate to Sent if has person data (legacy: Name=6 LI=7, 8-col: Name=5 LI=6)
        name_idx, li_idx = (6, 7) if is_legacy else (5, 6)
        li_name, li_url = _person_col(row, name_idx, li_idx)
        if status in (STATUS_PENDING, STATUS_SENT) and li_url and li_url not in sent_li_urls:
            sent_ws.append_row([
                li_name, company, li_url, job_url, role,
                STATUS_SENT if status == STATUS_SENT else STATUS_PENDING,
            ])
            sent_li_urls.add(li_url)

        new_rows.append([applied_date, company, role, job_url, status])

    ws.clear()
    ws.update(f"A1:E{len(new_rows)}", new_rows)


# ─── Read ─────────────────────────────────────────────────────────────────────

def get_applied_companies() -> list[dict]:
    """Returns tracker rows with Status == 'Applied'."""
    ws = _worksheet()
    records = ws.get_all_values()
    results = []
    for i, row in enumerate(records[1:], start=2):
        if len(row) < 5:
            continue
        if row[COL_STATUS].strip() != STATUS_APPLIED:
            continue
        results.append({
            "row_index":   i,
            "company":     row[COL_COMPANY].strip(),
            "role":        row[COL_ROLE].strip(),
            "url":         row[COL_URL].strip() if len(row) > COL_URL else "",
            "timestamp":   _to_applied_date(row[COL_APPLIED_DATE].strip() if len(row) > COL_APPLIED_DATE else ""),
        })
    return results


def get_all_jobs() -> list[dict]:
    """Returns ALL tracker rows that have a job URL (for multi-referral matching)."""
    ws = _worksheet()
    records = ws.get_all_values()
    results = []
    for i, row in enumerate(records[1:], start=2):
        if len(row) < 5:
            continue
        job_url = row[COL_URL].strip() if len(row) > COL_URL else ""
        if not job_url:
            continue
        results.append({
            "row_index":   i,
            "company":     row[COL_COMPANY].strip(),
            "role":        row[COL_ROLE].strip(),
            "url":         job_url,
            "status":      row[COL_STATUS].strip(),
            "timestamp":   _to_applied_date(row[COL_APPLIED_DATE].strip() if len(row) > COL_APPLIED_DATE else ""),
        })
    return results


def get_tracked_li_urls() -> set[str]:
    """Returns all LinkedIn URLs in Sent sheet (Pending + Sent)."""
    try:
        ws = _sent_worksheet()
    except gspread.exceptions.WorksheetNotFound:
        return set()
    records = ws.get_all_values()
    urls = set()
    for row in records[1:]:
        if len(row) > SENT_COL_LI_URL and row[SENT_COL_LI_URL].strip():
            urls.add(row[SENT_COL_LI_URL].strip())
    return urls


def add_pending_to_sent_sheet(
    li_name: str,
    li_url: str,
    company: str,
    role: str,
    job_url: str,
):
    """Add a row to Sent sheet with Status=Pending (when we match a connection)."""
    ensure_sent_sheet_exists()
    ws = _sent_worksheet()
    rows = ws.get_all_values()
    if not rows:
        ws.update("A1:F1", [SENT_HEADERS])
        rows = [SENT_HEADERS]
    for row in rows[1:]:
        if len(row) > SENT_COL_LI_URL and row[SENT_COL_LI_URL].strip() == li_url.strip() and (row[SENT_COL_COMPANY].strip() if len(row) > SENT_COL_COMPANY else "") == company.strip():
            return  # already recorded
    ws.append_row([li_name or "", company or "", li_url or "", job_url or "", role or "", STATUS_PENDING])


def get_pending_rows() -> list[dict]:
    """Returns rows from Sent sheet with Status == 'Pending Message'."""
    try:
        ws = _sent_worksheet()
    except gspread.exceptions.WorksheetNotFound:
        return []
    records = ws.get_all_values()
    results = []
    for i, row in enumerate(records[1:], start=2):
        if len(row) <= SENT_COL_STATUS:
            continue
        if row[SENT_COL_STATUS].strip() != STATUS_PENDING:
            continue
        results.append({
            "row_index":   i,
            "company":     row[SENT_COL_COMPANY].strip() if len(row) > SENT_COL_COMPANY else "",
            "role":        row[SENT_COL_ROLE].strip() if len(row) > SENT_COL_ROLE else "",
            "li_name":     row[SENT_COL_NAME].strip() if len(row) > SENT_COL_NAME else "",
            "li_url":      row[SENT_COL_LI_URL].strip() if len(row) > SENT_COL_LI_URL else "",
            "job_url":     row[SENT_COL_JOB_URL].strip() if len(row) > SENT_COL_JOB_URL else "",
        })
    return results


def mark_sent_in_sent_sheet(row_index: int):
    """Update Sent sheet row to Status=Message Sent."""
    ws = _sent_worksheet()
    ws.update_cell(row_index, SENT_COL_STATUS + 1, STATUS_SENT)


def mark_no_resume_in_sent_sheet(row_index: int):
    """Update Sent sheet row to Status=No Resume (skip sending)."""
    ws = _sent_worksheet()
    ws.update_cell(row_index, SENT_COL_STATUS + 1, STATUS_NO_RESUME)


# ─── Snapshot ─────────────────────────────────────────────────────────────────
# Columns: A=Profile URL  B=Name  C=Headline  D=Company

SNAPSHOT_TAB = "Snapshot"
SNAPSHOT_HEADERS = ["Profile URL", "Name", "Headline", "Company"]


def load_snapshot_from_sheet() -> dict[str, dict]:
    """Load connections snapshot. Supports both old (JSON) and new (columns) format."""
    try:
        ws = _client().open_by_key(SHEET_ID).worksheet(SNAPSHOT_TAB)
        rows = ws.get_all_values()
        result = {}
        for row in rows:
            if not row or not row[0].strip():
                continue
            url = row[0].strip()
            if len(row) >= 4:
                result[url] = {
                    "url": url,
                    "name": row[1].strip() if len(row) > 1 else "",
                    "headline": row[2].strip() if len(row) > 2 else "",
                    "current_company": row[3].strip() if len(row) > 3 else "",
                }
            else:
                import json as _json
                try:
                    data = _json.loads(row[1]) if len(row) > 1 and row[1] else {}
                except Exception:
                    data = {}
                data.setdefault("url", url)
                result[url] = data
        return result
    except gspread.exceptions.WorksheetNotFound:
        return {}


def save_snapshot_to_sheet(connections: dict[str, dict]):
    """Persist snapshot with clean columns: URL, Name, Headline, Company."""
    spreadsheet = _client().open_by_key(SHEET_ID)
    try:
        ws = spreadsheet.worksheet(SNAPSHOT_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SNAPSHOT_TAB, rows=5000, cols=4)

    ws.clear()
    if connections:
        rows = [[SNAPSHOT_HEADERS[0], SNAPSHOT_HEADERS[1], SNAPSHOT_HEADERS[2], SNAPSHOT_HEADERS[3]]]
        for url, d in connections.items():
            rows.append([
                url,
                d.get("name", ""),
                d.get("headline", ""),
                d.get("current_company", ""),
            ])
        ws.update(f"A1:D{len(rows)}", rows)
