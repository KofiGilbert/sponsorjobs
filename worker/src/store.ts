// D1 access for the meter, ported from backend/store_sqlite.py.
//
// Every counter change is ONE SQL statement (an upsert, or a guarded UPDATE ... RETURNING), never
// read-then-write in JavaScript: two requests from the same account can run on different
// isolates at the same moment, and a read-modify-write would lose one of the updates.

import { AccountState, DEFAULT_PLAN, EMPTY_STATE, PLANS } from "./metering";

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

/** Store a pass on an account (the same contract as UsageStore.set_pass). Phase 1 has no way to
 * buy one yet; tests and an operator use this directly. */
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
