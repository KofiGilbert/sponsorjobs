// The student price check: a 6-digit code to a school e-mail, confirmed once while still a student;
// the student price then holds until graduation month + 24 months. Resend is stubbed (no network)
// and the code is read back out of the stubbed e-mail, as the person would.
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Env } from "../src/env";
import { RESEND_API } from "../src/mailer";
import {
  ACADEMIC_SUFFIXES, eligibleThrough, MAX_CODE_ATTEMPTS, monthIndex, schoolDomain, SENDS_PER_HOUR, studentEligible,
} from "../src/student";
import { call, env, freshIp, newAccount, signedInAccount } from "./helpers";

afterEach(() => vi.restoreAllMocks());

const MAIL: Partial<Env> = { RESEND_API_KEY: "re_test_not_real" };

interface Mail { to: string[]; subject: string; text: string }

function stubResend(status = 200) {
  const mails: Mail[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const req = new Request(input as RequestInfo, init as RequestInit);
    if (req.url !== RESEND_API) throw new Error(`unexpected fetch ${req.url}`);
    mails.push(await req.json());
    return new Response(JSON.stringify({ id: "email_1" }), { status });
  });
  return mails;
}

const codeIn = (m: Mail) => /\b(\d{6})\b/.exec(m.text)![1]!;

/** "YYYY-MM" `delta` months from this month (UTC). */
function monthFromNow(delta: number, nowMs = Date.now()): string {
  const d = new Date(nowMs);
  const idx = d.getUTCFullYear() * 12 + d.getUTCMonth() + delta;
  return `${Math.floor(idx / 12)}-${String((idx % 12) + 1).padStart(2, "0")}`;
}

let seq = 0;
const school = (domain = "example.edu") => `student${++seq}.${Date.now()}@${domain}`;

const start = (token: string, email: string, grad_month = monthFromNow(12), ip = freshIp()) =>
  call("/account/student/start", { token, body: { email, grad_month }, headers: { "CF-Connecting-IP": ip }, env: MAIL });
const verify = (token: string, code: string) => call("/account/student/verify", { token, body: { code }, env: MAIL });
const status = async (token: string) => (await call("/account/student", { token })).data;

describe("school e-mail domains", () => {
  it("accepts .edu and the academic endings, subdomains included; refuses the rest", () => {
    expect(schoolDomain("a@stanford.edu")).toBe("stanford.edu");
    expect(schoolDomain("a@mail.example.ac.uk")).toBe("mail.example.ac.uk");
    for (const d of ["uni.edu.au", "iit.ac.in", "ug.edu.gh", "unilag.edu.ng"]) expect(schoolDomain(`a@${d}`)).toBe(d);
    for (const e of ["a@gmail.com", "a@edu", "a@ac.uk", "a@notedu.com", "a@example.education", "a@example.edu.com"]) {
      expect(schoolDomain(e), e).toBeNull();
    }
    // The required endings are on the one list.
    for (const s of ["edu", "ac.uk", "edu.au", "ac.in", "edu.gh", "edu.ng"]) expect(ACADEMIC_SUFFIXES).toContain(s);
  });
});

describe("the 24-month window", () => {
  const at = (iso: string) => Math.floor(Date.parse(iso) / 1000);
  it("holds through graduation month + 24 months and ends the month after", () => {
    const v = { grad_month: "2026-06" };
    expect(studentEligible(v, at("2025-01-15T00:00:00Z"))).toBe(true);     // still studying
    expect(studentEligible(v, at("2026-06-30T00:00:00Z"))).toBe(true);     // graduation month
    expect(studentEligible(v, at("2028-06-30T23:59:59Z"))).toBe(true);     // the 24th month after
    expect(studentEligible(v, at("2028-07-01T00:00:00Z"))).toBe(false);    // the 25th: Standard
    expect(studentEligible(null, at("2026-01-01T00:00:00Z"))).toBe(false);
    expect(eligibleThrough("2026-06")).toBe("2028-06");
    expect(eligibleThrough("2026-11")).toBe("2028-11");
    expect(monthIndex("2026-13")).toBeNull();
    expect(monthIndex("June 2026")).toBeNull();
  });
});

describe("verifying a school e-mail", () => {
  it("needs a signed-in account", async () => {
    stubResend();
    const anon = await newAccount();
    const r = await start(anon.token, school());
    expect(r.status).toBe(401);
    expect(r.data.reason).toBe("sign_in_required");
    expect((await call("/account/student/start", { body: { email: school() }, env: MAIL })).status).toBe(401);
  });

  it("is 503 without a mailer", async () => {
    const a = await signedInAccount();
    const r = await call("/account/student/start", { token: a.token, body: { email: school(), grad_month: monthFromNow(6) } });
    expect(r.status).toBe(503);
  });

  it("refuses a non-school address, a bad month and a graduation month in the past", async () => {
    const mails = stubResend();
    const a = await signedInAccount();
    expect((await start(a.token, "me@gmail.com")).data.reason).toBe("not_school");
    expect((await start(a.token, school(), "2026-13")).data.reason).toBe("bad_month");
    expect((await start(a.token, school(), monthFromNow(-1))).data.reason).toBe("graduated");
    expect((await start(a.token, school(), monthFromNow(200))).data.reason).toBe("bad_month");
    expect(mails).toHaveLength(0);
  });

  it("a good code verifies once and stores the domain and graduation month, never the address", async () => {
    const mails = stubResend();
    const a = await signedInAccount();
    const email = school("mail.example.ac.uk");
    const grad = monthFromNow(3);
    const r = await start(a.token, email, grad);
    expect(r.status).toBe(200);
    expect(r.data).toEqual({ sent: true, school_domain: "mail.example.ac.uk" });
    expect(mails[0]!.to).toEqual([email]);
    expect((await status(a.token)).verified).toBe(false);
    const ok = await verify(a.token, codeIn(mails[0]!));
    expect(ok.status).toBe(200);
    expect(ok.data).toMatchObject({ verified: true, eligible: true, school_domain: "mail.example.ac.uk", grad_month: grad });
    expect(await status(a.token)).toMatchObject({ verified: true, eligible: true });
    const row = await env.DB.prepare("SELECT * FROM student_verifications WHERE user = ?").bind(a.id).first<Record<string, unknown>>();
    expect(JSON.stringify(row)).not.toContain(email);
    // Once only: a second check is refused (no moving the graduation month later).
    expect((await start(a.token, school(), monthFromNow(40))).data.reason).toBe("already_verified");
    // And the code is spent.
    expect((await verify(a.token, codeIn(mails[0]!))).data.reason).toBe("expired");
  });

  it("a wrong code is refused and five wrong tries burn the code", async () => {
    const mails = stubResend();
    const a = await signedInAccount();
    await start(a.token, school());
    const good = codeIn(mails[0]!);
    const wrong = good === "000000" ? "111111" : "000000";
    for (let i = 0; i < MAX_CODE_ATTEMPTS; i++) expect((await verify(a.token, wrong)).data.reason).toBe("bad_code");
    expect((await verify(a.token, good)).data.reason).toBe("expired");
    expect((await verify(a.token, "12ab")).data.reason).toBe("bad_code");
    expect((await status(a.token)).verified).toBe(false);
  });

  it("an expired code is refused", async () => {
    const mails = stubResend();
    const a = await signedInAccount();
    await start(a.token, school());
    await env.DB.prepare("UPDATE student_codes SET expires = 1 WHERE user = ?").bind(a.id).run();
    const r = await verify(a.token, codeIn(mails[0]!));
    expect(r.status).toBe(400);
    expect(r.data.reason).toBe("expired");
  });

  it("code e-mails are rate limited per school address and per account", async () => {
    const mails = stubResend();
    const a = await signedInAccount();
    const email = school();
    for (let i = 0; i < SENDS_PER_HOUR; i++) expect((await start(a.token, email)).status).toBe(200);
    const r = await start(a.token, email);
    expect(r.status).toBe(429);
    expect(r.data.reason).toBe("rate_limited");
    // A different school address from the same account is still the same account's limit.
    expect((await start(a.token, school())).status).toBe(429);
    expect(mails).toHaveLength(SENDS_PER_HOUR);
  });

  it("one school e-mail verifies only one account", async () => {
    const mails = stubResend();
    const a = await signedInAccount();
    const b = await signedInAccount();
    const email = school();
    await start(a.token, email);
    expect((await verify(a.token, codeIn(mails[0]!))).status).toBe(200);
    const r = await start(b.token, email);
    expect(r.status).toBe(409);
    expect(r.data.reason).toBe("email_used");
  });
});
