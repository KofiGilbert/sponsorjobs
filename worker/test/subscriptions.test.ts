// Plans that renew automatically: the server picks the student or standard price, checkout needs
// the renewal consent, and Stripe's subscription events move the entitlement (paid through the
// period end). Stripe and Resend are stubbed (no network); webhook payloads are signed here with a
// test secret, the way Stripe signs them.
import { afterEach, describe, expect, it, vi } from "vitest";
import { renewalTerms, signPayload, STRIPE_API } from "../src/billing";
import type { Env } from "../src/env";
import { RESEND_API } from "../src/mailer";
import { DAY_SECONDS, INTERVIEW_SECONDS } from "../src/metering";
import { setPass } from "../src/store";
import { call, env, newAccount, signedInAccount } from "./helpers";

afterEach(() => vi.restoreAllMocks());

const WHSEC = "whsec_test_not_real";
const STRIPE: Partial<Env> = {
  STRIPE_SECRET_KEY: "sk_test_not_real",
  STRIPE_WEBHOOK_SECRET: WHSEC,
  STRIPE_PRICE_STUDENT_MONTHLY: "price_student_m",
  STRIPE_PRICE_STUDENT_QUARTERLY: "price_student_q",
  STRIPE_PRICE_STANDARD_MONTHLY: "price_standard_m",
  STRIPE_PRICE_STANDARD_QUARTERLY: "price_standard_q",
  STRIPE_PRICE_PACK_1: "price_pack1",
  RESEND_API_KEY: "re_test_not_real",
};

const now = () => Math.floor(Date.now() / 1000);

interface Sent { url: string; method: string; form: Record<string, string>; json?: any }

/** Stub fetch as Stripe (and Resend). `subscription` is what POST /subscriptions/<id> answers. */
function stubStripe(o: { subscription?: (form: Record<string, string>) => unknown; status?: number } = {}) {
  const sent: Sent[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const req = new Request(input as RequestInfo, init as RequestInit);
    if (req.url === RESEND_API) {
      sent.push({ url: req.url, method: req.method, form: {}, json: await req.json() });
      return new Response(JSON.stringify({ id: "email_1" }));
    }
    const form = Object.fromEntries([...(await req.formData()).entries()].map(([k, v]) => [k, String(v)]));
    sent.push({ url: req.url, method: req.method, form });
    if (o.status) return new Response(JSON.stringify({ error: { message: "secret detail" } }), { status: o.status });
    if (req.url === `${STRIPE_API}/checkout/sessions`) {
      return new Response(JSON.stringify({ id: "cs_sub_1", url: "https://checkout.stripe.com/c/pay/cs_sub_1" }));
    }
    if (req.url.startsWith(`${STRIPE_API}/subscriptions/`)) return new Response(JSON.stringify(o.subscription!(form)));
    if (req.url === `${STRIPE_API}/billing_portal/sessions`) {
      return new Response(JSON.stringify({ url: "https://billing.stripe.com/p/session/test_1" }));
    }
    throw new Error(`unexpected fetch ${req.url}`);
  });
  return sent;
}

async function deliver(event: unknown) {
  const raw = JSON.stringify(event);
  return call("/billing/webhook", { method: "POST", rawBody: raw, env: STRIPE,
                                    headers: { "Stripe-Signature": await signPayload(raw, WHSEC, now()) } });
}

const usage = async (token: string) => (await call("/me/usage", { token, env: STRIPE })).data;
const offers = async (token: string) => (await call("/billing/offers", { token, env: STRIPE })).data;
const subscribe = (token: string, b: Record<string, unknown>) => call("/billing/subscribe", { token, body: b, env: STRIPE });

/** "YYYY-MM" `delta` months from this month (UTC). */
function monthFromNow(delta: number): string {
  const d = new Date();
  const idx = d.getUTCFullYear() * 12 + d.getUTCMonth() + delta;
  return `${Math.floor(idx / 12)}-${String((idx % 12) + 1).padStart(2, "0")}`;
}

/** Store a student check directly (the code flow itself is tested in student.test.ts). */
async function makeStudent(user: string, gradMonth: string) {
  await env.DB.prepare("INSERT INTO student_verifications(user, school_domain, email_hash, grad_month, verified_at) " +
                       "VALUES(?, 'example.edu', ?, ?, ?)").bind(user, `h_${user}`, gradMonth, now()).run();
}

let seq = 0;
const evtId = () => `evt_${++seq}`;

function sessionCompleted(user: string, sub: string, plan: string) {
  return { id: evtId(), type: "checkout.session.completed", created: now(),
           data: { object: { id: `cs_${sub}`, object: "checkout.session", mode: "subscription", payment_status: "paid",
                             client_reference_id: user, subscription: sub, customer: `cus_${user}`,
                             metadata: { kind: "subscription", plan, user, consent_at: String(now()) } } } };
}

/** invoice.paid in the pre-2025 shape (invoice.subscription, subscription_details.metadata). */
function invoicePaid(user: string, sub: string, plan: string, start: number, end: number, id = `in_${++seq}`) {
  return { id: evtId(), type: "invoice.paid", created: now(),
           data: { object: { id, object: "invoice", subscription: sub, customer: `cus_${user}`, status: "paid",
                             amount_paid: 2900, subscription_details: { metadata: { user, plan } },
                             lines: { data: [{ period: { start, end }, price: { id: "price_x" } }] } } } };
}

/** invoice.paid in the 2025+ shape (parent.subscription_details, pricing.price_details), with no
 * metadata at all: the plan comes from the price and the account from our own table. */
function invoicePaidNewShape(sub: string, price: string, start: number, end: number, id = `in_${++seq}`) {
  return { id: evtId(), type: "invoice.paid", created: now(),
           data: { object: { id, object: "invoice", status: "paid", customer: "cus_new",
                             parent: { type: "subscription_details", subscription_details: { subscription: sub, metadata: {} } },
                             lines: { data: [{ period: { start, end }, pricing: { price_details: { price } } }] } } } };
}

function subEvent(type: string, user: string, sub: string, plan: string, over: Record<string, unknown> = {}, created = now()) {
  return { id: evtId(), type, created,
           data: { object: { id: sub, object: "subscription", customer: `cus_${user}`, status: "active",
                             cancel_at_period_end: false, current_period_end: now() + 30 * DAY_SECONDS,
                             metadata: { user, plan }, ...over } } };
}

describe("offers", () => {
  it("lists both tiers of every interval, renewing, and says which tier the caller pays", async () => {
    const a = await signedInAccount();
    const o = await offers(a.token);
    expect(o.plans.map((p: any) => p.id)).toEqual(["student_monthly", "student_quarterly", "standard_monthly", "standard_quarterly"]);
    expect(o.plans.map((p: any) => p.price_label)).toEqual(["$29", "$69", "$49", "$119"]);
    expect(o.plans.every((p: any) => p.auto_renew === true)).toBe(true);
    expect(o.plans[1]).toMatchObject({ tier: "student", interval: "quarterly", months: 3, interviews: 9, packages: 150 });
    expect(o.price_tier).toBe("standard");
    expect(o.passes).toEqual([]);                          // the retired passes are not on sale
    await makeStudent(a.id, monthFromNow(10));
    expect((await offers(a.token)).price_tier).toBe("student");
  });
});

describe("checkout picks the price on the server", () => {
  it("needs the renewal consent and a real interval; Stripe is not called otherwise", async () => {
    const a = await signedInAccount();
    const sent = stubStripe();
    expect((await subscribe(a.token, { interval: "monthly" })).data.reason).toBe("consent_required");
    expect((await subscribe(a.token, { interval: "monthly", consent: "yes" })).data.reason).toBe("consent_required");
    expect((await subscribe(a.token, { interval: "weekly", consent: true })).data.reason).toBe("bad_interval");
    expect(sent).toHaveLength(0);
  });

  it("an anonymous account must sign in first", async () => {
    const a = await newAccount();
    const r = await subscribe(a.token, { interval: "monthly", consent: true });
    expect(r.status).toBe(401);
    expect(r.data.reason).toBe("sign_in_required");
  });

  it("standard for everyone without a student check, whatever the client asks for", async () => {
    const a = await signedInAccount();
    const sent = stubStripe();
    const r = await subscribe(a.token, { interval: "quarterly", consent: true, plan: "student_quarterly", tier: "student" });
    expect(r.status).toBe(200);
    expect(r.data).toEqual({ url: "https://checkout.stripe.com/c/pay/cs_sub_1", plan: "standard_quarterly" });
    const f = sent[0]!.form;
    expect(f).toMatchObject({
      mode: "subscription", "line_items[0][price]": "price_standard_q", "line_items[0][quantity]": "1",
      client_reference_id: a.id, customer_email: a.email,
      "metadata[kind]": "subscription", "metadata[plan]": "standard_quarterly",
      "subscription_data[metadata][user]": a.id, "subscription_data[metadata][plan]": "standard_quarterly",
    });
    expect(Number(f["subscription_data[metadata][consent_at]"])).toBeGreaterThan(now() - 60);
    // The renewal terms are on Stripe's page too, next to its button.
    expect(f["custom_text[submit][message]"]).toBe(renewalTerms("standard_quarterly"));
    expect(f["custom_text[submit][message]"]).toContain("$119 every 3 months. Renews automatically until you cancel.");
  });

  it("the student price for a verified student, and for a graduate up to 24 months after", async () => {
    for (const grad of [monthFromNow(18), monthFromNow(-24)]) {
      const a = await signedInAccount();
      await makeStudent(a.id, grad);
      const sent = stubStripe();
      const r = await subscribe(a.token, { interval: "monthly", consent: true });
      expect(r.data.plan, grad).toBe("student_monthly");
      expect(sent[0]!.form["line_items[0][price]"]).toBe("price_student_m");
      vi.restoreAllMocks();
    }
  });

  it("standard again once the 24-month window has passed", async () => {
    const a = await signedInAccount();
    await makeStudent(a.id, monthFromNow(-25));
    stubStripe();
    expect((await offers(a.token)).price_tier).toBe("standard");
    expect((await subscribe(a.token, { interval: "monthly", consent: true })).data.plan).toBe("standard_monthly");
  });

  it("one running plan per account; a Stripe failure is a clean 502", async () => {
    const a = await signedInAccount();
    await deliver(sessionCompleted(a.id, `sub_one_${a.id}`, "standard_monthly"));
    stubStripe();
    const r = await subscribe(a.token, { interval: "monthly", consent: true });
    expect(r.status).toBe(409);
    expect(r.data.reason).toBe("already_subscribed");
    vi.restoreAllMocks();
    const b = await signedInAccount();
    stubStripe({ status: 500 });
    const failed = await subscribe(b.token, { interval: "monthly", consent: true });
    expect(failed.status).toBe(502);
    expect(failed.data).toEqual({ error: "could not start checkout" });
  });
});

describe("webhook: entitlement follows the paid period", () => {
  it("a paid first invoice starts the plan until its period end, once", async () => {
    const a = await signedInAccount();
    const sub = `sub_a_${a.id}`;
    const end = now() + 30 * DAY_SECONDS;
    await deliver(sessionCompleted(a.id, sub, "student_monthly"));
    expect((await usage(a.token)).tier).toBe("free");              // nothing until the invoice is paid
    const paid = invoicePaid(a.id, sub, "student_monthly", now(), end);
    expect((await deliver(paid)).status).toBe(200);
    const u = await usage(a.token);
    expect(u).toMatchObject({ tier: "student_monthly", pass_until: end, interviews_left: 3, packages_left: 60 });
    expect(u.subscription).toMatchObject({ plan: "student_monthly", renews: true, renews_at: end, cancel_at_period_end: false });
    await deliver(paid);                                             // redelivered: no second grant
    await deliver({ ...paid, id: evtId() });
    expect((await usage(a.token)).interviews_left).toBe(3);
    // The consent time from checkout is kept on the subscription.
    const row = await env.DB.prepare("SELECT consent_at FROM subscriptions WHERE id = ?").bind(sub).first<{ consent_at: number }>();
    expect(row!.consent_at).toBeGreaterThan(0);
  });

  it("each renewal refills the allowances; a late older invoice changes nothing", async () => {
    const a = await signedInAccount();
    const sub = `sub_b_${a.id}`;
    const t = now();
    await deliver(invoicePaid(a.id, sub, "standard_monthly", t, t + 30 * DAY_SECONDS, `in_first_${a.id}`));
    await env.DB.prepare("UPDATE users SET pass_seconds = 0, pass_packages = 2 WHERE user = ?").bind(a.id).run();
    const next = t + 61 * DAY_SECONDS;
    await deliver(invoicePaid(a.id, sub, "standard_monthly", t + 30 * DAY_SECONDS, next));
    expect(await usage(a.token)).toMatchObject({ tier: "standard_monthly", pass_until: next, interviews_left: 3, packages_left: 60 });
    await env.DB.prepare("UPDATE users SET pass_packages = 7 WHERE user = ?").bind(a.id).run();
    await deliver(invoicePaid(a.id, sub, "standard_monthly", t - 30 * DAY_SECONDS, t + 30 * DAY_SECONDS));
    expect(await usage(a.token)).toMatchObject({ pass_until: next, packages_left: 7 });
  });

  it("reads Stripe's 2025 invoice shape: subscription under parent, price under pricing", async () => {
    const a = await signedInAccount();
    const sub = `sub_c_${a.id}`;
    await deliver(sessionCompleted(a.id, sub, "student_quarterly"));
    const end = now() + 90 * DAY_SECONDS;
    await deliver(invoicePaidNewShape(sub, "price_student_q", now(), end));
    expect(await usage(a.token)).toMatchObject({ tier: "student_quarterly", pass_until: end, interviews_left: 9 });
  });

  it("someone still on a one-time pass keeps what is left of it", async () => {
    const a = await signedInAccount();
    const passEnd = now() + 50 * DAY_SECONDS;
    await setPass(env.DB, a.id, "pass90", passEnd, 2 * INTERVIEW_SECONDS, 40);
    const end = now() + 30 * DAY_SECONDS;
    await deliver(invoicePaid(a.id, `sub_d_${a.id}`, "standard_monthly", now(), end));
    expect(await usage(a.token)).toMatchObject({ tier: "standard_monthly", pass_until: passEnd,
                                                 interviews_left: 2 + 3, packages_left: 40 + 60 });
  });

  it("a failed renewal is shown, and access still ends with the paid period", async () => {
    const a = await signedInAccount();
    const sub = `sub_e_${a.id}`;
    const end = now() + 5 * DAY_SECONDS;
    await deliver(invoicePaid(a.id, sub, "standard_monthly", now() - 25 * DAY_SECONDS, end));
    await deliver({ id: evtId(), type: "invoice.payment_failed", created: now(),
                    data: { object: { id: "in_failed", subscription: sub, lines: { data: [] },
                                      subscription_details: { metadata: { user: a.id, plan: "standard_monthly" } } } } });
    const u = await usage(a.token);
    expect(u.pass_until).toBe(end);
    expect(u.subscription).toMatchObject({ status: "past_due", payment_failed: true });
    // A retry that succeeds clears it and starts the next period.
    await deliver(invoicePaid(a.id, sub, "standard_monthly", end, end + 30 * DAY_SECONDS));
    expect((await usage(a.token)).subscription).toMatchObject({ status: "active", payment_failed: false });
  });

  it("cancel at period end keeps access to the end; deletion ends it; an older event is ignored", async () => {
    const a = await signedInAccount();
    const sub = `sub_f_${a.id}`;
    const end = now() + 20 * DAY_SECONDS;
    await deliver(invoicePaid(a.id, sub, "student_monthly", now() - 10 * DAY_SECONDS, end));
    await deliver(subEvent("customer.subscription.updated", a.id, sub, "student_monthly",
                           { cancel_at_period_end: true, current_period_end: end }));
    let u = await usage(a.token);
    expect(u.tier).toBe("student_monthly");
    expect(u.subscription).toMatchObject({ renews: false, renews_at: null, ends_at: end, cancel_at_period_end: true });
    // An update that Stripe made earlier but delivered late does not undo the cancellation.
    await deliver(subEvent("customer.subscription.updated", a.id, sub, "student_monthly",
                           { cancel_at_period_end: false, current_period_end: end }, now() - 3600));
    expect((await usage(a.token)).subscription.cancel_at_period_end).toBe(true);
    // Newer API versions record a portal cancellation as cancel_at instead.
    await deliver(subEvent("customer.subscription.updated", a.id, sub, "student_monthly",
                           { cancel_at_period_end: false, cancel_at: end, current_period_end: undefined,
                             items: { data: [{ current_period_end: end, price: { id: "price_student_m" } }] } }));
    expect((await usage(a.token)).subscription).toMatchObject({ cancel_at_period_end: true, ends_at: end });
    await deliver(subEvent("customer.subscription.deleted", a.id, sub, "student_monthly", { status: "canceled" }));
    u = await usage(a.token);
    expect(u.tier).toBe("free");
    expect(u.subscription).toBeNull();
  });

  it("an event for an unknown subscription with no account is acknowledged and changes nothing", async () => {
    const r = await deliver(invoicePaidNewShape("sub_nobody", "price_unknown", now(), now() + DAY_SECONDS));
    expect(r.status).toBe(200);
    expect(r.data).toEqual({ received: true });
  });
});

describe("cancel, resume and the billing portal", () => {
  it("one call cancels renewal on Stripe; another resumes it", async () => {
    const a = await signedInAccount();
    const sub = `sub_g_${a.id}`;
    const end = now() + 30 * DAY_SECONDS;
    await deliver(sessionCompleted(a.id, sub, "standard_quarterly"));
    await deliver(invoicePaid(a.id, sub, "standard_quarterly", now(), end));
    const stripeSub = (form: Record<string, string>) => ({
      id: sub, object: "subscription", customer: `cus_${a.id}`, status: "active", metadata: { user: a.id, plan: "standard_quarterly" },
      cancel_at_period_end: form.cancel_at_period_end === "true", current_period_end: end });
    const sent = stubStripe({ subscription: stripeSub });
    const c = await call("/billing/subscription/cancel", { token: a.token, method: "POST", env: STRIPE });
    expect(c.status).toBe(200);
    expect(sent[0]).toMatchObject({ url: `${STRIPE_API}/subscriptions/${sub}`, method: "POST", form: { cancel_at_period_end: "true" } });
    expect(c.data.subscription).toMatchObject({ renews: false, ends_at: end });
    expect((await usage(a.token)).tier).toBe("standard_quarterly");    // still theirs until the end
    const r = await call("/billing/subscription/resume", { token: a.token, method: "POST", env: STRIPE });
    expect(sent[1]!.form).toEqual({ cancel_at_period_end: "false" });
    expect(r.data.subscription).toMatchObject({ renews: true, renews_at: end });
    const p = await call("/billing/portal", { token: a.token, method: "POST", env: STRIPE });
    expect(p.data).toEqual({ url: "https://billing.stripe.com/p/session/test_1" });
    expect(sent[2]!.form).toMatchObject({ customer: `cus_${a.id}`, return_url: "http://127.0.0.1:57000/" });
  });

  it("without a plan there is nothing to cancel", async () => {
    const a = await signedInAccount();
    const sent = stubStripe();
    for (const path of ["/billing/subscription/cancel", "/billing/subscription/resume", "/billing/portal"]) {
      const r = await call(path, { token: a.token, method: "POST", env: STRIPE });
      expect(r.status, path).toBe(404);
      expect(r.data.reason, path).toBe("no_subscription");
    }
    expect(sent).toHaveLength(0);
  });
});

describe("the renewal reminder", () => {
  const upcoming = (user: string, sub: string, renewAt: number) => ({
    id: evtId(), type: "invoice.upcoming", created: now(),
    data: { object: { object: "invoice", subscription: sub, amount_due: 4900,
                      subscription_details: { metadata: { user, plan: "standard_monthly" } },
                      lines: { data: [{ period: { start: renewAt, end: renewAt + 30 * DAY_SECONDS } }] } } } });

  it("e-mails the price, the date and how to cancel, once per period", async () => {
    const a = await signedInAccount();
    const sub = `sub_h_${a.id}`;
    const renewAt = now() + 3 * DAY_SECONDS;
    await deliver(invoicePaid(a.id, sub, "standard_monthly", now() - 27 * DAY_SECONDS, renewAt));
    const sent = stubStripe();
    await deliver(upcoming(a.id, sub, renewAt));
    await deliver(upcoming(a.id, sub, renewAt));
    const mails = sent.filter((s) => s.url === RESEND_API);
    expect(mails).toHaveLength(1);
    expect(mails[0]!.json.to).toEqual([a.email]);
    expect(mails[0]!.json.subject).toContain("renews on");
    expect(mails[0]!.json.text).toContain("$49");
    expect(mails[0]!.json.text).toContain("Cancel renewal");
  });

  it("is not sent for a plan that is set to end", async () => {
    const a = await signedInAccount();
    const sub = `sub_i_${a.id}`;
    const renewAt = now() + 3 * DAY_SECONDS;
    await deliver(invoicePaid(a.id, sub, "standard_monthly", now() - 27 * DAY_SECONDS, renewAt));
    await deliver(subEvent("customer.subscription.updated", a.id, sub, "standard_monthly",
                           { cancel_at_period_end: true, current_period_end: renewAt }));
    const sent = stubStripe();
    await deliver(upcoming(a.id, sub, renewAt));
    expect(sent.filter((s) => s.url === RESEND_API)).toHaveLength(0);
  });
});
