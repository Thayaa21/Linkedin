"""
sync_resumes.py — Resume links are now fetched from Drive at send time.

The agent no longer stores resume links in the sheet. When sending a DM,
it looks up Thayaa_{Company}.pdf in the Drive folder.

Ensure your resumes are named: Thayaa_{Company}.pdf (e.g. Thayaa_Nokia.pdf)
and have "Anyone with the link can view" sharing enabled.

This script is kept for backwards compatibility but does nothing.
"""

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sync_resumes")


def main():
    logger.info(
        "Resume links are fetched from Drive at send time. "
        "No sync needed. Ensure Thayaa_{Company}.pdf files exist in your Drive folder."
    )


if __name__ == "__main__":
    main()
