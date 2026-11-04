// The admin dashboard's few write actions. Everything else on admin.sponsorjobs.ai is read-only.
//
// Every action is a POST from the dashboard's own pages (same origin, checked), carries a typed
// reason, and writes one admin_actions row in the same batch as the change, so nothing changes
// without a record of who, what, to whom and why. The two that hurt if pressed by mistake
// (suspending an account, pausing the free AI) also need a typed confirmation word.
//
//   give_pass        start or extend a Job Hunt / Season Pass (the purchase path's grantPassOnce)
//   add_interviews   add 1 to 10 live interviews as never-expiring credit (addCreditOnce)
//   suspend          the account's tokens stop resolving (signed out everywhere) until restored
//   unsuspend        restore it; the same tokens work again
//   set_free_pool    the free tier's shared daily token limit: the deploy's value, a number, or 0 (paused)
//   test_alert       one Telegram message to the owner (src/outage.ts), logged with its result
//   reveal_email     showing a person's full e-mail on their page is logged too
//
// Refunds and disputes stay in Stripe: the dashboard links there and never moves money.

import type { Env } from "./env";
import { INTERVIEW_SECONDS, PASSES } from "./metering";
import { sendTestAlert } from "./outage";
import { addCreditOnce, FREE_POOL_CAP_KEY, grantPassOnce } from "./store";

export const REASON_MAX = 300;
export const MAX_INTERVIEWS = 10;
export const MAX_POOL_TOKENS = 100_000_000;

/** A POST counts only when the browser says it came from this host (see admin.ts). */
export function sameOrigin(req: Request): boolean {
  const url = new URL(req.url);
  return req.headers.get("origin") === url.origin || req.headers.get("sec-fetch-site") === "same-origin";
}

export async function formOf(req: Request): Promise<URLSearchParams> {
  const text = await req.text();
  return new URLSearchParams(text.length > 4000 ? "" : text);
}

export class ActionError extends Error {}

function reasonOf(f: URLSearchParams): string {
  const r = (f.get("reason") ?? "").replace(/\s+/g, " ").trim();
  if (r.length < 3) throw new ActionError("Type a short reason first.");
  return r.slice(0, REASON_MAX);
}

const audit = (db: D1Database, now: number, admin: string, action: string, target: string, detail: string, reason: string) =>
  db.prepare("INSERT INTO admin_actions(at, admin, action, target, detail, reason) VALUES(?, ?, ?, ?, ?, ?)")
    .bind(now, admin, action, target, detail, reason);
/** The audit row for the statement just before it in the same batch, written only when that
 * statement changed something (a batch is one transaction, so changes() is that statement's). */
const auditIfChanged = (db: D1Database, now: number, admin: string, action: string, target: string, reason: string) =>
  db.prepare("INSERT INTO admin_actions(at, admin, action, target, detail, reason) SELECT ?, ?, ?, ?, '', ? WHERE changes() > 0")
    .bind(now, admin, action, target, reason);

async function mustExist(db: D1Database, id: string): Promise<void> {
  const row = await db.prepare("SELECT 1 AS ok FROM accounts WHERE account_id = ?").bind(id).first();
  if (!row) throw new ActionError("No account with that id.");
}

/** Run one account action; returns the line shown on the person's page afterwards. */
export async function accountAction(env: Env, admin: string, id: string, action: string, f: URLSearchParams,
                                    now: number): Promise<string> {
  const db = env.DB;
  await mustExist(db, id);
  if (action === "give-pass") {
    const pass = f.get("pass") ?? "";
    if (!PASSES.includes(pass)) throw new ActionError("Pick a pass.");
    const reason = reasonOf(f);
    const ok = await grantPassOnce(db, `admin:${crypto.randomUUID()}`, id, pass, now);
    if (!ok) throw new ActionError("The pass was not applied. Try again.");
    await audit(db, now, admin, "give_pass", id, pass, reason).run();
    return pass === "pass90" ? "Season Pass given." : "Job Hunt Pass given.";
  }
  if (action === "add-interviews") {
    const n = Number.parseInt(f.get("interviews") ?? "", 10);
    if (!Number.isInteger(n) || n < 1 || n > MAX_INTERVIEWS) throw new ActionError(`Pick 1 to ${MAX_INTERVIEWS} interviews.`);
    const reason = reasonOf(f);
    const ok = await addCreditOnce(db, `admin:${crypto.randomUUID()}`, id, n * INTERVIEW_SECONDS, now);
    if (!ok) throw new ActionError("The interviews were not added. Try again.");
    await audit(db, now, admin, "add_interviews", id, `${n} interview${n === 1 ? "" : "s"}`, reason).run();
    return `${n} interview${n === 1 ? "" : "s"} added.`;
  }
  if (action === "suspend") {
    const reason = reasonOf(f);
    if ((f.get("confirm") ?? "").trim() !== "SUSPEND") throw new ActionError('Type SUSPEND to confirm.');
    const [upd] = await db.batch([
      db.prepare("UPDATE accounts SET suspended_at = ? WHERE account_id = ? AND suspended_at IS NULL").bind(now, id),
      auditIfChanged(db, now, admin, "suspend", id, reason),
    ]);
    if (!(upd!.meta.changes ?? 0)) throw new ActionError("That account is already suspended.");
    return "Account suspended. It is signed out everywhere until you restore it.";
  }
  if (action === "unsuspend") {
    const reason = reasonOf(f);
    const [upd] = await db.batch([
      db.prepare("UPDATE accounts SET suspended_at = NULL WHERE account_id = ? AND suspended_at IS NOT NULL").bind(id),
      auditIfChanged(db, now, admin, "unsuspend", id, reason),
    ]);
    if (!(upd!.meta.changes ?? 0)) throw new ActionError("That account is not suspended.");
    return "Account restored.";
  }
  throw new ActionError("Unknown action.");
}

/** Log that the admin chose to see a person's full e-mail. */
export async function logReveal(env: Env, admin: string, id: string, now: number): Promise<void> {
  await mustExist(env.DB, id);
  await audit(env.DB, now, admin, "reveal_email", id, "", "").run();
}

/** The free AI's shared daily limit: mode "default" (the deploy's value), "custom" (a number) or
 * "paused" (0, needs PAUSE typed). */
export async function setFreePool(env: Env, admin: string, f: URLSearchParams, now: number): Promise<string> {
  const db = env.DB;
  const mode = f.get("mode") ?? "";
  const reason = reasonOf(f);
  if (mode === "default") {
    await db.batch([
      db.prepare("DELETE FROM admin_settings WHERE key = ?").bind(FREE_POOL_CAP_KEY),
      audit(db, now, admin, "set_free_pool", "", "default", reason),
    ]);
    return "The free AI limit is back to the deploy's value.";
  }
  let value: number;
  if (mode === "paused") {
    if ((f.get("confirm") ?? "").trim() !== "PAUSE") throw new ActionError("Type PAUSE to confirm.");
    value = 0;
  } else if (mode === "custom") {
    value = Number.parseInt((f.get("tokens") ?? "").replace(/[,_\s]/g, ""), 10);
    if (!Number.isInteger(value) || value < 1 || value > MAX_POOL_TOKENS) {
      throw new ActionError(`Enter a daily limit from 1 to ${MAX_POOL_TOKENS.toLocaleString("en-US")} tokens.`);
    }
  } else {
    throw new ActionError("Pick a setting.");
  }
  await db.batch([
    db.prepare("INSERT INTO admin_settings(key, value, updated_at, updated_by) VALUES(?, ?, ?, ?) " +
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, updated_by = excluded.updated_by")
      .bind(FREE_POOL_CAP_KEY, String(value), now, admin),
    audit(db, now, admin, "set_free_pool", "", value === 0 ? "paused" : String(value), reason),
  ]);
  return value === 0 ? "The free AI is paused. Pass holders are not affected." : "The free AI limit is changed.";
}

/** The Health page's test alert, logged with what happened. */
export async function testAlert(env: Env, admin: string, now: number): Promise<string> {
  const result = await sendTestAlert(env, now);
  await audit(env.DB, now, admin, "test_alert", "", result, "").run();
  return result;
}
