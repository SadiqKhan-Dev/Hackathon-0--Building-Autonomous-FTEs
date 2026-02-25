# Social Automation Workflow — Full Test Guide

> **Tier:** 04 Auto Post AI
> **Last updated:** 2026-02-25
> **Scope:** End-to-end validation of `trigger_posts.py` → HITL review → `master_orchestrator.py` → `social_media_executor_v2.py`

---

## Table of Contents

1. [How the Workflow Works](#1-how-the-workflow-works)
2. [Prerequisites & One-Time Setup](#2-prerequisites--one-time-setup)
3. [Full Loop Walkthrough (Step-by-Step)](#3-full-loop-walkthrough-step-by-step)
4. [Real-Time Monitoring](#4-real-time-monitoring)
5. [Platform-Specific Tests](#5-platform-specific-tests)
6. [Batch / Multi-Platform Test](#6-batch--multi-platform-test)
7. [Retry & Failure Simulation](#7-retry--failure-simulation)
8. [Verifying Success](#8-verifying-success)
9. [Troubleshooting](#9-troubleshooting)
10. [Quick Reference Cheat Sheet](#10-quick-reference-cheat-sheet)

---

## 1. How the Workflow Works

```
[Human / Scheduler]
        │
        ▼
scripts/trigger_posts.py          ← Step 1: Create draft
        │
        │  writes POST_*.md with status: pending_approval
        ▼
Pending_Approval/                 ← Step 2: Human HITL review
        │
        │  human moves file to Approved/ (approve) or Rejected/ (discard)
        ▼
Approved/                         ← Step 3: Orchestrator detects
        │
        │  scripts/master_orchestrator.py (watchdog + 5 s poll)
        │  spawns subprocess:
        ▼
scripts/social_media_executor_v2.py   ← Step 4: Browser automation posts
        │
        │  Playwright → LinkedIn / Facebook
        │  on success → moves file to Done/
        │  on failure → retries up to 3×, then 5-min cooldown
        ▼
Done/                             ← Step 5: Completion
        │
        ▼
Logs/orchestrator_YYYY-MM-DD.log  ← All events logged
Logs/error_<platform>_*.txt       ← Per-failure error records
```

**State machine per file:**

| State | Meaning |
|-------|---------|
| `pending_approval` | Draft saved, awaiting human review |
| (in `Approved/`) | Human approved, queued for execution |
| RUNNING | Executor subprocess active |
| DONE | Successfully posted, file in `Done/` |
| COOLDOWN | All 3 retries exhausted; waits 5 min, then tries again |
| FAILED | Permanent failure after cooldown (very rare) |

**Supported platforms for posting:**

| Value | Posts to |
|-------|----------|
| `linkedin` | LinkedIn feed only |
| `facebook` | Facebook feed only |
| `both` | LinkedIn + Facebook (all must succeed to move to Done) |

> **Note:** `twitter` and `instagram` are listed in `trigger_posts.py` as valid draft
> platforms but `social_media_executor_v2.py` only handles `linkedin`, `facebook`, `both`.
> If you draft a twitter/instagram post, the executor will exit with an error.
> See §7 for how to test this edge case.

---

## 2. Prerequisites & One-Time Setup

### 2.1 Install Python dependencies

```bash
pip install watchdog playwright
playwright install chromium
```

### 2.2 Verify the directory structure

The project root must have these folders (created automatically on first run, but verify):

```
04_Auto Post AI/
├── scripts/
│   ├── trigger_posts.py
│   ├── master_orchestrator.py
│   └── social_media_executor_v2.py
├── Pending_Approval/      ← created by trigger_posts.py
├── Approved/              ← created by master_orchestrator.py
├── Done/                  ← created by executor on first success
├── Logs/                  ← created by orchestrator
└── session/
    ├── linkedin/          ← Playwright persistent session
    └── facebook/          ← Playwright persistent session
```

### 2.3 First-run browser login (one-time per platform)

The executor opens a **visible** browser window the first time (no saved session). You must
log in manually and press Enter in the terminal to continue. The session is then saved to
`session/linkedin/` or `session/facebook/` and all subsequent runs are headless (automatic).

**LinkedIn first login:**
```bash
# Run the executor directly with a test file (see §3, Step 2 for how to create one)
python scripts/social_media_executor_v2.py Approved/POST_test.md
# Browser opens → log into LinkedIn → press Enter → session saved
```

**Facebook first login:**
```bash
# Same, but with a post whose platform field is "facebook"
python scripts/social_media_executor_v2.py Approved/POST_test_fb.md
```

> After first login, you do **not** need to repeat this. Sessions persist in `session/`.

---

## 3. Full Loop Walkthrough (Step-by-Step)

This is the canonical end-to-end test. Run through it once to validate every component.

---

### Step 1 — Generate a draft with `trigger_posts.py`

Open **Terminal A** at the project root:

```bash
cd "E:/VS-CODES/Prompt-MCP/HackathonAI_Employee_Vault/04_Auto Post AI"

# LinkedIn draft (default)
python scripts/trigger_posts.py \
  --platform linkedin \
  --content "Testing our AI social media pipeline — end-to-end validation run. #AI #Automation"
```

**Expected output:**
```
[trigger_posts] Platform : linkedin
[trigger_posts] Content  : Testing our AI social media pipeline — end-to-end validation run. #AI #…
[trigger_posts] Draft saved -> E:\...\Pending_Approval\POST_2026-02-25_HHMMSS.md

Next steps:
  1. Open: E:\...\Pending_Approval\POST_2026-02-25_HHMMSS.md
  2. Review the content.
  3. Move to Approved/ to publish, or Rejected/ to discard.
```

**Verify:** A new `POST_*.md` file now exists in `Pending_Approval/` with this frontmatter:

```yaml
---
platform: linkedin
content: "Testing our AI social media pipeline…"
status: pending_approval
priority: medium
created: "2026-02-25 HH:MM:SS"
type: social_post_draft
---
```

---

### Step 2 — Start the Master Orchestrator

Open **Terminal B** (keep it open for the full test):

```bash
cd "E:/VS-CODES/Prompt-MCP/HackathonAI_Employee_Vault/04_Auto Post AI"
python scripts/master_orchestrator.py
```

**Expected startup output:**
```
[2026-02-25 HH:MM:SS] INFO     [WATCH] watchdog observer started on: …\Approved
[2026-02-25 HH:MM:SS] INFO     [ORCH] Orchestrator started. Watching: …\Approved
[2026-02-25 HH:MM:SS] INFO     [ORCH] Poll interval: 5s | Max retries: 3 | Cooldown: 5 min
```

The orchestrator is now polling `Approved/` every 5 seconds and watching via watchdog.
It will not react to files in `Pending_Approval/` — only `Approved/`.

---

### Step 3 — HITL Review: Approve the Draft

This is the human-in-the-loop gate. In **Terminal A** (or File Explorer):

**Option A — Command line (move file):**
```bash
# Windows bash
mv "Pending_Approval/POST_2026-02-25_HHMMSS.md" "Approved/"
```

**Option B — File Explorer:**
1. Open `Pending_Approval/`
2. Find the `POST_*.md` file you just created
3. Cut (Ctrl+X) and paste (Ctrl+V) into `Approved/`

**Option C — Reject it (to test rejection path):**
```bash
mv "Pending_Approval/POST_2026-02-25_HHMMSS.md" "Rejected/"
```
If rejected, the orchestrator never sees it. The file is archived and no post is made.

---

### Step 4 — Orchestrator Detects and Dispatches

Switch to **Terminal B**. Within 5 seconds of the file arriving in `Approved/`, you should see:

```
[2026-02-25 HH:MM:SS] INFO     [WATCH] Detected new file: POST_2026-02-25_HHMMSS.md
[2026-02-25 HH:MM:SS] INFO     [QUEUE] Enqueued: POST_2026-02-25_HHMMSS.md
[2026-02-25 HH:MM:SS] INFO     [RUN ] POST_2026-02-25_HHMMSS.md — attempt 1/3
```

The orchestrator spawns `social_media_executor_v2.py` as a subprocess. Executor output
streams directly to this terminal.

---

### Step 5 — Executor Posts to LinkedIn/Facebook

You'll see the executor's live output in Terminal B:

```
[2026-02-25 HH:MM:SS] [MAIN] Reading: …\Approved\POST_2026-02-25_HHMMSS.md
[2026-02-25 HH:MM:SS] [MAIN] Post ID  : POST_2026-02-25_HHMMSS
[2026-02-25 HH:MM:SS] [MAIN] Platform : linkedin
[2026-02-25 HH:MM:SS] [MAIN] Content  :
----------------------------------------
Testing our AI social media pipeline…
----------------------------------------
[2026-02-25 HH:MM:SS] [LINKEDIN] Attempt 1/3 …
[2026-02-25 HH:MM:SS] [LinkedIn] Navigating to feed …
[2026-02-25 HH:MM:SS] [LinkedIn] Looking for 'Start a post' button …
[2026-02-25 HH:MM:SS] [LinkedIn] Clicked composer via selector: button.share-box-feed-entry__trigger
[2026-02-25 HH:MM:SS] [LinkedIn] Editor found via: div.ql-editor[contenteditable='true']
[2026-02-25 HH:MM:SS] [LinkedIn] Content typed.
[2026-02-25 HH:MM:SS] [LinkedIn] 'Post' clicked via: button.share-actions__primary-action
[2026-02-25 HH:MM:SS] [LinkedIn] Post submitted successfully.
[2026-02-25 HH:MM:SS] [FILE] Moved to Done/ → POST_2026-02-25_HHMMSS.md
[2026-02-25 HH:MM:SS] [MAIN] All posts published. File moved to Done/.
```

Then back in the orchestrator:
```
[2026-02-25 HH:MM:SS] INFO     [DONE] POST_2026-02-25_HHMMSS.md — posted successfully.
[2026-02-25 HH:MM:SS] INFO     SUMMARY | POST_2026-02-25_HHMMSS.md | SUCCESS | attempts=1
```

---

### Step 6 — Verify Success

```bash
# Check Done/ for the moved file
ls Done/

# Check orchestrator log
cat Logs/orchestrator_2026-02-25.log

# Check LinkedIn feed manually (or Facebook) to confirm the post appeared
```

**Pass criteria:**
- `POST_*.md` is in `Done/` (not in `Approved/` or `Pending_Approval/`)
- `Logs/orchestrator_YYYY-MM-DD.log` contains `SUCCESS` for the file
- Post is visible on the target social platform

---

## 4. Real-Time Monitoring

### Watch the orchestrator log live

```bash
# PowerShell (Windows)
Get-Content Logs/orchestrator_2026-02-25.log -Wait

# Git Bash / WSL
tail -f Logs/orchestrator_2026-02-25.log
```

### Watch folder state changes

```bash
# PowerShell: watch Approved/ for new files
while ($true) {
    $files = Get-ChildItem Approved/ -Filter "POST_*.md" | Measure-Object
    Write-Host "[$(Get-Date -f HH:mm:ss)] Approved: $($files.Count) file(s)"
    Start-Sleep 3
}
```

### Watch Done/ for completions

```bash
# PowerShell
while ($true) {
    $done = Get-ChildItem Done/ -Filter "POST_*.md" | Measure-Object
    Write-Host "[$(Get-Date -f HH:mm:ss)] Done: $($done.Count) post(s)"
    Start-Sleep 5
}
```

---

## 5. Platform-Specific Tests

### 5.1 LinkedIn only

```bash
python scripts/trigger_posts.py \
  --platform linkedin \
  --content "LinkedIn-specific test post from our AI pipeline. #Testing"
# → review in Pending_Approval/ → mv to Approved/ → watch orchestrator
```

### 5.2 Facebook only

```bash
python scripts/trigger_posts.py \
  --platform facebook \
  --content "Facebook-specific test post from our AI pipeline. Automation working!"
# → review in Pending_Approval/ → mv to Approved/ → watch orchestrator
```

### 5.3 Both platforms simultaneously

```bash
python scripts/trigger_posts.py \
  --platform linkedin \
  --content "Cross-platform test. Posting to LinkedIn AND Facebook at the same time!"
```

Then **edit** the draft file before approving to change the platform to `both`:

```bash
# Edit the file
# Change: platform: linkedin
# To:     platform: both
# Then move to Approved/
```

The executor will post to LinkedIn first, then Facebook. The file only moves to `Done/`
if **both** succeed. If one fails, the file stays in `Approved/` for retry.

### 5.4 Edge case — Twitter/Instagram (unsupported by executor)

```bash
python scripts/trigger_posts.py --platform twitter \
  --content "This will fail at executor because twitter is not implemented."
mv "Pending_Approval/POST_*.md" "Approved/"
```

Expected executor output:
```
[ERROR] Unknown platform 'twitter'. Must be: linkedin | facebook | both
```

Orchestrator will log this as a failure and retry 3×, then enter cooldown. You can
manually move the file from `Approved/` to `Rejected/` to clean it up.

---

## 6. Batch / Multi-Platform Test

Test multiple drafts in sequence to verify the queue and sequential dispatch:

```bash
# Create 3 drafts quickly
python scripts/trigger_posts.py --platform linkedin \
  --content "Batch test post #1 — AI pipeline validation. #AI"

python scripts/trigger_posts.py --platform facebook \
  --content "Batch test post #2 — Facebook automation check. #Automation"

python scripts/trigger_posts.py --platform linkedin \
  --content "Batch test post #3 — Multi-post queue test. #Tech"
```

Now review all three in `Pending_Approval/` and approve them all:

```bash
# Approve all at once
mv Pending_Approval/POST_*.md Approved/
```

**Expected behavior:**
- Orchestrator enqueues all 3 files immediately (watchdog + poll picks them up)
- Executor runs them **sequentially** (one at a time, same process)
- Each file moves to `Done/` as it completes
- Log shows 3 `SUCCESS` summary lines

> The orchestrator dispatches sequentially to avoid race conditions on Playwright
> browser automation. There is no parallel posting.

---

## 7. Retry & Failure Simulation

### 7.1 Simulate network failure (kill internet mid-post)

1. Start the full loop as in §3
2. After the orchestrator enqueues a file, disconnect your network
3. The executor will fail with a Playwright navigation error

**Expected behavior in Terminal B:**
```
[LINKEDIN] Attempt 1/3 …
[LINKEDIN] Attempt 1 FAILED: net::ERR_INTERNET_DISCONNECTED
[LINKEDIN] Error logged → error_linkedin_20260225_HHMMSS.txt
[LINKEDIN] Waiting 5s before retry …
[LINKEDIN] Attempt 2/3 …
…
[LINKEDIN] All 3 attempts failed.
[ORCH] [FAIL] POST_*.md — all 3 retries exhausted. Cooling down until HH:MM:SS.
```

After 5-minute cooldown, the orchestrator automatically retries. Reconnect the network
before the cooldown expires to test the recovery path.

### 7.2 Simulate login expiry (delete session)

```bash
# Delete the LinkedIn session to force re-login
rm -rf session/linkedin/

# Now run the orchestrator and approve a file
# Browser will open and prompt for login
```

When the browser opens for login, you have 300 seconds (5 min hard cap from orchestrator)
to log in and press Enter. If you miss the deadline, the orchestrator marks it as timeout.

### 7.3 Simulate a bad file (corrupt YAML)

```bash
# Create a manually broken draft
cat > Approved/POST_bad_test.md << 'EOF'
---
platform:
content:
status: approved
---
Empty content post.
EOF
```

Expected executor output:
```
[ERROR] Post content is empty. Aborting.
```

Orchestrator logs this as exit code 1, retries 3×, then enters cooldown.

### 7.4 Check error logs after failure

```bash
ls Logs/error_linkedin_*.txt
cat Logs/error_linkedin_20260225_HHMMSS.txt
```

Each error log contains: Platform, Attempt number, Timestamp, Error message, full Python traceback.

---

## 8. Verifying Success

### 8.1 Check `Done/` folder

```bash
ls Done/POST_*.md
# Each file here represents a successfully posted item
```

Open any `Done/POST_*.md` — the content and metadata are intact; only the file location changed.

### 8.2 Check orchestrator log

```bash
cat Logs/orchestrator_2026-02-25.log
```

Look for these lines per file:
```
[DONE] POST_*.md — posted successfully.
SUMMARY | POST_*.md | SUCCESS | attempts=1
```

### 8.3 Verify on the actual platforms

| Platform | Where to check |
|----------|---------------|
| LinkedIn | Go to your profile → Activity → Posts |
| Facebook | Go to your profile → Posts / Timeline |

### 8.4 Audit the state machine folders

After a full test cycle, the folder state should be:

```
Pending_Approval/  → empty (all reviewed)
Approved/          → empty (all dispatched)
Done/              → contains all successfully posted files
Rejected/          → contains any manually rejected drafts
Logs/              → orchestrator_YYYY-MM-DD.log + any error_*.txt files
```

---

## 9. Troubleshooting

### Problem: Browser is not logging in / stays on login page

**Symptom:** Executor output shows:
```
[LinkedIn] NOT logged in — please log in manually in the browser window, then press Enter here.
```
or loops forever without opening a browser.

**Fixes:**

1. **Session corrupted** — delete the session folder and re-login:
   ```bash
   rm -rf session/linkedin/
   # Re-run executor; browser will open for fresh login
   ```

2. **Session expired by LinkedIn/Facebook** — same fix as above. Platforms can invalidate sessions
   after ~30 days of inactivity or after suspicious activity detection.

3. **Two-factor authentication triggered** — log in manually in the visible browser, complete
   2FA, then press Enter. The session is saved with the 2FA-passed cookies.

4. **Playwright chromium binary missing:**
   ```bash
   playwright install chromium
   ```

5. **Browser opens but hangs on blank page:** LinkedIn or Facebook changed their DOM.
   Check the executor's selector arrays and update them to match current HTML.

---

### Problem: "Start a post" button not found

**Symptom:**
```
RuntimeError: Could not find 'Start a post' button. LinkedIn layout may have changed.
```

**Fix:** LinkedIn runs A/B tests on UI layouts. The executor tries 5 different selectors.
If all fail, the current selectors are stale. To debug:

```bash
# Run executor standalone to see what page it lands on
python scripts/social_media_executor_v2.py Approved/POST_test.md
# Browser is visible (headless=False) — inspect the DOM manually
# Find the new button selector and add it to start_post_selectors in executor
```

---

### Problem: Orchestrator does not detect files moved to Approved/

**Symptom:** File appears in `Approved/` but orchestrator never logs `[WATCH] Detected`.

**Fixes:**

1. **Watchdog not receiving move events on Windows** — the poll loop (every 5s) should catch
   it anyway. Wait up to 10 seconds.

2. **File does not start with `POST_`** — the orchestrator filters on `FILE_PATTERN = "POST_"`.
   Rename the file or use `trigger_posts.py` to generate it correctly.

3. **File extension is not `.md`** — orchestrator only processes `.md` files.

4. **Orchestrator not running** — verify Terminal B is still active with the orchestrator.

---

### Problem: Executor times out (exit code from timeout)

**Symptom:**
```
[TIMEOUT] POST_*.md — executor timed out after 300s.
```

**Fix:** The orchestrator has a hard 5-minute cap per post attempt. This is usually caused by:
- The login prompt (`input()`) blocking in the executor — log in and press Enter
- Extremely slow internet connection during posting
- LinkedIn/Facebook showing an unexpected dialog that the selector loops can't dismiss

Check `Logs/error_<platform>_*.txt` and any `Logs/error_<platform>_*.png` screenshots for clues.

---

### Problem: File stays in Approved/ after multiple retries

**Symptom:** File is in `Approved/` and the orchestrator log shows multiple `[RETRY]` lines
and eventually `[FAIL]` + `Cooling down`.

**Checks:**
1. Are error logs in `Logs/`? Read the `.txt` file to find the root cause.
2. Is the platform supported? Only `linkedin`, `facebook`, `both` work in the executor.
3. Is the content field empty? Executor exits on empty content.
4. Is the network reachable from your machine?

**Manual recovery:**
```bash
# If you want to clean up a stuck file
mv Approved/POST_stuck.md Rejected/
# Orchestrator will see it's gone and mark it DONE (skipped)
```

---

### Problem: File posted twice (duplicate posts)

**Symptom:** Two identical posts appear on LinkedIn/Facebook.

**Cause:** The file was approved twice (moved to `Approved/` from `Rejected/` or moved back).
The orchestrator tracks by filename and ignores already-tracked DONE files, but if you delete
and recreate the file with the same name, it can run again.

**Prevention:** Never re-approve a file that was already posted. Create a new draft with
`trigger_posts.py` instead.

---

### Problem: `watchdog` is not installed

```
[FATAL] watchdog is not installed. Run: pip install watchdog
```

```bash
pip install watchdog
```

---

### Problem: `playwright` is not installed

```
[FATAL] playwright is not installed. Run: pip install playwright && playwright install chromium
```

```bash
pip install playwright
playwright install chromium
```

---

### Problem: Orchestrator log file not created

**Check:** `Logs/` directory must be writable. The orchestrator creates it automatically.
If running from a restricted directory, try:

```bash
mkdir -p Logs
python scripts/master_orchestrator.py
```

---

## 10. Quick Reference Cheat Sheet

### Create a draft

```bash
# LinkedIn
python scripts/trigger_posts.py --platform linkedin --content "Your post text here"

# Facebook
python scripts/trigger_posts.py --platform facebook --content "Your post text here"

# Both (edit the draft file to change platform to "both" before approving)
python scripts/trigger_posts.py --platform linkedin --content "Cross-platform post"
```

### Run the orchestrator

```bash
python scripts/master_orchestrator.py
# Ctrl+C to stop
```

### Run the executor manually (bypass orchestrator)

```bash
python scripts/social_media_executor_v2.py "Approved/POST_2026-02-25_HHMMSS.md"
```

### Approve a draft

```bash
mv "Pending_Approval/POST_YYYY-MM-DD_HHMMSS.md" "Approved/"
```

### Reject a draft

```bash
mv "Pending_Approval/POST_YYYY-MM-DD_HHMMSS.md" "Rejected/"
```

### Approve all pending drafts at once

```bash
mv Pending_Approval/POST_*.md Approved/
```

### Watch logs live

```bash
# PowerShell
Get-Content Logs/orchestrator_2026-02-25.log -Wait

# Git Bash
tail -f Logs/orchestrator_2026-02-25.log
```

### Reset a stuck file

```bash
# Move it out of Approved/ so orchestrator skips it
mv "Approved/POST_stuck.md" "Rejected/"
```

### Clear browser sessions (force re-login)

```bash
rm -rf session/linkedin/
rm -rf session/facebook/
```

### Full folder state audit

```bash
echo "=== Pending_Approval ===" && ls Pending_Approval/
echo "=== Approved ===" && ls Approved/
echo "=== Done ===" && ls Done/
echo "=== Rejected ===" && ls Rejected/ 2>/dev/null || echo "(empty)"
echo "=== Logs ===" && ls Logs/
```

---

## Appendix A — File Format Reference

Every draft file produced by `trigger_posts.py` follows this exact format:

```markdown
---
platform: linkedin
content: "Your post text here"
status: pending_approval
priority: medium
created: "2026-02-25 10:30:00"
type: social_post_draft
---

# Post Draft — Linkedin

**Platform:** Linkedin
**Created:** 2026-02-25 10:30:00
**Status:** Pending Approval

## Content

Your post text here

---
*Review this file, then move it to `Approved/` to publish or `Rejected/` to discard.*
```

**To change the platform to `both` before approving:** open the `.md` file in any editor
and change `platform: linkedin` to `platform: both`. The executor reads the frontmatter
on execution, so any edits made before the file lands in `Approved/` are respected.

---

## Appendix B — Log File Locations

| Log file | Created by | Content |
|----------|-----------|---------|
| `Logs/orchestrator_YYYY-MM-DD.log` | master_orchestrator.py | All enqueue/run/done/retry/cooldown events |
| `Logs/error_<platform>_HHMMSS.txt` | social_media_executor_v2.py | Per-failure error + traceback |
| `Logs/error_<platform>_HHMMSS.png` | social_media_executor_v2.py | Screenshot at time of failure |
| `Logs/audit_YYYY-MM-DD.json` | audit_logger.py (if integrated) | JSON Lines audit trail |

---

## Appendix C — Integration with Existing System

The `04_Auto Post AI` scripts operate independently of the Gold Tier watcher/skill pipeline.
However, they share the same conceptual state machine (HITL, folder-based routing). Here is
how you can bridge them:

### Option 1 — Drop a post draft via the Gold Tier pipeline

Skills like `auto_linkedin_poster.py` or `cross_domain_integrator.py` already write drafts to
`Pending Approval/` (with space). You can route their output to this pipeline's `Pending_Approval/`
(with underscore) by adjusting the `PENDING_DIR` path in those skills, or by using a watcher
script to mirror files between the two folders.

### Option 2 — Add social posting to `ralph_loop_runner.py`

Add a `run_skill` entry in the Ralph Loop Runner's `SKILL_MAP` that calls:
```python
"post_social": ["python", "scripts/trigger_posts.py", "--platform", "{platform}", "--content", "{content}"]
```

Then the Ralph loop can auto-draft posts when it detects a sales lead or LinkedIn interaction
in `Needs Action/`.

### Option 3 — MCP tool integration

The `mcp.json` in this tier configures `email-mcp`, `calendar-mcp`, and `slack-mcp`. There
is **no social-post MCP server** yet. If you want Claude Code to draft posts directly, you
would need to:
1. Create a `social-mcp` server (Node.js) that wraps `trigger_posts.py`
2. Add it to `mcp.json`
3. Claude Code can then call `draft_social_post(platform, content)` as an MCP tool

This would complete the HITL loop: Claude drafts → MCP writes to `Pending_Approval/` →
human approves → orchestrator posts.
