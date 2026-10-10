// Sending the sign-in code e-mail, behind one small interface so the provider can change without
// touching the sign-in logic.
//
// Chosen (2026-10): Resend's HTTPS API (secret RESEND_API_KEY). Its free plan sends 3,000 e-mails
// a month (at most 100 a day) to any recipient for $0, and verifying sponsorjobs.ai there is a
// one-click "Sign in to Cloudflare". Cloudflare's own Email Service can also send to any recipient
// from a Worker (the `send_email` binding), but outbound sending is in beta and needs the Workers
// Paid plan ($5/month); this broker runs on the free plan. The binding is supported here too
// (EMAIL), so switching later is a wrangler.jsonc change, not a code change.

import type { Env } from "./env";

export interface Mail { to: string; subject: string; text: string }

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
      body: JSON.stringify({ from: this.from, to: [m.to], subject: m.subject, text: m.text }),
    });
    if (!res.ok) throw new Error(`resend ${res.status}: ${(await res.text()).slice(0, 300)}`);
  }
}

class CloudflareMailer implements Mailer {
  readonly name = "cloudflare";
  constructor(private binding: SendEmailBinding, private from: string) {}
  async send(m: Mail): Promise<void> {
    await this.binding.send({ to: m.to, from: this.from, subject: m.subject, text: m.text });
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
