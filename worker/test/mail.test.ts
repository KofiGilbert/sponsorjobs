// Mail aliases for the apply agent: choosing the alias, the e-mail handler on a fake
// ForwardableEmailMessage (a raw MIME string as a stream), the detection rules ported from
// inbox/detect.py, the inbox cursor, and the exact body handed to Resend. No network: Resend is a
// stubbed fetch, and the message never leaves the test runtime.
import { afterEach, describe, expect, it, vi } from "vitest";
import worker from "../src/index";
import type { Env } from "../src/env";
import {
  aliasLocalFor, detect, EVENT_TTL_SECONDS, foldLocal, htmlToText, INBOX_LIMIT, kindOf, localOf, otpCode,
  passwordResetLink, RESERVED_LOCALS, verificationLink,
} from "../src/mail";
import { RESEND_API } from "../src/mailer";
import { call, env, newAccount, signedInAccount } from "./helpers";

afterEach(() => vi.restoreAllMocks());

const MAIL: Partial<Env> = { RESEND_API_KEY: "re_test_not_real" };

interface Sent { auth: string | null; body: any }

/** Stub fetch as Resend; records every e-mail the Worker sent. */
function stubResend(status = 200) {
  const sent: Sent[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const req = new Request(input as RequestInfo, init as RequestInit);
    if (req.url !== RESEND_API) throw new Error(`unexpected fetch ${req.url}`);
    sent.push({ auth: req.headers.get("Authorization"), body: await req.json() });
    return new Response(JSON.stringify({ id: "email_1" }), { status });
  });
  return sent;
}

const createAlias = (token: string, name?: string, extra: Partial<Env> = {}) =>
  call("/mail/alias", { token, body: name === undefined ? {} : { name }, env: extra });
const getAlias = (token: string) => call("/mail/alias", { token });
const dropAlias = (token: string) => call("/mail/alias", { method: "DELETE", token });
const inbox = (token: string, after?: number) =>
  call(`/mail/inbox${after === undefined ? "" : `?after=${after}`}`, { token });

/** A signed-in account with an alias; returns the alias's local part too. */
async function student(name = "Zoe Muller") {
  const a = await signedInAccount();
  const r = await createAlias(a.token, name);
  expect(r.status).toBe(200);
  return { ...a, alias: r.data.alias as string, local: (r.data.alias as string).split("@")[0]! };
}

/** A fake ForwardableEmailMessage over a raw MIME string, as Email Routing would hand it over. */
function fakeMessage(raw: string, to: string, from = "bounce@mailer.example") {
  const rejected: string[] = [];
  const forwarded: string[] = [];
  const headers = new Headers();
  for (const line of raw.split(/\r?\n/)) {
    if (!line) break;
    const i = line.indexOf(":");
    if (i > 0 && /^[A-Za-z-]+$/.test(line.slice(0, i))) headers.append(line.slice(0, i), line.slice(i + 1).trim());
  }
  const message = {
    from, to, headers, rawSize: raw.length,
    raw: new Blob([raw]).stream(),
    setReject: (reason: string) => { rejected.push(reason); },
    forward: async (rcpt: string) => { forwarded.push(rcpt); },
    reply: async () => {},
  } as unknown as ForwardableEmailMessage;
  return { message, rejected, forwarded };
}

const ctx = { waitUntil() {}, passThroughOnException() {}, props: {} } as unknown as ExecutionContext;

/** Deliver one raw message to `to` through the Worker's email handler. */
async function deliver(raw: string, to: string, extra: Partial<Env> = MAIL, from?: string) {
  const m = fakeMessage(raw, to, from);
  await worker.email(m.message, { ...env, ...extra } as Env, ctx);
  return m;
}

const events = (user: string) =>
  env.DB.prepare("SELECT kind, code, link, subject, sender FROM mail_events WHERE user = ? ORDER BY id").bind(user).all();

const plain = (from: string, subject: string, text: string) =>
  `From: ${from}\r\nTo: someone@sponsorjobs.ai\r\nSubject: ${subject}\r\n` +
  `Content-Type: text/plain; charset=utf-8\r\n\r\n${text}\r\n`;

const CODE_MAIL = plain("Workday <noreply@myworkday.com>", "Your verification code",
  "Hi,\n\nYour verification code is 482913. It expires in 10 minutes.\n\nThe Workday team\n");

// ---- choosing the alias ---------------------------------------------------------------------- //

describe("alias local part", () => {
  it("folds a name to ascii first.last", () => {
    expect(foldLocal("Zoë Ångström-Müller")).toBe("zoe.muller");
    expect(foldLocal("  Kofi   GILBERT ")).toBe("kofi.gilbert");
    expect(foldLocal("Jean-Luc Picard")).toBe("jean.picard");
    expect(foldLocal("Madonna")).toBe("madonna");
    expect(foldLocal("kofigilbert007")).toBe("kofigilbert007");
    expect(foldLocal("O'Brien, Pat")).toBe("o.pat");
  });

  it("falls back to 'student' when nothing usable is left, and clamps to 40 characters", () => {
    expect(foldLocal("")).toBe("student");
    expect(foldLocal("Al")).toBe("student");
    expect(foldLocal("!!! ???")).toBe("student");
    expect(foldLocal("江 澤民")).toBe("student");          // no ASCII letters at all
    const long = foldLocal("Abcdefghijklmnopqrstuvwxyz Abcdefghijklmnopqrstuvwxyz");
    expect(long).toHaveLength(40);
    expect(long).toMatch(/^[a-z.]+$/);
  });

  it("refuses reserved and company locals", () => {
    for (const r of ["support", "Admin", "no-reply", "No Reply", "noreply", "do.not.reply", "postmaster", "abuse",
                     "hello", "info", "billing", "security", "team", "kofi", "api"]) {
      expect(foldLocal(r), r).toBe("student");
    }
    expect(RESERVED_LOCALS.has("student")).toBe(false);                // the fallback itself is never reserved
  });

  it("adds a 2 to 4 digit number on collision, keeping 40 characters at most", async () => {
    const used = new Set(["zoe.muller"]);
    const taken = async (l: string) => used.has(l);
    const a = await aliasLocalFor("Zoe Muller", taken);
    expect(a).toMatch(/^zoe\.muller\d{2,4}$/);
    expect(await aliasLocalFor("Pat Lee", taken)).toBe("pat.lee");   // free: no number
    const crowded = await aliasLocalFor("Abcdefghijklmnopqrstuvwxyz Abcdefghijklmnopqrstuvwxyz",
                                        async (l) => l.length === 40 && !/\d/.test(l));
    expect(crowded.length).toBeLessThanOrEqual(40);
    expect(crowded).toMatch(/\d{2,4}$/);
  });

  it("gives up cleanly when every candidate is taken", async () => {
    await expect(aliasLocalFor("Zoe Muller", async () => true)).rejects.toThrow("no free alias");
  });

  it("reads the local part of a recipient case-insensitively, without a +tag", () => {
    expect(localOf("Zoe.Muller+workday@SponsorJobs.ai")).toBe("zoe.muller");
    expect(localOf("zoe.muller@sponsorjobs.ai")).toBe("zoe.muller");
    expect(localOf("zoe.muller")).toBe("zoe.muller");
  });
});

describe("POST/GET/DELETE /mail/alias", () => {
  it("needs a signed-in account: 401 without a token, 409 sign_in_first for the device account", async () => {
    expect((await call("/mail/alias", { body: { name: "x" } })).status).toBe(401);
    expect((await call("/mail/alias")).status).toBe(401);
    expect((await call("/mail/inbox")).status).toBe(401);
    const { token } = await newAccount();
    const r = await createAlias(token, "Zoe Muller");
    expect(r.status).toBe(409);
    expect(r.data).toEqual({ error: "sign_in_first" });
    expect((await getAlias(token)).data).toEqual({ alias: null, forward_to: null });
  });

  it("makes first.last@sponsorjobs.ai forwarding to the verified e-mail, never one the client names", async () => {
    const a = await signedInAccount();
    const r = await call("/mail/alias", { token: a.token, body: { name: "Zoë Ångström-Müller", forward_to: "evil@example.com" } });
    expect(r.status).toBe(200);
    expect(r.data.alias).toMatch(/^zoe\.muller(\d{2,4})?@sponsorjobs\.ai$/);
    expect(r.data.forward_to).toBe(a.email);
    expect((await getAlias(a.token)).data).toEqual(r.data);
  });

  it("is idempotent: asking again returns the same alias; a second account with the same name gets a number", async () => {
    const a = await signedInAccount();
    const first = (await createAlias(a.token, "Pat Lee")).data;
    expect((await createAlias(a.token, "Pat Lee")).data).toEqual(first);
    expect((await createAlias(a.token, "Someone Else")).data).toEqual(first);  // the name no longer matters
    const b = await signedInAccount();
    const second = (await createAlias(b.token, "Pat Lee")).data;
    expect(second.alias).not.toBe(first.alias);
    const [fl] = first.alias.split("@");
    expect(second.alias).toMatch(new RegExp(`^${fl.replace(/\d+$/, "")}\\d{2,4}@sponsorjobs\\.ai$`));
    expect(second.forward_to).toBe(b.email);
  });

  it("a reserved name becomes student<n>; no name folds the e-mail's own local part", async () => {
    const a = await signedInAccount("Support.Desk@example.com");
    expect((await createAlias(a.token, "Support")).data.alias).toMatch(/^student(\d{2,4})?@sponsorjobs\.ai$/);
    const b = await signedInAccount(`maria.lopez.${Date.now()}@example.com`);
    expect((await createAlias(b.token)).data.alias).toMatch(/^maria\.\d+(\d{2,4})?@sponsorjobs\.ai$/);
  });

  it("DELETE releases the alias and its events; mail to it then bounces", async () => {
    const s = await student("Rae Quinn");
    const sent = stubResend();
    await deliver(CODE_MAIL, s.alias);
    expect((await events(s.id)).results).toHaveLength(1);
    expect(sent).toHaveLength(1);
    const d = await dropAlias(s.token);
    expect(d.status).toBe(200);
    expect(d.data).toEqual({ alias: null, forward_to: null });
    expect((await getAlias(s.token)).data).toEqual({ alias: null, forward_to: null });
    expect((await events(s.id)).results).toHaveLength(0);
    const m = await deliver(CODE_MAIL, s.alias);
    expect(m.rejected).toEqual(["No such address"]);
    expect(sent).toHaveLength(1);
    // the local is free again
    const again = await createAlias(s.token, "Rae Quinn");
    expect(again.data.alias).toBe(s.alias);
  });
});

// ---- the e-mail handler ---------------------------------------------------------------------- //

describe("receiving mail", () => {
  it("a code mail: the code is kept for the agent and the mail is forwarded with reply-to the sender", async () => {
    const s = await student();
    const sent = stubResend();
    const m = await deliver(CODE_MAIL, s.alias);
    expect(m.rejected).toEqual([]);
    expect(m.forwarded).toEqual([]);                           // through Resend, not Cloudflare's forward()
    const { results } = await events(s.id);
    expect(results).toEqual([{ kind: "code", code: "482913", link: null, subject: "Your verification code",
                               sender: "Workday <noreply@myworkday.com>" }]);
    expect(sent).toHaveLength(1);
    const b = sent[0]!.body;
    expect(sent[0]!.auth).toBe("Bearer re_test_not_real");
    expect(b.from).toBe("\"Workday via SponsorJobs\" <no-reply@sponsorjobs.ai>");
    expect(b.reply_to).toBe("noreply@myworkday.com");
    expect(b.to).toEqual([s.email]);
    expect(b.subject).toBe("Your verification code");
    expect(b.text.startsWith(`Forwarded by SponsorJobs from ${s.alias}; attachments are not forwarded\n\n`)).toBe(true);
    expect(b.text).toContain("Your verification code is 482913. It expires in 10 minutes.");
    expect(b.html).toBeUndefined();
    expect(b.attachments).toBeUndefined();
    const inb = await inbox(s.token);
    expect(inb.data.events).toHaveLength(1);
    expect(inb.data.events[0]).toMatchObject({ kind: "code", code: "482913", link: null,
                                                subject: "Your verification code" });
    expect(inb.data.events[0].at).toBeGreaterThan(Math.floor(Date.now() / 1000) - 5);
  });

  it("a verification-link mail (text + html): the link is kept, both bodies are forwarded", async () => {
    const s = await student();
    const sent = stubResend();
    const link = "https://acme.greenhouse.io/verify?token=abc123&u=9";
    const raw =
      "From: Acme Careers <jobs@greenhouse-mail.io>\r\nSubject: Please verify your email address\r\n" +
      "Content-Type: multipart/alternative; boundary=\"b1\"\r\n\r\n" +
      "--b1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" +
      `Thanks for applying. Verify your email address: ${link}\r\n\r\n` +
      "--b1\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" +
      `<html><body><p>Thanks for applying.</p><p><a href="${link.replace("&", "&amp;")}">Verify your email address</a></p></body></html>\r\n` +
      "--b1--\r\n";
    await deliver(raw, s.alias);
    expect((await events(s.id)).results).toEqual([{ kind: "verify", code: null, link,
      subject: "Please verify your email address", sender: "Acme Careers <jobs@greenhouse-mail.io>" }]);
    const b = sent[0]!.body;
    expect(b.from).toBe("\"Acme Careers via SponsorJobs\" <no-reply@sponsorjobs.ai>");
    expect(b.reply_to).toBe("jobs@greenhouse-mail.io");
    expect(b.text).toContain(link);
    expect(b.html).toContain("<a href=");
    expect(b.html.startsWith("<p style=")).toBe(true);
    expect(b.html).toContain(`Forwarded by SponsorJobs from ${s.alias}; attachments are not forwarded`);
  });

  it("an html-only verification mail: the link comes out of the href, entities decoded", async () => {
    const s = await student();
    stubResend();
    const raw =
      "From: Lever <no-reply@hire.lever.co>\r\nSubject: Confirm your application\r\n" +
      "Content-Type: text/html; charset=utf-8\r\n\r\n" +
      "<html><body><p>Please confirm your application.</p>" +
      "<a href=\"https://hire.lever.co/confirm?id=42&amp;sig=zz\">Confirm</a></body></html>\r\n";
    await deliver(raw, s.alias);
    expect((await events(s.id)).results).toEqual([{ kind: "verify", code: null,
      link: "https://hire.lever.co/confirm?id=42&sig=zz", subject: "Confirm your application",
      sender: "Lever <no-reply@hire.lever.co>" }]);
  });

  it("a password-reset mail: kind reset with the reset link", async () => {
    const s = await student();
    stubResend();
    const raw = plain("Workday <noreply@myworkday.com>", "Reset your password",
      "We got a request to reset your password.\n\nhttps://acme.wd5.myworkdayjobs.com/reset?t=9f8e\n\nIf this was not you, ignore this.");
    await deliver(raw, s.alias);
    expect((await events(s.id)).results).toEqual([{ kind: "reset", code: null,
      link: "https://acme.wd5.myworkdayjobs.com/reset?t=9f8e", subject: "Reset your password",
      sender: "Workday <noreply@myworkday.com>" }]);
  });

  it("an unrelated mail is forwarded and nothing is stored", async () => {
    const s = await student();
    const sent = stubResend();
    const raw = plain("Acme Careers <jobs@acme.example>", "Thanks for applying to Acme",
      "We received your application for Software Engineer (req 20331). We will be in touch.\n\n(c) 2026 Acme");
    const m = await deliver(raw, s.alias);
    expect(m.rejected).toEqual([]);
    expect((await events(s.id)).results).toEqual([]);
    expect(sent).toHaveLength(1);
    expect(sent[0]!.body.subject).toBe("Thanks for applying to Acme");
    expect((await inbox(s.token)).data).toEqual({ events: [], cursor: 0 });
  });

  it("an unknown alias is rejected: nothing stored, nothing forwarded", async () => {
    const sent = stubResend();
    const before = (await env.DB.prepare("SELECT COUNT(*) AS n FROM mail_events").first<{ n: number }>())!.n;
    for (const to of ["nobody.here@sponsorjobs.ai", "support@sponsorjobs.ai", "@sponsorjobs.ai", ""]) {
      const m = await deliver(CODE_MAIL, to);
      expect(m.rejected, to).toEqual(["No such address"]);
    }
    expect(sent).toHaveLength(0);
    expect((await env.DB.prepare("SELECT COUNT(*) AS n FROM mail_events").first<{ n: number }>())!.n).toBe(before);
  });

  it("a plus-tagged, mixed-case recipient reaches the alias", async () => {
    const s = await student();
    const sent = stubResend();
    const to = `${s.local.toUpperCase()}+workday@SponsorJobs.ai`;
    const m = await deliver(CODE_MAIL, to);
    expect(m.rejected).toEqual([]);
    expect((await events(s.id)).results).toHaveLength(1);
    expect(sent[0]!.body.to).toEqual([s.email]);
  });

  it("attachments are dropped; the notice line says so", async () => {
    const s = await student();
    const sent = stubResend();
    const raw =
      "From: HR <hr@acme.example>\r\nSubject: Your code and the brochure\r\n" +
      "Content-Type: multipart/mixed; boundary=\"m1\"\r\n\r\n" +
      "--m1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nYour one-time code: 7741\r\n\r\n" +
      "--m1\r\nContent-Type: application/pdf; name=\"brochure.pdf\"\r\nContent-Disposition: attachment; filename=\"brochure.pdf\"\r\n" +
      "Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n--m1--\r\n";
    await deliver(raw, s.alias);
    expect((await events(s.id)).results[0]).toMatchObject({ kind: "code", code: "7741" });
    const b = sent[0]!.body;
    expect(b.attachments).toBeUndefined();
    expect(b.text).toContain("attachments are not forwarded");
    expect(b.text).toContain("Your one-time code: 7741");
    expect(JSON.stringify(b)).not.toContain("JVBERi0xLjQK");
  });

  it("when the forward fails the event is still kept, and no code or body is ever logged", async () => {
    const s = await student();
    stubResend(500);
    const logs: string[] = [];
    vi.spyOn(console, "error").mockImplementation((...a: unknown[]) => { logs.push(a.map(String).join(" ")); });
    vi.spyOn(console, "log").mockImplementation((...a: unknown[]) => { logs.push(a.map(String).join(" ")); });
    const m = await deliver(CODE_MAIL, s.alias);
    expect(m.rejected).toEqual([]);
    expect((await events(s.id)).results).toEqual([expect.objectContaining({ kind: "code", code: "482913" })]);
    expect(logs.some((l) => l.includes("mail forward failed"))).toBe(true);
    for (const l of logs) {
      expect(l).not.toContain("482913");
      expect(l).not.toContain("expires in 10 minutes");
      expect(l).not.toContain("myworkday");
    }
  });

  it("without a mailer the event is kept and nothing is sent", async () => {
    const s = await student();
    const spy = vi.spyOn(globalThis, "fetch");
    vi.spyOn(console, "error").mockImplementation(() => {});
    await deliver(CODE_MAIL, s.alias, { RESEND_API_KEY: "" });
    expect((await events(s.id)).results).toHaveLength(1);
    expect(spy).not.toHaveBeenCalled();
  });

  it("a forward counts against the Resend daily figure the dashboard shows", async () => {
    const s = await student();
    stubResend();
    const day = new Date().toISOString().slice(0, 10);
    const n = async () => (await env.DB.prepare("SELECT COALESCE(SUM(n), 0) AS n FROM email_sends WHERE day = ?")
      .bind(day).first<{ n: number }>())!.n;
    const before = await n();
    await deliver(CODE_MAIL, s.alias);
    expect(await n()).toBe(before + 1);
  });

  it("bodies are capped at 1 MiB and the subject at 200 characters in the event", async () => {
    const s = await student();
    const sent = stubResend();
    const subject = "Your code " + "x".repeat(300);
    const raw = plain("A <a@b.example>", subject, "code 123456\n" + "y".repeat(1_200_000));
    await deliver(raw, s.alias);
    const row = (await events(s.id)).results[0] as any;
    expect(row.subject).toHaveLength(200);
    expect(sent[0]!.body.text.length).toBe(1 << 20);
    expect(sent[0]!.body.subject).toBe(subject);           // the forward keeps the whole subject
  });

  it("the sender's display name cannot smuggle quotes or another address into our From line", async () => {
    const s = await student();
    const sent = stubResend();
    const raw = plain("\"Evil\" <x@y.example>, ceo@sponsorjobs.ai\" <evil@attacker.example>", "code 4477", "code 4477");
    await deliver(raw, s.alias);
    const from: string = sent[0]!.body.from;
    expect(from).toMatch(/^"[^"<>]* via SponsorJobs" <no-reply@sponsorjobs\.ai>$/);
  });
});

// ---- the detection rules (the port of inbox/detect.py; the same cases as tests/test_inbox_detect.py) //

describe("detection rules", () => {
  it("otp_code: a 4 to 8 digit run near a cue; the subject wins", () => {
    expect(otpCode("482913 is your verification code", "Call us on 020 7946 0123")).toBe("482913");
    expect(otpCode("Welcome", "Your one-time PIN is 7741. Order #55512345 shipped in 2026.")).toBe("7741");
    expect(otpCode("Welcome", "Thanks for applying in 2026. Req 20331. Phone (415) 555-2671.")).toBe("");
    expect(otpCode("Welcome", "Your account number is 12345678, see you at 1 Main St, CA 94105")).toBe("");
    expect(otpCode("Welcome", "Enter 2026 to confirm")).toBe("");                 // a year, blanked
    expect(otpCode("Welcome", "Security code: 123-456")).toBe("");                  // hyphenated: not a run
  });

  it("verification_link prefers a link that looks like the action, else the first link", () => {
    const body = "See https://acme.example/jobs and then https://acme.example/confirm?t=1.";
    expect(verificationLink(body)).toBe("https://acme.example/confirm?t=1");
    expect(verificationLink("Only https://acme.example/jobs, thanks.")).toBe("https://acme.example/jobs");
    expect(verificationLink("no links")).toBe("");
  });

  it("password_reset_link: the URL says so, or the words around it do", () => {
    expect(passwordResetLink("Go to https://acme.example/reset?x=1.")).toBe("https://acme.example/reset?x=1");
    expect(passwordResetLink("Forgot your password? Click https://acme.example/a/b to continue."))
      .toBe("https://acme.example/a/b");
    expect(passwordResetLink("Welcome aboard: https://acme.example/a/b")).toBe("");
  });

  it("kind_of: reset beats code beats verify; nothing otherwise", () => {
    expect(kindOf("Reset your password", "code 1234 https://acme.example/reset")).toBe("reset");
    expect(kindOf("Your code", "Your verification code is 482913")).toBe("code");
    expect(kindOf("Verify your email", "Please confirm: https://acme.example/verify")).toBe("verify");
    expect(kindOf("Verify your email", "no link here")).toBe("");
    expect(kindOf("Newsletter", "Hello https://acme.example/news")).toBe("");
    expect(detect("Your code", "Your verification code is 482913")).toEqual({ kind: "code", code: "482913", link: null });
  });

  it("htmlToText keeps link targets and drops markup", () => {
    const t = htmlToText("<style>p{}</style><p>Hi&nbsp;there &amp; welcome</p><a href=\"https://a.example/x?a=1&amp;b=2\">Go</a>");
    expect(t).toContain("Hi there & welcome");
    expect(t).toContain("https://a.example/x?a=1&b=2");
    expect(t).not.toContain("<");
  });
});

// ---- GET /mail/inbox -------------------------------------------------------------------------- //

describe("GET /mail/inbox", () => {
  const seed = (user: string, n: number, at = Math.floor(Date.now() / 1000)) =>
    env.DB.batch(Array.from({ length: n }, (_, i) =>
      env.DB.prepare("INSERT INTO mail_events(user, kind, code, link, subject, sender, at) VALUES(?, 'code', ?, NULL, ?, 's', ?)")
        .bind(user, String(100000 + i), `m${i}`, at)));

  it("lists this account's events oldest first with cursors; passing after acknowledges them", async () => {
    const s = await student();
    await seed(s.id, 3);
    const r = await inbox(s.token);
    expect(r.status).toBe(200);
    expect(r.data.events.map((e: any) => e.subject)).toEqual(["m0", "m1", "m2"]);
    const [c0, c1, c2] = r.data.events.map((e: any) => e.cursor);
    expect(c0 < c1 && c1 < c2).toBe(true);
    expect(r.data.cursor).toBe(c2);
    const r2 = await inbox(s.token, c1);
    expect(r2.data.events.map((e: any) => e.cursor)).toEqual([c2]);
    expect(r2.data.cursor).toBe(c2);
    const r3 = await inbox(s.token, c2);
    expect(r3.data).toEqual({ events: [], cursor: c2 });
    expect((await events(s.id)).results).toHaveLength(0);          // acknowledged rows are gone
    // a bad or missing after is 0: nothing acknowledged
    await seed(s.id, 1);
    expect((await call("/mail/inbox?after=garbage", { token: s.token })).data.events).toHaveLength(1);
    expect((await call("/mail/inbox?after=-5", { token: s.token })).data.events).toHaveLength(1);
  });

  it("another account never sees them, and cannot acknowledge them", async () => {
    const a = await student();
    const b = await student("Other Person");
    await seed(a.id, 2);
    const rb = await inbox(b.token, 1_000_000_000);
    expect(rb.data).toEqual({ events: [], cursor: 1_000_000_000 });
    expect((await inbox(a.token)).data.events).toHaveLength(2);
  });

  it("events older than 30 minutes are dropped on every read", async () => {
    const s = await student();
    const now = Math.floor(Date.now() / 1000);
    await seed(s.id, 1, now - EVENT_TTL_SECONDS - 1);
    await seed(s.id, 1, now - EVENT_TTL_SECONDS + 60);
    const r = await inbox(s.token);
    expect(r.data.events).toHaveLength(1);
    expect((await events(s.id)).results).toHaveLength(1);
  });

  it("returns at most 50; the rest come after acknowledging", async () => {
    const s = await student();
    await seed(s.id, INBOX_LIMIT + 10);
    const r = await inbox(s.token);
    expect(r.data.events).toHaveLength(INBOX_LIMIT);
    const r2 = await inbox(s.token, r.data.cursor);
    expect(r2.data.events).toHaveLength(10);
  });
});
