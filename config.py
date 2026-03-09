"""
config.py — single source of truth for all settings.
All values come from environment variables loaded from .env
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Google ───────────────────────────────────────────────────────────────────
SHEET_ID          = os.environ['SHEET_ID']           # Google Sheet ID
SHEET_TAB         = os.environ.get('SHEET_TAB', 'Applications')
GOOGLE_CREDS_FILE = os.environ.get('GOOGLE_CREDS_FILE', 'google_creds.json')  # service account JSON
DRIVE_FOLDER_ID   = os.environ['DRIVE_FOLDER_ID']   # Drive folder containing resumes

# ─── LinkedIn ─────────────────────────────────────────────────────────────────
COOKIES_FILE          = os.environ.get('COOKIES_FILE', 'linkedin_cookies.json')
CONNECTIONS_SNAPSHOT  = os.environ.get('CONNECTIONS_SNAPSHOT', 'connections_snapshot.json')

# ─── Scheduler ────────────────────────────────────────────────────────────────
POLL_INTERVAL_HOURS = int(os.environ.get('POLL_INTERVAL_HOURS', '6'))
SEND_HOUR           = int(os.environ.get('SEND_HOUR', '9'))   # 9 AM local time

# ─── Message template ─────────────────────────────────────────────────────────
# Available placeholders: {first_name} {company} {role} {resume_link}
MESSAGE_TEMPLATE = os.environ.get('MESSAGE_TEMPLATE', """\
Hi {first_name},

I recently applied to the {role} position at {company} and noticed we're connected on LinkedIn — small world!

I'd love to learn more about the team and share a bit about my background. I put together a resume tailored for this role if you'd like to take a look: {resume_link}

Would really appreciate any insight you might have. Thanks for your time!\
""")

# ─── Matching ────────────────────────────────────────────────────────────────
# Minimum fuzzy-match score (0–100) to consider a connection's company
# a match to a company in the sheet.
FUZZY_THRESHOLD = int(os.environ.get('FUZZY_THRESHOLD', '80'))
