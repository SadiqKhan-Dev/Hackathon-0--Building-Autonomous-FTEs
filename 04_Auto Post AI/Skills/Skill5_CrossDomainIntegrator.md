# Skill 5: Cross Domain Integrator

## Purpose
Unify personal (Gmail, WhatsApp) and business (LinkedIn, Twitter, Facebook) channels
in a single automated processing flow. Reads all files from `/Needs Action/`, classifies
each item as **personal** or **business**, routes it to the correct downstream skill,
and produces a unified cross-domain summary log in `/Logs/`.

This is a **Gold Tier autonomous skill** — it acts without constant human oversight for
business items, while keeping HITL gates for personal communications.

---

## Channel Map

| Domain   | Sources                             | Detection Method                        | Routed To                     |
|----------|-------------------------------------|-----------------------------------------|-------------------------------|
| Personal | Gmail (email), WhatsApp (message)   | File prefix `EMAIL_` / `WHATSAPP_` or personal keywords | HITL Approval Handler         |
| Business | LinkedIn, Twitter, Facebook         | File prefix `LINKEDIN_` / `TWITTER_` / `FACEBOOK_` or business keywords | Auto LinkedIn Poster (Skill 3)|

---

## Handbook Rules (from `/Company Handbook.md`)
- **Always be polite** — all drafted content checked before routing.
- **Payments > $500** — always escalate to HITL regardless of domain classification.
- Personal data (name, phone, address in a personal message) — always routes to HITL.

---

## Procedure

### Step 1: Load Company Handbook
Read `/Company Handbook.md` and cache active rules.
Apply payment threshold and politeness rules throughout.

### Step 2: Scan `/Needs Action/`
- List all `.md` files in `/Needs Action/`
- Skip files that are already metadata/system files (e.g. `*_metadata.md`, `FILE_*.md`)
- For each file, read its content and YAML frontmatter

If no files are found, output:
```
NO ITEMS: /Needs Action is empty. Nothing to integrate.
```
and stop.

### Step 3: Classify Each Item

**Priority order:**

1. **File prefix** (most reliable — set by watchers):
   - `EMAIL_*` or `WHATSAPP_*` → **personal**
   - `LINKEDIN_*` or `TWITTER_*` or `FACEBOOK_*` → **business**

2. **YAML `type` field** (set by watchers):
   - `type: email` / `type: whatsapp_message` → **personal**
   - `type: linkedin_lead` / `type: twitter_dm` / `type: facebook_message` → **business**

3. **Keyword scoring** (fallback):
   - Personal keywords: `personal, family, friend, home, vacation, appointment, health`
   - Business keywords: `sales, client, project, lead, proposal, partnership, revenue, contract`
   - Higher score wins; tie → default to **personal** (safer)

Output per file:
```
[CLASSIFY] LINKEDIN_lead_2026-02-19.md → BUSINESS  (prefix match)
[CLASSIFY] EMAIL_from_alice_2026-02-19.md → PERSONAL  (prefix match)
[CLASSIFY] message_2026-02-19.md → PERSONAL  (keyword: friend=1 vs sales=0)
```

### Step 4: Route — Personal Items → HITL

For each **personal** item:
1. Read the file to extract `from`, `subject`, `content preview`
2. Write a pending approval request to `/Pending Approval/` with:
   - `type: generic`
   - `action: review`
   - `source_skill: Skill5_CrossDomainIntegrator`
   - `priority: high` if the message is from a known contact or contains urgency keywords
3. Log: `PERSONAL → HITL | /Pending Approval/[filename]`

Format of pending approval file:
```yaml
---
type: generic
action: review
source_skill: Skill5_CrossDomainIntegrator
domain: personal
channel: email | whatsapp
details: "[summary of message]"
target: "[sender/contact]"
priority: high | medium | low
status: pending_approval
created: "YYYY-MM-DD HH:MM:SS"
draft_file: "Needs Action/[original filename]"
---
```

### Step 5: Route — Business Items → Auto LinkedIn Poster

For each **business** item:
1. Extract `from`, `subject`, `keyword_matched` from file
2. Scan for business keywords: `sales`, `client`, `project`, `lead`, `proposal`
3. Draft a LinkedIn post using the same template as Skill 3
4. Save draft to `/Plans/linkedin_post_[date]_[slug].md`
5. Copy to `/Pending Approval/` with `status: pending_approval`
6. In Gold Tier autonomous mode: execute immediately if no payment flag is present
7. Log: `BUSINESS → LINKEDIN POSTER | /Plans/linkedin_post_[date].md`

### Step 6: Write Unified Cross-Domain Summary Log

After all items are processed, write to:
`/Logs/cross_domain_[YYYY-MM-DD].md`

```markdown
---
type: cross_domain_log
date: "YYYY-MM-DD"
personal_count: N
business_count: N
---

# Cross Domain Integrator — YYYY-MM-DD HH:MM:SS

## Summary

| Domain   | Items | Routed To               |
|----------|-------|-------------------------|
| Personal | N     | HITL /Pending Approval  |
| Business | N     | Auto LinkedIn Poster    |
| Total    | N     |                         |

## Personal Items (→ HITL)

| File | Channel | From | Priority | Pending Path |
|------|---------|------|----------|--------------|
| ...  | email   | ...  | medium   | ...          |

## Business Items (→ Auto LinkedIn Poster)

| File | Channel | Keyword | Draft Path | Pending Path |
|------|---------|---------|------------|--------------|
| ...  | linkedin | sales  | ...        | ...          |

## Unclassified Items

| File | Reason | Action Taken |
|------|--------|--------------|
| ...  | No keywords matched | Routed to HITL as personal |
```

### Step 7: Output Success

```
CROSS DOMAIN INTEGRATOR COMPLETE
================================
Personal Items : N → /Pending Approval/ (HITL)
Business Items : N → /Plans/ + /Pending Approval/ (Auto LinkedIn Poster)
Total Processed: N
Log Path       : /Logs/cross_domain_YYYY-MM-DD.md
================================
Next Steps:
  Personal : Review /Pending Approval/ and move to /Approved or /Rejected
  Business : HITL Handler will auto-execute approved LinkedIn drafts
```

---

## State Machine

```
/Needs Action/[file].md
        |
        v
[Skill 5: Cross Domain Integrator]
        |
   CLASSIFY ITEM
        |
   +----+----+
   |         |
PERSONAL  BUSINESS
   |         |
   v         v
/Pending  [Draft LinkedIn Post]
Approval/      |
(HITL)    /Plans/linkedin_post_[date].md
               |
               v
          /Pending Approval/
          (Gold: auto-executes)
               |
          +----+----+
          |         |
       APPROVE    REJECT
          |         |
          v         v
       /Done/    /Rejected/

Both paths → /Logs/cross_domain_[date].md
```

---

## Classification Reference

### Personal Signals
| Signal | Example |
|--------|---------|
| File prefix | `EMAIL_*`, `WHATSAPP_*` |
| YAML type | `email`, `whatsapp_message` |
| Keywords | `personal`, `family`, `friend`, `home`, `appointment`, `health`, `vacation` |
| Channel | Gmail, WhatsApp |

### Business Signals
| Signal | Example |
|--------|---------|
| File prefix | `LINKEDIN_*`, `TWITTER_*`, `FACEBOOK_*` |
| YAML type | `linkedin_lead`, `twitter_dm`, `facebook_message` |
| Keywords | `sales`, `client`, `project`, `lead`, `proposal`, `partnership`, `revenue`, `contract` |
| Channel | LinkedIn, Twitter, Facebook |

---

## Error Handling

| Condition | Action |
|-----------|--------|
| `/Needs Action` missing | Log error and exit cleanly |
| File read error | Skip file, log warning, continue |
| Unclassifiable item | Default to **personal** (HITL) — safer |
| Handbook unavailable | Warn, apply default rules |
| Payment > $500 in business item | Escalate to HITL regardless of domain |
| `/Plans` or `/Pending Approval` missing | Create folder, continue |
| Duplicate log file for today | Append `_N` suffix |

---

## Python Implementation

```bash
# Process all files in /Needs Action (default mode)
python Skills/cross_domain_integrator.py

# Dry run — classify and report only, no files written
python Skills/cross_domain_integrator.py --dry-run

# Process a single specific file
python Skills/cross_domain_integrator.py --file "Needs Action/LINKEDIN_lead_2026-02-19.md"

# With PM2 (run once on schedule)
pm2 start Skills/cross_domain_integrator.py --interpreter python --name cross-domain
```

---

## Integration Points

```
Watchers (Gmail, WhatsApp, LinkedIn, Twitter, Facebook)
  └─► write files to /Needs Action/

Skill 5: Cross Domain Integrator  ←── YOU ARE HERE
  ├─► Personal items ─► /Pending Approval/ ─► Skill 4 (HITL Handler) executes
  └─► Business items ─► /Plans/ ─► /Pending Approval/ ─► Skill 3 (LinkedIn Poster) logic

Skill 4: HITL Approval Handler
  └─► picks up all /Pending Approval/ files and executes on human approval

Skill 3: Auto LinkedIn Poster
  └─► logic re-used internally by Skill 5 for business drafting
```

---

## Skill Dependencies

| Dependency | Used For |
|------------|----------|
| Skill 2 (Task Analyzer) | Classification taxonomy reference |
| Skill 3 (Auto LinkedIn Poster) | Business item drafting logic |
| Skill 4 (HITL Approval Handler) | Personal item gating + execution |
| Company Handbook | Politeness rules, payment threshold |

---

## Example Usage

**Agent invocation:**
```
@Cross Domain Integrator process Needs_Action
```

**Dry run (no files written):**
```
@Cross Domain Integrator dry-run Needs_Action
```

**Single file:**
```
@Cross Domain Integrator process Needs_Action/LINKEDIN_lead_2026-02-19.md
```

**Expected output:**
```
CROSS DOMAIN INTEGRATOR COMPLETE
================================
Personal Items : 2 → /Pending Approval/ (HITL)
Business Items : 1 → /Plans/ + /Pending Approval/ (Auto LinkedIn Poster)
Total Processed: 3
Log Path       : /Logs/cross_domain_2026-02-22.md
================================
Next Steps:
  Personal : Review /Pending Approval/ and move to /Approved or /Rejected
  Business : HITL Handler will auto-execute approved LinkedIn drafts
```
