"""
AI Employee Gold Tier - Facebook & Instagram Watcher
=====================================================

Monitors Facebook Messenger + notifications AND Instagram DMs + activity
for business lead keywords and saves actionable items as Markdown notes
in /Needs Action.

Both platforms are handled by one shared Chromium session stored in
/session/facebook. Since Facebook and Instagram share Meta's login
infrastructure, a single browser context can be authenticated to both.

KEYWORDS MONITORED: sales, client, project

PLATFORMS MONITORED:
  Facebook  - Messenger (DMs) + Notifications feed
  Instagram - Direct Messages (DMs) + Activity notifications

INSTALL:
    pip install playwright
    playwright install chromium

SESSION / LOGIN:
    - On first run, a visible browser window opens so you can log in to
      Facebook manually. Complete any 2FA prompts in the browser window.
    - Then navigate to instagram.com and log in there as well.
    - The session (cookies for both sites) is saved persistently in
      /session/facebook. Future runs start headless automatically.
    - If session expires, delete /session/facebook and repeat.

RUN (direct):
    python watchers/facebook_instagram_watcher.py

RUN with PM2:
    pm2 start watchers/facebook_instagram_watcher.py --interpreter python --name fb-ig-watcher
    pm2 save
    pm2 startup

PM2 MANAGEMENT:
    pm2 status                   # Check if running
    pm2 logs fb-ig-watcher       # View live logs
    pm2 restart fb-ig-watcher    # Restart watcher
    pm2 stop fb-ig-watcher       # Stop watcher

TEST:
    1. Ask a Facebook/Instagram contact to send you a DM containing:
       "sales", "client", or "project"
    2. Leave it unread
    3. Within 60 seconds a .md file should appear in /Needs Action:
         FACEBOOK_[timestamp]_[sender].md
         INSTAGRAM_[timestamp]_[sender].md
    4. Check the console log for MATCH output.

NOTES:
    - Facebook and Instagram may show CAPTCHA or identity checks on first login.
      Complete them manually before the 3-minute timeout expires.
    - This watcher reads messages/notifications only. It does NOT post,
      like, comment, or interact with any content.
    - Meta's DOM changes frequently. Selectors are written defensively
      with multiple fallbacks. If scraping breaks, inspect the live page
      and update the selectors in the SELECTORS section below.
    - Run headless=False manually for debugging:
        HEADLESS=0 python watchers/facebook_instagram_watcher.py

"""

import os
import sys
import time
import re
import traceback
from datetime import datetime
from pathlib import Path

# --- Gold Tier Error Recovery ---
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "Skills"
sys.path.insert(0, str(_SKILLS_DIR))
from error_recovery import with_retry, log_watcher_error  # noqa: E402

# --- Dependency Check ---
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("ERROR: Playwright not installed.")
    print("Run: pip install playwright && playwright install chromium")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR         = Path(__file__).resolve().parent.parent
NEEDS_ACTION_DIR = BASE_DIR / "Needs Action"
SESSION_DIR      = BASE_DIR / "session" / "facebook"   # shared for FB + IG

CHECK_INTERVAL   = 60          # seconds between full scan cycles
KEYWORDS         = ["sales", "client", "project"]
MAX_ITEMS_PER_SCAN = 25        # cap on items scraped per source per cycle
SESSION_REFRESH  = 20          # reload home page every N cycles to keep session alive

# Override headless mode via environment variable for debugging
HEADLESS_OVERRIDE = os.environ.get("HEADLESS", "").strip()

# URLs
FB_HOME_URL          = "https://www.facebook.com/"
FB_MESSAGES_URL      = "https://www.facebook.com/messages/"
FB_NOTIFICATIONS_URL = "https://www.facebook.com/notifications/"
IG_HOME_URL          = "https://www.instagram.com/"
IG_DM_URL            = "https://www.instagram.com/direct/inbox/"
IG_ACTIVITY_URL      = "https://www.instagram.com/notifications/"   # legacy; activity is on home

LOGIN_WAIT_TIMEOUT = 180_000   # ms - time for manual login on first run
PAGE_LOAD_TIMEOUT  = 45_000    # ms
ELEMENT_TIMEOUT    = 8_000     # ms - selector wait


# ---------------------------------------------------------------------------
# Keyword / Priority Helpers
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def contains_keyword(text: str) -> str | None:
    """Return the first matched keyword (case-insensitive), or None."""
    text_lower = text.lower()
    for kw in KEYWORDS:
        if kw in text_lower:
            return kw
    return None


def priority_from_keyword(keyword: str) -> str:
    return {
        "sales":   "high",
        "client":  "high",
        "project": "medium",
    }.get(keyword, "medium")


def sanitize_filename(text: str) -> str:
    text = re.sub(r'[\\/*?:"<>|]', "_", text)
    return text[:50].strip()


def generate_summary(sender: str, content: str, keyword: str,
                     platform: str, content_type: str) -> str:
    """
    Produce a concise human-readable summary of the message/post.
    Extracts first two meaningful sentences and highlights the keyword match.
    """
    # Strip HTML-like artifacts and normalize whitespace
    clean = re.sub(r'\s+', ' ', content).strip()

    # Extract up to two sentences (split on . ! ?)
    sentences = re.split(r'(?<=[.!?])\s+', clean)
    excerpt = " ".join(sentences[:2]).strip()
    if len(excerpt) > 300:
        excerpt = excerpt[:297] + "..."

    kw_note = f"Keyword '{keyword}' detected - possible {keyword} lead."

    return (
        f"[{platform.upper()} {content_type.upper()}] from {sender}: "
        f"{excerpt} | {kw_note}"
    )


# ---------------------------------------------------------------------------
# /Needs Action Writer
# ---------------------------------------------------------------------------

def save_needs_action(
    platform: str,      # "facebook" or "instagram"
    content_type: str,  # "message" or "notification" or "post"
    sender: str,
    content: str,
    keyword: str,
    priority: str,
    summary: str,
) -> Path:
    """Write a structured Markdown note to /Needs Action."""
    NEEDS_ACTION_DIR.mkdir(parents=True, exist_ok=True)

    ts           = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_sender  = sanitize_filename(sender)
    prefix       = "FACEBOOK" if platform == "facebook" else "INSTAGRAM"
    filename     = f"{prefix}_{ts}_{safe_sender}.md"
    filepath     = NEEDS_ACTION_DIR / filename
    received     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    content_preview = content[:600].replace('"', "'")
    summary_clean   = summary.replace('"', "'")

    file_content = f"""---
type: {platform}_{content_type}
platform: {platform}
content_type: {content_type}
from: "{sender}"
subject: "{platform.title()} {content_type} from {sender}"
received: "{received}"
keyword_matched: "{keyword}"
priority: {priority}
status: pending
summary: "{summary_clean}"
---

# {platform.title()} {content_type.title()}: {sender}

| Field         | Value |
|---------------|-------|
| Platform      | {platform.title()} |
| Type          | {content_type.title()} |
| From          | {sender} |
| Received      | {received} |
| Priority      | {priority.upper()} |
| Keyword       | `{keyword}` |

## AI Summary

> {summary}

## Full Content Preview

```
{content_preview}
```

## Next Steps

- [ ] Review the {content_type} from {sender} on {platform.title()}
- [ ] Draft a response if this is a qualified lead (use Skill 6: Social Summary Generator)
- [ ] Qualify as lead or archive
- [ ] Move to Done when resolved

## Skill Integration

Run the Social Summary Generator to draft a response:
```
python Skills/social_summary_generator.py --file "Needs Action/{filename}"
```
"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(file_content)

    return filepath


# ---------------------------------------------------------------------------
# Session Helpers
# ---------------------------------------------------------------------------

def session_exists() -> bool:
    """Return True if a saved browser session exists."""
    return SESSION_DIR.exists() and any(SESSION_DIR.iterdir())


def wait_for_login(page, home_url: str, platform: str,
                   nav_selectors: list[str]) -> bool:
    """
    Navigate to home_url and wait for any of nav_selectors to appear,
    signalling a successful manual login. Returns True on success.
    """
    try:
        page.goto(home_url, timeout=PAGE_LOAD_TIMEOUT)
    except Exception:
        pass

    log(f"Please log in to {platform} in the browser window (3 minutes)...")

    deadline = LOGIN_WAIT_TIMEOUT
    for selector in nav_selectors:
        try:
            page.wait_for_selector(selector, timeout=deadline)
            log(f"{platform} login detected!")
            return True
        except PlaywrightTimeout:
            continue

    return False


def is_logged_in(page, url: str, nav_selectors: list[str]) -> bool:
    """Check if already logged in by navigating to url and checking selectors."""
    try:
        page.goto(url, timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        for selector in nav_selectors:
            try:
                page.wait_for_selector(selector, timeout=ELEMENT_TIMEOUT)
                return True
            except PlaywrightTimeout:
                continue
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Facebook: Login Check
# ---------------------------------------------------------------------------

FB_NAV_SELECTORS = [
    'div[role="navigation"]',
    'div[aria-label="Facebook"]',
    'a[href="/messages/"]',
    'svg[aria-label="Facebook"]',
    'div[data-pagelet="LeftRail"]',
]


def fb_is_logged_in(page) -> bool:
    try:
        page.goto(FB_HOME_URL, timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)
        for sel in FB_NAV_SELECTORS:
            try:
                page.wait_for_selector(sel, timeout=ELEMENT_TIMEOUT)
                return True
            except PlaywrightTimeout:
                continue
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Facebook: Messenger Scraper
# ---------------------------------------------------------------------------

def scrape_fb_messages(page) -> list[dict]:
    """
    Navigate to Facebook Messenger and extract unread/recent DM previews.
    Returns list of {sender, preview, content_type}.
    """
    results = []

    try:
        page.goto(FB_MESSAGES_URL, timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(3)  # Messenger can be slow

        # --- Attempt 1: conversation list rows ---
        conv_rows = page.query_selector_all(
            'div[role="row"], '
            'div[data-testid="mwthreadlist-item"], '
            'li[data-testid="conversation"]'
        )

        if not conv_rows:
            # Attempt 2: aria-based list
            conv_rows = page.query_selector_all(
                'div[aria-label*="conversation"], '
                'div[aria-label*="Conversation"]'
            )

        if not conv_rows:
            # Attempt 3: grab all list items inside the sidebar
            conv_rows = page.query_selector_all('div[role="list"] > div')

        for row in conv_rows[:MAX_ITEMS_PER_SCAN]:
            try:
                text = row.inner_text().strip()
                if not text or len(text) < 3:
                    continue

                lines = [l.strip() for l in text.split("\n") if l.strip()]
                sender  = lines[0] if lines else "Facebook User"
                preview = " ".join(lines[1:3]) if len(lines) > 1 else ""

                # Skip obvious UI elements
                if sender.lower() in ("new message", "search", "messenger", "chats"):
                    continue

                results.append({
                    "sender":       sender[:80],
                    "preview":      preview[:400],
                    "content_type": "message",
                })
            except Exception:
                continue

    except Exception as exc:
        log(f"WARN: Could not scrape Facebook messages - {exc}")
        log_watcher_error("facebook_instagram", exc, context="scrape_fb_messages")

    return results


# ---------------------------------------------------------------------------
# Facebook: Notifications Scraper
# ---------------------------------------------------------------------------

def scrape_fb_notifications(page) -> list[dict]:
    """
    Navigate to Facebook Notifications and extract recent notification text.
    Returns list of {sender, preview, content_type}.
    """
    results = []

    try:
        page.goto(FB_NOTIFICATIONS_URL, timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)

        # Notification items
        notif_items = page.query_selector_all(
            'div[data-testid="notif_list_item"], '
            'div[role="article"], '
            'li[data-ft]'
        )

        if not notif_items:
            # Fallback: any clickable list items in the main content area
            notif_items = page.query_selector_all(
                'div[role="main"] div[role="listitem"], '
                'div[data-pagelet="Notif"] div[role="article"]'
            )

        for item in notif_items[:MAX_ITEMS_PER_SCAN]:
            try:
                text = item.inner_text().strip()
                if not text or len(text) < 5:
                    continue

                lines = [l.strip() for l in text.split("\n") if l.strip()]
                sender  = lines[0] if lines else "Facebook"
                preview = " ".join(lines[:3])

                results.append({
                    "sender":       sender[:80],
                    "preview":      preview[:400],
                    "content_type": "notification",
                })
            except Exception:
                continue

    except Exception as exc:
        log(f"WARN: Could not scrape Facebook notifications - {exc}")
        log_watcher_error("facebook_instagram", exc, context="scrape_fb_notifications")

    return results


# ---------------------------------------------------------------------------
# Instagram: Login Check
# ---------------------------------------------------------------------------

IG_NAV_SELECTORS = [
    'svg[aria-label="Home"]',
    'nav[role="navigation"]',
    'a[href="/direct/inbox/"]',
    'div[role="dialog"]',       # only present after login
    'span[aria-label="Home"]',
]


def ig_is_logged_in(page) -> bool:
    try:
        page.goto(IG_HOME_URL, timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)
        for sel in IG_NAV_SELECTORS:
            try:
                page.wait_for_selector(sel, timeout=ELEMENT_TIMEOUT)
                return True
            except PlaywrightTimeout:
                continue
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Instagram: DM Inbox Scraper
# ---------------------------------------------------------------------------

def scrape_ig_dms(page) -> list[dict]:
    """
    Navigate to Instagram Direct Inbox and extract DM previews.
    Returns list of {sender, preview, content_type}.
    """
    results = []

    try:
        page.goto(IG_DM_URL, timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(3)

        # Instagram DM thread list
        thread_items = page.query_selector_all(
            'div[role="listitem"], '
            'div[class*="x9f619"]'   # IG uses generated class names; role is more stable
        )

        if not thread_items:
            # Fallback: any links inside the DM sidebar
            thread_items = page.query_selector_all(
                'div[role="list"] > div[role="listitem"]'
            )

        for item in thread_items[:MAX_ITEMS_PER_SCAN]:
            try:
                text = item.inner_text().strip()
                if not text or len(text) < 3:
                    continue

                lines = [l.strip() for l in text.split("\n") if l.strip()]
                sender  = lines[0] if lines else "Instagram User"
                preview = " ".join(lines[1:3]) if len(lines) > 1 else ""

                # Skip UI chrome elements
                if sender.lower() in ("requests", "search", "direct", "new message",
                                      "message", "messages"):
                    continue

                results.append({
                    "sender":       sender[:80],
                    "preview":      preview[:400],
                    "content_type": "message",
                })
            except Exception:
                continue

    except Exception as exc:
        log(f"WARN: Could not scrape Instagram DMs - {exc}")
        log_watcher_error("facebook_instagram", exc, context="scrape_ig_dms")

    return results


# ---------------------------------------------------------------------------
# Instagram: Activity / Notifications Scraper
# ---------------------------------------------------------------------------

def scrape_ig_activity(page) -> list[dict]:
    """
    Navigate to Instagram home and check for notifications.
    Instagram's activity feed is accessed via the heart/notification icon.
    Returns list of {sender, preview, content_type}.
    """
    results = []

    try:
        page.goto(IG_HOME_URL, timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)

        # Try clicking the notifications/heart icon to open the activity panel
        notif_btn = None
        for sel in [
            'a[href="/notifications/"]',
            'svg[aria-label="Notifications"]',
            'svg[aria-label="Activity"]',
            'a[href="/explore/"]',    # fallback anchor nearby
        ]:
            try:
                notif_btn = page.query_selector(sel)
                if notif_btn:
                    break
            except Exception:
                continue

        if notif_btn:
            try:
                notif_btn.click()
                time.sleep(2)
            except Exception:
                pass

        # Grab visible notification items
        notif_items = page.query_selector_all(
            'div[role="dialog"] div[role="listitem"], '
            'section[aria-label*="activity"] div[role="listitem"], '
            'section div[class*="notification"]'
        )

        for item in notif_items[:MAX_ITEMS_PER_SCAN]:
            try:
                text = item.inner_text().strip()
                if not text or len(text) < 5:
                    continue
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                sender  = lines[0] if lines else "Instagram"
                preview = " ".join(lines[:3])
                results.append({
                    "sender":       sender[:80],
                    "preview":      preview[:400],
                    "content_type": "notification",
                })
            except Exception:
                continue

    except Exception as exc:
        log(f"WARN: Could not scrape Instagram activity - {exc}")
        log_watcher_error("facebook_instagram", exc, context="scrape_ig_activity")

    return results


# ---------------------------------------------------------------------------
# Core Scanner
# ---------------------------------------------------------------------------

def scan_platform(
    page,
    platform: str,
    items: list[dict],
    seen_keys: set,
) -> set:
    """
    Check a list of {sender, preview, content_type} items for keywords.
    Write /Needs Action files for matches. Return updated seen_keys set.
    """
    new_seen = set(seen_keys)
    matches  = 0

    for item in items:
        sender       = item["sender"]
        preview      = item["preview"]
        content_type = item["content_type"]

        combined = f"{sender} {preview}"
        keyword  = contains_keyword(combined)

        if not keyword:
            continue

        # Deduplicate: same sender + keyword within the same hour
        hour_key = datetime.now().strftime("%Y%m%d_%H")
        dedup    = f"{platform}:{content_type}:{sender}:{keyword}:{hour_key}"
        if dedup in new_seen:
            log(f"  SKIP (already logged this hour): [{platform}] {sender}")
            continue

        priority = priority_from_keyword(keyword)
        summary  = generate_summary(sender, preview, keyword, platform, content_type)

        filepath = save_needs_action(
            platform     = platform,
            content_type = content_type,
            sender       = sender,
            content      = preview,
            keyword      = keyword,
            priority     = priority,
            summary      = summary,
        )

        new_seen.add(dedup)
        matches += 1
        log(f"  MATCH [{keyword.upper()}] [{platform}/{content_type}] '{sender}' -> {filepath.name}")

    if matches == 0 and items:
        log(f"  No keyword matches in {len(items)} {platform} item(s).")

    return new_seen


def run_scan_cycle(page, seen_keys: set) -> set:
    """
    Full scan of Facebook + Instagram. Returns updated seen_keys.
    """
    new_seen = set(seen_keys)

    # --- Facebook Messages ---
    log("Checking Facebook Messenger...")
    fb_msgs = scrape_fb_messages(page)
    log(f"  Fetched {len(fb_msgs)} conversation(s).")
    new_seen = scan_platform(page, "facebook", fb_msgs, new_seen)

    # --- Facebook Notifications ---
    log("Checking Facebook Notifications...")
    fb_notifs = scrape_fb_notifications(page)
    log(f"  Fetched {len(fb_notifs)} notification(s).")
    new_seen = scan_platform(page, "facebook", fb_notifs, new_seen)

    # --- Instagram DMs ---
    log("Checking Instagram DMs...")
    ig_dms = scrape_ig_dms(page)
    log(f"  Fetched {len(ig_dms)} DM thread(s).")
    new_seen = scan_platform(page, "instagram", ig_dms, new_seen)

    # --- Instagram Activity ---
    log("Checking Instagram Activity...")
    ig_activity = scrape_ig_activity(page)
    log(f"  Fetched {len(ig_activity)} activity item(s).")
    new_seen = scan_platform(page, "instagram", ig_activity, new_seen)

    return new_seen


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  AI Employee Gold Tier - Facebook & Instagram Watcher")
    print("=" * 60)
    print()
    log(f"BASE DIR    : {BASE_DIR}")
    log(f"SESSION     : {SESSION_DIR}")
    log(f"OUTPUT      : {NEEDS_ACTION_DIR}")
    log(f"KEYWORDS    : {', '.join(KEYWORDS)}")
    log(f"INTERVAL    : {CHECK_INTERVAL}s")
    print()

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    NEEDS_ACTION_DIR.mkdir(parents=True, exist_ok=True)

    first_login = not session_exists()

    # Headless logic: visible browser on first login, headless after
    if HEADLESS_OVERRIDE == "0":
        headless = False
        log("HEADLESS=0 override: running with visible browser.")
    elif HEADLESS_OVERRIDE == "1":
        headless = True
        log("HEADLESS=1 override: running headless.")
    else:
        headless = not first_login

    if first_login:
        log("No existing session. Opening browser for manual login...")
        log("  Step 1: Log in to Facebook. Step 2: Log in to Instagram.")
        log("  You have 3 minutes for each platform.")
    else:
        log("Existing session found. Launching in headless mode...")

    seen_keys: set = set()
    run = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            user_data_dir = str(SESSION_DIR),
            headless      = headless,
            args          = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport      = {"width": 1440, "height": 900},
            user_agent    = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )

        page = browser.new_page()

        # ── First-run login flow ─────────────────────────────────────────
        if first_login:
            # Facebook login
            log("--- FACEBOOK LOGIN ---")
            fb_ok = wait_for_login(
                page, FB_HOME_URL, "Facebook", FB_NAV_SELECTORS
            )
            if not fb_ok:
                log("ERROR: Facebook login not completed within timeout.")
                log("Delete /session/facebook and try again.")
                browser.close()
                sys.exit(1)
            log("Facebook session established.")

            # Instagram login
            log("--- INSTAGRAM LOGIN ---")
            ig_ok = wait_for_login(
                page, IG_HOME_URL, "Instagram", IG_NAV_SELECTORS
            )
            if not ig_ok:
                log("WARN: Instagram login timed out. Instagram scraping may fail.")
                log("Delete /session/facebook and re-run to retry both logins.")
            else:
                log("Instagram session established.")

            log("Both sessions saved to: " + str(SESSION_DIR))

        # ── Session validation ───────────────────────────────────────────
        else:
            log("Validating Facebook session...")
            if not fb_is_logged_in(page):
                log("WARN: Facebook session expired. Delete /session/facebook and re-run.")

            log("Validating Instagram session...")
            if not ig_is_logged_in(page):
                log("WARN: Instagram session expired. Delete /session/facebook and re-run.")

        print("-" * 60)

        # ── Main poll loop ───────────────────────────────────────────────
        try:
            while True:
                run += 1
                log(f"--- Poll #{run} ---")

                try:
                    seen_keys = run_scan_cycle(page, seen_keys)
                except Exception as poll_err:
                    tb = traceback.format_exc()
                    log(f"ERROR: Poll #{run} failed — {poll_err}. Logging and continuing...")
                    err_path = log_watcher_error(
                        "facebook_instagram", poll_err,
                        context=f"run_scan_cycle poll #{run}", tb=tb,
                    )
                    log(f"  Error logged to: {err_path.name}")

                # Periodic session refresh: go back to Facebook home
                if run % SESSION_REFRESH == 0:
                    log("Refreshing session (navigating to Facebook home)...")
                    try:
                        page.goto(FB_HOME_URL, timeout=PAGE_LOAD_TIMEOUT)
                        page.wait_for_load_state("domcontentloaded")
                    except Exception as exc:
                        log(f"WARN: Session refresh failed - {exc}")
                        log_watcher_error("facebook_instagram", exc, context="session refresh")

                log(f"Sleeping {CHECK_INTERVAL}s until next check...")
                time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print()
            log("Shutdown requested by user.")

        except Exception as exc:
            tb = traceback.format_exc()
            log(f"FATAL ERROR: {exc}")
            log_watcher_error("facebook_instagram", exc, context="fatal error in main loop", tb=tb)
            raise

        finally:
            browser.close()
            log("Facebook & Instagram watcher stopped.")
            print("=" * 60)


if __name__ == "__main__":
    main()
