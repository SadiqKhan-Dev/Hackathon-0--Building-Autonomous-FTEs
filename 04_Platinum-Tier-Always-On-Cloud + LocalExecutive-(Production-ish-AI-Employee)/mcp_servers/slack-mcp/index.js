#!/usr/bin/env node
/**
 * AI Employee Gold Tier -- Slack MCP Server
 * ==========================================
 *
 * A Model Context Protocol (MCP) server exposing Slack messaging capabilities
 * to Claude via tool-use, with HITL (Human-in-the-Loop) approval gates.
 *
 * TOOLS EXPOSED:
 *   draft_slack_message    -- compose Slack message, save to /Plans + /Pending Approval
 *   approve_slack_draft    -- HITL: move draft to /Approved
 *   reject_slack_draft     -- HITL: move draft to /Rejected
 *   send_slack_message     -- send via Slack API only after HITL approval
 *   read_slack_channel     -- read recent messages from a Slack channel
 *   list_slack_drafts      -- list all Slack drafts and their approval status
 *
 * HITL FLOW:
 *   draft_slack_message  ->  /Plans + /Pending Approval  (status: pending_approval)
 *       |
 *   Human reviews /Pending Approval/slack_draft_*.md
 *       |
 *   approve_slack_draft  ->  /Approved  (status: approved)
 *       |
 *   send_slack_message   ->  Slack message sent + /Done  (status: sent)
 *
 * INSTALL:
 *   cd mcp_servers/slack-mcp
 *   npm install
 *
 * CREDENTIALS SETUP:
 *   1. Go to https://api.slack.com/apps -> Create New App -> From Scratch
 *   2. Name the app (e.g. "AI Employee") and select your workspace
 *   3. Go to "OAuth & Permissions" -> "Bot Token Scopes" and add:
 *        chat:write          (send messages)
 *        channels:read       (list public channels)
 *        channels:history    (read public channel messages)
 *        groups:read         (list private channels)
 *        groups:history      (read private channel messages)
 *        im:read             (read DMs)
 *        im:history          (read DM messages)
 *        users:read          (resolve user names)
 *   4. Go to "Install App" -> "Install to Workspace" -> Authorize
 *   5. Copy the "Bot User OAuth Token" (starts with xoxb-)
 *   6. Create a .env file in the project root:
 *        SLACK_TOKEN=xoxb-your-token-here
 *   7. Invite the bot to any channel you want it to post in:
 *        /invite @YourAppName
 *
 * RUN:
 *   node mcp_servers/slack-mcp/index.js
 *
 * RUN WITH PM2:
 *   pm2 start mcp_servers/slack-mcp/index.js --name slack-mcp --interpreter node
 *   pm2 save && pm2 startup
 *
 * PM2 MANAGEMENT:
 *   pm2 logs slack-mcp       # Live logs
 *   pm2 restart slack-mcp    # Restart
 *   pm2 stop slack-mcp       # Stop
 *
 * TEST:
 *   Ask Claude: "Draft a Slack message to #general: 'Team standup in 5 minutes'"
 *   Check /Plans and /Pending Approval for the saved draft.
 *   Then: "Approve slack_draft_[date]_general.md"
 *   Then: "Send the approved Slack draft"
 */

import { Server }               from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

import { WebClient } from "@slack/web-api";
import { config }    from "dotenv";

import {
  existsSync, mkdirSync, readFileSync, writeFileSync,
  readdirSync, renameSync, copyFileSync,
} from "fs";
import { join, dirname, basename } from "path";
import { fileURLToPath }          from "url";


// ---------------------------------------------------------------------------
// Paths + env
// ---------------------------------------------------------------------------

const __dirname = dirname(fileURLToPath(import.meta.url));
const BASE_DIR  = join(__dirname, "..", "..");

// Load .env from project root (SLACK_TOKEN lives there)
config({ path: join(BASE_DIR, ".env") });

const PLANS_DIR    = join(BASE_DIR, "Plans");
const PENDING_DIR  = join(BASE_DIR, "Pending Approval");
const APPROVED_DIR = join(BASE_DIR, "Approved");
const REJECTED_DIR = join(BASE_DIR, "Rejected");
const DONE_DIR     = join(BASE_DIR, "Done");
const HANDBOOK_FILE = join(BASE_DIR, "Company Handbook.md");

// Max messages returned by read_slack_channel
const DEFAULT_READ_LIMIT = 20;


// ---------------------------------------------------------------------------
// Slack WebClient
// ---------------------------------------------------------------------------

let _slackClient = null;

function getSlackClient() {
  if (_slackClient) return _slackClient;

  const token = process.env.SLACK_TOKEN;
  if (!token) {
    throw new Error(
      "SLACK_TOKEN not set.\n" +
      "Create .env in the project root with:\n" +
      "  SLACK_TOKEN=xoxb-your-bot-token\n" +
      "See CREDENTIALS SETUP in the file header for full instructions."
    );
  }

  _slackClient = new WebClient(token);
  return _slackClient;
}


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
  return (str || "general")
    .replace(/^#/, "")           // strip leading # from channel names
    .replace(/[/\\?%*:|"<>@]/g, "_")
    .replace(/\s+/g, "_")
    .slice(0, 50)
    .trim();
}

function uniquePath(dir, filename) {
  let target   = join(dir, filename);
  const stem   = filename.replace(/\.md$/, "");
  let counter  = 1;
  while (existsSync(target)) {
    target = join(dir, `${stem}_${counter}.md`);
    counter++;
  }
  return target;
}

/** Normalise channel: strip # prefix, lower-case. */
function normalizeChannel(channel) {
  return (channel || "general").replace(/^#/, "").toLowerCase().trim();
}

/** Parse YAML frontmatter fields from a .md draft file. */
function parseDraftFile(filePath) {
  try {
    const content    = readFileSync(filePath, "utf8");
    const yamlBlock  = content.match(/^---\n([\s\S]*?)\n---/);
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

/** Add or update a `slack_message_ts:` field in YAML frontmatter. */
function writeMessageTs(filePath, ts) {
  try {
    let content = readFileSync(filePath, "utf8");
    if (/^slack_message_ts:/m.test(content)) {
      content = content.replace(/^slack_message_ts:\s*.+$/m, `slack_message_ts: "${ts}"`);
    } else {
      content = content.replace(
        /^status:/m,
        `slack_message_ts: "${ts}"\nstatus:`
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

/** Resolve a channel name to a Slack channel ID (needed for API calls). */
async function resolveChannelId(client, channelName) {
  const normalized = normalizeChannel(channelName);

  // Try public channels
  try {
    const result = await client.conversations.list({ types: "public_channel,private_channel", limit: 1000 });
    const found  = (result.channels || []).find(
      (c) => c.name === normalized || c.id === normalized
    );
    if (found) return { id: found.id, name: found.name };
  } catch (_) { /* fall through */ }

  // If the input looks like a raw channel ID (starts with C), try it directly
  if (/^[CG]/.test(channelName)) {
    return { id: channelName, name: channelName };
  }

  throw new Error(
    `Channel "${channelName}" not found. Make sure:\n` +
    "  - The channel exists in your Slack workspace\n" +
    "  - The bot has been invited: /invite @YourAppName\n" +
    "  - The SLACK_TOKEN has channels:read scope"
  );
}


// ---------------------------------------------------------------------------
// Tool: draft_slack_message
// ---------------------------------------------------------------------------

async function draftSlackMessage({
  channel,
  message,
  thread_ts = "",
  notes     = "",
}) {
  ensureDir(PLANS_DIR);
  ensureDir(PENDING_DIR);

  const handbook    = loadHandbook();
  const date        = dateStr();
  const safeChannel = sanitizeFilename(channel);
  const filename    = `slack_draft_${date}_${safeChannel}.md`;

  const planPath    = uniquePath(PLANS_DIR,   filename);
  const pendingPath = uniquePath(PENDING_DIR, filename);
  const finalName   = basename(planPath);

  // Handbook: detect payments > $500 in message
  const paymentFlag =
    /\$\s?(\d[\d,]*\.?\d*)/.test(message) &&
    (() => {
      const m = message.match(/\$\s?(\d[\d,]*\.?\d*)/g) || [];
      return m.some((v) => parseFloat(v.replace(/[$,]/g, "")) > 500);
    })();

  const priority = paymentFlag ? "high" : "medium";
  const flagNote = paymentFlag
    ? "HANDBOOK FLAG: Payment > $500 detected -- HITL required."
    : "No special handbook flags.";

  const normalizedChannel = "#" + normalizeChannel(channel);
  const charCount = message.length;
  const threadNote = thread_ts
    ? `Thread reply (parent ts: ${thread_ts})`
    : "New message (not a thread reply)";

  const content = `---
type: slack_draft
channel: "${normalizedChannel}"
channel_raw: "${channel}"
thread_ts: "${thread_ts}"
char_count: ${charCount}
priority: ${priority}
status: pending_approval
created: "${isoNow()}"
slack_message_ts: ""
draft_file: "${finalName}"
payment_flag: ${paymentFlag}
---

# Slack Message Draft -- ${date}

| Field         | Value |
|---------------|-------|
| Channel       | ${normalizedChannel} |
| Thread        | ${threadNote} |
| Char Count    | ${charCount} |
| Priority      | ${priority.toUpperCase()} |
| Status        | pending_approval |
| Created       | ${isoNow()} |

## Message Body

\`\`\`
${message}
\`\`\`

## Handbook Check

- Rules applied: ${handbook.replace(/\n/g, " | ")}
- ${flagNote}

## Notes (Internal)

${notes || "(none)"}

## HITL Approval Checklist

- [ ] Verify message tone is appropriate and polite
- [ ] Confirm the correct channel: ${normalizedChannel}
- [ ] Check message does not expose sensitive information
- [ ] Approve any payment references > $500
- [ ] Move to /Approved to authorise sending

## Approval Flow

1. Review this file in /Pending Approval/
2. Run: approve_slack_draft("${finalName}")  -- to approve
3. Run: reject_slack_draft("${finalName}")   -- to reject
4. Run: send_slack_message("${finalName}")   -- after approval to post to Slack
`;

  writeFileSync(planPath, content, "utf8");
  copyFileSync(planPath, pendingPath);

  return {
    success:         true,
    filename:        finalName,
    plan_path:       `Plans/${finalName}`,
    pending_path:    `Pending Approval/${finalName}`,
    channel:         normalizedChannel,
    char_count:      charCount,
    priority,
    payment_flagged: paymentFlag,
    message:
      `Slack message draft saved. File is in /Pending Approval awaiting HITL review.\n` +
      `Next: approve_slack_draft("${finalName}") then send_slack_message("${finalName}")`,
  };
}


// ---------------------------------------------------------------------------
// Tool: approve_slack_draft
// ---------------------------------------------------------------------------

function approveSlackDraft({ filename }) {
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
        "Use list_slack_drafts() to see available drafts.",
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
    message:       `Draft approved. Now call send_slack_message("${filename}") to post to Slack.`,
  };
}


// ---------------------------------------------------------------------------
// Tool: reject_slack_draft
// ---------------------------------------------------------------------------

function rejectSlackDraft({ filename, reason = "Rejected by reviewer" }) {
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
// Tool: send_slack_message
// ---------------------------------------------------------------------------

async function sendSlackMessage({ filename }) {
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
        `  2. Approve: approve_slack_draft("${filename}")\n` +
        `  3. Send:    send_slack_message("${filename}")`,
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
          `Approve first: approve_slack_draft("${filename}")`,
      };
    }
    return {
      success: false,
      message: `Draft file not found: ${filename}. Use list_slack_drafts() to find it.`,
    };
  }

  // Parse draft
  const fields = parseDraftFile(approvedPath);
  if (!fields) {
    return { success: false, message: `Could not parse YAML frontmatter in ${filename}` };
  }

  const { channel_raw, channel, thread_ts } = fields;
  const targetChannel = channel_raw || channel;
  if (!targetChannel) {
    return { success: false, message: "Draft is missing channel field." };
  }

  // Extract message body from draft (between the ``` fences)
  let messageBody = "";
  try {
    const raw = readFileSync(approvedPath, "utf8");
    const match = raw.match(/## Message Body\s*\n+```\n([\s\S]*?)```/);
    if (match) {
      messageBody = match[1].trim();
    }
  } catch (_) { /* handled below */ }

  if (!messageBody) {
    return { success: false, message: "Could not extract message body from draft file." };
  }

  // Get Slack client
  let client;
  try {
    client = getSlackClient();
  } catch (err) {
    return { success: false, message: `Slack auth failed: ${err.message}` };
  }

  // Resolve channel name to ID
  let channelId;
  try {
    const resolved = await resolveChannelId(client, targetChannel);
    channelId = resolved.id;
  } catch (err) {
    return { success: false, message: `Channel resolution failed: ${err.message}` };
  }

  // Build Slack API call params
  const postParams = {
    channel: channelId,
    text:    messageBody,
  };

  // Attach to thread if thread_ts is set
  if (thread_ts && thread_ts !== '""' && thread_ts !== "") {
    postParams.thread_ts = thread_ts.replace(/"/g, "");
  }

  // Call Slack API
  let postedMessage;
  try {
    const response = await client.chat.postMessage(postParams);
    postedMessage  = response;
  } catch (err) {
    return {
      success: false,
      message: `Slack API error: ${err.message}`,
    };
  }

  const messageTs  = postedMessage.ts;
  const messageUrl =
    `https://slack.com/archives/${channelId}/p${messageTs.replace(".", "")}`;

  // Archive to /Done
  ensureDir(DONE_DIR);
  const donePath = join(DONE_DIR, filename);
  renameSync(approvedPath, donePath);
  updateDraftStatus(donePath, "sent");
  writeMessageTs(donePath, messageTs);

  const planPath = join(PLANS_DIR, filename);
  if (existsSync(planPath)) {
    updateDraftStatus(planPath, "sent");
    writeMessageTs(planPath, messageTs);
  }

  return {
    success:      true,
    filename,
    channel:      targetChannel,
    message_ts:   messageTs,
    message_url:  messageUrl,
    done_path:    `Done/${filename}`,
    message:
      `Slack message sent successfully to ${targetChannel}.\n` +
      `Timestamp: ${messageTs}\n` +
      `Draft archived to /Done.`,
  };
}


// ---------------------------------------------------------------------------
// Tool: read_slack_channel
// ---------------------------------------------------------------------------

async function readSlackChannel({ channel, limit = DEFAULT_READ_LIMIT, oldest = "" }) {
  let client;
  try {
    client = getSlackClient();
  } catch (err) {
    return { success: false, message: `Slack auth failed: ${err.message}` };
  }

  let channelId;
  let channelName;
  try {
    const resolved = await resolveChannelId(client, channel);
    channelId   = resolved.id;
    channelName = resolved.name;
  } catch (err) {
    return { success: false, message: `Channel resolution failed: ${err.message}` };
  }

  // Fetch history
  const historyParams = {
    channel: channelId,
    limit:   Math.min(Math.max(1, parseInt(limit) || DEFAULT_READ_LIMIT), 100),
  };
  if (oldest) historyParams.oldest = oldest;

  let history;
  try {
    history = await client.conversations.history(historyParams);
  } catch (err) {
    return { success: false, message: `Slack API error reading channel: ${err.message}` };
  }

  const messages = (history.messages || []).map((msg) => ({
    ts:      msg.ts,
    user:    msg.user || msg.bot_id || "(unknown)",
    text:    msg.text || "",
    subtype: msg.subtype || "message",
    thread:  msg.thread_ts !== msg.ts ? msg.thread_ts : null,
    reply_count: msg.reply_count || 0,
  }));

  // Try to resolve user IDs to display names
  const userIds = [...new Set(messages.map((m) => m.user).filter((u) => u && u.startsWith("U")))];
  const userMap = {};
  for (const uid of userIds) {
    try {
      const info = await client.users.info({ user: uid });
      userMap[uid] = info.user?.display_name || info.user?.real_name || uid;
    } catch (_) {
      userMap[uid] = uid;
    }
  }

  const formatted = messages.map((msg, i) => {
    const userName = userMap[msg.user] || msg.user;
    const date     = new Date(parseFloat(msg.ts) * 1000).toISOString().replace("T", " ").slice(0, 19);
    const thread   = msg.thread ? ` [thread reply]` : "";
    const replies  = msg.reply_count > 0 ? ` [${msg.reply_count} replies]` : "";
    return `${i + 1}. [${date}] ${userName}${thread}${replies}:\n   ${msg.text}`;
  }).join("\n\n");

  return {
    success:      true,
    channel:      `#${channelName}`,
    channel_id:   channelId,
    count:        messages.length,
    messages,
    formatted,
    message:      `Read ${messages.length} message(s) from #${channelName}.`,
  };
}


// ---------------------------------------------------------------------------
// Tool: list_slack_drafts
// ---------------------------------------------------------------------------

function listSlackDrafts() {
  const statusFolders = [
    { folder: PLANS_DIR,    label: "Plans (draft)" },
    { folder: PENDING_DIR,  label: "Pending Approval" },
    { folder: APPROVED_DIR, label: "Approved" },
    { folder: DONE_DIR,     label: "Done (sent)" },
    { folder: REJECTED_DIR, label: "Rejected" },
  ];

  const results = [];
  const seen    = new Set();

  for (const { folder, label } of statusFolders) {
    if (!existsSync(folder)) continue;
    for (const file of readdirSync(folder)) {
      if (!file.startsWith("slack_draft_") || !file.endsWith(".md")) continue;
      if (seen.has(file)) continue;
      seen.add(file);

      const fields = parseDraftFile(join(folder, file)) || {};
      results.push({
        filename:    file,
        location:    label,
        channel:     fields.channel    || "(unknown)",
        char_count:  fields.char_count || "?",
        priority:    fields.priority   || "medium",
        status:      fields.status     || "unknown",
        message_ts:  fields.slack_message_ts || "",
        created:     fields.created    || "(unknown)",
      });
    }
  }

  if (results.length === 0) {
    return { count: 0, drafts: [], message: "No Slack drafts found." };
  }

  const formatted = results.map(
    (r, i) =>
      `${i + 1}. [${r.status.toUpperCase()}] ${r.filename}\n` +
      `   Channel: ${r.channel} | Chars: ${r.char_count} | Location: ${r.location}` +
      (r.message_ts ? ` | Slack ts: ${r.message_ts}` : "")
  ).join("\n");

  return {
    count:     results.length,
    drafts:    results,
    formatted,
    message:   `Found ${results.length} Slack draft(s).`,
  };
}


// ---------------------------------------------------------------------------
// MCP Server Setup
// ---------------------------------------------------------------------------

const server = new Server(
  { name: "slack-mcp-server", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

// --- List Tools ---
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "draft_slack_message",
      description:
        "Draft a Slack message to a channel. Saves to /Plans and /Pending Approval. " +
        "HITL approval required before sending. Call approve_slack_draft then send_slack_message.",
      inputSchema: {
        type: "object",
        properties: {
          channel: {
            type: "string",
            description: "Slack channel name (with or without #), e.g. '#general' or 'general'. For DMs use the username or user ID. (required)",
          },
          message: {
            type: "string",
            description: "The Slack message text to send. Supports Slack markdown (bold *text*, italic _text_, code `code`). (required)",
          },
          thread_ts: {
            type: "string",
            description: "Optional. Thread timestamp (ts) to reply in a thread, e.g. '1708612345.123456'. Leave blank for a new top-level message.",
          },
          notes: {
            type: "string",
            description: "Internal reviewer notes (not sent to Slack). (optional)",
          },
        },
        required: ["channel", "message"],
      },
    },
    {
      name: "approve_slack_draft",
      description:
        "HITL approval step. Moves a Slack draft from /Pending Approval to /Approved. " +
        "Must be called before send_slack_message.",
      inputSchema: {
        type: "object",
        properties: {
          filename: {
            type: "string",
            description: "Draft filename, e.g. slack_draft_2026-02-22_general.md. Use list_slack_drafts to find it.",
          },
        },
        required: ["filename"],
      },
    },
    {
      name: "reject_slack_draft",
      description:
        "HITL rejection. Moves a Slack draft from /Pending Approval to /Rejected. Message will not be sent.",
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
      name: "send_slack_message",
      description:
        "Send an approved Slack message via the Slack API. " +
        "HITL GATE: Draft MUST be in /Approved first (call approve_slack_draft). " +
        "Moves the sent draft to /Done. Bot must be invited to the target channel.",
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
      name: "read_slack_channel",
      description:
        "Read recent messages from a Slack channel. " +
        "Requires channels:history (public) or groups:history (private) bot scopes.",
      inputSchema: {
        type: "object",
        properties: {
          channel: {
            type: "string",
            description: "Channel name (e.g. '#general') or channel ID (e.g. 'C01234ABCDE'). (required)",
          },
          limit: {
            type: "number",
            description: `Number of messages to retrieve (1-100, default ${DEFAULT_READ_LIMIT}).`,
          },
          oldest: {
            type: "string",
            description: "Only include messages after this Unix timestamp (optional). Use to paginate.",
          },
        },
        required: ["channel"],
      },
    },
    {
      name: "list_slack_drafts",
      description:
        "List all Slack message drafts across all folders (Plans, Pending Approval, Approved, Done, Rejected) with their status.",
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
      case "draft_slack_message":  result = await draftSlackMessage(args);  break;
      case "approve_slack_draft":  result = approveSlackDraft(args);        break;
      case "reject_slack_draft":   result = rejectSlackDraft(args);         break;
      case "send_slack_message":   result = await sendSlackMessage(args);   break;
      case "read_slack_channel":   result = await readSlackChannel(args);   break;
      case "list_slack_drafts":    result = listSlackDrafts();              break;
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
  const transport = new StdioServerTransport();
  await server.connect(transport);

  process.stderr.write("[slack-mcp] Server started. Listening on stdio.\n");
  process.stderr.write(`[slack-mcp] Base directory: ${BASE_DIR}\n`);
  process.stderr.write(`[slack-mcp] SLACK_TOKEN loaded: ${process.env.SLACK_TOKEN ? "YES" : "NO -- set SLACK_TOKEN in .env"}\n`);
  process.stderr.write(
    "[slack-mcp] Tools: draft_slack_message | approve_slack_draft | " +
    "reject_slack_draft | send_slack_message | read_slack_channel | list_slack_drafts\n"
  );
}

main().catch((err) => {
  process.stderr.write(`[slack-mcp] Fatal error: ${err.message}\n`);
  process.exit(1);
});
