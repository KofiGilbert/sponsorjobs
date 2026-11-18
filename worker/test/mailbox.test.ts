// The in-app mailbox: every message to a known alias is kept in mail_messages until the person's
// app downloads it (GET /mail/messages?after=<cursor> acknowledges and deletes), 30 days at most;
// the copy to the person's own e-mail follows the alias's notify_mode (forward, notice, off).
// No network: Resend is a stubbed fetch. Made-up people and companies only.
import { afterEach, describe, expect, it, vi } from "vitest";
import worker from "../src/index";
import type { Env } from "../src/env";
import { companyOf, fitBodies, labelOf, MESSAGE_TTL_SECONDS, MESSAGES_LIMIT, noticeFor } from "../src/mail";
import { RESEND_API } from "../src/mailer";
import { call, env, signedInAccount } from "./helpers";

afterEach(() => vi.restoreAllMocks());

const MAIL: Partial<Env> = { RESEND_API_KEY: "re_test_not_real" };

function stubResend(status = 200) {
  const sent: any[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const req = new Request(input as RequestInfo, init as RequestInit);
    if (req.url !== RESEND_API) throw new Error(`unexpected fetch ${req.url}`);
    sent.push(await req.json());
    return new Response(JSON.stringify({ id: "email_1" }), { status });
  });
  return sent;
}

async function student(name = "Zoe Muller") {
  const a = await signedInAccount();
  const r = await call("/mail/alias", { token: a.token, body: { name } });
  expect(r.status).toBe(200);
  return { ...a, alias: r.data.alias as string };
}

const ctx = { waitUntil() {}, passThroughOnException() {}, props: {} } as unknown as ExecutionContext;

async function deliver(raw: string, to: string, extra: Partial<Env> = MAIL) {
  const rejected: string[] = [];
  const message = {
    from: "bounce@mailer.example", to, headers: new Headers(), rawSize: raw.length,
    raw: new Blob([raw]).stream(),
    setReject: (reason: string) => { rejected.push(reason); },
    forward: async () => {}, reply: async () => {},
  } as unknown as ForwardableEmailMessage;
  await worker.email(message, { ...env, ...extra } as Env, ctx);
  return rejected;
}

const plain = (from: string, subject: string, text: string) =>
  `From: ${from}\r\nTo: someone@sponsorjobs.ai\r\nSubject: ${subject}\r\n` +
  `Content-Type: text/plain; charset=utf-8\r\n\r\n${text}\r\n`;

const RECEIVED = plain("Acme Robotics Recruiting <no-reply@greenhouse-mail.io>", "Thank you for applying to Acme Robotics",
  "Hi Zoe,\n\nThank you for applying to the Data Engineer role at Acme Robotics. We have received your application.\n" +
  "If selected, we will reach out to schedule an interview.\n\nAcme Robotics Recruiting\n");
const REJECTED = plain("Borealis Labs <careers@borealislabs.example>", "Your application to Borealis Labs",
  "Dear Zoe,\n\nThank you for your interest in Borealis Labs. Unfortunately, we have decided to pursue other candidates " +
  "for the Analyst position.\n\nBorealis Labs\n");
const CODE_MAIL = plain("Workday <noreply@myworkday.com>", "Your verification code",
  "Your verification code is 482913. It expires in 10 minutes.\n");

const messages = (token: string, q = "") => call(`/mail/messages${q}`, { token });
const rows = (user: string) =>
  env.DB.prepare("SELECT id, local, sender, sender_name, subject, text, html, size, has_attachments, attachment_names " +
                 "FROM mail_messages WHERE user = ? ORDER BY id").bind(user).all<any>();
const seed = (user: string, n: number, at = Math.floor(Date.now() / 1000), size = 10) =>
  env.DB.batch(Array.from({ length: n }, (_, i) =>
    env.DB.prepare("INSERT INTO mail_messages(user, local, sender, sender_name, subject, text, html, received_at, size) " +
                   "VALUES(?, 'x', 'hr@acme.example', 'Acme', ?, 'body', NULL, ?, ?)").bind(user, `m${i}`, at, size)));

describe("storing a received message", () => {
  it("keeps one row per message to a known alias: sender, name, subject, text, and no event for plain mail", async () => {
    const s = await student();
    stubResend();
    expect(await deliver(RECEIVED, s.alias)).toEqual([]);
    const { results } = await rows(s.id);
    expect(results).toHaveLength(1);
    expect(results[0]).toMatchObject({
      local: s.alias.split("@")[0], sender: "no-reply@greenhouse-mail.io", sender_name: "Acme Robotics Recruiting",
      subject: "Thank you for applying to Acme Robotics", html: null, has_attachments: 0, attachment_names: "[]",
    });
    expect(results[0].text).toContain("We have received your application.");
    expect(results[0].size).toBeGreaterThan(50);
    const ev = await env.DB.prepare("SELECT COUNT(*) AS n FROM mail_events WHERE user = ?").bind(s.id).first<{ n: number }>();
    expect(ev!.n).toBe(0);
  });

  it("a code mail gets both: the 30-minute event for the agent and the stored message", async () => {
    const s = await student();
    stubResend();
    await deliver(CODE_MAIL, s.alias);
    const ev = await env.DB.prepare("SELECT kind, code FROM mail_events WHERE user = ?").bind(s.id).all();
    expect(ev.results).toEqual([{ kind: "code", code: "482913" }]);
    expect((await rows(s.id)).results).toHaveLength(1);
  });

  it("keeps the html body, and attachment names without their content", async () => {
    const s = await student();
    stubResend();
    const raw =
      "From: HR <hr@acme.example>\r\nSubject: Offer details\r\n" +
      "Content-Type: multipart/mixed; boundary=\"m1\"\r\n\r\n" +
      "--m1\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Please see the <b>attached</b> file.</p>\r\n\r\n" +
      "--m1\r\nContent-Type: application/pdf; name=\"details.pdf\"\r\nContent-Disposition: attachment; filename=\"details.pdf\"\r\n" +
      "Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n--m1--\r\n";
    await deliver(raw, s.alias);
    const r = (await rows(s.id)).results[0];
    expect(r.html).toContain("<b>attached</b>");
    expect(r.text).toContain("Please see the attached file.");
    expect(r.has_attachments).toBe(1);
    expect(JSON.parse(r.attachment_names)).toEqual(["details.pdf"]);
    expect(JSON.stringify(r)).not.toContain("JVBERi0xLjQK");
    const got = (await messages(s.token)).data.messages[0];
    expect(got.has_attachments).toBe(true);
    expect(got.attachment_names).toEqual(["details.pdf"]);
  });

  it("an unknown alias stores nothing", async () => {
    stubResend();
    expect(await deliver(RECEIVED, "nobody.here@sponsorjobs.ai")).toEqual(["No such address"]);
    const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM mail_messages WHERE local = 'nobody.here'").first<{ n: number }>();
    expect(n!.n).toBe(0);
  });

  it("caps each body at 1 MiB and keeps text and html together inside one D1 row", async () => {
    const big = fitBodies("t".repeat(1_500_000), "h".repeat(1_500_000));
    expect(big.text.length).toBe(1 << 20);
    expect(big.html).toBeNull();                         // the two would not fit one row: text wins
    expect(big.size).toBe(1 << 20);
    const ok = fitBodies("hello", "<p>hello</p>");
    expect(ok).toEqual({ text: "hello", html: "<p>hello</p>", size: 17 });
    const wide = fitBodies("é".repeat(1 << 20), undefined);   // 2 bytes each: cut by bytes, not characters
    expect(wide.size).toBeLessThanOrEqual(1_800_000);
    const s = await student();
    stubResend();
    await deliver(plain("A <a@b.example>", "Long", "y".repeat(1_200_000)), s.alias);
    expect((await rows(s.id)).results[0].text.length).toBe(1 << 20);
  });

  it("a storage failure still records the event and forwards the whole message, and logs no body", async () => {
    const s = await student();
    await call("/mail/alias/notify", { token: s.token, body: { mode: "notice" } });
    const sent = stubResend();
    const logs: string[] = [];
    vi.spyOn(console, "error").mockImplementation((...a: unknown[]) => { logs.push(a.map(String).join(" ")); });
    const broken = new Proxy(env.DB, {
      get(target, prop) {
        if (prop === "prepare") {
          return (sql: string) => {
            if (sql.includes("INSERT INTO mail_messages")) throw new Error("D1_ERROR: disk full");
            return target.prepare(sql);
          };
        }
        const v = (target as any)[prop];
        return typeof v === "function" ? v.bind(target) : v;
      },
    });
    expect(await deliver(CODE_MAIL, s.alias, { ...MAIL, DB: broken as D1Database })).toEqual([]);
    const ev = await env.DB.prepare("SELECT kind, code FROM mail_events WHERE user = ?").bind(s.id).all();
    expect(ev.results).toEqual([{ kind: "code", code: "482913" }]);
    expect((await rows(s.id)).results).toHaveLength(0);
    // Not stored, so the notice ("read it in the Inbox") would be untrue: the whole message goes out.
    expect(sent).toHaveLength(1);
    expect(sent[0].from).toBe("\"Workday via SponsorJobs\" <no-reply@sponsorjobs.ai>");
    expect(sent[0].text).toContain("Your verification code is 482913");
    expect(logs.some((l) => l.includes("not stored"))).toBe(true);
    for (const l of logs) {
      expect(l).not.toContain("482913");
      expect(l).not.toContain("expires in 10 minutes");
    }
  });
});

describe("GET /mail/messages", () => {
  it("needs the bearer token", async () => {
    expect((await call("/mail/messages")).status).toBe(401);
    expect((await call("/mail/alias/notify", { body: { mode: "off" } })).status).toBe(401);
  });

  it("lists oldest first with every field; after=N acknowledges and deletes up to N", async () => {
    const s = await student();
    stubResend();
    await deliver(RECEIVED, s.alias);
    await deliver(REJECTED, s.alias);
    await deliver(CODE_MAIL, s.alias);
    const r = await messages(s.token);
    expect(r.status).toBe(200);
    expect(r.data.messages.map((m: any) => m.subject)).toEqual([
      "Thank you for applying to Acme Robotics", "Your application to Borealis Labs", "Your verification code"]);
    const first = r.data.messages[0];
    expect(Object.keys(first).sort()).toEqual(
      ["at", "attachment_names", "cursor", "has_attachments", "html", "sender", "sender_name", "subject", "text"]);
    expect(first).toMatchObject({ sender: "no-reply@greenhouse-mail.io", sender_name: "Acme Robotics Recruiting",
                                  html: null, has_attachments: false, attachment_names: [] });
    expect(first.at).toBeGreaterThan(Math.floor(Date.now() / 1000) - 5);
    const [c0, c1, c2] = r.data.messages.map((m: any) => m.cursor);
    expect(c0 < c1 && c1 < c2).toBe(true);
    expect(r.data.cursor).toBe(c2);
    expect(r.data.more).toBe(false);
    // Reading again without a cursor deletes nothing.
    expect((await messages(s.token)).data.messages).toHaveLength(3);
    const r2 = await messages(s.token, `?after=${c1}`);
    expect(r2.data.messages.map((m: any) => m.cursor)).toEqual([c2]);
    expect((await rows(s.id)).results).toHaveLength(1);
    const r3 = await messages(s.token, `?after=${c2}`);
    expect(r3.data).toEqual({ messages: [], cursor: c2, more: false });
    expect((await rows(s.id)).results).toHaveLength(0);
    await seed(s.id, 1);
    expect((await messages(s.token, "?after=garbage")).data.messages).toHaveLength(1);
    expect((await messages(s.token, "?after=-5")).data.messages).toHaveLength(1);
  });

  it("returns at most 25 per call, honours a smaller limit, and says when there is more", async () => {
    const s = await student();
    await seed(s.id, MESSAGES_LIMIT + 5);
    const r = await messages(s.token);
    expect(r.data.messages).toHaveLength(MESSAGES_LIMIT);
    expect(r.data.more).toBe(true);
    const r2 = await messages(s.token, `?after=${r.data.cursor}&limit=500`);
    expect(r2.data.messages).toHaveLength(5);
    expect(r2.data.more).toBe(false);
    await seed(s.id, 3);
    const r3 = await messages(s.token, `?after=${r2.data.cursor}&limit=2`);
    expect(r3.data.messages).toHaveLength(2);
    expect(r3.data.more).toBe(true);
  });

  it("a page stops early when the bodies are large, but always returns one message", async () => {
    const s = await student();
    await seed(s.id, 3, undefined, 3 << 20);
    const r = await messages(s.token);
    expect(r.data.messages).toHaveLength(1);
    expect(r.data.more).toBe(true);
  });

  it("another account can neither read nor acknowledge them", async () => {
    const a = await student();
    const b = await student("Other Person");
    await seed(a.id, 2);
    const rb = await messages(b.token, "?after=1000000000");
    expect(rb.data).toEqual({ messages: [], cursor: 1_000_000_000, more: false });
    expect((await messages(a.token)).data.messages).toHaveLength(2);
  });

  it("a message older than 30 days is dropped on every read, on every receive, and by the agent's inbox poll", async () => {
    const s = await student();
    const now = Math.floor(Date.now() / 1000);
    await seed(s.id, 1, now - MESSAGE_TTL_SECONDS - 1);
    await seed(s.id, 1, now - MESSAGE_TTL_SECONDS + 60);
    const r = await messages(s.token);
    expect(r.data.messages).toHaveLength(1);
    expect((await rows(s.id)).results).toHaveLength(1);
    // on a receive, for any account
    const other = await student("Other Person");
    await seed(s.id, 1, now - MESSAGE_TTL_SECONDS - 1);
    stubResend();
    await deliver(RECEIVED, other.alias);
    expect((await rows(s.id)).results).toHaveLength(1);
    // and wherever expired events are swept
    await seed(s.id, 1, now - MESSAGE_TTL_SECONDS - 1);
    await call("/mail/inbox", { token: other.token });
    expect((await rows(s.id)).results).toHaveLength(1);
  });

  it("DELETE /mail/alias drops the account's stored messages", async () => {
    const s = await student();
    const other = await student("Other Person");
    await seed(s.id, 2);
    await seed(other.id, 1);
    const d = await call("/mail/alias", { method: "DELETE", token: s.token });
    expect(d.data).toEqual({ alias: null, forward_to: null, notify_mode: null });
    expect((await rows(s.id)).results).toHaveLength(0);
    expect((await rows(other.id)).results).toHaveLength(1);
  });
});

describe("notify modes", () => {
  it("defaults to forward, is in the alias responses, and POST /mail/alias/notify changes it", async () => {
    const s = await student();
    expect((await call("/mail/alias", { token: s.token })).data).toMatchObject({ alias: s.alias, notify_mode: "forward" });
    expect((await call("/mail/alias", { token: s.token, body: {} })).data.notify_mode).toBe("forward");
    const r = await call("/mail/alias/notify", { token: s.token, body: { mode: "notice" } });
    expect(r.status).toBe(200);
    expect(r.data).toEqual({ alias: s.alias, forward_to: s.email, notify_mode: "notice" });
    expect((await call("/mail/alias", { token: s.token })).data.notify_mode).toBe("notice");
    expect((await call("/mail/alias", { token: s.token, body: {} })).data.notify_mode).toBe("notice");   // asking again keeps it
    const bad = await call("/mail/alias/notify", { token: s.token, body: { mode: "everything" } });
    expect(bad.status).toBe(400);
    expect(bad.data.error).toBe("bad_mode");
    expect((await call("/mail/alias/notify", { token: s.token, body: {} })).status).toBe(400);
    const none = await signedInAccount();
    expect((await call("/mail/alias/notify", { token: none.token, body: { mode: "off" } })).status).toBe(404);
  });

  it("forward: the raw forward is unchanged, and the message is stored too", async () => {
    const s = await student();
    const sent = stubResend();
    await deliver(RECEIVED, s.alias);
    expect(sent).toHaveLength(1);
    expect(sent[0].from).toBe("\"Acme Robotics Recruiting via SponsorJobs\" <no-reply@sponsorjobs.ai>");
    expect(sent[0].subject).toBe("Thank you for applying to Acme Robotics");
    expect(sent[0].text.startsWith(`Forwarded by SponsorJobs from ${s.alias}; attachments are not forwarded\n\n`)).toBe(true);
    expect((await rows(s.id)).results).toHaveLength(1);
  });

  it("notice: a short branded note with the reply going to the sender", async () => {
    const s = await student();
    await call("/mail/alias/notify", { token: s.token, body: { mode: "notice" } });
    const sent = stubResend();
    await deliver(RECEIVED, s.alias);
    await deliver(REJECTED, s.alias);
    expect(sent).toHaveLength(2);
    const a = sent[0];
    expect(a.from).toBe("\"SponsorJobs\" <no-reply@sponsorjobs.ai>");
    expect(a.reply_to).toBe("no-reply@greenhouse-mail.io");
    expect(a.to).toEqual([s.email]);
    expect(a.subject).toBe("Acme Robotics received your application");
    expect(a.html).toBeUndefined();
    expect(a.text).toContain("> Hi Zoe, Thank you for applying to the Data Engineer role at Acme Robotics.");
    expect(a.text).toContain("Open SponsorJobs and go to Inbox to read it.");
    expect(a.text).toContain(s.alias);
    expect(a.text).not.toMatch(/[–—]/);
    expect(sent[1].subject).toBe("An update on your application to Borealis Labs");
    expect(sent[1].reply_to).toBe("careers@borealislabs.example");
    expect((await rows(s.id)).results).toHaveLength(2);
    const day = new Date().toISOString().slice(0, 10);
    const n = await env.DB.prepare("SELECT COALESCE(SUM(n), 0) AS n FROM email_sends WHERE day = ?").bind(day).first<{ n: number }>();
    expect(n!.n).toBeGreaterThanOrEqual(2);              // a notice counts against the Resend figure too
  });

  it("notice: the quote is the first 300 characters, and an unlabelled message names the sender", async () => {
    const n = noticeFor({ senderName: "Pat Rivera", senderAddress: "pat@corvid.example", subject: "Quick question",
                          text: "Hello Zoe. " + "word ".repeat(200), alias: "zoe.muller@sponsorjobs.ai" });
    expect(n.subject).toBe("Pat Rivera sent you a message");
    const quote = n.text.split("\n").find((l) => l.startsWith("> "))!;
    expect(quote.length).toBeLessThanOrEqual(2 + 300 + 3);
    expect(quote.endsWith("...")).toBe(true);
    const bare = noticeFor({ senderName: "", senderAddress: "x@corvid.example", subject: "", text: "", alias: "a@sponsorjobs.ai" });
    expect(bare.subject).toBe("x@corvid.example sent you a message");
    expect(bare.text).not.toContain("> ");
    const ats = noticeFor({ senderName: "Workday", senderAddress: "noreply@myworkday.com", subject: "Application received",
                            text: "We have received your application.", alias: "a@sponsorjobs.ai" });
    expect(ats.subject).toBe("Your application was received");
  });

  it("off: nothing is sent, the message is still stored", async () => {
    const s = await student();
    await call("/mail/alias/notify", { token: s.token, body: { mode: "off" } });
    const spy = vi.spyOn(globalThis, "fetch");
    await deliver(RECEIVED, s.alias);
    expect(spy).not.toHaveBeenCalled();
    expect((await rows(s.id)).results).toHaveLength(1);
  });
});

// The port of inbox/labels.py; the same cases as tests/test_inbox_labels.py.
describe("label rules", () => {
  it("labels confirmations, rejections, interview requests and requests for action", () => {
    expect(labelOf("Thank you for applying to Acme Robotics",
      "We have received your application. If selected, we will reach out to schedule an interview.")).toBe("received");
    expect(labelOf("Your application", "Unfortunately, we have decided to pursue other candidates.")).toBe("rejected");
    expect(labelOf("Update", "We regret to inform you that the position has been filled.")).toBe("rejected");
    expect(labelOf("Update", "You have not been selected for this role.")).toBe("rejected");
    expect(labelOf("Update", "We will not be moving forward with your application.")).toBe("rejected");
    expect(labelOf("Interview with Corvid", "We would like to invite you to a phone interview. Pick a time: https://calendly.com/corvid/30min")).toBe("interview");
    expect(labelOf("Next step", "Please complete the HackerRank assessment within 5 days.")).toBe("action");
    expect(labelOf("Verify your email", "Verify your email: https://acme.example/verify?t=1")).toBe("action");
    expect(labelOf("Lunch?", "Are you around on Friday?")).toBe("other");
  });

  it("small print in a confirmation does not make it a rejection or an interview", () => {
    expect(labelOf("Application received",
      "Thanks for applying. Unfortunately we cannot respond to every applicant individually. " +
      "Our process typically includes a phone screen and a technical interview.")).toBe("received");
  });

  it("companyOf drops role words and refuses a mail system's own name", () => {
    expect(companyOf("Acme Robotics Recruiting")).toBe("Acme Robotics");
    expect(companyOf("Borealis Labs Talent Acquisition Team")).toBe("Borealis Labs");
    expect(companyOf("Corvid via Greenhouse")).toBe("Corvid");
    expect(companyOf("Workday")).toBe("");
    expect(companyOf("no-reply")).toBe("");
    expect(companyOf("")).toBe("");
  });
});
