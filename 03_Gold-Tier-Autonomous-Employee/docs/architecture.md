# Gold Tier — Architecture Documentation

> **Tier Declaration: Gold Tier**
> Autonomous Employee — acts without constant human oversight.
> Generated: 2026-02-23

---

## Overview

Gold Tier is the third level in the HackathonAI Employee Vault progressive system.
It extends Silver Tier with full autonomous execution: the AI Employee can classify,
plan, route, and process tasks without waiting for a human prompt at each step.

Human intervention is still required at designated **HITL gates** (high-value payments,
outbound lead responses, published social posts) — but the system reaches those gates
and queues work entirely on its own.

---

## System Architecture Diagram (ASCII)

```
╔══════════════════════════════════════════════════════════════════════════╗
║                    EXTERNAL INPUT CHANNELS                               ║
║  Gmail  │  LinkedIn  │  WhatsApp  │  Twitter  │  Facebook/Instagram  │  ║
║  Inbox  │    Feed    │   Chats    │   Feed    │     Messages/DMs      │  ║
╚════╤═════╧═════╤══════╧═════╤═════╧═════╤═════╧══════════╤════════════╝
     │           │            │           │                 │
     ▼           ▼            ▼           ▼                 ▼
╔══════════════════════════════════════════════════════════════════════════╗
║                         WATCHER LAYER                                    ║
║  gmail_watcher  │  linkedin_watcher  │  whatsapp_watcher               ║
║  twitter_watcher│  facebook_instagram_watcher  │  filesystem_watcher    ║
║                                                                          ║
║  • Runs continuously (PM2 / background)                                  ║
║  • Polls on interval (30–120 s) or watches filesystem events             ║
║  • Normalises input → YAML-frontmatter .md files                         ║
║  • Writes to /Needs Action/                                              ║
║  • Error recovery: exponential backoff (max 3 retries, 1–60 s)          ║
╚══════════════════════════╤═══════════════════════════════════════════════╝
                           │
                           ▼
╔══════════════════════════════════════════════════════════════════════════╗
║                     /Needs Action/  (inbox queue)                        ║
║  FACEBOOK_*.md  │  LINKEDIN_*.md  │  TWITTER_*.md  │  FILE_*.md  │ ...  ║
╚══════════════════════════╤═══════════════════════════════════════════════╝
                           │
                           ▼
╔══════════════════════════════════════════════════════════════════════════╗
║                    RALPH LOOP RUNNER                                     ║
║                  tools/ralph_loop_runner.py                              ║
║                                                                          ║
║  ITERATION CYCLE (up to 20 iterations, no API required):                ║
║                                                                          ║
║  1. DISCOVER   list /Needs Action → find unprocessed files               ║
║  2. CLASSIFY   YAML type + keyword scan + dollar-amount rules            ║
║  3. PLAN       write Plans/Plan_[ts]_[slug].md with checkboxes           ║
║  4. EXECUTE    run_skill(cross_domain_integrator) for leads              ║
║  5. ROUTE      move → Done  OR  Pending Approval (HITL gate)            ║
║  6. VERIFY     confirm /Needs Action empty                               ║
║  7. COMPLETE   emit TASK_COMPLETE, write loop log + audit entry          ║
╚═══════╤══════════════════════════╤════════════════════════════════════════╝
        │                          │
        ▼                          ▼
 ┌─────────────┐          ┌──────────────────────┐
 │    /Done/   │          │  /Pending Approval/  │  ← HITL gate
 │ (no review  │          │  (human review req.) │
 │  required)  │          └──────────┬───────────┘
 └─────────────┘                     │
                           ┌─────────┴──────────┐
                           ▼                    ▼
                      /Approved/           /Rejected/
                           │
                           ▼
╔══════════════════════════════════════════════════════════════════════════╗
║                         SKILL LAYER                                      ║
║  Reads /Approved/ → executes action → logs to /Done/ + /Logs/           ║
║                                                                          ║
║  Skill 3  auto_linkedin_poster.py   → publish LinkedIn post via MCP     ║
║  Skill 4  hitl_approval_handler.py  → process Approved / Rejected       ║
║  Skill 5  cross_domain_integrator.py→ route personal vs business items  ║
║  Skill 6  social_summary_generator.py → FB/IG → lead score → draft     ║
║  Skill 7  twitter_post_generator.py → Twitter → tweet/DM draft         ║
║  Skill 8  weekly_audit_briefer.py   → CEO briefing → /Briefings/        ║
╚═══════════════════════════╤══════════════════════════════════════════════╝
                            │
                            ▼
╔══════════════════════════════════════════════════════════════════════════╗
║                       MCP LAYER  (email-mcp)                             ║
║  mcp_servers/email-mcp/index.js  (Node.js)                               ║
║                                                                          ║
║  Tools: draft_email │ approve_draft │ reject_draft │ send_email          ║
║         list_email_drafts                                                ║
║  Gate : send_email only works if draft is in /Approved/                  ║
║  Auth : Gmail OAuth2 (credentials.json + token.json)                    ║
╚══════════════════════════════════════════════════════════════════════════╝
                            │
                            ▼
╔══════════════════════════════════════════════════════════════════════════╗
║                     SHARED INFRASTRUCTURE                                ║
║                                                                          ║
║  error_recovery.py   with_retry() │ log_watcher_error() │               ║
║                      log_skill_error() │ write_manual_action_plan()      ║
║                                                                          ║
║  audit_logger.py     log_action() → /Logs/audit_YYYY-MM-DD.json         ║
║                      purge_old_logs(90 days) │ get_weekly_summary()     ║
║                      build_markdown_section() → CEO briefing            ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

## File-Based State Machine

Every document is a `.md` file with YAML frontmatter. Files flow through folders
as pipeline stages — the folder name **is** the state.

```
/Inbox/             Raw drop zone (filesystem_watcher monitors this)
    │
    ▼
/Needs Action/      Watcher output — items awaiting classification
    │
    ├──────────────────────────────────────────┐
    ▼                                          ▼
/Plans/             Skill drafts, Plan.md    /Done/       Completed (no HITL needed)
    │               checkbox plans
    ▼
/Pending Approval/  HITL gate — human must act
    │
    ├──────────────┐
    ▼              ▼
/Approved/      /Rejected/
    │
    ▼
/Done/          (after MCP execution)

Side channels:
  /Logs/          Append-only: watcher events, loop transcripts, audit JSON Lines
  /Errors/        Skill/watcher error reports
  /Briefings/     Weekly CEO briefing Markdown files
```

### YAML Frontmatter Schema

Every file carries machine-readable metadata:

```yaml
---
type: email_draft | linkedin_post_draft | payment | sales_lead |
      file_drop | generic | facebook_message | twitter_dm | ...
status: pending_approval | approved | rejected | sent | executed | pending
priority: high | medium | low
created: "YYYY-MM-DD HH:MM:SS"
channel: email | linkedin | whatsapp | twitter | facebook | instagram
from: "sender@example.com"
---
```

---

## Components

### Watchers (6)

| File | Channel | Interval | Method |
|------|---------|----------|--------|
| `watchers/gmail_watcher.py` | Gmail inbox | 120 s | Gmail API (OAuth2) |
| `watchers/linkedin_watcher.py` | LinkedIn feed + DMs | 60 s | Playwright browser automation |
| `watchers/whatsapp_watcher.py` | WhatsApp Web | 30 s | Playwright browser automation |
| `watchers/twitter_watcher.py` | Twitter/X feed + DMs | 60 s | Playwright browser automation |
| `watchers/facebook_instagram_watcher.py` | FB messages + IG DMs | 60 s | Playwright, shared session |
| `watchers/filesystem_watcher.py` | Local /Inbox/ folder | event-driven | watchdog Observer |

All watchers share the same error recovery contract:
- Exponential backoff on network/navigation failures (max 3 retries, 1–60 s delay)
- Error logged to `/Logs/error_[watcher]_[date].log`
- Graceful skip on bad input — loop continues

---

### Skills (8)

| # | File | Purpose | Output |
|---|------|---------|--------|
| 1 | `Skill1_BasicFileHandler.md` | Spec: file classification rules | — (reference) |
| 2 | `Skill2_TaskAnalyzer.md` | Spec: task analysis prompt | — (reference) |
| 3 | `Skills/auto_linkedin_poster.py` | Post LinkedIn drafts from /Needs Action | Draft → /Pending Approval |
| 4 | `Skills/hitl_approval_handler.py` | Process /Approved + /Rejected folders | Execute or archive |
| 5 | `Skills/cross_domain_integrator.py` | Route items: personal → HITL, business → LinkedIn draft | Routing to Plans/ |
| 6 | `Skills/social_summary_generator.py` | FB/IG input → lead score → reply draft | Draft → /Plans |
| 7 | `Skills/twitter_post_generator.py` | Twitter input → tweet + DM draft | Draft → /Pending Approval |
| 8 | `Skills/weekly_audit_briefer.py` | Scan Done/Logs/bottlenecks → CEO briefing | `/Briefings/ceo_briefing_[date].md` |

All skills share the same error recovery contract:
- try/except around all logic
- On failure: write to `/Errors/skill_error_[date].md`
- On MCP failure: write manual action plan to `/Plans/`
- All actions logged via `audit_logger.log_action()`

---

### Shared Modules (2)

#### `Skills/error_recovery.py`
Central error handling library used by all watchers and skills.

```python
with_retry(fn, max_retries=3, base_delay=1.0, max_delay=60.0)
log_watcher_error(name, exc, context, tb)  → /Logs/error_{name}_{date}.log
log_skill_error(name, exc, context, tb)    → /Errors/skill_error_{date}.md
write_manual_action_plan(name, desc, ctx)  → /Plans/manual_{name}_{date}.md
```

#### `Skills/audit_logger.py`
Structured JSON Lines audit trail for every action in the system.

```python
log_action(action_type, actor, target, parameters, approval_status, result)
  → /Logs/audit_YYYY-MM-DD.json  (one JSON object per line, never raises)

purge_old_logs(days=90)         → deletes audit_*.json older than 90 days
get_weekly_summary(days=7)      → aggregated stats dict
build_markdown_section(summary) → Markdown for CEO briefing
```

Audit action types: `skill_start` | `skill_complete` | `skill_error` |
`item_drafted` | `item_queued` | `item_routed` | `item_classified` |
`hitl_queued` | `hitl_executed` | `hitl_rejected` | `briefing_written` |
`loop_start` | `loop_iteration` | `tool_executed` | `loop_complete` | `log_purged`

---

### Loop Runner

#### `tools/ralph_loop_runner.py` — Ralph Wiggum Loop (No-API Mode)

The autonomous orchestrator. Runs the full 6-step pipeline locally with
**no Anthropic API key required** — pure rule-based Python.

```
Inputs   : /Needs Action/*.md
Outputs  : Plans/*.md  +  file moves  +  skill invocations
Logs     : /Logs/ralph_loop_[ts].md  +  /Logs/audit_[date].json

Classification rules (in priority order):
  1. YAML type field          → direct mapping
  2. Dollar amount > $500     → needs_hitl = True, priority = high
  3. Sales keyword match      → type = sales_lead
  4. Payment keyword match    → type = payment
  5. Default                  → type = generic → route = Done

Routing rules:
  payment + amount > $500        → Pending Approval
  sales_lead / multi_step        → run cross_domain_integrator → Pending Approval
  email_draft / linkedin_post_draft  → Pending Approval
  file_drop / generic            → Done
```

CLI:
```bash
python tools/ralph_loop_runner.py
python tools/ralph_loop_runner.py "Process multi-step task in Needs_Action" --max-iterations 20
python tools/ralph_loop_runner.py --create-test-lead
python tools/ralph_loop_runner.py --dry-run
/ralph-loop "Process multi-step task in Needs_Action" --max-iterations 20
```

---

### MCP Server

#### `mcp_servers/email-mcp/index.js`
Node.js MCP server that exposes Gmail send capability as Claude Code tools.
Enforces the HITL gate — `send_email` only works when the draft is in `/Approved/`.

```
Tools: draft_email | approve_draft | reject_draft | send_email | list_email_drafts
Auth : Gmail OAuth2 (credentials.json → token.json via --auth flow)
Port : stdio (Claude Code MCP client)
```

---

## Data Flow: Sales Lead (Multi-Step Example)

```
1. External email arrives in Gmail inbox
        ↓
2. gmail_watcher.py polls Gmail API (every 120 s)
   → writes GMAIL_[ts]_[sender].md to /Needs Action/
        ↓
3. ralph_loop_runner.py discovers file in /Needs Action/
   Iteration 1: reads + classifies → sales_lead / high / $8,500 (> $500)
   Iteration 2: writes Plans/Plan_[ts]_lead.md with checkbox steps
   Iteration 3: run_skill("cross_domain_integrator")
                  → CDI reads Needs Action, scores lead, writes LinkedIn draft
   Iteration 4: moves file → /Pending Approval/
   Iteration 5: verifies /Needs Action/ empty → TASK_COMPLETE
        ↓
4. Human reviews /Pending Approval/GMAIL_[ts]_[sender].md
   → moves to /Approved/ (or /Rejected/)
        ↓
5. hitl_approval_handler.py --monitor detects /Approved/ file
   → calls MCP send_email (or posts LinkedIn draft)
   → moves to /Done/
   → logs: audit_logger.log_action("hitl_executed", ...)
        ↓
6. weekly_audit_briefer.py (Sunday) reads /Done/ + /Logs/
   → generates /Briefings/ceo_briefing_[date].md
   → includes ## Audit Log Summary section from audit_logger
```

---

## Folder Structure

```
03_Gold Tier Autonomous Employee/
├── Company Handbook.md          ← rules read by every skill at runtime
├── Business_Goals.md            ← used by weekly_audit_briefer suggestions
├── Dashboard.md                 ← Obsidian dashboard
├── mcp.json                     ← Claude Code MCP config
│
├── watchers/                    ← 6 channel watchers
│   ├── gmail_watcher.py
│   ├── linkedin_watcher.py
│   ├── whatsapp_watcher.py
│   ├── twitter_watcher.py
│   ├── facebook_instagram_watcher.py
│   └── filesystem_watcher.py
│
├── Skills/                      ← 6 executable skills + 2 shared modules
│   ├── error_recovery.py        ← shared: retry, error logging
│   ├── audit_logger.py          ← shared: JSON Lines audit trail
│   ├── auto_linkedin_poster.py
│   ├── hitl_approval_handler.py
│   ├── cross_domain_integrator.py
│   ├── social_summary_generator.py
│   ├── twitter_post_generator.py
│   └── weekly_audit_briefer.py
│
├── tools/
│   └── ralph_loop_runner.py     ← autonomous orchestrator (no API required)
│
├── mcp_servers/
│   └── email-mcp/index.js       ← Gmail MCP server (Node.js)
│
├── docs/                        ← this directory
│   ├── architecture.md
│   └── lessons_learned.md
│
├── Inbox/                       ← raw drop zone
├── Needs Action/                ← watcher output / loop input
├── Plans/                       ← skill drafts and Plan.md files
├── Pending Approval/            ← HITL gate queue
├── Approved/                    ← human-approved items
├── Rejected/                    ← human-rejected items
├── Done/                        ← completed items
├── Logs/                        ← event logs + audit JSON Lines
├── Errors/                      ← skill/watcher error reports
└── Briefings/                   ← weekly CEO briefing files
```

---

## Running the Full System

### One-time setup
```bash
# Gmail OAuth
cd mcp_servers/email-mcp && npm install && node index.js --auth

# Python dependencies
pip install watchdog playwright anthropic google-auth google-api-python-client
playwright install chromium
```

### Start watchers (PM2 recommended)
```bash
pm2 start watchers/gmail_watcher.py          --interpreter python --name gmail-watcher
pm2 start watchers/linkedin_watcher.py       --interpreter python --name linkedin-watcher
pm2 start watchers/whatsapp_watcher.py       --interpreter python --name whatsapp-watcher
pm2 start watchers/twitter_watcher.py        --interpreter python --name twitter-watcher
pm2 start watchers/facebook_instagram_watcher.py --interpreter python --name fb-ig-watcher
pm2 start watchers/filesystem_watcher.py     --interpreter python --name fs-watcher
pm2 save
```

### Run the loop (no API key needed)
```bash
python tools/ralph_loop_runner.py
# or with custom prompt and iteration limit:
python tools/ralph_loop_runner.py "Process multi-step task in Needs_Action" --max-iterations 20
```

### Run skills individually
```bash
python Skills/hitl_approval_handler.py --monitor    # watch Approved/ continuously
python Skills/weekly_audit_briefer.py               # generate CEO briefing (Sunday)
python Skills/cross_domain_integrator.py            # route Needs Action items
```

### Start MCP server
```bash
node mcp_servers/email-mcp/index.js
```

---

*Gold Tier — HackathonAI Employee Vault*
*Tier 3 of 4: Bronze → Silver → **Gold** → Platinum*
