# LinkedIn Referral Agent

An automated system that matches your LinkedIn connections to job applications and sends personalized referral request messages—so you can focus on applying while it handles the outreach.

---

## The Problem

When you apply to jobs, getting a referral from someone at the company dramatically increases your chances. But tracking which connections work where, when they accept your request, and reaching out with a tailored message is manual and time-consuming.

## The Solution

This agent:

1. **Tracks** your job applications in a Google Sheet
2. **Monitors** your LinkedIn connections and matches them to companies you've applied to (using headline extraction + fuzzy matching)
3. **Fetches** the right resume from Google Drive for each company
4. **Sends** personalized DMs to every matching connection—including multiple people at the same company

All automated. Runs on GitHub Actions. No manual follow-ups.

---

## How It Works

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Google Sheet   │     │  LinkedIn Agent   │     │  Google Drive   │
│  (Applications) │────▶│  Poll (every 6h)  │────▶│  (Resumes)      │
│  Applied jobs   │     │  Match connections│     │  Thayaa_X.pdf   │
└─────────────────┘     └────────┬─────────┘     └─────────────────┘
                                 │
                                 ▼
                        ┌──────────────────┐
                        │  Send (9 AM MST) │
                        │  Personalized DMs │
                        └──────────────────┘
```

**Flow:**
- You log applications in a sheet (Company, Role, Job URL, Status)
- Poll runs every 6 hours: scrapes connections, diffs against last snapshot, matches new connections to your applied companies
- When a connection matches → row becomes "Pending Message"
- Send runs weekdays at 9 AM MST: fetches resume from Drive, sends DM with your template
- Status updates: Applied → Pending Message → Message Sent

---

## Tech Stack

| Layer | Tech |
|-------|------|
| Automation | Playwright (headless Chromium) |
| Scheduling | GitHub Actions (cron) |
| Data | Google Sheets API, Google Drive API |
| Matching | RapidFuzz (fuzzy company matching) |
| Dashboard | Streamlit |

---

## Key Features

- **Multi-referral**: Messages *all* connections at a company you applied to, not just one
- **Headline-based matching**: Extracts company from "Engineer at Stripe" / "PM @ Google" patterns
- **Resume sync**: Auto-fetches `Thayaa_{Company}.pdf` from Drive and stores in sheet
- **Duplicate prevention**: Never messages the same person twice
- **LinkedIn quirks handled**: Disables hidden iframe that intercepts clicks; profile-scoped Message button selector

---

## Sheet Layout

**Tracker (Applications):** Applied Date | Company | Role | Job URL | Status | LI Name | LI URL | Resume Link

**Snapshot:** Profile URL | Name | Headline | Company

If you have an existing sheet with the old format (Timestamp, Source), run:
```bash
python migrate_sheet.py
```

---

## Setup

### Prerequisites

- Python 3.10+
- Google Cloud: Service account with Sheets + Drive access
- LinkedIn: Session cookies (one-time via `save_cookies.py`)

### Quick Start

```bash
git clone https://github.com/Thayaa21/Linkedin.git
cd Linkedin
python -m venv venv && source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

1. Copy `.env.example` → `.env` and fill in `SHEET_ID`, `DRIVE_FOLDER_ID`, etc.
2. Add `google_creds.json` (service account key)
3. Run `python save_cookies.py` — log into LinkedIn in the browser, press Enter to save cookies

### Run Locally

```bash
# Poll connections (match to sheet)
python -c "import asyncio; from main import poll_connections; asyncio.run(poll_connections())"

# Send pending messages
python -c "import asyncio; from main import send_messages; asyncio.run(send_messages())"

# Sync resumes from Drive to sheet
python sync_resumes.py

# Dashboard
streamlit run dashboard.py
```

### GitHub Actions

Configure repo secrets: `GOOGLE_CREDS_JSON`, `LINKEDIN_COOKIES`, `SHEET_ID`, `DRIVE_FOLDER_ID`, `CONNECTIONS_SNAPSHOT`. Workflows run automatically on schedule or trigger manually.

---

## Project Structure

```
├── main.py          # Orchestrator: poll + send jobs
├── linkedin.py      # Playwright: cookies, scraping, DMs
├── sheets.py        # Google Sheets read/write
├── drive.py         # Resume lookup from Drive
├── matcher.py       # Fuzzy company matching
├── config.py        # Env-based config
├── dashboard.py     # Streamlit monitoring UI
├── sync_resumes.py  # Manual resume sync
└── save_cookies.py  # One-time LinkedIn session capture
```

---

## License

MIT
