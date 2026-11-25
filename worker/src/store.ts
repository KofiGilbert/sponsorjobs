// D1 access for the meter, ported from backend/store_sqlite.py.
//
// Every counter change is ONE SQL statement (an upsert, or a guarded UPDATE ... RETURNING), never
// read-then-write in JavaScript: two requests from the same account can run on different
// isolates at the same moment, and a read-modify-write would lose one of the updates.

import { AccountState, DAY_SECONDS, DEFAULT_PLAN, EMPTY_STATE, ONE_TIME_PASSES, PASSES, PLANS, SUBSCRIPTION_PLANS } from "./metering";

export const FREE_POOL_USER = "__free_pool__"; // the free tier's shared daily budget lives here

interface UserRow { plan: string; pass_until: number; pass_seconds: number; pass_packages: number; credit_seconds: number }
interface UsageRow { llm_tokens: number; packages: number; recorded_interviews?: number }

/** The admin dashboard's free-pool override (admin_settings "free_pool_cap"): a token count, or
 * null for the deploy's FREE_POOL_DAILY_TOKENS. 0 pauses the free AI. */
export const FREE_POOL_CAP_KEY = "free_pool_cap";
export function poolCapOverride(raw: string | null | undefined): number | null {
  if (raw === null || raw === undefined || raw === "") return null;
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

/** Everything one request needs about an account, plus today's free-pool spend and the pool's
 * override, in one batch (one round trip to D1 instead of four). */
export async function loadState(db: D1Database, user: string, period: string, day: string):
    Promise<{ state: AccountState; freePoolTokens: number; freePoolCap: number | null }> {
  const [u, use, pool, cap] = await db.batch([
    db.prepare("SELECT plan, pass_until, pass_seconds, pass_packages, credit_seconds FROM users WHERE user = ?")
      .bind(user),
    db.prepare("SELECT llm_tokens, packages, recorded_interviews FROM usage WHERE user = ? AND period = ?").bind(user, period),
    db.prepare("SELECT llm_tokens, packages FROM usage WHERE user = ? AND period = ?").bind(FREE_POOL_USER, day),
    db.prepare("SELECT value FROM admin_settings WHERE key = ?").bind(FREE_POOL_CAP_KEY),
  ]);
  const ur = (u!.results[0] as UserRow | undefined);
  const us = (use!.results[0] as UsageRow | undefined);
  const pr = (pool!.results[0] as UsageRow | undefined);
  const state: AccountState = ur
    ? { plan: ur.plan, passUntil: ur.pass_until, passSeconds: ur.pass_seconds,
        passPackages: ur.pass_packages, creditSeconds: ur.credit_seconds,
        llmTokens: us?.llm_tokens ?? 0, packagesUsed: us?.packages ?? 0,
        recordedUsed: us?.recorded_interviews ?? 0 }
    : { ...EMPTY_STATE, llmTokens: us?.llm_tokens ?? 0, packagesUsed: us?.packages ?? 0,
        recordedUsed: us?.recorded_interviews ?? 0 };
  const capRow = (cap!.results[0] as { value: string } | undefined);
  return { state, freePoolTokens: pr?.llm_tokens ?? 0, freePoolCap: poolCapOverride(capRow?.value) };
}

/** Add the tokens a completion spent; returns the account's new monthly total. The free pool is
 * charged in the same batch when the caller is on the free tier. */
export async function recordLlm(db: D1Database, user: string, period: string, tokens: number,
                                poolDay: string | null): Promise<number> {
  const add = (who: string, per: string) => db.prepare(
    "INSERT INTO usage(user, period, llm_tokens) VALUES(?, ?, ?) " +
    "ON CONFLICT(user, period) DO UPDATE SET llm_tokens = llm_tokens + excluded.llm_tokens " +
    "RETURNING llm_tokens").bind(who, per, tokens);
  const stmts = [add(user, period)];
  if (poolDay) stmts.push(add(FREE_POOL_USER, poolDay));
  const [mine] = await db.batch<{ llm_tokens: number }>(stmts);
  return mine!.results[0]!.llm_tokens;
}

/** Take one package from the active pass's balance. The WHERE clause is the allowance check, so
 * the check and the decrement are one atomic step. Returns the balance left, or null when none
 * was left (or the pass ended in the meantime). */
export async function takePassPackage(db: D1Database, user: string, now: number,
                                      period?: string): Promise<number | null> {
  const row = await db.prepare(
    "UPDATE users SET pass_packages = pass_packages - 1 " +
    "WHERE user = ? AND pass_packages > 0 AND pass_until > ? RETURNING pass_packages")
    .bind(user, now).first<{ pass_packages: number }>();
  if (!row) return null;
  // Reporting only (the admin dashboard's "packages this month"); never part of the allowance.
  if (period) {
    await db.prepare(
      "INSERT INTO usage(user, period, pass_packages) VALUES(?, ?, 1) " +
      "ON CONFLICT(user, period) DO UPDATE SET pass_packages = pass_packages + 1")
      .bind(user, period).run();
  }
  return row.pass_packages;
}

/** Count one free package this month, unless the free allowance is already used up (the
 * conflict UPDATE is skipped then and RETURNING yields no row). Returns packages used, or null. */
export async function takeFreePackage(db: D1Database, user: string, period: string): Promise<number | null> {
  const allowance = PLANS[DEFAULT_PLAN]!.packages;
  const row = await db.prepare(
    "INSERT INTO usage(user, period, packages) VALUES(?, ?, 1) " +
    "ON CONFLICT(user, period) DO UPDATE SET packages = packages + 1 WHERE packages < ? " +
    "RETURNING packages")
    .bind(user, period, allowance).first<{ packages: number }>();
  return row ? row.packages : null;
}

/** Count one scored recorded practice interview on the free plan this month, unless the free
 * allowance is already used up (the conflict UPDATE is skipped then and RETURNING yields no row).
 * One statement, so two scores landing at once cannot both take the last one. Returns how many
 * are used this month, or null when none was left. */
export async function takeFreeRecorded(db: D1Database, user: string, period: string): Promise<number | null> {
  const allowance = PLANS[DEFAULT_PLAN]!.recordedInterviews ?? 0;
  if (allowance <= 0) return null;
  const row = await db.prepare(
    "INSERT INTO usage(user, period, recorded_interviews) VALUES(?, ?, 1) " +
    "ON CONFLICT(user, period) DO UPDATE SET recorded_interviews = recorded_interviews + 1 " +
    "WHERE recorded_interviews < ? RETURNING recorded_interviews")
    .bind(user, period, allowance).first<{ recorded_interviews: number }>();
  return row ? row.recorded_interviews : null;
}

/** Store a pass on an account (the same contract as UsageStore.set_pass). A purchase goes through
 * grantPassOnce; tests and an operator use this directly. */
export async function setPass(db: D1Database, user: string, name: string, until: number,
                              seconds: number, packages: number): Promise<void> {
  await db.prepare(
    "INSERT INTO users(user, plan, pass_until, pass_seconds, pass_packages) " +
    "VALUES(?, ?, ?, MAX(0, ?), MAX(0, ?)) ON CONFLICT(user) DO UPDATE SET " +
    "plan = excluded.plan, pass_until = excluded.pass_until, " +
    "pass_seconds = excluded.pass_seconds, pass_packages = excluded.pass_packages")
    .bind(user, name, Math.trunc(until), Math.trunc(seconds), Math.trunc(packages)).run();
}

/** Add tokens to a monthly counter without a model call (tests and an operator use this). */
export async function addLlmTokens(db: D1Database, user: string, period: string, tokens: number): Promise<void> {
  await recordLlm(db, user, period, tokens, null);
}

/** Deduct up to `seconds` of live-interview time, the active pass first, then purchased credits,
 * never below zero, and return the seconds LEFT afterwards. One statement: in an UPDATE every SET
 * expression reads the row's OLD values, so the pass share and the credit share come from the same
 * snapshot, atomically, even if two heartbeats land on different isolates at once. */
export async function consumeAvatar(db: D1Database, user: string, seconds: number,
                                    now: number): Promise<number> {
  const take = Math.max(0, Math.floor(seconds));
  const fromPass = "(CASE WHEN pass_until > ?2 THEN MIN(?1, MAX(pass_seconds, 0)) ELSE 0 END)";
  const row = await db.prepare(
    `UPDATE users SET
       pass_seconds = pass_seconds - ${fromPass},
       credit_seconds = credit_seconds - MIN(?1 - ${fromPass}, MAX(credit_seconds, 0))
     WHERE user = ?3
     RETURNING pass_seconds, credit_seconds, pass_until`,
  ).bind(take, now, user).first<{ pass_seconds: number; credit_seconds: number; pass_until: number }>();
  if (!row) return 0;                                  // no account row: no balance at all
  return (row.pass_until > now ? Math.max(0, row.pass_seconds) : 0) + Math.max(0, row.credit_seconds);
}

/** Remember which account started a conversation, so only that account can end it and read it. */
export async function recordAvatarSession(db: D1Database, id: string, user: string, now: number) {
  await db.prepare("INSERT OR REPLACE INTO avatar_sessions(conversation_id, user, started) VALUES(?, ?, ?)")
    .bind(id, user, now).run();
}

export async function avatarSessionOwner(db: D1Database, id: string): Promise<string | null> {
  const r = await db.prepare("SELECT user FROM avatar_sessions WHERE conversation_id = ?").bind(id)
    .first<{ user: string }>();
  return r?.user ?? null;
}

export async function markAvatarEnded(db: D1Database, id: string, now: number) {
  await db.prepare("UPDATE avatar_sessions SET ended = ? WHERE conversation_id = ? AND ended = 0")
    .bind(now, id).run();
}

/** Reserve up to `seconds` of charge against one interview, never past `cap` in total, and return
 * how many seconds may actually be charged. Only the account that started the interview counts. */
export async function chargeableSeconds(db: D1Database, id: string, user: string, seconds: number,
                                        cap: number): Promise<number> {
  const row = await db.prepare(
    `UPDATE avatar_sessions SET used = MIN(?1, used + ?2)
     WHERE conversation_id = ?3 AND user = ?4
     RETURNING used`,
  ).bind(cap, Math.max(0, Math.floor(seconds)), id, user).first<{ used: number }>();
  return row ? row.used : -1;
}

export async function sessionUsed(db: D1Database, id: string, user: string): Promise<number> {
  const r = await db.prepare("SELECT used FROM avatar_sessions WHERE conversation_id = ? AND user = ?")
    .bind(id, user).first<{ used: number }>();
  return r ? r.used : -1;
}

// ---- Stripe purchases (backend/billing.py Billing._apply_payment + Meter.grant_pass) ----------- //
//
// Each grant is ONE D1 batch (a transaction): the grant is guarded by "this key is not in
// processed_events yet" and the key is recorded in the same batch. Stripe redelivering an event,
// or two deliveries landing on different isolates at once, therefore grants exactly once.

const notProcessed = "NOT EXISTS (SELECT 1 FROM processed_events WHERE key = ?1)";
const markProcessed = (db: D1Database, key: string, now: number) =>
  db.prepare("INSERT OR IGNORE INTO processed_events(key, at) VALUES(?, ?)").bind(key, now);

/** Start or extend a pass, once per `key`; returns false when the key was already applied.
 * Same rules as Meter.grant_pass: while a pass is active the end date moves out by the new pass's
 * days and its allowances are ADDED (stacking; the longer pass's name is kept as the label);
 * otherwise a fresh period starts now (leftovers of an expired pass are not revived). */
export async function grantPassOnce(db: D1Database, key: string, user: string, passName: string,
                                    now: number): Promise<boolean> {
  const plan = PLANS[passName];
  if (!plan || plan.days <= 0) throw new Error(`unknown pass: ${passName}`);
  const passList = PASSES.map((n) => `'${n}'`).join(", ");      // our own constants, not input
  const curDays = `(CASE users.plan ${PASSES.map((n) => `WHEN '${n}' THEN ${PLANS[n]!.days}`).join(" ")} ELSE 0 END)`;
  const active = `(users.plan IN (${passList}) AND users.pass_until > ?4)`;
  const [grant] = await db.batch([
    db.prepare(
      `INSERT INTO users(user, plan, pass_until, pass_seconds, pass_packages)
       SELECT ?2, ?3, ?4 + ?5, ?6, ?7 WHERE ${notProcessed}
       ON CONFLICT(user) DO UPDATE SET
         plan = CASE WHEN ${active} AND ${curDays} >= ?8 THEN users.plan ELSE excluded.plan END,
         pass_until = CASE WHEN ${active} THEN users.pass_until + ?5 ELSE excluded.pass_until END,
         pass_seconds = CASE WHEN ${active} THEN MAX(0, users.pass_seconds + ?6) ELSE ?6 END,
         pass_packages = CASE WHEN ${active} THEN MAX(0, users.pass_packages + ?7) ELSE ?7 END`,
    ).bind(key, user, passName, now, plan.days * DAY_SECONDS, plan.avatarSecondsIncluded,
           plan.packages, plan.days),
    markProcessed(db, key, now),
  ]);
  return (grant!.meta.changes ?? 0) > 0;
}

/** Add purchased interview seconds (a pack), once per `key`; they never expire. Returns false when
 * the key was already applied. */
export async function addCreditOnce(db: D1Database, key: string, user: string, seconds: number,
                                    now: number): Promise<boolean> {
  const [grant] = await db.batch([
    db.prepare(
      `INSERT INTO users(user, credit_seconds) SELECT ?2, MAX(0, ?3) WHERE ${notProcessed}
       ON CONFLICT(user) DO UPDATE SET credit_seconds = MAX(0, users.credit_seconds + ?3)`,
    ).bind(key, user, Math.trunc(seconds)),
    markProcessed(db, key, now),
  ]);
  return (grant!.meta.changes ?? 0) > 0;
}

/** Start the paid period of a renewing plan, once per `key` (the Stripe invoice id); returns false
 * when the key was already applied. The period ends at `periodEnd` (Stripe's period end for that
 * invoice) and its allowances are the plan's, refilled: nothing rolls over from the last period.
 * Two exceptions, both in the same single statement:
 *   - a one-time pass still running is not lost: its remaining interviews and packages are added
 *     to the first period, and access runs to whichever ends later;
 *   - an invoice for a period that is not newer than the one already applied (a late delivery)
 *     changes nothing, so an old event can never shorten or refill a newer period. */
export async function grantSubscriptionPeriodOnce(db: D1Database, key: string, user: string, planName: string,
                                                  periodEnd: number, now: number): Promise<boolean> {
  const plan = PLANS[planName];
  if (!plan || !SUBSCRIPTION_PLANS.includes(planName)) throw new Error(`unknown plan: ${planName}`);
  const subs = SUBSCRIPTION_PLANS.map((n) => `'${n}'`).join(", ");     // our own constants, not input
  const passes = ONE_TIME_PASSES.map((n) => `'${n}'`).join(", ");
  const stale = `(users.plan IN (${subs}) AND users.pass_until >= ?4)`;
  const carry = `(users.plan IN (${passes}) AND users.pass_until > ?7)`;
  const [grant] = await db.batch([
    db.prepare(
      `INSERT INTO users(user, plan, pass_until, pass_seconds, pass_packages)
       SELECT ?2, ?3, ?4, ?5, ?6 WHERE ${notProcessed}
       ON CONFLICT(user) DO UPDATE SET
         plan = CASE WHEN ${stale} THEN users.plan ELSE excluded.plan END,
         pass_until = CASE WHEN ${stale} THEN users.pass_until
                           WHEN ${carry} THEN MAX(users.pass_until, ?4) ELSE ?4 END,
         pass_seconds = CASE WHEN ${stale} THEN users.pass_seconds
                             WHEN ${carry} THEN MAX(0, users.pass_seconds) + ?5 ELSE ?5 END,
         pass_packages = CASE WHEN ${stale} THEN users.pass_packages
                              WHEN ${carry} THEN MAX(0, users.pass_packages) + ?6 ELSE ?6 END`,
    ).bind(key, user, planName, Math.trunc(periodEnd), plan.avatarSecondsIncluded, plan.packages, now),
    markProcessed(db, key, now),
  ]);
  return (grant!.meta.changes ?? 0) > 0;
}

/** End a renewing plan's access now (a subscription that was cancelled immediately, or ended
 * unpaid), unless the account has another live subscription. A period that already ended is left
 * as it is. */
export async function endSubscriptionAccess(db: D1Database, user: string, subId: string, now: number): Promise<void> {
  const subs = SUBSCRIPTION_PLANS.map((n) => `'${n}'`).join(", ");
  await db.prepare(
    `UPDATE users SET pass_until = MIN(pass_until, ?1) WHERE user = ?2 AND plan IN (${subs})
     AND NOT EXISTS (SELECT 1 FROM subscriptions WHERE user = ?2 AND id != ?3
                     AND status IN ('active', 'trialing', 'past_due'))`,
  ).bind(now, user, subId).run();
}
