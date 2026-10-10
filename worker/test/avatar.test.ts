// Live interviews on the company Tavus key: start, metering, end + transcript. Tavus is stubbed;
// no key and no network are involved.
import { afterEach, describe, expect, it, vi } from "vitest";
import { INTERVIEW_SECONDS } from "../src/metering";
import { parseTranscriptEvents } from "../src/tavus";
import { call, env, givePass, newAccount } from "./helpers";

afterEach(() => vi.restoreAllMocks());

const TAVUS = { TAVUS_API_KEY: "tvs-test-not-real" };

interface Sent { url: string; method: string; headers: Record<string, string>; body: any }
function stubTavus(handler: (s: Sent) => { status?: number; body: unknown }) {
  const sent: Sent[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const req = new Request(input as RequestInfo, init as RequestInit);
    const text = await req.text();
    const s = { url: req.url, method: req.method, headers: Object.fromEntries(req.headers),
                body: text ? JSON.parse(text) : null };
    sent.push(s);
    const r = handler(s);
    return new Response(JSON.stringify(r.body), { status: r.status ?? 200 });
  });
  return sent;
}
const room = () => ({ body: { conversation_id: "c_123", conversation_url: "https://tavus.daily.co/c_123",
                              meeting_token: "mt_abc" } });

describe("live interviews", () => {
  it("answer 'not configured' until the company key is set", async () => {
    const { token } = await newAccount();
    const r = await call("/avatar/session/start", { token, body: {} });
    expect(r.status).toBe(503);
    expect(r.data.error).toBe("live interviews are not configured");
  });

  it("a free account gets 402 pass_required and Tavus is never called", async () => {
    const sent = stubTavus(room);
    const { token } = await newAccount();
    const r = await call("/avatar/session/start", { token, body: {}, env: TAVUS });
    expect(r.status).toBe(402);
    expect(r.data.reason).toBe("pass_required");
    expect(sent).toHaveLength(0);
  });

  it("a pass holder gets a private room capped at 20 minutes, with the briefing and the key server-side", async () => {
    const sent = stubTavus(room);
    const { id, token } = await newAccount();
    await givePass(id);
    const r = await call("/avatar/session/start",
      { token, body: { context: { prompt: "Interview for Data Analyst", greeting: "Hi there" } }, env: TAVUS });
    expect(r.status).toBe(200);
    expect(r.data.session_url).toBe("https://tavus.daily.co/c_123?t=mt_abc");
    expect(r.data.provider_session_id).toBe("c_123");
    expect(r.data.remaining).toBe(3 * INTERVIEW_SECONDS);
    expect(r.text).not.toContain("tvs-test-not-real");               // the key never reaches the app
    expect(sent[0]!.url).toBe("https://tavusapi.com/v2/conversations");
    expect(sent[0]!.headers["x-api-key"]).toBe("tvs-test-not-real");
    expect(sent[0]!.body.require_auth).toBe(true);
    expect(sent[0]!.body.properties.max_call_duration).toBe(1260);          // safety net one minute past 20:00
    expect(sent[0]!.body.conversational_context).toBe("Interview for Data Analyst");
    expect(sent[0]!.body.custom_greeting).toBe("Hi there");
    expect(sent[0]!.body.face_id).toBeTruthy();
  });

  it("heartbeats draw the pass down and say stop at zero, never below", async () => {
    stubTavus(room);
    const { id, token } = await newAccount();
    await givePass(id);
    const beat = (seconds: number) => call("/avatar/heartbeat", { token, body: { seconds }, env: TAVUS });
    let r = await beat(60);
    expect(r.data).toEqual({ remaining: 3 * INTERVIEW_SECONDS - 60, stop: false, charged: 60 });
    await env.DB.prepare("UPDATE users SET pass_seconds = 30 WHERE user = ?").bind(id).run();
    r = await beat(45);
    expect(r.data).toEqual({ remaining: 0, stop: true, charged: 45 });
    const row = await env.DB.prepare("SELECT pass_seconds, credit_seconds FROM users WHERE user = ?").bind(id).first();
    expect(row).toEqual({ pass_seconds: 0, credit_seconds: 0 });
    expect((await beat(-5)).data.remaining).toBe(0);
    expect((await call("/avatar/heartbeat", { token, body: { seconds: "x" }, env: TAVUS })).status).toBe(400);
  });

  it("the pass is spent before purchased credits", async () => {
    const { id, token } = await newAccount();
    await givePass(id);
    await env.DB.prepare("UPDATE users SET pass_seconds = 100, credit_seconds = 900 WHERE user = ?").bind(id).run();
    const r = await call("/avatar/heartbeat", { token, body: { seconds: 150 }, env: TAVUS });
    expect(r.data.remaining).toBe(850);
    const row = await env.DB.prepare("SELECT pass_seconds, credit_seconds FROM users WHERE user = ?").bind(id).first();
    expect(row).toEqual({ pass_seconds: 0, credit_seconds: 850 });
  });

  it("end hands back the transcript, and only to the account that started the interview", async () => {
    const sent = stubTavus((s) => {
      if (s.method === "POST" && s.url.endsWith("/conversations")) return room();
      if (s.url.endsWith("/end")) return { body: {} };
      return { body: { events: [{ event_type: "application.transcription_ready", properties: { transcript: [
        { role: "system", content: "hidden" },
        { role: "assistant", content: "Tell me about yourself." },
        { role: "user", content: "I build dashboards." },
      ] } }] } };
    });
    const a = await newAccount();
    await givePass(a.id);
    await call("/avatar/session/start", { token: a.token, body: {}, env: TAVUS });
    const b = await newAccount();
    expect((await call("/avatar/session/end", { token: b.token, body: { conversation_id: "c_123" }, env: TAVUS })).status).toBe(404);
    const r = await call("/avatar/session/end", { token: a.token, body: { conversation_id: "c_123" }, env: TAVUS });
    expect(r.status).toBe(200);
    expect(r.data.transcript.map((t: any) => [t.role, t.content])).toEqual([
      ["interviewer", "Tell me about yourself."], ["candidate", "I build dashboards."]]);
    expect(sent.some(s => s.url.endsWith("/conversations/c_123/end"))).toBe(true);
  });

  it("one interview never charges more than 20 minutes, however long the wrap-up runs", async () => {
    stubTavus(room);
    const { id, token } = await newAccount();
    await givePass(id);
    await call("/avatar/session/start", { token, body: {}, env: TAVUS });
    const beat = (seconds: number) =>
      call("/avatar/heartbeat", { token, body: { seconds, conversation_id: "c_123" }, env: TAVUS });
    const beats = INTERVIEW_SECONDS / 30;
    for (let i = 0; i < beats - 1; i++) await beat(30);              // up to 30 s before the end
    let r = await beat(30);                                          // the full interview
    expect(r.data).toMatchObject({ remaining: 2 * INTERVIEW_SECONDS, charged: 30, stop: false });
    r = await beat(30);                                              // 30 s past it: the wrap-up is free
    expect(r.data).toMatchObject({ remaining: 2 * INTERVIEW_SECONDS, charged: 0, stop: false });
    const other = await newAccount();
    expect((await call("/avatar/heartbeat",
      { token: other.token, body: { seconds: 30, conversation_id: "c_123" }, env: TAVUS })).status).toBe(404);
  });

  it("a Tavus failure is a clean 502 that says nothing about the key", async () => {
    stubTavus(() => ({ status: 401, body: { message: "invalid key tvs-test-not-real" } }));
    const { id, token } = await newAccount();
    await givePass(id);
    const r = await call("/avatar/session/start", { token, body: {}, env: TAVUS });
    expect(r.status).toBe(502);
    expect(r.text).not.toContain("tvs-test");
  });

  it("transcript parsing ignores system turns and empty lines", () => {
    expect(parseTranscriptEvents([{ event_type: "other" }, { event_type: "application.transcription_ready",
      properties: { transcript: [{ role: "user", content: "  " }, { role: "assistant", content: "Hi" }] } }]))
      .toEqual([{ role: "interviewer", content: "Hi", seconds_from_start: undefined, duration: undefined }]);
  });
});
