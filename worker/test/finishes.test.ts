// The apply agent's monthly allowance (2026-11-26): "applications the agent finishes" a calendar
// month, the same number as the plan's tailored resumes. Free 3, Student 40, Standard 60. Counted
// once per application by POST /apply/finish (the app calls it only when the agent steps in after
// the fast Autofill), refused with 402 finish_limit when used up, back with the month. Packs hold
// live interviews only and add none.
import { describe, expect, it } from "vitest";
import { EMPTY_STATE, finishesLeft, monthResets, PACKS, PLANS, status } from "../src/metering";
import { call, env, givePass, newAccount } from "./helpers";

const T0 = 1_780_000_000;              // 2026-05-28 UTC
const NO_ENV = {};

describe("the plans' agent finishes", () => {
  it("free 3, student 40, standard 60 a month, the same as their tailored resumes a month", () => {
    expect(PLANS.free!.agentFinishes).toBe(3);
    expect(PLANS.student_monthly!.agentFinishes).toBe(40);
    expect(PLANS.standard_monthly!.agentFinishes).toBe(60);
    expect(PLANS.student_monthly!.agentFinishes).toBe(PLANS.student_monthly!.packages);
    expect(PLANS.standard_monthly!.agentFinishes).toBe(PLANS.standard_monthly!.packages);
    // the every-3-months plans count by the month too, at their tier's monthly number
    expect(PLANS.student_quarterly!.agentFinishes).toBe(40);
    expect(PLANS.standard_quarterly!.agentFinishes).toBe(60);
    for (const p of Object.values(PLANS)) expect(p.agentFinishes).toBeGreaterThan(0);
  });

  it("packs add no agent finishes: they hold live interviews only", () => {
    expect(Object.keys(PACKS).length).toBeGreaterThan(0);
    const st = status({ ...EMPTY_STATE, creditSeconds: 9 * 1200 }, T0, NO_ENV);
    expect(st.finishes_left).toBe(3);
  });

  it("left, limit and the reset day on the summary", () => {
    expect(finishesLeft({ ...EMPTY_STATE, finishesUsed: 2 }, T0)).toBe(1);
    expect(finishesLeft({ ...EMPTY_STATE, finishesUsed: 9 }, T0)).toBe(0);
    const st = status({ ...EMPTY_STATE, finishesUsed: 1 }, T0, NO_ENV);
    expect([st.finishes_left, st.finishes_limit, st.finishes_reset]).toEqual([2, 3, "2026-06-01"]);
    expect(monthResets(Date.UTC(2026, 11, 31, 23, 59))).toBe("2027-01-01");
    expect(monthResets(Date.UTC(2026, 10, 1, 0, 0))).toBe("2026-12-01");
  });
});

describe("POST /apply/finish", () => {
  it("needs an account", async () => {
    expect((await call("/apply/finish", { method: "POST" })).status).toBe(401);
  });

  it("free: 3 a month, then 402 finish_limit; it resets with the month", async () => {
    const { id, token } = await newAccount();
    const reset = monthResets(Date.now());
    for (const left of [2, 1, 0]) {
      const r = await call("/apply/finish", { token, method: "POST" });
      expect(r.status).toBe(200);
      expect(r.data).toEqual({ finishes_left: left, finishes_limit: 3, finishes_reset: reset, tier: "free" });
    }
    expect((await call("/me/usage", { token })).data.finishes_left).toBe(0);
    const over = await call("/apply/finish", { token, method: "POST" });
    expect(over.status).toBe(402);
    expect(over.data).toEqual({ error: "this month's agent finishes are used up", reason: "finish_limit", tier: "free",
                                finishes_left: 0, finishes_limit: 3, finishes_reset: reset });
    // the tailored resumes are a separate count
    expect((await call("/me/usage", { token })).data.packages_left).toBe(3);
    // a new calendar month is a new row: back to 3
    await env.DB.prepare("UPDATE usage SET period = '2000-01' WHERE user = ?").bind(id).run();
    expect((await call("/me/usage", { token })).data.finishes_left).toBe(3);
    expect((await call("/apply/finish", { token, method: "POST" })).status).toBe(200);
  });

  it("student 40 and standard 60 a month", async () => {
    for (const [plan, limit] of [["student_monthly", 40], ["standard_monthly", 60]] as const) {
      const { id, token } = await newAccount();
      await givePass(id, plan);
      expect((await call("/me/usage", { token })).data.finishes_left).toBe(limit);
      await env.DB.prepare("INSERT INTO usage(user, period, agent_finishes) VALUES(?, ?, ?)")
        .bind(id, new Date().toISOString().slice(0, 7), limit - 1).run();
      const last = await call("/apply/finish", { token, method: "POST" });
      expect(last.status).toBe(200);
      expect(last.data.finishes_left).toBe(0);
      expect(last.data.tier).toBe(plan);
      expect((await call("/apply/finish", { token, method: "POST" })).status).toBe(402);
    }
  });

  it("two applications starting at once cannot both take the last one", async () => {
    const { id, token } = await newAccount();
    await env.DB.prepare("INSERT INTO usage(user, period, agent_finishes) VALUES(?, ?, 2)")
      .bind(id, new Date().toISOString().slice(0, 7)).run();
    const rs = await Promise.all([1, 2, 3].map(() => call("/apply/finish", { token, method: "POST" })));
    expect(rs.map((r) => r.status).sort()).toEqual([200, 402, 402]);
  });

  it("the migration adds the counter at 0 for every existing row", async () => {
    const cols = await env.DB.prepare("PRAGMA table_info(usage)").all<{ name: string; dflt_value: string; notnull: number }>();
    const c = cols.results.find((x) => x.name === "agent_finishes");
    expect(c).toBeTruthy();
    expect(c!.notnull).toBe(1);
    expect(String(c!.dflt_value)).toBe("0");
  });
});
