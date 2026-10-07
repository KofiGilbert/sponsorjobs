// The SponsorJobs managed-AI broker on Cloudflare Workers + D1, phase 1.
//
// A parallel implementation of backend/broker.py for the routes the app needs to run on the
// bundled AI: anonymous accounts, metered completions, packages and usage. The Python broker
// stays the reference (dev + tests); where this file differs it says why. Phase-2 routes the app
// may call (billing checkout, sign-in, live interviews, Telegram) answer like the Python broker
// does when that feature is not configured, so the app degrades the same way.
//
// Privacy, as in the Python broker: nothing here stores prompts, resumes or replies. D1 holds
// only account ids, token hashes and usage counts.

import { allowRegistration, emailOf, identify, register } from "./accounts";
import { complete } from "./anthropic";
import { type Env, intVar } from "./env";
import {
  dayOf, isPass, modelFor, packagesLeft, passActive, periodOf, planFor, QuotaExceeded, requireLlm,
  status, tierFor,
} from "./metering";
import { loadState, recordLlm, takeFreePackage, takePassPackage } from "./store";

const json = (data: unknown, status = 200): Response =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

const noUser = () => json({ error: "no user" }, 401);

/** Flask's get_json(silent=True) or {}: a missing, malformed or non-object body is just empty. */
async function body(req: Request): Promise<Record<string, unknown>> {
  try {
    const b = await req.json();
    return b && typeof b === "object" && !Array.isArray(b) ? (b as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

/** Python's int(x or 0) for the max_tokens field. A value Python's int() would refuse throws, and
 * the caller turns that into the same 502 the Python broker answers with. */
function pyInt(v: unknown): number {
  if (!v) return 0;
  if (typeof v === "boolean") return v ? 1 : 0;
  if (typeof v === "number" && Number.isFinite(v)) return Math.trunc(v);
  if (typeof v === "string" && /^\s*[+-]?\d+\s*$/.test(v)) return Number.parseInt(v, 10);
  throw new TypeError("max_tokens is not an integer");
}

interface Ctx { req: Request; env: Env; now: number /* unix seconds */; nowMs: number }
type Handler = (c: Ctx) => Promise<Response> | Response;

// ---- phase 1 ---------------------------------------------------------------------------------- //

const health: Handler = ({ env }) =>
  // Reports whether the LLM is REAL, not just whether the process is alive: the app only uses a
  // broker that says so. Without the key this Worker has no model at all (there is no fake).
  json({ ok: true, real_providers: Boolean(env.ANTHROPIC_API_KEY) });

const accountRegister: Handler = async ({ req, env, nowMs }) => {
  // On Cloudflare the edge sets CF-Connecting-IP itself, so a client cannot spoof it.
  const ip = req.headers.get("CF-Connecting-IP") || "unknown";
  const limit = intVar(env.REGISTER_PER_IP_PER_DAY, 50);   // a campus shares one IP; 5 locked out a dorm
  if (!(await allowRegistration(env.DB, ip, dayOf(nowMs), limit))) {
    return json({ error: "too many new accounts from this network today; try again tomorrow",
                  reason: "register_limit" }, 429);
  }
  return json(await register(env.DB));
};

const accountMe: Handler = async ({ req, env }) => {
  const user = await identify(req, env);
  if (!user) return noUser();
  return json({ account_id: user, email: await emailOf(env.DB, user) });
};

const meUsage: Handler = async ({ req, env, now, nowMs }) => {
  const user = await identify(req, env);
  if (!user) return noUser();
  const { state } = await loadState(env.DB, user, periodOf(nowMs), dayOf(nowMs));
  const st = status(state, now, env);
  return json({ plan: st.tier, ...st });
};

const llmComplete: Handler = async ({ req, env, now, nowMs }) => {
  const user = await identify(req, env);
  if (!user) return noUser();
  const period = periodOf(nowMs);
  const day = dayOf(nowMs);
  const b = await body(req);
  const prompt = b.prompt == null ? "" : String(b.prompt);
  const system = b.system == null ? "" : String(b.system);
  const messages = Array.isArray(b.messages) ? b.messages : null;
  // Estimate the input up front (~1 token / 4 chars) so one oversized prompt or history cannot
  // blow past a capped plan on the company key; the plain check only sees usage AFTER the spend.
  let estChars = prompt.length + system.length;
  if (messages) {
    for (const m of messages) {
      if (m && typeof m === "object" && !Array.isArray(m)) {
        const c = (m as Record<string, unknown>).content;
        estChars += typeof c === "string" ? c.length : c == null ? 0 : JSON.stringify(c).length;
      }
    }
  }
  const { state, freePoolTokens } = await loadState(env.DB, user, period, day);
  try {
    requireLlm(state, now, Math.max(1, Math.floor(estChars / 4)));
  } catch (e) {
    if (e instanceof QuotaExceeded) {
      return json({ error: e.message, reason: e.reason, tier: tierFor(state, now) }, 402);
    }
    throw e;
  }
  const plan = planFor(state, now);
  const free = !isPass(plan);
  // The whole free tier shares a daily token budget; past it free requests get a polite "busy"
  // and paid users are untouched. Keeps a script of anonymous accounts from running up the bill.
  if (free && freePoolTokens >= intVar(env.FREE_POOL_DAILY_TOKENS, 5_000_000)) {
    return json({ error: "the free AI is very busy today; try again tomorrow, or get a pass",
                  reason: "free_busy", tier: tierFor(state, now) }, 503);
  }
  const model = modelFor(plan, env);
  if (!env.ANTHROPIC_API_KEY) {
    // The Python broker would fall back to its FakeProvider here; a hosted broker must never
    // serve placeholder text, so say plainly that the model is not configured.
    return json({ error: "the AI service is not configured" }, 503);
  }
  let out;
  try {
    out = await complete(env.ANTHROPIC_API_KEY, {
      model, prompt, system, messages,
      maxTokens: pyInt(b.max_tokens) || null,
      effort: b.effort,
    });
  } catch (err) {
    // Log before flattening: every upstream failure collapses into the same opaque 502 for the
    // client, and without this line the only way to find the cause is to rebuild the call by
    // hand. The log never includes the key; the client gets no detail at all.
    console.error(`llm/complete failed for model ${model}:`, err instanceof Error ? err.message : err);
    return json({ error: "the AI service is unavailable" }, 502);
  }
  const spent = out.input_tokens + out.output_tokens;
  const used = await recordLlm(env.DB, user, period, spent, free ? day : null);
  const cap = plan.llmTokenCap;
  return json({ text: out.text, model, tokens: { used, cap, over_cap: cap !== null && used > cap } });
};

const llmPackage: Handler = async ({ req, env, now, nowMs }) => {
  // Called ONCE at the start of a tailoring run on the bundled AI; one run is one package.
  const user = await identify(req, env);
  if (!user) return noUser();
  const period = periodOf(nowMs);
  const { state } = await loadState(env.DB, user, period, dayOf(nowMs));
  const active = passActive(state, now);
  const taken = active ? await takePassPackage(env.DB, user, now) : await takeFreePackage(env.DB, user, period);
  if (taken === null) {
    const error = active ? "this pass's tailored packages are used up"
                         : "the free plan's 3 tailored packages this month are used up";
    return json({ error, reason: "package_limit", tier: tierFor(state, now), packages_left: 0 }, 402);
  }
  const after = active ? { ...state, passPackages: taken } : { ...state, packagesUsed: taken };
  return json({ model: modelFor(planFor(state, now), env), packages_left: packagesLeft(after, now),
                tier: tierFor(state, now) });
};

// ---- phase 2: answered as the Python broker answers when the feature is not configured -------- //

const off = (error: string, status = 503): Handler => () => json({ error }, status);
const billingOff = off("billing is not configured");

/** /billing/offers with billing off: nothing on sale, but where the caller stands (Python does
 * the same), so the app's Upgrade panel still shows the current tier. */
const billingOffers: Handler = async ({ req, env, now, nowMs }) => {
  const user = await identify(req, env);
  let current = null;
  if (user) {
    const { state } = await loadState(env.DB, user, periodOf(nowMs), dayOf(nowMs));
    const st = status(state, now, env);
    current = { tier: st.tier, pass_until: st.pass_until, interviews_left: st.interviews_left,
                packages_left: st.packages_left };
  }
  return json({ passes: [], packs: [], current });
};

// ---- routing ---------------------------------------------------------------------------------- //

type Route = [method: string, pattern: RegExp, handler: Handler];

const ROUTES: Route[] = [
  ["GET", /^\/health$/, health],
  ["POST", /^\/account\/register$/, accountRegister],
  ["GET", /^\/account\/me$/, accountMe],
  ["POST", /^\/llm\/complete$/, llmComplete],
  ["POST", /^\/llm\/package$/, llmPackage],
  ["GET", /^\/me\/usage$/, meUsage],

  ["GET", /^\/billing\/offers$/, billingOffers],
  ["GET", /^\/billing\/packs$/, () => json({ packs: [] })],
  ["POST", /^\/billing\/passes\/[^/]+\/checkout$/, billingOff],
  ["POST", /^\/billing\/packs\/[^/]+\/checkout$/, billingOff],
  ["POST", /^\/billing\/webhook$/, billingOff],
  // The free-pass dev stubs are off whenever the broker is hosted, as in Python.
  ["POST", /^\/billing\/(plan|credits)$/, off("not available", 403)],
  ["POST", /^\/account\/(signup|login|claim)$/, off("email accounts are not configured")],
  ["GET", /^\/account\/google\/(start|callback)$/, off("google sign-in is not configured")],
  ["POST", /^\/avatar\/(session\/start|heartbeat)$/, off("live interviews are not configured")],
  ["GET", /^\/notify\/telegram\/(status|inbox)$/, off("telegram_not_configured")],
  ["POST", /^\/notify\/telegram\/(link|unlink|send|send_media|edit)$/, off("telegram_not_configured")],
  ["POST", /^\/telegram\/webhook$/, off("telegram_not_configured")],
];

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const path = new URL(req.url).pathname;
    const nowMs = Date.now();
    const matches = ROUTES.filter(([, pattern]) => pattern.test(path));
    if (matches.length === 0) return json({ error: "not found" }, 404);
    const method = req.method === "HEAD" ? "GET" : req.method;
    const route = matches.find(([m]) => m === method);
    if (!route) return json({ error: "method not allowed" }, 405);
    try {
      return await route[2]({ req, env, now: Math.floor(nowMs / 1000), nowMs });
    } catch (err) {
      // A bug or a D1 hiccup: answer JSON the app can read, never a stack trace.
      console.error(`${req.method} ${path} failed:`, err instanceof Error ? err.stack ?? err.message : err);
      return json({ error: "internal error" }, 500);
    }
  },
} satisfies ExportedHandler<Env>;
