// The allowance rules from tests/test_metering.py that phase 1 uses (tiers, models, pass expiry,
// packages, token cap), checked on the pure functions and the D1 store with a fixed clock.
import { describe, expect, it } from "vitest";
import {
  AccountState, DAY_SECONDS, DEFAULT_PLAN, EMPTY_STATE, INTERVIEW_SECONDS, PLANS, QuotaExceeded, modelFor,
  packagesLeft, passActive, periodOf, planFor, requireLlm, status, tierFor,
} from "../src/metering";
import { loadState, recordLlm, setPass, takeFreePackage, takePassPackage } from "../src/store";
import { supportsEffort } from "../src/anthropic";
import { env } from "./helpers";

const T0 = 1_780_000_000;
const P = "2026-07";
const Q = "2026-08";
const D = "2026-07-15";
const NO_ENV = {};

const onPass = (name: "pass30" | "pass90", at = T0): AccountState => ({
  ...EMPTY_STATE, plan: name, passUntil: at + PLANS[name]!.days * DAY_SECONDS,
  passSeconds: PLANS[name]!.avatarSecondsIncluded, passPackages: PLANS[name]!.packages,
});

describe("plans", () => {
  it("the three tiers as decided", () => {
    expect(INTERVIEW_SECONDS).toBe(1200);
    expect(Object.keys(PLANS).sort()).toEqual(["free", "pass30", "pass90"]);
    const shape = Object.fromEntries(Object.entries(PLANS).map(([k, p]) =>
      [k, [p.days, Math.floor(p.avatarSecondsIncluded / INTERVIEW_SECONDS), p.packages, p.llmModel, p.priceLabel, p.llmTokenCap]]));
    expect(shape).toEqual({
      free: [0, 0, 3, "claude-haiku-4-5", "", 400_000],
      pass30: [30, 3, 60, "claude-sonnet-5-5", "$29", 8_000_000],
      pass90: [90, 9, 150, "claude-sonnet-5-5", "$69", 8_000_000],
    });
  });

  it("a new user is free on the cheap model with 3 packages", () => {
    expect(tierFor(EMPTY_STATE, T0)).toBe(DEFAULT_PLAN);
    expect(modelFor(planFor(EMPTY_STATE, T0), NO_ENV)).toBe("claude-haiku-4-5");
    expect(packagesLeft(EMPTY_STATE, T0)).toBe(3);
  });

  it("a pass gives its allowances and the better model", () => {
    const st = status(onPass("pass30"), T0, NO_ENV);
    expect(st).toEqual({ tier: "pass30", pass_until: T0 + 30 * DAY_SECONDS, interviews_left: 3,
                         packages_left: 60, avatar_seconds_left: 3600, llm_model: "claude-sonnet-5-5" });
    expect(status(onPass("pass90"), T0, NO_ENV).interviews_left).toBe(9);
  });

  it("model per tier honours env overrides; an empty override means the default", () => {
    const env = { TAILOR_FREE_MODEL: "claude-haiku-x", TAILOR_PASS_MODEL: "claude-opus-x" };
    expect(modelFor(planFor(EMPTY_STATE, T0), env)).toBe("claude-haiku-x");
    expect(modelFor(planFor(onPass("pass30"), T0), env)).toBe("claude-opus-x");
    expect(modelFor(PLANS.free!, { TAILOR_FREE_MODEL: "" })).toBe("claude-haiku-4-5");
  });

  it("a pass expires exactly at pass_until and the account is free again", () => {
    const s = onPass("pass30");
    const end = T0 + 30 * DAY_SECONDS;
    expect(tierFor(s, end - 1)).toBe("pass30");
    expect(tierFor(s, end)).toBe("free");
    expect(status(s, end, NO_ENV)).toMatchObject({ pass_until: null, packages_left: 3, avatar_seconds_left: 0,
                                                    llm_model: "claude-haiku-4-5" });
  });

  it("retired or unknown plan names read as free", () => {
    for (const plan of ["pro", "student", "trial", "browse", "platinum"]) {
      expect(passActive({ ...EMPTY_STATE, plan, passUntil: T0 + 999 }, T0)).toBe(false);
    }
  });

  it("the token cap is a monthly safety net that includes the request's estimate", () => {
    const cap = PLANS.free!.llmTokenCap!;
    expect(() => requireLlm(EMPTY_STATE, T0, cap)).not.toThrow();
    expect(() => requireLlm(EMPTY_STATE, T0, cap + 1)).toThrow(QuotaExceeded);
    try {
      requireLlm({ ...EMPTY_STATE, llmTokens: cap }, T0, 1);
    } catch (e) {
      expect((e as QuotaExceeded).reason).toBe("token_cap");
    }
    expect(() => requireLlm({ ...onPass("pass30"), llmTokens: cap }, T0, 1_000_000)).not.toThrow();
  });

  it("periods are UTC calendar months", () => {
    expect(periodOf(Date.UTC(2026, 6, 31, 23, 59))).toBe("2026-07");
    expect(periodOf(Date.UTC(2026, 7, 1, 0, 0))).toBe("2026-08");
  });

  it("effort is only sent to models that accept it (same list as provider_anthropic.py)", () => {
    expect(supportsEffort("claude-sonnet-5-5")).toBe(true);
    expect(supportsEffort("claude-opus-4-8")).toBe(true);
    expect(supportsEffort("claude-haiku-4-5")).toBe(false);
    expect(supportsEffort("claude-sonnet-4-5")).toBe(false);
    expect(supportsEffort("some-future-model")).toBe(false);
  });
});

describe("D1 store", () => {
  it("free packages: 3 a month, then refused, and a new month resets", async () => {
    const u = "store-free";
    expect(await takeFreePackage(env.DB, u, P)).toBe(1);
    expect(await takeFreePackage(env.DB, u, P)).toBe(2);
    expect(await takeFreePackage(env.DB, u, P)).toBe(3);
    expect(await takeFreePackage(env.DB, u, P)).toBeNull();
    expect(await takeFreePackage(env.DB, u, Q)).toBe(1);
  });

  it("pass packages come from the pass, not the free monthly count, and run out", async () => {
    const u = "store-pass";
    for (let i = 0; i < 3; i++) await takeFreePackage(env.DB, u, P);
    await setPass(env.DB, u, "pass30", T0 + 30 * DAY_SECONDS, 2700, 2);
    expect(await takePassPackage(env.DB, u, T0)).toBe(1);
    expect(await takePassPackage(env.DB, u, T0)).toBe(0);
    expect(await takePassPackage(env.DB, u, T0)).toBeNull();
    const { state } = await loadState(env.DB, u, P, D);
    expect(state.packagesUsed).toBe(3);                       // the free counter did not move
    expect(packagesLeft(state, T0)).toBe(0);
  });

  it("an expired pass cannot hand out packages", async () => {
    const u = "store-expired";
    await setPass(env.DB, u, "pass30", T0, 2700, 60);
    expect(await takePassPackage(env.DB, u, T0)).toBeNull();
  });

  it("llm tokens are counted per period and the pool only when asked", async () => {
    const u = "store-tokens";
    expect(await recordLlm(env.DB, u, P, 40_000, null)).toBe(40_000);
    expect(await recordLlm(env.DB, u, P, 10_000, D)).toBe(50_000);
    expect(await recordLlm(env.DB, u, Q, 1_000, null)).toBe(1_000);
    const { state, freePoolTokens } = await loadState(env.DB, u, P, D);
    expect(state.llmTokens).toBe(50_000);
    expect(freePoolTokens).toBe(10_000);
  });

  it("setPass clamps negative balances at zero", async () => {
    await setPass(env.DB, "store-clamp", "pass90", T0 + 10, -5, -1);
    const { state } = await loadState(env.DB, "store-clamp", P, D);
    expect(state.passSeconds).toBe(0);
    expect(state.passPackages).toBe(0);
  });
});
