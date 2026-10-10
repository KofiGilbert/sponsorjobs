// Popup: shows where the visa data comes from (the extension's own index, or the local
// SponsorJobs app) and lets you check any employer by name. Fully usable without visiting a job
// board, and without the app: the badges and the check run on the index (sponsor_core.js).

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function setStatus(cls, text) {
  $("dot").className = "dot " + cls;
  $("statusText").textContent = text;
}

function ask(msg) {
  return new Promise((resolve) => {
    try { chrome.runtime.sendMessage(msg, (r) => resolve(chrome.runtime.lastError ? { ok: false } : r)); }
    catch { resolve({ ok: false }); }
  });
}

function dataParts(s) {
  const parts = [];
  if (s.h1b) parts.push(`${s.h1b.toLocaleString()} H-1B`);
  if (s.perm) parts.push(`${s.perm.toLocaleString()} green-card`);
  if (s.e_verify) parts.push(`${s.e_verify.toLocaleString()} E-Verify`);
  return parts;
}

async function refreshStatus() {
  const s = await ask({ type: "status" });
  if (!s || s.ok === false) {
    // No app: the badges still work from the extension's own copy of the public data.
    const idx = await ask({ type: "index_status" });
    if (idx && idx.ok) {
      const parts = dataParts(idx);
      setStatus("ok", `Visa data ready · ${parts.join(" · ") || (idx.employers || 0).toLocaleString() + " employers"}`);
      $("appHint").hidden = false;
      return true;
    }
    setStatus("bad", "Visa data not downloaded yet. Check your connection, or open the SponsorJobs app.");
    return false;
  }
  const parts = dataParts(s);
  const n = s.employers || 0;
  setStatus("ok", n ? `Connected to your local app · ${parts.join(" · ") || n.toLocaleString() + " employers"}`
                     : "Connected to your local app · visa data still loading…");
  return true;
}

// A read-only mirror of the app's application tracking (the app stays the source of truth).
async function loadTracking() {
  const t = await ask({ type: "tracking" });
  if (!t || t.ok === false) return;   // app not running: leave the section hidden
  $("tTotal").textContent = (t.total || 0).toLocaleString();
  $("tApplied").textContent = (t.applied || 0).toLocaleString();
  $("tReview").textContent = (t.in_review || 0).toLocaleString();
  $("trackSection").hidden = false;
}

function renderResult(data) {
  const box = $("result");
  if (!data || data.ok === false) {
    box.innerHTML = `<span class="muted">Couldn't load the visa data. Check your connection.</span>`;
    return;
  }
  if (!data.matched) {
    box.innerHTML = `<span class="muted">No H-1B / green-card sponsorship record found for “${esc(data.company)}”.</span>`;
    return;
  }
  // No location here, so `visa` (the per-ROLE claim) is empty by design; show the employer's
  // record from the profile instead, the same facts the page's corner panel shows.
  const p = data.profile || {};
  const codes = (data.visa || []).length ? data.visa.map(v => v.code)
    : [p.h1b_approvals > 0 && "H-1B", p.perm_certs > 0 && "GREEN-CARD", p.e_verify && "STEM-OPT",
       p.cap_exempt && "CAP-EXEMPT"].filter(Boolean);
  const chips = codes.map(c => `<span class="b ${esc(c)}">${esc(c)}</span>`).join("");
  box.innerHTML = chips + `<span class="muted" style="width:100%">Matched: ${esc(data.matched_name)}</span>`;
}

async function check() {
  const company = $("company").value.trim();
  if (!company) return;
  $("result").innerHTML = `<span class="muted">Checking…</span>`;
  renderResult(await ask({ type: "lookup", company }));
}

$("check").addEventListener("click", check);
$("company").addEventListener("keydown", (e) => { if (e.key === "Enter") check(); });

// --- Application answers (work auth / sponsorship / EEO), saved in the local app ---
async function loadPrefs() {
  const r = await ask({ type: "prefs_get" });
  const p = (r && r.prefs) || {};
  $("workAuth").value = p.work_authorized || "";
  $("needSpon").value = p.needs_sponsorship || "";
  $("relocate").value = p.willing_to_relocate || "";
  $("over18").value = p.over_18 || "";
  $("yearsExp").value = p.years_experience || "";
  $("desiredSalary").value = p.desired_salary || "";
  $("earliestStart").value = p.earliest_start || "";
  $("hearAbout").value = p.hear_about_us || "";
  $("eeoDecline").checked = !!p.eeo_decline;
}

async function savePrefs() {
  const btn = $("savePrefs");
  btn.disabled = true;
  const prefs = {
    work_authorized: $("workAuth").value,
    needs_sponsorship: $("needSpon").value,
    willing_to_relocate: $("relocate").value,
    over_18: $("over18").value,
    years_experience: $("yearsExp").value,
    desired_salary: $("desiredSalary").value,
    earliest_start: $("earliestStart").value,
    hear_about_us: $("hearAbout").value,
    eeo_decline: $("eeoDecline").checked,
  };
  const r = await ask({ type: "prefs_set", prefs });
  $("prefsSaved").textContent = r && r.ok ? "Saved, used next time you fill a form." : "Couldn't save. Saved answers live in the SponsorJobs app: open it (or install it free), then try again.";
  setTimeout(() => { $("prefsSaved").textContent = ""; }, 3000);
  btn.disabled = false;
}

$("savePrefs").addEventListener("click", savePrefs);

refreshStatus();
loadPrefs();
loadTracking();
