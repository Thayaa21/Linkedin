"""
migrate_sheet.py — Convert tracker to clean layout: Applied Date, no Source.

Run once to clean your Applications sheet:
  - Converts Timestamp → Applied Date (YYYY-MM-DD)
  - Removes Source column
  - Updates headers

Usage:
    python migrate_sheet.py
"""

import re
import gspread
from google.oauth2.service_account import Credentials
from config import SHEET_ID, SHEET_TAB, GOOGLE_CREDS_FILE

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def to_date(ts: str) -> str:
    m = re.match(r"(\d{4}-\d{2}-\d{2})", (ts or "").strip())
    return m.group(1) if m else (ts[:10] if len(ts or "") >= 10 else ts or "")


def main():
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)

    rows = ws.get_all_values()
    if len(rows) < 2:
        print("No data to migrate.")
        return

    headers = ["Applied Date", "Company", "Role", "Job URL", "Status", "LI Name", "LI URL", "Resume Link"]
    new_rows = [headers]

    for row in rows[1:]:
        if len(row) < 5:
            continue
        # Old: A=Timestamp, B=Company, C=Role, D=URL, E=Status, F=Source, G=LI Name, H=LI URL, I=Resume
        applied_date = to_date(row[0]) if row else ""
        company = row[1] if len(row) > 1 else ""
        role = row[2] if len(row) > 2 else ""
        job_url = row[3] if len(row) > 3 else ""
        status = row[4] if len(row) > 4 else ""
        li_name = row[6] if len(row) > 6 else ""
        li_url = row[7] if len(row) > 7 else ""
        resume = row[8] if len(row) > 8 else ""

        new_rows.append([applied_date, company, role, job_url, status, li_name, li_url, resume])

    ws.clear()
    ws.update(f"A1:H{len(new_rows)}", new_rows)
    print(f"Migrated {len(new_rows) - 1} rows. New layout: Applied Date, Company, Role, Job URL, Status, LI Name, LI URL, Resume Link.")


if __name__ == "__main__":
    main()
