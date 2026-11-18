// Mail aliases for the apply agent: first.last@sponsorjobs.ai, one per student.
//
// WHY: the desktop app's apply agent (docs/apply-agent.md) creates portal accounts on a student's
// behalf, and the portal mails back a verification link or a one-time code. With the student away
// the agent has to read that mail. Rather than read the student's own Gmail, each student gets an
// alias on our domain. Cloudflare Email Routing hands every message for sponsorjobs.ai to this
// Worker (the catch-all rule, docs/mail-alias.md). The Worker looks the alias up, pulls out the
// code or the link and keeps that for 30 minutes so the apply agent can poll it.
//
// The in-app mailbox (2026-11): the whole message (subject, sender, text and HTML bodies, the
// NAMES of attachments, never their content) is also kept in mail_messages, but only until the
// person's own app has downloaded it (GET /mail/messages?after=<cursor> acknowledges and
// deletes) and never longer than 30 days. After the download the mailbox lives on the person's
// machine. What goes to the person's real e-mail is set per alias (notify_mode): the whole
// message as before ("forward", the default), a short notice ("notice"), or nothing ("off").
// No body, code or link is ever logged.
//
// The detection rules are a port of inbox/detect.py (otp_code, verification_link,
// password_reset_link, kind_of). Keep the two in step: the app and the Worker must agree on what
// counts as a code. The label rules further down (labelOf) are a port of inbox/labels.py, kept in
// step the same way.

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
const BODY_CAP = 1 << 20;                    // 1 MiB each for the text and html (forwarded or stored)
export const MESSAGE_TTL_SECONDS = 30 * 24 * 60 * 60;   // a stored message is dropped after 30 days
export const MESSAGES_LIMIT = 25;            // messages per sync call (bodies are large)
const PAGE_BYTES = 4 << 20;                  // and at most about this much body per call
// One D1 row holds 2,000,000 bytes at most. Text and html together are kept under this, with
// room for the other columns.
const ROW_BYTES = 1_800_000;
const NAME_CAP = 200;                        // one attachment file name
const NAMES_MAX = 50;                        // attachment names kept per message
export const NOTIFY_MODES = ["forward", "notice", "off"] as const;
export type NotifyMode = (typeof NOTIFY_MODES)[number];
const notifyMode = (v: unknown): NotifyMode =>
  (NOTIFY_MODES as readonly string[]).includes(String(v)) ? (v as NotifyMode) : "forward";
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
  const have = await db.prepare("UPDATE mail_aliases SET forward_to = ? WHERE user = ? RETURNING local, notify_mode")
    .bind(o.email, o.user).first<{ local: string; notify_mode: string }>();
  if (have) return json({ alias: aliasOf(have.local), forward_to: o.email, notify_mode: notifyMode(have.notify_mode) });
  const b = await body(req);
  const name = typeof b.name === "string" && b.name.trim() ? b.name : o.email.split("@")[0]!;
  const taken = async (local: string) =>
    Boolean(await db.prepare("SELECT 1 FROM mail_aliases WHERE local = ?").bind(local).first());
  for (let attempt = 0; attempt < 3; attempt++) {
    const local = await aliasLocalFor(name, taken);
    try {
      await db.prepare("INSERT INTO mail_aliases(local, user, forward_to, created_at) VALUES(?, ?, ?, ?)")
        .bind(local, o.user, o.email, now).run();
      return json({ alias: aliasOf(local), forward_to: o.email, notify_mode: "forward" });
    } catch {
      // The local, or this account, was claimed by a parallel request a moment ago: look again.
      const again = await db.prepare("SELECT local, notify_mode FROM mail_aliases WHERE user = ?").bind(o.user)
        .first<{ local: string; notify_mode: string }>();
      if (again) return json({ alias: aliasOf(again.local), forward_to: o.email, notify_mode: notifyMode(again.notify_mode) });
    }
  }
  throw new Error("could not allot an alias");
}

/** GET /mail/alias: {alias, forward_to, notify_mode}, all null until one is made. */
export async function aliasGet(req: Request, env: Env): Promise<Response> {
  const user = await identify(req, env);
  if (!user) return json({ error: "no user" }, 401);
  const row = await env.DB.prepare("SELECT local, forward_to, notify_mode FROM mail_aliases WHERE user = ?")
    .bind(user).first<{ local: string; forward_to: string; notify_mode: string }>();
  return json(row ? { alias: aliasOf(row.local), forward_to: row.forward_to, notify_mode: notifyMode(row.notify_mode) }
                  : { alias: null, forward_to: null, notify_mode: null });
}

/** POST /mail/alias/notify {mode}: what the person's own e-mail gets for each message to the
 * alias: "forward" (the whole message), "notice" (a short note) or "off". 400 for any other
 * value, 404 when the account has no alias. */
export async function aliasNotify(req: Request, env: Env): Promise<Response> {
  const user = await identify(req, env);
  if (!user) return json({ error: "no user" }, 401);
  const mode = (await body(req)).mode;
  if (typeof mode !== "string" || !(NOTIFY_MODES as readonly string[]).includes(mode)) {
    return json({ error: "bad_mode", modes: NOTIFY_MODES }, 400);
  }
  const row = await env.DB.prepare("UPDATE mail_aliases SET notify_mode = ? WHERE user = ? RETURNING local, forward_to")
    .bind(mode, user).first<{ local: string; forward_to: string }>();
  if (!row) return json({ error: "no_alias" }, 404);
  return json({ alias: aliasOf(row.local), forward_to: row.forward_to, notify_mode: mode });
}

/** DELETE /mail/alias: release the alias and drop its pending events and any stored messages
 * the app has not downloaded yet. Mail to it bounces from then on ("No such address"). */
export async function aliasDelete(req: Request, env: Env): Promise<Response> {
  const user = await identify(req, env);
  if (!user) return json({ error: "no user" }, 401);
  await env.DB.batch([
    env.DB.prepare("DELETE FROM mail_events WHERE user = ?").bind(user),
    env.DB.prepare("DELETE FROM mail_messages WHERE user = ?").bind(user),
    env.DB.prepare("DELETE FROM mail_aliases WHERE user = ?").bind(user),
  ]);
  return json({ alias: null, forward_to: null, notify_mode: null });
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
  const [, , , rows] = await env.DB.batch<EventRow>([
    env.DB.prepare("DELETE FROM mail_messages WHERE received_at < ?").bind(now - MESSAGE_TTL_SECONDS),
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

// ---- the in-app mailbox: whole messages, held until the app downloads them -------------------- //

interface MessageMeta { id: number; size: number }
interface MessageRow { id: number; sender: string; sender_name: string; subject: string; text: string;
                       html: string | null; received_at: number; has_attachments: number; attachment_names: string }

function namesOf(raw: string): string[] {
  try {
    const v = JSON.parse(raw);
    return Array.isArray(v) ? v.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

/** GET /mail/messages?after=<cursor>&limit=<n>: this account's stored messages, oldest first, at
 * most 25 per call (fewer when the bodies are large). Passing `after=N` acknowledges and DELETES
 * every message with cursor <= N for this account: the app passes the last cursor it has safely
 * written to its own disk. Messages older than 30 days are dropped on every read. Returns
 * {messages, cursor, more}; `cursor` is the last returned message's, or `after` when there are
 * none, and `more` says another call would return further messages. */
export async function messagesRead(req: Request, env: Env, now: number): Promise<Response> {
  const user = await identify(req, env);
  if (!user) return json({ error: "no user" }, 401);
  const q = new URL(req.url).searchParams;
  const after = Math.max(0, Number.parseInt(q.get("after") ?? "0", 10) || 0);
  const limit = Math.min(MESSAGES_LIMIT, Math.max(1, Number.parseInt(q.get("limit") ?? "", 10) || MESSAGES_LIMIT));
  const [, , metas] = await env.DB.batch<MessageMeta>([
    env.DB.prepare("DELETE FROM mail_messages WHERE received_at < ?").bind(now - MESSAGE_TTL_SECONDS),
    env.DB.prepare("DELETE FROM mail_messages WHERE user = ? AND id <= ?").bind(user, after),
    env.DB.prepare("SELECT id, size FROM mail_messages WHERE user = ? ORDER BY id LIMIT ?").bind(user, limit + 1),
  ]);
  // Take messages until the page is full by count or by bytes (always at least one).
  const take: number[] = [];
  let bytes = 0;
  for (const m of metas!.results.slice(0, limit)) {
    if (take.length && bytes + m.size > PAGE_BYTES) break;
    take.push(m.id);
    bytes += m.size;
  }
  const more = metas!.results.length > take.length;
  if (!take.length) return json({ messages: [], cursor: after, more: false });
  const last = take[take.length - 1]!;
  const { results } = await env.DB.prepare(
    "SELECT id, sender, sender_name, subject, text, html, received_at, has_attachments, attachment_names " +
    "FROM mail_messages WHERE user = ? AND id <= ? ORDER BY id").bind(user, last).all<MessageRow>();
  const messages = results.map((r) => ({
    cursor: r.id, sender: r.sender, sender_name: r.sender_name, subject: r.subject, text: r.text, html: r.html,
    at: r.received_at, has_attachments: Boolean(r.has_attachments), attachment_names: namesOf(r.attachment_names),
  }));
  return json({ messages, cursor: last, more });
}

const utf8 = new TextEncoder();
const bytesOf = (s: string) => utf8.encode(s).length;

/** `s` cut to at most `max` UTF-8 bytes (on a character boundary). */
function cutBytes(s: string, max: number): string {
  if (bytesOf(s) <= max) return s;
  let lo = 0, hi = s.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (bytesOf(s.slice(0, mid)) <= max) lo = mid; else hi = mid - 1;
  }
  return s.slice(0, lo);
}

/** The bodies as they are stored: each capped at 1 MiB, and together small enough for one D1
 * row. When they are not, the text keeps its share first and the HTML is dropped (the app then
 * shows the text) rather than cut in the middle of a tag. */
export function fitBodies(text: string, html: string | undefined): { text: string; html: string | null; size: number } {
  const t = cutBytes(text.slice(0, BODY_CAP), ROW_BYTES);
  let h: string | null = html ? html.slice(0, BODY_CAP) : null;
  if (h !== null && bytesOf(t) + bytesOf(h) > ROW_BYTES) h = null;
  return { text: t, html: h, size: bytesOf(t) + (h === null ? 0 : bytesOf(h)) };
}

/** Keep one received message for the app to download. Returns false (and logs no content) when
 * the write fails: the caller then falls back to forwarding the whole message, so mail is never
 * lost to a storage problem. */
async function storeMessage(env: Env, user: string, local: string, m: {
  sender: string; senderName: string; subject: string; text: string; html: string | undefined; names: string[];
}, now: number): Promise<boolean> {
  try {
    const b = fitBodies(m.text, m.html);
    const names = m.names.slice(0, NAMES_MAX).map((n) => n.slice(0, NAME_CAP));
    await env.DB.batch([
      env.DB.prepare("DELETE FROM mail_messages WHERE received_at < ?").bind(now - MESSAGE_TTL_SECONDS),
      env.DB.prepare(
        "INSERT INTO mail_messages(user, local, sender, sender_name, subject, text, html, received_at, size, " +
        "has_attachments, attachment_names) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        .bind(user, local, m.sender.slice(0, 320), m.senderName.slice(0, FIELD_CAP), m.subject.slice(0, 998),
              b.text, b.html, now, b.size, m.names.length ? 1 : 0, JSON.stringify(names)),
    ]);
    return true;
  } catch (err) {
    console.error(`mail for ${user} not stored:`, err instanceof Error ? err.message : err);
    return false;
  }
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

// ---- labels: a port of inbox/labels.py (keep the two in step) --------------------------------- //

export type MailLabel = "received" | "rejected" | "interview" | "action" | "other";

// A sentence that only says what MAY happen ("if selected, we will reach out to schedule an
// interview") decides nothing, so it is dropped before the rules run.
const CONDITIONAL_RE = /\b(?:if|should|whether|unless|in case|in the event)\b/i;
const OUTCOME_RE = /select|shortlist|chosen|match|\bfit\b|suitab|qualif|mov(?:e|ing) forward|proceed|next (?:step|stage|round)|interview|successful|consider/i;
// A sentence that describes the hiring process in general is not an invitation.
const PROCESS_RE = /process (?:includes|consists|involves|typically|usually)|typically|usually|generally|may include|what to expect/i;

const REJECT_RE = new RegExp([
  "\\bnot (?:been |be )?(?:selected|chosen|shortlisted|successful)\\b",
  "\\b(?:not|won'?t)\\b[^.\\n]{0,25}\\b(?:mov(?:e|ing) (?:forward|ahead)|proceed(?:ing)?\\b|progress(?:ing)? (?:your|you|with))",
  "\\bunable to (?:move forward|proceed with your (?:application|candidacy)|offer you)",
  "decided to (?:pursue|move forward with|proceed with|go with|continue with|progress) (?:other|another|different)\\b",
  "(?:pursu|mov)(?:e|ing) (?:forward )?with (?:other|another) (?:candidates?|applicants?)",
  "other (?:candidates|applicants) (?:whose|who|that)\\b",
  "regret to inform|we regret\\b|regret that",
  "(?:position|role|vacancy|job) has (?:now |already )?been (?:filled|closed|cancell?ed)",
  "(?:position|role|vacancy|job|requisition) is no longer (?:available|open)",
  "no longer (?:under consideration|being considered)",
  "not (?:be )?(?:offering you|able to offer)",
].join("|"), "i");
const UNFORTUNATELY_RE = /unfortunately[^.!?\n]{0,160}/gi;
const UNFORTUNATELY_NEG_RE = /\b(?:not|unable|cannot|can'?t|won'?t|other candidates|another candidate|filled|closed)\b/i;
// "Unfortunately we cannot reply to every applicant" is a confirmation's small print.
const UNFORTUNATELY_SMALL_PRINT_RE = /(?:respond|reply|feedback|contact|get back|update)[^.]{0,40}(?:every|each|all|individual)|(?:every|each|all|individual)[^.]{0,40}(?:respond|reply|feedback)/i;

const INTERVIEW_RE = new RegExp([
  "invit(?:e|ing|ed) you (?:to|for) (?:an? |the |our )?(?:[a-z-]+ ){0,3}(?:interview|call|chat|conversation|screen|meeting)",
  "(?:schedule|scheduling|arrange|arranging|set up|setting up|book|booking) (?:an?|your|the|some) (?:[a-z-]+ ){0,3}(?:interview|call|chat|conversation|screen|time|meeting)",
  "interview (?:invitation|invite|request|confirmation|details|availability|scheduled)",
  "(?:would|'d) (?:like|love) to (?:interview|speak|chat|talk|meet|connect)",
  "(?:phone|video|technical|onsite|on-site|virtual|final|first|second|panel|initial|recruiter|hiring manager) (?:interview|screen)\\b",
  "(?:your|share|send|provide)[a-z ]{0,20}availability",
  "(?:book|pick|select|choose|find) a (?:time|slot)",
  "calendly\\.com|cal\\.com/|goodtime\\.io|savvycal\\.com|calendar\\.app\\.google|youcanbook\\.me|/scheduling?/",
  "to the (?:next|interview) (?:stage|round|step)",
  "next (?:step|stage|round) (?:is|will be|would be) (?:an?|the) [a-z -]{0,30}(?:interview|call|screen|conversation)",
].join("|"), "i");

const ACTION_RE = new RegExp([
  "hackerrank|codility|codesignal|testgorilla|pymetrics|hirevue|take-?home",
  "(?:online|technical|coding|skills?|aptitude|cognitive|personality|pre-?employment) (?:assessment|test|challenge|exercise|questionnaire)",
  "complete (?:the|this|your|an?|our) (?:[a-z-]+ ){0,3}(?:assessment|questionnaire|form|test|survey|challenge|exercise|background check|paperwork|profile|application)",
  "action (?:is )?(?:required|needed)",
  "(?:additional|more|further|missing) (?:information|details|documents?|documentation) (?:is |are )?(?:required|needed|requested)",
  "(?:please|kindly) (?:provide|upload|submit|send|attach|complete|fill|sign|review and sign)",
  "(?:we|i) need (?:you to|your|a copy|some|the following)",
  "background check|reference check",
  "(?:finish|complete|continue|resume) your application|application is (?:incomplete|not complete)|incomplete application",
  "verify your (?:e-?mail|identity|account)|confirm your (?:e-?mail|identity|account)",
].join("|"), "i");
const CODE_WORD_RE = /\b(?:code|passcode|otp|pin)\b|one[- ]time/i;

const RECEIVED_RE = new RegExp([
  "application (?:has been |was |is )?(?:successfully )?(?:received|submitted|sent|complete)\\b",
  "(?:thank you|thanks) for (?:applying|your application|your interest|submitting|taking the time to apply)",
  "received your (?:application|resume|submission)",
  "application confirmation|successfully (?:submitted|applied)|we have your application",
  "your application (?:to|for|with) \\S",
  "has been (?:received|submitted)",
].join("|"), "i");

const sentences = (text: string) => text.split(/(?<=[.!?])\s+|\n+/);

/** The text with the sentences that decide nothing removed. */
function settled(text: string, alsoProcess = false): string {
  return sentences(text)
    .filter((s) => !(CONDITIONAL_RE.test(s) && OUTCOME_RE.test(s)) && !(alsoProcess && PROCESS_RE.test(s)))
    .join("\n");
}

function saysRejected(text: string): boolean {
  if (REJECT_RE.test(text)) return true;
  for (const m of text.matchAll(UNFORTUNATELY_RE)) {
    if (UNFORTUNATELY_NEG_RE.test(m[0]) && !UNFORTUNATELY_SMALL_PRINT_RE.test(m[0])) return true;
  }
  return false;
}

/** One label for a message to the alias, from its subject and text only (no model). The first
 * rule that fires wins: rejected, interview, action, received, else other. */
export function labelOf(subject: string, text: string): MailLabel {
  const whole = `${subject}\n${text}`.slice(0, 20000);
  const plain = settled(whole);
  if (saysRejected(plain)) return "rejected";
  if (INTERVIEW_RE.test(settled(whole, true))) return "interview";
  const kind = kindOf(subject, text);
  if (kind === "verify" || kind === "reset" || (kind === "code" && CODE_WORD_RE.test(whole))) return "action";
  if (ACTION_RE.test(plain)) return "action";
  if (RECEIVED_RE.test(whole)) return "received";
  return "other";
}

// ---- the notice sent to the person's own e-mail ----------------------------------------------- //

// Display names that say nothing about the employer: the mail system's own name, or a role.
const GENERIC_SENDER_RE = /^(?:no-?reply|do-?not-?reply|notifications?|workday|greenhouse|lever|ashby|icims|smartrecruiters|workable|teamtailor|recruitee|adp|taleo|jobvite|bamboohr|successfactors|careers?|jobs?|recruiting|recruitment|talent acquisition|hiring team|hr|human resources|mailer|system|admin)$/i;
const SENDER_TAIL_RE = /\s+(?:via .+|recruiting(?: team)?|recruitment(?: team)?|careers?(?: team)?|talent(?: acquisition)?(?: team)?|hiring(?: team)?|jobs|hr|human resources|people team|team|notifications?|no-?reply)$/i;

/** The employer's name as the sender's display name gives it ("Acme Robotics Recruiting" ->
 * "Acme Robotics"), or "" when the name is the mail system's or a bare role. */
export function companyOf(senderName: string): string {
  let name = displayName(senderName).replace(/\s*[@|].*$/, "").trim();
  for (let i = 0; i < 3; i++) name = name.replace(SENDER_TAIL_RE, "").trim();
  return name.length < 2 || GENERIC_SENDER_RE.test(name) ? "" : name;
}

export interface Notice { subject: string; text: string }

/** The short note that replaces the raw forward in "notice" mode: who wrote, the first ~300
 * characters as a quote, and where to read the rest. Plain text, no marketing. */
export function noticeFor(m: { senderName: string; senderAddress: string; subject: string; text: string; alias: string }): Notice {
  const company = companyOf(m.senderName);
  const who = company || displayName(m.senderName) || m.senderAddress || "Someone";
  const label = labelOf(m.subject, m.text);
  let subject: string;
  let first: string;
  if (label === "received") {
    subject = company ? `${company} received your application` : "Your application was received";
    first = company ? `${company} confirmed that your application arrived.` : "An employer confirmed that your application arrived.";
  } else if (label === "rejected" || label === "interview" || label === "action") {
    subject = company ? `An update on your application to ${company}` : "An update on your application";
    first = `${who} sent an update about your application.`;
  } else {
    subject = `${who} sent you a message`;
    first = `${who} wrote to your SponsorJobs address.`;
  }
  const flat = m.text.replace(/\s+/g, " ").trim();
  const quote = flat.length > 300 ? `${flat.slice(0, 300).trimEnd()}...` : flat;
  const lines = [
    first,
    `It was sent to ${m.alias}${m.subject ? ` with the subject "${m.subject.slice(0, FIELD_CAP)}"` : ""}.`,
    "",
    ...(quote ? [`> ${quote}`, ""] : []),
    "Open SponsorJobs and go to Inbox to read it.",
    "sponsorjobs://inbox",
    "",
    "A reply to this e-mail goes to the sender, not to SponsorJobs.",
  ];
  return { subject: subject.replace(/[\r\n]+/g, " ").slice(0, 200), text: lines.join("\n") };
}

/** Cloudflare Email Routing hands every message for the domain here. Unknown alias: a permanent
 * reject, so the sender learns the address does not exist. Known alias: record the code or link
 * (if any) for the apply agent, keep the message for the app's Inbox to download, then tell the
 * person's own e-mail according to the alias's notify_mode: the whole message ("forward"), a
 * short notice ("notice") or nothing ("off"). A message that could not be stored is always
 * forwarded whole, whatever the mode, so a storage problem never loses mail. A send failure is
 * logged and the event is kept: the agent must still get the code. */
export async function receiveMail(message: ForwardableEmailMessage, env: Env, now: number): Promise<void> {
  const local = localOf(message.to);
  const row = local
    ? await env.DB.prepare("SELECT user, forward_to, notify_mode FROM mail_aliases WHERE local = ?").bind(local)
        .first<{ user: string; forward_to: string; notify_mode: string }>()
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
  const readable = parsed.text ?? (parsed.html ? htmlToText(parsed.html) : "");
  // File names only; inline images that belong to the HTML are not attachments to the reader.
  const names = (parsed.attachments ?? []).filter((a) => !a.related)
    .map((a) => String(a.filename || "attachment").replace(/[\r\n]+/g, " ").trim() || "attachment");
  const stored = await storeMessage(env, row.user, local, {
    sender: fromAddress, senderName: fromName, subject, text: readable, html: parsed.html || undefined, names,
  }, now);
  const alias = aliasOf(local);
  const mode: NotifyMode = stored ? notifyMode(row.notify_mode) : "forward";
  if (mode === "off") return;
  const mailer = mailerFrom(env);
  if (!mailer) {
    console.error(`mail for ${row.user} not forwarded: no mailer configured`);
    return;
  }
  try {
    if (mode === "notice") {
      const n = noticeFor({ senderName: fromName, senderAddress: fromAddress, subject, text: readable, alias });
      await mailer.send({
        to: row.forward_to,
        from: `"SponsorJobs" <${senderAddress(env)}>`,
        reply_to: fromAddress || undefined,
        subject: n.subject,
        text: n.text,
      });
    } else {
      const notice = `Forwarded by SponsorJobs from ${alias}; attachments are not forwarded`;
      const text = `${notice}\n\n${readable}`.slice(0, BODY_CAP);
      const html = parsed.html
        ? `<p style="font:13px system-ui;color:#555">${notice.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</p>\n${parsed.html}`.slice(0, BODY_CAP)
        : undefined;
      await mailer.send({
        to: row.forward_to,
        from: `"${displayName(fromName || fromAddress || "Unknown sender")} via SponsorJobs" <${senderAddress(env)}>`,
        reply_to: fromAddress || undefined,
        subject: subject || "(no subject)",
        text,
        html,
      });
    }
    await countSend(env.DB, now);
  } catch (err) {
    console.error(`mail forward failed (${mailer.name}) for ${row.user}:`, err instanceof Error ? err.message : err);
  }
}
