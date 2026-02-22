# Gold Tier Error Recovery — Test Guide

How to simulate each failure mode and confirm recovery works end-to-end.

---

## 1. Test `with_retry` — Exponential Backoff

```python
# Run from project root
cd "03_Gold Tier Autonomous Employee"
python - <<'EOF'
import sys; sys.path.insert(0, 'Skills')
from error_recovery import with_retry

attempts = []

def flaky_network_call():
    attempts.append(1)
    if len(attempts) < 3:
        raise ConnectionError("Simulated network timeout")
    return "Data fetched successfully"

result = with_retry(flaky_network_call, max_retries=3, base_delay=1, max_delay=60,
                    label="test_api_call")
print(f"Result: {result} — succeeded on attempt {len(attempts)}")
EOF
```

**Expected output:**
```
[HH:MM:SS] RETRY [test_api_call] attempt 1/3 failed — ConnectionError: Simulated network timeout. Retrying in 1s...
[HH:MM:SS] RETRY [test_api_call] attempt 2/3 failed — ConnectionError: Simulated network timeout. Retrying in 2s...
Result: Data fetched successfully — succeeded on attempt 3
```

**Delay schedule:** 1s → 2s → 4s → 8s … capped at 60s.

---

## 2. Test Watcher Error Logging — `/Logs/error_{watcher}_{date}.log`

```python
python - <<'EOF'
import sys; sys.path.insert(0, 'Skills')
from error_recovery import log_watcher_error

err = ConnectionError("LinkedIn page load timed out after 45s")
path = log_watcher_error("linkedin", err, context="scrape_messages poll #7")
print(f"Error log written: {path}")

# Read it back
print(open(path).read())
EOF
```

**Expected file:** `Logs/error_linkedin_2026-02-22.log`

**Expected content:**
```
============================================================
TIMESTAMP : 2026-02-22 HH:MM:SS
WATCHER   : linkedin
ERROR     : ConnectionError: LinkedIn page load timed out after 45s
CONTEXT   : scrape_messages poll #7
TRACEBACK :
NoneType: None
============================================================
```

---

## 3. Test Skill Error Logging — `/Errors/skill_error_{date}.md`

```python
python - <<'EOF'
import sys; sys.path.insert(0, 'Skills')
from error_recovery import log_skill_error

err = RuntimeError("Plans directory write permission denied")
path = log_skill_error("Skill3_AutoLinkedInPoster", err,
                       context="write_plan_draft failed for LINKEDIN_lead.md")
print(f"Error file written: {path}")
EOF
```

**Expected file:** `Errors/skill_error_2026-02-22.md`

---

## 4. Test MCP Degradation — `/Plans/manual_{skill}_{date}.md`

```python
python - <<'EOF'
import sys; sys.path.insert(0, 'Skills')
from error_recovery import write_manual_action_plan

path = write_manual_action_plan(
    skill_name="Skill4_HITLApprovalHandler",
    action_description=(
        "Send email manually:\n"
        "  To      : client@example.com\n"
        "  Subject : Project Proposal Follow-up\n"
        "  Body    : See /Approved/email_draft_2026-02-22.md"
    ),
    context="Gmail API returned 503 Service Unavailable",
    error="HttpError 503",
)
print(f"Manual plan written: {path}")
EOF
```

**Expected file:** `Plans/manual_skill4_hitlapprovalhandler_2026-02-22.md`
**Expected YAML:** `type: manual_action`, `priority: high`, `status: pending_approval`

---

## 5. Simulate Gmail Watcher Poll Failure

The gmail watcher now wraps each poll cycle in try/except. To test:

```python
# Simulate a crashed poll cycle
python - <<'EOF'
import sys, traceback; sys.path.insert(0, 'Skills')
from error_recovery import log_watcher_error

try:
    raise TimeoutError("Gmail API timeout after 30s on poll #5")
except Exception as e:
    tb = traceback.format_exc()
    path = log_watcher_error("gmail", e, context="poll cycle #5", tb=tb)
    print(f"Logged to: {path}")
    print("Watcher loop CONTINUES (no crash)")
EOF
```

**Expected:** Error logged to `Logs/error_gmail_[date].log`, loop does NOT crash.

---

## 6. Simulate Skill Run Failure + Auto Manual Plan

```python
python Skills/auto_linkedin_poster.py --file "Needs Action/nonexistent_file.md"
```

**Expected behaviour:**
1. File not found error caught in `run_skill()`
2. Error appended to `Errors/skill_error_[date].md`
3. Manual action plan written to `Plans/manual_skill3_autolinkeddinposter_[date].md`
4. Process exits cleanly (no unhandled exception)

---

## 7. Simulate HITL Handler MCP/Gmail Failure

```python
# Create a fake email_draft approval file
python Skills/hitl_approval_handler.py --write \
  --type email_draft \
  --details "Send intro to test@example.com" \
  --draft-file "Plans/test_email.md"

# Move it to /Approved to trigger execution
# (Gmail API will fail if not authenticated — that triggers fallback)
python Skills/hitl_approval_handler.py --check
```

**Expected when Gmail unavailable:**
1. `execute_email_draft` raises `HttpError` or `AttributeError`
2. Error caught → logged to `Errors/skill_error_[date].md`
3. Manual action plan created in `Plans/manual_skill4_hitlapprovalhandler_[date].md`
4. Monitor loop continues to next file

---

## 8. Full Watcher Retry Simulation (LinkedIn/Twitter/Facebook)

These watchers wrap `page.goto()` with `with_retry(max_retries=3, base_delay=2, max_delay=30)`.

To observe:
```bash
# Run watcher in foreground, disconnect network briefly
python watchers/linkedin_watcher.py
# Then disconnect WiFi for 5 seconds → reconnect
# Observe: "RETRY [linkedin_goto_messages] attempt 1/3..." in console
# Observe: reconnects and continues normally after network restored
```

---

## Output Paths Summary

| Event | Output File |
|-------|-------------|
| Watcher network error | `Logs/error_{watcher}_{date}.log` |
| Watcher fatal crash | `Logs/error_{watcher}_{date}.log` |
| Skill exception | `Errors/skill_error_{date}.md` |
| MCP/API fail degradation | `Plans/manual_{skill}_{date}.md` |
| Retry log lines | Console stdout only |

---

## Recovery File Examples

### `Logs/error_gmail_2026-02-22.log`
```
============================================================
TIMESTAMP : 2026-02-22 09:15:33
WATCHER   : gmail
ERROR     : HttpError 503: Service Unavailable
CONTEXT   : poll cycle #3
TRACEBACK :
  File "...", line 245, in process_messages
    messages = fetch_unread_important(service)
...
============================================================
```

### `Errors/skill_error_2026-02-22.md`
```yaml
---
skill: Skill3_AutoLinkedInPoster
error_type: FileNotFoundError
timestamp: "2026-02-22 09:20:11"
---
## [2026-02-22 09:20:11] Skill3_AutoLinkedInPoster — FileNotFoundError
**Error:** `FileNotFoundError: No such file: Needs Action/test.md`
**Context:** specific_file=test.md
```

### `Plans/manual_skill4_hitlapprovalhandler_2026-02-22.md`
```yaml
---
type: manual_action
skill: "Skill4_HITLApprovalHandler"
status: pending_approval
priority: high
---
# Manual Action Required: Skill4_HITLApprovalHandler
> The automated skill failed. A human must complete it manually.
## Action Required
Send email manually:
  To      : client@example.com
  Subject : Project Proposal Follow-up
```
