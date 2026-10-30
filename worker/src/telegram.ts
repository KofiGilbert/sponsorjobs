// The official SponsorJobs Telegram bot, relayed by this Worker (docs/notify.md). A port of
// backend/telegram_relay.py with the same routes, status codes and error strings, so the desktop
// app's OfficialBotChannel (notify/channel.py) works unchanged against either broker.
//
// One bot for everyone: the token lives ONLY here (the TELEGRAM_BOT_TOKEN secret), so a person
// connects by tapping a t.me/<bot>?start=<code> link instead of making a bot of their own.
//
// The Worker is a RELAY, not a store of personal data. It never sees a resume. D1 keeps exactly
// (migrations/0008_telegram.sql):
//   * telegram_links  : the sha256 of a one-time link code -> account, 15 minutes, deleted on use;
//   * telegram_chats  : account -> chat id, Telegram username, paused flag, linked time;
//   * telegram_events : taps / replies waiting for the app, deleted once acknowledged or after 7 days;
//   * telegram_sends  : one timestamp per message sent or edited, for the rate limit, 24 hours.
// Outgoing messages carry only what the app sends and are not stored. A CV preview picture or PDF
// (send_media) is passed straight through to Telegram inside the same request and never written
// anywhere; only its size is logged. Every photo and document goes out with protect_content.
//
// Rate limits: the Python broker kept them in memory (one process). A Worker runs in many isolates
// at once, so the counters are rows in D1, checked and taken in ONE guarded INSERT so parallel
// sends cannot slip past (the same idea as sign_limits in botauth.ts).

import { identify, sha256Hex, tokenUrlsafe } from "./accounts";
import type { Env } from "./env";

export const LINK_TTL = 900;                       // seconds a /start code stays valid
export const EVENT_TTL = 7 * 24 * 3600;            // inbox events are dropped after 7 days
export const INBOX_LIMIT = 50;
export const SEND_PER_DAY = 100;                   // a CV review session uses about 10
export const SEND_MIN_INTERVAL_MS = 3000;
export const EDIT_PER_DAY = 200;
export const EDIT_MIN_INTERVAL_MS = 1000;
export const MAX_TEXT = 4096;                      // Telegram's own message limit
export const MAX_EVENT_TEXT = 1000;                // what is kept of a typed reply
export const MAX_CAPTION = 1024;
export const MAX_PHOTO_BYTES = 10 * 1024 * 1024;
export const MAX_DOCUMENT_BYTES = 20 * 1024 * 1024;
const DAY_MS = 86_400_000;
const MAX_ROWS = 3;
const MAX_COLS = 3;
const MAX_BUTTON_ID = 40;
const MAX_BUTTON_LABEL = 30;
const BUTTON_ID_RE = /^[A-Za-z0-9:_-]+$/;

export const TELEGRAM_API = "https://api.telegram.org";

export const MSG_CONNECTED = "Connected. I'll message you about new jobs and applications. Send /stop to pause.";
export const MSG_EXPIRED = "That link expired, open SponsorJobs and try again.";
export const MSG_UNBOUND = "Open SponsorJobs, Settings, Notifications to connect.";
export const MSG_PAUSED = "Paused. I won't message you until you send /resume.";
export const MSG_RESUMED = "Resumed. I'll message you about new jobs and applications again.";
export const MSG_HELP = "I'm the SponsorJobs bot. I message you about new jobs and applications.\n" +
  "/stop pauses messages, /resume turns them back on.\n" +
  "To disconnect, open SponsorJobs, Settings, Notifications.";
export const MSG_DISCONNECTED = "Disconnected from SponsorJobs.";
// A file, picture or voice note from a linked chat. The bot can't open one, so it says so at once
// instead of queueing nothing and leaving the app to guess (2026-10-30: a resume photo and PDF were
// dropped without a word). The app's own-bot channel sends the same (notify/channel.py FILES_REPLY).
export const MSG_NO_FILES = "I can't open files here yet. To add your resume, open SponsorJobs and go to Resumes, then Upload.";
export const ATTACHMENT_KINDS = ["document", "photo", "voice", "video", "audio", "sticker", "video_note", "animation"] as const;

const json = (data: unknown, status = 200, headers: Record<string, string> = {}): Response =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json", ...headers } });

const off = () => json({ error: "telegram_not_configured" }, 503);
const noUser = () => json({ error: "no user" }, 401);

interface Bot { token: string; username: string; secret: string }

/** The bot, only when all three secrets are set; a half configuration stays off (503). */
export function botFrom(env: Env): Bot | null {
  const token = (env.TELEGRAM_BOT_TOKEN ?? "").trim();
  const username = (env.TELEGRAM_BOT_USERNAME ?? "").trim().replace(/^@+/, "");
  const secret = (env.TELEGRAM_WEBHOOK_SECRET ?? "").trim();
  return token && username && secret ? { token, username, secret } : null;
}

// ---- small helpers ---------------------------------------------------------------------------- //

/** Length and slicing in code points, as Python's len() and [:n] count (the app chunks by those). */
const cpLen = (s: string): number => [...s].length;
const cpSlice = (s: string, n: number): string => {
  const cps = [...s];
  return cps.length <= n ? s : cps.slice(0, n).join("");
};

const iso = (seconds: number): string => new Date(seconds * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");

/** Python truthiness for the "buttons or []" default: null, false, 0, "", [] and {} are empty. */
function pyFalsy(v: unknown): boolean {
  if (v === null || v === undefined || v === false || v === 0 || v === "") return true;
  if (Array.isArray(v)) return v.length === 0;
  if (typeof v === "object") return Object.keys(v as object).length === 0;
  return false;
}

async function jsonBody(req: Request): Promise<Record<string, unknown>> {
  try {
    const b = await req.json();
    return b && typeof b === "object" && !Array.isArray(b) ? (b as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

// ---- validation (backend/telegram_relay.py validate_*) ---------------------------------------- //

export class BadRequest extends Error {}

export interface Button { id: string; label: string }

/** Rows of {id, label}; throws BadRequest with a short reason. */
export function validateButtons(buttons: unknown): Button[][] {
  const rows = pyFalsy(buttons) ? [] : buttons;
  if (!Array.isArray(rows) || rows.length > MAX_ROWS) throw new BadRequest(`buttons must be at most ${MAX_ROWS} rows`);
  return rows.map((row) => {
    if (!Array.isArray(row) || row.length === 0 || row.length > MAX_COLS) {
      throw new BadRequest(`each button row must hold 1 to ${MAX_COLS} buttons`);
    }
    return row.map((b) => {
      if (!b || typeof b !== "object" || Array.isArray(b)) throw new BadRequest("a button must be {id, label}");
      const { id, label } = b as Record<string, unknown>;
      if (typeof id !== "string" || !BUTTON_ID_RE.test(id) || id.length > MAX_BUTTON_ID) {
        throw new BadRequest("a button id must be 1-40 chars of A-Z a-z 0-9 : _ -");
      }
      if (typeof label !== "string" || !label.trim() || cpLen(label) > MAX_BUTTON_LABEL) {
        throw new BadRequest(`a button label must be 1-${MAX_BUTTON_LABEL} characters`);
      }
      return { id, label };
    });
  });
}

export function validateSend(body: Record<string, unknown>): { text: string; buttons: Button[][] } {
  const text = body.text;
  if (typeof text !== "string" || !text.trim()) throw new BadRequest("text is required");
  if (cpLen(text) > MAX_TEXT) throw new BadRequest(`text is longer than ${MAX_TEXT} characters`);
  return { text, buttons: validateButtons(body.buttons) };
}

const markup = (buttons: Button[][]) =>
  ({ inline_keyboard: buttons.map((row) => row.map((b) => ({ text: b.label, callback_data: b.id }))) });

// ---- the Telegram Bot API --------------------------------------------------------------------- //

/** A Bot API call failed: network (no status), an HTTP error, or ok:false. */
export class TelegramError extends Error {
  constructor(message: string, readonly status: number | null = null) { super(message); }
}

/** POST one Bot API method; returns its `result`. The URL holds the token, so it is never put in
 * an error message or a log line. */
async function tg(bot: Bot, method: string, payload: Record<string, unknown> | FormData,
                  timeoutMs = 10_000): Promise<Record<string, unknown>> {
  const isForm = payload instanceof FormData;
  let res: Response;
  try {
    res = await fetch(`${TELEGRAM_API}/bot${bot.token}/${method}`, {
      method: "POST",
      headers: isForm ? undefined : { "content-type": "application/json" },
      body: isForm ? payload : JSON.stringify(payload),
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (err) {
    throw new TelegramError(err instanceof Error ? err.name : "network error");
  }
  let out: Record<string, unknown> = {};
  try { out = ((await res.json()) ?? {}) as Record<string, unknown>; } catch { /* not JSON */ }
  const description = typeof out.description === "string" ? out.description : "";
  if (!res.ok) throw new TelegramError(description || `HTTP ${res.status}`, res.status);
  if (!out.ok) {
    throw new TelegramError(description || "telegram error", typeof out.error_code === "number" ? out.error_code : null);
  }
  const result = out.result;
  return result && typeof result === "object" ? (result as Record<string, unknown>) : {};
}

/** A courtesy reply: a failure is not worth failing the request over. */
async function say(bot: Bot, chatId: number, text: string): Promise<void> {
  try { await tg(bot, "sendMessage", { chat_id: chatId, text }); } catch { /* ignored */ }
}

const messageId = (r: Record<string, unknown>): number => Number(r.message_id) || 0;

// ---- storage ---------------------------------------------------------------------------------- //

interface Chat { account: string; chat_id: number; username: string | null; paused: number; linked_at: string }

const getChat = (db: D1Database, account: string) =>
  db.prepare("SELECT account, chat_id, username, paused, linked_at FROM telegram_chats WHERE account = ?")
    .bind(account).first<Chat>();

const accountForChat = async (db: D1Database, chatId: number): Promise<string | null> =>
  (await db.prepare("SELECT account FROM telegram_chats WHERE chat_id = ?").bind(chatId)
    .first<{ account: string }>())?.account ?? null;

/** Drop the binding and anything still queued for the app. */
const unbind = (db: D1Database, account: string) => db.batch([
  db.prepare("DELETE FROM telegram_chats WHERE account = ?").bind(account),
  db.prepare("DELETE FROM telegram_events WHERE account = ?").bind(account),
]);

const addEvent = (db: D1Database, account: string, type: string, data: string, text: string, at: number) =>
  db.prepare("INSERT INTO telegram_events(account, type, data, text, at) VALUES(?, ?, ?, ?, ?)")
    .bind(account, type, data, text, at).run();

/** Reserve one send ("send") or edit ("edit") slot for this account. Returns the seconds to wait
 * when over a limit, else null (and the slot is taken). The check and the take are one guarded
 * INSERT, so two parallel sends cannot both get the last slot; old rows are swept first. */
export async function rateCheck(db: D1Database, account: string, kind: "send" | "edit",
                                nowMs: number): Promise<number | null> {
  const [interval, perDay] = kind === "send" ? [SEND_MIN_INTERVAL_MS, SEND_PER_DAY] : [EDIT_MIN_INTERVAL_MS, EDIT_PER_DAY];
  const [, taken] = await db.batch([
    db.prepare("DELETE FROM telegram_sends WHERE at <= ?").bind(nowMs - DAY_MS),
    db.prepare(
      "INSERT INTO telegram_sends(account, kind, at) SELECT ?1, ?2, ?3 " +
      "WHERE NOT EXISTS (SELECT 1 FROM telegram_sends WHERE account = ?1 AND kind = ?2 AND at > ?3 - ?4) " +
      "AND (SELECT COUNT(*) FROM telegram_sends WHERE account = ?1 AND kind = ?2 AND at > ?3 - ?5) < ?6 " +
      "RETURNING at").bind(account, kind, nowMs, interval, DAY_MS, perDay),
  ]);
  if (taken!.results.length > 0) return null;
  const row = await db.prepare(
    "SELECT MAX(at) AS last, MIN(at) AS first, COUNT(*) AS n FROM telegram_sends " +
    "WHERE account = ? AND kind = ? AND at > ?").bind(account, kind, nowMs - DAY_MS)
    .first<{ last: number | null; first: number | null; n: number }>();
  if (row?.last != null && nowMs - row.last < interval) return (interval - (nowMs - row.last)) / 1000;
  if (row?.first != null && row.n >= perDay) return (row.first + DAY_MS - nowMs) / 1000;
  return interval / 1000;   // a parallel send took the slot between the two reads
}

function rateLimited(wait: number): Response {
  const retry = Math.max(1, Math.ceil(wait));
  return json({ error: "rate_limited", retry_after: retry }, 429, { "Retry-After": String(retry) });
}

/** Linked, not paused, inside the rate limit: the chat, or the response to answer instead. */
async function gate(env: Env, user: string, kind: "send" | "edit", nowMs: number): Promise<Chat | Response> {
  const chat = await getChat(env.DB, user);
  if (!chat) return json({ error: "not_linked" }, 409);
  if (chat.paused) return json({ error: "paused" }, 409);
  const wait = await rateCheck(env.DB, user, kind, nowMs);
  return wait === null ? chat : rateLimited(wait);
}

/** Telegram failed: a 403 means the person blocked the bot or deleted the chat, so the link is
 * dead and the app is told to show "connect" again; anything else is a 502. */
async function upstreamFailed(env: Env, user: string, err: unknown): Promise<Response> {
  if (err instanceof TelegramError && err.status === 403) {
    await unbind(env.DB, user);
    return json({ error: "not_linked" }, 409);
  }
  console.warn("telegram call failed:", err instanceof Error ? err.message : err);
  return json({ error: "telegram_unavailable" }, 502);
}

// ---- routes ----------------------------------------------------------------------------------- //

export interface TgCtx { req: Request; env: Env; now: number /* unix seconds */; nowMs: number }

/** Wrap a /notify/telegram/* handler: 503 when the bot is off, 401 with no account. */
const authed = (h: (c: TgCtx, bot: Bot, user: string) => Promise<Response>) => async (c: TgCtx): Promise<Response> => {
  const bot = botFrom(c.env);
  if (!bot) return off();
  const user = await identify(c.req, c.env);
  if (!user) return noUser();
  return h(c, bot, user);
};

/** POST /notify/telegram/link -> {code, url, expires_in}. The code is stored only as its hash. */
export const link = authed(async ({ env, now }, bot, user) => {
  const code = tokenUrlsafe(18);                       // [A-Za-z0-9_-], fits Telegram's 64
  await env.DB.batch([
    env.DB.prepare("DELETE FROM telegram_links WHERE expires_at <= ?").bind(now),
    env.DB.prepare("INSERT INTO telegram_links(code_hash, account, expires_at) VALUES(?, ?, ?)")
      .bind(await sha256Hex(code), user, now + LINK_TTL),
  ]);
  return json({ code, url: `https://t.me/${bot.username}?start=${code}`, expires_in: LINK_TTL });
});

/** GET /notify/telegram/status -> {linked, username?, linked_at?, paused?}. */
export const status = authed(async ({ env }, _bot, user) => {
  const chat = await getChat(env.DB, user);
  if (!chat) return json({ linked: false });
  const out: Record<string, unknown> = { linked: true, linked_at: chat.linked_at, paused: Boolean(chat.paused) };
  if (chat.username) out.username = chat.username;
  return json(out);
});

/** POST /notify/telegram/unlink -> {linked: false}. Tells the chat, then forgets it. */
export const unlink = authed(async ({ env }, bot, user) => {
  const chat = await getChat(env.DB, user);
  if (chat) {
    await say(bot, chat.chat_id, MSG_DISCONNECTED);
    await unbind(env.DB, user);
  }
  return json({ linked: false });
});

/** POST /notify/telegram/send {text, buttons?} -> {ok, message_id}. A bad body uses no quota. */
export const send = authed(async ({ req, env, nowMs }, bot, user) => {
  let msg;
  try { msg = validateSend(await jsonBody(req)); } catch (e) {
    if (e instanceof BadRequest) return json({ error: e.message }, 400);
    throw e;
  }
  const chat = await gate(env, user, "send", nowMs);
  if (chat instanceof Response) return chat;
  const payload: Record<string, unknown> = { chat_id: chat.chat_id, text: msg.text, disable_web_page_preview: true };
  if (msg.buttons.length) payload.reply_markup = markup(msg.buttons);
  try {
    return json({ ok: true, message_id: messageId(await tg(bot, "sendMessage", payload)) });
  } catch (err) {
    return upstreamFailed(env, user, err);
  }
});

/** POST /notify/telegram/send_media (multipart: kind, file, caption?, buttons? as JSON): a CV
 * preview picture or PDF, passed through to Telegram and never stored. protect_content always. */
export const sendMedia = authed(async ({ req, env, nowMs }, bot, user) => {
  if (Number(req.headers.get("content-length") ?? 0) > MAX_DOCUMENT_BYTES + 64 * 1024) {
    return json({ error: "file too large" }, 413);
  }
  let form: FormData;
  try { form = await req.formData(); } catch { return json({ error: "a multipart form is required" }, 400); }
  const kind = String(form.get("kind") ?? "");
  if (kind !== "photo" && kind !== "document") return json({ error: 'kind must be "photo" or "document"' }, 400);
  const caption = String(form.get("caption") ?? "");
  if (cpLen(caption) > MAX_CAPTION) return json({ error: `caption is longer than ${MAX_CAPTION} characters` }, 400);
  let buttons: Button[][];
  try {
    const raw = String(form.get("buttons") ?? "");
    let parsed: unknown = [];
    if (raw) {
      try { parsed = JSON.parse(raw); } catch { throw new BadRequest("buttons must be JSON"); }
    }
    buttons = validateButtons(parsed);
  } catch (e) {
    if (e instanceof BadRequest) return json({ error: e.message }, 400);
    throw e;
  }
  const file = form.get("file");
  if (file === null || typeof file === "string") return json({ error: "file is required" }, 400);
  const cap = kind === "photo" ? MAX_PHOTO_BYTES : MAX_DOCUMENT_BYTES;
  if (file.size === 0) return json({ error: "file is empty" }, 400);
  if (file.size > cap) return json({ error: `${kind} is larger than ${cap / (1024 * 1024)} MB` }, 413);
  const chat = await gate(env, user, "send", nowMs);
  if (chat instanceof Response) return chat;
  const [method, field, name, ctype] = kind === "photo"
    ? ["sendPhoto", "photo", "cv.png", "image/png"] : ["sendDocument", "document", "cv.pdf", "application/pdf"];
  const out = new FormData();
  out.set("chat_id", String(chat.chat_id));
  out.set("protect_content", "true");
  if (caption) out.set("caption", caption);
  if (buttons.length) out.set("reply_markup", JSON.stringify(markup(buttons)));
  out.set(field, new File([await file.arrayBuffer()], name, { type: ctype }));
  console.log(`telegram media: ${kind}, ${file.size} bytes`);   // size only, never content
  try {
    return json({ ok: true, message_id: messageId(await tg(bot, method, out, 60_000)) });
  } catch (err) {
    return upstreamFailed(env, user, err);
  }
});

/** Python's int() for message_id: an integer, a whole-number string, or a number (truncated). */
function intOf(v: unknown): number | null {
  if (typeof v === "boolean") return v ? 1 : 0;
  if (typeof v === "number" && Number.isFinite(v)) return Math.trunc(v);
  if (typeof v === "string" && /^\s*[+-]?\d+\s*$/.test(v)) return Number.parseInt(v, 10);
  return null;
}

/** POST /notify/telegram/edit {message_id, text? | caption?, buttons?} -> {ok: true}. */
export const edit = authed(async ({ req, env, nowMs }, bot, user) => {
  const body = await jsonBody(req);
  const mid = intOf(body.message_id);
  if (mid === null) return json({ error: "message_id is required" }, 400);
  const text = body.text ?? null;
  const caption = body.caption ?? null;
  if (text !== null && (typeof text !== "string" || !text.trim() || cpLen(text) > MAX_TEXT)) {
    return json({ error: `text must be 1 to ${MAX_TEXT} characters` }, 400);
  }
  if (caption !== null && (typeof caption !== "string" || cpLen(caption) > MAX_CAPTION)) {
    return json({ error: `caption is longer than ${MAX_CAPTION} characters` }, 400);
  }
  let buttons: Button[][] | null = null;
  try { if ("buttons" in body) buttons = validateButtons(body.buttons); } catch (e) {
    if (e instanceof BadRequest) return json({ error: e.message }, 400);
    throw e;
  }
  if (text === null && caption === null && buttons === null) return json({ error: "nothing to edit" }, 400);
  const chat = await gate(env, user, "edit", nowMs);
  if (chat instanceof Response) return chat;
  const payload: Record<string, unknown> = { chat_id: chat.chat_id, message_id: mid };
  if (buttons !== null) payload.reply_markup = markup(buttons);
  try {
    if (text !== null) await tg(bot, "editMessageText", { ...payload, text, disable_web_page_preview: true });
    else if (caption !== null) await tg(bot, "editMessageCaption", { ...payload, caption });
    else await tg(bot, "editMessageReplyMarkup", payload);
  } catch (err) {
    if (err instanceof TelegramError && err.status === 400) return json({ error: "cannot_edit" }, 409);
    return upstreamFailed(env, user, err);
  }
  return json({ ok: true });
});

interface EventRow { id: number; type: string; data: string | null; text: string | null; at: number }

/** GET /notify/telegram/inbox?after=<cursor>: this account's events, oldest first, at most 50.
 * `after=N` acknowledges (deletes) every event with cursor <= N; events older than 7 days are
 * dropped on every read. */
export const inbox = authed(async ({ req, env, now }, _bot, user) => {
  const raw = new URL(req.url).searchParams.get("after") ?? "";
  let after = 0;
  if (raw) {
    if (!/^\s*[+-]?\d+\s*$/.test(raw)) return json({ error: "bad cursor" }, 400);
    after = Math.max(0, Number.parseInt(raw, 10));
  }
  const [, , rows] = await env.DB.batch<EventRow>([
    env.DB.prepare("DELETE FROM telegram_events WHERE at < ?").bind(now - EVENT_TTL),
    env.DB.prepare("DELETE FROM telegram_events WHERE account = ? AND id <= ?").bind(user, after),
    env.DB.prepare("SELECT id, type, data, text, at FROM telegram_events WHERE account = ? ORDER BY id LIMIT ?")
      .bind(user, INBOX_LIMIT),
  ]);
  const events = rows!.results.map((r) => ({
    cursor: r.id, type: r.type, data: r.data ?? "", text: r.text ?? "", at: iso(r.at),
  }));
  return json({ events, cursor: events.length ? events[events.length - 1]!.cursor : after });
});

// ---- webhook (Telegram calls this) ------------------------------------------------------------ //

const enc = new TextEncoder();

function secretOk(header: string | null, secret: string): boolean {
  const a = enc.encode(header ?? "");
  const b = enc.encode(secret);
  return Boolean(secret) && a.byteLength === b.byteLength && crypto.subtle.timingSafeEqual(a, b);
}

type Obj = Record<string, unknown>;
const obj = (v: unknown): Obj | null => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : null);
const chatIdOf = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

export async function handleUpdate(env: Env, bot: Bot, update: Obj, now: number): Promise<void> {
  const db = env.DB;
  const cq = obj(update.callback_query);
  if (cq) return handleCallback(env, bot, cq, now);
  const msg = obj(update.message);
  if (!msg) return;                                           // edits, channel posts: ignored
  const chat = obj(msg.chat) ?? {};
  const chatId = chatIdOf(chat.id);
  if (chatId === null || ("type" in chat ? chat.type : "private") !== "private") return;   // private chats only
  const text = String(msg.text ?? "").trim();
  const from = obj(msg.from) ?? {};
  const uname = from.username || chat.username;
  const username = typeof uname === "string" && uname ? uname : null;
  const account = await accountForChat(db, chatId);

  if (text.startsWith("/")) {
    const sp = text.indexOf(" ");
    const head = sp < 0 ? text : text.slice(0, sp);
    const arg = sp < 0 ? "" : text.slice(sp + 1).trim();
    const cmd = head.slice(1).split("@", 1)[0]!.toLowerCase();
    if (cmd === "start" && arg) {
      const bound = await db.batch<{ account: string }>([
        db.prepare("DELETE FROM telegram_links WHERE expires_at <= ?").bind(now),
        db.prepare("DELETE FROM telegram_links WHERE code_hash = ? RETURNING account").bind(await sha256Hex(arg)),
      ]);
      const target = bound[1]!.results[0]?.account;
      if (!target) return say(bot, chatId, MSG_EXPIRED);
      await db.batch([
        // One chat belongs to one account: a chat re-linked to another account moves.
        db.prepare("DELETE FROM telegram_chats WHERE chat_id = ? AND account != ?").bind(chatId, target),
        db.prepare(
          "INSERT INTO telegram_chats(account, chat_id, username, paused, linked_at) VALUES(?, ?, ?, 0, ?) " +
          "ON CONFLICT(account) DO UPDATE SET chat_id = excluded.chat_id, username = excluded.username, " +
          "paused = 0, linked_at = excluded.linked_at").bind(target, chatId, username, iso(now)),
      ]);
      return say(bot, chatId, MSG_CONNECTED);
    }
    if (account === null) return say(bot, chatId, MSG_UNBOUND);
    if (cmd === "start") return say(bot, chatId, MSG_CONNECTED);
    if (cmd === "stop" || cmd === "resume") {
      await db.batch([
        db.prepare("UPDATE telegram_chats SET paused = ? WHERE account = ?").bind(cmd === "stop" ? 1 : 0, account),
        db.prepare("INSERT INTO telegram_events(account, type, data, text, at) VALUES(?, 'command', ?, '', ?)")
          .bind(account, cmd, now),
      ]);
      return say(bot, chatId, cmd === "stop" ? MSG_PAUSED : MSG_RESUMED);
    }
    if (cmd === "help") return say(bot, chatId, MSG_HELP);
    await addEvent(db, account, "command", cpSlice(cmd, 40), cpSlice(arg, MAX_EVENT_TEXT), now);
    return;
  }

  if (account === null) return say(bot, chatId, MSG_UNBOUND);
  if (!text && ATTACHMENT_KINDS.some((k) => msg[k])) {
    // Nothing is queued for the file itself; a caption is the person's words, so it goes on as text.
    const caption = String(msg.caption ?? "").trim();
    if (caption) await addEvent(db, account, "text", "", cpSlice(caption, MAX_EVENT_TEXT), now);
    return say(bot, chatId, MSG_NO_FILES);
  }
  if (text) await addEvent(db, account, "text", "", cpSlice(text, MAX_EVENT_TEXT), now);
}

async function handleCallback(env: Env, bot: Bot, cq: Obj, now: number): Promise<void> {
  const chatId = chatIdOf(obj(obj(cq.message)?.chat)?.id) ?? chatIdOf(obj(cq.from)?.id);
  const account = chatId === null ? null : await accountForChat(env.DB, chatId);
  try {
    await tg(bot, "answerCallbackQuery", { callback_query_id: cq.id, text: account ? "Got it" : MSG_UNBOUND });
  } catch { /* the toast is a courtesy */ }
  if (account === null) return;
  await addEvent(env.DB, account, "button", cpSlice(String(cq.data ?? ""), 64), "", now);
}

/** POST /telegram/webhook: the secret header must match (403 otherwise). An authentic call always
 * gets 200, even when handling it failed, so Telegram does not retry it forever. */
export async function webhook({ req, env, now }: TgCtx): Promise<Response> {
  const bot = botFrom(env);
  if (!bot) return off();
  if (!secretOk(req.headers.get("X-Telegram-Bot-Api-Secret-Token"), bot.secret)) {
    return json({ error: "forbidden" }, 403);
  }
  try {
    await handleUpdate(env, bot, await jsonBody(req), now);
  } catch (err) {
    console.error("telegram webhook update failed:", err instanceof Error ? err.message : err);
  }
  return json({ ok: true });
}
