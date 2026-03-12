"""
main.py — Scheduler entry point.

Two jobs run on separate schedules:
  1. poll_connections()   — every POLL_INTERVAL_HOURS hours
                            Scrapes LinkedIn connections, diffs vs snapshot,
                            matches new connections to sheet companies,
                            adds matches to Sent sheet (Pending).

  2. send_messages()      — weekdays at SEND_HOUR (default 9 AM)
                            Sends DMs to all Pending rows in Sent sheet,
                            updates Status to Message Sent.

Usage:
    python main.py
"""

import asyncio
import logging
from datetime import datetime

SEND_DELAY_SECONDS = 8  # Delay between sends (prevents wrong recipient when back-to-back)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from playwright.async_api import async_playwright

import sheets
from sheets import STATUS_APPLIED
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
    snapshot, and for each match adds a row to Sent sheet (Pending).
    """
    logger.info("=== poll_connections started ===")

    # Ensure Sent sheet exists (creates 3rd tab)
    try:
        sheets.ensure_sent_sheet_exists()
        logger.info("Sent sheet ensured")
    except Exception as e:
        logger.warning("Could not ensure Sent sheet: %s", e)

    # Remove duplicate rows (e.g. same person twice — keep Message Sent)
    try:
        n = sheets.deduplicate_sent_sheet()
        if n:
            logger.info("Deduplicated Sent sheet: removed %d duplicate row(s)", n)
    except Exception as e:
        logger.warning("Could not deduplicate Sent sheet: %s", e)

    # Refine tracker: timestamp→date, 5 cols only (no Name, LI, Resume)
    try:
        sheets.refine_tracker_sheet()
        logger.info("Tracker sheet refined (5 columns)")
    except Exception as e:
        logger.warning("Could not refine tracker: %s", e)

    async with async_playwright() as p:
        browser, context = await li.make_browser_context(p)
        try:
            ok = await li.load_cookies(context)
            if not ok:
                logger.error("No cookies available. Run save_cookies.py first.")
                return

            page = await context.new_page()

            if not await li.is_logged_in(page):
                logger.error("LinkedIn session expired. Re-run save_cookies.py.")
                return

            old_snapshot = sheets.load_snapshot_from_sheet()
            logger.info("Snapshot loaded: %d previously seen connections", len(old_snapshot))

            current = await li.get_connections(page, old_snapshot)

            # ── Pass 1: headline extraction ────────────────────────────────────
            headline_filled = 0
            for conn in current.values():
                if conn.get("current_company"):
                    continue
                headline = conn.get("headline", "")
                extracted = m.extract_company_from_headline(headline)
                if extracted and extracted != headline.strip():
                    conn["current_company"] = extracted
                    headline_filled += 1
            logger.info("Headline extraction filled company for %d connections", headline_filled)

            for url, snap_conn in old_snapshot.items():
                if url in current and not current[url].get("current_company"):
                    stored = snap_conn.get("current_company", "")
                    if stored:
                        current[url]["current_company"] = stored

            new_connections = li.diff_connections(old_snapshot, current)
            logger.info("New connections since last poll: %d", len(new_connections))

            if new_connections:
                applied_rows = sheets.get_applied_companies()
                logger.info("Applied rows in sheet: %d", len(applied_rows))

                for conn in new_connections:
                    headline        = conn.get("headline", "")
                    current_company = conn.get("current_company", "")
                    company_hint    = m.extract_company_from_headline(headline)

                    logger.info(
                        "New connection: %s | headline: '%s' | extracted: '%s' | company: '%s'",
                        conn["name"], headline, company_hint, current_company,
                    )

                    matched_row = m.find_matching_row(company_hint, applied_rows)
                    if not matched_row and current_company:
                        matched_row = m.find_matching_row(current_company, applied_rows)

                    if matched_row:
                        logger.info(
                            "Matched! %s → %s @ %s — adding to Sent sheet",
                            conn["name"], matched_row["role"], matched_row["company"],
                        )
                        sheets.add_pending_to_sent_sheet(
                            li_name=conn["name"],
                            li_url=conn["url"],
                            company=matched_row["company"],
                            role=matched_row["role"],
                            job_url=matched_row.get("url", ""),
                        )
                    else:
                        logger.info("No sheet match for connection: %s", conn["name"])

            # ── Pass 2: log connections still missing a company ───────────────
            still_empty = [
                conn["name"] for conn in current.values()
                if not conn.get("current_company")
            ]
            if still_empty:
                logger.info(
                    "%d connections have no company: %s",
                    len(still_empty), ", ".join(still_empty),
                )

            # ── Pass 3: Multi-referral — add ALL connections at target companies to Sent ─
            all_jobs     = sheets.get_all_jobs()
            tracked_urls = sheets.get_tracked_li_urls()
            new_referrals = 0

            if all_jobs:
                for conn in current.values():
                    li_url = conn["url"]
                    li_norm = sheets.normalize_li_url(li_url)
                    if li_norm in tracked_urls:
                        continue

                    company      = conn.get("current_company", "")
                    company_hint = m.extract_company_from_headline(conn.get("headline", ""))

                    matched = None
                    if company:
                        matched = m.find_matching_row(company, all_jobs)
                    if not matched and company_hint and company_hint != conn.get("headline", "").strip():
                        matched = m.find_matching_row(company_hint, all_jobs)

                    if matched:
                        logger.info(
                            "Multi-referral: %s → %s @ %s",
                            conn["name"], matched["role"], matched["company"],
                        )
                        sheets.add_pending_to_sent_sheet(
                            li_name=conn["name"],
                            li_url=li_url,
                            company=matched["company"],
                            role=matched["role"],
                            job_url=matched.get("url", ""),
                        )
                        tracked_urls.add(li_norm)
                        new_referrals += 1

            if new_referrals:
                logger.info("Multi-referral: added %d new pending rows to Sent sheet", new_referrals)

            sheets.save_snapshot_to_sheet(current)
            logger.info("Snapshot saved to Sheets: %d connections", len(current))

        finally:
            await browser.close()

    logger.info("=== poll_connections complete ===")


# ─── Job 2: Send messages ─────────────────────────────────────────────────────

async def send_messages():
    """
    Sends DMs to all Pending rows in Sent sheet.
    Updates Status to Message Sent on success.
    """
    logger.info("=== send_messages started ===")
    pending = sheets.get_pending_rows(include_no_resume=True)  # Retry No Resume when resume added to Drive
    logger.info("Rows to message (Pending + No Resume retry): %d", len(pending))

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

            # Verify session with a temp page, then close it
            check_page = await context.new_page()
            if not await li.is_logged_in(check_page):
                await check_page.close()
                logger.error("LinkedIn session expired. Re-run save_cookies.py.")
                return
            await check_page.close()

            already_sent = sheets.get_sent_li_urls()
            for i, row in enumerate(pending):
                profile_url = row["li_url"]
                company     = row["company"]
                role        = row["role"]
                li_name     = row["li_name"]
                first_name  = li_name.split()[0] if li_name else "there"

                # Never send to someone we've already messaged (duplicate row guard)
                if sheets.normalize_li_url(profile_url) in already_sent:
                    logger.info("Skipping %s — already sent (duplicate row), marking as Message Sent", li_name)
                    sheets.mark_sent_in_sent_sheet(row["row_index"])
                    sheets.update_tracker_status_for_company(company, sheets.STATUS_SENT)
                    continue

                resume_link = drive.get_resume_link(company)
                if not resume_link:
                    logger.warning("No resume for %s — skipping DM, marking No Resume", company)
                    sheets.mark_no_resume_in_sent_sheet(row["row_index"])
                    continue

                message = MESSAGE_TEMPLATE.format(
                    first_name=first_name,
                    company=company,
                    role=role,
                    resume_link=resume_link,
                )

                success = False
                for attempt in range(2):
                    page = await context.new_page()
                    try:
                        success = await li.send_message(page, profile_url, message)
                        if success:
                            break
                        if attempt == 0:
                            logger.warning("Send to %s failed, retrying...", li_name)
                            await asyncio.sleep(3)
                    except Exception as e:
                        logger.error("Error sending to %s (attempt %d): %s", li_name, attempt + 1, e)
                        if attempt == 0:
                            await asyncio.sleep(3)
                    finally:
                        await page.close()
                if success:
                    sheets.mark_sent_in_sent_sheet(row["row_index"])
                    sheets.update_tracker_status_for_company(company, sheets.STATUS_SENT)
                    already_sent.add(sheets.normalize_li_url(profile_url))
                    logger.info("Sent %d/%d: %s", i + 1, len(pending), li_name)
                else:
                    logger.warning("Failed to send to %s after 2 attempts, will retry next run.", li_name)

                # Delay between sends to avoid rate limiting
                if i < len(pending) - 1:
                    await asyncio.sleep(SEND_DELAY_SECONDS)

        finally:
            await browser.close()

    logger.info("=== send_messages complete ===")


# ─── Scheduler setup ──────────────────────────────────────────────────────────

async def main():
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        poll_connections,
        trigger=IntervalTrigger(hours=POLL_INTERVAL_HOURS),
        id="poll_connections",
        name="Poll LinkedIn connections",
        next_run_time=datetime.now(),
        coalesce=True,
        max_instances=1,
    )

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

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down.")
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
