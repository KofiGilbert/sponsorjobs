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
  // Live interviews on the company Tavus account. The key is a secret (`wrangler secret put
  // TAVUS_API_KEY`); without it the avatar routes answer "not configured", as before.
  TAVUS_API_KEY?: string;
  TAVUS_FACE_ID?: string;   // the interviewer's face; defaults to the stock one the app uses
  TAVUS_PAL_ID?: string;    // optional: a PAL created once on the company account
  // Stripe billing (passes and extra-interview packs), the same names backend/server.py reads.
  // All set with `wrangler secret put`. Without a secret key AND at least one pass price, every
  // /billing/* route answers exactly as when billing is off ("billing is not configured").
  STRIPE_SECRET_KEY?: string;       // live key (sk_live_...); wins over the test key
  STRIPE_TEST_SECRET_KEY?: string;  // sandbox key (sk_test_...), used only when no live key is set
  STRIPE_WEBHOOK_SECRET?: string;   // whsec_...: proves a /billing/webhook call came from Stripe
  STRIPE_PRICE_PASS30?: string;     // price_... ids; an unset one is simply not offered
  STRIPE_PRICE_PASS90?: string;
  STRIPE_PRICE_PACK_1?: string;
  STRIPE_PRICE_PACK_3?: string;
  STRIPE_PRICE_PACK_5?: string;
  // Where Stripe sends the person back after paying (+ "/?checkout=success|cancel"). Defaults to
  // the desktop app's local address, as in backend/server.py.
  TAILOR_APP_URL?: string;
}

/** A numeric var with a fallback, so a missing or mistyped value never turns into NaN limits. */
export function intVar(value: string | undefined, fallback: number): number {
  const n = Number.parseInt(value ?? "", 10);
  return Number.isFinite(n) ? n : fallback;
}
