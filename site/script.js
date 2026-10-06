// SponsorJobs landing page. No analytics, no third-party scripts. Two jobs:
//   1. pull the live count of jobs in the shared feed (falls back to static text), and
//   2. point the download buttons at the latest GitHub release, with the button for the
//      visitor's own OS as the single primary action.

// The ONE place the feed host lives; site/_headers connect-src must list the same host.
const FEED_BASE = "https://feed.sponsorjobs.ai";

const REPO_URL = "https://github.com/KofiGilbert/sponsorjobs";

const LINKS = {
  repo: REPO_URL,
  releases: REPO_URL + "/releases/latest",
  issues: REPO_URL + "/issues",
  "repo-install": REPO_URL + "#install",
};

function wireLinks() {
  document.querySelectorAll("a[data-link]").forEach((a) => {
    const href = LINKS[a.dataset.link];
    if (href) a.href = href;
  });
}

function markPrimaryForThisOS() {
  const mac = document.getElementById("dl-mac");
  const win = document.getElementById("dl-win");
  if (!mac || !win) return;
  const ua = navigator.userAgent || "";
  const isWindows = /Windows/i.test(ua);
  const isMac = /Macintosh|Mac OS X/i.test(ua);
  if (isWindows) {
    mac.classList.replace("btn-primary", "btn-ghost");
    win.classList.replace("btn-ghost", "btn-primary");
  } else if (!isMac) {
    // Linux, iOS, Android, unknown: no download applies, so spend the accent nowhere.
    mac.classList.replace("btn-primary", "btn-ghost");
  }
}

function relativeTime(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  const mins = Math.round((Date.now() - t) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return mins + (mins === 1 ? " minute ago" : " minutes ago");
  const hours = Math.round(mins / 60);
  if (hours < 48) return hours + (hours === 1 ? " hour ago" : " hours ago");
  const days = Math.round(hours / 24);
  return days + " days ago";
}

async function liveNumbers() {
  const el = document.getElementById("live-line");
  if (!el) return;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 6000);
    const res = await fetch(FEED_BASE + "/feed/manifest.json", {
      signal: controller.signal,
      cache: "no-store",
      mode: "cors",
    });
    clearTimeout(timer);
    if (!res.ok) return;
    const data = await res.json();
    const count = Number(data.count);
    if (!Number.isFinite(count) || count <= 0) return;
    const when = data.generated_at ? relativeTime(String(data.generated_at)) : null;
    el.textContent = "";
    const n = document.createElement("strong");
    n.textContent = count.toLocaleString("en-US");
    el.append(n, " jobs in the shared list right now");
    if (when) el.append(", updated " + when);
    el.append(".");
  } catch (_err) {
    // Keep the static sentence already in the HTML. It makes no numeric claim.
  }
}

wireLinks();
markPrimaryForThisOS();
liveNumbers();
