// When the company AI key stops working, say so honestly and tell the owner at once.
//
// On 2026-11-01 the company Anthropic organization ran out of credits. Every model call failed,
// the app told every person "this one needs you", and nobody was alerted. This module is the fix
// on the broker side:
//
//   * a failed model call is classified (src/anthropic.ts classifyUpstream) as "company" (our
//     key: no credit, disabled, invalid) or "transient" (rate limit, overload, a 5xx), and the
//     app gets {error: "ai_unavailable", reason, retry_after?} with a 503. The upstream text is
//     never in that answer; the person is never told to buy anything for a failure that is ours;
//   * the state lives in one D1 row (migrations/0010_ai_status.sql), so every isolate agrees on
//     it and /health can say "company_outage since <time>";
//   * on the first "company" failure the owner gets ONE Telegram message through the official bot
//     with the cause and the billing link, then at most one an hour while it lasts, and one
//     "back" message when a call goes through again. The alert slot is claimed with a guarded
//     UPDATE, so parallel isolates cannot each send one. Every outage is also logged.

import type { Outage } from "./anthropic";
import type { Env } from "./env";
import { botFrom, linkedChatId, sendText } from "./telegram";

export const ALERT_INTERVAL = 3600;            // seconds between owner reminders while it lasts
export const BILLING_URL = "https://console.anthropic.com/settings/billing";
export const USER_FACING = "SponsorJobs' AI is temporarily unavailable";   // what people see

export type AiState = "ok" | "company_outage" | "transient";

export interface AiStatus {
  state: AiState;
  since: number;               // unix seconds the current state began (0 = never recorded)
  last_seen: number;           // unix seconds of the last failure, or of the recovery
  last_alert_at: number | null;
  note: string;                // the short cause; for the owner and the admin page only
}

const OK: AiStatus = { state: "ok", since: 0, last_seen: 0, last_alert_at: null, note: "" };

const iso = (s: number) => new Date(s * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");

export async function aiStatus(db: D1Database): Promise<AiStatus> {
  const row = await db.prepare("SELECT state, since, last_seen, last_alert_at, note FROM ai_status WHERE key = 'ai'")
    .first<AiStatus>();
  return row ?? OK;
}

/** The one-line form /health and the admin page show: "ok" | "company_outage since <time>" |
 * "transient". */
export function describeAi(s: AiStatus): string {
  if (s.state === "company_outage") return `company_outage since ${iso(s.since)}`;
  return s.state;
}

// Per isolate: once a call has gone through and the row said "ok", a success skips the read for
// OK_RECHECK seconds. Not forever: an outage recorded by ANOTHER isolate never reaches this one, so
// an isolate that had seen "ok" before the outage would never clear it after the fix (2026-11-10:
// credits topped up, calls going through, /health still said company_outage).
export const OK_RECHECK = 60;
let knownOkAt: number | null = null;

/** Who to tell: the OWNER_TELEGRAM_CHAT_ID secret, else the first ADMIN_EMAILS address's linked
 * chat. null when neither resolves (the log is then the only alert). */
export async function ownerChatId(env: Env): Promise<number | null> {
  const fixed = Number.parseInt((env.OWNER_TELEGRAM_CHAT_ID ?? "").trim(), 10);
  if (Number.isFinite(fixed) && fixed !== 0) return fixed;         // a group chat id is negative
  const first = (env.ADMIN_EMAILS ?? "").split(",").map((e) => e.trim().toLowerCase()).filter(Boolean)[0];
  if (!first) return null;
  const row = await env.DB.prepare("SELECT account_id FROM accounts WHERE lower(email) = ?")
    .bind(first).first<{ account_id: string }>();
  return row ? linkedChatId(env.DB, row.account_id) : null;
}

export function alertText(note: string, since: number, reminder: boolean): string {
  return (reminder ? "SponsorJobs AI is STILL down" : "SponsorJobs AI outage") +
    ": every model call on the company Anthropic key is failing.\n" +
    `Cause: ${note || "unknown"}\n` +
    `Since: ${iso(since)}\n` +
    `People see "${USER_FACING}". Passes and packages are not affected.\n` +
    `Check billing: ${BILLING_URL}\n` +
    "You get one message an hour while it lasts, and one when it is back.";
}

export function recoveredText(since: number, now: number): string {
  const minutes = Math.max(1, Math.round((now - since) / 60));
  return `SponsorJobs AI is back: model calls are going through again (down ${minutes} min, since ${iso(since)}).`;
}

async function tell(env: Env, text: string): Promise<string> {
  const bot = botFrom(env);
  if (!bot) return "telegram not configured";
  const chat = await ownerChatId(env);
  if (chat === null) return "no owner chat (set OWNER_TELEGRAM_CHAT_ID or link the first admin's Telegram)";
  try {
    await sendText(bot, chat, text);
    return "sent";
  } catch (err) {
    return `telegram failed: ${err instanceof Error ? err.message : String(err)}`;
  }
}

// The admin dashboard's "Send test alert" button: the same path a real alert takes (the bot, the
// owner chat, sendMessage), so "sent" proves an outage would reach the owner. One per 30 seconds
// per isolate, so a double click is one message.
export const TEST_ALERT_TEXT =
  "SponsorJobs test alert: outage alerts reach you here. Nothing is wrong; this was sent from the admin dashboard.";
let lastTestAt = 0;

/** Send the test alert. Returns "sent", or why it was not sent (the same strings as a real alert). */
export async function sendTestAlert(env: Env, now: number): Promise<string> {
  if (now - lastTestAt < 30) return "a test was sent a moment ago; wait 30 seconds";
  const result = await tell(env, TEST_ALERT_TEXT);
  if (result === "sent") lastTestAt = now;
  return result;
}

/** Record a failed model call. For a company failure, alert the owner once an hour. */
export async function recordOutage(env: Env, outage: Outage, now: number): Promise<void> {
  knownOkAt = null;
  const state: AiState = outage.reason === "company" ? "company_outage" : "transient";
  // One write: a new state starts a new "since"; the same state keeps it; a transient blip while
  // the key is dead does not clear the outage (only a success does).
  await env.DB.prepare(
    "INSERT INTO ai_status(key, state, since, last_seen, last_alert_at, note) VALUES('ai', ?1, ?2, ?2, NULL, ?3) " +
    "ON CONFLICT(key) DO UPDATE SET " +
    "  state = CASE WHEN ai_status.state = 'company_outage' AND excluded.state = 'transient' THEN ai_status.state ELSE excluded.state END, " +
    "  since = CASE WHEN ai_status.state = excluded.state OR (ai_status.state = 'company_outage' AND excluded.state = 'transient') " +
    "          THEN ai_status.since ELSE excluded.since END, " +
    "  last_seen = excluded.last_seen, " +
    "  note = CASE WHEN ai_status.state = 'company_outage' AND excluded.state = 'transient' THEN ai_status.note ELSE excluded.note END")
    .bind(state, now, outage.note).run();
  if (outage.reason !== "company") {
    console.warn(`AI transient failure: ${outage.note}`);
    return;
  }
  // Claim the alert slot: exactly one isolate gets the row back when it is free.
  const claimed = await env.DB.prepare(
    "UPDATE ai_status SET last_alert_at = ?1 WHERE key = 'ai' AND (last_alert_at IS NULL OR last_alert_at <= ?1 - ?2) " +
    "RETURNING since, (?1 - COALESCE(since, ?1)) >= ?2 AS reminder")
    .bind(now, ALERT_INTERVAL).first<{ since: number; reminder: number }>();
  if (!claimed) {
    console.error(`AI OUTAGE (company): ${outage.note}; owner already alerted this hour`);
    return;
  }
  const result = await tell(env, alertText(outage.note, claimed.since, Boolean(claimed.reminder)));
  console.error(`AI OUTAGE (company): ${outage.note}; owner alert: ${result}; billing: ${BILLING_URL}`);
}

/** Record that a model call went through. Clears an outage, and tells the owner the AI is back
 * when they had been alerted. Costs nothing once this isolate has seen "ok". */
export async function recordOk(env: Env, now: number): Promise<void> {
  if (knownOkAt !== null && now - knownOkAt < OK_RECHECK) return;
  const before = await aiStatus(env.DB);
  if (before.state === "ok") { knownOkAt = now; return; }
  // Only the isolate whose UPDATE flips the state sends the "back" message.
  const flipped = await env.DB.prepare(
    "UPDATE ai_status SET state = 'ok', since = ?1, last_seen = ?1, note = '' WHERE key = 'ai' AND state = ?2 RETURNING key")
    .bind(now, before.state).first();
  knownOkAt = now;
  if (!flipped) return;
  if (before.state === "company_outage" && before.last_alert_at !== null) {
    const result = await tell(env, recoveredText(before.since, now));
    console.error(`AI back after company outage since ${iso(before.since)}; owner told: ${result}`);
  } else {
    console.warn(`AI back after ${before.state} since ${iso(before.since)}`);
  }
}

/** Test hook: forget the per-isolate "known ok" so the next success reads the row again. */
export function resetOutageCache(): void { knownOkAt = null; }
