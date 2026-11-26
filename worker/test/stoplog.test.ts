// Where Autofill and the apply agent stop (src/stoplog.ts, migration 0019): the app's small batches
// at POST /v1/autofill/events, kept 90 days, capped per account a day, grouped for the admin page.
// No personal value is stored: unknown fields are dropped and the texts lose anything that looks
// like an email, a link or a long digit run.
import { describe, expect, it } from "vitest";
import { clean, DAILY_CAP, MAX_BATCH, qnorm, sanitize, stopGroups, stopHosts, stopTypes, stopWrites, storeEvents } from "../src/stoplog";
import { stopsPage } from "../src/admin_pages";
import { call, env, newAccount } from "./helpers";

const ev = (o: Record<string, unknown> = {}) => ({ type: "asked_person", ats: "workday", host: "acme.wd1.myworkdayjobs.com",
  page_kind: "form", question: "Please confirm your veteran status:", options: ["Yes", "No"], reason: "No saved answer fits",
  steps: 7, cost: 0.0412, version: "0.9.1", ...o });

describe("POST /v1/autofill/events", () => {
  it("needs an account", async () => {
    expect((await call("/v1/autofill/events", { body: { events: [ev()] } })).status).toBe(401);
  });

  it("keeps the listed fields only, cleaned and cut; no personal value survives", async () => {
    const { id, token } = await newAccount();
    const r = await call("/v1/autofill/events", { token, body: { events: [ev({
      question: "Is avery.example@mail.test your email? Call +1 (555) 010-2000 or see https://acme.test/x",
      reason: "x".repeat(400), options: Array.from({ length: 12 }, (_, i) => `Option ${i}`),
      name: "Avery Example", email: "avery.example@mail.test", answer: "Male", resume: "lots of text", type: "asked_person" })] } });
    expect(r.status).toBe(200);
    expect(r.data).toMatchObject({ ok: true, kept: 1 });
    const row = await env.DB.prepare("SELECT * FROM autofill_events WHERE account = ?").bind(id).first<Record<string, unknown>>();
    const all = JSON.stringify(row);
    for (const bad of ["avery.example@mail.test", "555", "010-2000", "acme.test", "Avery Example", "lots of text"]) expect(all).not.toContain(bad);
    expect(row!.question).toBe("Is [x] your email? Call [x] or see [link]");
    expect(String(row!.reason).length).toBe(160);
    expect(JSON.parse(String(row!.options)).length).toBe(8);
    expect(Object.keys(row!).sort()).toEqual(["account", "at", "ats", "cost", "day", "host", "id", "options", "page_kind", "qnorm",
      "question", "reason", "steps", "type", "version"]);
  });

  it("drops an unknown type, takes at most 50 a request and 300 an account a day", async () => {
    const { id, token } = await newAccount();
    const r = await call("/v1/autofill/events", { token, body: { events: [ev({ type: "drop_table" }), ...Array.from({ length: 60 }, () => ev())] } });
    expect(r.data).toMatchObject({ ok: true, kept: MAX_BATCH - 1, dropped: 1 });
    const day = new Date().toISOString().slice(0, 10);
    await env.DB.prepare("UPDATE autofill_events SET day = day WHERE account = ?").bind(id).run();
    // fill the day to the cap, then the rest is turned away
    for (let i = 0; i < 6; i++) await call("/v1/autofill/events", { token, body: { events: Array.from({ length: 50 }, () => ev()) } });
    const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM autofill_events WHERE account = ? AND day = ?").bind(id, day).first<{ n: number }>();
    expect(n!.n).toBe(DAILY_CAP);
    const over = await call("/v1/autofill/events", { token, body: { events: [ev()] } });
    expect(over.data).toMatchObject({ ok: true, kept: 0, capped: 1 });
  });

  it("prunes rows past 90 days on a write", async () => {
    const { id } = await newAccount();
    const now = Math.floor(Date.now() / 1000);
    await env.DB.prepare("INSERT INTO autofill_events(account, at, day, type) VALUES(?, ?, '2000-01-01', 'other')").bind(id, now - 91 * 86_400).run();
    await storeEvents(env.DB, id, [ev()], now, "2026-11-26", () => 0);           // the prune always runs here
    const old = await env.DB.prepare("SELECT COUNT(*) AS n FROM autofill_events WHERE at < ?").bind(now - 90 * 86_400).first<{ n: number }>();
    expect(old!.n).toBe(0);
  });
});

describe("the event rules", () => {
  it("clean and qnorm", () => {
    expect(clean("  a@b.co   called 07700 900123 ", 100)).toBe("[x] called [x]");
    expect(qnorm("Veteran Status (please select one): 2024")).toBe("veteran status please select one");
    expect(sanitize({ type: "nope" })).toBeNull();
    expect(sanitize({ type: "captcha", page_kind: "weird" })!.page_kind).toBe("other");
  });

  it("groups by question (else host), seen most first, with the newest reason; and the admin page shows them", async () => {
    const acct = "acct-stops";
    const now = Math.floor(Date.now() / 1000), day = new Date().toISOString().slice(0, 10);
    await storeEvents(env.DB, acct, [ev(), ev(), ev({ reason: "Newest reason" })], now, day, () => 1);
    await storeEvents(env.DB, acct, [ev({ type: "captcha", question: "", host: "jobs.example.org", ats: "careersite" })], now, day, () => 1);
    const groups = await stopGroups(env.DB, now);
    const vet = groups.find((g) => g.type === "asked_person" && g.qnorm === "please confirm your veteran status");
    expect(vet && vet.n30).toBeGreaterThanOrEqual(3);
    expect(vet!.reason).toBe("Newest reason");
    expect(groups[0]!.n30).toBeGreaterThanOrEqual(groups[groups.length - 1]!.n30);
    const types = await stopTypes(env.DB, now);
    expect(types.find((t) => t.type === "captcha")!.n7).toBeGreaterThanOrEqual(1);
    const hosts = await stopHosts(env.DB, now);
    const writes = await stopWrites(env.DB, day, day);
    expect(writes.today).toBeGreaterThanOrEqual(4);
    const html = stopsPage({ types, groups, hosts, type: "", now, writes });
    expect(html).toContain("Autofill stops");
    expect(html).toContain("Please confirm your veteran status:");
    expect(html).toContain("Newest reason");
    expect(html).toContain("Asked the person");
    expect(html).not.toMatch(/—|–/);
  });
});
