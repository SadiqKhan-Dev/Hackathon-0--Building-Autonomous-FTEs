"""
AI Employee Silver Tier - Gmail Watcher
========================================

Monitors unread Gmail messages for priority keywords and saves
actionable items as Markdown notes in /Needs Action.

KEYWORDS MONITORED: urgent, invoice, payment, sales

INSTALL:
    pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client

CREDENTIALS SETUP:
    1. Go to https://console.cloud.google.com/
    2. Create a project and enable the Gmail API
    3. Create OAuth 2.0 credentials (Desktop app type)
    4. Download as credentials.json and place in the project root folder
    5. Run this script once manually to complete OAuth browser login
    6. A token.json file will be saved for future runs (no re-login needed)

RUN (direct):
    python watchers/gmail_watcher.py

RUN with PM2:
    pm2 start watchers/gmail_watcher.py --interpreter python --name gmail-watcher
    pm2 save
    pm2 startup

PM2 MANAGEMENT:
    pm2 status                    # Check if running
    pm2 logs gmail-watcher        # View live logs
    pm2 restart gmail-watcher     # Restart watcher
    pm2 stop gmail-watcher        # Stop watcher

TEST:
    1. Send yourself a Gmail with the subject or body containing:
       "urgent", "invoice", "payment", or "sales"
    2. Leave it unread
    3. Within 120 seconds a .md file should appear in /Needs Action
    4. Check the console log for MATCH or NO MATCH output

"""

import os
import sys
import time
import re
import base64
import json
import traceback
from datetime import datetime
from pathlib import Path

# --- Gold Tier Error Recovery ---
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "Skills"
sys.path.insert(0, str(_SKILLS_DIR))
from error_recovery import with_retry, log_watcher_error  # noqa: E402

# --- Dependency Checks ---
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("ERROR: Google API libraries not installed.")
    print("Run: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"
NEEDS_ACTION_DIR = BASE_DIR / "Needs Action"

CHECK_INTERVAL = 120  # seconds

KEYWORDS = ["urgent", "invoice", "payment", "sales"]

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


# ---------------------------------------------------------------------------
# Helpers
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
    priority_map = {
        "urgent": "high",
        "invoice": "high",
        "payment": "high",
        "sales": "medium",
    }
    return priority_map.get(keyword, "medium")


def sanitize_filename(text: str) -> str:
    """Remove characters unsafe for filenames."""
    text = re.sub(r'[\\/*?:"<>|]', "_", text)
    return text[:80].strip()


def decode_email_body(payload: dict) -> str:
    """Recursively extract plain text body from Gmail message payload."""
    body = ""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            body = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    elif mime_type.startswith("multipart"):
        for part in payload.get("parts", []):
            body += decode_email_body(part)

    return body


def get_header(headers: list, name: str) -> str:
    """Extract a specific header value by name."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def save_needs_action(sender: str, subject: str, received: str,
                      priority: str, keyword: str, snippet: str) -> Path:
    """Write a Markdown note to /Needs Action."""
    NEEDS_ACTION_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_subject = sanitize_filename(subject)
    filename = f"EMAIL_{ts}_{safe_subject}.md"
    filepath = NEEDS_ACTION_DIR / filename

    content = f"""---
type: email
from: "{sender}"
subject: "{subject}"
received: "{received}"
keyword_matched: "{keyword}"
priority: {priority}
status: pending
---

# Email Alert: {subject}

| Field    | Value |
|----------|-------|
| From     | {sender} |
| Subject  | {subject} |
| Received | {received} |
| Priority | {priority.upper()} |
| Keyword  | `{keyword}` |

## Snippet

> {snippet}

## Next Steps

- [ ] Read full email in Gmail
- [ ] Respond or escalate as needed
- [ ] Move to Done when resolved
"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    return filepath


# ---------------------------------------------------------------------------
# Gmail Auth
# ---------------------------------------------------------------------------

def get_gmail_service():
    """Authenticate and return a Gmail API service object."""
    creds = None

    if not CREDENTIALS_FILE.exists():
        log(f"ERROR: credentials.json not found at {CREDENTIALS_FILE}")
        log("Download OAuth credentials from Google Cloud Console and place in project root.")
        sys.exit(1)

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log("Refreshing expired access token...")
            creds.refresh(Request())
        else:
            log("First run: opening browser for Gmail OAuth login...")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        log(f"Token saved to {TOKEN_FILE}")

    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Core Watcher Logic
# ---------------------------------------------------------------------------

def _do_list_messages(service):
    return service.users().messages().list(
        userId="me", q="is:unread is:important", maxResults=20
    ).execute()


def fetch_unread_important(service) -> list:
    """
    Return list of unread messages matching Gmail IMPORTANT label.
    Uses exponential backoff retry on network/API failures.
    """
    results = with_retry(
        _do_list_messages,
        service,
        label="gmail_list_messages",
        max_retries=3,
        base_delay=1,
        max_delay=60,
    )
    return results.get("messages", [])


def process_messages(service, seen_ids: set) -> set:
    """Fetch unread important emails, filter by keyword, save .md files."""
    new_seen = set()

    try:
        messages = fetch_unread_important(service)
    except Exception as e:
        log(f"ERROR: Gmail API call failed after retries - {e}")
        log_watcher_error("gmail", e, context="fetch_unread_important failed")
        return seen_ids

    if not messages:
        log("No unread important emails found.")
        return seen_ids

    log(f"Found {len(messages)} unread important email(s). Scanning for keywords...")

    for msg_ref in messages:
        msg_id = msg_ref["id"]
        new_seen.add(msg_id)

        if msg_id in seen_ids:
            continue  # Already processed this session

        try:
            msg = with_retry(
                lambda: service.users().messages().get(
                    userId="me", id=msg_id, format="full"
                ).execute(),
                label=f"gmail_get_message_{msg_id[:8]}",
                max_retries=3,
                base_delay=1,
                max_delay=30,
            )
        except Exception as e:
            log(f"WARN: Could not fetch message {msg_id} after retries - {e}")
            log_watcher_error("gmail", e, context=f"get message id={msg_id}")
            continue

        headers = msg.get("payload", {}).get("headers", [])
        sender = get_header(headers, "from")
        subject = get_header(headers, "subject") or "(no subject)"
        date = get_header(headers, "date")
        snippet = msg.get("snippet", "")
        body = decode_email_body(msg.get("payload", {}))

        combined_text = f"{subject} {snippet} {body}"
        keyword = contains_keyword(combined_text)

        if keyword:
            priority = priority_from_keyword(keyword)
            filepath = save_needs_action(sender, subject, date, priority, keyword, snippet)
            log(f"MATCH [{keyword.upper()}] '{subject}' from {sender} -> {filepath.name}")
        else:
            log(f"SKIP: '{subject}' from {sender} (no keyword match)")

    return seen_ids | new_seen


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("AI Employee Silver Tier - Gmail Watcher")
    print("=" * 60)
    print()
    log(f"BASE DIR    : {BASE_DIR}")
    log(f"OUTPUT      : {NEEDS_ACTION_DIR}")
    log(f"KEYWORDS    : {', '.join(KEYWORDS)}")
    log(f"INTERVAL    : {CHECK_INTERVAL}s")
    print()

    log("Authenticating with Gmail API...")
    service = get_gmail_service()
    log("Gmail API connected.")
    print("-" * 60)

    seen_ids: set = set()
    run = 0

    try:
        while True:
            run += 1
            log(f"--- Poll #{run} ---")
            try:
                seen_ids = process_messages(service, seen_ids)
            except Exception as poll_err:
                tb = traceback.format_exc()
                log(f"ERROR: Poll #{run} failed — {poll_err}. Logging and continuing...")
                err_path = log_watcher_error(
                    "gmail", poll_err,
                    context=f"poll cycle #{run}",
                    tb=tb,
                )
                log(f"  Error logged to: {err_path.name}")
            log(f"Sleeping {CHECK_INTERVAL}s until next check...")
            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print()
        log("Shutdown requested by user.")

    except Exception as e:
        tb = traceback.format_exc()
        log(f"FATAL ERROR: {e}")
        log_watcher_error("gmail", e, context="fatal error in main loop", tb=tb)
        raise

    finally:
        log("Gmail watcher stopped.")
        print("=" * 60)


if __name__ == "__main__":
    main()
