// Mail aliases for the apply agent: first.last@sponsorjobs.ai, one per student.
//
// WHY: the desktop app's apply agent (docs/apply-agent.md) creates portal accounts on a student's
// behalf, and the portal mails back a verification link or a one-time code. With the student away
// the agent has to read that mail. Rather than read the student's own Gmail, each student gets an
// alias on our domain. Cloudflare Email Routing hands every message for sponsorjobs.ai to this
// Worker (the catch-all rule, docs/mail-alias.md). The Worker looks the alias up, pulls out the
// code or the link, keeps ONLY that for 30 minutes so the app can poll it, and forwards the whole
// message to the student's real inbox through the configured mailer (Resend). Nothing else is
// stored, and no body, code or link is ever logged.
//
// The detection rules are a port of inbox/detect.py (otp_code, verification_link,
// password_reset_link, kind_of). Keep the two in step: the app and the Worker must agree on what
// counts as a code.

import PostalMime from "postal-mime";
import type { Email } from "postal-mime";
import { emailOf, identify } from "./accounts";
import type { Env } from "./env";
import { countSend, mailerFrom, senderAddress } from "./mailer";

const json = (data: unknown, status = 200): Response =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

async function body(req: Request): Promise<Record<string, unknown>> {
  try {
    const b = await req.json();
    return b && typeof b === "object" && !Array.isArray(b) ? (b as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

export const ALIAS_DOMAIN = "sponsorjobs.ai";
export const EVENT_TTL_SECONDS = 30 * 60;   // a code or link is kept this long, then dropped
export const INBOX_LIMIT = 50;               // events per poll
const BODY_CAP = 1 << 20;                    // 1 MiB each for the forwarded text and html
const FIELD_CAP = 200;                       // subject and sender in an event

// Locals an alias never takes: role addresses, the company's own addresses (support@ and
// no-reply@ are in use; api and admin are our hosts), and names that would read as us.
export const RESERVED_LOCALS = new Set([
  "support", "admin", "no-reply", "noreply", "do-not-reply", "donotreply", "postmaster", "abuse",
  "hello", "info", "billing", "security", "team", "kofi", "api", "mail", "webmaster", "hostmaster",
  "root", "sales", "contact", "help", "press", "legal", "privacy", "jobs", "careers", "notifications",
  "sponsorjobs", "blackorigin", "founder", "ceo",
]);

/** Reserved, compared without dots and hyphens on either side, so "no.reply" (the fold of
 * "No-Reply") is caught as well as "noreply". */
const RESERVED_SQUASHED = new Set([...RESERVED_LOCALS].map((r) => r.replace(/[.-]/g, "")));
const isReserved = (local: string) => RESERVED_SQUASHED.has(local.replace(/[.-]/g, ""));

// ---- choosing a local part ------------------------------------------------------------------- //

/** Fold a name to a local part: ASCII letters, digits and dots, "first.last" (the first and the
 * last word), 3 to 40 characters. "student" when nothing usable is left or the result is reserved. */
export function foldLocal(name: string): string {
  const words = name.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase()
    .split(/[^a-z0-9]+/).filter(Boolean);
  let local = words.length >= 2 ? `${words[0]}.${words[words.length - 1]}` : (words[0] ?? "");
  local = local.slice(0, 40).replace(/\.+$/, "");
  // Reserved as first.last, or as all the words run together ("Do Not Reply" -> "donotreply").
  if (local.length < 3 || isReserved(local) || isReserved(words.join(""))) return "student";
  return local;
}

/** A random n-digit number without a leading zero. Not a secret, just a tie-breaker. */
const suffix = (n: number) => String(10 ** (n - 1) + Math.floor(Math.random() * 9 * 10 ** (n - 1)));

/** The local part for `name`: the folded name when free, else with a short random number
 * ("zoe.muller42"); longer numbers only when the short ones are crowded. The base is cut so the
 * result stays within 40 characters. `taken` answers whether a local is already in use. */
export async function aliasLocalFor(name: string, taken: (local: string) => Promise<boolean>): Promise<string> {
  const base = foldLocal(name);
  if (!(await taken(base))) return base;
  for (const n of [2, 2, 2, 3, 3, 3, 4, 4, 4, 4]) {
    const local = `${base.slice(0, 40 - n)}${suffix(n)}`;
    if (!(await taken(local))) return local;
  }
  throw new Error("no free alias");
}

/** The alias local part a message was sent to: the part before the @, lowercased, with a +tag
 * removed ("Zoe.Muller+workday@SponsorJobs.ai" -> "zoe.muller"). */
export function localOf(to: string): string {
  const at = to.indexOf("@");
  const local = (at < 0 ? to : to.slice(0, at)).trim().toLowerCase();
  const plus = local.indexOf("+");
  return plus < 0 ? local : local.slice(0, plus);
}

const aliasOf = (local: string) => `${local}@${ALIAS_DOMAIN}`;

// ---- routes ---------------------------------------------------------------------------------- //

interface Owner { user: string; email: string }

/** The caller's account and verified e-mail, or the response to answer instead: 401 with no
 * token, 409 sign_in_first for the anonymous device account. An alias forwards to the verified
 * e-mail and nothing else; the client never supplies the forward address. */
async function owner(req: Request, env: Env): Promise<Owner | Response> {
  const user = await identify(req, env);
  if (!user) return json({ error: "no user" }, 401);
  const email = await emailOf(env.DB, user);
  if (!email) return json({ error: "sign_in_first" }, 409);
  return { user, email };
}

/** POST /mail/alias {name?}: make (or return) the caller's alias. Idempotent: an account has one
 * alias, and asking again returns it with the forward address refreshed to the verified e-mail.
 * Without a name the e-mail's own local part is folded instead. */
export async function aliasCreate(req: Request, env: Env, now: number): Promise<Response> {
  const o = await owner(req, env);
  if (o instanceof Response) return o;
  const db = env.DB;
  const have = await db.prepare("UPDATE mail_aliases SET forward_to = ? WHERE user = ? RETURNING local")
    .bind(o.email, o.user).first<{ local: string }>();
  if (have) return json({ alias: aliasOf(have.local), forward_to: o.email });
  const b = await body(req);
  const name = typeof b.name === "string" && b.name.trim() ? b.name : o.email.split("@")[0]!;
  const taken = async (local: string) =>
    Boolean(await db.prepare("SELECT 1 FROM mail_aliases WHERE local = ?").bind(local).first());
  for (let attempt = 0; attempt < 3; attempt++) {
    const local = await aliasLocalFor(name, taken);
    try {
      await db.prepare("INSERT INTO mail_aliases(local, user, forward_to, created_at) VALUES(?, ?, ?, ?)")
        .bind(local, o.user, o.email, now).run();
      return json({ alias: aliasOf(local), forward_to: o.email });
    } catch {
      // The local, or this account, was claimed by a parallel request a moment ago: look again.
      const again = await db.prepare("SELECT local FROM mail_aliases WHERE user = ?").bind(o.user).first<{ local: string }>();
      if (again) return json({ alias: aliasOf(again.local), forward_to: o.email });
    }
  }
  throw new Error("could not allot an alias");
}

/** GET /mail/alias: {alias, forward_to}, both null until one is made. */
export async function aliasGet(req: Request, env: Env): Promise<Response> {
  const user = await identify(req, env);
  if (!user) return json({ error: "no user" }, 401);
  const row = await env.DB.prepare("SELECT local, forward_to FROM mail_aliases WHERE user = ?")
    .bind(user).first<{ local: string; forward_to: string }>();
  return json(row ? { alias: aliasOf(row.local), forward_to: row.forward_to } : { alias: null, forward_to: null });
}

/** DELETE /mail/alias: release the alias and drop its pending events. Mail to it bounces from
 * then on ("No such address"). */
export async function aliasDelete(req: Request, env: Env): Promise<Response> {
  const user = await identify(req, env);
  if (!user) return json({ error: "no user" }, 401);
  await env.DB.batch([
    env.DB.prepare("DELETE FROM mail_events WHERE user = ?").bind(user),
    env.DB.prepare("DELETE FROM mail_aliases WHERE user = ?").bind(user),
  ]);
  return json({ alias: null, forward_to: null });
}

interface EventRow { id: number; kind: string; code: string | null; link: string | null; subject: string | null;
                     sender: string | null; at: number }

/** GET /mail/inbox?after=<cursor>: this account's codes and links from the last 30 minutes,
 * oldest first, at most 50. Passing `after=N` acknowledges (deletes) every event with cursor <= N;
 * expired events are dropped on every read. The same shape as the Telegram inbox (docs/notify.md):
 * the returned `cursor` is the last event's, or `after` when there are none. */
export async function inboxRead(req: Request, env: Env, now: number): Promise<Response> {
  const user = await identify(req, env);
  if (!user) return json({ error: "no user" }, 401);
  const after = Math.max(0, Number.parseInt(new URL(req.url).searchParams.get("after") ?? "0", 10) || 0);
  const [, , rows] = await env.DB.batch<EventRow>([
    env.DB.prepare("DELETE FROM mail_events WHERE at < ?").bind(now - EVENT_TTL_SECONDS),
    env.DB.prepare("DELETE FROM mail_events WHERE user = ? AND id <= ?").bind(user, after),
    env.DB.prepare("SELECT id, kind, code, link, subject, sender, at FROM mail_events WHERE user = ? ORDER BY id LIMIT ?")
      .bind(user, INBOX_LIMIT),
  ]);
  const events = rows!.results.map((r) => ({
    cursor: r.id, kind: r.kind, code: r.code, link: r.link, subject: r.subject, sender: r.sender, at: r.at,
  }));
  return json({ events, cursor: events.length ? events[events.length - 1]!.cursor : after });
}

// ---- what the apply agent waits for: a port of inbox/detect.py ------------------------------- //

// Subject/body wording that marks an application email-verification / confirmation.
const VERIFY_RE = /verify (your )?(e-?mail|account|address)|confirm (your )?(e-?mail|application|account)|complete (your )?application|activate (your )?account|email verification|please confirm|verify it'?s you/i;
const LINK_RE = /https?:\/\/[^\s"'<>)\]]+/i;          // .test only; LINKS_RE is the global twin
const LINKS_RE = /https?:\/\/[^\s"'<>)\]]+/gi;
// A link that itself looks like a verify/confirm action.
const VERIFY_LINK_RE = /verif|confirm|activat|validate|token|\/auth\/|magic/i;

// Words that sit next to a one-time code. "verif" covers verify/verification.
const CODE_CUE_RE = /\bcode\b|verif|one[- ]time|\bpin\b|passcode|\botp\b|confirm|security|authenticat/i;
// Digit runs that are not a code: a phone number, a zip after a state, an order/ticket number, a
// year. They are blanked before the scan so they never win.
const PHONE_RE = /(?:\+?\d{1,3}[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]?\d{4}\b/g;
const ZIP_RE = /,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b/g;
const REFERENCE_RE = /\b(?:order|ticket|case|invoice|tracking|account|reference|ref|req(?:uisition)?|job|application)\s*(?:number|no\.?|id|#)?\s*[:#]?\s*#?\s*\d{4,8}\b/gi;
const YEAR_RE = /(?<!\d)(?:19|20)\d{2}(?!\d)/g;
const CODE_RE = /(?<![\d-])(\d{4,8})(?![\d-])/g;
const WINDOW = 70;

const RESET_WORDS_RE = /reset|forgot|forgotten|new password|change (your )?password|recover(y)? (your )?(account|password)/i;
const RESET_LINK_RE = /reset|forgot|password|passwd|recover/i;

const blankNonCodes = (text: string) =>
  [PHONE_RE, ZIP_RE, REFERENCE_RE, YEAR_RE].reduce((t, re) => t.replace(re, (m) => " ".repeat(m.length)), text);

/** The first 4 to 8 digit run with a code cue within WINDOW characters of it. */
function codeIn(text: string): string {
  const clean = blankNonCodes(text);
  for (const m of clean.matchAll(CODE_RE)) {
    const around = clean.slice(Math.max(0, m.index - WINDOW), m.index + m[0].length + WINDOW);
    if (CODE_CUE_RE.test(around)) return m[1]!;
  }
  return "";
}

/** The one-time code a message carries, or "". The subject wins when it has one ("482913 is your
 * verification code"); the body is scanned only then. */
export const otpCode = (subject: string, body: string): string => codeIn(subject) || codeIn(body);

const trimLink = (link: string) => link.replace(/[.,)]+$/, "");

/** The link to a password reset, or "": its URL says reset/forgot/password, or the words around
 * it do. */
export function passwordResetLink(body: string): string {
  const links = [...body.matchAll(LINKS_RE)];
  for (const m of links) if (RESET_LINK_RE.test(m[0])) return trimLink(m[0]);
  for (const m of links) {
    const end = m.index + m[0].length;
    const around = body.slice(Math.max(0, m.index - 120), m.index) + body.slice(end, end + 60);
    if (RESET_WORDS_RE.test(around)) return trimLink(m[0]);
  }
  return "";
}

/** The verify/confirm link to visit for a verification message (best candidate first). */
export function verificationLink(body: string): string {
  const links = [...body.matchAll(LINKS_RE)].map((m) => m[0]);
  for (const link of links) if (VERIFY_LINK_RE.test(link)) return trimLink(link);
  return links[0] ? trimLink(links[0]) : "";
}

export type MailKind = "reset" | "code" | "verify" | "";

/** "verify" | "code" | "reset" | "" for one message the agent may be waiting on. */
export function kindOf(subject: string, body: string): MailKind {
  const text = `${subject}\n${body}`;
  if (RESET_WORDS_RE.test(text) && passwordResetLink(body)) return "reset";
  if (otpCode(subject, body)) return "code";
  if (VERIFY_RE.test(text) && LINK_RE.test(text)) return "verify";
  return "";
}

export interface Detected { kind: MailKind; code: string | null; link: string | null }

/** What the agent would wait on in this message: the kind, and the one value that goes with it. */
export function detect(subject: string, body: string): Detected {
  const kind = kindOf(subject, body);
  if (kind === "code") return { kind, code: otpCode(subject, body), link: null };
  if (kind === "verify") return { kind, code: null, link: verificationLink(body) || null };
  if (kind === "reset") return { kind, code: null, link: passwordResetLink(body) };
  return { kind, code: null, link: null };
}

// ---- reading the message --------------------------------------------------------------------- //

const ENTITIES: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: "\"", apos: "'", nbsp: " " };

function decodeEntities(s: string): string {
  return s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (whole, ent: string) => {
    const e = ent.toLowerCase();
    if (e.startsWith("#x")) return String.fromCodePoint(Number.parseInt(e.slice(2), 16) || 63);
    if (e.startsWith("#")) return String.fromCodePoint(Number.parseInt(e.slice(1), 10) || 63);
    return ENTITIES[e] ?? whole;
  });
}

/** Every link target in an HTML body, in order, entities decoded. */
function hrefsOf(html: string): string[] {
  return [...html.matchAll(/<a\b[^>]*?\shref\s*=\s*["']?([^"'\s>]+)/gi)].map((m) => decodeEntities(m[1]!));
}

/** The readable text of an HTML body, with each link's address at the end, so a verification
 * link in an HTML-only message (where the visible words are just "Verify") is still found. */
export function htmlToText(html: string): string {
  const text = decodeEntities(
    html.replace(/<(script|style)\b[\s\S]*?<\/\1\s*>/gi, " ")
      .replace(/<\s*(br|\/p|\/div|\/tr|\/li|\/h[1-6])\b[^>]*>/gi, "\n")
      .replace(/<[^>]+>/g, " "))
    .replace(/[ \t]+/g, " ").replace(/\s*\n\s*/g, "\n").trim();
  const hrefs = hrefsOf(html);
  return hrefs.length ? `${text}\n${hrefs.join("\n")}` : text;
}

/** The text the rules run over: the plain part (or the HTML read as text), plus any link the HTML
 * carries that the plain part does not. */
function textForRules(text: string | undefined, html: string | undefined): string {
  const base = text || (html ? htmlToText(html) : "");
  const extra = html ? hrefsOf(html).filter((h) => !base.includes(h)) : [];
  return extra.length ? `${base}\n${extra.join("\n")}` : base;
}

/** A display name safe inside a quoted From: no quotes, angle brackets or line breaks. */
const displayName = (s: string) => s.replace(/["<>\\\r\n]/g, "").trim().slice(0, 60);

/** Cloudflare Email Routing hands every message for the domain here. Unknown alias: a permanent
 * reject, so the sender learns the address does not exist. Known alias: record the code or link
 * (if any), then forward the whole message to the student's verified e-mail. A forward failure is
 * logged and the event is kept: the agent must still get the code. */
export async function receiveMail(message: ForwardableEmailMessage, env: Env, now: number): Promise<void> {
  const local = localOf(message.to);
  const row = local
    ? await env.DB.prepare("SELECT user, forward_to FROM mail_aliases WHERE local = ?").bind(local)
        .first<{ user: string; forward_to: string }>()
    : null;
  if (!row) {
    message.setReject("No such address");
    return;
  }
  let parsed: Email;
  try {
    parsed = await PostalMime.parse(message.raw);
  } catch (err) {
    console.error("mail parse failed:", err instanceof Error ? err.message : err);
    message.setReject("Message could not be read");
    return;
  }
  const subject = (parsed.subject ?? message.headers.get("subject") ?? "").trim();
  const fromAddress = (parsed.from?.address ?? message.from ?? "").trim();
  const fromName = (parsed.from?.name ?? "").trim();
  const sender = fromName ? `${fromName} <${fromAddress}>` : fromAddress;
  const d = detect(subject, textForRules(parsed.text, parsed.html));
  if (d.kind) {
    await env.DB.prepare("INSERT INTO mail_events(user, kind, code, link, subject, sender, at) VALUES(?, ?, ?, ?, ?, ?, ?)")
      .bind(row.user, d.kind, d.code, d.link, subject.slice(0, FIELD_CAP), sender.slice(0, FIELD_CAP), now).run();
  }
  const alias = aliasOf(local);
  const mailer = mailerFrom(env);
  if (!mailer) {
    console.error(`mail for ${row.user} not forwarded: no mailer configured`);
    return;
  }
  const notice = `Forwarded by SponsorJobs from ${alias}; attachments are not forwarded`;
  const text = `${notice}\n\n${parsed.text ?? (parsed.html ? htmlToText(parsed.html) : "")}`.slice(0, BODY_CAP);
  const html = parsed.html
    ? `<p style="font:13px system-ui;color:#555">${notice.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</p>\n${parsed.html}`.slice(0, BODY_CAP)
    : undefined;
  try {
    await mailer.send({
      to: row.forward_to,
      from: `"${displayName(fromName || fromAddress || "Unknown sender")} via SponsorJobs" <${senderAddress(env)}>`,
      reply_to: fromAddress || undefined,
      subject: subject || "(no subject)",
      text,
      html,
    });
    await countSend(env.DB, now);
  } catch (err) {
    console.error(`mail forward failed (${mailer.name}) for ${row.user}:`, err instanceof Error ? err.message : err);
  }
}
