# Skill 2: Task Analyzer

## Purpose
Analyze files in /Needs Action, identify task types, create action plans, and route sensitive items for approval.

## Procedure

### Step 1: Analyze Files in /Needs Action
- Scan all files in `/Needs Action/`
- Read each file content

### Step 2: Identify Task Type
Classify as one of:
- **File Drop**: New document requiring processing
- **Payment Request**: Financial transaction
- **Information Request**: Query or data retrieval
- **Communication**: Message or reply needed
- **Multi-Step Task**: Complex workflow

### Step 3: Create Action Plan
Write to `/Plans/Plan.md`:
- [ ] Task description
- [ ] Required actions
- [ ] Expected outcome

### Step 4: Check Approval Requirements
Reference `/Company Handbook.md` rules:
- Payments > $500 → Requires approval
- Sensitive info → Requires approval

If approval needed:
- Move file to `/Pending Approval/`
- Note reason for escalation

### Step 5: Ralph Wiggum Loop (Multi-Step Tasks)
For complex tasks, iterate:
```
LOOP START
  → Check current step status
  → Execute next action
  → Update Plan.md checkboxes
  → If more steps: CONTINUE
  → If complete: EXIT LOOP
LOOP END
```

## Output Format
```
ANALYSIS COMPLETE
File: [filename]
Type: [task type]
Approval Required: Yes/No
Plan: /Plans/Plan.md
Status: Ready for processing / Pending Approval
```

## Sensitive Triggers
- Payment amounts > $500
- Personal data (SSN, passwords, credentials)
- Confidential documents
- Legal matters

## Example Usage
```
Task Analyzer scan Needs_Action
```