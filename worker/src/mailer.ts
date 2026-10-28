// Sending e-mail (the sign-in code, and mail forwarded from a student's alias), behind one small
// interface so the provider can change without touching the logic that sends.
//
// Chosen (2026-10): Resend's HTTPS API (secret RESEND_API_KEY). Its free plan sends 3,000 e-mails
// a month (at most 100 a day) to any recipient for $0, and verifying sponsorjobs.ai there is a
// one-click "Sign in to Cloudflare". Cloudflare's own Email Service can also send to any recipient
// from a Worker (the `send_email` binding), but outbound sending is in beta and needs the Workers
// Paid plan ($5/month); this broker runs on the free plan. The binding is supported here too
// (EMAIL), so switching later is a wrangler.jsonc change, not a code change.

import type { Env } from "./env";

export interface Mail {
  to: string;
  subject: string;
  text: string;
  html?: string;       // an HTML alternative (forwarded mail keeps the original's)
  reply_to?: string;   // where a reply goes (forwarded mail: the original sender); Resend only
  // The From line, when not the configured one. Forwarded mail uses a display name on our own
  // address: "<sender> via SponsorJobs" <no-reply@sponsorjobs.ai>. Never another address.
  from?: string;
}

export interface Mailer {
  readonly name: string;
  /** Deliver one e-mail; throws on any failure (the route turns that into a clean 502). */
  send(mail: Mail): Promise<void>;
}

export const RESEND_API = "https://api.resend.com/emails";
export const DEFAULT_FROM = "SponsorJobs <no-reply@sponsorjobs.ai>";

class ResendMailer implements Mailer {
  readonly name = "resend";
  constructor(private key: string, private from: string) {}
  async send(m: Mail): Promise<void> {
    const res = await fetch(RESEND_API, {
      method: "POST",
      headers: { Authorization: `Bearer ${this.key}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        from: m.from ?? this.from, to: [m.to], subject: m.subject, text: m.text,
        ...(m.html ? { html: m.html } : {}), ...(m.reply_to ? { reply_to: m.reply_to } : {}),
      }),
    });
    if (!res.ok) throw new Error(`resend ${res.status}: ${(await res.text()).slice(0, 300)}`);
  }
}

class CloudflareMailer implements Mailer {
  readonly name = "cloudflare";
  constructor(private binding: SendEmailBinding, private from: string) {}
  async send(m: Mail): Promise<void> {
    // The binding's simple form has no reply-to; a forwarded message's reply goes to us then.
    await this.binding.send({ to: m.to, from: m.from ?? this.from, subject: m.subject, text: m.text,
                              ...(m.html ? { html: m.html } : {}) });
  }
}

/** Cloudflare Email Service's Workers binding (only the call used here). */
export interface SendEmailBinding {
  send(msg: { to: string; from: string; subject: string; text: string; html?: string }): Promise<unknown>;
}

/** The configured sender, or null (then e-mail sign-in answers "not configured"). Resend wins when
 * both are present. */
export function mailerFrom(env: Env): Mailer | null {
  const from = (env.EMAIL_FROM || DEFAULT_FROM).trim();
  if (env.RESEND_API_KEY) return new ResendMailer(env.RESEND_API_KEY, from);
  if (env.EMAIL && typeof env.EMAIL.send === "function") return new CloudflareMailer(env.EMAIL, from);
  return null;
}

/** The bare address inside the configured From ("SponsorJobs <no-reply@sponsorjobs.ai>" ->
 * "no-reply@sponsorjobs.ai"), for a From line with another display name. */
export function senderAddress(env: Env): string {
  const from = (env.EMAIL_FROM || DEFAULT_FROM).trim();
  const m = /<([^>]+)>/.exec(from);
  return m ? m[1]!.trim() : from;
}

/** One more e-mail sent today, for the admin dashboard's Resend quota line (the free plan allows
 * 100 a day). A counting hiccup must never fail a send that has already gone out, so this only
 * logs. */
export async function countSend(db: D1Database, now: number): Promise<void> {
  try {
    await db.prepare("INSERT INTO email_sends(day, n) VALUES(?, 1) ON CONFLICT(day) DO UPDATE SET n = n + 1")
      .bind(new Date(now * 1000).toISOString().slice(0, 10)).run();
  } catch (err) {
    console.error("email_sends count failed:", err instanceof Error ? err.message : err);
  }
}
