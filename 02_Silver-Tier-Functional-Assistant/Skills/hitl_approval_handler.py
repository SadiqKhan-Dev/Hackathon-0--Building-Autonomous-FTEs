"""
AI Employee Silver Tier — Skill 4: HITL Approval Handler
==========================================================

Central Human-in-the-Loop gateway. Monitors /Pending Approval and /Approved,
executes approved actions, logs all events to /Logs/hitl_[date].md.

INTEGRATIONS:
  email_draft        → sends via Gmail API (same OAuth2 as gmail_watcher.py)
  linkedin_post_draft → logs as ready-to-post (manual publish, Silver Tier)
  payment            → logs approval and notifies
  generic            → logs approval

MODES:
  --monitor    Continuous polling loop (default interval: 15s)
  --check      One-shot report of all pending/approved items
  --write      Manually write a new pending approval request
  --approve    Approve a pending file by name (for testing)
  --reject     Reject a pending file by name

INSTALL:
  pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client

AUTH (one-time, shares token.json with gmail_watcher.py):
  python watchers/gmail_watcher.py        # Run once to complete OAuth login
  OR: python Skills/hitl_approval_handler.py --auth

RUN (monitor mode):
  python Skills/hitl_approval_handler.py --monitor

RUN with PM2:
  pm2 start Skills/hitl_approval_handler.py --interpreter python --name hitl-handler -- --monitor
  pm2 save && pm2 startup

PM2 MANAGEMENT:
  pm2 status                   # Check if running
  pm2 logs hitl-handler        # View live logs
  pm2 restart hitl-handler     # Restart
  pm2 stop hitl-handler        # Stop

TEST:
  1. Create a test pending approval file:
       python Skills/hitl_approval_handler.py --write \\
         --type generic --details "Test approval request"
  2. Move the created file from /Pending Approval to /Approved manually
  3. Run check:
       python Skills/hitl_approval_handler.py --check
  4. Run monitor for one cycle:
       python Skills/hitl_approval_handler.py --monitor --max-cycles 1
  5. Check /Logs/hitl_[date].md for events

TEST EMAIL FLOW:
  1. Draft an email via Email MCP or:
       python Skills/hitl_approval_handler.py --write \\
         --type email_draft \\
         --details "Send intro to client@example.com" \\
         --draft-file "Plans/email_draft_2024-02-19_Hello.md"
  2. Move file from /Pending Approval to /Approved
  3. Run: python Skills/hitl_approval_handler.py --monitor --max-cycles 1
  4. Email is sent, file moves to /Done, event logged.

"""

import os
import sys
import re
import shutil
import argparse
import time
from datetime import datetime
from pathlib import Path

# --- Gmail API (optional — only needed for email_draft execution) ---
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    import base64
    GMAIL_AVAILABLE = True
except ImportError:
    GMAIL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR          = Path(__file__).resolve().parent.parent
PENDING_DIR       = BASE_DIR / "Pending Approval"
APPROVED_DIR      = BASE_DIR / "Approved"
REJECTED_DIR      = BASE_DIR / "Rejected"
DONE_DIR          = BASE_DIR / "Done"
PLANS_DIR         = BASE_DIR / "Plans"
LOGS_DIR          = BASE_DIR / "Logs"
HANDBOOK_FILE     = BASE_DIR / "Company Handbook.md"
CREDENTIALS_FILE  = BASE_DIR / "credentials.json"
TOKEN_FILE        = BASE_DIR / "token.json"

MONITOR_INTERVAL  = 15   # seconds between /Approved scans
GMAIL_SCOPES      = ["https://www.googleapis.com/auth/gmail.send",
                     "https://www.googleapis.com/auth/gmail.readonly"]

VALID_TYPES = {"email_draft", "linkedin_post_draft", "payment", "generic"}

ACTION_TYPE_MAP = {
    "email_draft":         "send_email",
    "linkedin_post_draft": "post_linkedin",
    "payment":             "approve_payment",
    "generic":             "review",
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def console(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def log_event(event: str, filename: str, action: str, status: str, details: str = "") -> None:
    """Append a structured log entry to /Logs/hitl_[date].md."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"hitl_{_date()}.md"

    # Create header if file is new
    if not log_file.exists():
        header = f"""---
type: hitl_log
date: "{_date()}"
---

# HITL Log — {_date()}

| Time     | Event       | File                                  | Action           | Status  |
|----------|-------------|---------------------------------------|------------------|---------|
"""
        log_file.write_text(header, encoding="utf-8")

    row = f"| {_ts()} | {event:<11} | {filename[:38]:<38} | {action[:16]:<16} | {status:<7} |"
    if details:
        row += f"\n|          |             | ↳ {details[:60]}"

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(row + "\n")

    console(f"LOG [{event}] {filename} | {status}")


# ---------------------------------------------------------------------------
# YAML Frontmatter Parser
# ---------------------------------------------------------------------------

def parse_yaml_field(content: str, field: str) -> str:
    pattern = rf'^{re.escape(field)}:\s*["\']?(.+?)["\']?\s*$'
    m = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def update_yaml_field(content: str, field: str, value: str) -> str:
    pattern = rf'^({re.escape(field)}:\s*).*$'
    replacement = rf'\g<1>{value}'
    updated = re.sub(pattern, replacement, content, flags=re.MULTILINE)
    if updated == content:
        # Field doesn't exist — append to frontmatter
        content = content.replace("---\n\n", f"---\n{field}: {value}\n\n", 1)
    return updated


def parse_draft_file(path: Path) -> dict:
    """Parse all YAML frontmatter fields from an .md file."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}

    fields = {}
    for line in (content.split("---")[1].split("\n") if "---" in content else []):
        m = re.match(r'^(\w+):\s*["\']?(.+?)["\']?\s*$', line)
        if m:
            fields[m.group(1)] = m.group(2).strip()
    fields["__content__"] = content
    return fields


def update_file_status(path: Path, new_status: str) -> None:
    """Rewrite the `status:` field in a file's YAML frontmatter."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        content = update_yaml_field(content, "status", new_status)
        path.write_text(content, encoding="utf-8")
    except Exception as e:
        console(f"WARN: Could not update status in {path.name}: {e}")


def extract_email_body(content: str) -> str:
    """Extract text block after '## Body' heading."""
    m = re.search(r'^## Body\n\n([\s\S]*?)(?=\n##|\Z)', content, re.MULTILINE)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Handbook
# ---------------------------------------------------------------------------

def load_handbook() -> str:
    if HANDBOOK_FILE.exists():
        return HANDBOOK_FILE.read_text(encoding="utf-8").strip()
    return "Always be polite. Flag payments > $500 for approval."


# ---------------------------------------------------------------------------
# Gmail API
# ---------------------------------------------------------------------------

_gmail_service = None


def get_gmail_service():
    global _gmail_service
    if _gmail_service:
        return _gmail_service

    if not GMAIL_AVAILABLE:
        raise RuntimeError(
            "Google API libraries not installed. "
            "Run: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client"
        )

    if not CREDENTIALS_FILE.exists():
        raise RuntimeError(
            f"credentials.json not found at {CREDENTIALS_FILE}. "
            "Download from Google Cloud Console and place in project root."
        )

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(
                "No valid token.json found. Run one-time auth:\n"
                "  python watchers/gmail_watcher.py\n"
                "OR: python Skills/hitl_approval_handler.py --auth"
            )

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    _gmail_service = build("gmail", "v1", credentials=creds)
    return _gmail_service


def run_auth_flow() -> None:
    """One-time Gmail OAuth2 flow — saves token.json."""
    if not GMAIL_AVAILABLE:
        print("ERROR: Google API libraries not installed.")
        print("Run: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")
        sys.exit(1)

    if not CREDENTIALS_FILE.exists():
        print(f"ERROR: credentials.json not found at {CREDENTIALS_FILE}")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), GMAIL_SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"\ntoken.json saved to: {TOKEN_FILE}")
    print("Gmail auth complete. You can now run the HITL handler.")


def build_raw_email(from_addr: str, to: str, subject: str, body: str,
                    cc: str = "", bcc: str = "") -> str:
    parts = [
        f"From: {from_addr}",
        f"To: {to}",
        f"Cc: {cc}" if cc else None,
        f"Bcc: {bcc}" if bcc else None,
        "MIME-Version: 1.0",
        "Content-Type: text/plain; charset=utf-8",
        f"Subject: {subject}",
        "",
        body,
    ]
    raw = "\r\n".join(p for p in parts if p is not None)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


# ---------------------------------------------------------------------------
# Execution Handlers
# ---------------------------------------------------------------------------

def execute_email_draft(filename: str, fields: dict, approved_path: Path) -> dict:
    """Send email via Gmail API from an approved draft file."""
    draft_file_rel = fields.get("draft_file", "")

    # Resolve draft source: approved file itself OR referenced draft_file
    source_path = approved_path
    if draft_file_rel:
        candidate = BASE_DIR / draft_file_rel
        if candidate.exists():
            source_path = candidate

    content = source_path.read_text(encoding="utf-8", errors="replace")
    to      = parse_yaml_field(content, "to")
    subject = parse_yaml_field(content, "subject")
    cc      = parse_yaml_field(content, "cc")
    bcc     = parse_yaml_field(content, "bcc")
    body    = extract_email_body(content)

    if not to or not subject:
        return {"success": False, "error": f"Missing 'to' or 'subject' in {source_path.name}"}

    try:
        service = get_gmail_service()
        profile = service.users().getProfile(userId="me").execute()
        sender  = profile.get("emailAddress", "me")

        raw = build_raw_email(sender, to, subject, body, cc, bcc)
        result = service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()

        gmail_id = result.get("id", "unknown")
        return {
            "success":  True,
            "gmail_id": gmail_id,
            "sent_to":  to,
            "subject":  subject,
            "message":  f"Email sent to {to} (Gmail ID: {gmail_id})",
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


def execute_linkedin_post(filename: str, fields: dict, approved_path: Path) -> dict:
    """Log LinkedIn post as ready for manual publish (API posting is Gold Tier)."""
    draft_file_rel = fields.get("draft_file", "")
    content_preview = fields.get("details", "(see draft file)")
    return {
        "success":  True,
        "message":  f"LinkedIn post marked READY. Publish manually from /Approved/{filename}",
        "note":     "Automated LinkedIn posting is a Gold Tier feature. "
                    "Review and post the content manually.",
        "draft":    draft_file_rel or approved_path.name,
    }


def execute_payment(filename: str, fields: dict, approved_path: Path) -> dict:
    """Log payment approval."""
    amount = fields.get("amount", "unspecified")
    target = fields.get("target", "unspecified")
    return {
        "success": True,
        "message": f"Payment of {amount} for '{target}' has been approved and logged.",
        "note":    "Process payment through your payment system using the approved details.",
    }


def execute_generic(filename: str, fields: dict, approved_path: Path) -> dict:
    """Log generic approval."""
    details = fields.get("details", "No details provided.")
    return {
        "success": True,
        "message": f"Action approved: {details}",
    }


EXECUTORS = {
    "email_draft":         execute_email_draft,
    "linkedin_post_draft": execute_linkedin_post,
    "payment":             execute_payment,
    "generic":             execute_generic,
}


# ---------------------------------------------------------------------------
# Core: Process an Approved File
# ---------------------------------------------------------------------------

def process_approved_file(approved_path: Path) -> None:
    """Detect type, execute action, move to /Done, log event."""
    filename = approved_path.name
    fields   = parse_draft_file(approved_path)

    if not fields:
        console(f"WARN: Could not parse {filename} — skipping.")
        log_event("ERROR", filename, "parse", "error", "Could not read YAML frontmatter")
        return

    action_type = fields.get("type", "generic").lower()
    action      = fields.get("action", ACTION_TYPE_MAP.get(action_type, "review"))
    priority    = fields.get("priority", "medium")
    details     = fields.get("details", "")

    console(f"EXECUTING [{action_type.upper()}] {filename} (priority: {priority})")
    log_event("APPROVED", filename, action, "approved", details[:60])

    executor = EXECUTORS.get(action_type, execute_generic)

    try:
        result = executor(filename, fields, approved_path)
    except Exception as e:
        result = {"success": False, "error": str(e)}

    if result["success"]:
        new_status = {
            "email_draft":         "sent",
            "linkedin_post_draft": "approved_ready",
            "payment":             "payment_approved",
            "generic":             "executed",
        }.get(action_type, "executed")

        update_file_status(approved_path, new_status)

        # Move to /Done
        DONE_DIR.mkdir(parents=True, exist_ok=True)
        done_path = DONE_DIR / filename
        counter = 1
        while done_path.exists():
            done_path = DONE_DIR / f"{approved_path.stem}_{counter}{approved_path.suffix}"
            counter += 1
        shutil.move(str(approved_path), str(done_path))

        # Also sync Plans copy status
        plans_copy = PLANS_DIR / filename
        if plans_copy.exists():
            update_file_status(plans_copy, new_status)

        console(f"SUCCESS: {filename} → /Done | Status: {new_status}")
        console(f"  {result['message']}")
        log_event("EXECUTED", filename, action, new_status, result.get("message", "")[:60])

    else:
        error = result.get("error", "unknown error")
        update_file_status(approved_path, "error")
        console(f"ERROR: {filename} failed — {error}")
        log_event("ERROR", filename, action, "error", error[:60])


# ---------------------------------------------------------------------------
# Core: Process a Rejected File
# ---------------------------------------------------------------------------

def process_rejected_file(rejected_path: Path) -> None:
    """Update status on newly-rejected files."""
    filename = rejected_path.name
    fields   = parse_draft_file(rejected_path)
    current  = fields.get("status", "")

    if current == "rejected":
        return  # Already processed

    update_file_status(rejected_path, "rejected")

    plans_copy = PLANS_DIR / filename
    if plans_copy.exists():
        update_file_status(plans_copy, "rejected")

    reason = fields.get("rejection_reason", "moved to /Rejected by reviewer")
    console(f"REJECTED: {filename} — {reason}")
    log_event("REJECTED", filename, fields.get("action", "review"), "rejected", reason[:60])


# ---------------------------------------------------------------------------
# Mode: --write
# ---------------------------------------------------------------------------

def write_pending(action_type: str, details: str, draft_file: str = "",
                  target: str = "", amount: str = "", priority: str = "medium") -> Path:
    """Create a new pending approval request in /Pending Approval."""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)

    if action_type not in VALID_TYPES:
        console(f"WARN: Unknown type '{action_type}', using 'generic'.")
        action_type = "generic"

    ts_str   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{action_type}_{ts_str}.md"
    filepath = PENDING_DIR / filename
    action   = ACTION_TYPE_MAP.get(action_type, "review")

    # Handbook check: auto-escalate payment > $500
    if action_type == "payment" and amount:
        try:
            amt_val = float(re.sub(r"[^0-9.]", "", amount))
            if amt_val > 500:
                priority = "high"
                console(f"HANDBOOK FLAG: Payment {amount} > $500 — priority elevated to HIGH.")
        except ValueError:
            pass

    content = f"""---
type: {action_type}
action: {action}
source_skill: manual
details: "{details}"
target: "{target}"
amount: "{amount}"
priority: {priority}
status: pending_approval
created: "{_iso()}"
draft_file: "{draft_file}"
---

# Pending Approval: {action_type.replace("_", " ").title()}

| Field       | Value |
|-------------|-------|
| Type        | {action_type} |
| Action      | {action} |
| Priority    | {priority.upper()} |
| Status      | pending_approval |
| Created     | {_iso()} |
| Draft File  | {draft_file or "(none)"} |
| Target      | {target or "(none)"} |
| Amount      | {amount or "(none)"} |

## Details

{details}

## HITL Instructions

1. Review the details above
2. If approving: **move this file to `/Approved/`**
3. If rejecting: **move this file to `/Rejected/`**
4. The HITL Handler will detect the move and execute automatically.

## Checklist

- [ ] Review details
- [ ] Check Company Handbook rules
- [ ] Verify target / recipient / amount
- [ ] Move to /Approved or /Rejected
"""

    filepath.write_text(content, encoding="utf-8")

    console(f"QUEUED: {filename}")
    log_event("QUEUED", filename, action, "pending_approval", details[:60])

    print()
    print(f"SUCCESS: Pending approval request created")
    print(f"File    : Pending Approval/{filename}")
    print(f"Type    : {action_type}")
    print(f"Status  : pending_approval")
    print(f"Next    : Move to /Approved to execute, or /Rejected to decline.")

    return filepath


# ---------------------------------------------------------------------------
# Mode: --approve / --reject (CLI helpers for testing)
# ---------------------------------------------------------------------------

def cli_approve(filename: str) -> None:
    """Move a pending file to /Approved (for testing without manual file move)."""
    src = PENDING_DIR / filename
    if not src.exists():
        console(f"ERROR: Not found in /Pending Approval: {filename}")
        return

    APPROVED_DIR.mkdir(parents=True, exist_ok=True)
    dst = APPROVED_DIR / filename
    shutil.move(str(src), str(dst))
    console(f"APPROVED: {filename} moved to /Approved")


def cli_reject(filename: str, reason: str = "Rejected via CLI") -> None:
    """Move a pending file to /Rejected (for testing)."""
    src = PENDING_DIR / filename
    if not src.exists():
        console(f"ERROR: Not found in /Pending Approval: {filename}")
        return

    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    dst = REJECTED_DIR / filename
    shutil.move(str(src), str(dst))

    # Inject rejection reason into file
    content = dst.read_text(encoding="utf-8", errors="replace")
    content = update_yaml_field(content, "rejection_reason", reason)
    dst.write_text(content, encoding="utf-8")

    console(f"REJECTED: {filename} moved to /Rejected | Reason: {reason}")
    process_rejected_file(dst)


# ---------------------------------------------------------------------------
# Mode: --check
# ---------------------------------------------------------------------------

def check_pending() -> None:
    """One-shot report of all pending and approved items."""
    print("=" * 60)
    print("  HITL Approval Handler — Status Check")
    print("=" * 60)

    def scan_dir(d: Path, label: str) -> list:
        if not d.exists():
            return []
        items = []
        for f in sorted(d.glob("*.md")):
            fields = parse_draft_file(f)
            items.append({
                "name":     f.name,
                "type":     fields.get("type", "?"),
                "priority": fields.get("priority", "?"),
                "status":   fields.get("status", "?"),
                "details":  fields.get("details", "")[:50],
            })
        return items

    pending  = scan_dir(PENDING_DIR,  "Pending Approval")
    approved = scan_dir(APPROVED_DIR, "Approved")
    rejected = scan_dir(REJECTED_DIR, "Rejected")

    print(f"\nPending Approval : {len(pending)} item(s)")
    for i, item in enumerate(pending, 1):
        print(f"  {i}. [{item['type']:<22}] {item['name'][:45]}  | priority: {item['priority']}")

    print(f"\nApproved (queued): {len(approved)} item(s)")
    for i, item in enumerate(approved, 1):
        print(f"  {i}. [{item['type']:<22}] {item['name'][:45]}  | status: {item['status']}")

    print(f"\nRejected         : {len(rejected)} item(s)")
    for i, item in enumerate(rejected, 1):
        print(f"  {i}. [{item['type']:<22}] {item['name'][:45]}")

    log_file = LOGS_DIR / f"hitl_{_date()}.md"
    print(f"\nLog              : {log_file}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Mode: --monitor
# ---------------------------------------------------------------------------

def monitor(interval: int = MONITOR_INTERVAL, max_cycles: int = 0) -> None:
    """
    Continuous polling loop.
    Watches /Approved for new files and /Rejected for newly-rejected ones.
    Executes approved actions and logs everything.
    """
    APPROVED_DIR.mkdir(parents=True, exist_ok=True)
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  HITL Approval Handler — Monitor Mode")
    print("=" * 60)
    console(f"BASE DIR  : {BASE_DIR}")
    console(f"APPROVED  : {APPROVED_DIR}")
    console(f"REJECTED  : {REJECTED_DIR}")
    console(f"INTERVAL  : {interval}s")
    console(f"LOG       : {LOGS_DIR}/hitl_{_date()}.md")
    print("-" * 60)
    console("Watching /Approved and /Rejected. Press Ctrl+C to stop.")
    print()

    processed: set[str] = set()   # Track executed file names this session
    cycle = 0

    try:
        while True:
            cycle += 1
            any_work = False

            # ── Process newly approved files ──
            for f in sorted(APPROVED_DIR.glob("*.md")):
                if f.name in processed:
                    continue
                fields = parse_draft_file(f)
                status = fields.get("status", "")

                # Only process if status is 'approved' or not yet updated
                if status in ("sent", "executed", "approved_ready",
                              "payment_approved", "error"):
                    processed.add(f.name)
                    continue

                processed.add(f.name)
                process_approved_file(f)
                any_work = True

            # ── Process newly rejected files ──
            for f in sorted(REJECTED_DIR.glob("*.md")):
                if f.name in processed:
                    continue
                fields = parse_draft_file(f)
                if fields.get("status", "") != "rejected":
                    process_rejected_file(f)
                    any_work = True
                processed.add(f.name)

            if not any_work and cycle % 4 == 0:
                console(f"Idle — watching /Approved ({len(processed)} processed so far)...")

            if max_cycles and cycle >= max_cycles:
                console(f"Reached max cycles ({max_cycles}). Stopping.")
                break

            time.sleep(interval)

    except KeyboardInterrupt:
        print()
        console("Shutdown requested.")

    console("HITL Monitor stopped.")
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="hitl-approval-handler",
        description="Skill 4: HITL Approval Handler — Silver Tier AI Employee",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  --monitor            Continuous watch of /Approved (default)
  --check              One-shot status report
  --write              Create a new pending approval request
  --approve FILENAME   Move a file from /Pending Approval to /Approved (testing)
  --reject  FILENAME   Move a file from /Pending Approval to /Rejected (testing)
  --auth               Run one-time Gmail OAuth2 login

Examples:
  python Skills/hitl_approval_handler.py --monitor
  python Skills/hitl_approval_handler.py --check
  python Skills/hitl_approval_handler.py --write --type email_draft --details "Send to client"
  python Skills/hitl_approval_handler.py --approve email_draft_2024-02-19_Hello.md
  python Skills/hitl_approval_handler.py --reject  email_draft_2024-02-19_Hello.md --reason "Wrong tone"
""",
    )

    # Mode flags
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--monitor",  action="store_true", help="Continuous monitor mode (default)")
    mode.add_argument("--check",    action="store_true", help="One-shot status check")
    mode.add_argument("--write",    action="store_true", help="Write a new pending approval request")
    mode.add_argument("--approve",  metavar="FILENAME",  help="Approve a pending file (testing)")
    mode.add_argument("--reject",   metavar="FILENAME",  help="Reject a pending file (testing)")
    mode.add_argument("--auth",     action="store_true", help="Run Gmail OAuth2 setup")

    # --write options
    parser.add_argument("--type",       default="generic", help="Action type: email_draft | linkedin_post_draft | payment | generic")
    parser.add_argument("--details",    default="Pending approval request", help="Human-readable details")
    parser.add_argument("--draft-file", default="", dest="draft_file", help="Path to source draft file")
    parser.add_argument("--target",     default="", help="Target (email address, LinkedIn user, etc.)")
    parser.add_argument("--amount",     default="", help="Payment amount (if applicable)")
    parser.add_argument("--priority",   default="medium", choices=["high", "medium", "low"])

    # --reject option
    parser.add_argument("--reason", default="Rejected via CLI", help="Rejection reason (for --reject)")

    # --monitor options
    parser.add_argument("--interval",   type=int, default=MONITOR_INTERVAL, help=f"Poll interval in seconds (default: {MONITOR_INTERVAL})")
    parser.add_argument("--max-cycles", type=int, default=0, dest="max_cycles", help="Stop after N cycles (0 = unlimited)")

    args = parser.parse_args()

    # Route to correct handler
    if args.auth:
        run_auth_flow()

    elif args.check:
        check_pending()

    elif args.write:
        write_pending(
            action_type=args.type,
            details=args.details,
            draft_file=args.draft_file,
            target=args.target,
            amount=args.amount,
            priority=args.priority,
        )

    elif args.approve:
        cli_approve(args.approve)

    elif args.reject:
        cli_reject(args.reject, args.reason)

    else:
        # Default: monitor mode
        monitor(interval=args.interval, max_cycles=args.max_cycles)


if __name__ == "__main__":
    main()
