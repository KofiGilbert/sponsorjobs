// The admin dashboard's "Cloudflare Free Plan" card and its "Time to switch to Workers Paid" banner
// (src/admin_cf.ts): the thresholds, what the card and banner say, the search migration that holds
// the daily snapshot, and search noting the days it said daily_budget. No network.
import { beforeEach, describe, expect, it } from "vitest";
import { env } from "cloudflare:workers";
import {
  CF_CLOSE_AT, CF_LIMITS, CF_PAY_AT, CF_PLANS_URL, type CfRow, type CfUsage, cfBanner, cfCard, cfUsage, cfVerdict, loadText,
} from "../src/admin_cf";
import type { Env } from "../src/env";

const TODAY = "2026-11-26";
const NOW_MS = Date.parse(`${TODAY}T09:00:00Z`);

const row = (day: string, o: Partial<CfRow> = {}): CfRow => ({
  day, taken_at: `${TODAY}T03:23:00Z`, worker_requests: 10_000, d1_rows_read: 100_000, d1_rows_written: 5_000,
  d1_storage_bytes: 50_000_000, d1_largest_bytes: 40_000_000, d1_largest_name: "sponsorjobs-search",
  load_rows: 96_000, load_total: 500_000, load_per_day: 32_000, load_ready: 0, warning: null, ...o,
});
const usage = (rows: CfRow[], budgetDays: string[] = []): CfUsage => ({ ok: true, today: TODAY, rows, budgetDays });
/** A value at exactly `pct` percent of a limit. */
const at = (key: (typeof CF_LIMITS)[number]["key"], pct: number) => (CF_LIMITS.find((l) => l.key === key)!.limit * pct) / 100;

describe("the thresholds", () => {
  it("no snapshot yet: no banner, and the card says so plainly", () => {
    const v = cfVerdict(usage([]));
    expect(v.level).toBe("none");
    expect(cfBanner(v)).toBe("");
    const html = cfCard(usage([]), v, NOW_MS / 1000);
    expect(html).toContain("No snapshot yet");
    expect(html).toContain("Account Analytics Read");
    expect(html).toContain('id="cloudflare"');
  });

  it("49%: within limits, no banner", () => {
    const v = cfVerdict(usage([row("2026-11-25", { worker_requests: at("worker_requests", 49) })]));
    expect(v.level).toBe("ok");
    expect(cfBanner(v)).toBe("");
  });

  it("50%: the softer Getting close note, naming the limit, with no pay link", () => {
    const v = cfVerdict(usage([row("2026-11-25", { d1_rows_read: at("d1_rows_read", CF_CLOSE_AT) })]));
    expect(v.level).toBe("close");
    expect(v.reasons).toEqual(["D1 rows read reached 50% of the free 5,000,000 a day"]);
    const b = cfBanner(v);
    expect(b).toContain("Getting close to the Cloudflare free plan");
    expect(b).not.toContain("Time to switch");
    expect(b).not.toContain(CF_PLANS_URL);
  });

  it("70% on any day of the last 7 (the peak): Time to switch, naming the limit, with the plans link", () => {
    const v = cfVerdict(usage([
      row("2026-11-25", { d1_rows_written: 20_000 }),
      row("2026-11-21", { d1_rows_written: at("d1_rows_written", CF_PAY_AT) }),     // the peak, 5 days ago
    ]));
    expect(v.level).toBe("pay");
    expect(v.reasons).toEqual(["D1 rows written reached 70% of the free 100,000 a day"]);
    const b = cfBanner(v);
    expect(b).toContain("Time to switch to Workers Paid ($5 a month)");
    expect(b).toContain(`href="${CF_PLANS_URL}"`);
    expect(b).toContain('href="/health#cloudflare"');
    expect(b).not.toMatch(/[\u2013\u2014]/);                                     // no en or em dashes
    // Just under 70% stays a "close" note (and is shown as 69%, never rounded up to 70%).
    const under = cfVerdict(usage([row("2026-11-25", { d1_rows_written: at("d1_rows_written", CF_PAY_AT) - 1 })]));
    expect(under.level).toBe("close");
    expect(under.reasons[0]).toContain("69%");
    // A peak older than 7 days no longer counts.
    const old = cfVerdict(usage([row("2026-11-25"), row("2026-11-18", { d1_rows_written: 99_000 })]));
    expect(old.level).toBe("ok");
  });

  it("storage at 70% of 500 MB for one database triggers it too", () => {
    const v = cfVerdict(usage([row("2026-11-25", { d1_largest_bytes: 350_000_000 })]));
    expect(v.level).toBe("pay");
    expect(v.reasons).toEqual(["Largest D1 database reached 70% of the free 500 MB a database"]);
  });

  it("search said daily_budget on one of the last 7 days: Time to switch, even with low numbers", () => {
    const v = cfVerdict(usage([row("2026-11-25")], ["2026-11-24"]));
    expect(v.level).toBe("pay");
    expect(v.reasons).toEqual(["Search hit its daily limit on 2026-11-24"]);
    expect(cfBanner(v)).toContain("Search hit its daily limit on 2026-11-24");
    // With no snapshot at all, the daily_budget days still raise it.
    expect(cfVerdict(usage([], ["2026-11-26"])).level).toBe("pay");
  });
});

describe("the card", () => {
  it("shows each limit as used / limit with a percentage, latest and 7-day peak, the load and the snapshot date", () => {
    const u = usage([row("2026-11-25", { worker_requests: 72_431 }), row("2026-11-24", { worker_requests: 80_000 })]);
    const html = cfCard(u, cfVerdict(u), NOW_MS / 1000);
    expect(html).toContain("Cloudflare Free Plan");
    expect(html).toContain("Time to pay");
    expect(html).toContain("72,431 (72%)");                 // latest day
    expect(html).toContain("80,000 (80%)");                 // 7-day peak
    expect(html).toContain("100,000 a day");
    expect(html).toContain("Peak, 2 days");
    expect(html).toContain("40 MB (8%)");                   // storage, per database
    expect(html).toContain("500 MB a database");
    expect(html).toContain("5 GB in all");
    expect(html).toContain("96,000 of 500,000 jobs loaded, about 13 days left");
    expect(html).toContain("2026-11-25, written 6 h ago");
    expect(html).toContain("Not hit in 7 days");
    expect(html).not.toContain("Mostly the search first load");      // rows written are low here
    expect(html).not.toMatch(/[\u2013\u2014]/);
    // During the first load the loader spends most of the write allowance: the card says why.
    const busy = usage([row("2026-11-25", { d1_rows_written: 80_000 })]);
    expect(cfCard(busy, cfVerdict(busy), NOW_MS / 1000)).toContain("Mostly the search first load");
  });

  it("says what could not be read, and when the snapshot is old", () => {
    const u = { ...usage([row("2026-11-20", { worker_requests: null, warning: "Workers requests: not authorized" })]) };
    const html = cfCard(u, cfVerdict(u), NOW_MS / 1000);
    expect(html).toContain("Not read");
    expect(html).toContain("Workers requests: not authorized");
    expect(html).toContain("The last snapshot is 6 days old");
    expect(cfCard({ ok: false, why: "The search database is not connected to this Worker.", today: TODAY, rows: [], budgetDays: [] },
      cfVerdict(usage([])), NOW_MS / 1000)).toContain("not connected");
  });

  it("the first-load line", () => {
    expect(loadText(row("2026-11-25", { load_ready: 1, load_rows: 500_000 }))).toBe("First load done: 500,000 jobs in search");
    expect(loadText(row("2026-11-25", { load_per_day: null }))).toBe("96,000 of 500,000 jobs loaded, days left unknown");
    expect(loadText(row("2026-11-25", { load_rows: 499_000 }))).toContain("about 1 day left");
    expect(loadText(row("2026-11-25", { load_total: null }))).toBeNull();
  });
});

describe("the search database (migration 0002 and the reads)", () => {
  const db = env.SEARCH_DB!;
  beforeEach(async () => {
    await db.exec("DELETE FROM cf_usage; DELETE FROM search_budget_days; DELETE FROM search_usage;");
  });

  it("creates cf_usage (one row a day) and search_budget_days", async () => {
    const cols = (await db.prepare("SELECT name FROM pragma_table_info('cf_usage')").all<{ name: string }>()).results.map((r) => r.name);
    expect(cols).toEqual(["day", "taken_at", "worker_requests", "d1_rows_read", "d1_rows_written", "d1_storage_bytes",
      "d1_largest_bytes", "d1_largest_name", "d1_databases", "load_rows", "load_total", "load_per_day", "load_ready", "warning"]);
    await db.prepare("INSERT INTO cf_usage (day, taken_at) VALUES ('2026-11-25', 'x')").run();
    await expect(db.prepare("INSERT INTO cf_usage (day, taken_at) VALUES ('2026-11-25', 'y')").run()).rejects.toThrow();
    const hits = (await db.prepare("SELECT name FROM pragma_table_info('search_budget_days')").all<{ name: string }>()).results.map((r) => r.name);
    expect(hits).toEqual(["day", "hits"]);
  });

  it("cfUsage reads the snapshots newest first and the daily_budget days, from the table and from the counters", async () => {
    await db.batch([
      db.prepare("INSERT INTO cf_usage (day, taken_at, worker_requests) VALUES ('2026-11-24', 'a', 1), ('2026-11-25', 'b', 2)"),
      db.prepare("INSERT INTO search_budget_days (day, hits) VALUES ('2026-11-23', 2), ('2026-11-01', 1)"),
      db.prepare("INSERT INTO search_usage (day, rows_read, rows_written, requests) VALUES ('2026-11-25', 3500000, 0, 10), ('2026-11-26', 10, 0, 1)"),
    ]);
    const u = await cfUsage(env as Env, NOW_MS);
    expect(u.ok).toBe(true);
    expect(u.rows.map((r) => r.day)).toEqual(["2026-11-25", "2026-11-24"]);
    expect(u.budgetDays).toEqual(["2026-11-25", "2026-11-23"]);     // 11-01 is too old; 11-25 reached the cap
    // A paid plan with no search cap ("0") infers nothing from the counters.
    expect((await cfUsage({ ...env, SEARCH_DAILY_ROWS_READ: "0" } as Env, NOW_MS)).budgetDays).toEqual(["2026-11-23"]);
    const none = await cfUsage({ ...env, SEARCH_DB: undefined } as Env, NOW_MS);
    expect(none.ok).toBe(false);
    expect(none.why).toContain("not connected");
  });
});
