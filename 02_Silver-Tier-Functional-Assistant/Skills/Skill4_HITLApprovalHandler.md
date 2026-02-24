# Skill 4: HITL Approval Handler

## Purpose
Central Human-in-the-Loop gateway for ALL sensitive actions in the Silver Tier
system. When any skill or watcher produces an action that requires human sign-off
(emails, LinkedIn posts, payments, any high-priority item), this skill:

1. Writes the request to `/Pending Approval/` as a structured `.md` file
2. Monitors `/Approved/` for files moved there by the human reviewer
3. On approval: dispatches execution via the correct integration (Email MCP, LinkedIn Poster)
4. On rejection: archives to `/Rejected/` and writes a log entry
5. Logs every event to `/Logs/hitl_[YYYY-MM-DD].md`

This skill is the **single source of truth** for all HITL state in the system.

---

## Integrated Skills & Services

| Source | Action Type | Execution on Approval |
|--------|------------|----------------------|
| Email MCP (`draft_email`) | `email_draft` | Send via Gmail API |
| Skill 3 (Auto LinkedIn Poster) | `linkedin_post_draft` | Log as ready-to-post |
| Skill 2 (Task Analyzer) | `payment` | Log approved + notify |
| Any watcher or skill | `generic` | Log approved + notify |

---

## Handbook Rules (from `/Company Handbook.md`)
- **Always polite** — all outbound communications checked before dispatch.
- **Payments > $500** — always requires this HITL flow before any action.
- No action is taken without an explicit move to `/Approved/`.

---

## Procedure

### Step 1: Load Company Handbook
Read `/Company Handbook.md` and extract active rules.
Apply rules when assessing incoming requests.

### Step 2: Write Request to /Pending Approval
For any sensitive action, create:
`/Pending Approval/[action_type]_[YYYY-MM-DD_HH-MM-SS].md`

Required YAML frontmatter:
```yaml
---
type: email_draft | linkedin_post_draft | payment | generic
action: send_email | post_linkedin | approve_payment | review
source_skill: Skill3_AutoLinkedInPoster | email-mcp | Skill2_TaskAnalyzer | manual
details: "[human-readable description of what will happen on approval]"
target: "[email address / LinkedIn profile / payment recipient]"
amount: "[payment amount if applicable, else null]"
priority: high | medium | low
status: pending_approval
created: "[YYYY-MM-DD HH:MM:SS]"
draft_file: "[path to the source draft file, if any]"
---
```

### Step 3: Monitor /Approved Folder
Poll `/Approved/` every 15 seconds (configurable).

For each `.md` file found:
- Read YAML frontmatter to determine `type` and `action`
- Check `status` — only process files with `status: approved`
- Route to the correct execution handler (Step 4)
- Mark as processed to avoid double-execution

### Step 4: Execute on Approval

#### 4a — Email Draft (`type: email_draft`)
1. Read source draft from `draft_file` path
2. Extract: `to`, `subject`, `body`, `cc`, `bcc`
3. Send via Gmail API using OAuth2 credentials
4. Update status to `sent`
5. Move processed file to `/Done/`
6. Log: `EMAIL SENT → [to] | Subject: [subject]`

#### 4b — LinkedIn Post Draft (`type: linkedin_post_draft`)
1. Read source draft from `draft_file` path
2. Extract post content
3. Log post as `ready_to_post` in `/Logs/`
4. Update status to `approved_ready`
5. Move to `/Done/`
6. Log: `LINKEDIN POST READY → [filename]`
   *(Note: Actual API posting is Gold Tier. Silver Tier logs and flags for manual publish.)*

#### 4c — Payment (`type: payment`)
1. Read payment details from file
2. Log approval to `/Logs/`
3. Update status to `payment_approved`
4. Move to `/Done/`
5. Log: `PAYMENT APPROVED → [amount] for [target]`

#### 4d — Generic (`type: generic`)
1. Log approval
2. Update status to `approved`
3. Move to `/Done/`

### Step 5: Handle Rejection
If the human moves a file to `/Rejected/` instead of `/Approved/`:
1. Detect file in `/Rejected/` with `status: pending_approval` or `approved`
2. Update status to `rejected`
3. Log: `REJECTED → [filename] | Reason: [rejection_reason if present]`
4. File stays in `/Rejected/` (archived)

### Step 6: Log All Events
Append every event (write, approval, rejection, execution, error) to:
`/Logs/hitl_[YYYY-MM-DD].md`

Log entry format:
```
[HH:MM:SS] [EVENT_TYPE] [filename] | Status: [status] | Action: [action] | Details: [...]
```

---

## State Machine

```
            ┌─────────────────────────────────────────┐
            │          SENSITIVE ACTION TRIGGERED       │
            │  (email draft / post / payment / generic) │
            └────────────────────┬────────────────────┘
                                 │
                                 ▼
            ┌─────────────────────────────────────────┐
            │     WRITE to /Pending Approval/          │
            │   [type]_[timestamp].md                  │
            │   status: pending_approval               │
            └────────────────────┬────────────────────┘
                                 │
                    ┌────────────┴───────────┐
                    │   HUMAN REVIEWS FILE   │
                    └────────────┬───────────┘
                    │                        │
               MOVE TO                  MOVE TO
             /Approved/               /Rejected/
                    │                        │
                    ▼                        ▼
        ┌──────────────────┐    ┌──────────────────────┐
        │ HITL HANDLER     │    │ Log rejection        │
        │ detects approval │    │ Update status        │
        │ Executes action  │    │ Archive in /Rejected │
        └────────┬─────────┘    └──────────────────────┘
                 │
                 ▼
        ┌─────────────────┐
        │ Move to /Done   │
        │ Log to /Logs/   │
        │ Update status   │
        └─────────────────┘
```

---

## Log Format (`/Logs/hitl_[date].md`)

```markdown
---
type: hitl_log
date: "YYYY-MM-DD"
---

# HITL Log — YYYY-MM-DD

| Time     | Event       | File                | Action      | Status  |
|----------|-------------|---------------------|-------------|---------|
| 10:31:00 | QUEUED      | email_draft_*.md    | send_email  | pending |
| 10:35:00 | APPROVED    | email_draft_*.md    | send_email  | approved |
| 10:35:01 | EXECUTED    | email_draft_*.md    | EMAIL SENT  | sent    |
| 10:40:00 | REJECTED    | linkedin_post_*.md  | post        | rejected |
```

---

## Status Reference

| Status | Meaning |
|--------|---------|
| `pending_approval` | Waiting for human review in /Pending Approval |
| `approved` | Human moved to /Approved, execution pending |
| `sent` | Email dispatched via Gmail API |
| `approved_ready` | LinkedIn post ready for manual publish |
| `payment_approved` | Payment cleared for processing |
| `rejected` | Human rejected, archived in /Rejected |
| `executed` | Action completed, archived in /Done |
| `error` | Execution failed — see log for details |

---

## Error Handling

| Condition | Action |
|-----------|--------|
| Gmail API error on send | Update status to `error`, log error, keep in /Approved |
| Draft file missing | Log error, update status to `error_missing_draft` |
| Unrecognised action type | Log warning, treat as `generic` |
| /Approved folder missing | Create folder, continue monitoring |
| Handbook unavailable | Warn and apply default rules |
| Double-execution guard | Track processed IDs in memory; skip already-executed files |

---

## Python Implementation

```bash
# Monitor mode (run continuously, watches /Approved every 15s)
python Skills/hitl_approval_handler.py --monitor

# Write a new pending request manually
python Skills/hitl_approval_handler.py --write \
  --type email_draft \
  --details "Send follow-up to client@example.com" \
  --draft-file "Plans/email_draft_2024-02-19_Follow-up.md"

# Check current pending items (one-shot report)
python Skills/hitl_approval_handler.py --check

# With PM2
pm2 start Skills/hitl_approval_handler.py \
  --interpreter python --name hitl-handler \
  -- --monitor
pm2 save && pm2 startup
```

---

## Integration Points

```
Skill 2: Task Analyzer
  └─► writes payment to /Pending Approval ─► HITL Handler executes on approval

Skill 3: Auto LinkedIn Poster
  └─► writes linkedin_post_draft ─► HITL Handler logs as ready-to-post

Email MCP (email-mcp/index.js)
  └─► writes email_draft ─► HITL Handler sends via Gmail API

Watchers (Gmail, WhatsApp, LinkedIn)
  └─► write urgent leads to /Needs Action
      ─► Task Analyzer routes to /Pending Approval
          ─► HITL Handler executes
```

---

## Example Usage

**Agent invocation:**
```
@HITL Approval Handler check Pending_Approval
```

**Monitor all approvals (continuous):**
```
@HITL Approval Handler monitor Approved
```

**Write a new approval request:**
```
@HITL Approval Handler write email_draft Plans/email_draft_2024-02-19.md
```

**Expected output (check mode):**
```
HITL CHECK COMPLETE
Pending Approval : 2 item(s)
  1. [email_draft]         email_draft_2024-02-19_Follow-up.md  | priority: high
  2. [linkedin_post_draft] linkedin_post_2024-02-19.md          | priority: medium
Approved (queued): 0 item(s)
Log              : /Logs/hitl_2024-02-19.md
```
