"""
AI Employee Silver Tier - LinkedIn Watcher
============================================

Monitors LinkedIn messages and notifications for business lead keywords
and saves actionable items as Markdown notes in /Needs Action.

KEYWORDS MONITORED: sales, client, project

INSTALL:
    pip install playwright
    playwright install chromium

SESSION / LOGIN:
    - On first run, a visible browser window opens so you can log in to LinkedIn manually.
    - Your session is saved persistently in /session/linkedin.
    - Future runs start headless automatically (no login required).
    - If session expires or LinkedIn logs you out, delete /session/linkedin and re-login.

RUN (direct):
    python watchers/linkedin_watcher.py

RUN with PM2:
    pm2 start watchers/linkedin_watcher.py --interpreter python --name linkedin-watcher
    pm2 save
    pm2 startup

PM2 MANAGEMENT:
    pm2 status                        # Check if running
    pm2 logs linkedin-watcher         # View live logs
    pm2 restart linkedin-watcher      # Restart watcher
    pm2 stop linkedin-watcher         # Stop watcher

TEST:
    1. Ask a LinkedIn connection to send you a message containing:
       "sales", "client", or "project"
    2. Or create a test notification by posting something and commenting.
    3. Within 60 seconds a .md file should appear in /Needs Action.
    4. Check the console log for MATCH output.

NOTES:
    - LinkedIn may show CAPTCHA or 2FA challenges on first login.
      Complete them manually in the browser window before the timeout.
    - This watcher reads messages and notifications pages only.
      It does NOT post, like, or interact with LinkedIn content.
    - LinkedIn's DOM changes frequently; selectors are written defensively
      with multiple fallbacks. If scraping breaks, check for updates.

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

BASE_DIR = Path(__file__).resolve().parent.parent
NEEDS_ACTION_DIR = BASE_DIR / "Needs Action"
SESSION_DIR = BASE_DIR / "session" / "linkedin"

CHECK_INTERVAL = 60  # seconds
KEYWORDS = ["sales", "client", "project"]

LINKEDIN_MESSAGES_URL = "https://www.linkedin.com/messaging/"
LINKEDIN_NOTIFICATIONS_URL = "https://www.linkedin.com/notifications/"
LINKEDIN_HOME_URL = "https://www.linkedin.com/feed/"

LOGIN_WAIT_TIMEOUT = 180_000   # ms — time for manual login
PAGE_LOAD_TIMEOUT = 45_000     # ms


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def contains_keyword(text: str) -> str | None:
    """Return first matched keyword (case-insensitive), or None."""
    text_lower = text.lower()
    for kw in KEYWORDS:
        if kw in text_lower:
            return kw
    return None


def priority_from_keyword(keyword: str) -> str:
    return {"sales": "high", "client": "high", "project": "medium"}.get(keyword, "medium")


def sanitize_filename(text: str) -> str:
    text = re.sub(r'[\\/*?:"<>|]', "_", text)
    return text[:60].strip()


def save_needs_action(source: str, sender: str, message: str,
                      keyword: str, priority: str, lead_type: str) -> Path:
    """Write a Markdown note to /Needs Action."""
    NEEDS_ACTION_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_sender = sanitize_filename(sender)
    filename = f"LINKEDIN_{ts}_{safe_sender}.md"
    filepath = NEEDS_ACTION_DIR / filename
    received = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    content = f"""---
type: linkedin_{lead_type}
from: "{sender}"
subject: "LinkedIn {lead_type} from {sender}"
received: "{received}"
source: "{source}"
keyword_matched: "{keyword}"
priority: {priority}
status: pending
---

# LinkedIn Lead: {sender}

| Field     | Value |
|-----------|-------|
| From      | {sender} |
| Source    | {source} |
| Type      | {lead_type.title()} |
| Received  | {received} |
| Priority  | {priority.upper()} |
| Keyword   | `{keyword}` |

## Content Preview

> {message[:600]}

## Next Steps

- [ ] Visit {sender}'s LinkedIn profile
- [ ] Reply to the message or notification
- [ ] Qualify as a lead or archive
- [ ] Move to Done when resolved
"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    return filepath


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def session_exists() -> bool:
    return SESSION_DIR.exists() and any(SESSION_DIR.iterdir())


def is_logged_in(page) -> bool:
    """Check if we're logged in by looking for the nav bar."""
    try:
        page.wait_for_selector(
            'nav[aria-label="Primary Navigation"], div.global-nav__me',
            timeout=8_000
        )
        return True
    except PlaywrightTimeout:
        return False


def wait_for_manual_login(page) -> bool:
    """
    Navigate to LinkedIn and wait for the user to complete login manually.
    Returns True when logged in successfully.
    """
    page.goto(LINKEDIN_HOME_URL)
    log("Please log in to LinkedIn in the browser window (you have 3 minutes)...")

    try:
        page.wait_for_selector(
            'nav[aria-label="Primary Navigation"], div.global-nav__me',
            timeout=LOGIN_WAIT_TIMEOUT
        )
        log("LinkedIn login detected!")
        return True
    except PlaywrightTimeout:
        return False


# ---------------------------------------------------------------------------
# Messages Scraper
# ---------------------------------------------------------------------------

def scrape_messages(page) -> list[dict]:
    """
    Navigate to LinkedIn Messaging and extract unread conversations.
    Returns list of {sender, preview}.
    """
    results = []

    try:
        with_retry(
            lambda: page.goto(LINKEDIN_MESSAGES_URL, timeout=PAGE_LOAD_TIMEOUT),
            label="linkedin_goto_messages", max_retries=3, base_delay=2, max_delay=30,
        )
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)

        # Conversation list items
        conv_items = page.query_selector_all(
            'li.msg-conversation-listitem, '
            'li[data-control-name="conversation_listitem"]'
        )

        if not conv_items:
            # Fallback selector
            conv_items = page.query_selector_all('div.msg-conversations-container li')

        for item in conv_items:
            try:
                # Unread indicator: bold name or unread badge
                unread_badge = item.query_selector(
                    'span.notification-badge, '
                    'span[data-test-badge], '
                    'span.msg-conversation-listitem__unread-count'
                )
                bold_name = item.query_selector('span.t-bold')

                if not unread_badge and not bold_name:
                    continue

                # Sender name
                name_el = item.query_selector(
                    'span.msg-conversation-listitem__participant-names, '
                    'h3.msg-conversation-listitem__group-name, '
                    'span.t-bold'
                )
                sender = name_el.inner_text().strip() if name_el else "Unknown"

                # Preview text
                preview_el = item.query_selector(
                    'p.msg-conversation-listitem__message-snippet, '
                    'span.msg-conversation-listitem__message-snippet'
                )
                preview = preview_el.inner_text().strip() if preview_el else ""

                results.append({"sender": sender, "preview": preview, "source": "message"})

            except Exception:
                continue

    except Exception as e:
        log(f"WARN: Could not scrape messages - {e}")
        log_watcher_error("linkedin", e, context="scrape_messages")

    return results


# ---------------------------------------------------------------------------
# Notifications Scraper
# ---------------------------------------------------------------------------

def scrape_notifications(page) -> list[dict]:
    """
    Navigate to LinkedIn Notifications and extract recent notification texts.
    Returns list of {sender, preview}.
    """
    results = []

    try:
        with_retry(
            lambda: page.goto(LINKEDIN_NOTIFICATIONS_URL, timeout=PAGE_LOAD_TIMEOUT),
            label="linkedin_goto_notifications", max_retries=3, base_delay=2, max_delay=30,
        )
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)

        # Notification cards
        notif_items = page.query_selector_all(
            'div.nt-card__text, '
            'li[data-urn], '
            'div[data-finite-scroll-hotspot]'
        )

        if not notif_items:
            notif_items = page.query_selector_all('section.core-rail li')

        for item in notif_items[:20]:  # Cap at 20 most recent
            try:
                text = item.inner_text().strip()
                if not text:
                    continue

                # Try to extract an actor name (first line is usually the actor)
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                sender = lines[0] if lines else "LinkedIn"
                preview = " ".join(lines[:3])

                results.append({"sender": sender, "preview": preview, "source": "notification"})

            except Exception:
                continue

    except Exception as e:
        log(f"WARN: Could not scrape notifications - {e}")
        log_watcher_error("linkedin", e, context="scrape_notifications")

    return results


# ---------------------------------------------------------------------------
# Core Scanner
# ---------------------------------------------------------------------------

def scan_for_leads(page, seen_keys: set) -> set:
    """Scan messages and notifications for keyword matches."""
    new_seen = set(seen_keys)

    # --- Messages ---
    log("Checking LinkedIn messages...")
    messages = scrape_messages(page)
    log(f"  Found {len(messages)} unread conversation(s).")

    # --- Notifications ---
    log("Checking LinkedIn notifications...")
    notifications = scrape_notifications(page)
    log(f"  Found {len(notifications)} recent notification(s).")

    all_items = messages + notifications

    if not all_items:
        log("Nothing to scan.")
        return new_seen

    matches = 0
    for item in all_items:
        sender = item["sender"]
        preview = item["preview"]
        source = item["source"]

        combined = f"{sender} {preview}"
        keyword = contains_keyword(combined)

        if not keyword:
            continue

        # Deduplicate: same sender + keyword within the same hour
        dedup_key = f"{source}:{sender}:{keyword}:{datetime.now().strftime('%Y%m%d_%H')}"
        if dedup_key in new_seen:
            log(f"  SKIP (already logged): [{source}] {sender}")
            continue

        priority = priority_from_keyword(keyword)
        lead_type = source  # "message" or "notification"
        filepath = save_needs_action(source, sender, preview, keyword, priority, lead_type)
        new_seen.add(dedup_key)
        matches += 1
        log(f"  MATCH [{keyword.upper()}] [{source}] '{sender}' -> {filepath.name}")

    if matches == 0:
        log("No keyword matches found this cycle.")

    return new_seen


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("AI Employee Silver Tier - LinkedIn Watcher")
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
    headless = not first_login

    if first_login:
        log("No existing session. Opening browser for manual login...")
    else:
        log("Existing session found. Launching in headless mode...")

    seen_keys: set = set()
    run = 0

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        page = browser.new_page()

        if first_login:
            if not wait_for_manual_login(page):
                log("ERROR: Login not completed within timeout. Exiting.")
                browser.close()
                sys.exit(1)
            log("Login successful! Session saved for future runs.")
        else:
            page.goto(LINKEDIN_HOME_URL, timeout=PAGE_LOAD_TIMEOUT)
            if not is_logged_in(page):
                log("WARN: Session may have expired. Delete /session/linkedin and re-run.")

        print("-" * 60)

        try:
            while True:
                run += 1
                log(f"--- Poll #{run} ---")
                try:
                    seen_keys = scan_for_leads(page, seen_keys)
                except Exception as poll_err:
                    tb = traceback.format_exc()
                    log(f"ERROR: Poll #{run} failed — {poll_err}. Logging and continuing...")
                    err_path = log_watcher_error(
                        "linkedin", poll_err,
                        context=f"scan_for_leads poll #{run}", tb=tb,
                    )
                    log(f"  Error logged to: {err_path.name}")
                log(f"Sleeping {CHECK_INTERVAL}s until next check...")
                time.sleep(CHECK_INTERVAL)

                # Reload home page periodically to keep session alive
                if run % 30 == 0:
                    log("Refreshing session...")
                    try:
                        page.goto(LINKEDIN_HOME_URL, timeout=PAGE_LOAD_TIMEOUT)
                        time.sleep(2)
                    except Exception as refresh_err:
                        log(f"WARN: Session refresh failed — {refresh_err}")
                        log_watcher_error("linkedin", refresh_err, context="session refresh")

        except KeyboardInterrupt:
            print()
            log("Shutdown requested by user.")

        except Exception as e:
            tb = traceback.format_exc()
            log(f"FATAL ERROR: {e}")
            log_watcher_error("linkedin", e, context="fatal error in main loop", tb=tb)
            raise

        finally:
            browser.close()
            log("LinkedIn watcher stopped.")
            print("=" * 60)


if __name__ == "__main__":
    main()
