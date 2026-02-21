#!/usr/bin/env node
/**
 * AI Employee Silver Tier — Email MCP Server
 * ===========================================
 *
 * A Model Context Protocol (MCP) server exposing Gmail draft and send
 * capabilities to Claude via tool-use.
 *
 * TOOLS EXPOSED:
 *   draft_email        — compose email, save to /Plans + /Pending Approval
 *   send_email         — send only after HITL approval (file must be in /Approved)
 *   list_email_drafts  — list all email drafts and their approval status
 *   approve_draft      — move a draft from /Pending Approval to /Approved (HITL gate)
 *   reject_draft       — move a draft from /Pending Approval to /Rejected
 *
 * HITL FLOW:
 *   draft_email  →  /Plans + /Pending Approval  (status: pending_approval)
 *       ↓
 *   Human reviews /Pending Approval
 *       ↓
 *   approve_draft  →  /Approved  (status: approved)
 *       ↓
 *   send_email  →  Gmail sent + /Done  (status: sent)
 *
 * INSTALL:
 *   cd mcp_servers/email-mcp
 *   npm install
 *
 * AUTH (one-time Gmail OAuth2 setup):
 *   node index.js --auth
 *   Follow browser prompt, paste the code — saves token.json to project root.
 *
 * RUN:
 *   node mcp_servers/email-mcp/index.js
 *
 * RUN WITH PM2:
 *   pm2 start mcp_servers/email-mcp/index.js --name email-mcp --interpreter node
 *   pm2 save && pm2 startup
 *
 * TEST DRAFT:
 *   Use Claude with this MCP server enabled and call:
 *   "Draft an email to test@example.com, subject 'Hello', body 'Test message'"
 *   Check /Plans and /Pending Approval for the saved draft.
 *
 * TEST SEND:
 *   1. Run draft_email to create a draft
 *   2. Run approve_draft with the draft filename
 *   3. Run send_email with the approved draft filename
 *   4. Check /Done — the sent draft will be archived there.
 *
 * INTEGRATE WITH CLAUDE CODE:
 *   Add to your project's mcp.json (see root mcp.json).
 *   Then in Claude: the email tools appear automatically.
 */

import { Server }               from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

import { google }        from "googleapis";
import { OAuth2Client }  from "google-auth-library";

import {
  existsSync, mkdirSync, readFileSync, writeFileSync,
  readdirSync, renameSync, copyFileSync,
} from "fs";
import { join, dirname, basename } from "path";
import { fileURLToPath }          from "url";
import { createServer }           from "http";


// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

const __dirname          = dirname(fileURLToPath(import.meta.url));
const BASE_DIR           = join(__dirname, "..", "..");

const CREDENTIALS_FILE   = join(BASE_DIR, "credentials.json");
const TOKEN_FILE         = join(BASE_DIR, "token.json");
const PLANS_DIR          = join(BASE_DIR, "Plans");
const PENDING_DIR        = join(BASE_DIR, "Pending Approval");
const APPROVED_DIR       = join(BASE_DIR, "Approved");
const REJECTED_DIR       = join(BASE_DIR, "Rejected");
const DONE_DIR           = join(BASE_DIR, "Done");
const HANDBOOK_FILE      = join(BASE_DIR, "Company Handbook.md");

const GMAIL_SCOPES = [
  "https://www.googleapis.com/auth/gmail.send",
  "https://www.googleapis.com/auth/gmail.compose",
  "https://www.googleapis.com/auth/gmail.readonly",
];


// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function ts() {
  return new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
}

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
  return (str || "no-subject").replace(/[/\\?%*:|"<>]/g, "_").slice(0, 60).trim();
}

/**
 * Encode an email body as base64url for the Gmail API.
 */
function buildRawEmail({ from, to, cc, bcc, subject, body }) {
  const headers = [
    `From: ${from}`,
    `To: ${to}`,
    cc  ? `Cc: ${cc}`   : null,
    bcc ? `Bcc: ${bcc}` : null,
    "MIME-Version: 1.0",
    "Content-Type: text/plain; charset=utf-8",
    `Subject: ${subject}`,
    "",
    body,
  ].filter(Boolean).join("\r\n");

  return Buffer.from(headers).toString("base64url");
}

/**
 * Read a draft .md file and parse its YAML frontmatter fields.
 */
function parseDraftFile(filePath) {
  const content = readFileSync(filePath, "utf8");
  const yamlBlock = content.match(/^---\n([\s\S]*?)\n---/);
  if (!yamlBlock) return null;

  const fields = {};
  for (const line of yamlBlock[1].split("\n")) {
    const m = line.match(/^(\w+):\s*["']?(.+?)["']?\s*$/);
    if (m) fields[m[1]] = m[2];
  }
  return fields;
}

/**
 * Update the `status` field in a draft .md file's YAML frontmatter.
 */
function updateDraftStatus(filePath, newStatus) {
  let content = readFileSync(filePath, "utf8");
  content = content.replace(/^status:\s*.+$/m, `status: ${newStatus}`);
  writeFileSync(filePath, content, "utf8");
}

/**
 * Load handbook rules as a string (with fallback).
 */
function loadHandbook() {
  if (existsSync(HANDBOOK_FILE)) {
    return readFileSync(HANDBOOK_FILE, "utf8").trim();
  }
  return "Always be polite. Flag payments > $500 for approval.";
}

/**
 * Return the file path in /Pending Approval or /Approved for a given filename.
 */
function findDraftFile(filename) {
  for (const dir of [APPROVED_DIR, PENDING_DIR, PLANS_DIR]) {
    const p = join(dir, filename);
    if (existsSync(p)) return { path: p, folder: basename(dir) };
  }
  return null;
}


// ---------------------------------------------------------------------------
// Gmail OAuth2
// ---------------------------------------------------------------------------

let _gmailClient = null;

function loadCredentials() {
  if (!existsSync(CREDENTIALS_FILE)) {
    throw new Error(
      `credentials.json not found at ${CREDENTIALS_FILE}. ` +
      "Download OAuth2 credentials from Google Cloud Console and place in project root."
    );
  }
  const raw = JSON.parse(readFileSync(CREDENTIALS_FILE, "utf8"));
  const { client_secret, client_id, redirect_uris } =
    raw.installed || raw.web || (() => { throw new Error("Invalid credentials.json format."); })();
  return new OAuth2Client(client_id, client_secret, redirect_uris[0]);
}

function getAuthenticatedClient() {
  if (_gmailClient) return _gmailClient;

  const client = loadCredentials();

  if (!existsSync(TOKEN_FILE)) {
    throw new Error(
      "No token.json found. Run one-time auth:\n" +
      "  node mcp_servers/email-mcp/index.js --auth\n" +
      "Then restart the MCP server."
    );
  }

  const token = JSON.parse(readFileSync(TOKEN_FILE, "utf8"));
  client.setCredentials(token);

  // Auto-refresh on expiry
  client.on("tokens", (newTokens) => {
    const merged = { ...token, ...newTokens };
    writeFileSync(TOKEN_FILE, JSON.stringify(merged, null, 2));
  });

  _gmailClient = client;
  return client;
}

/**
 * One-time OAuth2 flow — opens browser, waits for code, saves token.json.
 * Run with:  node mcp_servers/email-mcp/index.js --auth
 */
async function runAuthFlow() {
  const client = loadCredentials();

  const authUrl = client.generateAuthUrl({
    access_type: "offline",
    scope: GMAIL_SCOPES,
    prompt: "consent",
  });

  console.log("\n========================================");
  console.log(" Email MCP Server — Gmail OAuth2 Setup ");
  console.log("========================================\n");
  console.log("1. Open this URL in your browser:\n");
  console.log("   " + authUrl + "\n");
  console.log("2. Sign in and approve the Gmail permissions.");
  console.log("3. Copy the authorization code from the browser.");
  console.log("4. Paste it below and press Enter.\n");

  const code = await new Promise((resolve) => {
    const rl = (await import("readline")).createInterface({
      input: process.stdin,
      output: process.stdout,
    });
    rl.question("Authorization code: ", (ans) => {
      rl.close();
      resolve(ans.trim());
    });
  });

  const { tokens } = await client.getToken(code);
  client.setCredentials(tokens);
  writeFileSync(TOKEN_FILE, JSON.stringify(tokens, null, 2));

  console.log("\n✓ token.json saved to:", TOKEN_FILE);
  console.log("✓ Gmail authentication complete. You can now run the MCP server.\n");
  process.exit(0);
}


// ---------------------------------------------------------------------------
// Tool: draft_email
// ---------------------------------------------------------------------------

async function draftEmail({ to, subject, body, cc = "", bcc = "", notes = "" }) {
  ensureDir(PLANS_DIR);
  ensureDir(PENDING_DIR);

  const handbook = loadHandbook();
  const timestamp = ts();
  const date = dateStr();
  const safeSubject = sanitizeFilename(subject);
  const filename = `email_draft_${date}_${safeSubject}.md`;

  const planPath    = join(PLANS_DIR, filename);
  const pendingPath = join(PENDING_DIR, filename);

  // Handbook check: flag payments
  const paymentFlag =
    /\$\s?(\d[\d,]*\.?\d*)/.test(body) &&
    (() => {
      const m = body.match(/\$\s?(\d[\d,]*\.?\d*)/g) || [];
      return m.some((v) => parseFloat(v.replace(/[$,]/g, "")) > 500);
    })();

  const priority = paymentFlag ? "high" : "medium";
  const flagNote = paymentFlag
    ? "⚠ HANDBOOK FLAG: Payment > $500 detected — HITL approval required."
    : "No special handbook flags.";

  const content = `---
type: email_draft
to: "${to}"
cc: "${cc}"
bcc: "${bcc}"
subject: "${subject}"
priority: ${priority}
status: pending_approval
created: "${isoNow()}"
draft_file: "${filename}"
---

# Email Draft — ${date}

| Field    | Value |
|----------|-------|
| To       | ${to} |
| Cc       | ${cc || "(none)"} |
| Bcc      | ${bcc || "(none)"} |
| Subject  | ${subject} |
| Priority | ${priority.toUpperCase()} |
| Status   | pending_approval |

## Body

${body}

## Handbook Check

- Rules applied: ${handbook.replace(/\n/g, " | ")}
- ${flagNote}

## Notes

${notes || "(none)"}

## HITL Approval Checklist

- [ ] Review email body for tone (must be polite per handbook)
- [ ] Verify recipient address is correct
- [ ] Confirm subject line is appropriate
- [ ] Approve payment amounts if any
- [ ] Move to /Approved to authorise sending

## Approval Flow

1. Review this file in /Pending Approval/
2. Run: approve_draft("${filename}")  — to approve
3. Run: reject_draft("${filename}")   — to reject
4. Run: send_email("${filename}")     — after approval to send
`;

  writeFileSync(planPath, content, "utf8");
  copyFileSync(planPath, pendingPath);

  return {
    success: true,
    filename,
    plan_path: `Plans/${filename}`,
    pending_path: `Pending Approval/${filename}`,
    priority,
    payment_flagged: paymentFlag,
    message:
      `Draft saved. File is in /Pending Approval awaiting HITL review.\n` +
      `Next: approve_draft("${filename}") then send_email("${filename}")`,
  };
}


// ---------------------------------------------------------------------------
// Tool: approve_draft
// ---------------------------------------------------------------------------

function approveDraft({ filename }) {
  ensureDir(APPROVED_DIR);

  const pendingPath  = join(PENDING_DIR, filename);
  const approvedPath = join(APPROVED_DIR, filename);

  if (!existsSync(pendingPath)) {
    // Check if already approved
    if (existsSync(approvedPath)) {
      return { success: false, message: `Draft already approved: ${filename}` };
    }
    return {
      success: false,
      message: `Draft not found in /Pending Approval: ${filename}. ` +
               "Use list_email_drafts to see available drafts.",
    };
  }

  renameSync(pendingPath, approvedPath);
  updateDraftStatus(approvedPath, "approved");

  // Sync status in /Plans copy too
  const planPath = join(PLANS_DIR, filename);
  if (existsSync(planPath)) updateDraftStatus(planPath, "approved");

  return {
    success: true,
    filename,
    approved_path: `Approved/${filename}`,
    message: `Draft approved. Now call send_email("${filename}") to dispatch.`,
  };
}


// ---------------------------------------------------------------------------
// Tool: reject_draft
// ---------------------------------------------------------------------------

function rejectDraft({ filename, reason = "Rejected by reviewer" }) {
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
    success: true,
    filename,
    rejected_path: `Rejected/${filename}`,
    reason,
    message: `Draft rejected and archived. Reason: ${reason}`,
  };
}


// ---------------------------------------------------------------------------
// Tool: send_email
// ---------------------------------------------------------------------------

async function sendEmail({ filename }) {
  // --- HITL gate: must be in /Approved ---
  const approvedPath = join(APPROVED_DIR, filename);
  const pendingPath  = join(PENDING_DIR, filename);

  if (existsSync(pendingPath) && !existsSync(approvedPath)) {
    return {
      success: false,
      hitl_blocked: true,
      message:
        `HITL GATE: Draft "${filename}" is in /Pending Approval and has NOT been approved yet.\n` +
        `Steps to send:\n` +
        `  1. Review: Pending Approval/${filename}\n` +
        `  2. Approve: approve_draft("${filename}")\n` +
        `  3. Send:    send_email("${filename}")`,
    };
  }

  if (!existsSync(approvedPath)) {
    // Check Plans as fallback — might have been placed there without approval
    const planPath = join(PLANS_DIR, filename);
    if (existsSync(planPath)) {
      return {
        success: false,
        hitl_blocked: true,
        message:
          `HITL GATE: Draft found in /Plans but not in /Approved.\n` +
          `You must approve it first: approve_draft("${filename}")`,
      };
    }
    return {
      success: false,
      message: `Draft file not found: ${filename}. Use list_email_drafts to see available drafts.`,
    };
  }

  // --- Parse draft ---
  const fields = parseDraftFile(approvedPath);
  if (!fields) {
    return { success: false, message: `Could not parse YAML frontmatter in ${filename}` };
  }

  const { to, cc, bcc, subject } = fields;
  if (!to || !subject) {
    return { success: false, message: "Draft is missing required 'to' or 'subject' fields." };
  }

  // Extract body — everything after the "## Body" heading
  const fullContent = readFileSync(approvedPath, "utf8");
  const bodyMatch = fullContent.match(/^## Body\n\n([\s\S]*?)(?=\n##|$)/m);
  const body = bodyMatch ? bodyMatch[1].trim() : "(no body)";

  // --- Send via Gmail API ---
  let auth;
  try {
    auth = getAuthenticatedClient();
  } catch (err) {
    return { success: false, message: `Gmail auth failed: ${err.message}` };
  }

  const gmail = google.gmail({ version: "v1", auth });

  // Get sender address
  let senderEmail = "me";
  try {
    const profile = await gmail.users.getProfile({ userId: "me" });
    senderEmail = profile.data.emailAddress;
  } catch (_) {
    // non-fatal
  }

  const raw = buildRawEmail({
    from: senderEmail,
    to,
    cc:  cc  || "",
    bcc: bcc || "",
    subject,
    body,
  });

  let gmailMessageId;
  try {
    const result = await gmail.users.messages.send({
      userId: "me",
      requestBody: { raw },
    });
    gmailMessageId = result.data.id;
  } catch (err) {
    return {
      success: false,
      message: `Gmail API send failed: ${err.message}`,
    };
  }

  // --- Archive to /Done ---
  ensureDir(DONE_DIR);
  const donePath = join(DONE_DIR, filename);
  renameSync(approvedPath, donePath);
  updateDraftStatus(donePath, "sent");

  const planPath = join(PLANS_DIR, filename);
  if (existsSync(planPath)) updateDraftStatus(planPath, "sent");

  return {
    success: true,
    filename,
    gmail_message_id: gmailMessageId,
    sent_to: to,
    subject,
    done_path: `Done/${filename}`,
    message: `Email sent successfully. Gmail message ID: ${gmailMessageId}. Archived to /Done.`,
  };
}


// ---------------------------------------------------------------------------
// Tool: list_email_drafts
// ---------------------------------------------------------------------------

function listEmailDrafts() {
  const results = [];

  const statusFolders = [
    { folder: PLANS_DIR,    label: "Plans (draft)" },
    { folder: PENDING_DIR,  label: "Pending Approval" },
    { folder: APPROVED_DIR, label: "Approved" },
    { folder: DONE_DIR,     label: "Done (sent)" },
    { folder: REJECTED_DIR, label: "Rejected" },
  ];

  const seen = new Set();

  for (const { folder, label } of statusFolders) {
    if (!existsSync(folder)) continue;
    for (const file of readdirSync(folder)) {
      if (!file.startsWith("email_draft_") || !file.endsWith(".md")) continue;
      if (seen.has(file)) continue;
      seen.add(file);

      const filePath = join(folder, file);
      const fields   = parseDraftFile(filePath) || {};

      results.push({
        filename:  file,
        location:  label,
        to:        fields.to      || "(unknown)",
        subject:   fields.subject || "(unknown)",
        priority:  fields.priority || "medium",
        status:    fields.status   || "unknown",
        created:   fields.created  || "(unknown)",
      });
    }
  }

  if (results.length === 0) {
    return { count: 0, drafts: [], message: "No email drafts found." };
  }

  const formatted = results.map(
    (r, i) =>
      `${i + 1}. [${r.status.toUpperCase()}] ${r.filename}\n` +
      `   To: ${r.to} | Subject: ${r.subject} | Location: ${r.location}`
  ).join("\n");

  return {
    count: results.length,
    drafts: results,
    formatted,
    message: `Found ${results.length} email draft(s).`,
  };
}


// ---------------------------------------------------------------------------
// MCP Server Setup
// ---------------------------------------------------------------------------

const server = new Server(
  {
    name:    "email-mcp-server",
    version: "1.0.0",
  },
  {
    capabilities: { tools: {} },
  }
);

// --- List Tools ---
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "draft_email",
      description:
        "Compose and save an email draft. Saves to /Plans and /Pending Approval. " +
        "HITL approval required before sending. Use send_email after approve_draft.",
      inputSchema: {
        type: "object",
        properties: {
          to:      { type: "string",  description: "Recipient email address (required)" },
          subject: { type: "string",  description: "Email subject line (required)" },
          body:    { type: "string",  description: "Full email body text (required)" },
          cc:      { type: "string",  description: "CC recipients (optional, comma-separated)" },
          bcc:     { type: "string",  description: "BCC recipients (optional, comma-separated)" },
          notes:   { type: "string",  description: "Internal notes for reviewer (optional)" },
        },
        required: ["to", "subject", "body"],
      },
    },
    {
      name: "approve_draft",
      description:
        "HITL approval step. Moves a draft from /Pending Approval to /Approved. " +
        "Must be called before send_email. Simulates human reviewer approval.",
      inputSchema: {
        type: "object",
        properties: {
          filename: {
            type: "string",
            description: "Draft filename (e.g. email_draft_2024-02-19_Hello.md). Use list_email_drafts to find it.",
          },
        },
        required: ["filename"],
      },
    },
    {
      name: "reject_draft",
      description:
        "HITL rejection step. Moves a draft from /Pending Approval to /Rejected. " +
        "The draft will not be sent.",
      inputSchema: {
        type: "object",
        properties: {
          filename: {
            type: "string",
            description: "Draft filename to reject.",
          },
          reason: {
            type: "string",
            description: "Reason for rejection (optional, shown in log).",
          },
        },
        required: ["filename"],
      },
    },
    {
      name: "send_email",
      description:
        "Send an approved email draft via Gmail API. " +
        "HITL GATE: The draft MUST be in /Approved first (call approve_draft). " +
        "Moves sent draft to /Done.",
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
      name: "list_email_drafts",
      description:
        "List all email drafts across all folders (Plans, Pending Approval, Approved, Done, Rejected) " +
        "with their current status.",
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
      case "draft_email":
        result = await draftEmail(args);
        break;
      case "approve_draft":
        result = approveDraft(args);
        break;
      case "reject_draft":
        result = rejectDraft(args);
        break;
      case "send_email":
        result = await sendEmail(args);
        break;
      case "list_email_drafts":
        result = listEmailDrafts();
        break;
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
    content: [
      {
        type: "text",
        text: JSON.stringify(result, null, 2),
      },
    ],
  };
});


// ---------------------------------------------------------------------------
// Entry Point
// ---------------------------------------------------------------------------

async function main() {
  // Handle --auth flag for one-time OAuth setup
  if (process.argv.includes("--auth")) {
    await runAuthFlow();
    return;
  }

  const transport = new StdioServerTransport();
  await server.connect(transport);

  // Log to stderr only (stdout is reserved for MCP JSON-RPC)
  process.stderr.write("[email-mcp] Server started. Listening on stdio.\n");
  process.stderr.write(`[email-mcp] Base directory: ${BASE_DIR}\n`);
  process.stderr.write("[email-mcp] Tools: draft_email | send_email | approve_draft | reject_draft | list_email_drafts\n");
}

main().catch((err) => {
  process.stderr.write(`[email-mcp] Fatal error: ${err.message}\n`);
  process.exit(1);
});
