// Stripe billing parity with tests/test_billing.py: checkout sessions, the pass-only rule for packs,
// webhook signature checks and exactly-once grants. Stripe is stubbed (no network) and webhook
// payloads are signed here with a test secret, the way Stripe signs them.
import { afterEach, describe, expect, it, vi } from "vitest";
import { signPayload, STRIPE_API } from "../src/billing";
import type { Env } from "../src/env";
import { DAY_SECONDS, INTERVIEW_SECONDS, PACKS, PLANS } from "../src/metering";
import { setPass } from "../src/store";
import { call, env, givePass, newAccount } from "./helpers";

afterEach(() => vi.restoreAllMocks());

const WHSEC = "whsec_test_not_real";
const STRIPE: Partial<Env> = {
  STRIPE_SECRET_KEY: "sk_test_not_real",
  STRIPE_WEBHOOK_SECRET: WHSEC,
  STRIPE_PRICE_PASS30: "price_pass30",
  STRIPE_PRICE_PASS90: "price_pass90",
  STRIPE_PRICE_PACK_1: "price_pack1",
  STRIPE_PRICE_PACK_3: "price_pack3",
  STRIPE_PRICE_PACK_5: "price_pack5",
};

interface StripeCall { url: string; auth: string | null; form: Record<string, string> }

/** Stub global fetch as Stripe's Checkout API; records each request's form fields. */
function stubStripe(reply: { status?: number; body: unknown } = { body: { id: "cs_test_1", url: "https://checkout.stripe.com/c/pay/cs_test_1" } }) {
  const sent: StripeCall[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const req = new Request(input as RequestInfo, init as RequestInit);
    sent.push({ url: req.url, auth: req.headers.get("Authorization"),
                form: Object.fromEntries([...(await req.formData()).entries()].map(([k, v]) => [k, String(v)])) });
    return new Response(JSON.stringify(reply.body), { status: reply.status ?? 200 });
  });
  return sent;
}

const now = () => Math.floor(Date.now() / 1000);
let sessionSeq = 0;

function completed(user: string, metadata: Record<string, unknown>, over: Record<string, unknown> = {}) {
  const id = `cs_test_${++sessionSeq}_${user}`;
  return {
    id: `evt_${sessionSeq}`, type: "checkout.session.completed",
    data: { object: { id, object: "checkout.session", mode: "payment", payment_status: "paid",
                      client_reference_id: user, metadata, ...over } },
  };
}

async function deliver(event: unknown, o: { secret?: string; t?: number; header?: string } = {}) {
  const raw = JSON.stringify(event);
  const header = o.header ?? await signPayload(raw, o.secret ?? WHSEC, o.t ?? now());
  return call("/billing/webhook", { method: "POST", rawBody: raw, headers: { "Stripe-Signature": header },
                                    env: STRIPE });
}

const usage = async (token: string) => (await call("/me/usage", { token })).data;

describe("unconfigured billing answers exactly as before", () => {
  it("no secret key: offers/packs list nothing, checkout and webhook are 503 not configured", async () => {
    const { token } = await newAccount();
    const noKey = { ...STRIPE, STRIPE_SECRET_KEY: "" };
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    expect((await call("/billing/offers", { token, env: noKey })).data).toEqual({
      passes: [], packs: [], current: { tier: "free", pass_until: null, interviews_left: 0, packages_left: 3 } });
    expect((await call("/billing/packs", { env: noKey })).data).toEqual({ packs: [] });
    for (const path of ["/billing/passes/pass30/checkout", "/billing/packs/pack_1/checkout", "/billing/webhook"]) {
      const r = await call(path, { method: "POST", token, env: noKey });
      expect(r.status, path).toBe(503);
      expect(r.data, path).toEqual({ error: "billing is not configured" });
    }
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("a key without any pass price is still off (as backend/server.py build_billing)", async () => {
    const r = await call("/billing/passes/pass30/checkout", { method: "POST",
      env: { STRIPE_SECRET_KEY: "sk_test_x", STRIPE_PRICE_PACK_1: "price_pack1" } });
    expect(r.status).toBe(503);
    expect((await call("/billing/packs", { env: { STRIPE_SECRET_KEY: "sk_test_x", STRIPE_PRICE_PACK_1: "p" } })).data)
      .toEqual({ packs: [] });
  });
});

describe("offers", () => {
  it("lists the priced passes; packs only for a caller with an active pass", async () => {
    const { id, token } = await newAccount();
    const pass30 = { id: "pass30", price_label: "$29", days: 30, interviews: 3, packages: 60, auto_renew: false };
    const pass90 = { id: "pass90", price_label: "$69", days: 90, interviews: 9, packages: 150, auto_renew: false };
    const free = await call("/billing/offers", { token, env: STRIPE });
    expect(free.data.passes).toEqual([pass30, pass90]);
    expect(free.data.packs).toEqual([]);
    await givePass(id);
    const paid = await call("/billing/offers", { token, env: STRIPE });
    expect(paid.data.packs.map((p: any) => p.id)).toEqual(["pack_1", "pack_3", "pack_5"]);
    expect(paid.data.packs[1]).toEqual({ id: "pack_3", price_label: "$24", interviews: 3, seconds: 3 * INTERVIEW_SECONDS });
    expect(paid.data.current.tier).toBe("pass30");
    expect((await call("/billing/offers", { env: STRIPE })).data.current).toBeNull();
  });

  it("an unset price is simply not offered", async () => {
    const r = await call("/billing/packs", { env: { ...STRIPE, STRIPE_PRICE_PACK_3: "" } });
    expect(r.data.packs.map((p: any) => p.id)).toEqual(["pack_1", "pack_5"]);
  });

  it("pass and pack allowances are N interviews x INTERVIEW_SECONDS", () => {
    expect(PLANS.pass30!.avatarSecondsIncluded).toBe(3 * INTERVIEW_SECONDS);
    expect(PLANS.pass90!.avatarSecondsIncluded).toBe(9 * INTERVIEW_SECONDS);
    expect(PACKS).toEqual({ pack_1: 1, pack_3: 3, pack_5: 5 });
  });
});

describe("checkout", () => {
  it("a pass checkout sends the pass's price, the account and the metadata, and returns the url", async () => {
    const { id, token } = await newAccount();
    const sent = stubStripe();
    const r = await call("/billing/passes/pass90/checkout", { method: "POST", token, env: STRIPE });
    expect(r.status).toBe(200);
    expect(r.data).toEqual({ url: "https://checkout.stripe.com/c/pay/cs_test_1" });
    expect(sent).toHaveLength(1);
    expect(sent[0]!.url).toBe(`${STRIPE_API}/checkout/sessions`);
    expect(sent[0]!.auth).toBe("Bearer sk_test_not_real");
    expect(sent[0]!.form).toEqual({
      mode: "payment",
      "line_items[0][price]": "price_pass90",
      "line_items[0][quantity]": "1",
      client_reference_id: id,
      "metadata[kind]": "pass",
      "metadata[pass]": "pass90",
      "metadata[days]": "90",
      "metadata[seconds]": String(9 * INTERVIEW_SECONDS),
      "metadata[packages]": "150",
      success_url: "http://127.0.0.1:57000/?checkout=success",
      cancel_url: "http://127.0.0.1:57000/?checkout=cancel",
    });
  });

  it("needs a user; an unknown pass is a 400; a Stripe failure is a clean 502", async () => {
    const { token } = await newAccount();
    stubStripe({ status: 500, body: { error: { message: "secret detail" } } });
    expect((await call("/billing/passes/pass30/checkout", { method: "POST", env: STRIPE })).status).toBe(401);
    const unknown = await call("/billing/passes/gold/checkout", { method: "POST", token, env: STRIPE });
    expect(unknown.status).toBe(400);
    expect(unknown.data).toEqual({ error: "unknown pass: gold" });
    const failed = await call("/billing/passes/pass30/checkout", { method: "POST", token, env: STRIPE });
    expect(failed.status).toBe(502);
    expect(failed.data).toEqual({ error: "could not start checkout" });
  });

  it("packs are refused without an active pass (402 pass_required) and Stripe is never called", async () => {
    const { id, token } = await newAccount();
    const sent = stubStripe();
    const r = await call("/billing/packs/pack_3/checkout", { method: "POST", token, env: STRIPE });
    expect(r.status).toBe(402);
    expect(r.data).toEqual({ error: "extra interviews are sold only while a pass is active", reason: "pass_required" });
    // an expired pass is no pass
    await setPass(env.DB, id, "pass30", now() - 10, 900, 5);
    expect((await call("/billing/packs/pack_3/checkout", { method: "POST", token, env: STRIPE })).status).toBe(402);
    expect(sent).toHaveLength(0);
    expect((await call("/billing/packs/pack_9/checkout", { method: "POST", token, env: STRIPE })).status).toBe(400);
  });

  it("with an active pass a pack checkout sends the pack's price and seconds", async () => {
    const { id, token } = await newAccount();
    await givePass(id);
    const sent = stubStripe();
    const r = await call("/billing/packs/pack_5/checkout", { method: "POST", token,
      env: { ...STRIPE, TAILOR_APP_URL: "https://sponsorjobs.ai/" } });
    expect(r.status).toBe(200);
    expect(sent[0]!.form).toMatchObject({
      mode: "payment", "line_items[0][price]": "price_pack5", client_reference_id: id,
      "metadata[kind]": "credit_pack", "metadata[pack]": "pack_5", "metadata[seconds]": String(5 * INTERVIEW_SECONDS),
      success_url: "https://sponsorjobs.ai/?checkout=success", cancel_url: "https://sponsorjobs.ai/?checkout=cancel",
    });
  });
});

describe("webhook", () => {
  it("rejects a bad, missing, wrong-secret or stale signature with 400 and grants nothing", async () => {
    const { id, token } = await newAccount();
    const ev = completed(id, { kind: "pass", pass: "pass30" });
    const good = await signPayload(JSON.stringify(ev), WHSEC, now());
    const tampered = good.replace(/v1=(.)/, (_m, c) => `v1=${c === "0" ? "1" : "0"}`);
    for (const r of [
      await deliver(ev, { header: tampered }),
      await deliver(ev, { header: "" }),
      await deliver(ev, { secret: "whsec_someone_else" }),
      await deliver(ev, { t: now() - 301 }),
    ]) {
      expect(r.status).toBe(400);
      expect(r.data).toEqual({ error: "invalid signature" });
    }
    expect((await usage(token)).tier).toBe("free");
  });

  it("a paid pass is granted once even when Stripe delivers the event twice", async () => {
    const { id, token } = await newAccount();
    const ev = completed(id, { kind: "pass", pass: "pass30" });
    const first = await deliver(ev);
    expect(first.status).toBe(200);
    expect(first.data).toEqual({ received: true });
    const u1 = await usage(token);
    expect(u1.tier).toBe("pass30");
    expect(u1.interviews_left).toBe(3);
    expect(u1.packages_left).toBe(60);
    expect(u1.pass_until).toBeGreaterThan(now() + 29 * DAY_SECONDS);
    expect((await deliver(ev)).status).toBe(200);
    // the same session again as async_payment_succeeded also counts once
    expect((await deliver({ ...ev, type: "checkout.session.async_payment_succeeded" })).status).toBe(200);
    expect(await usage(token)).toEqual(u1);
  });

  it("parallel deliveries of one event still grant once", async () => {
    const { id, token } = await newAccount();
    const ev = completed(id, { kind: "pass", pass: "pass90" });
    await Promise.all([deliver(ev), deliver(ev), deliver(ev)]);
    expect((await usage(token)).interviews_left).toBe(9);
  });

  it("a second pass bought while one is active stacks days and allowances", async () => {
    const { id, token } = await newAccount();
    await deliver(completed(id, { kind: "pass", pass: "pass30" }));
    const before = await usage(token);
    await deliver(completed(id, { kind: "pass", pass: "pass90" }));
    const after = await usage(token);
    expect(after.tier).toBe("pass90");                       // the longer pass's name is kept
    expect(after.pass_until).toBe(before.pass_until + 90 * DAY_SECONDS);
    expect(after.interviews_left).toBe(12);
    expect(after.packages_left).toBe(210);
  });

  it("a pack purchase adds never-expiring credit seconds, once", async () => {
    const { id, token } = await newAccount();
    await givePass(id);
    const ev = completed(id, { kind: "credit_pack", pack: "pack_3", seconds: 1 });   // pack id wins over seconds
    await deliver(ev);
    await deliver(ev);
    const row = await env.DB.prepare("SELECT credit_seconds FROM users WHERE user = ?").bind(id).first();
    expect(row).toEqual({ credit_seconds: 3 * INTERVIEW_SECONDS });
    expect((await usage(token)).interviews_left).toBe(3 + 3);
  });

  it("unpaid, non-payment, unknown or unrelated events change nothing but are acknowledged", async () => {
    const { id, token } = await newAccount();
    for (const ev of [
      completed(id, { kind: "pass", pass: "pass30" }, { payment_status: "unpaid" }),
      completed(id, { kind: "pass", pass: "pass30" }, { mode: "subscription" }),
      completed(id, { kind: "pass", pass: "gold" }),
      completed(id, { kind: "pass", pass: "pass30" }, { client_reference_id: null }),
      { ...completed(id, { kind: "pass", pass: "pass30" }), type: "customer.subscription.updated" },
    ]) {
      const r = await deliver(ev);
      expect(r.status).toBe(200);
    }
    expect((await usage(token)).tier).toBe("free");
  });
});
