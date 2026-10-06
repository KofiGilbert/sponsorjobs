"""Job-match alerts and the away digest (Telegram, CLAUDE.md sections 5 and 7).

After the feed refreshes, pick the few NEW roles that fit the saved profile best and tell
the person on Telegram, with honest buttons:

* a role on a site in the verified AUTO allowlist (submit/allowlist.py), when the person has
  turned autonomous submission ON: [Apply for me] (tailor, then submit through the
  sanctioned path);
* every other role, and the auto sites while autonomous submission is off:
  [Tailor and queue] (tailor into the review queue; the person clicks submit);
* always [Skip] (dismiss) and [Fewer like this] (down-weight that title/company).

Limits: a daily cap (default 3), a match threshold (default 70), quiet hours (22:00 to
08:00 local; alerts wait until morning), never the same job twice. Everything here is
local state in one JSON file in the data dir. The broker never sees a source_id or profile
data: button ids carry a short local hash (``job:queue:1a2b3c4d``) mapped back here.

The scoring, sending and action callables are injected, so all of it runs offline in tests.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

PREFS_DEFAULT = {
    "job_alerts": True,       # "New job matches"
    "app_updates": True,      # "Application updates" (the away digest)
    "daily_cap": 3,           # 1, 3 or 5
    "threshold": 70,          # minimum match percent to alert
    "quiet_start": 22,        # local hour quiet hours begin
    "quiet_end": 8,           # local hour they end
    "show_contact": False,    # CV previews in Telegram show the contact line (default: covered)
}
CAP_CHOICES = (1, 3, 5)
LOOKBACK_DAYS = 3             # a role is "new" if first seen / posted within this window
JD_FETCH_TOP = 5              # only the top few by the cheap pre-score get a JD fetch
QUEUE_TTL_HOURS = 36          # a queued alert older than this is stale; drop it
_MAX_SENT = 5000
_MAX_CONSIDERED = 4000

_STOP = frozenset("""a an and the of for in at to with on or i ii iii iv
senior sr jr junior lead staff principal associate intern internship new grad
remote hybrid onsite us usa full time part contract temporary level entry mid""".split())


def short_id(source_id: str) -> str:
    return hashlib.sha1(str(source_id).encode("utf-8")).hexdigest()[:10]


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9+#]+", (text or "").lower())
            if len(t) > 1 and t not in _STOP]


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    start, end = int(start) % 24, int(end) % 24
    if start == end:
        return False
    if start > end:                       # wraps midnight, e.g. 22 -> 8
        return hour >= start or hour < end
    return start <= hour < end


def _parse_when(value) -> datetime | None:
    """ISO / sqlite timestamps (sqlite CURRENT_TIMESTAMP is UTC, no zone) -> aware UTC."""
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    for cand in (s, s.replace(" ", "T", 1)):
        try:
            d = datetime.fromisoformat(cand)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------- state
class NotifyState:
    """The one small JSON file every notification feature shares (atomic replace)."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:   # noqa: BLE001 - missing or corrupt: start clean
            return {}

    def save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def update(self, fn):
        """Load, let ``fn`` mutate, save. Returns fn's result."""
        with self._lock:
            d = self.load()
            out = fn(d)
            self.save(d)
            return out

    # -- prefs ------------------------------------------------------------------ #
    def prefs(self) -> dict:
        return {**PREFS_DEFAULT, **(self.load().get("prefs") or {})}

    def set_prefs(self, patch: dict) -> dict:
        def fn(d):
            cur = {**PREFS_DEFAULT, **(d.get("prefs") or {})}
            for k in ("job_alerts", "app_updates", "show_contact"):
                if k in patch:
                    cur[k] = bool(patch[k])
            if "daily_cap" in patch:
                try:
                    cap = int(patch["daily_cap"])
                except (TypeError, ValueError):
                    cap = PREFS_DEFAULT["daily_cap"]
                cur["daily_cap"] = cap if cap in CAP_CHOICES else PREFS_DEFAULT["daily_cap"]
            if "threshold" in patch:
                try:
                    cur["threshold"] = max(40, min(95, int(patch["threshold"])))
                except (TypeError, ValueError):
                    pass
            for k in ("quiet_start", "quiet_end"):
                if k in patch:
                    try:
                        cur[k] = int(patch[k]) % 24
                    except (TypeError, ValueError):
                        pass
            d["prefs"] = cur
            return cur
        return self.update(fn)

    # -- small keyed values (cursor, offset, markers) ---------------------------- #
    def get(self, key, default=None):
        return self.load().get(key, default)

    def set(self, key, value) -> None:
        def fn(d):
            d[key] = value
        self.update(fn)


# --------------------------------------------------------------------------- scoring
def profile_targets(profile: dict) -> dict:
    """The cheap pre-score's view of the profile: role-title words and skill words."""
    titles, skills = [], []
    for org in (profile or {}).get("experience") or []:
        for role in (org or {}).get("roles") or []:
            titles.append(role.get("title") or "")
        if org.get("title"):
            titles.append(org.get("title"))
    for p in (profile or {}).get("projects") or []:
        titles.append((p or {}).get("title") or "")
    sk = (profile or {}).get("skills") or {}
    if isinstance(sk, dict):
        for v in sk.values():
            skills.extend(v if isinstance(v, list) else str(v).split(","))
    elif isinstance(sk, list):
        skills.extend(str(s) for s in sk)
    for t in (profile or {}).get("target_titles") or []:
        titles.append(str(t))
    return {"title_words": set(_tokens(" ".join(titles))),
            "skill_words": {w for s in skills for w in _tokens(s)}}


def fewer_penalty(row: dict, fewer: dict) -> int:
    comp = (fewer or {}).get("company") or {}
    title = (fewer or {}).get("title") or {}
    pen = 15 * int(comp.get((row.get("company") or "").strip().lower(), 0))
    pen += 8 * sum(int(title.get(t, 0)) for t in set(_tokens(row.get("title") or "")))
    return min(pen, 60)


def prescore(row: dict, targets: dict) -> int:
    """0..100 from the job TITLE alone vs the profile's role titles and skills. Cheap: no JD."""
    words = set(_tokens(row.get("title") or ""))
    if not words:
        return 0
    tw, sw = targets.get("title_words") or set(), targets.get("skill_words") or set()
    hit = len(words & tw) + 0.5 * len((words - tw) & sw)
    return int(round(100 * min(1.0, hit / max(1, len(words)))))


def match_percent(match: dict | None) -> int | None:
    """One honest number from ui/app.py's _job_match: must-have coverage weighs most."""
    if not match:
        return None
    ratio = float(match.get("ratio") or 0.0)
    req_total = int(match.get("required_total") or 0)
    req = (int(match.get("required_covered") or 0) / req_total) if req_total else ratio
    return int(round(100 * (0.6 * req + 0.4 * ratio)))


def is_new(row: dict, now: datetime, days: int = LOOKBACK_DAYS) -> bool:
    when = None
    for k in ("first_seen", "posted_at"):
        d = _parse_when(row.get(k))
        if d and (when is None or d > when):
            when = d
    return bool(when) and when >= now.astimezone(timezone.utc) - timedelta(days=days)


def sponsor_ok(row: dict) -> bool:
    """US roles from sponsors (an H-1B/PERM badge) or that state sponsorship."""
    if row.get("us") is False:
        return False
    return bool(row.get("visa")) or row.get("sponsorship_stated") is True


def sponsor_phrase(row: dict) -> str:
    codes = [str((b or {}).get("code") or "") for b in (row.get("visa") or [])]
    if "H-1B" in codes:
        return "H-1B sponsor"
    if "GREEN-CARD" in codes:
        return "green card sponsor"
    if row.get("sponsorship_stated") is True:
        return "sponsorship stated"
    return ""


# --------------------------------------------------------------------------- messages
def _article(word: str) -> str:
    return "an" if (word or "x")[0].lower() in "aeiou" else "a"


def alert_text(alert: dict, can_apply: bool) -> str:
    company = alert.get("company") or "A company"
    title = alert.get("title") or "role"
    loc = (alert.get("location") or "").strip()
    where = f" in {loc}" if loc else ""
    score = int(alert.get("score") or 0)
    fit = "Strong fit" if score >= 85 else "Good fit"
    extras = [f"{score}% match"]
    if alert.get("sponsor"):
        extras.append(alert["sponsor"])
    head = (f"{company} is hiring {_article(title)} {title}{where}. "
            f"{fit} for your profile ({', '.join(extras)}).")
    if can_apply:
        return (head + " This site accepts applications from SponsorJobs. "
                "Want me to tailor your resume and apply for you?")
    return head + " Want me to tailor your resume and queue it? I'll prepare it; you click submit."


def alert_buttons(alert: dict, can_apply: bool) -> list:
    sid = alert["short"]
    first = ({"id": f"job:apply:{sid}", "label": "Apply for me"} if can_apply
             else {"id": f"job:queue:{sid}", "label": "Tailor and queue"})
    return [[first, {"id": f"job:skip:{sid}", "label": "Skip"}],
            [{"id": f"job:fewer:{sid}", "label": "Fewer like this"}]]


def digest_text(d: dict) -> str:
    parts = [f"tailored {d['tailored']}"]
    if d.get("submitted"):
        sites = ", ".join(d.get("submitted_sites") or [])
        parts.append(f"submitted {d['submitted']}" + (f" ({sites})" if sites else ""))
    if d.get("waiting"):
        parts.append(f"{d['waiting']} waiting for your click")
    tail = (" Open SponsorJobs and go to the review queue to finish them."
            if d.get("waiting") else "")
    return "While you were away: " + ", ".join(parts) + "." + tail


# --------------------------------------------------------------------------- engine
class AlertEngine:
    """Pick, queue, send and act on job-match alerts.

    ``match_fn(row) -> int | None`` scores one shortlisted role (fetches its JD); the app
    supplies it from _job_match. ``targets`` is profile_targets(saved profile)."""

    def __init__(self, state: NotifyState, match_fn=None):
        self.state = state
        self.match_fn = match_fn

    # -- picking -------------------------------------------------------------- #
    def pick(self, rows, targets: dict, now: datetime) -> list[dict]:
        """Score NEW rows and queue the best ones above threshold. Returns what was queued."""
        st = self.state.load()
        prefs = {**PREFS_DEFAULT, **(st.get("prefs") or {})}
        if not prefs["job_alerts"]:
            return []
        sent = set(st.get("sent") or [])
        dismissed = set(st.get("dismissed") or [])
        considered = st.get("considered") or {}
        queued = {q["sid"] for q in st.get("queue") or []}
        fewer = st.get("fewer") or {}
        cands = []
        for r in rows or []:
            sid = r.get("source_id")
            if not sid or sid in sent or sid in dismissed or sid in queued or sid in considered:
                continue
            if not sponsor_ok(r) or not is_new(r, now):
                continue
            pre = prescore(r, targets) - fewer_penalty(r, fewer)
            if pre <= 0:
                continue
            cands.append((pre, r))
        cands.sort(key=lambda x: -x[0])
        threshold = int(prefs["threshold"])
        picked = []
        for pre, r in cands[:JD_FETCH_TOP]:
            score = None
            if self.match_fn is not None:
                try:
                    score = self.match_fn(r)
                except Exception:   # noqa: BLE001 - one bad JD never stops the rest
                    score = None
            if score is None:
                score = pre
            score = int(score) - fewer_penalty(r, fewer)
            considered[r["source_id"]] = score
            if score >= threshold:
                picked.append({"sid": r["source_id"], "short": short_id(r["source_id"]),
                               "title": r.get("title") or "", "company": r.get("company") or "",
                               "location": r.get("location") or "", "url": r.get("url") or "",
                               "source": r.get("source") or "",
                               "score": score, "sponsor": sponsor_phrase(r),
                               "queued_at": now.astimezone(timezone.utc).isoformat()})
        picked.sort(key=lambda a: -a["score"])

        def fn(d):
            c = d.get("considered") or {}
            c.update(considered)
            if len(c) > _MAX_CONSIDERED:
                c = dict(list(c.items())[-_MAX_CONSIDERED:])
            d["considered"] = c
            q = d.get("queue") or []
            have = {x["sid"] for x in q}
            q.extend(a for a in picked if a["sid"] not in have)
            q.sort(key=lambda a: -int(a.get("score") or 0))
            d["queue"] = q[:20]
        self.state.update(fn)
        return picked

    # -- sending -------------------------------------------------------------- #
    def send_due(self, channel, now: datetime, can_apply_fn) -> list[dict]:
        """Send queued alerts when not in quiet hours and under today's cap. A failed send
        leaves the alert queued. ``can_apply_fn(alert) -> bool`` decides the honest buttons."""
        st = self.state.load()
        prefs = {**PREFS_DEFAULT, **(st.get("prefs") or {})}
        if not prefs["job_alerts"] or channel is None:
            return []
        local = now.astimezone() if now.tzinfo else now
        if in_quiet_hours(local.hour, prefs["quiet_start"], prefs["quiet_end"]):
            return []
        today = local.date().isoformat()
        count = int((st.get("sent_days") or {}).get(today, 0))
        cutoff = now.astimezone(timezone.utc) - timedelta(hours=QUEUE_TTL_HOURS)
        sent_now = []
        for a in list(st.get("queue") or []):
            if count >= int(prefs["daily_cap"]):
                break
            when = _parse_when(a.get("queued_at"))
            if when and when < cutoff:
                self._drop_from_queue(a["sid"])
                continue
            if a["sid"] in set(st.get("sent") or []) or a["sid"] in set(st.get("dismissed") or []):
                self._drop_from_queue(a["sid"])
                continue
            can_apply = bool(can_apply_fn(a))
            channel.send(alert_text(a, can_apply), alert_buttons(a, can_apply))
            count += 1
            sent_now.append(a)
            self._mark_sent(a, today, count)
        return sent_now

    def _drop_from_queue(self, sid):
        def fn(d):
            d["queue"] = [x for x in (d.get("queue") or []) if x["sid"] != sid]
        self.state.update(fn)

    def _mark_sent(self, alert, today, count):
        def fn(d):
            d["queue"] = [x for x in (d.get("queue") or []) if x["sid"] != alert["sid"]]
            sent = list(d.get("sent") or [])
            sent.append(alert["sid"])
            d["sent"] = sent[-_MAX_SENT:]
            days = {k: v for k, v in (d.get("sent_days") or {}).items() if k >= _days_ago(today, 7)}
            days[today] = count
            d["sent_days"] = days
            shorts = d.get("shorts") or {}
            shorts[alert["short"]] = {k: alert.get(k) for k in
                                      ("sid", "title", "company", "location", "url", "source")}
            if len(shorts) > 500:
                shorts = dict(list(shorts.items())[-500:])
            d["shorts"] = shorts
        self.state.update(fn)

    # -- button taps ---------------------------------------------------------- #
    def handle_button(self, data: str, actions) -> str:
        """``job:<action>:<short>`` -> do it, return the reply text. ``actions`` supplies
        queue(job), apply(job), dismiss(sid), autonomous_ok(job); each returns a message."""
        parts = (data or "").split(":")
        if len(parts) != 3 or parts[0] != "job":
            return "I didn't recognise that button."
        _, act, short = parts
        job = (self.state.load().get("shorts") or {}).get(short)
        if not job:
            return "That alert has expired. Open SponsorJobs to find the role."
        who = f"{job.get('title') or 'the role'} at {job.get('company') or 'the company'}"
        if act == "skip":
            self._dismiss(job["sid"])
            try:
                actions.dismiss(job["sid"])
            except Exception:   # noqa: BLE001 - our own dismissed list is what matters
                pass
            return f"Skipped {who}. I won't show it again."
        if act == "fewer":
            self._fewer(job)
            self._dismiss(job["sid"])
            return f"Got it. Fewer roles like {who}."
        if act == "queue":
            return actions.queue(job)
        if act == "apply":
            # Re-check at tap time: the setting or the allowlist may have changed since.
            if not actions.autonomous_ok(job):
                return actions.queue(job) + " (Autonomous submission is off, so you click submit.)"
            return actions.apply(job)
        return "I didn't recognise that button."

    def _dismiss(self, sid):
        def fn(d):
            ds = list(d.get("dismissed") or [])
            if sid not in ds:
                ds.append(sid)
            d["dismissed"] = ds[-_MAX_SENT:]
            d["queue"] = [x for x in (d.get("queue") or []) if x["sid"] != sid]
        self.state.update(fn)

    def _fewer(self, job):
        def fn(d):
            f = d.get("fewer") or {}
            comp = f.get("company") or {}
            title = f.get("title") or {}
            c = (job.get("company") or "").strip().lower()
            if c:
                comp[c] = int(comp.get(c, 0)) + 1
            for t in set(_tokens(job.get("title") or "")):
                title[t] = int(title.get(t, 0)) + 1
            d["fewer"] = {"company": comp, "title": title}
        self.state.update(fn)


def _days_ago(today_iso: str, n: int) -> str:
    try:
        return (datetime.fromisoformat(today_iso) - timedelta(days=n)).date().isoformat()
    except ValueError:
        return ""


# --------------------------------------------------------------------------- digest
def away_activity(records: list[dict], since: datetime, site_name_fn=None) -> dict:
    """What happened since ``since``: tailored (records created), submitted (auto submissions,
    with site names), waiting (created and still waiting for the person's click). ``records``
    are cv_record rows with ``data`` already a dict. Also returns the first/last activity time."""
    tailored = submitted = waiting = 0
    sites: list[str] = []
    first = last = None

    def touch(t):
        nonlocal first, last
        if t is None:
            return
        first = t if first is None or t < first else first
        last = t if last is None or t > last else last

    for r in records or []:
        data = r.get("data") or {}
        created = _parse_when(r.get("created_at"))
        if created and created > since:
            tailored += 1
            touch(created)
            if (data.get("status") or "ready") in ("ready", "approved"):
                waiting += 1
        sub = data.get("submission") or {}
        at = _parse_when(sub.get("at"))
        if sub.get("status") == "auto_submitted" and at and at > since:
            submitted += 1
            touch(at)
            name = site_name_fn((data.get("source_job") or {}).get("url") or "") if site_name_fn else ""
            if name and name not in sites:
                sites.append(name)
    return {"tailored": tailored, "submitted": submitted, "submitted_sites": sites,
            "waiting": waiting, "first": first, "last": last}


DIGEST_SETTLE_MIN = 10   # wait this long after the last activity before sending


def digest_due(activity: dict, presence_at: datetime | None, now: datetime) -> str:
    """'send', 'wait', 'reset' (the person came back, so they saw it) or 'none'."""
    if not (activity.get("tailored") or activity.get("submitted")):
        return "none"
    first, last = activity.get("first"), activity.get("last")
    if presence_at and first and presence_at >= first:
        return "reset"
    if last and now.astimezone(timezone.utc) - last < timedelta(minutes=DIGEST_SETTLE_MIN):
        return "wait"
    return "send"
