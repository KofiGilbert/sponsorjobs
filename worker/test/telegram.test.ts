// The official Telegram bot relay (src/telegram.ts, docs/notify.md): the same contract the Python
// broker (backend/telegram_relay.py) answers and the app's OfficialBotChannel calls. No network:
// every call to api.telegram.org is a stubbed fetch that records what the Worker sent.
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Env } from "../src/env";
import worker from "../src/index";
import {
  EDIT_PER_DAY, EVENT_TTL, INBOX_LIMIT, MAX_TEXT, MSG_CONNECTED, MSG_DISCONNECTED, MSG_EXPIRED, MSG_FILE_QUEUED,
  MSG_FILE_TYPE, MSG_NO_MEDIA, MSG_PAUSED, MSG_TOO_BIG, MSG_UNBOUND, rateCheck, SEND_PER_DAY, TELEGRAM_API,
} from "../src/telegram";
import { call, env, newAccount } from "./helpers";

afterEach(() => vi.restoreAllMocks());

const SECRET = "whsec-test_not-real";
const BOT: Partial<Env> = {
  TELEGRAM_BOT_TOKEN: "123456:TEST-not-real",
  TELEGRAM_BOT_USERNAME: "@SponsorJobsBot",
  TELEGRAM_WEBHOOK_SECRET: SECRET,
};

interface TgCall { method: string; body: any; form?: FormData }
type Reply = { status?: number; body: unknown };

/** Stub fetch as the Bot API. `replies` maps a method to its reply (default ok, message_id 77). */
function stubTelegram(replies: Record<string, Reply> = {}) {
  const calls: TgCall[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const req = new Request(input as RequestInfo, init as RequestInit);
    const prefix = `${TELEGRAM_API}/bot${BOT.TELEGRAM_BOT_TOKEN}/`;
    const filePrefix = `${TELEGRAM_API}/file/bot${BOT.TELEGRAM_BOT_TOKEN}/`;
    if (req.url.startsWith(filePrefix)) {          // a file download: replies.download is its body
      calls.push({ method: "download", body: req.url.slice(filePrefix.length) });
      const r = replies.download ?? { body: "" };
      return new Response(String(r.body), { status: r.status ?? 200 });
    }
    if (!req.url.startsWith(prefix)) throw new Error(`unexpected fetch ${req.url}`);
    const method = req.url.slice(prefix.length);
    const ctype = req.headers.get("content-type") ?? "";
    if (ctype.startsWith("multipart/form-data")) {
      const form = await req.formData();
      calls.push({ method, body: Object.fromEntries([...form.entries()].filter(([, v]) => typeof v === "string")), form });
    } else {
      calls.push({ method, body: await req.json() });
    }
    const r = replies[method] ?? { body: { ok: true, result: { message_id: 77 } } };
    return new Response(JSON.stringify(r.body), { status: r.status ?? 200 });
  });
  return calls;
}

const tgCall = (path: string, token: string, o: { body?: unknown; method?: string } = {}) =>
  call(path, { ...o, token, env: BOT });

let updateSeq = 0;
let chatSeq = 900_000_000;
const freshChat = () => ++chatSeq;

/** Telegram delivering one update to the webhook. */
const hook = (update: unknown, secret: string | null = SECRET) =>
  call("/telegram/webhook", {
    body: { update_id: ++updateSeq, ...(update as object) },
    headers: secret === null ? {} : { "X-Telegram-Bot-Api-Secret-Token": secret },
    env: BOT,
  });

const message = (chatId: number, text: string, extra: Record<string, unknown> = {}) =>
  ({ message: { message_id: 1, chat: { id: chatId, type: "private" }, from: { id: chatId, username: "zoe" }, text, ...extra } });

/** An account linked to a fresh private chat through the real link + /start flow. */
async function linked() {
  const a = await newAccount();
  const chatId = freshChat();
  stubTelegram();
  const l = await tgCall("/notify/telegram/link", a.token, { body: {} });
  await hook(message(chatId, `/start ${l.data.code}`));
  vi.restoreAllMocks();
  return { ...a, chatId };
}

const clearRate = (account: string) => env.DB.prepare("DELETE FROM telegram_sends WHERE account = ?").bind(account).run();

describe("not configured", () => {
  it("every route, including the webhook, answers 503 telegram_not_configured", async () => {
    const a = await newAccount();
    const routes: Array<[string, string]> = [
      ["GET", "/notify/telegram/status"], ["GET", "/notify/telegram/inbox"],
      ["POST", "/notify/telegram/link"], ["POST", "/notify/telegram/unlink"], ["POST", "/notify/telegram/send"],
      ["POST", "/notify/telegram/send_media"], ["POST", "/notify/telegram/edit"], ["POST", "/telegram/webhook"],
    ];
    for (const [method, path] of routes) {
      const r = await call(path, { method, token: a.token, rawBody: method === "POST" ? "{}" : undefined });
      expect([path, r.status, r.data]).toEqual([path, 503, { error: "telegram_not_configured" }]);
    }
  });

  it("a half configuration stays off", async () => {
    const a = await newAccount();
    const r = await call("/notify/telegram/status", { token: a.token, env: { ...BOT, TELEGRAM_WEBHOOK_SECRET: "" } });
    expect(r.status).toBe(503);
  });

  it("with the bot configured, no account is a 401", async () => {
    expect((await call("/notify/telegram/status", { env: BOT })).status).toBe(401);
  });
});

describe("link, webhook /start, status, unlink", () => {
  it("binds the chat to the code's account and reports linked", async () => {
    const a = await newAccount();
    const l = await tgCall("/notify/telegram/link", a.token, { body: {} });
    expect(l.status).toBe(200);
    expect(l.data.expires_in).toBe(900);
    expect(l.data.url).toBe(`https://t.me/SponsorJobsBot?start=${l.data.code}`);
    expect(l.data.code).toMatch(/^[A-Za-z0-9_-]{20,64}$/);
    // only the hash is stored
    const stored = await env.DB.prepare("SELECT code_hash FROM telegram_links WHERE account = ?").bind(a.id).all();
    expect(stored.results).toHaveLength(1);
    expect(JSON.stringify(stored.results)).not.toContain(l.data.code);

    expect((await tgCall("/notify/telegram/status", a.token)).data).toEqual({ linked: false });
    const calls = stubTelegram();
    const chatId = freshChat();
    expect((await hook(message(chatId, `/start ${l.data.code}`))).status).toBe(200);
    expect(calls).toEqual([{ method: "sendMessage", body: { chat_id: chatId, text: MSG_CONNECTED } }]);
    const s = await tgCall("/notify/telegram/status", a.token);
    expect(s.data).toMatchObject({ linked: true, username: "zoe", paused: false });
    expect(s.data.linked_at).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);

    // the code is one-time
    const other = freshChat();
    await hook(message(other, `/start ${l.data.code}`));
    expect(calls.at(-1)).toEqual({ method: "sendMessage", body: { chat_id: other, text: MSG_EXPIRED } });
  });

  it("an expired code is refused", async () => {
    const a = await newAccount();
    const l = await tgCall("/notify/telegram/link", a.token, { body: {} });
    await env.DB.prepare("UPDATE telegram_links SET expires_at = 1 WHERE account = ?").bind(a.id).run();
    const calls = stubTelegram();
    await hook(message(freshChat(), `/start ${l.data.code}`));
    expect(calls[0]!.body.text).toBe(MSG_EXPIRED);
    expect((await tgCall("/notify/telegram/status", a.token)).data).toEqual({ linked: false });
  });

  it("unlink tells the chat, then drops the binding and queued events", async () => {
    const a = await linked();
    const calls = stubTelegram();
    await hook(message(a.chatId, "hello"));
    const r = await tgCall("/notify/telegram/unlink", a.token, { body: {} });
    expect(r.data).toEqual({ linked: false });
    expect(calls.at(-1)).toEqual({ method: "sendMessage", body: { chat_id: a.chatId, text: MSG_DISCONNECTED } });
    expect((await tgCall("/notify/telegram/status", a.token)).data).toEqual({ linked: false });
    const left = await env.DB.prepare("SELECT COUNT(*) AS n FROM telegram_events WHERE account = ?").bind(a.id).first<{ n: number }>();
    expect(left!.n).toBe(0);
  });
});

describe("POST /notify/telegram/send", () => {
  it("sends the text with an inline keyboard and returns the message id", async () => {
    const a = await linked();
    const calls = stubTelegram();
    const buttons = [[{ id: "job:tailor:abc", label: "Tailor and queue" }, { id: "job:skip:abc", label: "Skip" }]];
    const r = await tgCall("/notify/telegram/send", a.token, { body: { text: "New job", buttons } });
    expect(r.status).toBe(200);
    expect(r.data).toEqual({ ok: true, message_id: 77 });
    expect(calls[0]).toEqual({ method: "sendMessage", body: {
      chat_id: a.chatId, text: "New job", disable_web_page_preview: true,
      reply_markup: { inline_keyboard: [[{ text: "Tailor and queue", callback_data: "job:tailor:abc" },
                                          { text: "Skip", callback_data: "job:skip:abc" }]] },
    } });
  });

  it("validates the body (400) without using any quota", async () => {
    const a = await linked();
    const calls = stubTelegram();
    const bad: unknown[] = [
      {}, { text: "  " }, { text: 5 }, { text: "x".repeat(MAX_TEXT + 1) },
      { text: "t", buttons: [[{ id: "a", label: "A" }], [{ id: "b", label: "B" }], [{ id: "c", label: "C" }], [{ id: "d", label: "D" }]] },
      { text: "t", buttons: [[]] },
      { text: "t", buttons: [[{ id: "a", label: "A" }, { id: "b", label: "B" }, { id: "c", label: "C" }, { id: "d", label: "D" }]] },
      { text: "t", buttons: [[{ id: "bad id!", label: "A" }]] },
      { text: "t", buttons: [[{ id: "x".repeat(41), label: "A" }]] },
      { text: "t", buttons: [[{ id: "a", label: "" }]] },
      { text: "t", buttons: [[{ id: "a", label: "y".repeat(31) }]] },
      { text: "t", buttons: [["a"]] },
      { text: "t", buttons: "nope" },
    ];
    for (const body of bad) {
      const r = await tgCall("/notify/telegram/send", a.token, { body });
      expect([body, r.status]).toEqual([body, 400]);
      expect(typeof r.data.error).toBe("string");
    }
    expect(calls).toHaveLength(0);
    // exactly 4096 characters (code points, as Python counts) is fine
    const ok = await tgCall("/notify/telegram/send", a.token, { body: { text: "\u{1F600}".repeat(MAX_TEXT) } });
    expect(ok.status).toBe(200);
  });

  it("409 not_linked with no chat, 409 paused after /stop, sends again after /resume", async () => {
    const lone = await newAccount();
    stubTelegram();
    expect((await tgCall("/notify/telegram/send", lone.token, { body: { text: "hi" } })).data).toEqual({ error: "not_linked" });

    const a = await linked();
    const calls = stubTelegram();
    await hook(message(a.chatId, "/stop"));
    expect(calls.at(-1)!.body.text).toBe(MSG_PAUSED);
    const r = await tgCall("/notify/telegram/send", a.token, { body: { text: "hi" } });
    expect([r.status, r.data]).toEqual([409, { error: "paused" }]);
    expect((await tgCall("/notify/telegram/status", a.token)).data.paused).toBe(true);
    await hook(message(a.chatId, "/resume"));
    expect((await tgCall("/notify/telegram/send", a.token, { body: { text: "hi" } })).status).toBe(200);
  });

  it("429 rate_limited with retry_after and a Retry-After header past 1 per 3 seconds", async () => {
    const a = await linked();
    stubTelegram();
    expect((await tgCall("/notify/telegram/send", a.token, { body: { text: "one" } })).status).toBe(200);
    const res = await worker.fetch(new Request("https://broker.test/notify/telegram/send", {
      method: "POST", body: JSON.stringify({ text: "two" }),
      headers: { Authorization: `Bearer ${a.token}`, "content-type": "application/json" },
    }), { ...env, ...BOT } as Env);
    expect(res.status).toBe(429);
    const data: any = await res.json();
    expect(data.error).toBe("rate_limited");
    expect(data.retry_after).toBeGreaterThanOrEqual(1);
    expect(data.retry_after).toBeLessThanOrEqual(3);
    expect(res.headers.get("Retry-After")).toBe(String(data.retry_after));
  });

  it("the daily cap holds across a rolling 24 hours", async () => {
    const a = await newAccount();
    const now = Date.now();
    await env.DB.batch(Array.from({ length: SEND_PER_DAY }, (_, i) =>
      env.DB.prepare("INSERT INTO telegram_sends(account, kind, at) VALUES(?, 'send', ?)").bind(a.id, now - 3_600_000 - i * 1000)));
    const wait = await rateCheck(env.DB, a.id, "send", now);
    expect(wait).not.toBeNull();
    expect(wait!).toBeGreaterThan(3600);                // the oldest one leaves the window in ~22 h
    // edits have their own budget
    expect(await rateCheck(env.DB, a.id, "edit", now)).toBeNull();
    expect(await rateCheck(env.DB, a.id, "edit", now + 500)).toBeCloseTo(0.5);
    expect(EDIT_PER_DAY).toBe(200);
  });

  it("a 403 from Telegram (bot blocked) drops the binding and answers 409 not_linked", async () => {
    const a = await linked();
    stubTelegram({ sendMessage: { status: 403, body: { ok: false, error_code: 403, description: "Forbidden: bot was blocked by the user" } } });
    const r = await tgCall("/notify/telegram/send", a.token, { body: { text: "hi" } });
    expect([r.status, r.data]).toEqual([409, { error: "not_linked" }]);
    expect((await tgCall("/notify/telegram/status", a.token)).data).toEqual({ linked: false });
  });

  it("any other Telegram failure is 502 telegram_unavailable and keeps the binding", async () => {
    const a = await linked();
    vi.spyOn(console, "warn").mockImplementation(() => {});
    stubTelegram({ sendMessage: { status: 500, body: { ok: false, description: "boom" } } });
    const r = await tgCall("/notify/telegram/send", a.token, { body: { text: "hi" } });
    expect([r.status, r.data]).toEqual([502, { error: "telegram_unavailable" }]);
    expect((await tgCall("/notify/telegram/status", a.token)).data.linked).toBe(true);
  });
});

describe("POST /notify/telegram/send_media", () => {
  const media = (token: string, fields: Record<string, string>, file?: Blob) => {
    const form = new FormData();
    for (const [k, v] of Object.entries(fields)) form.set(k, v);
    if (file) form.set("file", file, "upload.bin");
    return worker.fetch(new Request("https://broker.test/notify/telegram/send_media", {
      method: "POST", body: form, headers: { Authorization: `Bearer ${token}` },
    }), { ...env, ...BOT } as Env);
  };

  it("passes the file through with protect_content and buttons", async () => {
    const a = await linked();
    const calls = stubTelegram();
    vi.spyOn(console, "log").mockImplementation(() => {});
    const res = await media(a.token, { kind: "photo", caption: "Your CV", buttons: JSON.stringify([[{ id: "cv:pdf:1", label: "Send PDF" }]]) },
                            new Blob([new Uint8Array([137, 80, 78, 71])], { type: "image/png" }));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, message_id: 77 });
    expect(calls[0]!.method).toBe("sendPhoto");
    expect(calls[0]!.body).toMatchObject({ chat_id: String(a.chatId), protect_content: "true", caption: "Your CV" });
    expect(JSON.parse(calls[0]!.body.reply_markup)).toEqual({ inline_keyboard: [[{ text: "Send PDF", callback_data: "cv:pdf:1" }]] });
    const f = calls[0]!.form!.get("photo") as File;
    expect(f.name).toBe("cv.png");
    expect(f.size).toBe(4);
  });

  it("rejects a bad form with 400 and no quota used", async () => {
    const a = await linked();
    const calls = stubTelegram();
    const png = new Blob([new Uint8Array([1])]);
    expect((await media(a.token, { kind: "video" }, png)).status).toBe(400);
    expect((await media(a.token, { kind: "photo" })).status).toBe(400);
    expect((await media(a.token, { kind: "photo", buttons: "{" }, png)).status).toBe(400);
    expect((await media(a.token, { kind: "photo", caption: "c".repeat(1025) }, png)).status).toBe(400);
    expect((await media(a.token, { kind: "document" }, new Blob([]))).status).toBe(400);
    expect(calls).toHaveLength(0);
    expect((await media(a.token, { kind: "document" }, png)).status).toBe(200);
    expect(calls[0]!.method).toBe("sendDocument");
  });
});

describe("POST /notify/telegram/edit", () => {
  it("picks the Bot API method from the fields, and 409 cannot_edit when Telegram refuses", async () => {
    const a = await linked();
    const calls = stubTelegram({ editMessageCaption: { status: 400, body: { ok: false, error_code: 400, description: "message is not modified" } } });
    expect((await tgCall("/notify/telegram/edit", a.token, { body: { message_id: 5, buttons: [] } })).data).toEqual({ ok: true });
    expect(calls[0]).toEqual({ method: "editMessageReplyMarkup", body: { chat_id: a.chatId, message_id: 5, reply_markup: { inline_keyboard: [] } } });
    await clearRate(a.id);
    await tgCall("/notify/telegram/edit", a.token, { body: { message_id: "6", text: "new" } });
    expect(calls[1]!.method).toBe("editMessageText");
    await clearRate(a.id);
    const r = await tgCall("/notify/telegram/edit", a.token, { body: { message_id: 7, caption: "c" } });
    expect([r.status, r.data]).toEqual([409, { error: "cannot_edit" }]);
    expect((await tgCall("/notify/telegram/edit", a.token, { body: { message_id: 7 } })).status).toBe(400);
    expect((await tgCall("/notify/telegram/edit", a.token, { body: { text: "x" } })).status).toBe(400);
  });
});

describe("GET /notify/telegram/inbox", () => {
  it("queues taps, text and commands; after=N acknowledges", async () => {
    const a = await linked();
    const calls = stubTelegram();
    await hook({ callback_query: { id: "cq1", data: "job:skip:abc", from: { id: a.chatId },
                                   message: { message_id: 9, chat: { id: a.chatId, type: "private" } } } });
    expect(calls[0]).toEqual({ method: "answerCallbackQuery", body: { callback_query_id: "cq1", text: "Got it" } });
    await hook(message(a.chatId, "x".repeat(1500)));
    await hook(message(a.chatId, "/approve 12"));
    await hook(message(a.chatId, "/stop"));

    const r = await tgCall("/notify/telegram/inbox", a.token);
    expect(r.status).toBe(200);
    const ev = r.data.events;
    expect(ev.map((e: any) => [e.type, e.data, e.text.length])).toEqual([
      ["button", "job:skip:abc", 0], ["text", "", 1000], ["command", "approve", 2], ["command", "stop", 0],
    ]);
    expect(ev[2].text).toBe("12");
    expect(ev[0].at).toMatch(/Z$/);
    expect(r.data.cursor).toBe(ev[3].cursor);

    const r2 = await tgCall(`/notify/telegram/inbox?after=${ev[1].cursor}`, a.token);
    expect(r2.data.events.map((e: any) => e.cursor)).toEqual([ev[2].cursor, ev[3].cursor]);
    const r3 = await tgCall(`/notify/telegram/inbox?after=${ev[3].cursor}`, a.token);
    expect(r3.data).toEqual({ events: [], cursor: ev[3].cursor });
    expect((await tgCall("/notify/telegram/inbox?after=garbage", a.token)).status).toBe(400);
  });

  it("another account never sees them; old events expire; at most 50 a page", async () => {
    const a = await linked();
    const b = await newAccount();
    const now = Math.floor(Date.now() / 1000);
    await env.DB.batch([
      env.DB.prepare("INSERT INTO telegram_events(account, type, data, text, at) VALUES(?, 'text', '', 'old', ?)").bind(a.id, now - EVENT_TTL - 5),
      ...Array.from({ length: INBOX_LIMIT + 3 }, () =>
        env.DB.prepare("INSERT INTO telegram_events(account, type, data, text, at) VALUES(?, 'text', '', 'm', ?)").bind(a.id, now)),
    ]);
    stubTelegram();
    expect((await tgCall("/notify/telegram/inbox?after=999999999", b.token)).data).toEqual({ events: [], cursor: 999999999 });
    const r = await tgCall("/notify/telegram/inbox", a.token);
    expect(r.data.events).toHaveLength(INBOX_LIMIT);
    expect(r.data.events.some((e: any) => e.text === "old")).toBe(false);
    expect((await tgCall(`/notify/telegram/inbox?after=${r.data.cursor}`, a.token)).data.events).toHaveLength(3);
  });
});

describe("POST /telegram/webhook", () => {
  it("403 without the right secret header, and nothing is handled", async () => {
    const calls = stubTelegram();
    expect((await hook(message(freshChat(), "/help"), null)).status).toBe(403);
    expect((await hook(message(freshChat(), "/help"), "wrong")).status).toBe(403);
    expect(calls).toHaveLength(0);
  });

  it("an unbound chat is told how to connect and nothing is stored; groups are ignored", async () => {
    const calls = stubTelegram();
    const chatId = freshChat();
    await hook(message(chatId, "hello"));
    await hook({ callback_query: { id: "cq9", data: "x", from: { id: chatId } } });
    expect(calls.map((c) => c.body.text)).toEqual([MSG_UNBOUND, MSG_UNBOUND]);
    await hook({ message: { chat: { id: -100, type: "group" }, text: "/help" } });
    expect(calls).toHaveLength(2);
  });

  it("a file from an unlinked chat keeps the connect reply and stores nothing", async () => {
    const calls = stubTelegram();
    const chatId = freshChat();
    await hook({ message: { message_id: 3, chat: { id: chatId, type: "private" }, document: { file_id: "d" }, caption: "hi" } });
    expect(calls.map((c) => c.body.text)).toEqual([MSG_UNBOUND]);
    const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM telegram_files WHERE file_id = 'd'").first<{ n: number }>();
    expect(n?.n).toBe(0);
  });
});

describe("files the person sends the bot", () => {
  const fileMsg = (chatId: number, extra: Record<string, unknown>) =>
    ({ message: { message_id: 2, chat: { id: chatId, type: "private" }, from: { id: chatId }, ...extra } });

  it("a resume or photo is queued for the app as a file event: file_id, name, type, size, caption", async () => {
    const a = await linked();
    const calls = stubTelegram();
    await hook(fileMsg(a.chatId, { document: { file_id: "DOC1", file_name: "Kofi Gilbert_Resume.pdf",
                                                mime_type: "application/pdf", file_size: 81234 }, caption: "my resume" }));
    await hook(fileMsg(a.chatId, { photo: [{ file_id: "SMALL", width: 90, height: 120, file_size: 900 },
                                           { file_id: "BIG", width: 1280, height: 1706, file_size: 190000 },
                                           { file_id: "MID", width: 320, height: 427, file_size: 9000 }] }));
    expect(calls.map((c) => [c.method, c.body.chat_id, c.body.text])).toEqual(
      [["sendMessage", a.chatId, MSG_FILE_QUEUED], ["sendMessage", a.chatId, MSG_FILE_QUEUED]]);
    const ev = (await tgCall("/notify/telegram/inbox", a.token)).data.events;
    expect(ev.map((e: any) => [e.type, e.data])).toEqual([["file", "DOC1"], ["file", "BIG"]]);
    expect(JSON.parse(ev[0].text)).toEqual({ name: "Kofi Gilbert_Resume.pdf", mime: "application/pdf", size: 81234, caption: "my resume" });
    expect(JSON.parse(ev[1].text)).toEqual({ name: "photo.jpg", mime: "image/jpeg", size: 190000, caption: "" });
  });

  it("voice, video, stickers, other file types and files over 20 MB get an honest reply and are not queued", async () => {
    const a = await linked();
    const calls = stubTelegram();
    for (const kind of ["voice", "video", "audio", "sticker"]) await hook(fileMsg(a.chatId, { [kind]: { file_id: "x" } }));
    await hook(fileMsg(a.chatId, { document: { file_id: "Z", file_name: "a.zip", mime_type: "application/zip" }, caption: "see this" }));
    await hook(fileMsg(a.chatId, { document: { file_id: "H", file_name: "cv.pdf", file_size: 21 * 1024 * 1024 } }));
    expect(calls.map((c) => c.body.text)).toEqual([MSG_NO_MEDIA, MSG_NO_MEDIA, MSG_NO_MEDIA, MSG_NO_MEDIA, MSG_FILE_TYPE, MSG_TOO_BIG]);
    for (const m of [MSG_FILE_QUEUED, MSG_NO_MEDIA, MSG_FILE_TYPE, MSG_TOO_BIG]) expect(m).not.toMatch(/[\u2013\u2014]/);
    const ev = (await tgCall("/notify/telegram/inbox", a.token)).data.events;
    expect(ev.map((e: any) => [e.type, e.text])).toEqual([["text", "see this"]]);   // only the caption
    const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM telegram_files WHERE account = ?").bind(a.id).first<{ n: number }>();
    expect(n?.n).toBe(0);
  });

  it("POST /notify/telegram/file streams the bytes through from Telegram, stores nothing", async () => {
    const a = await linked();
    stubTelegram();
    await hook(fileMsg(a.chatId, { document: { file_id: "DOC2", file_name: "cv.docx", file_size: 11 } }));
    const calls = stubTelegram({ getFile: { body: { ok: true, result: { file_id: "DOC2", file_path: "documents/file_7.docx", file_size: 11 } } },
                                 download: { body: "hello bytes" } });
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const res = await worker.fetch(new Request("https://broker.test/notify/telegram/file", {
      method: "POST", body: JSON.stringify({ file_id: "DOC2" }),
      headers: { authorization: `Bearer ${a.token}`, "content-type": "application/json" },
    }), { ...env, ...BOT } as Env);
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("application/octet-stream");
    expect(await res.text()).toBe("hello bytes");
    expect(calls.map((c) => c.method)).toEqual(["getFile", "download"]);
    expect(calls[0]!.body).toEqual({ file_id: "DOC2" });
    expect(calls[1]!.body).toBe("documents/file_7.docx");
    expect(log.mock.calls.flat().join(" ")).not.toMatch(/cv\.docx|hello/);   // only the size is logged
  });

  it("refuses a file_id from another account's chat, an unknown one, and a file over 20 MB", async () => {
    const a = await linked();
    const b = await linked();
    stubTelegram();
    await hook(fileMsg(a.chatId, { document: { file_id: "MINE", file_name: "cv.pdf" } }));
    const calls = stubTelegram({ getFile: { body: { ok: true, result: { file_path: "d/x.pdf", file_size: 25 * 1024 * 1024 } } } });
    expect((await tgCall("/notify/telegram/file", b.token, { body: { file_id: "MINE" } })).status).toBe(404);
    expect((await tgCall("/notify/telegram/file", a.token, { body: { file_id: "NOPE" } })).status).toBe(404);
    expect((await tgCall("/notify/telegram/file", a.token, { body: {} })).status).toBe(400);
    expect(calls).toHaveLength(0);                 // Telegram is never asked for someone else's file
    const big = await tgCall("/notify/telegram/file", a.token, { body: { file_id: "MINE" } });
    expect([big.status, big.data.error]).toEqual([413, "too_big"]);
    expect(calls.map((c) => c.method)).toEqual(["getFile"]);
    stubTelegram({ getFile: { status: 500, body: { ok: false, description: "boom" } } });
    vi.spyOn(console, "warn").mockImplementation(() => {});
    expect((await tgCall("/notify/telegram/file", a.token, { body: { file_id: "MINE" } })).status).toBe(502);
  });

  it("unlink forgets the files", async () => {
    const a = await linked();
    stubTelegram();
    await hook(fileMsg(a.chatId, { document: { file_id: "GONE", file_name: "cv.pdf" } }));
    await tgCall("/notify/telegram/unlink", a.token, { body: {} });
    const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM telegram_files WHERE account = ?").bind(a.id).first<{ n: number }>();
    expect(n?.n).toBe(0);
  });
  it("answers 200 even when handling fails, so Telegram does not retry", async () => {
    const a = await linked();
    vi.spyOn(console, "error").mockImplementation(() => {});
    stubTelegram();
    const broken = { ...env, ...BOT, DB: { prepare: () => { throw new Error("d1 down"); } } } as unknown as Env;
    const res = await worker.fetch(new Request("https://broker.test/telegram/webhook", {
      method: "POST", body: JSON.stringify(message(a.chatId, "hi")),
      headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET, "content-type": "application/json" },
    }), broken);
    expect(res.status).toBe(200);
  });
});
