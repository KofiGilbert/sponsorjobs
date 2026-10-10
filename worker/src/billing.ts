// Stripe billing for the broker: one-time passes and extra-interview packs, ported from
// backend/billing.py (the reference) and the /billing/* routes in backend/broker.py.
//
// Nothing auto-renews and no subscription is sold: every purchase is a one-time Stripe Checkout
// Session (mode=payment). The webhook grants what was bought on checkout.session.completed once the
// session is paid, exactly once per session id (processed_events), so Stripe redelivering an event
// never grants twice.
//
// Stripe is called with plain fetch and form-encoded bodies (no SDK). The secret key lives only in
// the Worker's secrets. Nothing about a resume or history is ever sent to Stripe: only the account
// id and the product.

import type { Env } from "./env";
import { interviewsIn, PACK_PRICE_LABEL, PACKS, packSeconds, PASSES, PLANS } from "./metering";

const PASS_PRICE_ENV: Record<string, keyof Env> = {
  pass30: "STRIPE_PRICE_PASS30", pass90: "STRIPE_PRICE_PASS90" };
const PACK_PRICE_ENV: Record<string, keyof Env> = {
  pack_1: "STRIPE_PRICE_PACK_1", pack_3: "STRIPE_PRICE_PACK_3", pack_5: "STRIPE_PRICE_PACK_5" };

/** Stripe's default webhook timestamp tolerance (stripe.Webhook.DEFAULT_TOLERANCE). */
export const SIGNATURE_TOLERANCE_SECONDS = 300;

export const STRIPE_API = "https://api.stripe.com/v1";

export interface Billing {
  key: string;
  webhookSecret: string;
  passPrices: Record<string, string>;
  packPrices: Record<string, string>;
  successUrl: string;
  cancelUrl: string;
}

/** Billing as backend/server.py build_billing decides it: on only with a secret key AND at least
 * one pass price; otherwise null and every route answers "billing is not configured". */
export function billingFrom(env: Env): Billing | null {
  const key = env.STRIPE_SECRET_KEY || env.STRIPE_TEST_SECRET_KEY || "";
  const pick = (m: Record<string, keyof Env>): Record<string, string> => Object.fromEntries(
    Object.entries(m).map(([id, name]) => [id, String(env[name] ?? "").trim()]).filter(([, v]) => v));
  const passPrices = pick(PASS_PRICE_ENV);
  if (!key || Object.keys(passPrices).length === 0) return null;
  const app = (env.TAILOR_APP_URL || "http://127.0.0.1:57000").replace(/\/+$/, "");
  return { key, webhookSecret: env.STRIPE_WEBHOOK_SECRET || "", passPrices, packPrices: pick(PACK_PRICE_ENV),
           successUrl: `${app}/?checkout=success`, cancelUrl: `${app}/?checkout=cancel` };
}

// ---- what is on sale ------------------------------------------------------------------------ //

export function passOffer(id: string) {
  const p = PLANS[id]!;
  return { id, price_label: p.priceLabel, days: p.days, interviews: interviewsIn(p),
           packages: p.packages, auto_renew: false };
}

export function packOffer(id: string) {
  return { id, price_label: PACK_PRICE_LABEL[id], interviews: PACKS[id], seconds: packSeconds(id) };
}

export const offeredPasses = (b: Billing) => PASSES.filter((id) => b.passPrices[id]).map(passOffer);

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

/** Create a one-time Checkout Session and return its URL. Throws on any Stripe/network failure;
 * the route turns that into a plain 502 without the detail. */
async function session(b: Billing, user: string, price: string, metadata: Record<string, unknown>): Promise<string> {
  const res = await fetch(`${STRIPE_API}/checkout/sessions`, {
    method: "POST",
    headers: { Authorization: `Bearer ${b.key}`, "Content-Type": "application/x-www-form-urlencoded" },
    body: form({
      mode: "payment",                       // one-time: nothing renews, nothing to cancel
      line_items: [{ price, quantity: 1 }],
      client_reference_id: user,
      metadata,
      success_url: b.successUrl,
      cancel_url: b.cancelUrl,
    }),
  });
  if (!res.ok) throw new Error(`stripe checkout ${res.status}: ${(await res.text()).slice(0, 300)}`);
  const url = ((await res.json()) as { url?: string }).url;
  if (!url) throw new Error("stripe returned no checkout url");
  return url;
}

/** An unknown or unpriced pass/pack id: a 400, as Python's ValueError. */
export class UnknownProduct extends Error {}

export async function passCheckoutUrl(b: Billing, user: string, passId: string): Promise<string> {
  const price = b.passPrices[passId];
  if (!price) throw new UnknownProduct(`unknown pass: ${passId}`);
  const p = PLANS[passId]!;
  return session(b, user, price, { kind: "pass", pass: passId, days: p.days,
                                   seconds: p.avatarSecondsIncluded, packages: p.packages });
}

/** The pack's price, or UnknownProduct. Checked BEFORE the active-pass rule, in the Python order
 * (an unknown pack is a 400 even without a pass). */
export function packPrice(b: Billing, pack: string): string {
  const price = b.packPrices[pack];
  if (!price) throw new UnknownProduct(`unknown pack: ${pack}`);
  return price;
}

export async function packCheckoutUrl(b: Billing, user: string, pack: string): Promise<string> {
  return session(b, user, packPrice(b, pack), { kind: "credit_pack", pack, seconds: packSeconds(pack) });
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

/** What a VERIFIED event grants (Billing.apply_event / _apply_payment), or null. Both granting
 * events carry the same session, so the SESSION id is the idempotency key (as in Python): a
 * redelivered event, or completed followed by async_payment_succeeded, grants once. Anything else,
 * including the retired subscription events, changes nothing. */
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
    return PASSES.includes(pass) ? { kind: "pass", key, user, pass } : null;
  }
  if (meta.kind === "credit_pack") {
    const pack = String(meta.pack ?? "");
    const fromMeta = Number.parseInt(String(meta.seconds ?? ""), 10);
    const seconds = pack in PACKS ? packSeconds(pack) : Number.isFinite(fromMeta) ? fromMeta : 0;
    return seconds > 0 ? { kind: "credit", key, user, seconds } : null;
  }
  return null;
}
