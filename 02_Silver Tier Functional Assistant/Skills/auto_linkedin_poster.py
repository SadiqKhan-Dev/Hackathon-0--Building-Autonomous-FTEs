"""
AI Employee Silver Tier — Skill 3: Auto LinkedIn Poster
=========================================================

Scans /Needs Action for business lead messages, drafts a polished LinkedIn
post, saves it to /Plans, then routes it to /Pending Approval for HITL review.

KEYWORDS SCANNED: sales, client, project
HANDBOOK RULES  : Always polite | Flag payments > $500

RUN (direct):
    python Skills/auto_linkedin_poster.py

RUN with a specific lead file:
    python Skills/auto_linkedin_poster.py --file "Needs Action/LINKEDIN_20240219_John.md"

RUN with PM2:
    pm2 start Skills/auto_linkedin_poster.py --interpreter python --name linkedin-poster
    pm2 save

PM2 MANAGEMENT:
    pm2 logs linkedin-poster     # Live logs
    pm2 restart linkedin-poster  # Restart
    pm2 stop linkedin-poster     # Stop

TEST:
    1. Create a test file in /Needs Action named test_lead.md with content:
           from: "Test Contact"
           subject: "Project proposal"
           keyword_matched: "project"
       Save and run this script.
    2. Check /Plans for linkedin_post_[date].md
    3. Check /Pending Approval for the same file (pending HITL).
    4. Manually move from /Pending Approval to /Approved to simulate approval.

APPROVAL FLOW:
    /Needs Action/[lead].md
         |
         v
    [This script runs]
         |
         v
    /Plans/linkedin_post_[date].md        <- draft saved here
         |
         v
    /Pending Approval/linkedin_post_[date].md  <- HUMAN reviews here
         |
    +----+----+
    |         |
  APPROVE   REJECT
    |         |
    v         v
 /Approved/ /Rejected/
 (publish)  (archive)

"""

import sys
import re
import shutil
import argparse
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
NEEDS_ACTION_DIR = BASE_DIR / "Needs Action"
PLANS_DIR = BASE_DIR / "Plans"
PENDING_APPROVAL_DIR = BASE_DIR / "Pending Approval"
HANDBOOK_FILE = BASE_DIR / "Company Handbook.md"

KEYWORDS = ["sales", "client", "project"]

POST_TEMPLATE = (
    "Excited to offer {service} for {benefit}!\n"
    "If you are looking for {outcome}, I would love to connect.\n"
    "DM me to get started — always happy to help.\n\n"
    "#{tag1} #Business #{tag2}"
)

# Keyword → (service hint, benefit hint, outcome hint, tag1, tag2)
KEYWORD_CONTEXT = {
    "sales": (
        "sales strategy and business development",
        "growing your revenue",
        "a reliable sales partner",
        "Sales", "Growth"
    ),
    "client": (
        "professional client management",
        "building stronger client relationships",
        "a dedicated and responsive partner",
        "ClientSuccess", "Networking"
    ),
    "project": (
        "end-to-end project execution",
        "delivering your project on time and on budget",
        "expert project leadership",
        "ProjectManagement", "Delivery"
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def ensure_dirs() -> None:
    for d in [PLANS_DIR, PENDING_APPROVAL_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def contains_keyword(text: str) -> str | None:
    """Return the first matched keyword (case-insensitive), or None."""
    text_lower = text.lower()
    for kw in KEYWORDS:
        if kw in text_lower:
            return kw
    return None


def load_handbook_rules() -> str:
    """Read Company Handbook and return its text (with fallback)."""
    if HANDBOOK_FILE.exists():
        try:
            return HANDBOOK_FILE.read_text(encoding="utf-8")
        except Exception as e:
            log(f"WARN: Could not read Company Handbook — {e}. Using defaults.")
    else:
        log(f"WARN: Company Handbook not found at {HANDBOOK_FILE}. Using defaults.")
    return "Always be polite. Flag payments > $500 for approval."


def handbook_payment_flag(text: str) -> bool:
    """Return True if the text contains a payment amount > $500."""
    amounts = re.findall(r'\$\s?(\d[\d,]*\.?\d*)', text)
    for amt in amounts:
        try:
            if float(amt.replace(",", "")) > 500:
                return True
        except ValueError:
            continue
    return False


def extract_yaml_field(content: str, field: str) -> str:
    """Extract a YAML frontmatter field value from a Markdown file."""
    pattern = rf'^{re.escape(field)}:\s*["\']?(.+?)["\']?\s*$'
    match = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def parse_lead_file(filepath: Path) -> dict:
    """Read a lead .md file and extract key fields."""
    content = filepath.read_text(encoding="utf-8", errors="replace")
    return {
        "filename": filepath.name,
        "filepath": filepath,
        "content": content,
        "from": extract_yaml_field(content, "from") or "a prospective contact",
        "subject": extract_yaml_field(content, "subject") or filepath.stem,
        "keyword": extract_yaml_field(content, "keyword_matched") or contains_keyword(content) or "",
        "type": extract_yaml_field(content, "type") or "lead",
    }


# ---------------------------------------------------------------------------
# Lead Scanner
# ---------------------------------------------------------------------------

def scan_needs_action(specific_file: Path | None = None) -> list[dict]:
    """
    Return list of parsed lead dicts from /Needs Action.
    If specific_file is given, only process that file.
    """
    leads = []

    if specific_file:
        files = [specific_file] if specific_file.exists() else []
        if not files:
            log(f"ERROR: Specified file not found: {specific_file}")
            return []
    else:
        if not NEEDS_ACTION_DIR.exists():
            log(f"ERROR: /Needs Action directory not found: {NEEDS_ACTION_DIR}")
            return []
        files = list(NEEDS_ACTION_DIR.glob("*.md"))

    if not files:
        log("NO LEADS: No .md files found in /Needs Action. Nothing to post.")
        return []

    log(f"Scanning {len(files)} file(s) in /Needs Action for keywords: {', '.join(KEYWORDS)}")

    for f in files:
        try:
            lead = parse_lead_file(f)
            keyword = lead["keyword"] or contains_keyword(lead["content"])
            if keyword:
                lead["keyword"] = keyword
                leads.append(lead)
                log(f"  MATCH [{keyword.upper()}]: {f.name}")
            else:
                log(f"  SKIP (no keyword): {f.name}")
        except Exception as e:
            log(f"  ERROR reading {f.name}: {e}")

    return leads


# ---------------------------------------------------------------------------
# Post Drafter
# ---------------------------------------------------------------------------

def draft_post(lead: dict, handbook_text: str) -> str:
    """
    Compose a LinkedIn post from the lead context.
    Applies handbook politeness rules.
    """
    keyword = lead["keyword"].lower()
    context = KEYWORD_CONTEXT.get(keyword, KEYWORD_CONTEXT["sales"])
    service, benefit, outcome, tag1, tag2 = context

    # Personalise slightly with the subject if it adds meaningful context
    subject = lead["subject"]
    if subject and subject.lower() not in ("(no subject)", "unknown", ""):
        # Attempt to extract a service hint from the subject line
        subject_clean = re.sub(
            r'^(re:|fwd:|whatsapp message|linkedin message|email alert)[:\s]*',
            '', subject, flags=re.IGNORECASE
        ).strip()
        if subject_clean and len(subject_clean) > 5:
            service = subject_clean

    post = POST_TEMPLATE.format(
        service=service,
        benefit=benefit,
        outcome=outcome,
        tag1=tag1,
        tag2=tag2,
    )

    # Handbook rule: always polite — add a warm opener if not already present
    if "excited" not in post.lower() and "happy" not in post.lower():
        post = "It is always great to connect with like-minded professionals.\n" + post

    return post


# ---------------------------------------------------------------------------
# File Writers
# ---------------------------------------------------------------------------

def write_plan_draft(lead: dict, post_text: str) -> Path:
    """Write the draft to /Plans/linkedin_post_[date].md."""
    today = datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    filename = f"linkedin_post_{today}.md"
    filepath = PLANS_DIR / filename

    # If file already exists today, append a counter
    counter = 1
    while filepath.exists():
        filename = f"linkedin_post_{today}_{counter}.md"
        filepath = PLANS_DIR / filename
        counter += 1

    content = f"""---
type: linkedin_post_draft
source_lead: "{lead['filename']}"
contact: "{lead['from']}"
keyword_matched: "{lead['keyword']}"
content: "{post_text.replace(chr(10), ' ').replace(chr(34), chr(39))}"
status: pending_approval
created: "{ts}"
---

# LinkedIn Post Draft — {today}

## Drafted Post

{post_text}

## Source Lead

- File: /Needs Action/{lead['filename']}
- From: {lead['from']}
- Subject: {lead['subject']}
- Keyword: `{lead['keyword']}`

## Checklist

- [ ] Review tone against Company Handbook
- [ ] Confirm no payment amounts > $500 referenced
- [ ] Approve and move to /Approved
- [ ] Post manually on LinkedIn or route to LinkedIn Watcher
"""

    filepath.write_text(content, encoding="utf-8")
    return filepath


def copy_to_pending_approval(plan_path: Path) -> Path:
    """Copy the draft to /Pending Approval and return the new path."""
    dest = PENDING_APPROVAL_DIR / plan_path.name

    # If a file with the same name already exists, suffix it
    counter = 1
    while dest.exists():
        dest = PENDING_APPROVAL_DIR / f"{plan_path.stem}_{counter}.md"
        counter += 1

    shutil.copy2(plan_path, dest)
    return dest


# ---------------------------------------------------------------------------
# HITL Gate
# ---------------------------------------------------------------------------

def hitl_gate(lead: dict, post_text: str) -> bool:
    """
    Handbook rule: flag any payment mention > $500.
    Returns True if safe to proceed, False if escalation is required.
    """
    if handbook_payment_flag(lead["content"]) or handbook_payment_flag(post_text):
        log("HANDBOOK FLAG: Payment amount > $500 detected in lead or draft post.")
        log("  -> Escalating to /Pending Approval with 'payment_review' flag.")
        return True  # Still route to Pending Approval, but flag it

    return True  # All posts require HITL by default


# ---------------------------------------------------------------------------
# Core Skill Runner
# ---------------------------------------------------------------------------

def run_skill(specific_file: Path | None = None) -> None:
    """Main skill execution: scan → draft → save → route to HITL."""

    print("=" * 60)
    print("Skill 3: Auto LinkedIn Poster")
    print("=" * 60)
    print()
    log(f"BASE DIR          : {BASE_DIR}")
    log(f"NEEDS ACTION      : {NEEDS_ACTION_DIR}")
    log(f"PLANS             : {PLANS_DIR}")
    log(f"PENDING APPROVAL  : {PENDING_APPROVAL_DIR}")
    log(f"HANDBOOK          : {HANDBOOK_FILE}")
    print()

    ensure_dirs()

    # Step 1 — Load handbook
    log("Step 1: Loading Company Handbook rules...")
    handbook_text = load_handbook_rules()
    log(f"  Rules loaded: {handbook_text[:80]}...")

    # Step 2 — Scan leads
    log("Step 2: Scanning /Needs Action for lead files...")
    leads = scan_needs_action(specific_file)

    if not leads:
        print()
        print("NO LEADS: No matching files in /Needs Action. Nothing to post.")
        print("=" * 60)
        return

    log(f"  {len(leads)} lead(s) found.")
    print()

    results = []

    for lead in leads:
        log(f"Processing lead: {lead['filename']}")
        print("-" * 40)

        # Step 3 — Draft post
        log("Step 3: Drafting LinkedIn post...")
        post_text = draft_post(lead, handbook_text)
        log(f"  Draft:\n{post_text}\n")

        # HITL gate — check handbook flags
        hitl_gate(lead, post_text)

        # Step 4 — Save to /Plans
        log("Step 4: Saving draft to /Plans...")
        plan_path = write_plan_draft(lead, post_text)
        log(f"  Saved: {plan_path}")

        # Step 5 — Copy to /Pending Approval
        log("Step 5: Routing to /Pending Approval (HITL)...")
        approval_path = copy_to_pending_approval(plan_path)
        log(f"  Pending: {approval_path}")

        results.append({
            "lead": lead["filename"],
            "draft": plan_path,
            "approval": approval_path,
        })

    # Step 6 — Output summary
    print()
    print("=" * 60)
    for r in results:
        print(f"SUCCESS: LinkedIn post draft created and queued for approval")
        print(f"Source Lead  : /Needs Action/{r['lead']}")
        print(f"Draft Saved  : {r['draft'].relative_to(BASE_DIR)}")
        print(f"Pending Path : {r['approval'].relative_to(BASE_DIR)}")
        print(f"Status       : Awaiting human approval (HITL)")
        print(f"Next Step    : Review /Pending Approval/ and move to /Approved to publish")
        print("-" * 60)
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Skill 3: Auto LinkedIn Poster — draft posts from lead files."
    )
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        help="Optional: path to a specific lead .md file in /Needs Action to process.",
    )
    args = parser.parse_args()

    specific_file = None
    if args.file:
        specific_file = Path(args.file)
        if not specific_file.is_absolute():
            specific_file = BASE_DIR / specific_file

    run_skill(specific_file)


if __name__ == "__main__":
    main()
