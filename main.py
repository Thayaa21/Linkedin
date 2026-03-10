"""
main.py — Scheduler entry point.

Two jobs run on separate schedules:
  1. poll_connections()   — every POLL_INTERVAL_HOURS hours
                            Scrapes LinkedIn connections, diffs vs snapshot,
                            matches new connections to sheet companies,
                            marks matched rows as "Pending Message".

  2. send_messages()      — weekdays at SEND_HOUR (default 9 AM)
                            Sends DMs to all "Pending Message" rows,
                            writes status back to sheet.

Usage:
    python main.py
"""

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from playwright.async_api import async_playwright

import sheets
import drive
import linkedin as li
import matcher as m
from config import (
    POLL_INTERVAL_HOURS,
    SEND_HOUR,
    MESSAGE_TEMPLATE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


# ─── Job 1: Poll connections ──────────────────────────────────────────────────

async def poll_connections():
    """
    Scrapes current LinkedIn 1st-degree connections, diffs against the last
    snapshot, and for each newly accepted connection tries to match it to a
    company row in the sheet.
    """
    logger.info("=== poll_connections started ===")

    async with async_playwright() as p:
        browser, context = await li.make_browser_context(p)
        try:
            # Load saved cookies
            ok = await li.load_cookies(context)
            if not ok:
                logger.error("No cookies available. Run save_cookies.py first.")
                return

            page = await context.new_page()

            # Verify session is still valid
            if not await li.is_logged_in(page):
                logger.error("LinkedIn session expired. Re-run save_cookies.py.")
                return

            # Scrape current connections
            current = await li.get_connections(page)

            # ── Load snapshot from Google Sheets (persists across GH Actions runs) ──
            old_snapshot = sheets.load_snapshot_from_sheet()
            logger.info("Snapshot loaded: %d previously seen connections", len(old_snapshot))

            new_connections = li.diff_connections(old_snapshot, current)
            logger.info("New connections since last poll: %d", len(new_connections))

            if new_connections:
                # Load sheet rows that are still in "Applied" state
                applied_rows = sheets.get_applied_companies()
                logger.info("Applied rows in sheet: %d", len(applied_rows))

                # Get CSRF token once — reuse for all profile lookups
                csrf_token = await li.get_csrf_token(page)

                for conn in new_connections:
                    headline        = conn.get("headline", "")
                    current_company = conn.get("current_company", "")
                    company_hint    = m.extract_company_from_headline(headline)

                    # ── Enrich: if headline extraction looks weak, fetch profile ──
                    # The profile Voyager API returns real work-experience company names.
                    # We only call it when the headline didn't give us a clean company,
                    # keeping API calls minimal.
                    if not current_company and csrf_token:
                        pub_id = conn["url"].split("/in/")[-1].rstrip("/")
                        fetched = await li.get_profile_company(page, pub_id, csrf_token)
                        if fetched:
                            current_company = fetched
                            conn["current_company"] = fetched
                            logger.info(
                                "  Enriched %s → company from profile: '%s'",
                                conn["name"], current_company,
                            )

                    logger.info(
                        "New connection: %s | headline: '%s' | extracted: '%s' | company: '%s'",
                        conn["name"], headline, company_hint, current_company,
                    )

                    # 1st attempt: match from headline extraction
                    matched_row = m.find_matching_row(company_hint, applied_rows)

                    # 2nd attempt: match using current company from work experience
                    if not matched_row and current_company:
                        logger.info(
                            "Headline match failed — retrying with company: '%s'",
                            current_company,
                        )
                        matched_row = m.find_matching_row(current_company, applied_rows)

                    if matched_row:
                        logger.info(
                            "Matched! %s → row %d (%s @ %s)",
                            conn["name"], matched_row["row_index"],
                            matched_row["role"], matched_row["company"],
                        )
                        sheets.mark_pending(
                            row_index=matched_row["row_index"],
                            li_name=conn["name"],
                            li_url=conn["url"],
                        )
                    else:
                        logger.info("No sheet match for connection: %s", conn["name"])

            # ── Save snapshot back to Sheets so next run only sees truly new ones ──
            sheets.save_snapshot_to_sheet(current)
            logger.info("Snapshot saved to Sheets: %d connections", len(current))

        finally:
            await browser.close()

    # After LinkedIn scraping, fetch & store resume links for Applied rows
    # that don't have one yet. This runs every poll cycle so the user has
    # time to upload the resume after applying (well before the next poll).
    logger.info("Checking resume links for Applied rows...")
    applied_rows = sheets.get_applied_companies()
    for row in applied_rows:
        if row["resume_link"]:
            continue  # already stored, skip
        link = drive.get_resume_link(row["company"])
        if link:
            sheets.store_resume_link(row["row_index"], link)
            logger.info("Stored resume link for %s → row %d", row["company"], row["row_index"])
        else:
            logger.info("No resume yet for %s (row %d)", row["company"], row["row_index"])

    logger.info("=== poll_connections complete ===")


# ─── Job 2: Send messages ─────────────────────────────────────────────────────

async def send_messages():
    """
    Sends DMs to all rows marked 'Pending Message'.
    Only runs on weekdays — APScheduler cron handles the day filter,
    but we double-check here for safety.
    """
    today = datetime.now()
    if today.weekday() >= 5:   # 5=Sat, 6=Sun
        logger.info("send_messages: skipping — weekend")
        return

    logger.info("=== send_messages started ===")
    pending = sheets.get_pending_rows()
    logger.info("Pending rows to message: %d", len(pending))

    if not pending:
        logger.info("Nothing to send.")
        return

    async with async_playwright() as p:
        browser, context = await li.make_browser_context(p)
        try:
            ok = await li.load_cookies(context)
            if not ok:
                logger.error("No cookies. Run save_cookies.py first.")
                return

            page = await context.new_page()

            if not await li.is_logged_in(page):
                logger.error("LinkedIn session expired. Re-run save_cookies.py.")
                return

            for row in pending:
                profile_url = row["li_url"]
                company     = row["company"]
                role        = row["role"]
                li_name     = row["li_name"]
                first_name  = li_name.split()[0] if li_name else "there"

                # Step 5: get resume link from sheet (stored during poll)
                resume_link = row.get("resume_link", "")
                if not resume_link:
                    # Fallback: try Drive directly in case poll missed it
                    resume_link = drive.get_resume_link(company)
                if not resume_link:
                    logger.warning("No resume for %s — skipping DM, marking No Resume", company)
                    sheets.mark_no_resume(row["row_index"])
                    continue

                # Build personalised message
                message = MESSAGE_TEMPLATE.format(
                    first_name=first_name,
                    company=company,
                    role=role,
                    resume_link=resume_link,
                )

                # Step 6: send DM
                success = await li.send_message(page, profile_url, message)

                # Step 7: log result back to sheet
                if success:
                    sheets.mark_sent(row["row_index"])
                else:
                    # Leave as Pending — will retry next send window
                    logger.warning("Failed to send to %s, will retry next run.", li_name)

        finally:
            await browser.close()

    logger.info("=== send_messages complete ===")


# ─── Scheduler setup ──────────────────────────────────────────────────────────

async def main():
    scheduler = AsyncIOScheduler()

    # Poll connections every N hours
    scheduler.add_job(
        poll_connections,
        trigger=IntervalTrigger(hours=POLL_INTERVAL_HOURS),
        id="poll_connections",
        name="Poll LinkedIn connections",
        next_run_time=datetime.now(),  # run immediately on startup
        coalesce=True,
        max_instances=1,
    )

    # Send messages at SEND_HOUR on Mon–Fri
    scheduler.add_job(
        send_messages,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=SEND_HOUR,
            minute=0,
        ),
        id="send_messages",
        name="Send LinkedIn DMs",
        coalesce=True,
        max_instances=1,
    )

    scheduler.start()
    logger.info(
        "Scheduler running. Polling every %dh, sending at %d:00 Mon–Fri.",
        POLL_INTERVAL_HOURS, SEND_HOUR,
    )

    # Keep the event loop alive
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down.")
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
