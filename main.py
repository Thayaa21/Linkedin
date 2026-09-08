"""
main.py — Scheduler entry point.

Two jobs run on separate schedules:
  1. poll_connections()   — every POLL_INTERVAL_HOURS hours
                            Fetches the most-recent MAX_POLL_CONNECTIONS
                            connections, skips anyone already in the snapshot,
                            extracts each new person's company (GPT-4o-mini with
                            regex fallback), matches to the Tracker, and queues
                            in-window matches to the Sent sheet (Pending).

  2. send_messages()      — weekdays at SEND_HOUR (default 9 AM)
                            Sends one DM per Pending row within the 12-day
                            window, then marks Message Sent in BOTH the Sent
                            sheet and the Tracker.

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
import drive
import linkedin as li
import matcher as m
from config import (
    POLL_INTERVAL_HOURS,
    SEND_HOUR,
    MESSAGE_TEMPLATE,
    MESSAGE_APPLY_WITHIN_DAYS,
    MAX_POLL_CONNECTIONS,
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
    Fetch the most-recent connections, skip anyone already in the snapshot,
    extract each new person's company (GPT-4o-mini), match to the Tracker, and
    add matches within the 12-day window to the Sent sheet as Pending.

    Reads the Tracker and Sent sheet ONCE each (not per connection) to stay well
    under Google's per-minute quota.
    """
    logger.info("=== poll_connections started ===")

    # Ensure Sent sheet exists; refresh tracker's outreach column once.
    try:
        sheets.ensure_sent_sheet_exists()
    except Exception as e:
        logger.warning("Could not ensure Sent sheet: %s", e)
    try:
        sheets.refresh_tracker_outreach_column()
    except Exception as e:
        logger.warning("Could not refresh tracker outreach column: %s", e)

    # ── Load sheet state ONCE ──────────────────────────────────────────────────
    snapshot = sheets.load_snapshot_from_sheet()
    known_urls = {sheets.normalize_li_url(u) for u in snapshot.keys()}
    logger.info("Snapshot: %d previously seen connections", len(snapshot))

    tracker = sheets.load_tracker_index()
    all_jobs = [r for r in tracker["rows"] if r.get("url")]
    tracked_urls = sheets.get_tracked_li_urls()  # already in Sent sheet (any status)
    logger.info("Tracker rows with a job URL: %d", len(all_jobs))

    # ── Fetch recent connections ───────────────────────────────────────────────
    async with async_playwright() as p:
        browser, context = await li.make_browser_context(p)
        try:
            if not await li.load_cookies(context):
                logger.error("No cookies available. Run save_cookies.py first.")
                return
            page = await context.new_page()
            if not await li.is_logged_in(page):
                logger.error("LinkedIn session expired. Re-run save_cookies.py.")
                return

            new_people = await li.get_recent_connections(
                page,
                max_connections=MAX_POLL_CONNECTIONS,
                known_urls=known_urls,
            )
        finally:
            await browser.close()

    logger.info("New connections to process: %d", len(new_people))

    added = 0
    for conn in new_people:
        name = conn["name"]
        url = conn["url"]
        headline = conn.get("headline", "")

        company = m.extract_company_from_headline(headline)
        logger.info("New: %s | headline='%s' | company='%s'", name, headline[:60], company)

        # Record in snapshot regardless of match, so we never reprocess this person.
        snapshot[url] = {
            "url": url, "name": name, "headline": headline, "current_company": company,
        }

        if not company:
            continue

        matched = m.find_matching_row(company, all_jobs)
        if not matched:
            logger.info("  no Tracker match for %s", name)
            continue

        # Only queue if the application is within the 12-day window.
        if not sheets.within_window_from_index(
            tracker, matched["company"], matched.get("url", ""), MESSAGE_APPLY_WITHIN_DAYS
        ):
            logger.info(
                "  %s matched %s but application is outside the %d-day window — skipping",
                name, matched["company"], MESSAGE_APPLY_WITHIN_DAYS,
            )
            continue

        if sheets.add_pending_to_sent_sheet(
            li_name=name,
            li_url=url,
            company=matched["company"],
            role=matched["role"],
            job_url=matched.get("url", ""),
            tracked_urls=tracked_urls,
            check_window=False,  # already checked above with the preloaded index
        ):
            added += 1
            logger.info("  queued: %s → %s @ %s", name, matched["role"], matched["company"])

    # ── Save snapshot ONCE ─────────────────────────────────────────────────────
    sheets.save_snapshot_to_sheet(snapshot)
    logger.info("Snapshot saved: %d connections | %d new pending rows", len(snapshot), added)
    logger.info("=== poll_connections complete ===")


# ─── Job 2: Send messages ─────────────────────────────────────────────────────

async def send_messages():
    """
    Sends DMs to all Pending rows in Sent sheet.
    Updates Status to Message Sent on success.
    """
    logger.info("=== send_messages started ===")

    # Deduplicate first — remove duplicate rows that could cause double sends
    try:
        n = sheets.deduplicate_sent_sheet()
        if n:
            logger.info("Deduplicated Sent sheet: removed %d duplicate row(s)", n)
    except Exception as e:
        logger.warning("Could not deduplicate Sent sheet: %s", e)

    pending = sheets.get_pending_rows(include_no_resume=True)  # Retry No Resume when resume added to Drive
    logger.info("Rows to message (Pending + No Resume retry): %d", len(pending))

    if not pending:
        logger.info("Nothing to send.")
        return

    # Load state ONCE (avoids per-row sheet reads that trip the quota).
    tracker = sheets.load_tracker_index()

    # One-time session check
    async with async_playwright() as p:
        browser, context = await li.make_browser_context(p)
        try:
            ok = await li.load_cookies(context)
            if not ok:
                logger.error("No cookies. Run save_cookies.py first.")
                return
            check_page = await context.new_page()
            if not await li.is_logged_in(check_page):
                await check_page.close()
                logger.error("LinkedIn session expired. Re-run save_cookies.py.")
                return
            await check_page.close()
        finally:
            await browser.close()

    already_sent = sheets.get_sent_li_urls()
    for i, row in enumerate(pending):
        profile_url = row["li_url"]
        company     = row["company"]
        role        = row["role"]
        li_name     = row["li_name"]
        first_name  = li_name.split()[0] if li_name else "there"

        # Skip if we've already sent to this person (dedup gate, in-memory set).
        li_norm = sheets.normalize_li_url(profile_url)
        if li_norm in already_sent:
            logger.info("Skipping %s — already sent; marking Message Sent", li_name)
            sheets.mark_sent_in_sent_sheet(row["row_index"])
            sheets.update_tracker_status_for_company(company, sheets.STATUS_SENT)
            continue

        # 12-day window check using the preloaded tracker index (no network read).
        if not sheets.within_window_from_index(
            tracker, company, row.get("job_url", ""), MESSAGE_APPLY_WITHIN_DAYS
        ):
            logger.info(
                "Skipping %s — application for %s is outside the last %d days (Tracker Applied Date); marking Outside Message Window",
                li_name,
                company,
                MESSAGE_APPLY_WITHIN_DAYS,
            )
            sheets.mark_outside_message_window_in_sent_sheet(row["row_index"])
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
        async with async_playwright() as p:
            browser, context = await li.make_browser_context(p)
            try:
                ok = await li.load_cookies(context)
                if not ok:
                    logger.error("No cookies for %s — skipping", li_name)
                else:
                    page = await context.new_page()
                    success = await li.send_message(page, profile_url, message)
                    await page.close()
            except Exception as e:
                logger.error("Error sending to %s: %s", li_name, e)
            finally:
                await browser.close()

        if success:
            sheets.mark_sent_in_sent_sheet(row["row_index"])
            sheets.update_tracker_status_for_company(company, sheets.STATUS_SENT)
            already_sent.add(sheets.normalize_li_url(profile_url))
            logger.info("Sent %d/%d: %s", i + 1, len(pending), li_name)
        else:
            # success is only False when nothing was actually sent (Message
            # button / composer / Send button not found). Safe to retry next run.
            logger.warning("Failed to send to %s (nothing sent) — will retry next run.", li_name)

        if i < len(pending) - 1:
            await asyncio.sleep(SEND_DELAY_SECONDS)

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
