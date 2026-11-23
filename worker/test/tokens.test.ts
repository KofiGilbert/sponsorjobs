// Bearer token lifetime and sign-out: a token is good for TOKEN_TTL_SECONDS from its last use
// (rolling), POST /account/revoke deletes the token the call carries, and a token that existed
// before the expiry migration counts as used now (nobody is signed out by the deploy).
import { describe, expect, it } from "vitest";
import { issueToken, resolve, revokeToken, TOKEN_TOUCH_SECONDS, TOKEN_TTL_SECONDS, sha256Hex } from "../src/accounts";
import { call, env, newAccount, signedInAccount } from "./helpers";

const now = () => Math.floor(Date.now() / 1000);

async function lastUsed(token: string): Promise<number | null> {
  const row = await env.DB.prepare("SELECT last_used_at FROM tokens WHERE token_hash = ?")
    .bind(await sha256Hex(token)).first<{ last_used_at: number }>();
  return row ? row.last_used_at : null;
}

async function setLastUsed(token: string, at: number) {
  await env.DB.prepare("UPDATE tokens SET last_used_at = ? WHERE token_hash = ?").bind(at, await sha256Hex(token)).run();
}

describe("token expiry", () => {
  it("a new token is stamped with its issue time", async () => {
    const { token } = await newAccount();
    expect(Math.abs((await lastUsed(token))! - now())).toBeLessThanOrEqual(2);
    const s = await signedInAccount();
    expect(await lastUsed(s.token)).toBeGreaterThan(now() - 5);
  });

  it("a token used inside the window keeps working and its window moves forward", async () => {
    const { token } = await newAccount();
    const old = now() - (TOKEN_TTL_SECONDS - 3600);              // an hour before it would expire
    await setLastUsed(token, old);
    expect((await call("/account/me", { token })).status).toBe(200);
    expect(await lastUsed(token)).toBeGreaterThan(now() - 5);    // extended to now
  });

  it("the extension is written at most once a day", async () => {
    const { token } = await newAccount();
    const recent = now() - (TOKEN_TOUCH_SECONDS - 600);
    await setLastUsed(token, recent);
    expect((await call("/account/me", { token })).status).toBe(200);
    expect(await lastUsed(token)).toBe(recent);
  });

  it("a token unused for longer than the window answers 401 and is deleted", async () => {
    const { token } = await newAccount();
    await setLastUsed(token, now() - TOKEN_TTL_SECONDS - 60);
    const r = await call("/me/usage", { token });
    expect(r.status).toBe(401);
    expect(await lastUsed(token)).toBeNull();
    // Gone for good, even if the clock were turned back.
    expect(await resolve(env.DB, token, now() - TOKEN_TTL_SECONDS)).toBeNull();
  });

  it("a token with no stamp (imported, or from before the migration) counts as used now", async () => {
    const { id, token } = await newAccount();
    await setLastUsed(token, 0);
    expect(await resolve(env.DB, token)).toBe(id);
    expect(await lastUsed(token)).toBeGreaterThan(now() - 5);
  });

  it("the migration stamps every existing token with the time it ran, never 0", async () => {
    const { results } = await env.DB.prepare("SELECT COUNT(*) AS n FROM tokens WHERE last_used_at IS NULL").all<{ n: number }>();
    expect(results[0]!.n).toBe(0);
  });
});

describe("POST /account/revoke", () => {
  it("revokes the calling token only: it answers 401 afterwards, other devices keep working", async () => {
    const a = await signedInAccount();
    const other = await issueToken(env.DB, a.id);       // a second device on the same account
    const r = await call("/account/revoke", { method: "POST", token: a.token });
    expect(r.status).toBe(200);
    expect(r.data).toEqual({ ok: true, revoked: true });
    expect((await call("/account/me", { token: a.token })).status).toBe(401);
    expect((await call("/account/me", { token: other })).status).toBe(200);
  });

  it("is idempotent and answers 200 without a token or with an unknown one", async () => {
    const { token } = await newAccount();
    expect((await call("/account/revoke", { method: "POST", token })).data.revoked).toBe(true);
    const again = await call("/account/revoke", { method: "POST", token });
    expect(again.status).toBe(200);
    expect(again.data).toEqual({ ok: true, revoked: false });
    expect((await call("/account/revoke", { method: "POST" })).data).toEqual({ ok: true, revoked: false });
    expect((await call("/account/revoke", { method: "POST", token: "not-a-token" })).data.revoked).toBe(false);
    expect(await revokeToken(env.DB, "")).toBe(false);
  });
});
