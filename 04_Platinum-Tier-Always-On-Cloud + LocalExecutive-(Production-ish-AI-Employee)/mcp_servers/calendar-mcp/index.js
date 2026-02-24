#!/usr/bin/env node
/**
 * AI Employee Gold Tier — Calendar MCP Server
 * ============================================
 *
 * A Model Context Protocol (MCP) server exposing Google Calendar event
 * creation and update capabilities to Claude via tool-use.
 *
 * TOOLS EXPOSED:
 *   draft_calendar_event    — compose event, save to /Plans + /Pending Approval
 *   approve_calendar_draft  — HITL: move draft to /Approved
 *   reject_calendar_draft   — HITL: move draft to /Rejected
 *   create_calendar_event   — create in Google Calendar only after HITL approval
 *   update_calendar_event   — update an existing event only after HITL approval
 *   list_calendar_drafts    — list all calendar drafts and their approval status
 *
 * HITL FLOW:
 *   draft_calendar_event  →  /Plans + /Pending Approval  (status: pending_approval)
 *       |
 *   Human reviews /Pending Approval/calendar_draft_*.md
 *       |
 *   approve_calendar_draft  →  /Approved  (status: approved)
 *       |
 *   create_calendar_event  →  Google Calendar event created + /Done  (status: created)
 *
 * INSTALL:
 *   cd mcp_servers/calendar-mcp
 *   npm install
 *
 * AUTH (one-time Google Calendar OAuth2 setup):
 *   node index.js --auth
 *   Opens browser, complete OAuth consent, saves calendar_token.json to project root.
 *   Uses the same credentials.json as the email-mcp.
 *
 * RUN:
 *   node mcp_servers/calendar-mcp/index.js
 *
 * RUN WITH PM2:
 *   pm2 start mcp_servers/calendar-mcp/index.js --name calendar-mcp --interpreter node
 *   pm2 save && pm2 startup
 *
 * PM2 MANAGEMENT:
 *   pm2 logs calendar-mcp       # Live logs
 *   pm2 restart calendar-mcp    # Restart
 *   pm2 stop calendar-mcp       # Stop
 *
 * CREDENTIALS SETUP:
 *   1. Go to console.cloud.google.com -> APIs & Services -> Library
 *   2. Enable "Google Calendar API"
 *   3. Go to APIs & Services -> Credentials
 *   4. Use same OAuth 2.0 Desktop credentials.json as email-mcp (or create new one)
 *   5. Place credentials.json in project root (same location as email-mcp uses)
 *   6. Run: node mcp_servers/calendar-mcp/index.js --auth
 *   7. Complete OAuth consent in browser -> calendar_token.json saved to project root
 *
 * TEST:
 *   Ask Claude: "Draft a calendar event: Team standup tomorrow 10am-10:30am"
 *   Check /Plans and /Pending Approval for the saved draft.
 *   Then: "Approve calendar_draft_[date]_Team_standup.md"
 *   Then: "Create the approved calendar event"
 */

import { Server }               from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

import { google }       from "googleapis";
import { OAuth2Client } from "google-auth-library";

import {
  existsSync, mkdirSync, readFileSync, writeFileSync,
  readdirSync, renameSync, copyFileSync,
} from "fs";
import { join, dirname, basename } from "path";
import { fileURLToPath }          from "url";
import readline                   from "readline";


// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

const __dirname        = dirname(fileURLToPath(import.meta.url));
const BASE_DIR         = join(__dirname, "..", "..");

const CREDENTIALS_FILE = join(BASE_DIR, "credentials.json");
const TOKEN_FILE       = join(BASE_DIR, "calendar_token.json");   // separate from email token
const PLANS_DIR        = join(BASE_DIR, "Plans");
const PENDING_DIR      = join(BASE_DIR, "Pending Approval");
const APPROVED_DIR     = join(BASE_DIR, "Approved");
const REJECTED_DIR     = join(BASE_DIR, "Rejected");
const DONE_DIR         = join(BASE_DIR, "Done");
const HANDBOOK_FILE    = join(BASE_DIR, "Company Handbook.md");

const CALENDAR_SCOPES = [
  "https://www.googleapis.com/auth/calendar.events",
  "https://www.googleapis.com/auth/calendar.readonly",
];

// Default timezone — override via draft_calendar_event(timezone: "...")
const DEFAULT_TIMEZONE = "America/New_York";


// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function dateStr() {
  return new Date().toISOString().slice(0, 10);
}

function isoNow() {
  return new Date().toISOString().replace("T", " ").slice(0, 19);
}

function ensureDir(dir) {
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
}

function sanitizeFilename(str) {
  return (str || "event")
    .replace(/[/\\?%*:|"<>]/g, "_")
    .replace(/\s+/g, "_")
    .slice(0, 50)
    .trim();
}

function uniquePath(dir, filename) {
  let target = join(dir, filename);
  const stem   = filename.replace(/\.md$/, "");
  let counter  = 1;
  while (existsSync(target)) {
    target = join(dir, `${stem}_${counter}.md`);
    counter++;
  }
  return target;
}

/** Parse YAML frontmatter fields from a .md draft file. */
function parseDraftFile(filePath) {
  try {
    const content  = readFileSync(filePath, "utf8");
    const yamlBlock = content.match(/^---\n([\s\S]*?)\n---/);
    if (!yamlBlock) return null;
    const fields = {};
    for (const line of yamlBlock[1].split("\n")) {
      const m = line.match(/^(\w+):\s*["']?(.+?)["']?\s*$/);
      if (m) fields[m[1]] = m[2];
    }
    return fields;
  } catch {
    return null;
  }
}

/** Rewrite the `status:` field in a draft's YAML frontmatter. */
function updateDraftStatus(filePath, newStatus) {
  try {
    let content = readFileSync(filePath, "utf8");
    content = content.replace(/^status:\s*.+$/m, `status: ${newStatus}`);
    writeFileSync(filePath, content, "utf8");
  } catch (_) { /* non-fatal */ }
}

/** Add or update a `calendar_event_id:` field in YAML frontmatter. */
function writeEventId(filePath, eventId) {
  try {
    let content = readFileSync(filePath, "utf8");
    if (/^calendar_event_id:/m.test(content)) {
      content = content.replace(/^calendar_event_id:\s*.+$/m, `calendar_event_id: "${eventId}"`);
    } else {
      content = content.replace(
        /^status:/m,
        `calendar_event_id: "${eventId}"\nstatus:`
      );
    }
    writeFileSync(filePath, content, "utf8");
  } catch (_) { /* non-fatal */ }
}

function loadHandbook() {
  if (existsSync(HANDBOOK_FILE)) {
    return readFileSync(HANDBOOK_FILE, "utf8").trim();
  }
  return "Always be polite. Flag payments > $500 for approval.";
}


// ---------------------------------------------------------------------------
// Google Calendar OAuth2
// ---------------------------------------------------------------------------

let _calendarClient = null;

function loadCredentials() {
  if (!existsSync(CREDENTIALS_FILE)) {
    throw new Error(
      `credentials.json not found at ${CREDENTIALS_FILE}.\n` +
      "Download OAuth2 Desktop credentials from Google Cloud Console\n" +
      "and place in the project root (same file as email-mcp uses)."
    );
  }
  const raw = JSON.parse(readFileSync(CREDENTIALS_FILE, "utf8"));
  const { client_secret, client_id, redirect_uris } =
    raw.installed || raw.web ||
    (() => { throw new Error("Invalid credentials.json format."); })();
  return new OAuth2Client(client_id, client_secret, redirect_uris[0]);
}

function getCalendarClient() {
  if (_calendarClient) return _calendarClient;

  const auth = loadCredentials();

  if (!existsSync(TOKEN_FILE)) {
    throw new Error(
      "calendar_token.json not found. Run one-time auth:\n" +
      "  node mcp_servers/calendar-mcp/index.js --auth\n" +
      "Then restart the MCP server."
    );
  }

  const token = JSON.parse(readFileSync(TOKEN_FILE, "utf8"));
  auth.setCredentials(token);

  // Auto-refresh on token expiry
  auth.on("tokens", (newTokens) => {
    const merged = { ...token, ...newTokens };
    writeFileSync(TOKEN_FILE, JSON.stringify(merged, null, 2));
  });

  _calendarClient = auth;
  return auth;
}

/** One-time OAuth2 consent flow. Run: node index.js --auth */
async function runAuthFlow() {
  const auth = loadCredentials();

  const authUrl = auth.generateAuthUrl({
    access_type: "offline",
    scope:       CALENDAR_SCOPES,
    prompt:      "consent",
  });

  console.log("\n================================================");
  console.log("  Calendar MCP Server -- Google Calendar OAuth2");
  console.log("================================================\n");
  console.log("1. Open this URL in your browser:\n");
  console.log("   " + authUrl + "\n");
  console.log("2. Sign in with your Google account.");
  console.log("3. Grant access to Google Calendar.");
  console.log("4. Copy the authorization code from the browser.");
  console.log("5. Paste it below and press Enter.\n");

  const code = await new Promise((resolve) => {
    const rl = readline.createInterface({
      input:  process.stdin,
      output: process.stdout,
    });
    rl.question("Authorization code: ", (ans) => {
      rl.close();
      resolve(ans.trim());
    });
  });

  const { tokens } = await auth.getToken(code);
  auth.setCredentials(tokens);
  writeFileSync(TOKEN_FILE, JSON.stringify(tokens, null, 2));

  console.log("\n[OK] calendar_token.json saved to:", TOKEN_FILE);
  console.log("[OK] Google Calendar auth complete. You can now run the MCP server.\n");
  process.exit(0);
}


// ---------------------------------------------------------------------------
// Build a Google Calendar event resource from draft fields
// ---------------------------------------------------------------------------

function buildEventResource(fields) {
  const {
    title, description = "", location = "",
    start_datetime, end_datetime,
    timezone = DEFAULT_TIMEZONE,
    attendees = "",
  } = fields;

  const resource = {
    summary:     title,
    description: description,
    location:    location,
    start: {
      dateTime: new Date(start_datetime).toISOString(),
      timeZone: timezone,
    },
    end: {
      dateTime: new Date(end_datetime).toISOString(),
      timeZone: timezone,
    },
  };

  // Parse comma-separated attendee emails
  const attendeeList = attendees
    .split(",")
    .map((e) => e.trim())
    .filter((e) => e.includes("@"))
    .map((email) => ({ email }));

  if (attendeeList.length > 0) {
    resource.attendees = attendeeList;
  }

  return resource;
}


// ---------------------------------------------------------------------------
// Tool: draft_calendar_event
// ---------------------------------------------------------------------------

async function draftCalendarEvent({
  title,
  start_datetime,
  end_datetime,
  description = "",
  attendees   = "",
  location    = "",
  timezone    = DEFAULT_TIMEZONE,
  notes       = "",
}) {
  ensureDir(PLANS_DIR);
  ensureDir(PENDING_DIR);

  const handbook = loadHandbook();
  const date     = dateStr();
  const safeTitle = sanitizeFilename(title);
  const filename  = `calendar_draft_${date}_${safeTitle}.md`;

  const planPath    = uniquePath(PLANS_DIR,   filename);
  const pendingPath = uniquePath(PENDING_DIR, filename);
  const finalName   = basename(planPath);

  // Handbook: detect payments > $500 in description
  const paymentFlag =
    /\$\s?(\d[\d,]*\.?\d*)/.test(description) &&
    (() => {
      const m = description.match(/\$\s?(\d[\d,]*\.?\d*)/g) || [];
      return m.some((v) => parseFloat(v.replace(/[$,]/g, "")) > 500);
    })();

  const priority = paymentFlag ? "high" : "medium";
  const flagNote = paymentFlag
    ? "HANDBOOK FLAG: Payment > $500 in event description -- HITL required."
    : "No special handbook flags.";

  const attendeeList = attendees || "(none)";
  const content = `---
type: calendar_draft
title: "${title}"
start_datetime: "${start_datetime}"
end_datetime: "${end_datetime}"
timezone: "${timezone}"
attendees: "${attendees}"
location: "${location}"
description: "${description.replace(/"/g, "'")}"
priority: ${priority}
status: pending_approval
created: "${isoNow()}"
calendar_event_id: ""
draft_file: "${finalName}"
---

# Calendar Event Draft -- ${date}

| Field         | Value |
|---------------|-------|
| Title         | ${title} |
| Start         | ${start_datetime} |
| End           | ${end_datetime} |
| Timezone      | ${timezone} |
| Location      | ${location || "(none)"} |
| Attendees     | ${attendeeList} |
| Priority      | ${priority.toUpperCase()} |
| Status        | pending_approval |
| Created       | ${isoNow()} |

## Event Description

${description || "(none)"}

## Handbook Check

- Rules applied: ${handbook.replace(/\n/g, " | ")}
- ${flagNote}

## Notes (Internal)

${notes || "(none)"}

## HITL Approval Checklist

- [ ] Verify event title and description are appropriate
- [ ] Confirm start/end times are correct
- [ ] Check timezone is correct (${timezone})
- [ ] Verify attendee list
- [ ] Approve any payment references > $500
- [ ] Move to /Approved to authorise calendar creation

## Approval Flow

1. Review this file in /Pending Approval/
2. Run: approve_calendar_draft("${finalName}")  -- to approve
3. Run: reject_calendar_draft("${finalName}")   -- to reject
4. Run: create_calendar_event("${finalName}")   -- after approval to create in Google Calendar
`;

  writeFileSync(planPath, content, "utf8");
  copyFileSync(planPath, pendingPath);

  return {
    success:         true,
    filename:        finalName,
    plan_path:       `Plans/${finalName}`,
    pending_path:    `Pending Approval/${finalName}`,
    priority,
    payment_flagged: paymentFlag,
    message:
      `Calendar event draft saved. File is in /Pending Approval awaiting HITL review.\n` +
      `Next: approve_calendar_draft("${finalName}") then create_calendar_event("${finalName}")`,
  };
}


// ---------------------------------------------------------------------------
// Tool: approve_calendar_draft
// ---------------------------------------------------------------------------

function approveCalendarDraft({ filename }) {
  ensureDir(APPROVED_DIR);

  const pendingPath  = join(PENDING_DIR, filename);
  const approvedPath = join(APPROVED_DIR, filename);

  if (!existsSync(pendingPath)) {
    if (existsSync(approvedPath)) {
      return { success: false, message: `Draft already approved: ${filename}` };
    }
    return {
      success: false,
      message:
        `Draft not found in /Pending Approval: ${filename}.\n` +
        "Use list_calendar_drafts() to see available drafts.",
    };
  }

  renameSync(pendingPath, approvedPath);
  updateDraftStatus(approvedPath, "approved");

  const planPath = join(PLANS_DIR, filename);
  if (existsSync(planPath)) updateDraftStatus(planPath, "approved");

  return {
    success:       true,
    filename,
    approved_path: `Approved/${filename}`,
    message:       `Draft approved. Now call create_calendar_event("${filename}") to create in Google Calendar.`,
  };
}


// ---------------------------------------------------------------------------
// Tool: reject_calendar_draft
// ---------------------------------------------------------------------------

function rejectCalendarDraft({ filename, reason = "Rejected by reviewer" }) {
  ensureDir(REJECTED_DIR);

  const pendingPath  = join(PENDING_DIR, filename);
  const rejectedPath = join(REJECTED_DIR, filename);

  if (!existsSync(pendingPath)) {
    return {
      success: false,
      message: `Draft not found in /Pending Approval: ${filename}`,
    };
  }

  renameSync(pendingPath, rejectedPath);
  updateDraftStatus(rejectedPath, "rejected");

  const planPath = join(PLANS_DIR, filename);
  if (existsSync(planPath)) updateDraftStatus(planPath, "rejected");

  return {
    success:       true,
    filename,
    rejected_path: `Rejected/${filename}`,
    reason,
    message:       `Draft rejected and archived. Reason: ${reason}`,
  };
}


// ---------------------------------------------------------------------------
// Tool: create_calendar_event
// ---------------------------------------------------------------------------

async function createCalendarEvent({ filename }) {
  // HITL gate: must be in /Approved
  const approvedPath = join(APPROVED_DIR, filename);
  const pendingPath  = join(PENDING_DIR, filename);

  if (existsSync(pendingPath) && !existsSync(approvedPath)) {
    return {
      success:      false,
      hitl_blocked: true,
      message:
        `HITL GATE: Draft "${filename}" is in /Pending Approval and has NOT been approved.\n` +
        `Steps:\n` +
        `  1. Review:  Pending Approval/${filename}\n` +
        `  2. Approve: approve_calendar_draft("${filename}")\n` +
        `  3. Create:  create_calendar_event("${filename}")`,
    };
  }

  if (!existsSync(approvedPath)) {
    const planPath = join(PLANS_DIR, filename);
    if (existsSync(planPath)) {
      return {
        success:      false,
        hitl_blocked: true,
        message:
          `HITL GATE: Draft found in /Plans but not approved.\n` +
          `Approve first: approve_calendar_draft("${filename}")`,
      };
    }
    return {
      success: false,
      message: `Draft file not found: ${filename}. Use list_calendar_drafts() to find it.`,
    };
  }

  // Parse draft
  const fields = parseDraftFile(approvedPath);
  if (!fields) {
    return { success: false, message: `Could not parse YAML frontmatter in ${filename}` };
  }

  const { title, start_datetime, end_datetime } = fields;
  if (!title || !start_datetime || !end_datetime) {
    return {
      success: false,
      message: "Draft is missing required fields: title, start_datetime, or end_datetime.",
    };
  }

  // Build event resource
  const eventResource = buildEventResource(fields);

  // Call Google Calendar API
  let auth;
  try {
    auth = getCalendarClient();
  } catch (err) {
    return { success: false, message: `Calendar auth failed: ${err.message}` };
  }

  const calendar = google.calendar({ version: "v3", auth });

  let createdEvent;
  try {
    const response = await calendar.events.insert({
      calendarId:   "primary",
      requestBody:  eventResource,
      sendUpdates:  "all",   // notify attendees
    });
    createdEvent = response.data;
  } catch (err) {
    return {
      success: false,
      message: `Google Calendar API error: ${err.message}`,
    };
  }

  const eventId  = createdEvent.id;
  const eventUrl = createdEvent.htmlLink;

  // Archive to /Done
  ensureDir(DONE_DIR);
  const donePath = join(DONE_DIR, filename);
  renameSync(approvedPath, donePath);
  updateDraftStatus(donePath, "created");
  writeEventId(donePath, eventId);

  const planPath = join(PLANS_DIR, filename);
  if (existsSync(planPath)) {
    updateDraftStatus(planPath, "created");
    writeEventId(planPath, eventId);
  }

  return {
    success:          true,
    filename,
    calendar_event_id: eventId,
    event_url:        eventUrl,
    title,
    start:            start_datetime,
    end:              end_datetime,
    attendees:        fields.attendees || "(none)",
    done_path:        `Done/${filename}`,
    message:
      `Google Calendar event created successfully.\n` +
      `Event ID: ${eventId}\n` +
      `URL: ${eventUrl}\n` +
      `Draft archived to /Done.`,
  };
}


// ---------------------------------------------------------------------------
// Tool: update_calendar_event
// ---------------------------------------------------------------------------

async function updateCalendarEvent({ filename, event_id }) {
  // HITL gate: must be in /Approved
  const approvedPath = join(APPROVED_DIR, filename);
  const pendingPath  = join(PENDING_DIR, filename);

  if (existsSync(pendingPath) && !existsSync(approvedPath)) {
    return {
      success:      false,
      hitl_blocked: true,
      message:
        `HITL GATE: Draft "${filename}" has NOT been approved.\n` +
        `Approve first: approve_calendar_draft("${filename}")`,
    };
  }

  if (!existsSync(approvedPath)) {
    return {
      success: false,
      message: `Approved draft not found: ${filename}. Use list_calendar_drafts() to see status.`,
    };
  }

  const fields = parseDraftFile(approvedPath);
  if (!fields) {
    return { success: false, message: `Could not parse draft: ${filename}` };
  }

  // Resolve event_id: parameter takes priority, then YAML field
  const targetEventId = event_id || fields.calendar_event_id;
  if (!targetEventId) {
    return {
      success: false,
      message:
        "No event_id provided and none found in draft YAML.\n" +
        "Pass event_id parameter explicitly: update_calendar_event(filename, event_id)",
    };
  }

  const eventResource = buildEventResource(fields);

  let auth;
  try {
    auth = getCalendarClient();
  } catch (err) {
    return { success: false, message: `Calendar auth failed: ${err.message}` };
  }

  const calendar = google.calendar({ version: "v3", auth });

  let updatedEvent;
  try {
    const response = await calendar.events.update({
      calendarId:   "primary",
      eventId:      targetEventId,
      requestBody:  eventResource,
      sendUpdates:  "all",
    });
    updatedEvent = response.data;
  } catch (err) {
    return {
      success: false,
      message: `Google Calendar API error on update: ${err.message}`,
    };
  }

  const eventUrl = updatedEvent.htmlLink;

  ensureDir(DONE_DIR);
  const donePath = join(DONE_DIR, filename);
  renameSync(approvedPath, donePath);
  updateDraftStatus(donePath, "updated");
  writeEventId(donePath, targetEventId);

  const planPath = join(PLANS_DIR, filename);
  if (existsSync(planPath)) {
    updateDraftStatus(planPath, "updated");
    writeEventId(planPath, targetEventId);
  }

  return {
    success:           true,
    filename,
    calendar_event_id: targetEventId,
    event_url:         eventUrl,
    title:             fields.title,
    done_path:         `Done/${filename}`,
    message:
      `Google Calendar event updated.\n` +
      `Event ID: ${targetEventId}\n` +
      `URL: ${eventUrl}`,
  };
}


// ---------------------------------------------------------------------------
// Tool: list_calendar_drafts
// ---------------------------------------------------------------------------

function listCalendarDrafts() {
  const statusFolders = [
    { folder: PLANS_DIR,    label: "Plans (draft)" },
    { folder: PENDING_DIR,  label: "Pending Approval" },
    { folder: APPROVED_DIR, label: "Approved" },
    { folder: DONE_DIR,     label: "Done (created)" },
    { folder: REJECTED_DIR, label: "Rejected" },
  ];

  const results = [];
  const seen    = new Set();

  for (const { folder, label } of statusFolders) {
    if (!existsSync(folder)) continue;
    for (const file of readdirSync(folder)) {
      if (!file.startsWith("calendar_draft_") || !file.endsWith(".md")) continue;
      if (seen.has(file)) continue;
      seen.add(file);

      const fields = parseDraftFile(join(folder, file)) || {};
      results.push({
        filename:    file,
        location:    label,
        title:       fields.title    || "(unknown)",
        start:       fields.start_datetime || "(unknown)",
        priority:    fields.priority || "medium",
        status:      fields.status   || "unknown",
        event_id:    fields.calendar_event_id || "",
        created:     fields.created  || "(unknown)",
      });
    }
  }

  if (results.length === 0) {
    return { count: 0, drafts: [], message: "No calendar drafts found." };
  }

  const formatted = results.map(
    (r, i) =>
      `${i + 1}. [${r.status.toUpperCase()}] ${r.filename}\n` +
      `   Title: ${r.title} | Start: ${r.start} | Location: ${r.location}` +
      (r.event_id ? ` | Calendar ID: ${r.event_id}` : "")
  ).join("\n");

  return {
    count:   results.length,
    drafts:  results,
    formatted,
    message: `Found ${results.length} calendar draft(s).`,
  };
}


// ---------------------------------------------------------------------------
// MCP Server Setup
// ---------------------------------------------------------------------------

const server = new Server(
  { name: "calendar-mcp-server", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

// --- List Tools ---
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "draft_calendar_event",
      description:
        "Draft a Google Calendar event. Saves to /Plans and /Pending Approval. " +
        "HITL approval required before creating. Call approve_calendar_draft then create_calendar_event.",
      inputSchema: {
        type: "object",
        properties: {
          title: {
            type: "string",
            description: "Event title / summary (required)",
          },
          start_datetime: {
            type: "string",
            description: "Start date/time in ISO 8601 format or natural language, e.g. '2026-02-22T10:00:00' or '2026-02-22 10:00' (required)",
          },
          end_datetime: {
            type: "string",
            description: "End date/time in ISO 8601 format or natural language (required)",
          },
          description: {
            type: "string",
            description: "Event description / agenda (optional)",
          },
          attendees: {
            type: "string",
            description: "Comma-separated attendee email addresses, e.g. 'alice@co.com, bob@co.com' (optional)",
          },
          location: {
            type: "string",
            description: "Physical location or video call URL (optional)",
          },
          timezone: {
            type: "string",
            description: `Timezone string, e.g. 'America/New_York', 'Europe/London' (default: ${DEFAULT_TIMEZONE})`,
          },
          notes: {
            type: "string",
            description: "Internal reviewer notes (not added to calendar event) (optional)",
          },
        },
        required: ["title", "start_datetime", "end_datetime"],
      },
    },
    {
      name: "approve_calendar_draft",
      description:
        "HITL approval step. Moves a calendar draft from /Pending Approval to /Approved. " +
        "Must be called before create_calendar_event.",
      inputSchema: {
        type: "object",
        properties: {
          filename: {
            type: "string",
            description: "Draft filename, e.g. calendar_draft_2026-02-22_Team_standup.md. Use list_calendar_drafts to find it.",
          },
        },
        required: ["filename"],
      },
    },
    {
      name: "reject_calendar_draft",
      description:
        "HITL rejection. Moves a calendar draft from /Pending Approval to /Rejected. Event will not be created.",
      inputSchema: {
        type: "object",
        properties: {
          filename: { type: "string", description: "Draft filename to reject." },
          reason:   { type: "string", description: "Reason for rejection (optional)." },
        },
        required: ["filename"],
      },
    },
    {
      name: "create_calendar_event",
      description:
        "Create an approved calendar event in Google Calendar. " +
        "HITL GATE: Draft MUST be in /Approved first (call approve_calendar_draft). " +
        "Moves created draft to /Done. Notifies attendees automatically.",
      inputSchema: {
        type: "object",
        properties: {
          filename: {
            type: "string",
            description: "Approved draft filename. Must be in /Approved folder.",
          },
        },
        required: ["filename"],
      },
    },
    {
      name: "update_calendar_event",
      description:
        "Update an existing Google Calendar event from an approved draft. " +
        "HITL GATE: Draft must be approved first. Provide the existing Google Calendar event_id.",
      inputSchema: {
        type: "object",
        properties: {
          filename: {
            type: "string",
            description: "Approved draft filename to use for the update.",
          },
          event_id: {
            type: "string",
            description: "Google Calendar event ID to update (from a previous create or from the Calendar UI). Optional if already stored in the draft YAML.",
          },
        },
        required: ["filename"],
      },
    },
    {
      name: "list_calendar_drafts",
      description:
        "List all calendar drafts across all folders (Plans, Pending Approval, Approved, Done, Rejected) with their status.",
      inputSchema: {
        type: "object",
        properties: {},
        required: [],
      },
    },
  ],
}));

// --- Call Tools ---
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  let result;

  try {
    switch (name) {
      case "draft_calendar_event":    result = await draftCalendarEvent(args); break;
      case "approve_calendar_draft":  result = approveCalendarDraft(args);     break;
      case "reject_calendar_draft":   result = rejectCalendarDraft(args);      break;
      case "create_calendar_event":   result = await createCalendarEvent(args); break;
      case "update_calendar_event":   result = await updateCalendarEvent(args); break;
      case "list_calendar_drafts":    result = listCalendarDrafts();            break;
      default:
        return {
          content: [{ type: "text", text: `Unknown tool: ${name}` }],
          isError: true,
        };
    }
  } catch (err) {
    return {
      content: [{ type: "text", text: `Tool execution error: ${err.message}` }],
      isError: true,
    };
  }

  return {
    content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
  };
});


// ---------------------------------------------------------------------------
// Entry Point
// ---------------------------------------------------------------------------

async function main() {
  if (process.argv.includes("--auth")) {
    await runAuthFlow();
    return;
  }

  const transport = new StdioServerTransport();
  await server.connect(transport);

  process.stderr.write("[calendar-mcp] Server started. Listening on stdio.\n");
  process.stderr.write(`[calendar-mcp] Base directory: ${BASE_DIR}\n`);
  process.stderr.write("[calendar-mcp] Token file: calendar_token.json\n");
  process.stderr.write(
    "[calendar-mcp] Tools: draft_calendar_event | approve_calendar_draft | " +
    "reject_calendar_draft | create_calendar_event | update_calendar_event | list_calendar_drafts\n"
  );
}

main().catch((err) => {
  process.stderr.write(`[calendar-mcp] Fatal error: ${err.message}\n`);
  process.exit(1);
});
