// The student price check (owner decision 2026-11-20): free and self-built, no paid service.
//
// A signed-in person gives a school e-mail and their expected (or actual) graduation month; a
// 6-digit code goes to the school address through the existing mailer and they type it back. That
// is done ONCE, while still a student (the graduation month may not be in the past). The student
// price is then offered while the current month is no later than graduation month + 24 months, so
// a graduate keeps it for two years. Everyone else sees the Standard price.
//
// What is stored (migrations/0014): the school DOMAIN, a sha256 of the school address (one school
// e-mail verifies one account), the graduation month and the date. Never the address itself.
// Codes follow the sign-in code rules (src/signin.ts): 10 minutes, 5 attempts spent before the
// compare, at most 5 code e-mails an hour per school address, per account and per network.

import { identify, sha256Hex, tokenUrlsafe } from "./accounts";
import type { Env } from "./env";
import { countSend, mailerFrom } from "./mailer";
import { newCode, normaliseEmail } from "./signin";

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

/** The academic e-mail endings that count as a school address, in ONE place. A school address is
 * one whose domain IS one of these or ends with "." + one of these (so mail.example.edu counts).
 * Add a country here when someone asks; nothing else needs to change. */
export const ACADEMIC_SUFFIXES: readonly string[] = [
  "edu",                                                   // United States
  "ac.uk", "edu.au", "ac.nz", "ac.in", "edu.in",           // UK, Australia, New Zealand, India
  "edu.gh", "edu.ng", "ac.ke", "ac.za", "ac.ug", "ac.tz",  // Ghana, Nigeria, Kenya, South Africa, Uganda, Tanzania
  "ac.rw", "edu.et", "edu.eg", "ac.zm", "ac.zw", "ac.bw",  // Rwanda, Ethiopia, Egypt, Zambia, Zimbabwe, Botswana
  "edu.cm", "edu.sn",                                      // Cameroon, Senegal
  "edu.sg", "edu.my", "edu.ph", "edu.pk", "ac.bd", "edu.bd", "ac.lk", "edu.np",
  "edu.cn", "edu.hk", "edu.tw", "ac.jp", "ac.kr", "ac.th", "ac.id", "edu.vn",
  "ac.il", "edu.tr", "edu.sa", "ac.ae", "ac.ir",
  "edu.br", "edu.mx", "edu.ar", "edu.co", "edu.pe", "edu.ec", "edu.uy",
  "ac.at", "ac.cy",
];

export const STUDENT_WINDOW_MONTHS = 24;     // a graduate keeps the student price this long
export const MAX_FUTURE_MONTHS = 8 * 12;     // a graduation month further out than this is a typo
export const CODE_TTL_SECONDS = 10 * 60;
export const MAX_CODE_ATTEMPTS = 5;
export const SENDS_PER_HOUR = 5;

/** The academic domain of an address ("a@mail.example.ac.uk" -> "mail.example.ac.uk"), or null
 * when it is not a school address. */
export function schoolDomain(email: string): string | null {
  const at = email.lastIndexOf("@");
  if (at < 0) return null;
  const domain = email.slice(at + 1).toLowerCase();
  for (const s of ACADEMIC_SUFFIXES) {
    // The bare suffix ("edu") is not a school; there must be a name in front of it.
    if (domain.endsWith(`.${s}`) && domain.length > s.length + 1) return domain;
  }
  return null;
}

/** "YYYY-MM" -> a month index (year * 12 + month - 1), or null when it is not a real month. */
export function monthIndex(m: string): number | null {
  const x = /^(\d{4})-(\d{2})$/.exec(String(m ?? "").trim());
  if (!x) return null;
  const y = Number(x[1]), mo = Number(x[2]);
  if (y < 1990 || y > 2200 || mo < 1 || mo > 12) return null;
  return y * 12 + mo - 1;
}

const monthIndexAt = (nowSec: number) => {
  const d = new Date(nowSec * 1000);
  return d.getUTCFullYear() * 12 + d.getUTCMonth();
};

const monthLabel = (idx: number) => `${Math.floor(idx / 12)}-${String((idx % 12) + 1).padStart(2, "0")}`;

/** The last month the student price is offered for a graduation month ("2026-06" -> "2028-06"). */
export const eligibleThrough = (gradMonth: string): string | null => {
  const g = monthIndex(gradMonth);
  return g === null ? null : monthLabel(g + STUDENT_WINDOW_MONTHS);
};

/** Whether a stored verification still earns the student price at `now`: the current month is no
 * later than graduation month + 24 months. */
export function studentEligible(v: { grad_month: string } | null | undefined, now: number): boolean {
  if (!v) return false;
  const g = monthIndex(v.grad_month);
  return g !== null && monthIndexAt(now) <= g + STUDENT_WINDOW_MONTHS;
}

export interface StudentRow { school_domain: string; grad_month: string; verified_at: number }

export async function studentRow(db: D1Database, user: string): Promise<StudentRow | null> {
  return db.prepare("SELECT school_domain, grad_month, verified_at FROM student_verifications WHERE user = ?")
    .bind(user).first<StudentRow>();
}

/** What the app shows about the student check. */
export function studentStatus(v: StudentRow | null, now: number) {
  return {
    verified: Boolean(v),
    eligible: studentEligible(v, now),
    school_domain: v?.school_domain ?? null,
    grad_month: v?.grad_month ?? null,
    eligible_through: v ? eligibleThrough(v.grad_month) : null,
    verified_at: v?.verified_at ?? null,
  };
}

const codeHash = (salt: string, user: string, code: string) => sha256Hex(`${salt}:student:${user}:${code}`);

function sameHex(a: string, b: string): boolean {
  const x = new TextEncoder().encode(a);
  const y = new TextEncoder().encode(b);
  return x.byteLength === y.byteLength && crypto.subtle.timingSafeEqual(x, y);
}

/** Count one code e-mail for this school address, this account and this network this hour; false
 * when any of them is at the limit. Guarded upserts on the sign-in code's table, so parallel
 * requests cannot slip past. */
async function allowSend(db: D1Database, emailHash: string, user: string, ip: string, now: number): Promise<boolean> {
  const hour = Math.floor(now / 3600);
  const upsert = (key: string) => db.prepare(
    "INSERT INTO send_limits(key, hour, n) VALUES(?, ?, 1) " +
    "ON CONFLICT(key, hour) DO UPDATE SET n = n + 1 WHERE n < ? RETURNING n").bind(key, hour, SENDS_PER_HOUR);
  const [, byEmail, byUser, byIp] = await db.batch<{ n: number }>([
    db.prepare("DELETE FROM send_limits WHERE hour < ?").bind(hour),
    upsert(`se:${emailHash}`),
    upsert(`su:${await sha256Hex(user)}`),
    upsert(`si:${await sha256Hex(ip)}`),
  ]);
  return byEmail!.results.length > 0 && byUser!.results.length > 0 && byIp!.results.length > 0;
}

/** The signed-in caller, or the Response to answer instead. The check belongs to an account with
 * an e-mail, like buying, so it follows the person to any device. */
async function caller(req: Request, env: Env): Promise<string | Response> {
  const user = await identify(req, env);
  if (!user) return json({ error: "no user" }, 401);
  const e = await env.DB.prepare("SELECT email FROM accounts WHERE account_id = ?").bind(user)
    .first<{ email: string | null }>();
  if (!e?.email) return json({ error: "sign in first", reason: "sign_in_required" }, 401);
  return user;
}

/** GET /account/student: where the caller stands. */
export async function studentGet(req: Request, env: Env, now: number): Promise<Response> {
  const user = await identify(req, env);
  if (!user) return json({ error: "no user" }, 401);
  return json(studentStatus(await studentRow(env.DB, user), now));
}

/** POST /account/student/start {email, grad_month}: send a code to the school address. */
export async function studentStart(req: Request, env: Env, now: number): Promise<Response> {
  const mailer = mailerFrom(env);
  if (!mailer) return json({ error: "email is not configured" }, 503);
  const user = await caller(req, env);
  if (user instanceof Response) return user;
  const b = await body(req);
  const email = normaliseEmail(b.email);
  const domain = email ? schoolDomain(email) : null;
  if (!email || !domain) {
    return json({ error: "use your school email (for example one ending in .edu or .ac.uk)", reason: "not_school" }, 400);
  }
  const grad = String(b.grad_month ?? "").trim();
  const g = monthIndex(grad);
  const nowIdx = monthIndexAt(now);
  if (g === null) return json({ error: "pick your graduation month", reason: "bad_month" }, 400);
  // Verified while still a student: a graduation month in the past cannot start the check.
  if (g < nowIdx) return json({ error: "the student price is checked while you are still a student", reason: "graduated" }, 400);
  if (g > nowIdx + MAX_FUTURE_MONTHS) return json({ error: "pick your graduation month", reason: "bad_month" }, 400);
  if (await studentRow(env.DB, user)) return json({ error: "you are already verified", reason: "already_verified" }, 409);
  const emailHash = await sha256Hex(email);
  const owner = await env.DB.prepare("SELECT user FROM student_verifications WHERE email_hash = ?").bind(emailHash)
    .first<{ user: string }>();
  if (owner && owner.user !== user) {
    return json({ error: "this school email is already verified on another account", reason: "email_used" }, 409);
  }
  const ip = req.headers.get("CF-Connecting-IP") || "unknown";
  if (!(await allowSend(env.DB, emailHash, user, ip, now))) {
    return json({ error: "too many codes requested; wait a while and try again", reason: "rate_limited" }, 429);
  }
  const code = newCode();
  const salt = tokenUrlsafe(16);
  await env.DB.prepare(
    "INSERT INTO student_codes(user, email_hash, school_domain, grad_month, code_hash, salt, expires, attempts) " +
    "VALUES(?, ?, ?, ?, ?, ?, ?, 0) ON CONFLICT(user) DO UPDATE SET email_hash = excluded.email_hash, " +
    "school_domain = excluded.school_domain, grad_month = excluded.grad_month, code_hash = excluded.code_hash, " +
    "salt = excluded.salt, expires = excluded.expires, attempts = 0")
    .bind(user, emailHash, domain, monthLabel(g), await codeHash(salt, user, code), salt, now + CODE_TTL_SECONDS).run();
  try {
    await mailer.send({
      to: email,
      subject: `Your SponsorJobs student code is ${code}`,
      text: `Your SponsorJobs student code is ${code}. It expires in 10 minutes.\n\n` +
            "Type it into the app to get the student price. If you did not ask for it, you can ignore this email.\n",
    });
  } catch (err) {
    console.error(`student code e-mail failed (${mailer.name}):`, err instanceof Error ? err.message : err);
    return json({ error: "could not send the code; try again in a moment" }, 502);
  }
  await countSend(env.DB, now);
  return json({ sent: true, school_domain: domain });
}

/** POST /account/student/verify {code}: confirm the code; the verification is stored once. */
export async function studentVerify(req: Request, env: Env, now: number): Promise<Response> {
  const user = await caller(req, env);
  if (user instanceof Response) return user;
  const code = String((await body(req)).code ?? "").replace(/\s+/g, "");
  if (!/^\d{6}$/.test(code)) return json({ error: "that code is not right", reason: "bad_code" }, 400);
  const db = env.DB;
  // Spend one attempt FIRST (atomically, only while the code is live and has attempts left).
  const row = await db.prepare(
    "UPDATE student_codes SET attempts = attempts + 1 WHERE user = ? AND attempts < ? AND expires > ? " +
    "RETURNING email_hash, school_domain, grad_month, code_hash, salt, attempts")
    .bind(user, MAX_CODE_ATTEMPTS, now)
    .first<{ email_hash: string; school_domain: string; grad_month: string; code_hash: string; salt: string; attempts: number }>();
  if (!row) {
    await db.prepare("DELETE FROM student_codes WHERE user = ? AND (expires <= ? OR attempts >= ?)")
      .bind(user, now, MAX_CODE_ATTEMPTS).run();
    return json({ error: "this code has expired; ask for a new one", reason: "expired" }, 400);
  }
  if (!sameHex(row.code_hash, await codeHash(row.salt, user, code))) {
    if (row.attempts >= MAX_CODE_ATTEMPTS) {
      await db.prepare("DELETE FROM student_codes WHERE user = ? AND code_hash = ?").bind(user, row.code_hash).run();
    }
    return json({ error: "that code is not right", reason: "bad_code" }, 400);
  }
  const used = await db.prepare("DELETE FROM student_codes WHERE user = ? AND code_hash = ? RETURNING user")
    .bind(user, row.code_hash).first();
  if (!used) return json({ error: "this code has expired; ask for a new one", reason: "expired" }, 400);
  try {
    await db.prepare(
      "INSERT INTO student_verifications(user, school_domain, email_hash, grad_month, verified_at) VALUES(?, ?, ?, ?, ?) " +
      "ON CONFLICT(user) DO NOTHING")
      .bind(user, row.school_domain, row.email_hash, row.grad_month, now).run();
  } catch {
    // The UNIQUE email_hash: another account verified this school address in the meantime.
    return json({ error: "this school email is already verified on another account", reason: "email_used" }, 409);
  }
  return json(studentStatus(await studentRow(db, user), now));
}
