# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**HackathonAI Employee Vault** is a progressive 4-tier system for building an "AI Employee" — starting from a local file-based assistant (Bronze) up to a cloud-deployed, always-on autonomous agent (Platinum). Each tier is a standalone, self-contained project that builds on the concepts of the previous one.

## Architecture

### The 4-Tier Model

| Tier | Purpose | Key Tech |
|------|---------|----------|
| **Bronze** | MVP: local file-watching + task organization | `watchdog`, Markdown, Obsidian |
| **Silver** | Multi-channel assistant with HITL approval gates | MCP, Gmail API, Playwright, Anthropic SDK |
| **Gold** | Autonomous employee — acts without constant human oversight | All Silver + autonomous execution |
| **Platinum** | Production cloud deployment, always-on | All Gold + cloud infra |

### File-Based State Machine (all tiers)

All tiers share the same folder-based state model. Files flow through:

```
Inbox/ → Needs Action/ → Plans/ → Pending Approval/ → Approved/ → Done/
                                                     ↘ Rejected/
Logs/   (append-only event log)
```

Every document uses YAML frontmatter for metadata:
```yaml
---
type: email_draft | linkedin_post_draft | payment | generic | file_drop
status: pending_approval | approved | rejected | sent | executed
priority: high | medium | low
created: "YYYY-MM-DD HH:MM:SS"
---
```

### HITL (Human-in-the-Loop) Pattern

The core safety mechanism across Silver/Gold/Platinum:
1. Agent drafts an action → saves to `Pending Approval/`
2. Human reviews the Markdown file
3. Human approves → moves to `Approved/` → system executes
4. Human rejects → moves to `Rejected/`
5. All decisions logged to `Logs/hitl_YYYY-MM-DD.md`

### Company Handbook

Each tier reads its own `Company Handbook.md` before executing skills. Skills check these rules at runtime (e.g., "flag payments > $500 for approval", "always be polite").

### Watcher → Skill Pattern (Silver+)

```
External source (Gmail, LinkedIn, WhatsApp)
  → Watcher monitors & saves to Needs Action/
  → Skill reads Needs Action/, applies Handbook rules
  → Routes output to Plans/, Pending Approval/, or Done/
```

### MCP Server (email-mcp)

The Email MCP server (`*/mcp_servers/email-mcp/index.js`) exposes Gmail capabilities as MCP tools to Claude Code. It enforces the HITL gate — `send_email` only works if the draft is already in `Approved/`.

Tools: `draft_email`, `approve_draft`, `reject_draft`, `send_email`, `list_email_drafts`

Config: `*/mcp.json` points Claude Code at the running MCP server.

---

## Running Each Tier

### Bronze Tier
```bash
cd "01_Bronze Tier Foundation (Minimum Viable Deliverable)"
pip install watchdog
python watchers/filesystem_watcher.py
```

### Silver Tier

**One-time OAuth setup (Gmail):**
```bash
cd "02_Silver Tier Functional Assistant/mcp_servers/email-mcp"
npm install
node index.js --auth
```

**Run the Email MCP server:**
```bash
node "02_Silver Tier Functional Assistant/mcp_servers/email-mcp/index.js"
```

**Run watchers (each in its own terminal or via PM2):**
```bash
cd "02_Silver Tier Functional Assistant"
python watchers/gmail_watcher.py
python watchers/linkedin_watcher.py
python watchers/whatsapp_watcher.py
```

**Run skills:**
```bash
python Skills/auto_linkedin_poster.py
python Skills/hitl_approval_handler.py --monitor   # continuous watch
python Skills/hitl_approval_handler.py --check     # one-shot report
```

**Run the agentic Claude loop:**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
python tools/ralph_loop_runner.py
python tools/ralph_loop_runner.py --max-iterations 10
python tools/ralph_loop_runner.py --dry-run
```

**HITL manual commands (for testing):**
```bash
python Skills/hitl_approval_handler.py --write --type email_draft --details "Send to client" --draft-file "Plans/email_draft.md"
python Skills/hitl_approval_handler.py --approve filename
python Skills/hitl_approval_handler.py --reject filename
```

### Gold Tier
Same setup as Silver. The Gold tier has the same structure but with autonomous execution enabled (no manual approval step for certain actions).

---

## Required Credentials & Environment

- **`ANTHROPIC_API_KEY`** — required for `ralph_loop_runner.py` and any Claude API calls
- **`credentials.json`** — Google OAuth2 client secrets; place in each tier's root or the `mcp_servers/email-mcp/` folder. Run `node index.js --auth` once to generate `token.json`.
- Both files are gitignored. Never commit them.

## Python Dependencies (no requirements.txt — install manually)

```
watchdog              # Bronze+ file monitoring
google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client  # Gmail API
playwright            # LinkedIn/WhatsApp browser automation
anthropic             # Claude API (ralph_loop_runner)
```

For Playwright first run:
```bash
pip install playwright
playwright install chromium
```

## Node.js Dependencies (email-mcp)

```bash
cd mcp_servers/email-mcp
npm install   # installs @modelcontextprotocol/sdk, googleapis, google-auth-library
```

## LinkedIn / WhatsApp Watcher Sessions

Browser sessions persist in `session/linkedin/` and `session/whatsapp/`. On first run the watcher will open a visible browser for login; subsequent runs are headless using the saved session.

## PM2 (recommended for long-running watchers)

```bash
pm2 start watchers/gmail_watcher.py --interpreter python --name gmail-watcher
pm2 start watchers/linkedin_watcher.py --interpreter python --name linkedin-watcher
pm2 start Skills/hitl_approval_handler.py --interpreter python -- --monitor
```
