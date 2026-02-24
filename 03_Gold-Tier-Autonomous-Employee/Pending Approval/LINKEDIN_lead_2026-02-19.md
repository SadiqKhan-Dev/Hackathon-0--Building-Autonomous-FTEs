---
type: linkedin_post_draft
source_file: LINKEDIN_lead_2026-02-19.md
created: 2026-02-19
status: pending_approval
approval_required: true
approval_reason: Payment $750 exceeds $500 handbook threshold; LinkedIn post requires human review
skill: Skill3_AutoLinkedInPoster
hitl_handler: hitl_approval_handler.py
email_draft_id: email_draft_sarah_chen_2026-02-19
---

# PENDING APPROVAL — LinkedIn Lead Response
## Sarah Chen, TechCorp Partnership ($750)

> **ACTION REQUIRED:** Review and approve before any response or post is published.

---

## Original Lead Summary

- **From:** Sarah Chen, Head of Partnerships @ TechCorp
- **Platform:** LinkedIn (detected by linkedin_watcher.py)
- **Message:** Partnership inquiry for AI automation client project
- **Budget Mentioned:** $750 — exceeds $500 HITL threshold
- **Keywords:** sales, project, client

## Proposed LinkedIn Post

> Excited about a new strategic collaboration opportunity coming together!
> Working with innovative partners to deliver impactful AI automation solutions
> for their upcoming client project. If you're exploring how AI can scale your
> business outcomes — let's connect. #AIAutomation #StrategicPartnerships #Sales

## Proposed Email Reply (via email-mcp draft_email)

**To:** sarah.chen@techcorp.com
**Subject:** Re: Strategic Partnership Opportunity

> Hi Sarah,
>
> Thank you for reaching out! I'd be delighted to discuss how we can collaborate
> on your upcoming client project. AI automation partnerships are exactly where
> I focus my energy. Let's schedule a call to explore the fit.
>
> Looking forward to connecting!

## Approval Instructions

**To approve this action:**
```bash
# Option 1 — Move file to /Approved folder manually
# Option 2 — Use HITL handler CLI:
python Skills/hitl_approval_handler.py --approve LINKEDIN_lead_2026-02-19.md
```

**To reject this action:**
```bash
python Skills/hitl_approval_handler.py --reject LINKEDIN_lead_2026-02-19.md --reason "Not interested"
```

**Monitor mode (continuous):**
```bash
python Skills/hitl_approval_handler.py --monitor
```

## On Approval

- [ ] LinkedIn post logged as approved and ready-to-publish
- [ ] Email draft sent via Gmail API (email-mcp `send_email` tool)
- [ ] File moved to /Approved → then /Done
- [ ] Log entry written to /Logs/hitl_[date].md

## On Rejection

- [ ] File moved to /Rejected
- [ ] No post published, no email sent
- [ ] Rejection logged to /Logs/hitl_[date].md
