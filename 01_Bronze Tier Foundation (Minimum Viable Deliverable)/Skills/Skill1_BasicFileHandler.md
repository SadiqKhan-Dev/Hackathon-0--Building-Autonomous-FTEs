# Skill 1: Basic File Handler

## Purpose
Process files from /Needs Action folder, summarize content, create action plans, and organize completed work.

## Procedure

### Step 1: Reference Company Handbook
Before any action, review rules from `/Company Handbook.md`:
- Always be polite in replies
- Flag payments > $500 for approval

### Step 2: Read File from /Needs Action
- Navigate to `/Needs Action/`
- Read the target .md file
- Extract key information

### Step 3: Summarize Content
- Identify main request/task
- Note any amounts, dates, or sensitive info
- Check against handbook rules

### Step 4: Write Plan to /Plans
Create `Plan.md` in `/Plans/` with:
- [ ] Checkbox item 1
- [ ] Checkbox item 2
- [ ] Checkbox item 3

### Step 5: Move Completed File
- Move processed file from `/Needs Action/` to `/Done/`
- Confirm file path change

## Output Format
```
SUCCESS: File processed
Source: /Needs Action/[filename].md
Plan: /Plans/Plan.md
Destination: /Done/[filename].md
```

## Example Usage
```
Basic File Handler process Needs_Action/invoice_request.md
```