// Tavus Conversational Video Interface, the company key, server-side only. Ported from
// backend/tavus_client.py + backend/provider_tavus.py. The key never leaves this Worker: the app
// gets back a private room URL with a short-lived meeting token, and the person's browser streams
// the interview to Tavus directly, so no video passes through us.

export const TAVUS_API = "https://tavusapi.com/v2";
export const DEFAULT_FACE_ID = "r9d30b0e55ac";

export class TavusError extends Error {
  constructor(readonly status: number, message: string) {
    super(`tavus ${status}: ${message}`);
  }
}

async function call(key: string, method: string, path: string, body?: unknown): Promise<any> {
  const res = await fetch(`${TAVUS_API}${path}`, {
    method,
    headers: { "x-api-key": key, "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let out: any = {};
  try { out = text ? JSON.parse(text) : {}; } catch { out = { raw: text.slice(0, 200) }; }
  if (res.status >= 400) {
    throw new TavusError(res.status, String(out?.message ?? out?.error ?? out?.raw ?? "").slice(0, 200));
  }
  return out ?? {};
}

export interface NewConversation { conversationId: string; url: string; token: string }

export async function createConversation(key: string, o: {
  faceId?: string; palId?: string; context?: string; greeting?: string; name: string;
  maxCallSeconds: number; joinTimeout?: number; leftTimeout?: number;
}): Promise<NewConversation> {
  const body: Record<string, unknown> = {
    conversation_name: o.name,
    require_auth: true,                                   // private room, short-lived meeting token
    properties: {
      max_call_duration: o.maxCallSeconds,                // Tavus itself ends the call here
      participant_absent_timeout: o.joinTimeout ?? 300,   // time to grant camera and mic and join
      participant_left_timeout: o.leftTimeout ?? 90,      // linger if they briefly drop
    },
  };
  if (o.palId) body.pal_id = o.palId; else body.face_id = o.faceId || DEFAULT_FACE_ID;
  if (o.context) body.conversational_context = o.context;
  if (o.greeting && !o.palId) body.custom_greeting = o.greeting;
  const out = await call(key, "POST", "/conversations", body);
  let url = String(out.conversation_url ?? "");
  const token = String(out.meeting_token ?? "");
  if (token && url && !url.includes("t=")) url += `${url.includes("?") ? "&" : "?"}t=${token}`;
  return { conversationId: String(out.conversation_id ?? ""), url, token };
}

export async function endConversation(key: string, id: string): Promise<void> {
  await call(key, "POST", `/conversations/${encodeURIComponent(id)}/end`);
}

export interface Turn { role: "interviewer" | "candidate"; content: string;
                        seconds_from_start?: number; duration?: number }

/** The dialogue from a verbose conversation's application.transcription_ready event. */
export function parseTranscriptEvents(events: unknown): Turn[] {
  const turns: Turn[] = [];
  for (const ev of Array.isArray(events) ? events : []) {
    if (!ev || typeof ev !== "object" || (ev as any).event_type !== "application.transcription_ready") continue;
    for (const t of ((ev as any).properties?.transcript ?? []) as any[]) {
      if (!t || typeof t !== "object") continue;
      const role = String(t.role ?? "").toLowerCase();
      if (role === "system") continue;
      const content = String(t.content ?? "").trim();
      if (!content) continue;
      turns.push({ role: role === "assistant" ? "interviewer" : "candidate", content,
                   seconds_from_start: t.seconds_from_start, duration: t.duration });
    }
  }
  return turns;
}

export async function transcript(key: string, id: string): Promise<Turn[]> {
  const data = await call(key, "GET", `/conversations/${encodeURIComponent(id)}?verbose=true`);
  return parseTranscriptEvents(data?.events);
}
