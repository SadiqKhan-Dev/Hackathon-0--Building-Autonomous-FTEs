---
type: test_guide
title: "Gold Tier Error Recovery — Test Guide"
created: "2026-02-23"
skill: error_recovery
---

# Gold Tier Error Recovery — Test Guide

> **Purpose:** Simulate failures in each watcher and skill, then verify that
> error logs are written, retries fire with exponential backoff, and the loop
> continues without crashing.

---

## Architecture Summary

All error recovery flows through `Skills/error_recovery.py`:

| Function | What it does | Output |
|---|---|---|
| `with_retry(fn, ...)` | Retries up to 3×, delay 1 → 2 → 4 … 60s | Console retry lines |
| `log_watcher_error(watcher, exc, ...)` | Appends structured entry | `/Logs/error_{watcher}_{date}.log` |
| `log_skill_error(skill, exc, ...)` | Appends structured entry | `/Errors/skill_error_{date}.md` |
| `write_manual_action_plan(skill, ...)` | Writes fallback task card | `/Plans/manual_{skill}_{date}.md` |

---

## Part 1 — Watcher Error Recovery Tests

### 1A. Gmail Watcher (`watchers/gmail_watcher.py`)

**Simulate: network timeout on API list call**

1. Start the watcher normally.
2. Disconnect the network interface (or block `googleapis.com` in your hosts file / firewall).
3. Wait for the next poll cycle (≤ 120 s).

**Expected console output:**
```
[2026-02-23 10:01:00] --- Poll #2 ---
[2026-02-23 10:01:01] RETRY [gmail_list_messages] attempt 1/3 failed — ConnectionError: ...
                       Retrying in 1s...
[2026-02-23 10:01:03] RETRY [gmail_list_messages] attempt 2/3 failed — ConnectionError: ...
                       Retrying in 2s...
[2026-02-23 10:01:07] RETRY [gmail_list_messages] all 3 attempts exhausted — ConnectionError: ...
[2026-02-23 10:01:07] ERROR: Gmail API call failed after retries - ...
[2026-02-23 10:01:07] Sleeping 120s until next check...
```

**Verify:**
```
# Error log created:
Logs/error_gmail_2026-02-23.log

# Content includes:
WATCHER   : gmail
ERROR     : ConnectionError / HttpError
CONTEXT   : fetch_unread_important failed
```

**Loop stays alive:** reconnect network → next poll succeeds normally.

---

### 1B. LinkedIn Watcher (`watchers/linkedin_watcher.py`)

**Simulate: page navigation timeout**

1. Start the watcher with an existing session (`/session/linkedin` exists).
2. Block `www.linkedin.com` in your firewall, or kill your WiFi for one cycle.

**Expected output:**
```
[...] RETRY [linkedin_goto_messages] attempt 1/3 failed — TimeoutError: ...
      Retrying in 2s...
[...] RETRY [linkedin_goto_messages] attempt 2/3 failed — TimeoutError: ...
      Retrying in 4s...
[...] RETRY [linkedin_goto_messages] all 3 attempts exhausted — TimeoutError: ...
[...] WARN: Could not scrape messages - ...
```

**Verify:** `Logs/error_linkedin_2026-02-23.log` is appended with watcher=linkedin.

**Loop continues:** notifications still attempted; next poll cycle starts after sleep.

---

### 1C. WhatsApp Watcher (`watchers/whatsapp_watcher.py`)

**Simulate: bad input (malformed DOM element)**

1. Start the watcher.
2. The inner `extract_unread_chats` function wraps each chat item in `try/except`. To test: temporarily add a `raise ValueError("bad element")` inside the `for item in chat_items:` loop in a local copy, run once, then revert.

**Expected output:**
```
[...] WARN: Error reading chat list - bad element
```
**Verify:** `Logs/error_whatsapp_2026-02-23.log` written. The next chat item is still processed (skip-and-continue).

---

### 1D. File System Watcher (`watchers/filesystem_watcher.py`)

**Simulate: permission error on copy**

1. Create a file in `/Inbox/` named `test_locked.txt`.
2. Lock it (Windows: open it exclusively in Notepad; Linux/macOS: `chmod 000`).
3. The watcher will detect it and try to copy.

**Expected output:**
```
[...] RETRY [fs_copy_to_needs_action] attempt 1/3 failed — PermissionError: ...
      Retrying in 1s...
[...] ERROR: Permission denied - test_locked.txt
```
**Verify:** `Logs/error_filesystem_2026-02-23.log`. Observer stays alive; next file drop works normally.

---

### 1E. Twitter Watcher (`watchers/twitter_watcher.py`)

**Simulate: DM scrape network failure**

1. Start the watcher.
2. Kill the network mid-cycle (after DMs start loading, before notifications).

**Expected output:**
```
[...] RETRY [twitter_goto_messages] attempt 1/3 failed — PlaywrightTimeout: ...
      Retrying in 2s...
[...] WARN: Could not scrape DMs -- ...
[...] Checking Twitter/X Notifications...
```
Notification and home-feed scans still proceed independently.

**Verify:** `Logs/error_twitter_2026-02-23.log`

---

### 1F. Facebook & Instagram Watcher (`watchers/facebook_instagram_watcher.py`)

**Simulate: navigation timeout on Messenger**

1. Start the watcher (existing `/session/facebook`).
2. Block `www.facebook.com` for one cycle.

**Expected output** (the newly-added `with_retry` fires):
```
[...] RETRY [fb_goto_messages] attempt 1/3 failed — TimeoutError: ...
      Retrying in 2s...
[...] RETRY [fb_goto_messages] attempt 2/3 failed — TimeoutError: ...
      Retrying in 4s...
[...] RETRY [fb_goto_messages] all 3 attempts exhausted — TimeoutError: ...
[...] WARN: Could not scrape Facebook messages - ...
```

Instagram scrapers (`ig_goto_dms`, `ig_goto_activity`) get the same treatment.

**Verify:** `Logs/error_facebook_instagram_2026-02-23.log`

---

## Part 2 — Skill Error Recovery Tests

### 2A. Skill 3: Auto LinkedIn Poster (`Skills/auto_linkedin_poster.py`)

**Simulate: corrupted lead file**

```bash
# Create a malformed lead file
echo "not valid yaml frontmatter" > "Needs Action/BAD_LEAD.md"

# Run the skill
python Skills/auto_linkedin_poster.py
```

**Expected:** The bad file raises an exception inside `parse_lead_file`. The skill's
top-level `run_skill()` catches it, writes:
- `Errors/skill_error_2026-02-23.md` (error detail)
- `Plans/manual_skill3_autolinkdinposter_2026-02-23.md` (human fallback card)

```
[...] SKILL ERROR: ...
[...] Error logged to: Errors/skill_error_2026-02-23.md
[...] Manual action plan written to: Plans/manual_...md
```

**Verify both files exist and contain the expected content.**

---

### 2B. Skill 4: HITL Approval Handler (`Skills/hitl_approval_handler.py`)

**Simulate: Gmail API unavailable (email_draft execution)**

1. Remove or rename `token.json` temporarily.
2. Create a test pending approval:
   ```bash
   python Skills/hitl_approval_handler.py --write \
     --type email_draft \
     --details "Send test email to test@example.com"
   ```
3. Move the created file from `/Pending Approval/` to `/Approved/`.
4. Run monitor for one cycle:
   ```bash
   python Skills/hitl_approval_handler.py --monitor --max-cycles 1
   ```

**Expected:**
```
[...] EXECUTING [EMAIL_DRAFT] email_draft_...md (priority: medium)
[...] ERROR: email_draft_...md failed — No valid token.json found.
```

**Verify:**
- `Errors/skill_error_2026-02-23.md` — skill=Skill4_HITLApprovalHandler
- `Plans/manual_skill4_hitlapprovalhandler_2026-02-23.md` — manual email send instructions

---

### 2C. Skill 5: Cross Domain Integrator (`Skills/cross_domain_integrator.py`)

**Simulate: Needs Action file unreadable**

```bash
# Create a file with a bad encoding
python -c "
with open('Needs Action/BAD_ENCODE.md', 'wb') as f:
    f.write(b'\xff\xfe invalid bytes')
"
python Skills/cross_domain_integrator.py
```

**Expected:** The bad file is skipped with `ERROR reading BAD_ENCODE.md: ...` — all other files
are still processed. Top-level error recovery writes to `/Errors/` and `/Plans/` only if
the entire skill crashes.

---

### 2D. Skill 6: Social Summary Generator (`Skills/social_summary_generator.py`)

**Simulate: missing Needs Action directory**

```bash
# Temporarily rename the directory
mv "Needs Action" "Needs Action_bak"
python Skills/social_summary_generator.py
mv "Needs Action_bak" "Needs Action"
```

**Expected:**
```
[...] ERROR: /Needs Action not found: .../Needs Action
```
The skill exits gracefully (no crash, no orphaned process).

**Simulate MCP/API failure (dry-run to verify degradation path):**
```bash
python -c "
from pathlib import Path
import sys
sys.path.insert(0, 'Skills')
from error_recovery import log_skill_error, write_manual_action_plan
plan = write_manual_action_plan(
    skill_name='Skill6_SocialSummaryGenerator',
    action_description='Manually review Facebook/Instagram items in /Needs Action/',
    context='Simulated MCP failure test',
    error='SimulatedError: MCP not reachable',
)
print('Plan written:', plan)
"
```

**Verify:** `Plans/manual_skill6_summarygenerator_2026-02-23.md` exists with HITL instructions.

---

### 2E. Skill 7: Twitter Post Generator (`Skills/twitter_post_generator.py`)

**Simulate: read error on a Twitter file**

```bash
echo "" > "Needs Action/TWITTER_20260223_test.md"   # empty file
python Skills/twitter_post_generator.py
```

**Expected:** Empty file is loaded; `extract_yaml_field` returns empty strings; the skill
still runs gracefully (no crash), producing a generic draft or skipping if keyword is absent.

---

### 2F. Skill 8: Weekly Audit Briefer (`Skills/weekly_audit_briefer.py`)

**Simulate: Logs directory missing**

```bash
python Skills/weekly_audit_briefer.py --dry-run
```

Even with no `/Logs/` directory, the skill warns and continues:
```
[...] WARN: /Logs/ not found at .../Logs
```
The briefing is still generated with empty financial sections.

**Simulate fatal crash and degradation plan:**
```bash
python -c "
from pathlib import Path
import sys
sys.path.insert(0, 'Skills')
from error_recovery import log_skill_error, write_manual_action_plan
import ValueError
" 2>/dev/null || true

# Force a ValueError inside run_skill by passing a bad date
python Skills/weekly_audit_briefer.py --date "NOT-A-DATE"
```

**Verify:** `Errors/skill_error_2026-02-23.md` and `Plans/manual_skill8_weeklyauditbr…_2026-02-23.md`

---

## Part 3 — Verify All Output Paths

After running the tests above, confirm these files exist:

### Error logs (append-only, plain text):
```
Logs/error_gmail_2026-02-23.log
Logs/error_linkedin_2026-02-23.log
Logs/error_whatsapp_2026-02-23.log
Logs/error_filesystem_2026-02-23.log
Logs/error_twitter_2026-02-23.log
Logs/error_facebook_instagram_2026-02-23.log
```

Each file has entries like:
```
============================================================
TIMESTAMP : 2026-02-23 10:01:07
WATCHER   : gmail
ERROR     : ConnectionError: ...
CONTEXT   : fetch_unread_important failed
TRACEBACK :
Traceback (most recent call last):
  ...
============================================================
```

### Skill error log (Markdown, date-scoped):
```
Errors/skill_error_2026-02-23.md
```

Contains one `---` block per skill failure with error type, timestamp, traceback.

### Manual action plans (only on MCP / execution failure):
```
Plans/manual_skill3_autolinkdinposter_2026-02-23.md
Plans/manual_skill4_hitlapprovalhandler_2026-02-23.md
Plans/manual_skill6_socialsummarygene_2026-02-23.md
Plans/manual_skill8_weeklyauditbrie…_2026-02-23.md
```

Each plan has `status: pending_approval` and step-by-step human fallback instructions.

---

## Part 4 — Retry Timing Cheat Sheet

| Attempt | Base delay | Formula |
|---------|-----------|---------|
| 1→2 | 1 s | `base_delay = 1` |
| 2→3 | 2 s | `1 × 2` |
| 3 exhausted | — | max 3 retries |
| Playwright watchers | 2 s → 4 s → cap 30 s | `base_delay=2, max_delay=30` |
| Gmail API calls | 1 s → 2 s → cap 60 s | `base_delay=1, max_delay=60` |
| File copy | 1 s → 2 s → cap 10 s | `base_delay=1, max_delay=10` |

---

## Part 5 — Quick Smoke Test (All Components, 2 Minutes)

Run this sequence from the Gold Tier root:

```bash
# 1. Verify error_recovery module loads cleanly
python -c "
import sys; sys.path.insert(0, 'Skills')
from error_recovery import with_retry, log_watcher_error, log_skill_error, write_manual_action_plan
print('error_recovery.py — OK')
"

# 2. Trigger watcher error log (no credentials needed)
python -c "
import sys; sys.path.insert(0, 'Skills')
from error_recovery import log_watcher_error
p = log_watcher_error('smoke_test', RuntimeError('simulated'), context='smoke test run')
print('Watcher error log ->', p)
"

# 3. Trigger skill error log
python -c "
import sys; sys.path.insert(0, 'Skills')
from error_recovery import log_skill_error
p = log_skill_error('SmokeTest', RuntimeError('simulated'), context='smoke test run')
print('Skill error log ->', p)
"

# 4. Trigger manual action plan
python -c "
import sys; sys.path.insert(0, 'Skills')
from error_recovery import write_manual_action_plan
p = write_manual_action_plan('SmokeTest', 'Verify error recovery is wired up.', error='SimulatedError')
print('Manual plan ->', p)
"

# 5. Run each skill in dry-run mode (no external credentials required)
python Skills/cross_domain_integrator.py --dry-run
python Skills/social_summary_generator.py --dry-run
python Skills/twitter_post_generator.py --dry-run
python Skills/weekly_audit_briefer.py --dry-run
```

All five steps should complete without unhandled exceptions.

---

*Generated by Gold Tier Error Recovery Audit — 2026-02-23*
