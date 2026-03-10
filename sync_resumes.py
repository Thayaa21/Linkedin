"""
sync_resumes.py — Fetch resume links from Google Drive and store in the sheet.

Run when you've uploaded new resumes to Drive and want to update the sheet
without running the full poll.

Usage:
    python sync_resumes.py
"""

import logging
import sheets
import drive

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sync_resumes")


def main():
    logger.info("Fetching resume links from Drive...")
    rows = sheets.get_rows_needing_resume()
    logger.info("Rows needing resume: %d", len(rows))

    for row in rows:
        link = drive.get_resume_link(row["company"])
        if link:
            sheets.store_resume_link(row["row_index"], link)
            logger.info("Stored: %s → row %d", row["company"], row["row_index"])
        else:
            logger.info("Not found: %s (row %d)", row["company"], row["row_index"])

    logger.info("Done.")


if __name__ == "__main__":
    main()
