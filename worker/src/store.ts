// D1 access for the meter, ported from backend/store_sqlite.py.
//
// Every counter change is ONE SQL statement (an upsert, or a guarded UPDATE ... RETURNING), never
// read-then-write in JavaScript: two requests from the same account can run on different
// isolates at the same moment, and a read-modify-write would lose one of the updates.

import { AccountState, DAY_SECONDS, DEFAULT_PLAN, EMPTY_STATE, PASSES, PLANS } from "./metering";

export const FREE_POOL_USER = "__free_pool__"; // the free tier's shared daily budget lives here

interface UserRow { plan: string; pass_until: number; pass_seconds: number; pass_packages: number; credit_seconds: number }
interface UsageRow { llm_tokens: number; packages: number }

/** Everything one request needs about an account, plus today's free-pool spend, in one batch
 * (one round trip to D1 instead of three). */
export async function loadState(db: D1Database, user: string, period: string, day: string):
    Promise<{ state: AccountState; freePoolTokens: number }> {
  const [u, use, pool] = await db.batch([
    db.prepare("SELECT plan, pass_until, pass_seconds, pass_packages, credit_seconds FROM users WHERE user = ?")
      .bind(user),
    db.prepare("SELECT llm_tokens, packages FROM usage WHERE user = ? AND period = ?").bind(user, period),
    db.prepare("SELECT llm_tokens, packages FROM usage WHERE user = ? AND period = ?").bind(FREE_POOL_USER, day),
  ]);
  const ur = (u!.results[0] as UserRow | undefined);
  const us = (use!.results[0] as UsageRow | undefined);
  const pr = (pool!.results[0] as UsageRow | undefined);
  const state: AccountState = ur
    ? { plan: ur.plan, passUntil: ur.pass_until, passSeconds: ur.pass_seconds,
        passPackages: ur.pass_packages, creditSeconds: ur.credit_seconds,
        llmTokens: us?.llm_tokens ?? 0, packagesUsed: us?.packages ?? 0 }
    : { ...EMPTY_STATE, llmTokens: us?.llm_tokens ?? 0, packagesUsed: us?.packages ?? 0 };
  return { state, freePoolTokens: pr?.llm_tokens ?? 0 };
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
export async function takePassPackage(db: D1Database, user: string, now: number): Promise<number | null> {
  const row = await db.prepare(
    "UPDATE users SET pass_packages = pass_packages - 1 " +
    "WHERE user = ? AND pass_packages > 0 AND pass_until > ? RETURNING pass_packages")
    .bind(user, now).first<{ pass_packages: number }>();
  return row ? row.pass_packages : null;
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
