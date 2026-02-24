"""
AI Employee Silver Tier - WhatsApp Watcher
============================================

Monitors WhatsApp Web for unread messages containing priority keywords
and saves actionable items as Markdown notes in /Needs Action.

KEYWORDS MONITORED: urgent, invoice, payment, sales

INSTALL:
    pip install playwright
    playwright install chromium

SESSION / LOGIN:
    - On first run, WhatsApp Web will show a QR code in the browser window.
    - Scan it with your phone (WhatsApp -> Linked Devices -> Link a Device).
    - The session is saved persistently in /session/whatsapp so you only
      need to scan the QR code once.
    - If the session expires, delete /session/whatsapp and re-scan.

RUN (direct):
    python watchers/whatsapp_watcher.py

RUN with PM2:
    pm2 start watchers/whatsapp_watcher.py --interpreter python --name whatsapp-watcher
    pm2 save
    pm2 startup

PM2 MANAGEMENT:
    pm2 status                       # Check if running
    pm2 logs whatsapp-watcher        # View live logs
    pm2 restart whatsapp-watcher     # Restart watcher
    pm2 stop whatsapp-watcher        # Stop watcher

TEST:
    1. Ask a contact to send you a WhatsApp message containing:
       "urgent", "invoice", "payment", or "sales"
    2. Leave it unread
    3. Within 30 seconds a .md file should appear in /Needs Action
    4. Check the console log for MATCH output

NOTES:
    - WhatsApp Web requires an active phone connection.
    - The script runs Chromium in headless=False mode on first login
      so you can scan the QR code. After the session is saved it
      switches to headless mode automatically.
    - Do not use your phone's WhatsApp while the watcher scans
      (messages may get marked as read by the browser).

"""

import os
import sys
import time
import re
from datetime import datetime
from pathlib import Path

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
SESSION_DIR = BASE_DIR / "session" / "whatsapp"

CHECK_INTERVAL = 30  # seconds
KEYWORDS = ["urgent", "invoice", "payment", "sales"]

WHATSAPP_URL = "https://web.whatsapp.com"
PAGE_LOAD_TIMEOUT = 60_000   # ms — time to wait for WhatsApp Web to load
SCAN_WAIT_TIMEOUT = 120_000  # ms — time allowed to scan QR code


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
    return {"urgent": "high", "invoice": "high",
            "payment": "high", "sales": "medium"}.get(keyword, "medium")


def sanitize_filename(text: str) -> str:
    text = re.sub(r'[\\/*?:"<>|]', "_", text)
    return text[:60].strip()


def save_needs_action(sender: str, message: str, keyword: str, priority: str) -> Path:
    """Write a Markdown note to /Needs Action."""
    NEEDS_ACTION_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_sender = sanitize_filename(sender)
    filename = f"WHATSAPP_{ts}_{safe_sender}.md"
    filepath = NEEDS_ACTION_DIR / filename
    received = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    content = f"""---
type: whatsapp_message
from: "{sender}"
subject: "WhatsApp message from {sender}"
received: "{received}"
keyword_matched: "{keyword}"
priority: {priority}
status: pending
---

# WhatsApp Message: {sender}

| Field    | Value |
|----------|-------|
| From     | {sender} |
| Received | {received} |
| Priority | {priority.upper()} |
| Keyword  | `{keyword}` |

## Message Preview

> {message[:500]}

## Next Steps

- [ ] Reply to {sender} on WhatsApp
- [ ] Escalate if needed
- [ ] Move to Done when resolved
"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    return filepath


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def session_exists() -> bool:
    """Check if a WhatsApp session already exists."""
    return SESSION_DIR.exists() and any(SESSION_DIR.iterdir())


# ---------------------------------------------------------------------------
# WhatsApp Scraping
# ---------------------------------------------------------------------------

def wait_for_whatsapp_ready(page) -> bool:
    """
    Wait until the main chat list is visible.
    Returns True if ready, False on timeout.
    """
    try:
        # The side panel with chats loads after login
        page.wait_for_selector('div[data-testid="chat-list"]', timeout=PAGE_LOAD_TIMEOUT)
        return True
    except PlaywrightTimeout:
        try:
            # Fallback selector for older WA Web versions
            page.wait_for_selector('#pane-side', timeout=10_000)
            return True
        except PlaywrightTimeout:
            return False


def wait_for_qr_scan(page) -> bool:
    """
    Wait for QR scan to complete (i.e. chat list appears).
    Returns True when logged in, False on timeout.
    """
    log("Waiting for QR code scan (you have 120 seconds)...")
    try:
        page.wait_for_selector('div[data-testid="chat-list"]', timeout=SCAN_WAIT_TIMEOUT)
        return True
    except PlaywrightTimeout:
        try:
            page.wait_for_selector('#pane-side', timeout=10_000)
            return True
        except PlaywrightTimeout:
            return False


def extract_unread_chats(page) -> list[dict]:
    """
    Return list of {sender, preview, unread_count} for chats with unread messages.
    Uses WhatsApp Web DOM structure.
    """
    unread_chats = []

    try:
        # Locate chat list items that have an unread badge
        chat_items = page.query_selector_all('div[data-testid="cell-frame-container"]')

        for item in chat_items:
            try:
                # Unread badge (a span with unread count)
                badge = item.query_selector('span[data-testid="icon-unread-count"]')
                if not badge:
                    # Try alternative: aria-label contains "unread"
                    badge = item.query_selector('[aria-label*="unread"]')

                if not badge:
                    continue

                # Sender name
                sender_el = item.query_selector('span[data-testid="cell-frame-title"]')
                sender = sender_el.inner_text() if sender_el else "Unknown"

                # Last message preview
                preview_el = item.query_selector('span[data-testid="last-msg-status"] ~ span, '
                                                 'div[data-testid="last-msg"]')
                preview = preview_el.inner_text() if preview_el else ""

                # Fallback: grab any text within the subtitle area
                if not preview:
                    subtitle = item.query_selector('div._ak8q, div[class*="message"]')
                    preview = subtitle.inner_text() if subtitle else ""

                unread_chats.append({
                    "sender": sender.strip(),
                    "preview": preview.strip(),
                })

            except Exception:
                continue

    except Exception as e:
        log(f"WARN: Error reading chat list - {e}")

    return unread_chats


def open_chat_and_get_messages(page, sender: str) -> str:
    """
    Click on a chat by sender name and return the last few message texts.
    """
    try:
        # Find chat by title text
        chat = page.query_selector(
            f'span[data-testid="cell-frame-title"][title="{sender}"]'
        )
        if not chat:
            # Fallback: search by text content
            chats = page.query_selector_all('span[data-testid="cell-frame-title"]')
            for c in chats:
                if c.inner_text().strip() == sender:
                    chat = c
                    break

        if not chat:
            return ""

        chat.click()
        time.sleep(1.5)  # Let messages load

        # Grab last 5 incoming messages
        msgs = page.query_selector_all(
            'div.message-in span[data-testid="msg-container"] span.selectable-text'
        )
        texts = [m.inner_text() for m in msgs[-5:] if m.inner_text()]
        return " ".join(texts)

    except Exception as e:
        log(f"WARN: Could not open chat for {sender} - {e}")
        return ""


def scan_for_keywords(page, seen_senders: set) -> set:
    """Scan unread chats for keyword matches and save .md files."""
    new_seen = set(seen_senders)

    unread = extract_unread_chats(page)

    if not unread:
        log("No unread messages detected.")
        return new_seen

    log(f"Found {len(unread)} unread chat(s). Scanning for keywords...")

    for chat in unread:
        sender = chat["sender"]
        preview = chat["preview"]

        # Check preview first for quick match
        keyword = contains_keyword(preview)

        # If not found in preview, open the chat for full text
        if not keyword:
            full_text = open_chat_and_get_messages(page, sender)
            keyword = contains_keyword(full_text)
            message_text = full_text
        else:
            message_text = preview

        if keyword:
            session_key = f"{sender}:{keyword}:{datetime.now().strftime('%Y%m%d_%H')}"
            if session_key in new_seen:
                log(f"SKIP (already logged this hour): {sender}")
                continue

            priority = priority_from_keyword(keyword)
            filepath = save_needs_action(sender, message_text, keyword, priority)
            new_seen.add(session_key)
            log(f"MATCH [{keyword.upper()}] from '{sender}' -> {filepath.name}")
        else:
            log(f"SKIP: '{sender}' (no keyword match)")

    return new_seen


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("AI Employee Silver Tier - WhatsApp Watcher")
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
    headless = not first_login  # Show browser only for first QR scan

    if first_login:
        log("No existing session found. Opening browser for QR code scan...")
    else:
        log("Existing session found. Launching in headless mode...")

    seen_senders: set = set()
    run = 0

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            viewport={"width": 1280, "height": 900},
        )

        page = browser.new_page()
        log(f"Navigating to {WHATSAPP_URL}...")
        page.goto(WHATSAPP_URL)

        if first_login:
            if not wait_for_qr_scan(page):
                log("ERROR: QR code was not scanned within timeout. Exiting.")
                browser.close()
                sys.exit(1)
            log("Login successful! Session saved.")
        else:
            if not wait_for_whatsapp_ready(page):
                log("WARN: WhatsApp Web took too long to load. Retrying next cycle.")

        print("-" * 60)

        try:
            while True:
                run += 1
                log(f"--- Poll #{run} ---")
                seen_senders = scan_for_keywords(page, seen_senders)
                log(f"Sleeping {CHECK_INTERVAL}s until next check...")
                time.sleep(CHECK_INTERVAL)

                # Reload the page periodically to keep the session fresh
                if run % 60 == 0:
                    log("Refreshing page to keep session alive...")
                    page.reload()
                    wait_for_whatsapp_ready(page)

        except KeyboardInterrupt:
            print()
            log("Shutdown requested by user.")

        except Exception as e:
            log(f"FATAL ERROR: {e}")
            raise

        finally:
            browser.close()
            log("WhatsApp watcher stopped.")
            print("=" * 60)


if __name__ == "__main__":
    main()
