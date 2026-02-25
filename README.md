# HackathonAI Employee Vault

> **Build your own AI Employee** — from a local file-watching MVP to a fully autonomous, multi-channel agent with human-in-the-loop safety gates.

A progressive **4-tier system** where each tier is a standalone, self-contained project that builds on the concepts of the previous one. Start simple, go autonomous.

---

## Table of Contents

- [What is this?](#what-is-this)
- [The 4-Tier Model](#the-4-tier-model)
- [Core Architecture](#core-architecture)
  - [File-Based State Machine](#file-based-state-machine)
  - [HITL (Human-in-the-Loop) Pattern](#hitl-human-in-the-loop-pattern)
  - [Watcher → Skill Pipeline](#watcher--skill-pipeline)
- [Tier 1: Bronze — Minimum Viable MVP](#tier-1-bronze--minimum-viable-mvp)
- [Tier 2: Silver — Multi-Channel Assistant](#tier-2-silver--multi-channel-assistant)
- [Tier 3: Gold — Autonomous Employee](#tier-3-gold--autonomous-employee)
- [Tier 4: Platinum — Always-On Cloud Executive](#tier-4-platinum--always-on-cloud-executive)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Dependencies](#dependencies)
- [Configuration & Credentials](#configuration--credentials)
- [How the HITL Gate Works](#how-the-hitl-gate-works)
- [Ralph Loop Runner (Gold)](#ralph-loop-runner-gold)
- [MCP Servers](#mcp-servers)
- [Error Recovery (Gold)](#error-recovery-gold)
- [Audit Trail (Gold)](#audit-trail-gold)
- [Company Handbook Pattern](#company-handbook-pattern)
- [Contributing](#contributing)

---

## What is this?

Most "AI Agent" demos are toy examples. This project builds something real: an **AI Employee** that monitors your business channels (Gmail, LinkedIn, WhatsApp, Twitter, Facebook/Instagram), classifies incoming messages, drafts responses, routes high-value items to you for approval, and executes approved actions automatically.

The system is intentionally **file-based and transparent** — every action is a Markdown file you can read, edit, approve, or reject. No black boxes.

---

## The 4-Tier Model

| Tier | Name | Purpose | Key Tech |
|------|------|---------|----------|
| **Bronze** | Minimum Viable Deliverable | Local file-watching + task organization | `watchdog`, Markdown |
| **Silver** | Functional Assistant | Multi-channel monitoring with HITL approval gates | MCP, Gmail API, Playwright |
| **Gold** | Autonomous Employee | Acts without constant human oversight | All Silver + autonomous loop, 6 channels |
| **Platinum** | Always-On Cloud Executive | Production deployment, continuous operation | All Gold + cloud infrastructure |

Each tier is in its own folder and is **fully self-contained**. You can run Bronze without Silver, or jump straight to Gold.

---

## Core Architecture

### File-Based State Machine

All tiers share the same folder-based state model. Files flow through the system like a pipeline:

```
Inbox/
  └─▶  Needs Action/
          └─▶  Plans/
                  └─▶  Pending Approval/  ─▶  Approved/  ─▶  Done/
                                          ↘  Rejected/

Logs/     (append-only event log)
Errors/   (skill error reports)        [Gold+]
Briefings/ (CEO weekly reports)        [Gold+]
```

Every document uses **YAML frontmatter** for metadata:

```yaml
---
type: email_draft | linkedin_post_draft | payment | sales_lead | file_drop | generic
status: pending_approval | approved | rejected | sent | executed | pending
priority: high | medium | low
created: "YYYY-MM-DD HH:MM:SS"
channel: email | linkedin | whatsapp | twitter | facebook | instagram
from: "sender@example.com"
---

# Document Title

Body content...
```

### HITL (Human-in-the-Loop) Pattern

The core safety mechanism across Silver, Gold, and Platinum:

```
1. Agent drafts an action
        ↓
2. Saves draft to  Pending Approval/
        ↓
3. YOU review the Markdown file
        ↓
   ┌──── Approve ────┐        ┌──── Reject ────┐
   ↓                 ↓        ↓                ↓
Moves to          System   Moves to         Archived
Approved/         executes  Rejected/
        ↓
All decisions logged to  Logs/hitl_YYYY-MM-DD.md
```

Nothing is sent, posted, or executed without passing through this gate (unless explicitly configured as auto-approved for low-risk actions in Gold tier).

### Watcher → Skill Pipeline

```
External source  (Gmail, LinkedIn, WhatsApp, Twitter, Facebook/Instagram)
        │
        ▼
  Watcher  monitors source on a timer, detects new items
        │
        ▼
  Needs Action/  ← saves .md file with YAML metadata
        │
        ▼
  Skill  reads files, applies Company Handbook rules
        │
        ├──▶  Plans/            (drafts)
        ├──▶  Pending Approval/ (high-value, payments > $500, posts)
        └──▶  Done/             (low-risk auto-approved items)
```

---

## Tier 1: Bronze — Minimum Viable MVP

**Goal:** Get something working. Local file-watching, zero external APIs.

```
01_Bronze-Tier-Foundation-(.../
├── Company Handbook.md          # Business rules reference
├── watchers/
│   └── filesystem_watcher.py   # Core: monitors Inbox/
└── Skills/
    ├── Skill1_BasicFileHandler.md
    └── Skill2_TaskAnalyzer.md
```

### What it does

- **`filesystem_watcher.py`** monitors the `Inbox/` folder using `watchdog`
- When a file is dropped in `Inbox/`, the watcher automatically:
  - Reads the file
  - Generates a YAML metadata header
  - Prefixes it with `FILE_`
  - Moves it to `Needs Action/`
- Check interval: **5 seconds**

### Run it

```bash
cd "01_Bronze-Tier-Foundation-(...)"
pip install watchdog
python watchers/filesystem_watcher.py

# Drop any file into Inbox/ — watch it appear in Needs Action/
```

---

## Tier 2: Silver — Multi-Channel Assistant

**Goal:** Monitor real business channels. Always ask before acting (HITL).

```
02_Silver-Tier-Functional-Assistant/
├── mcp.json                              # MCP server config
├── mcp_servers/
│   └── email-mcp/
│       └── index.js                     # Gmail draft/send via MCP
├── watchers/
│   ├── gmail_watcher.py
│   ├── linkedin_watcher.py
│   └── whatsapp_watcher.py
└── Skills/
    ├── auto_linkedin_poster.py
    └── hitl_approval_handler.py
```

### Watchers

| Watcher | Source | Interval | Keywords Tracked |
|---------|--------|----------|-----------------|
| `gmail_watcher.py` | Gmail API (OAuth2) | 120s | urgent, invoice, payment, sales |
| `linkedin_watcher.py` | LinkedIn Web (Playwright) | 60s | sales, client, project |
| `whatsapp_watcher.py` | WhatsApp Web (Playwright) | 30s | all messages |

Each watcher saves to `Needs Action/` with structured YAML frontmatter. Browser sessions (LinkedIn, WhatsApp) persist in `session/` — first run opens a visible browser for login; all subsequent runs are headless.

### Skills

| Skill | What it does |
|-------|-------------|
| `auto_linkedin_poster.py` | Reads business leads from `Needs Action/`, drafts polished LinkedIn posts using Company Handbook templates, moves to `Pending Approval/` |
| `hitl_approval_handler.py` | Monitors `Pending Approval/` and `Approved/`, processes human decisions, moves files to `Done/` or `Rejected/`, logs everything |

### Email MCP Server

`mcp_servers/email-mcp/index.js` exposes Gmail as MCP tools to Claude Code:

| Tool | What it does |
|------|-------------|
| `draft_email` | Saves a draft to `Plans/` + `Pending Approval/` |
| `send_email` | Calls Gmail API — **only works if draft is in `Approved/`** |
| `approve_draft` | Moves `Pending Approval/` → `Approved/` |
| `reject_draft` | Moves `Pending Approval/` → `Rejected/` |
| `list_email_drafts` | Lists all drafts with status |

### Run it

```bash
cd "02_Silver-Tier-Functional-Assistant"

# One-time Gmail OAuth setup
cd mcp_servers/email-mcp && npm install && node index.js --auth

# Start the MCP server
node mcp_servers/email-mcp/index.js

# Run watchers (each in its own terminal)
python watchers/gmail_watcher.py
python watchers/linkedin_watcher.py
python watchers/whatsapp_watcher.py

# Run skills
python Skills/auto_linkedin_poster.py
python Skills/hitl_approval_handler.py --monitor   # continuous

# Manual approval commands
python Skills/hitl_approval_handler.py --approve filename
python Skills/hitl_approval_handler.py --reject filename
```

---

## Tier 3: Gold — Autonomous Employee

**Goal:** The AI Employee acts without constant oversight. It watches 6 channels, classifies everything, and only asks for human review on high-stakes decisions.

```
03_Gold-Tier-Autonomous-Employee/
├── mcp.json                                    # 3 MCP servers
├── docs/
│   ├── architecture.md
│   └── lessons_learned.md
├── mcp_servers/
│   ├── email-mcp/
│   ├── calendar-mcp/                           # Google Calendar
│   └── slack-mcp/                              # Slack messaging
├── watchers/
│   ├── gmail_watcher.py
│   ├── linkedin_watcher.py
│   ├── whatsapp_watcher.py
│   ├── filesystem_watcher.py
│   ├── twitter_watcher.py                      # NEW
│   └── facebook_instagram_watcher.py           # NEW (shared FB + IG session)
├── Skills/
│   ├── error_recovery.py                       # NEW: retry + error logging
│   ├── audit_logger.py                         # NEW: JSON Lines audit trail
│   ├── auto_linkedin_poster.py
│   ├── hitl_approval_handler.py
│   ├── cross_domain_integrator.py              # NEW: Skill 5
│   ├── social_summary_generator.py             # NEW: Skill 6
│   ├── twitter_post_generator.py               # NEW: Skill 7
│   └── weekly_audit_briefer.py                 # NEW: Skill 8
└── tools/
    └── ralph_loop_runner.py                    # NEW: autonomous task loop
```

### New Watchers

| Watcher | Source | Interval | Notes |
|---------|--------|----------|-------|
| `twitter_watcher.py` | Twitter/X Web | 60s | DMs, notifications, home feed; `data-testid` selectors; automation detection mitigation |
| `facebook_instagram_watcher.py` | Facebook + Instagram | 60s | Shared Chromium session; monitors Messenger, notifications, IG DMs, IG activity feed; max 25 items/scan |

### New Skills

| Skill | File | What it does |
|-------|------|-------------|
| **Skill 5** | `cross_domain_integrator.py` | Unifies all channels; routes personal items (Gmail, WhatsApp) → HITL; routes business items (LinkedIn, Twitter, Facebook) → LinkedIn post draft |
| **Skill 6** | `social_summary_generator.py` | Processes Facebook/Instagram leads; scores lead quality; drafts reply + outbound post; sends to `Pending Approval/` |
| **Skill 7** | `twitter_post_generator.py` | Converts Twitter DMs/mentions into 280-char tweet drafts + DM replies for high/medium leads |
| **Skill 8** | `weekly_audit_briefer.py` | Runs weekly; audits all activity; detects bottlenecks (stale files > 48h); writes CEO briefing to `Briefings/` |

### New MCP Servers

| Server | Tools | Auth |
|--------|-------|------|
| `calendar-mcp` | `draft_calendar_event`, `create_calendar_event`, `update_calendar_event`, `approve/reject_calendar_draft`, `list_calendar_drafts` | Google OAuth2 |
| `slack-mcp` | `draft_slack_message`, `send_slack_message`, `read_slack_channel`, `approve/reject_slack_draft`, `list_slack_drafts` | Slack Bot Token |

### Run it

```bash
cd "03_Gold-Tier-Autonomous-Employee"

# Setup MCP servers
cd mcp_servers/email-mcp && npm install && node index.js --auth
cd ../calendar-mcp && npm install && node index.js --auth
cd ../slack-mcp && npm install      # set SLACK_TOKEN in .env

# Start watchers via PM2 (recommended)
pm2 start watchers/gmail_watcher.py              --interpreter python --name gmail
pm2 start watchers/linkedin_watcher.py           --interpreter python --name linkedin
pm2 start watchers/twitter_watcher.py            --interpreter python --name twitter
pm2 start watchers/facebook_instagram_watcher.py --interpreter python --name fb-ig
pm2 start watchers/whatsapp_watcher.py           --interpreter python --name whatsapp

# Run the autonomous loop
python tools/ralph_loop_runner.py --max-iterations 20

# Test with a sample lead
python tools/ralph_loop_runner.py --create-test-lead

# Weekly CEO briefing
python Skills/weekly_audit_briefer.py

# Start daily scheduler
./schedulers/daily_scheduler.sh
```

---

## Tier 4: Platinum — Always-On Cloud Executive

**Goal:** Production deployment. The AI Employee never sleeps.

Platinum mirrors the Gold Tier structure and adds:

- **Cloud hosting** — containerized watchers + skills running 24/7
- **Multi-instance orchestration** — horizontal scaling for high-volume channels
- **Persistent sessions** — browser sessions managed via cloud storage
- **Dashboard** — real-time view of the pipeline state
- **Alerting** — push notifications on high-priority items

> **Status:** Scaffold in place. Cloud deployment configuration in progress.

---

## Quick Start

### Minimum (Bronze — no API keys needed)

```bash
git clone <this-repo>
cd "01_Bronze-Tier-Foundation-(...)"
pip install watchdog
python watchers/filesystem_watcher.py
# Drop a .txt or .md file into Inbox/ and watch it move
```

### With Gmail + LinkedIn (Silver)

```bash
# 1. Get Google OAuth2 credentials from Google Cloud Console
#    → Download as credentials.json

# 2. Place credentials.json in the Silver tier root
cp credentials.json "02_Silver-Tier-Functional-Assistant/"

# 3. Authenticate
cd "02_Silver-Tier-Functional-Assistant/mcp_servers/email-mcp"
npm install
node index.js --auth   # opens browser, completes OAuth flow

# 4. Start everything
python watchers/gmail_watcher.py &
python watchers/linkedin_watcher.py &
python Skills/hitl_approval_handler.py --monitor
```

### Fully Autonomous (Gold)

```bash
# Set your API keys
export ANTHROPIC_API_KEY="sk-ant-..."
export SLACK_TOKEN="xoxb-..."

# Run the autonomous loop
cd "03_Gold-Tier-Autonomous-Employee"
python tools/ralph_loop_runner.py --max-iterations 20 --dry-run   # preview
python tools/ralph_loop_runner.py --max-iterations 20             # live
```

---

## Project Structure

```
HackathonAI_Employee_Vault/
├── README.md
├── CLAUDE.md                                          # Dev guidance
│
├── 01_Bronze-Tier-Foundation-(Minimum-Viable-Deliverable)/
│   ├── Company Handbook.md
│   ├── watchers/filesystem_watcher.py
│   ├── Skills/Skill1_BasicFileHandler.md
│   ├── Skills/Skill2_TaskAnalyzer.md
│   └── [Inbox/ Needs Action/ Plans/ ...]
│
├── 02_Silver-Tier-Functional-Assistant/
│   ├── mcp.json
│   ├── mcp_servers/email-mcp/index.js
│   ├── watchers/{gmail,linkedin,whatsapp,filesystem}_watcher.py
│   ├── Skills/{auto_linkedin_poster,hitl_approval_handler}.py
│   ├── schedulers/{daily_scheduler.sh,.ps1}
│   └── [session/ Inbox/ Needs Action/ Plans/ ...]
│
├── 03_Gold-Tier-Autonomous-Employee/
│   ├── mcp.json                                       # 3 MCP servers
│   ├── docs/{architecture.md, lessons_learned.md}
│   ├── mcp_servers/{email-mcp,calendar-mcp,slack-mcp}/
│   ├── watchers/{gmail,linkedin,whatsapp,filesystem,twitter,facebook_instagram}_watcher.py
│   ├── Skills/{error_recovery,audit_logger,auto_linkedin_poster,hitl_approval_handler,
│   │          cross_domain_integrator,social_summary_generator,
│   │          twitter_post_generator,weekly_audit_briefer}.py
│   ├── tools/ralph_loop_runner.py
│   ├── schedulers/{daily_scheduler.sh,.ps1}
│   └── [session/ Briefings/ Errors/ Inbox/ Needs Action/ Plans/ ...]
│
└── 04_Platinum-Tier-Always-On-Cloud+LocalExecutive-(Production-ish-AI-Employee)/
    └── [mirrors Gold Tier + cloud config]
```

---

## Dependencies

### Python

```bash
# Bronze+
pip install watchdog

# Silver+ (Gmail API)
pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client

# Silver+ (browser automation)
pip install playwright
playwright install chromium

# Gold+ (Claude API — for ralph_loop_runner)
pip install anthropic
```

### Node.js (MCP Servers)

```bash
# In each mcp_servers/*/  folder:
npm install
# Installs: @modelcontextprotocol/sdk  googleapis  google-auth-library
```

### System

- Python 3.9+
- Node.js 18+
- PM2 (optional, recommended for long-running watchers): `npm install -g pm2`

---

## Configuration & Credentials

| File | Purpose | Where to put it |
|------|---------|-----------------|
| `credentials.json` | Google OAuth2 client secrets | Each tier's root, or `mcp_servers/email-mcp/` |
| `token.json` | Gmail/Calendar OAuth token (auto-generated) | Same folder as `credentials.json` |
| `ANTHROPIC_API_KEY` | Claude API key | Environment variable |
| `SLACK_TOKEN` | Slack Bot Token (`xoxb-...`) | `.env` in `mcp_servers/slack-mcp/` |

**None of these are committed to git.** Check `.gitignore`.

### Google OAuth2 Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → Enable Gmail API + Google Calendar API
3. Create OAuth2 credentials → Download as `credentials.json`
4. Run `node index.js --auth` in the MCP server folder
5. Complete the browser flow → `token.json` is created automatically

---

## How the HITL Gate Works

The HITL gate is enforced at **multiple layers**:

**1. File system layer** — `send_email` in the MCP server checks if the draft file exists in `Approved/` before calling Gmail API. If it's still in `Pending Approval/`, the send is blocked.

**2. Skill layer** — all skills route high-value items (payments > $500, outbound social posts, email drafts) to `Pending Approval/` and stop. Nothing happens until a human moves the file.

**3. Ralph Loop layer** — the autonomous loop routes by type:

| Item Type | Amount | Route |
|-----------|--------|-------|
| `payment` | > $500 | `Pending Approval/` (HITL) |
| `payment` | ≤ $500 | `Done/` (auto-approved) |
| `sales_lead` | any | Cross-domain integrator → `Pending Approval/` |
| `email_draft` | any | `Pending Approval/` (HITL) |
| `linkedin_post_draft` | any | `Pending Approval/` (HITL) |
| `file_drop` | any | `Done/` (auto-approved) |

---

## Ralph Loop Runner (Gold)

`tools/ralph_loop_runner.py` is the autonomous brain of the Gold tier. It runs a **rule-based pipeline** (no LLM required for basic operation) with up to 20 iterations.

### 6-Step Pipeline

```
STEP 1: DISCOVER   — scan Needs Action/ for unprocessed .md files
STEP 2: CLASSIFY   — parse YAML + keyword scan + dollar-amount rules
STEP 3: PLAN       — write Plans/Plan_[ts].md with checkbox steps
STEP 4: EXECUTE    — run cross_domain_integrator for business leads
STEP 5: ROUTE      — move to Done/ or Pending Approval/ (HITL gate)
STEP 6: COMPLETE   — emit TASK_COMPLETE, write loop log to Logs/
```

### CLI Options

```bash
python tools/ralph_loop_runner.py                          # default (20 iterations)
python tools/ralph_loop_runner.py --max-iterations 5       # custom limit
python tools/ralph_loop_runner.py --dry-run                # preview only, no writes
python tools/ralph_loop_runner.py --create-test-lead       # inject a realistic $8,500 sales lead
```

### Available Tools (internal)

| Tool | Description |
|------|-------------|
| `tool_list_files(directory)` | List .md files in a folder |
| `tool_read_file(path)` | Read file content + YAML frontmatter |
| `tool_write_file(path, content)` | Write a new file |
| `tool_append_file(path, content)` | Append to a log file |
| `tool_move_file(src, dest_folder)` | Move file to another stage |
| `tool_run_skill(skill_name, args)` | Run a skill as subprocess |

---

## MCP Servers

[Model Context Protocol](https://modelcontextprotocol.io/) servers expose business capabilities as tools that Claude Code can call directly.

### email-mcp (Silver+)

```jsonc
// mcp.json
{
  "mcpServers": {
    "email": {
      "command": "node",
      "args": ["mcp_servers/email-mcp/index.js"]
    }
  }
}
```

Tools: `draft_email`, `send_email` (HITL-gated), `approve_draft`, `reject_draft`, `list_email_drafts`

### calendar-mcp (Gold+)

Tools: `draft_calendar_event`, `create_calendar_event`, `update_calendar_event`, `approve_calendar_draft`, `reject_calendar_draft`, `list_calendar_drafts`

### slack-mcp (Gold+)

Tools: `draft_slack_message`, `send_slack_message`, `read_slack_channel`, `approve_slack_draft`, `reject_slack_draft`, `list_slack_drafts`

---

## Error Recovery (Gold)

All Gold tier watchers and skills import from `Skills/error_recovery.py`:

### `with_retry(fn, *args, **kwargs)`

Exponential backoff retry wrapper:
- Max retries: **3**
- Base delay: **1 second**
- Max delay: **60 seconds** (cap)
- Backoff: 1s → 2s → 4s → 8s...

```python
# Example usage in a watcher
result = with_retry(page.goto, "https://linkedin.com/messaging", label="linkedin_goto_dms")
```

### Error Logging

| Function | Output file | When used |
|----------|------------|-----------|
| `log_watcher_error(name, exc)` | `Logs/error_{name}_{date}.log` | In watchers (network, browser errors) |
| `log_skill_error(name, exc)` | `Errors/skill_error_{date}.md` | In skills (API failures) |
| `write_manual_action_plan(name, desc)` | `Plans/manual_{name}_{date}.md` | When external API is down — creates a human-readable fallback task |

The system **never crashes** on external failures. If a watcher can't reach LinkedIn, it logs and waits for the next cycle.

---

## Audit Trail (Gold)

`Skills/audit_logger.py` writes an append-only JSON Lines audit log:

**File:** `Logs/audit_YYYY-MM-DD.json` — one JSON object per line.

```json
{"timestamp": "2026-02-25T14:32:01", "action_type": "item_drafted", "actor": "auto_linkedin_poster", "target": "Plans/linkedin_draft_2026-02-25.md", "approval_status": "pending", "result": "success"}
```

### Tracked Action Types

`skill_start` · `skill_complete` · `skill_error` · `item_drafted` · `item_routed` · `item_queued` · `hitl_queued` · `hitl_executed` · `hitl_rejected` · `briefing_written` · `log_purged`

### Log Lifecycle

- **Retention:** 90 days
- **Auto-purge:** `weekly_audit_briefer.py` calls `purge_old_logs(90)` every Sunday
- **Weekly summary:** `get_weekly_summary(days=7)` → total actions, by type, by actor, error count
- **CEO briefing integration:** `build_markdown_section(summary)` embeds the summary in `Briefings/ceo_briefing_[date].md`

---

## Company Handbook Pattern

Each tier reads its own `Company Handbook.md` **at skill startup** (not hardcoded). This means you can update business rules without touching code.

Example rules in the handbook:
```markdown
- Always be polite and professional in all communications
- Flag any payment or invoice over $500 for human approval
- Do not post on social media between 11pm and 7am
- Respond to sales inquiries within 24 hours
```

Skills load the handbook at runtime and apply rules before routing any item. If the file is missing, sensible defaults are used.

---

## Contributing

This is a hackathon project exploring progressive autonomy in AI agents. Contributions, forks, and experiments are welcome.

### Areas to explore

- **Platinum Tier** — cloud deployment (Docker, Railway, Fly.io)
- **New watchers** — Telegram, Slack inbound, calendar events
- **LLM classification** — replace keyword matching with Claude API calls
- **Dashboard** — real-time pipeline visualization
- **Tests** — unit tests for error_recovery.py, audit_logger.py

### Code style

- Python: standard library preferred, minimal dependencies
- Error recovery: always use `with_retry()` for external I/O in watchers
- Audit: call `log_action()` at skill_start, skill_complete, skill_error
- New skills: import both `error_recovery` and `audit_logger`

---

## License

MIT — use freely, build on top, ship something.

---

*Built during a hackathon to prove that AI agents can be both powerful and transparent. The file system is the interface.*
