"""
sheets.py — Google Sheets read/write via gspread (service account auth).

Tracker layout:
  A: Applied Date  B: Company  C: Role  D: Job URL  E: Status  F: LI Name  G: LI URL  H: Resume Link

Status lifecycle:
  Applied  →  Pending Message  →  Message Sent  |  No Resume  |  Already Messaged
"""

import re
import gspread
from google.oauth2.service_account import Credentials
from config import SHEET_ID, SHEET_TAB, SENT_TAB, GOOGLE_CREDS_FILE

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Column indices (0-based) — no Source column
COL_APPLIED_DATE = 0
COL_COMPANY     = 1
COL_ROLE        = 2
COL_URL         = 3
COL_STATUS      = 4
COL_LI_NAME     = 5
COL_LI_URL      = 6
COL_RESUME_LINK = 7

# Legacy: old format had Source at 5, so LI Name=6, LI URL=7, Resume=8
COL_SOURCE_LEGACY = 5  # skip when reading

STATUS_APPLIED          = "Applied"
STATUS_PENDING          = "Pending Message"
STATUS_SENT             = "Message Sent"
STATUS_NO_RESUME        = "No Resume"
STATUS_ALREADY_MESSAGED = "Already Messaged"

TRACKER_HEADERS = ["Applied Date", "Company", "Role", "Job URL", "Status", "Name", "LinkedIn ID", "Resume Link"]


def _client():
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def _worksheet():
    return _client().open_by_key(SHEET_ID).worksheet(SHEET_TAB)


def refine_tracker_sheet():
    """
    Refines the tracker: timestamp→date, removes Source, adds headers.
    Call at poll start — cleans data from extension (timestamp, Source).
    """
    ws = _worksheet()
    rows = ws.get_all_values()
    if len(rows) < 2:
        return

    new_rows = [TRACKER_HEADERS]
    for row in rows[1:]:
        if len(row) < 5:
            continue
        # Legacy (9 cols): A=Timestamp, B=Company, C=Role, D=URL, E=Status, F=Source, G=Name, H=LI URL, I=Resume
        # Already refined (8 cols): A=Date, B=Company, C=Role, D=URL, E=Status, F=Name, G=LI URL, H=Resume
        is_legacy = len(row) >= 9
        applied_date = _to_applied_date(row[0]) if row else ""
        company = row[1] if len(row) > 1 else ""
        role = row[2] if len(row) > 2 else ""
        job_url = row[3] if len(row) > 3 else ""
        status = row[4] if len(row) > 4 else ""
        name = row[6] if is_legacy and len(row) > 6 else (row[5] if len(row) > 5 else "")
        li_url = row[7] if is_legacy and len(row) > 7 else (row[6] if len(row) > 6 else "")
        resume = row[8] if is_legacy and len(row) > 8 else (row[7] if len(row) > 7 else "")
        new_rows.append([applied_date, company, role, job_url, status, name, li_url, resume])

    ws.clear()
    ws.update(f"A1:H{len(new_rows)}", new_rows)


def _to_applied_date(ts: str) -> str:
    """Extract YYYY-MM-DD from timestamp like 2026-03-10T07:..."""
    if not ts:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", ts.strip())
    return m.group(1) if m else ts[:10] if len(ts) >= 10 else ts


def _col(row: list, idx: int, legacy_offset: int = 0) -> str:
    """Get column value; legacy sheets had Source at 5, shifting LI columns by 1."""
    actual = idx + legacy_offset if legacy_offset and idx >= COL_LI_NAME else idx
    return row[actual].strip() if len(row) > actual else ""


def _is_legacy_format(row: list) -> bool:
    """Old format had 9+ columns (with Source)."""
    return len(row) >= 9


# ─── Read ─────────────────────────────────────────────────────────────────────

def get_applied_companies() -> list[dict]:
    """Returns rows with Status == 'Applied'."""
    ws = _worksheet()
    records = ws.get_all_values()
    results = []
    for i, row in enumerate(records[1:], start=2):
        if len(row) < 5:
            continue
        status = row[COL_STATUS].strip()
        if status != STATUS_APPLIED:
            continue
        legacy = 1 if _is_legacy_format(row) else 0
        results.append({
            "row_index":   i,
            "company":     row[COL_COMPANY].strip(),
            "role":        row[COL_ROLE].strip(),
            "url":         row[COL_URL].strip() if len(row) > COL_URL else "",
            "timestamp":   _to_applied_date(row[COL_APPLIED_DATE].strip() if len(row) > COL_APPLIED_DATE else ""),
            "resume_link": _col(row, COL_RESUME_LINK, legacy),
        })
    return results


def get_all_jobs() -> list[dict]:
    """Returns ALL rows that have a job URL (for multi-referral matching)."""
    ws = _worksheet()
    records = ws.get_all_values()
    results = []
    for i, row in enumerate(records[1:], start=2):
        if len(row) < 5:
            continue
        job_url = row[COL_URL].strip() if len(row) > COL_URL else ""
        if not job_url:
            continue
        legacy = 1 if _is_legacy_format(row) else 0
        results.append({
            "row_index":   i,
            "company":     row[COL_COMPANY].strip(),
            "role":        row[COL_ROLE].strip(),
            "url":         job_url,
            "status":      row[COL_STATUS].strip(),
            "timestamp":   _to_applied_date(row[COL_APPLIED_DATE].strip() if len(row) > COL_APPLIED_DATE else ""),
            "li_url":      _col(row, COL_LI_URL, legacy),
            "resume_link": _col(row, COL_RESUME_LINK, legacy),
        })
    return results


def get_tracked_li_urls() -> set[str]:
    """Returns all LinkedIn profile URLs already in the sheet."""
    ws = _worksheet()
    records = ws.get_all_values()
    urls = set()
    for row in records[1:]:
        legacy = 1 if _is_legacy_format(row) else 0
        url = _col(row, COL_LI_URL, legacy)
        if url:
            urls.add(url)
    return urls


def add_pending_row(
    timestamp: str,
    company: str,
    role: str,
    job_url: str,
    source: str,  # kept for API compat, ignored
    li_name: str,
    li_url: str,
):
    """Appends a new Pending Message row."""
    ws = _worksheet()
    ws.append_row([
        _to_applied_date(timestamp),
        company,
        role,
        job_url,
        STATUS_PENDING,
        li_name,
        li_url,
        "",
    ])


def get_rows_needing_resume() -> list[dict]:
    """Returns rows (Applied or Pending) that need a resume link from Drive."""
    ws = _worksheet()
    records = ws.get_all_values()
    results = []
    for i, row in enumerate(records[1:], start=2):
        if len(row) < 5:
            continue
        status = row[COL_STATUS].strip()
        if status not in (STATUS_APPLIED, STATUS_PENDING):
            continue
        legacy = 1 if _is_legacy_format(row) else 0
        resume = _col(row, COL_RESUME_LINK, legacy)
        if resume:
            continue
        results.append({
            "row_index":   i,
            "company":     row[COL_COMPANY].strip(),
            "role":        row[COL_ROLE].strip(),
            "status":      status,
        })
    return results


def get_pending_rows() -> list[dict]:
    """Returns rows with Status == 'Pending Message'."""
    ws = _worksheet()
    records = ws.get_all_values()
    results = []
    for i, row in enumerate(records[1:], start=2):
        if len(row) < 5:
            continue
        if row[COL_STATUS].strip() != STATUS_PENDING:
            continue
        legacy = 1 if _is_legacy_format(row) else 0
        results.append({
            "row_index":   i,
            "company":     row[COL_COMPANY].strip(),
            "role":        row[COL_ROLE].strip(),
            "li_name":     _col(row, COL_LI_NAME, legacy),
            "li_url":      _col(row, COL_LI_URL, legacy),
            "resume_link": _col(row, COL_RESUME_LINK, legacy),
        })
    return results


# ─── Write ────────────────────────────────────────────────────────────────────

def store_resume_link(row_index: int, link: str):
    """Store the Drive resume link. Column H (8) for new layout, I (9) for legacy."""
    ws = _worksheet()
    col = 9 if _row_is_legacy(ws, row_index) else 8
    ws.update_cell(row_index, col, link)


def _row_is_legacy(ws, row_index: int) -> bool:
    row = ws.row_values(row_index)
    return len(row) >= 9


def mark_pending(row_index: int, li_name: str, li_url: str, source: str = ""):
    """Mark row as Pending Message + store LinkedIn info."""
    ws = _worksheet()
    legacy = _row_is_legacy(ws, row_index)
    if legacy:
        ws.update(f"E{row_index}:H{row_index}", [[STATUS_PENDING, source or "", li_name, li_url]])
    else:
        ws.update(f"E{row_index}:G{row_index}", [[STATUS_PENDING, li_name, li_url]])


def mark_sent(row_index: int):
    ws = _worksheet()
    ws.update_cell(row_index, COL_STATUS + 1, STATUS_SENT)


def mark_no_resume(row_index: int):
    ws = _worksheet()
    ws.update_cell(row_index, COL_STATUS + 1, STATUS_NO_RESUME)


def mark_already_messaged(row_index: int):
    ws = _worksheet()
    ws.update_cell(row_index, COL_STATUS + 1, STATUS_ALREADY_MESSAGED)


# ─── Sent Messages ───────────────────────────────────────────────────────────
# Separate sheet: people we've already messaged (LinkedIn ID + Company).
# Multiple people from same company allowed.

SENT_HEADERS = ["Name", "LinkedIn ID", "Company"]


def _sent_worksheet():
    """Get or create the Sent Messages worksheet."""
    spreadsheet = _client().open_by_key(SHEET_ID)
    try:
        return spreadsheet.worksheet(SENT_TAB)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=SENT_TAB, rows=5000, cols=3)


def add_to_sent_sheet(li_name: str, li_url: str, company: str):
    """Append a row to Sent Messages when we successfully send a DM."""
    ws = _sent_worksheet()
    rows = ws.get_all_values()
    if not rows:
        ws.update("A1:C1", [SENT_HEADERS])
        rows = [SENT_HEADERS]
    # Avoid duplicate: same person + company already recorded
    for row in rows[1:]:
        if len(row) >= 3 and row[1].strip() == li_url.strip() and row[2].strip() == company.strip():
            return
    ws.append_row([li_name or "", li_url or "", company or ""])


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
            # New format: A=URL, B=Name, C=Headline, D=Company
            if len(row) >= 4:
                result[url] = {
                    "url": url,
                    "name": row[1].strip() if len(row) > 1 else "",
                    "headline": row[2].strip() if len(row) > 2 else "",
                    "current_company": row[3].strip() if len(row) > 3 else "",
                }
            else:
                # Legacy: B was JSON blob
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
