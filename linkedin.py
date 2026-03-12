"""
linkedin.py — All Playwright interactions with LinkedIn.

Functions:
  load_cookies(context)        — restores saved session
  get_connections(page)        — scrapes all 1st-degree connections
  diff_connections(old, new)   — returns newly added connections
  save_snapshot(connections)   — persists snapshot to disk
  load_snapshot()              — loads previous snapshot from disk
  send_message(page, url, msg) — opens profile, sends a DM

LinkedIn is a SPA — we use human-like delays throughout to reduce
detection risk.
"""

import json
import asyncio
import logging
import random
from pathlib import Path
from playwright.async_api import BrowserContext, Page, async_playwright
from config import COOKIES_FILE, CONNECTIONS_SNAPSHOT

logger = logging.getLogger(__name__)

LINKEDIN_BASE = "https://www.linkedin.com"
CONNECTIONS_URL = f"{LINKEDIN_BASE}/mynetwork/invite-connect/connections/"


def _normalize_url(url: str) -> str:
    """Normalize LinkedIn profile URL for comparison."""
    u = (url or "").strip().lower().rstrip("/")
    if "linkedin.com/in/" in u:
        parts = u.split("linkedin.com/in/", 1)
        profile = parts[-1].split("?")[0].rstrip("/")
        return f"linkedin.com/in/{profile}"
    return u


# ─── Human-like delays ────────────────────────────────────────────────────────

async def _pause(lo=1.0, hi=3.0):
    await asyncio.sleep(random.uniform(lo, hi))


# ─── Cookie management ────────────────────────────────────────────────────────

def _cookies_exist() -> bool:
    return Path(COOKIES_FILE).exists()


async def load_cookies(context: BrowserContext) -> bool:
    """
    Loads saved cookies into the browser context.
    Returns True if cookies were loaded, False if the file doesn't exist.
    """
    if not _cookies_exist():
        logger.error("Cookie file not found: %s. Run save_cookies.py first.", COOKIES_FILE)
        return False
    cookies = json.loads(Path(COOKIES_FILE).read_text())
    await context.add_cookies(cookies)
    logger.info("Loaded %d cookies from %s", len(cookies), COOKIES_FILE)
    return True


async def is_logged_in(page: Page) -> bool:
    """Navigates to feed and checks we're not bounced to login."""
    await page.goto(f"{LINKEDIN_BASE}/feed/", wait_until="domcontentloaded")
    await _pause(2, 4)
    return "login" not in page.url and "authwall" not in page.url


# ─── Connections scraping ─────────────────────────────────────────────────────

async def get_connections(page: Page, old_snapshot: dict | None = None) -> dict[str, dict]:
    """
    Scrapes the LinkedIn Connections page. Fetches 50 per page (RECENTLY_ADDED order);
    stops when we hit someone already in old_snapshot, then merges new into existing.

    Returns a dict keyed by profile URL (old_snapshot + newly fetched).
    """
    old_snapshot = old_snapshot or {}
    # Navigate to LinkedIn home to establish session (not connections page directly)
    logger.info("Navigating to LinkedIn to establish session...")
    page.on("console", lambda msg: logger.info("BROWSER: %s", msg.text) if msg.type == "log" else None)
    await page.goto(f"{LINKEDIN_BASE}/mynetwork/", wait_until="domcontentloaded")
    await _pause(2, 4)

    # Extract CSRF token from JSESSIONID cookie (required for Voyager API)
    cookies = await page.context.cookies()
    csrf_token = next(
        (c["value"].strip('"') for c in cookies if c["name"] == "JSESSIONID"),
        ""
    )
    logger.info("CSRF token found: %s", "yes" if csrf_token else "NO — cannot call API")

    if not csrf_token:
        logger.error("JSESSIONID cookie missing. Re-run save_cookies.py.")
        return {}

    # ── Step 2: Navigate to connections page and intercept LinkedIn's own API ────
    # LinkedIn's JavaScript makes its own Voyager calls with all the right headers.
    # We intercept those responses to collect profile + position data automatically.
    # This is more reliable than calling the API ourselves.
    logger.info("Setting up network interceptor for connections page...")

    intercepted_profiles: dict[str, dict] = {}    # publicIdentifier → profile dict
    intercepted_connections_urns: list[str] = []   # connectedMember URNs (recency order)
    intercepted_companies: dict[str, str] = {}     # company entityUrn → name
    intercepted_positions: dict[str, str] = {}     # profile entityUrn → company name

    import json as _json

    async def _on_response(response):
        url = response.url
        if "voyager/api" not in url:
            return
        try:
            text = await response.text()
            data = _json.loads(text)
        except Exception:
            return

        included = data.get("included", [])

        # Collect profile URNs from connections endpoint (preserves recency order)
        if "relationships" in url and "connections" in url:
            for item in included:
                if not isinstance(item, dict):
                    continue
                member = item.get("connectedMember", "")
                if member and member not in intercepted_connections_urns:
                    intercepted_connections_urns.append(member)

        for item in included:
            if not isinstance(item, dict):
                continue
            item_type = item.get("$type", "")

            # ── Collect Company objects for URN resolution ────────────────────
            if item_type and ("Company" in item_type or "MiniCompany" in item_type):
                urn = item.get("entityUrn") or item.get("objectUrn", "")
                name = item.get("name", "")
                if urn and name:
                    intercepted_companies[urn] = name

            # ── Collect Position data (current positions only) ────────────────
            if item_type and ("Position" in item_type or "WorkExperience" in item_type):
                # Only current positions (no end date)
                tp = item.get("timePeriod", {}) or {}
                dr = item.get("dateRange", {}) or {}
                if not tp.get("endDate") and not dr.get("end"):
                    profile_urn = (item.get("profileUrn")
                                   or item.get("*profileUrn", ""))
                    comp_name = item.get("companyName", "")
                    if not comp_name:
                        comp_ref = (item.get("*company")
                                    or item.get("companyUrn", "")
                                    or item.get("company", ""))
                        if isinstance(comp_ref, str):
                            comp_name = intercepted_companies.get(comp_ref, "")
                        elif isinstance(comp_ref, dict):
                            comp_name = comp_ref.get("name", "")
                    if comp_name and profile_urn:
                        intercepted_positions[profile_urn] = comp_name

            # ── Collect profile objects ────────────────────────────────────────
            pid = item.get("publicIdentifier", "")
            if not pid:
                continue
            # Merge: keep whichever copy has more fields
            existing = intercepted_profiles.get(pid, {})
            if len(item) > len(existing):
                intercepted_profiles[pid] = item

    page.on("response", _on_response)

    # Navigate — LinkedIn's app will fetch connections + mini-profiles automatically
    logger.info("Navigating to connections page (intercepting API traffic)...")
    await page.goto(CONNECTIONS_URL, wait_until="domcontentloaded")
    await _pause(3, 5)

    # Scroll down so LinkedIn lazy-loads the rest of the connection cards.
    # Each scroll triggers LinkedIn's own API calls for mini-profiles, which
    # our interceptor captures (including current company / position data).
    logger.info("Scrolling to trigger lazy-loaded profile API calls...")
    for scroll_i in range(12):
        await page.evaluate("window.scrollBy(0, 600)")
        await asyncio.sleep(1.2)
        if len(intercepted_profiles) >= 45:
            logger.info("Enough profiles collected (%d), stopping scroll", len(intercepted_profiles))
            break

    await _pause(2, 3)  # final wait for any in-flight requests

    logger.info(
        "Intercepted: %d profile URNs, %d profiles, %d positions, %d companies",
        len(intercepted_connections_urns), len(intercepted_profiles),
        len(intercepted_positions), len(intercepted_companies),
    )

    # ── Step 3: If scrolling didn't yield profiles, fall back to API ──────────
    known_urls = [_normalize_url(u) for u in old_snapshot.keys()]
    if len(intercepted_profiles) < 5:
        logger.info("Still few profiles after scroll — calling Voyager API directly...")
        api_result = await page.evaluate("""
            async ({csrfToken, knownUrls}) => {
                const PAGE_SIZE = 50;
                const knownSet = new Set((knownUrls || []).map(u => u.toLowerCase()));
                const hasExisting = knownSet.size > 0;
                let allProfiles = {};
                let start = 0;
                let hitExisting = false;

                while (true) {
                    const connResp = await fetch(
                        '/voyager/api/relationships/dash/connections?count=' + PAGE_SIZE + '&start=' + start + '&q=search&sortType=RECENTLY_ADDED',
                        {
                            headers: {
                                'Accept': 'application/vnd.linkedin.normalized+json+2.1',
                                'csrf-token': csrfToken,
                                'x-restli-protocol-version': '2.0.0',
                            },
                            credentials: 'include',
                        }
                    );
                    if (!connResp.ok) return { ok: false, error: 'connections ' + connResp.status };
                    const connData = JSON.parse(await connResp.text());

                    const memberUrns = (connData.included || [])
                        .filter(e => e.$type && e.$type.toLowerCase().includes('connection') && e.connectedMember)
                        .map(e => e.connectedMember);

                    if (!memberUrns.length) break;

                    const encodedUrns = memberUrns.map(encodeURIComponent).join(',');
                    const encodedIds = memberUrns.map(u => u.split(':').pop()).map(encodeURIComponent).join(',');
                    const endpoints = [
                        '/voyager/api/identity/dash/profiles?ids=List(' + encodedUrns + ')',
                        '/voyager/api/identity/profiles?ids=List(' + encodedIds + ')',
                        '/voyager/api/identity/miniProfiles?ids=List(' + encodedIds + ')',
                    ];

                    let got = false;
                    for (const ep of endpoints) {
                        try {
                            const r = await fetch(ep, {
                                headers: {
                                    'Accept': 'application/vnd.linkedin.normalized+json+2.1',
                                    'csrf-token': csrfToken,
                                    'x-restli-protocol-version': '2.0.0',
                                },
                                credentials: 'include',
                            });
                            const body = await r.text();
                            console.log('Batch profiles ' + ep.split('?')[0] + ' → ' + r.status + ' ' + body.slice(0, 200));
                            if (r.ok) {
                                const profiles = JSON.parse(body);
                                for (const item of (profiles.included || [])) {
                                    if (item && item.publicIdentifier) {
                                        allProfiles[item.publicIdentifier] = item;
                                        const urlNorm = 'linkedin.com/in/' + item.publicIdentifier.toLowerCase();
                                        if (knownSet.has(urlNorm)) hitExisting = true;
                                    }
                                }
                                for (const item of Object.values(profiles.results || {})) {
                                    if (item && item.publicIdentifier) {
                                        allProfiles[item.publicIdentifier] = item;
                                        const urlNorm = 'linkedin.com/in/' + item.publicIdentifier.toLowerCase();
                                        if (knownSet.has(urlNorm)) hitExisting = true;
                                    }
                                }
                                got = true;
                                break;
                            }
                        } catch(e) { console.log('Error ' + ep + ': ' + e.message); }
                    }
                    if (!got) break;

                    console.log('Connection URNs this page: ' + memberUrns.length + ', hitExisting: ' + hitExisting);
                    if (hitExisting || memberUrns.length < PAGE_SIZE) break;
                    if (!hasExisting) break;
                    start += PAGE_SIZE;
                    await new Promise(r => setTimeout(r, 800));
                }

                return { ok: true, profiles: { included: Object.values(allProfiles) } };
            }
        """, {"csrfToken": csrf_token, "knownUrls": known_urls})

        if api_result and api_result.get("ok"):
            profiles_raw = api_result.get("profiles") or {}
            for item in (profiles_raw.get("included") or []):
                if isinstance(item, dict):
                    pid = item.get("publicIdentifier", "")
                    if pid:
                        intercepted_profiles[pid] = item
            for item in (profiles_raw.get("results") or {}).values():
                if isinstance(item, dict):
                    pid = item.get("publicIdentifier", "")
                    if pid:
                        intercepted_profiles[pid] = item

        logger.info("After API fallback: %d profiles collected", len(intercepted_profiles))

    # ── Step 4: Build connections dict (merge old_snapshot + newly fetched) ────
    connections: dict[str, dict] = dict(old_snapshot)

    for profile in intercepted_profiles.values():
        try:
            name = f"{profile.get('firstName', '')} {profile.get('lastName', '')}".strip()
            identifier = profile.get("publicIdentifier", "")
            if not identifier:
                continue
            # Reject invalid names (API glitch, wrong data) — avoid writing "dom" etc to sheet
            if not name or len(name) < 4 or name.lower() in ("dom", "nil", "null", "undefined"):
                logger.warning("Skipping profile %s: invalid name '%s'", identifier, name)
                continue

            url = f"{LINKEDIN_BASE}/in/{identifier}"
            headline = profile.get("headline", "") or ""
            if not isinstance(headline, str):
                headline = ""

            # ── Extract current company ───────────────────────────────────────
            # Priority:
            #   1. Positions captured from intercepted API responses (most reliable)
            #   2. Inline fields on the profile object (currentPositionV2, etc.)
            current_company = ""

            # 1) Check positions captured during scroll
            profile_urn = profile.get("entityUrn") or profile.get("objectUrn", "")
            if profile_urn:
                current_company = intercepted_positions.get(profile_urn, "")

            # 2) Fallback: inline position fields on the profile
            if not current_company:
                for pos_field in (
                    "currentPositionV2", "currentPosition", "positions",
                    "currentPositions", "experience",
                ):
                    pos_data = profile.get(pos_field)
                    if isinstance(pos_data, list) and pos_data:
                        first = pos_data[0]
                        if isinstance(first, dict):
                            current_company = (
                                first.get("companyName")
                                or first.get("company", {}).get("name", "")
                                or ""
                            )
                            if not current_company:
                                # try resolving URN
                                ref = (first.get("*company")
                                       or first.get("companyUrn", "")
                                       or first.get("company", ""))
                                if isinstance(ref, str):
                                    current_company = intercepted_companies.get(ref, "")
                            if current_company:
                                break
                    elif isinstance(pos_data, dict):
                        current_company = pos_data.get("companyName", "")
                        if current_company:
                            break

            logger.info(
                "  %-30s headline='%s' | company='%s'",
                name, headline[:50], current_company,
            )

            if url not in connections:
                connections[url] = {
                    "name": name,
                    "headline": headline,
                    "current_company": current_company,
                    "url": url,
                }
        except Exception as e:
            logger.debug("Error building connection from profile: %s", e)

    logger.info("Total connections scraped: %d", len(connections))
    return connections


# ─── Profile company lookup ──────────────────────────────────────────────────

async def get_csrf_token(page: Page) -> str:
    """Return the CSRF token (JSESSIONID cookie value) for the current session."""
    cookies = await page.context.cookies()
    return next(
        (c["value"].strip('"') for c in cookies if c["name"] == "JSESSIONID"),
        "",
    )


async def get_profile_company(page: Page, public_identifier: str, csrf_token: str) -> str:
    """
    Fetches the person's current employer from their LinkedIn work experience.
    Tries four endpoints in order — stops at the first that returns data.
    Returns "" on any error or if no position data is found.
    All attempts are logged via console.log (captured by the page's console listener).
    """
    result = await page.evaluate("""
        async ([publicId, csrfToken]) => {
            const headers = {
                'Accept': 'application/vnd.linkedin.normalized+json+2.1',
                'csrf-token': csrfToken,
                'x-restli-protocol-version': '2.0.0',
            };

            function extractCompany(included) {
                if (!Array.isArray(included)) return '';

                // Build a company URN → name lookup from Company/MiniCompany objects
                const companies = {};
                for (const e of included) {
                    if (!e || !e.$type) continue;
                    const t = e.$type.toLowerCase();
                    if (t.includes('company') || t.includes('minicompany')) {
                        const urn = e.entityUrn || e.objectUrn || '';
                        if (urn && e.name) companies[urn] = e.name;
                    }
                }

                // Find Position / WorkExperience objects
                const positions = included.filter(e =>
                    e && e.$type && (
                        e.$type.toLowerCase().includes('position') ||
                        e.$type.toLowerCase().includes('workexperience')
                    )
                );
                if (!positions.length) return '';

                // Prefer a current position (no end date)
                const current = positions.find(
                    p => !p.timePeriod?.endDate && !p.dateRange?.end
                );
                const best = current || positions[0];
                if (!best) return '';

                // 1) Direct string field
                if (best.companyName) return best.companyName;
                // 2) Nested company object
                if (best.company && typeof best.company === 'object' && best.company.name)
                    return best.company.name;
                // 3) Resolve *company URN reference (LinkedIn normalized JSON)
                const ref = best['*company'] || best.companyUrn
                    || (typeof best.company === 'string' ? best.company : '');
                if (ref) return companies[ref] || '';

                return '';
            }

            // ── Attempt 1: dedicated /positions endpoint ───────────────────────
            try {
                const r = await fetch(
                    '/voyager/api/identity/profiles/' + publicId + '/positions',
                    { headers, credentials: 'include' }
                );
                const body = await r.text();
                console.log('[enrich1] ' + publicId + ' → ' + r.status + ' ' + body.slice(0, 120));
                if (r.ok) {
                    const d = JSON.parse(body);
                    const c = extractCompany(d.included || [])
                           || extractCompany(d.elements  || []);
                    if (c) { console.log('[enrich1] company=' + c); return c; }
                    console.log('[enrich1] no company in response');
                }
            } catch(e) { console.log('[enrich1] error: ' + e.message); }

            // ── Attempt 2: full profile (no projection) ────────────────────────
            // Returns a large normalized response — positions are in included[]
            try {
                const r = await fetch(
                    '/voyager/api/identity/profiles/' + publicId,
                    { headers, credentials: 'include' }
                );
                const body = await r.text();
                console.log('[enrich2] ' + publicId + ' → ' + r.status + ' ' + body.slice(0, 120));
                if (r.ok) {
                    const d = JSON.parse(body);
                    const c = extractCompany(d.included || []);
                    if (c) { console.log('[enrich2] company=' + c); return c; }
                    // Also check positionView (legacy field)
                    const pv = d.positionView || (d.data && d.data.positionView);
                    if (pv && pv.elements && pv.elements.length) {
                        const first = pv.elements[0];
                        const c2 = first.companyName
                               || (first.company && first.company.name)
                               || '';
                        if (c2) { console.log('[enrich2-pv] company=' + c2); return c2; }
                    }
                    console.log('[enrich2] no company in response');
                }
            } catch(e) { console.log('[enrich2] error: ' + e.message); }

            // ── Attempt 3: dash profile by vanityName ──────────────────────────
            // This is the endpoint LinkedIn's own SPA uses when you open a profile
            try {
                const r = await fetch(
                    '/voyager/api/identity/dash/profiles?q=viewee&vieweeVanityName=' + publicId,
                    { headers, credentials: 'include' }
                );
                const body = await r.text();
                console.log('[enrich3] ' + publicId + ' → ' + r.status + ' ' + body.slice(0, 180));
                if (r.ok) {
                    const d = JSON.parse(body);
                    const c = extractCompany(d.included || []);
                    if (c) { console.log('[enrich3] company=' + c); return c; }
                    console.log('[enrich3] no company in response');
                }
            } catch(e) { console.log('[enrich3] error: ' + e.message); }

            // ── Attempt 4: profile with profileView projection ─────────────────
            try {
                const r = await fetch(
                    '/voyager/api/identity/profiles/' + publicId +
                    '?projection=(profileView)',
                    { headers, credentials: 'include' }
                );
                const body = await r.text();
                console.log('[enrich4] ' + publicId + ' → ' + r.status + ' ' + body.slice(0, 180));
                if (r.ok) {
                    const d = JSON.parse(body);
                    const c = extractCompany(d.included || []);
                    if (c) { console.log('[enrich4] company=' + c); return c; }
                    console.log('[enrich4] no company in response');
                }
            } catch(e) { console.log('[enrich4] error: ' + e.message); }

            console.log('[enrich] all 4 attempts failed for ' + publicId);
            return '';
        }
    """, [public_identifier, csrf_token])
    return result or ""


# ─── Snapshot management ─────────────────────────────────────────────────────

def load_snapshot() -> dict[str, dict]:
    p = Path(CONNECTIONS_SNAPSHOT)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def save_snapshot(connections: dict[str, dict]):
    Path(CONNECTIONS_SNAPSHOT).write_text(json.dumps(connections, indent=2))
    logger.info("Snapshot saved: %d connections", len(connections))


def diff_connections(
    old: dict[str, dict],
    new: dict[str, dict],
) -> list[dict]:
    """Returns connections in `new` that aren't in `old`."""
    new_keys = set(new.keys()) - set(old.keys())
    return [new[k] for k in new_keys]


# ─── Send DM ─────────────────────────────────────────────────────────────────

async def send_message(page: Page, profile_url: str, message: str) -> bool:
    """
    Opens a LinkedIn profile and sends a DM via the Message button.
    Returns True on success, False on failure.

    Note: LinkedIn does NOT support file attachments in regular DMs.
    Include the resume as a Google Drive link inside the message text.
    """
    logger.info("Sending DM to %s", profile_url)
    try:
        # Longer timeout for typing long messages (default 30s can be too short)
        page.set_default_timeout(60000)
        # Fresh page per send — navigate directly to profile (no stale message panel)
        await page.goto(profile_url, wait_until="domcontentloaded")
        await _pause(2, 4)

        # Disable hidden interop iframe that intercepts clicks (opacity:0, full viewport)
        # Without this, clicks hit the iframe instead of Message button → goes to Learning
        await page.evaluate("""
            () => {
                const iframe = document.querySelector('[data-testid="interop-iframe"]');
                if (iframe) {
                    iframe.style.pointerEvents = 'none';
                    iframe.style.visibility = 'hidden';
                }
            }
        """)
        await _pause(0.5, 1)

        # Dismiss overlays that block the Message button (cookie banner, prompts, etc.)
        for selector in [
            "button[aria-label='Dismiss']",
            "button:has-text('Accept'), button:has-text('Accept all')",
            "[data-test-modal-close-btn]",
            ".artdeco-modal__dismiss",
        ]:
            try:
                dismiss = await page.query_selector(selector)
                if dismiss:
                    await dismiss.click(timeout=2000)
                    await _pause(0.5, 1)
                    break
            except Exception:
                pass

        # Click the "Message" button — scoped to profile main (data-sdui-screen="...Profile")
        # Hidden iframe was intercepting clicks; we disabled it above
        msg_btn = None
        for sel in [
            '[data-sdui-screen="com.linkedin.sdui.flagshipnav.profile.Profile"] button:has-text("Message")',
            '[data-sdui-screen="com.linkedin.sdui.flagshipnav.profile.Profile"] a:has-text("Message")',
            "div.pv-top-card a.message-anywhere-button",
            "div.pv-top-card-v2-ctas a:has-text('Message')",
            ".pv-top-card button:has-text('Message')",
            ".pv-top-card a:has-text('Message')",
        ]:
            try:
                msg_btn = await page.query_selector(sel)
                if msg_btn:
                    href = await msg_btn.get_attribute("href") or ""
                    if "learning" in href.lower():
                        msg_btn = None
                        continue
                    break
            except Exception:
                continue

        if not msg_btn:
            msg_btn = await page.query_selector("button:has-text('Message'):not([href*='learning']), a:has-text('Message'):not([href*='learning'])")
        if not msg_btn:
            logger.warning("No Message button found on %s", profile_url)
            return False

        await msg_btn.evaluate("el => el.scrollIntoView({block: 'center'})")
        await _pause(0.3, 0.5)
        # Use JS click to bypass any remaining overlay issues
        await msg_btn.evaluate("el => el.click()")
        await _pause(3, 5)  # Panel animates in — wait for it

        # Disable iframe again (panel may have re-rendered it)
        await page.evaluate("""
            () => {
                const iframe = document.querySelector('[data-testid="interop-iframe"]');
                if (iframe) { iframe.style.pointerEvents = 'none'; iframe.style.visibility = 'hidden'; }
            }
        """)

        # Scroll message overlay into view (panel may be off-screen)
        await page.evaluate("""
            () => {
                const overlay = document.querySelector('.msg-overlay-conversation-bubble, .msg-overlay, [data-test-id="msg-overlay"]');
                if (overlay) overlay.scrollIntoView({block: 'center'});
            }
        """)
        await _pause(0.5, 1)

        # Type message in the composer
        composer = None
        for sel in [
            ".msg-form__contenteditable",
            ".msg-form [contenteditable='true']",
            "[data-placeholder='Write a message']",
            "[placeholder='Write a message']",
            "[data-placeholder*='Write']",
            "[placeholder*='message']",
            ".msg-overlay-conversation-bubble [contenteditable='true']",
            "div[contenteditable='true'][aria-label]",
            "[role='textbox']",
        ]:
            try:
                composer = await page.wait_for_selector(sel, timeout=3000)
                if composer:
                    break
            except Exception:
                continue

        if not composer:
            logger.warning("Message composer not found for %s", profile_url)
            return False

        await composer.evaluate("el => el.scrollIntoView({block: 'center'})")
        await _pause(0.3, 0.5)
        await composer.evaluate("el => { el.focus(); el.click(); }")
        await _pause(0.5, 1)

        # Type message — use JS insertText as fallback (works with contenteditable + iframe)
        try:
            await composer.type(message, delay=15)
        except Exception:
            # Fallback: JS for contenteditable (bypasses iframe/focus issues)
            await composer.evaluate(
                """(el, msg) => {
                    el.focus();
                    el.innerText = msg;
                    el.dispatchEvent(new InputEvent('input', { bubbles: true }));
                }""",
                message,
            )
        await _pause(1, 2)

        # Click Send
        send_btn = await page.query_selector(
            "button.msg-form__send-button, "
            "button[type='submit']:has-text('Send'), "
            "[aria-label='Send']"
        )
        if not send_btn:
            logger.warning("Send button not found for %s", profile_url)
            return False

        await send_btn.evaluate("el => el.scrollIntoView({block: 'center'})")
        await _pause(0.3, 0.5)
        await send_btn.evaluate("el => el.click()")
        await _pause(2, 3)
        logger.info("Message sent to %s", profile_url)
        return True

    except Exception as e:
        logger.error("Failed to send message to %s: %s", profile_url, e)
        return False


# ─── Browser factory (used by main.py) ───────────────────────────────────────

async def make_browser_context(playwright):
    """Creates a headless browser context."""
    browser = await playwright.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1440, "height": 900},
    )
    # Mask navigator.webdriver flag
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, context
