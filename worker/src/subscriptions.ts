// Plans that renew automatically (owner decision 2026-11-20): Stripe's subscription events turned
// into what an account may use, plus cancel, resume, the billing portal and renewal reminders.
//
// Entitlement: an account is on its plan until the end of the period it has PAID for. Each paid
// invoice (invoice.paid) starts a period with fresh allowances, once per invoice id
// (store.grantSubscriptionPeriodOnce), and access runs to that invoice's period end. Nothing else
// extends access: a failed renewal (invoice.payment_failed) just marks the plan so the app can
// say so, and access ends when the paid period does unless a retry succeeds. A subscription that
// is deleted (cancelled immediately, or ended unpaid) ends access at once.
//
// The `subscriptions` table (migrations/0014) mirrors what the app shows: the plan, whether it
// renews and when. It follows customer.subscription.created / updated / deleted, applying an event
// only when it is not older than the last one applied (Stripe does not promise delivery order).
//
// Stripe's object shapes changed in its 2025 API versions (the period end moved onto subscription
// items; an invoice's subscription moved under `parent`), and our webhook endpoint renders events
// in the account's default version, so every reader below accepts both shapes.
//
// Renewal reminder: Stripe sends invoice.upcoming a few days before each renewal (the number of
// days is the "Upcoming renewal events" setting in the Stripe dashboard, docs/stripe-setup.md). We
// answer it with our own e-mail through the existing mailer, once per subscription and period, so
// the reminder does not depend on which customer e-mails are switched on in Stripe.

import { emailOf } from "./accounts";
import { type Billing, planForPrice, stripeCall } from "./billing";
import type { Env } from "./env";
import { countSend, mailerFrom } from "./mailer";
import { PLANS, SUBSCRIPTION_PLANS } from "./metering";
import { endSubscriptionAccess, grantSubscriptionPeriodOnce } from "./store";

/** Statuses in which a subscription is still running (it may renew, or end at its period end). */
export const LIVE_STATUSES = ["active", "trialing", "past_due"] as const;
const ENDED_STATUSES = new Set(["canceled", "unpaid", "incomplete_expired"]);

export interface SubRow {
  id: string; user: string; customer: string | null; plan: string; status: string;
  current_period_end: number; cancel_at_period_end: number; consent_at: number | null;
  payment_failed_at: number | null; created_at: number; event_at: number;
}

const obj = (v: unknown): Record<string, any> => (v && typeof v === "object" ? v as Record<string, any> : {});
const idOf = (v: unknown): string => typeof v === "string" ? v : typeof obj(v).id === "string" ? obj(v).id : "";
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? Math.trunc(v) : 0);
const validPlan = (v: unknown): string | null => (typeof v === "string" && SUBSCRIPTION_PLANS.includes(v) ? v : null);

// ---- reading Stripe objects (both API shapes) ------------------------------------------------- //

/** A subscription's current period end: top level before 2025-03, on its items after. */
export function subPeriodEnd(sub: Record<string, any>): number {
  const top = num(sub.current_period_end);
  if (top) return top;
  const items = Array.isArray(obj(sub.items).data) ? obj(sub.items).data : [];
  return items.reduce((m: number, it: unknown) => Math.max(m, num(obj(it).current_period_end)), 0);
}

/** Whether a subscription is set to end instead of renewing: cancel_at_period_end, or a cancel_at
 * at (or before) the period end, which is how newer API versions record a portal cancellation. */
export function subCanceling(sub: Record<string, any>): boolean {
  if (sub.cancel_at_period_end === true) return true;
  const at = num(sub.cancel_at);
  return at > 0 && at <= subPeriodEnd(sub) + 60;
}

/** The first price id on a subscription or invoice line. */
function priceOfItem(it: Record<string, any>): string {
  return idOf(it.price) || idOf(obj(obj(it.pricing).price_details).price) || idOf(it.plan);
}

export function subPlan(sub: Record<string, any>, b: Billing): string | null {
  const fromMeta = validPlan(obj(sub.metadata).plan);
  if (fromMeta) return fromMeta;
  const items = Array.isArray(obj(sub.items).data) ? obj(sub.items).data : [];
  for (const it of items) {
    const p = planForPrice(b, priceOfItem(obj(it)));
    if (p) return p;
  }
  return null;
}

/** The subscription an invoice belongs to, its metadata, its paid period end and its price. */
export function invoiceParts(inv: Record<string, any>) {
  const parent = obj(obj(inv.parent).subscription_details);
  const sub = idOf(inv.subscription) || idOf(parent.subscription);
  const meta = { ...obj(parent.metadata), ...obj(obj(inv.subscription_details).metadata) };
  const lines = Array.isArray(obj(inv.lines).data) ? obj(inv.lines).data.map(obj) : [];
  const periodEnd = lines.reduce((m: number, l: Record<string, any>) => Math.max(m, num(obj(l.period).end)), 0);
  const periodStarts = lines.map((l: Record<string, any>) => num(obj(l.period).start)).filter((x: number) => x > 0);
  const periodStart = periodStarts.length ? Math.min(...periodStarts) : 0;
  const price = lines.map(priceOfItem).find((p: string) => p) ?? "";
  return { sub, meta, periodEnd, periodStart, price };
}

// ---- the table ------------------------------------------------------------------------------- //

export async function subById(db: D1Database, id: string): Promise<SubRow | null> {
  return id ? db.prepare("SELECT * FROM subscriptions WHERE id = ?").bind(id).first<SubRow>() : null;
}

/** The account's running subscription, if any (newest first). */
export async function liveSub(db: D1Database, user: string): Promise<SubRow | null> {
  return db.prepare(
    `SELECT * FROM subscriptions WHERE user = ? AND status IN (${LIVE_STATUSES.map((s) => `'${s}'`).join(", ")}) ` +
    "ORDER BY created_at DESC LIMIT 1").bind(user).first<SubRow>();
}

/** The newest Stripe customer this account has paid as, for the portal and the next checkout. */
export async function customerOf(db: D1Database, user: string): Promise<string | null> {
  const r = await db.prepare("SELECT customer FROM subscriptions WHERE user = ? AND customer IS NOT NULL " +
                             "ORDER BY created_at DESC LIMIT 1").bind(user).first<{ customer: string }>();
  return r?.customer ?? null;
}

/** What the app shows about a running plan: whether and when it renews, or when it ends. */
export function subView(r: SubRow | null) {
  if (!r || !LIVE_STATUSES.includes(r.status as typeof LIVE_STATUSES[number])) return null;
  const end = r.current_period_end || null;
  const canceling = Boolean(r.cancel_at_period_end);
  return {
    plan: r.plan, tier: PLANS[r.plan]?.tier ?? null, interval: PLANS[r.plan]?.interval ?? null,
    price_label: PLANS[r.plan]?.priceLabel ?? null, status: r.status,
    renews: !canceling, renews_at: canceling ? null : end, ends_at: canceling ? end : null,
    current_period_end: end, cancel_at_period_end: canceling,
    payment_failed: r.status === "past_due" || Boolean(r.payment_failed_at),
  };
}

/** Write a subscription's state from a Stripe subscription object, unless a newer event already
 * did. Returns the row as stored. */
async function upsertFromSub(db: D1Database, sub: Record<string, any>, user: string, plan: string,
                             eventAt: number, now: number): Promise<SubRow | null> {
  await db.prepare(
    `INSERT INTO subscriptions(id, user, customer, plan, status, current_period_end, cancel_at_period_end, created_at, event_at)
     VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
     ON CONFLICT(id) DO UPDATE SET
       customer = COALESCE(excluded.customer, subscriptions.customer),
       plan = excluded.plan, status = excluded.status,
       current_period_end = CASE WHEN excluded.current_period_end > 0 THEN excluded.current_period_end
                                 ELSE subscriptions.current_period_end END,
       cancel_at_period_end = excluded.cancel_at_period_end,
       payment_failed_at = CASE WHEN excluded.status = 'active' THEN NULL ELSE subscriptions.payment_failed_at END,
       event_at = excluded.event_at
     WHERE excluded.event_at >= subscriptions.event_at`,
  ).bind(idOf(sub), user, idOf(sub.customer) || null, plan, String(sub.status ?? "active"), subPeriodEnd(sub),
         subCanceling(sub) ? 1 : 0, num(sub.created) || now, eventAt).run();
  return subById(db, idOf(sub));
}

// ---- webhook --------------------------------------------------------------------------------- //

const HANDLED = new Set([
  "checkout.session.completed", "invoice.paid", "invoice.payment_failed", "invoice.upcoming",
  "customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted",
]);

/** Apply one VERIFIED Stripe event that concerns a renewing plan. Anything else is ignored (the
 * one-time payments are granted by billing.grantFor). Throws only on a database or mail failure,
 * which the route turns into a 5xx so Stripe delivers the event again; every step is idempotent. */
export async function applySubscriptionEvent(env: Env, b: Billing, event: unknown, now: number): Promise<void> {
  const e = obj(event);
  const type = String(e.type ?? "");
  if (!HANDLED.has(type)) return;
  const o = obj(obj(e.data).object);
  const eventAt = num(e.created) || now;
  const db = env.DB;

  if (type === "checkout.session.completed") {
    if (o.mode !== "subscription") return;
    const meta = obj(o.metadata);
    const user = typeof o.client_reference_id === "string" ? o.client_reference_id : "";
    const plan = validPlan(meta.plan);
    const subId = idOf(o.subscription);
    if (!user || !plan || !subId) return;
    // Remember who the subscription belongs to and the consent time; the period itself is granted
    // by invoice.paid (which may arrive before or after this event).
    await db.prepare(
      `INSERT INTO subscriptions(id, user, customer, plan, status, consent_at, created_at, event_at)
       VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, 0)
       ON CONFLICT(id) DO UPDATE SET customer = COALESCE(subscriptions.customer, excluded.customer),
         consent_at = COALESCE(subscriptions.consent_at, excluded.consent_at)`,
    ).bind(subId, user, idOf(o.customer) || null, plan, o.payment_status === "paid" ? "active" : "incomplete",
           num(Number(meta.consent_at)) || null, now).run();
    return;
  }

  if (type.startsWith("invoice.")) {
    const inv = invoiceParts(o);
    if (!inv.sub) return;                                      // not a plan's invoice
    const row = await subById(db, inv.sub);
    const user = (typeof inv.meta.user === "string" && inv.meta.user) || row?.user || "";
    const plan = validPlan(inv.meta.plan) ?? row?.plan ?? planForPrice(b, inv.price);
    if (!user || !plan || !validPlan(plan)) {
      console.error(`subscription ${inv.sub}: ${type} with no known account or plan; ignored`);
      return;
    }
    if (type === "invoice.paid") {
      const invId = idOf(o);
      if (!invId || inv.periodEnd <= 0) return;
      await grantSubscriptionPeriodOnce(db, `invoice:${invId}`, user, plan, inv.periodEnd, now);
      await db.prepare(
        `INSERT INTO subscriptions(id, user, customer, plan, status, current_period_end, created_at, event_at)
         VALUES(?1, ?2, ?3, ?4, 'active', ?5, ?6, 0)
         ON CONFLICT(id) DO UPDATE SET customer = COALESCE(subscriptions.customer, excluded.customer),
           current_period_end = MAX(subscriptions.current_period_end, excluded.current_period_end),
           status = CASE WHEN subscriptions.status IN ('past_due', 'incomplete') THEN 'active' ELSE subscriptions.status END,
           payment_failed_at = NULL`,
      ).bind(inv.sub, user, idOf(o.customer) || null, plan, inv.periodEnd, now).run();
      return;
    }
    if (type === "invoice.payment_failed") {
      if (!row) return;
      await db.prepare("UPDATE subscriptions SET payment_failed_at = ?, " +
                       "status = CASE WHEN status = 'active' THEN 'past_due' ELSE status END WHERE id = ?")
        .bind(now, inv.sub).run();
      return;
    }
    // invoice.upcoming: the renewal reminder.
    await sendRenewalReminder(env, row, user, plan, inv, num(o.amount_due), now);
    return;
  }

  // customer.subscription.created / updated / deleted
  const subId = idOf(o);
  if (!subId) return;
  const row = await subById(db, subId);
  const user = (typeof obj(o.metadata).user === "string" && obj(o.metadata).user) || row?.user || "";
  const plan = subPlan(o, b) ?? row?.plan ?? null;
  if (!user || !plan) {
    console.error(`subscription ${subId}: ${type} with no known account or plan; ignored`);
    return;
  }
  const status = type === "customer.subscription.deleted" ? "canceled" : String(o.status ?? "active");
  await upsertFromSub(db, { ...o, status }, user, plan, eventAt, now);
  if (ENDED_STATUSES.has(status)) await endSubscriptionAccess(db, user, subId, now);
}

/** The reminder e-mail before a renewal, at most once per subscription and period. */
async function sendRenewalReminder(env: Env, row: SubRow | null, user: string, plan: string,
                                   inv: ReturnType<typeof invoiceParts>, amountDue: number, now: number) {
  if (row && (row.cancel_at_period_end || !LIVE_STATUSES.includes(row.status as typeof LIVE_STATUSES[number]))) return;
  const renewAt = inv.periodStart || row?.current_period_end || 0;
  if (!renewAt) return;
  const mailer = mailerFrom(env);
  const email = await emailOf(env.DB, user);
  if (!mailer || !email) {
    console.error(`renewal reminder for ${inv.sub} not sent: ${mailer ? "no e-mail on the account" : "no mailer configured"}`);
    return;
  }
  const key = `reminder:${inv.sub}:${renewAt}`;
  const claimed = await env.DB.prepare("INSERT OR IGNORE INTO processed_events(key, at) VALUES(?, ?)").bind(key, now).run();
  if ((claimed.meta.changes ?? 0) === 0) return;               // already sent for this period
  const p = PLANS[plan]!;
  const price = amountDue > 0 ? `$${(amountDue / 100).toFixed(amountDue % 100 ? 2 : 0)}` : p.priceLabel;
  const date = new Date(renewAt * 1000).toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });
  const every = p.months === 1 ? "every month" : `every ${p.months} months`;
  const name = `${p.tier === "student" ? "Student" : "Standard"} plan`;
  try {
    await mailer.send({
      to: email,
      subject: `Your SponsorJobs plan renews on ${date}`,
      text: `Your SponsorJobs ${name} renews on ${date}. You will be charged ${price}, and it renews ${every} until you cancel.\n\n` +
            "Nothing to do if you want to keep it. To cancel, open SponsorJobs, click your account, then Your plan, " +
            "then Cancel renewal. You keep the plan until the end of the period you paid for.\n",
    });
  } catch (err) {
    // Let a redelivery try again, and make Stripe redeliver.
    await env.DB.prepare("DELETE FROM processed_events WHERE key = ?").bind(key).run();
    throw err;
  }
  await countSend(env.DB, now);
}

// ---- cancel, resume, portal ------------------------------------------------------------------ //

/** Set a running subscription to end at its period end (cancel) or to renew again (resume), on
 * Stripe first, then here from Stripe's answer. Returns the stored row. */
export async function setCanceling(env: Env, b: Billing, row: SubRow, cancel: boolean, now: number): Promise<SubRow | null> {
  let sub = await stripeCall(b, "POST", `/subscriptions/${encodeURIComponent(row.id)}`, { cancel_at_period_end: cancel });
  // A cancellation made in the billing portal on a newer API version sets cancel_at instead;
  // resuming clears that too.
  if (!cancel && num(sub.cancel_at) > 0) {
    sub = await stripeCall(b, "POST", `/subscriptions/${encodeURIComponent(row.id)}`, { cancel_at: "" });
  }
  return upsertFromSub(env.DB, sub, row.user, subPlan(sub, b) ?? row.plan, Math.max(now, row.event_at), now);
}

/** A Stripe billing portal session (update the card, see invoices) for this customer. */
export async function portalUrl(b: Billing, customer: string, returnUrl: string, configuration?: string): Promise<string> {
  // Our own portal settings (scripts/stripe_setup.py) when set; otherwise the dashboard's default.
  const out = await stripeCall(b, "POST", "/billing_portal/sessions",
                               { customer, return_url: returnUrl, configuration: configuration || undefined });
  if (!out.url) throw new Error("stripe returned no portal url");
  return String(out.url);
}
