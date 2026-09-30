// Reminds an agent, once per session and agent, that the Gmail tool it is about to call reads the account
// linked to its AI harness, which is often not the pipeline's mailbox (AGENTS.md, "Read the pipeline's
// mailbox, never a harness mailbox"). It is a reminder, not a lock: the first Gmail call, and every call
// in the same batch of parallel calls, is denied with the reason; after a minute the tool goes through the
// normal permission flow, and any error here lets the call through.
//
//   PreToolUse (matcher [Gg]mail)        remind
//   SessionStart (compact|resume)        --forget-session: remind again after a compaction or resume
//
// PIPELINE_MAILBOX_GUARD_NOW_MS replaces the clock, for tests. No dependencies.

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const WINDOW_MS = 60_000;
const KEEP_MS = 7 * 24 * 60 * 60 * 1000;

const REASON = [
  "This Gmail tool reads the account linked to your AI harness, which is often not the pipeline's mailbox.",
  "Outreach replies, interview mail and bounces live in the pipeline mailbox: the account the app's Gmail connection is signed into.",
  "Read that one with `py -3 scripts/pipeline_mailbox.py whoami`, then `search \"<gmail query>\"` or `thread <id>` (python3 on macOS/Linux), from the checkout where the app runs.",
  "Replies carry the student's reply label; whoami shows its search form and whether `label:` alone can be trusted (both counts 0).",
  "If the script fails, say so and ask rather than searching another mailbox.",
  "Mail that is not outreach (for example LinkedIn job alerts for `pipeline.py import-emails`) can be in either account: ask the person which account holds it.",
  "Never report that nothing was found without naming the mailbox you searched.",
  "Call this tool again only when the person explicitly asked about their AI-linked Gmail account.",
  "Gmail tools stay held for a minute after this reminder; it appears once per session and agent.",
].join(" ");

const HELD = (seconds) => [
  `Gmail tools stay held for ${seconds} more ${seconds === 1 ? "second" : "seconds"} after the mailbox reminder, so every call in a parallel batch sees it.`,
  "If the person explicitly asked about their AI-linked Gmail account, call it again after that;",
  "otherwise read the pipeline mailbox with scripts/pipeline_mailbox.py.",
].join(" ");

const sha = (value) => crypto.createHash("sha256").update(String(value)).digest("hex");
const root = () => path.join(os.tmpdir(), "opportunity-pipeline-mailbox-guard");
const sessionDir = (input) => path.join(root(), sha(input.session_id ?? "unknown").slice(0, 16));

function readInput() {
  try {
    const value = JSON.parse(fs.readFileSync(0, "utf8"));
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

// File times are the real clock's, so this ignores the test clock.
function prune() {
  try {
    for (const name of fs.readdirSync(root())) {
      const entry = path.join(root(), name);
      if (Date.now() - fs.statSync(entry).mtimeMs > KEEP_MS) fs.rmSync(entry, { recursive: true, force: true });
    }
  } catch {
    // a reminder is not worth failing over
  }
}

function deny(reason = REASON) {
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: { hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: reason },
  }));
}

function remind(input) {
  if (!/gmail/i.test(String(input.tool_name ?? ""))) return;
  const now = Number(process.env.PIPELINE_MAILBOX_GUARD_NOW_MS) || Date.now();
  prune();
  const dir = sessionDir(input);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, input.agent_id ? sha(input.agent_id).slice(0, 16) : "main");
  try {
    // "wx" is the atomic part: of parallel calls, one creates the file, the rest find it.
    fs.writeFileSync(file, String(now), { flag: "wx" });
    deny();
    return;
  } catch (error) {
    if (error.code !== "EEXIST") throw error;
  }
  const written = Number.parseInt(fs.readFileSync(file, "utf8"), 10);
  const first = Number.isFinite(written) ? written : fs.statSync(file).mtimeMs;
  if (now - first < WINDOW_MS) deny(HELD(Math.max(1, Math.ceil((first + WINDOW_MS - now) / 1000))));
}

try {
  const input = readInput();
  if (process.argv.includes("--forget-session")) fs.rmSync(sessionDir(input), { recursive: true, force: true });
  else remind(input);
} catch {
  // fail open
}
process.exit(0);
