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

I'm interested in applying for the {role} position at {company}. I know a lot of people use AI to 'game' the application process, but I'm taking a different route. My resume reflects my actual, raw experience, even where it doesn't perfectly mirror the JD. I'd rather be hired for my real-world problem-solving than for a perfectly tailored document.

If you're willing to refer me, I'm confident I can prove my value in the technical rounds. I'd love to make sure your recommendation is one you're proud of.

Here's my resume if you'd like to take a look: {resume_link}\
""")

# ─── Matching ────────────────────────────────────────────────────────────────
# Minimum fuzzy-match score (0–100) to consider a connection's company
# a match to a company in the sheet.
FUZZY_THRESHOLD = int(os.environ.get('FUZZY_THRESHOLD', '80'))
