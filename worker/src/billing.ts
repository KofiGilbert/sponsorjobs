// Stripe billing for the broker: plans that renew automatically, extra-interview packs, and the
// retired one-time passes, ported from backend/billing.py (the reference) and the /billing/* routes
// in backend/broker.py.
//
// Plans (owner decision 2026-11-20) are Stripe subscriptions (Checkout mode=subscription): Student
// $29 a month or $69 every 3 months, Standard $49 a month or $119 every 3 months. Which price a
// person pays is decided HERE from the stored student check (src/student.ts), never by the client.
// A plan renews until cancelled; src/subscriptions.ts turns Stripe's webhook events into the
// entitlement (paid through the period end) and handles cancel, resume and renewal reminders.
//
// Extra-interview packs stay one-time Checkout Sessions (mode=payment), granted on
// checkout.session.completed once paid, exactly once per session id (processed_events). The old
// one-time passes are granted the same way, so anyone who bought one keeps it until it ends; they
// are only offered while their STRIPE_PRICE_PASS* secrets are still set (for app versions that
// predate the plans).
//
// Stripe is called with plain fetch and form-encoded bodies (no SDK). The secret key lives only in
// the Worker's secrets. Nothing about a resume or history is ever sent to Stripe: only the account
// id, the buyer's sign-in e-mail (prefilled on the checkout page) and the product.

import type { Env } from "./env";
import {
  type Interval, interviewsIn, ONE_TIME_PASSES, PACK_PRICE_LABEL, PACKS, packSeconds, PLANS, SUBSCRIPTION_PLANS,
} from "./metering";

const PASS_PRICE_ENV: Record<string, keyof Env> = {
  pass30: "STRIPE_PRICE_PASS30", pass90: "STRIPE_PRICE_PASS90" };
const PACK_PRICE_ENV: Record<string, keyof Env> = {
  pack_1: "STRIPE_PRICE_PACK_1", pack_3: "STRIPE_PRICE_PACK_3", pack_5: "STRIPE_PRICE_PACK_5" };
export const PLAN_PRICE_ENV: Record<string, keyof Env> = {
  student_monthly: "STRIPE_PRICE_STUDENT_MONTHLY", student_quarterly: "STRIPE_PRICE_STUDENT_QUARTERLY",
  standard_monthly: "STRIPE_PRICE_STANDARD_MONTHLY", standard_quarterly: "STRIPE_PRICE_STANDARD_QUARTERLY" };

/** Stripe's default webhook timestamp tolerance (stripe.Webhook.DEFAULT_TOLERANCE). */
export const SIGNATURE_TOLERANCE_SECONDS = 300;

export const STRIPE_API = "https://api.stripe.com/v1";

export interface Billing {
  key: string;
  webhookSecret: string;
  passPrices: Record<string, string>;   // the retired one-time passes, while still configured
  planPrices: Record<string, string>;   // the renewing plans
  packPrices: Record<string, string>;
  successUrl: string;
  cancelUrl: string;
}

/** Billing on only with a secret key AND at least one plan or pass price; otherwise null and every
 * route answers "billing is not configured" (as backend/server.py build_billing). */
export function billingFrom(env: Env): Billing | null {
  const key = env.STRIPE_SECRET_KEY || env.STRIPE_TEST_SECRET_KEY || "";
  const pick = (m: Record<string, keyof Env>): Record<string, string> => Object.fromEntries(
    Object.entries(m).map(([id, name]) => [id, String(env[name] ?? "").trim()]).filter(([, v]) => v));
  const passPrices = pick(PASS_PRICE_ENV);
  const planPrices = pick(PLAN_PRICE_ENV);
  if (!key || Object.keys(passPrices).length + Object.keys(planPrices).length === 0) return null;
  return { key, webhookSecret: env.STRIPE_WEBHOOK_SECRET || "", passPrices, planPrices, packPrices: pick(PACK_PRICE_ENV),
           successUrl: `${appUrl(env)}/?checkout=success`, cancelUrl: `${appUrl(env)}/?checkout=cancel` };
}

/** Where Stripe sends the person back (the desktop app's local address unless configured). */
export const appUrl = (env: Env) => (env.TAILOR_APP_URL || "http://127.0.0.1:57000").replace(/\/+$/, "");

/** The renewing plan a Stripe price id belongs to (reverse of planPrices), or null. */
export function planForPrice(b: Billing, price: string): string | null {
  for (const [plan, id] of Object.entries(b.planPrices)) if (id === price) return plan;
  return null;
}

// ---- test mode: only for named testers --------------------------------------------------------- //
//
// A Stripe TEST key alone (no live key) puts billing in test mode, where Stripe's published test
// card (4242 4242 4242 4242) "pays". Open to everyone, anyone could get a real pass, and real
// live interviews on our Tavus credit, with a fake card. So in test mode billing is visible only
// to the accounts listed in STRIPE_TEST_ACCOUNTS (comma-separated account ids); everyone else
// sees "billing is not configured", exactly as before. The live key never has this limit.

export const testModeOnly = (env: Env): boolean =>
  !env.STRIPE_SECRET_KEY && Boolean(env.STRIPE_TEST_SECRET_KEY);

export function billingFor(env: Env, user: string | null): Billing | null {
  const b = billingFrom(env);
  if (!b || !testModeOnly(env)) return b;
  const testers = String(env.STRIPE_TEST_ACCOUNTS ?? "").split(",").map((x) => x.trim()).filter(Boolean);
  return user && testers.includes(user) ? b : null;
}

// ---- what is on sale ------------------------------------------------------------------------ //

export function passOffer(id: string) {
  const p = PLANS[id]!;
  return { id, price_label: p.priceLabel, days: p.days, interviews: interviewsIn(p),
           packages: p.packages, auto_renew: false };
}

/** A renewing plan as the app shows it. `auto_renew` is always true: the app must show the price,
 * the interval, that it renews until cancelled and how to cancel next to the buy button. */
export function planOffer(id: string) {
  const p = PLANS[id]!;
  return { id, tier: p.tier!, interval: p.interval!, months: p.months!, price_label: p.priceLabel,
           interviews: interviewsIn(p), packages: p.packages, auto_renew: true };
}

/** Every configured renewing plan, Student first, monthly before every 3 months. Both tiers are
 * listed so the app can show both; checkout charges the one the caller qualifies for. */
export const offeredPlans = (b: Billing) => SUBSCRIPTION_PLANS.filter((id) => b.planPrices[id]).map(planOffer);

export function packOffer(id: string) {
  return { id, price_label: PACK_PRICE_LABEL[id], interviews: PACKS[id], seconds: packSeconds(id) };
}

/** The retired one-time passes, listed only while their prices are still configured (older app
 * versions read this list; the current app shows `plans`). */
export const offeredPasses = (b: Billing) => ONE_TIME_PASSES.filter((id) => b.passPrices[id]).map(passOffer);

/** The packs on sale (those with a configured price), smallest first. */
export const offeredPacks = (b: Billing) => Object.keys(PACKS)
  .sort((a, c) => PACKS[a]! - PACKS[c]!).filter((id) => b.packPrices[id]).map(packOffer);

// ---- checkout ------------------------------------------------------------------------------- //

/** Stripe's form encoding: nested keys as a[b][0][c]=v. */
function form(params: Record<string, unknown>, prefix = "", out = new URLSearchParams()): URLSearchParams {
  for (const [k, v] of Object.entries(params)) {
    const name = prefix ? `${prefix}[${k}]` : k;
    if (v === undefined || v === null) continue;
    if (typeof v === "object") form(v as Record<string, unknown>, name, out);
    else out.append(name, String(v));
  }
  return out;
}

/** One call to Stripe's API with a form body (or none); the parsed JSON, or a thrown error with
 * Stripe's status (the routes turn that into a plain 502 without the detail). */
export async function stripeCall(b: Billing, method: "GET" | "POST", path: string,
                                 params?: Record<string, unknown>): Promise<Record<string, any>> {
  const res = await fetch(`${STRIPE_API}${path}`, {
    method,
    headers: { Authorization: `Bearer ${b.key}`, "Content-Type": "application/x-www-form-urlencoded" },
    body: params ? form(params) : undefined,
  });
  if (!res.ok) throw new Error(`stripe ${path} ${res.status}: ${(await res.text()).slice(0, 300)}`);
  return (await res.json()) as Record<string, any>;
}

/** Create a one-time Checkout Session and return its URL. Throws on any Stripe/network failure;
 * the route turns that into a plain 502 without the detail. */
async function session(b: Billing, user: string, price: string, metadata: Record<string, unknown>,
                       email: string): Promise<string> {
  const out = await stripeCall(b, "POST", "/checkout/sessions", {
    mode: "payment",                       // one-time: a pack (or a retired pass), nothing renews
    line_items: [{ price, quantity: 1 }],
    client_reference_id: user,
    customer_email: email,                 // prefilled (and the receipt goes there)
    metadata: { ...metadata, email },
    success_url: b.successUrl,
    cancel_url: b.cancelUrl,
  });
  if (!out.url) throw new Error("stripe returned no checkout url");
  return String(out.url);
}

/** The renewal terms, in the words the app shows next to the button, repeated on Stripe's page
 * above its Subscribe button. */
export function renewalTerms(planId: string): string {
  const p = PLANS[planId]!;
  const every = p.months === 1 ? "every month" : `every ${p.months} months`;
  return `${p.priceLabel} ${every}. Renews automatically until you cancel. ` +
         "Cancel anytime in SponsorJobs: open your account menu, then Your plan, then Cancel renewal.";
}

/** A Checkout Session for a renewing plan (mode=subscription); returns its URL. The caller has
 * already picked the plan server-side and checked the person agreed to the renewal terms;
 * `consentAt` is recorded on the session and the subscription. With a Stripe customer from an
 * earlier plan the same customer is reused, so the billing portal shows one history. */
export async function subscriptionCheckoutUrl(b: Billing, user: string, planId: string, email: string,
                                              consentAt: number, customer: string | null): Promise<string> {
  const price = b.planPrices[planId];
  if (!price || !SUBSCRIPTION_PLANS.includes(planId)) throw new UnknownProduct(`unknown plan: ${planId}`);
  const meta = { kind: "subscription", plan: planId, user, consent_at: consentAt };
  const out = await stripeCall(b, "POST", "/checkout/sessions", {
    mode: "subscription",                  // renews every interval until cancelled
    line_items: [{ price, quantity: 1 }],
    client_reference_id: user,
    ...(customer ? { customer } : { customer_email: email }),
    metadata: { ...meta, email },
    subscription_data: { metadata: meta }, // carried on every invoice, so renewals find the account
    custom_text: { submit: { message: renewalTerms(planId) } },
    success_url: b.successUrl,
    cancel_url: b.cancelUrl,
  });
  if (!out.url) throw new Error("stripe returned no checkout url");
  return String(out.url);
}

export type { Interval };

/** An unknown or unpriced pass/pack id: a 400, as Python's ValueError. */
export class UnknownProduct extends Error {}

export async function passCheckoutUrl(b: Billing, user: string, passId: string, email: string): Promise<string> {
  const price = b.passPrices[passId];
  if (!price) throw new UnknownProduct(`unknown pass: ${passId}`);
  const p = PLANS[passId]!;
  return session(b, user, price, { kind: "pass", pass: passId, days: p.days,
                                   seconds: p.avatarSecondsIncluded, packages: p.packages }, email);
}

/** The pack's price, or UnknownProduct. Checked BEFORE the active-pass rule, in the Python order
 * (an unknown pack is a 400 even without a pass). */
export function packPrice(b: Billing, pack: string): string {
  const price = b.packPrices[pack];
  if (!price) throw new UnknownProduct(`unknown pack: ${pack}`);
  return price;
}

export async function packCheckoutUrl(b: Billing, user: string, pack: string, email: string): Promise<string> {
  return session(b, user, packPrice(b, pack), { kind: "credit_pack", pack, seconds: packSeconds(pack) }, email);
}

// ---- webhook signature (Stripe-Signature: t=<unix>,v1=<hex hmac>[,v1=...]) ------------------- //

function hexToBytes(hex: string): Uint8Array | null {
  if (!/^[0-9a-f]+$/i.test(hex) || hex.length % 2) return null;
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

const hmacKey = (secret: string, use: "sign" | "verify") => crypto.subtle.importKey(
  "raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, [use]);

/** Verify a webhook body the way stripe.Webhook.construct_event does: the HMAC-SHA256 of
 * "<t>.<raw body>" under the endpoint secret must match one of the v1 signatures, and t must not
 * be older than the tolerance (replay protection). crypto.subtle.verify compares in constant time. */
export async function verifySignature(payload: string, header: string, secret: string, now: number,
                                      tolerance = SIGNATURE_TOLERANCE_SECONDS): Promise<boolean> {
  if (!secret || !header) return false;
  let t = Number.NaN;
  const sigs: string[] = [];
  for (const part of header.split(",")) {
    const i = part.indexOf("=");
    if (i < 0) continue;
    const k = part.slice(0, i).trim();
    const v = part.slice(i + 1).trim();
    if (k === "t") t = /^\d+$/.test(v) ? Number(v) : Number.NaN;
    else if (k === "v1") sigs.push(v);
  }
  if (!Number.isFinite(t) || sigs.length === 0) return false;
  if (tolerance > 0 && t < now - tolerance) return false;
  const key = await hmacKey(secret, "verify");
  const signed = new TextEncoder().encode(`${t}.${payload}`);
  for (const s of sigs) {
    const bytes = hexToBytes(s);
    if (bytes && await crypto.subtle.verify("HMAC", key, bytes, signed)) return true;
  }
  return false;
}

/** Sign a payload as Stripe does (the tests build webhook calls with it). */
export async function signPayload(payload: string, secret: string, t: number): Promise<string> {
  const mac = new Uint8Array(await crypto.subtle.sign(
    "HMAC", await hmacKey(secret, "sign"), new TextEncoder().encode(`${t}.${payload}`)));
  return `t=${t},v1=${[...mac].map((x) => x.toString(16).padStart(2, "0")).join("")}`;
}

// ---- what a paid session grants ------------------------------------------------------------- //

export type Grant =
  | { kind: "pass"; key: string; user: string; pass: string }
  | { kind: "credit"; key: string; user: string; seconds: number };

const GRANTING_EVENTS = new Set(["checkout.session.completed", "checkout.session.async_payment_succeeded"]);

/** What a VERIFIED one-time payment grants (Billing.apply_event / _apply_payment), or null. Both
 * granting events carry the same session, so the SESSION id is the idempotency key (as in Python):
 * a redelivered event, or completed followed by async_payment_succeeded, grants once. Renewing
 * plans are not granted here: src/subscriptions.ts grants each paid period from invoice.paid. */
export function grantFor(event: unknown): Grant | null {
  const e = (event && typeof event === "object" ? event : {}) as Record<string, any>;
  if (!GRANTING_EVENTS.has(String(e.type ?? ""))) return null;
  const s = (e.data && typeof e.data === "object" && e.data.object) || {};
  if (s.mode !== "payment" || s.payment_status !== "paid") return null;
  const meta = (s.metadata && typeof s.metadata === "object" ? s.metadata : {}) as Record<string, unknown>;
  const user = typeof s.client_reference_id === "string" ? s.client_reference_id : "";
  const sid = typeof s.id === "string" ? s.id : "";
  if (!user || !sid) return null;
  const key = `checkout:${sid}`;
  if (meta.kind === "pass") {
    const pass = String(meta.pass ?? "");
    return ONE_TIME_PASSES.includes(pass) ? { kind: "pass", key, user, pass } : null;
  }
  if (meta.kind === "credit_pack") {
    const pack = String(meta.pack ?? "");
    const fromMeta = Number.parseInt(String(meta.seconds ?? ""), 10);
    const seconds = pack in PACKS ? packSeconds(pack) : Number.isFinite(fromMeta) ? fromMeta : 0;
    return seconds > 0 ? { kind: "credit", key, user, seconds } : null;
  }
  return null;
}
