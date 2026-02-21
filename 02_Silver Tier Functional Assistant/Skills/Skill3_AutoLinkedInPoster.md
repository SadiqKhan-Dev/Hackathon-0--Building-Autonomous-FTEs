# Skill 3: Auto LinkedIn Poster

## Purpose
Scan `/Needs Action/` for sales and business lead messages, draft a polished LinkedIn
post based on the lead context, save the draft to `/Plans/`, then route it to
`/Pending Approval/` for Human-in-the-Loop (HITL) review before any post goes live.

## Handbook Rules (from `/Company Handbook.md`)
Before drafting any content:
- **Always be polite** — no aggressive sales language, no pressure tactics.
- **Flag payments > $500** for manual approval before quoting in a post.
- Keep tone professional and benefit-focused.

---

## Procedure

### Step 1: Load Company Handbook
Read `/Company Handbook.md` and extract active rules.
These rules govern all drafted post language.

### Step 2: Scan /Needs Action for Lead Files
- List all `.md` files in `/Needs Action/`
- Filter files where content contains **any** of these keywords:
  - `sales`, `client`, `project`
- For each matched file, extract:
  - `from` (contact name / sender)
  - `subject` (topic or service area)
  - `keyword_matched` (the trigger word found)
  - `content preview` (first meaningful line of the message)

If no matching files are found, output:
```
NO LEADS: No matching files in /Needs Action. Nothing to post.
```
and stop.

### Step 3: Draft LinkedIn Post
Using the extracted lead context, generate a post following this template:

```
Excited to offer [service/expertise] for [specific benefit]!
If you are looking for [outcome], I would love to connect.
DM me to get started — always happy to help.

#[keyword] #Business #[relevant tag]
```

Tone rules (from Handbook):
- Professional and warm — never pushy or boastful.
- One specific benefit per post — no laundry lists.
- End with a soft call-to-action (DM / connect).
- Maximum 3 hashtags.

### Step 4: Save Draft to /Plans
Create file: `/Plans/linkedin_post_[YYYY-MM-DD].md`

File structure:
```yaml
---
type: linkedin_post_draft
source_lead: "[original lead filename]"
contact: "[sender name from lead]"
keyword_matched: "[trigger keyword]"
content: "[full drafted post text]"
status: pending_approval
created: "[YYYY-MM-DD HH:MM:SS]"
---

# LinkedIn Post Draft — [YYYY-MM-DD]

## Drafted Post

[drafted post text here]

## Source Lead

- File: /Needs Action/[lead filename]
- From: [sender]
- Keyword: [keyword_matched]

## Checklist

- [ ] Review tone against Company Handbook
- [ ] Confirm no payment amounts > $500 referenced
- [ ] Approve and move to /Approved
- [ ] Post manually on LinkedIn or route to LinkedIn Watcher
```

### Step 5: HITL — Move to /Pending Approval
- Copy the saved draft from `/Plans/linkedin_post_[date].md`
  to `/Pending Approval/linkedin_post_[date].md`
- Update the YAML `status` field to `pending_approval`
- Do **NOT** post to LinkedIn directly — wait for human approval.
- Log the action and output the approval path.

### Step 6: Output Result
```
SUCCESS: LinkedIn post draft created and queued for approval
Source Lead  : /Needs Action/[lead filename]
Draft Saved  : /Plans/linkedin_post_[date].md
Pending Path : /Pending Approval/linkedin_post_[date].md
Status       : Awaiting human approval (HITL)
Next Step    : Review /Pending Approval/ and move to /Approved to publish
```

---

## HITL Approval Flow

```
/Needs Action/[lead].md
        |
        v
[Skill 3 runs]
        |
        v
/Plans/linkedin_post_[date].md  (draft created)
        |
        v
/Pending Approval/linkedin_post_[date].md  <-- HUMAN REVIEWS HERE
        |
   +----+----+
   |         |
APPROVE    REJECT
   |         |
   v         v
/Approved/  /Rejected/
(publish)   (archive)
```

---

## Approval Statuses

| Status | Meaning |
|--------|---------|
| `pending_approval` | Draft waiting for human review |
| `approved` | Human approved — ready to post |
| `rejected` | Human rejected — archived in /Rejected |
| `posted` | Successfully published to LinkedIn |

---

## Error Handling

| Condition | Action |
|-----------|--------|
| No lead files found | Log `NO LEADS` and exit cleanly |
| Lead has no extractable service/topic | Use generic template with contact name |
| Company Handbook unavailable | Warn and apply default polite rules |
| `/Plans` or `/Pending Approval` folder missing | Create folder, then continue |

---

## Python Implementation

This skill has a runnable Python script:

```
python Skills/auto_linkedin_poster.py
```

Or with PM2:
```
pm2 start Skills/auto_linkedin_poster.py --interpreter python --name linkedin-poster
```

---

## Example Usage

**Agent invocation:**
```
@Auto LinkedIn Poster process sales lead
```

**With specific file:**
```
@Auto LinkedIn Poster process Needs_Action/LINKEDIN_20240219_John_Doe.md
```

**Expected output:**
```
SUCCESS: LinkedIn post draft created and queued for approval
Source Lead  : /Needs Action/LINKEDIN_20240219_John_Doe.md
Draft Saved  : /Plans/linkedin_post_2024-02-19.md
Pending Path : /Pending Approval/linkedin_post_2024-02-19.md
Status       : Awaiting human approval (HITL)
Next Step    : Review /Pending Approval/ and move to /Approved to publish
```
