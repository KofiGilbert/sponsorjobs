// The bindings and settings the Worker runs with. Plain `vars` come from wrangler.jsonc (always
// strings); ANTHROPIC_API_KEY is a secret set with `wrangler secret put` and never committed.
export interface Env {
  DB: D1Database;
  ANTHROPIC_API_KEY?: string;
  REGISTER_PER_IP_PER_DAY?: string;
  FREE_POOL_DAILY_TOKENS?: string;
  TAILOR_FREE_MODEL?: string;
  TAILOR_PASS_MODEL?: string;
  // "1" lets an X-Tailor-User header stand in for a bearer token. Local dev only: in production
  // anyone could type any user id into that header and spend someone else's plan.
  DEV_HEADER_AUTH?: string;
}

/** A numeric var with a fallback, so a missing or mistyped value never turns into NaN limits. */
export function intVar(value: string | undefined, fallback: number): number {
  const n = Number.parseInt(value ?? "", 10);
  return Number.isFinite(n) ? n : fallback;
}
