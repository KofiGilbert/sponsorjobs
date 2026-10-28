/* SponsorJobs, Stage 2. API-driven: real intake, real draft, real assembler PDF,
   real coverage, flagged-title confirm, memory. Visual design unchanged. */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const el = (t, c) => { const n = document.createElement(t); if (c) n.className = c; return n; };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, m =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));

  /* SponsorJobs is an ONLINE, real-model tool. When it can't reach the model we don't want a
     generic error toast: "you're offline", "add your key" and "your credit ran out" are
     different problems with different fixes, so the server tags each with a `reason` and we
     show the matching screen. Anything else stays an ordinary error for the caller. */
  const OUTAGE = {
    offline: {
      h: "You're offline",
      p: "SponsorJobs writes with the real model, so it needs a connection. Reconnect and try again.",
    },
    no_account: {
      h: "Couldn't set up your free account",
      p: "Too many new sign-ups came from this network today, or the service is busy. Try again in a while.",
    },
    no_key: {
      h: "Connect your AI key",
      p: "SponsorJobs runs on your own Anthropic key. Add it once and it stays on this machine.",
      btn: "Add your key",
    },
    no_credit: {
      h: "Your Anthropic credit has run out",
      p: "SponsorJobs runs on your own key at cost, so nothing can be written until you top up. Add credit at Anthropic, then try again. Tip: turn on auto-reload so it never stops mid-run.",
      link: "https://platform.claude.com/settings/billing",
      linkLabel: "Add credits",
    },
    rate_limited: {
      h: "Too many requests",
      p: "Anthropic is rate-limiting your key right now. Wait a moment, then try again.",
    },
    busy: {
      h: "Anthropic is busy",
      p: "The model is overloaded at the moment. This usually clears in a minute.",
    },
    // Bundled (managed AI) paywall: the person used the tailored packages on their tier (Free: 3 a
    // month; a pass: its balance). Different from no_credit (a bring-your-own-key wallet); the fix
    // is a pass or their own key, so the button opens the passes panel, not the add-a-key modal.
    upgrade_required: {
      h: "You've used your tailored packages",
      p: "Free includes 3 tailored packages a month. A Job Hunt Pass or a Season Pass gives you many more, paid once with no auto-renew.",
      btn: "See passes",
    },
    service_down: {
      h: "SponsorJobs' AI is briefly unavailable",
      p: "Our AI service is having a moment. Please try again in a minute.",
    },
  };
  let lastCall = null;   // so "Try again" can actually retry the thing that failed
  let lastOutageReason = null;   // so the fix button knows which action to take

  function showOutage(reason) {
    const o = OUTAGE[reason];
    const box = $("#downModal");
    if (!o || !box) return;
    lastOutageReason = reason;
    $("#downH").textContent = o.h;
    $("#downP").textContent = o.p;
    const link = $("#downFix"), btn = $("#downFixBtn");
    link.hidden = !o.link;
    if (o.link) { link.href = o.link; link.textContent = o.linkLabel || "Fix it"; }
    btn.hidden = !o.btn;
    if (o.btn) btn.textContent = o.btn;
    // Retrying cannot fix a used-up allowance or a missing key; the button would only fail again.
    const retry = $("#downRetry");
    if (retry) retry.hidden = ["upgrade_required", "no_key", "no_credit", "no_account"].includes(reason);
    box.hidden = false;
  }
  function hideOutage() { const b = $("#downModal"); if (b) b.hidden = true; }

  async function api(url, body) {
    lastCall = { url, body };
    const r = await fetch(url, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || data.error) {
      if (data.reason === "locked") showLock();      // App Lock engaged mid-session
      // No key yet: this is the moment it is needed, so ask for it right here (just in time),
      // rather than at first launch or behind a "you have no key" screen.
      else if (data.reason === "no_key" && window.__openAiKeyBox) window.__openAiKeyBox();
      else if (data.reason) showOutage(data.reason);
      const err = new Error(data.error || ("HTTP " + r.status));
      err.reason = data.reason || "";
      err.data = data;
      throw err;
    }
    return data;
  }

  /* ---------------------------------------------------------------- theme
     Preference is system | light | dark, defaulting to SYSTEM. "system" follows the
     OS live via prefers-color-scheme; we resolve it to a concrete value and stamp
     that on <html>, so CSS only ever deals with data-theme="light" vs the default.
     (Preference splits ~50/50 and is partly an accessibility need: respect the OS,
     allow an override, persist it.) */
  const THEME_KEY = "tailor-theme";
  const darkMQ = matchMedia("(prefers-color-scheme: dark)");
  const themePref = () => localStorage.getItem(THEME_KEY) || "system";
  function applyTheme(pref) {
    const resolved = pref === "system" ? (darkMQ.matches ? "dark" : "light") : pref;
    document.documentElement.setAttribute("data-theme", resolved);
    document.querySelectorAll("[data-theme-opt]").forEach(b =>
      b.classList.toggle("is-on", b.dataset.themeOpt === pref));
  }
  function setTheme(pref) { localStorage.setItem(THEME_KEY, pref); applyTheme(pref); }
  applyTheme(themePref());
  darkMQ.addEventListener("change", () => { if (themePref() === "system") applyTheme("system"); });

  /* ------------------------------------------------- collapsible menu (icon rail) */
  const RAIL_KEY = "tailor-rail";
  const appEl = document.querySelector(".app");
  function setRail(on) {
    appEl?.classList.toggle("is-rail", on);
    const t = $("#railToggle");
    if (t) {
      t.title = on ? "Expand menu" : "Collapse menu";
      t.setAttribute("aria-label", t.title);
      t.setAttribute("aria-expanded", String(!on));
    }
    localStorage.setItem(RAIL_KEY, on ? "1" : "0");
  }
  if (window.tailorShell) document.documentElement.classList.add("in-shell");
  setRail(true);   // the rail is the only sidebar now (2026-10-09); no toggle, no expanded state

  /* ---------------------------------------------------------------- views */
  function showView(name) {
    document.querySelectorAll(".view").forEach(v => v.classList.remove("is-active"));
    $("#view-" + name).classList.add("is-active");
  }

  /* ---------------------------------------------------------------- live CV in the nav */
  // The CV you're working on lives in the sidebar, next to every other destination. It was
  // reachable only via a Continue card on the dashboard, which treated work you were doing
  // seconds ago as history to be retrieved: "I click a different tab and return and it
  // shows me a continue button, I don't think that is how it should be." A destination you
  // can click from anywhere is not a card you have to find first.
  function paintLiveNav(st) {
    const host = $("#liveNav");
    if (!host) return;
    if (!st || !(st.active || st.phase)) { host.hidden = true; host.innerHTML = ""; return; }
    const who = [st.role, st.company].filter(Boolean).join(" · ") || "Untitled Resume";
    host.hidden = false;
    host.innerHTML = `<div class="nav-sec">In progress</div>
      <button class="nav-item nav-live" id="liveNavBtn" title="${esc(who)}">
        <span class="live-dot" aria-hidden="true"></span>
        <span class="nav-label nav-live-t">${esc(who)}</span>
      </button>`;
    $("#liveNavBtn").addEventListener("click", () => resumeBuilder());
  }

  // Ask the server whether a CV is open, on load and after finishing one, so the sidebar
  // entry is right even in a tab that never started the session itself.
  async function refreshLiveNav() {
    let st = null;
    try { st = await api("/api/session/state"); } catch { return; }
    paintLiveNav(st && st.active ? st : null);
  }

  async function loadDashboard() {
    // Prompt-first Home: nothing to fetch for the hero, the prompt IS the page.
    await refreshLiveNav();   // the sidebar entry is the way back into an open CV
    loadHomeToday();
  }

  /* ---------------- Saved CVs manager: folders + rename + pointer drag --------
     Research-backed interactions (Atlassian DnD guidelines / Pencil & Paper):
     the grid never jumps while dragging - the source card dims to 40% and a
     lifted preview follows the pointer with a small offset; a thin edge line
     marks the drop slot; folder headers highlight as targets; the drop settles
     fast. Rename avoids the double-click-opens trap: pencil, F2, or the card
     menu, never a timing-sensitive click. */
  let CV_RECS = [], CV_FOLDERS = [], CV_TEMPLATES = [], CV_FOLLOWUPS = [];
  const cvSortKey = (r) => (typeof r.sort === "number" ? r.sort : -r.id);

  const cvsTplCard = (t) => `<button class="cvs-tpl" data-use="${esc(t.name)}" type="button" title="${esc(t.best_for || t.display_name)}">
      <div class="cvs-tpl-prev"><img class="cvs-tpl-img" loading="lazy" alt="" draggable="false"
        src="/api/templates/${esc(t.name)}/thumb.png" onerror="this.remove()"></div>
      <div class="cvs-tpl-name">${esc(t.display_name)}</div>
    </button>`;

  function renderCvsTemplates(list) {
    const strip = $("#cvsTplStrip");
    if (!strip) return;
    CV_TEMPLATES = list || [];
    // The strip shows the featured few (Word shows about seven); "More templates" has all.
    const featured = CV_TEMPLATES.filter(t => t.featured);
    strip.innerHTML = (featured.length ? featured : CV_TEMPLATES).map(cvsTplCard).join("");
    // Click PREVIEWS the template (look before you commit); "Use this template" in the
    // preview is the separate, explicit step that starts the JD flow.
    strip.querySelectorAll("[data-use]").forEach(b =>
      b.addEventListener("click", () => openTplPreview(b.dataset.use)));
  }

  async function loadCvs() {
    const root = $("#cvManager");
    try {
      const [r, tpl, f, fu] = await Promise.all([
        api("/api/records"), api("/api/templates"), api("/api/folders"),
        api("/api/followups").catch(() => ({ due: [] }))]);
      CV_RECS = r.records || [];
      CV_FOLDERS = f.folders || [];   // load saved folders so the organizer actually works
      CV_FOLLOWUPS = fu.due || [];
      renderCvsTemplates(tpl.templates || []);
    } catch {
      root.innerHTML = `<div class="empty">Couldn't load your applications, the SponsorJobs app may have stopped. <b>Reload</b> to retry.</div>`;
      return;
    }
    renderCvManager();
  }

  function cvGroups() {
    const groups = new Map([["", []]]);
    CV_FOLDERS.forEach(f => groups.set(f, []));
    CV_RECS.forEach(r => groups.get(groups.has(r.folder) ? r.folder : "").push(r));
    groups.forEach(list => list.sort((a, b) => cvSortKey(a) - cvSortKey(b)));
    return groups;
  }

  // The application funnel the person tracks each application through.
  const CV_STAGES = [["saved", "Saved"], ["applied", "Applied"], ["interviewing", "Interviewing"],
    ["offer", "Offer"], ["rejected", "Rejected"]];

  function cvCardHTML(c) {
    // Word "Recent" style row: doc icon, name + company/match, date on the right.
    // Drag, inline rename, and the actions menu all still work on the row.
    const stage = c.stage || (c.status === "applied" ? "applied" : "saved");
    const opts = CV_STAGES.map(([v, lbl]) =>
      `<option value="${v}"${v === stage ? " selected" : ""}>${lbl}</option>`).join("");
    const nudge = c.follow_up_is_due ? `<span class="cv-followup" title="It has been a few days, a short follow-up helps">Follow up due</span>` : "";
    const sub = [esc(c.company || ""), esc(c.template_label || ""), c.match != null ? `${c.match}% match` : ""].filter(Boolean).join("  \u00b7  ");
    return `<article class="cv-card cv-row" tabindex="0" role="button" data-id="${c.id}" title="${esc(c.role)}">
      <span class="cv-row-ic"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 1.5h5l3 3v10H4z" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M9 1.5v3h3" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M6 8.2h4M6 10.6h4" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/></svg></span>
      <div class="cv-row-main">
        <div class="cv-row-name cv-name-line">${esc(c.role)}</div>
        <div class="cv-row-sub">${sub}${nudge}</div>
      </div>
      <select class="cv-stage cv-stage-${stage}" data-id="${c.id}" title="Application status" aria-label="Application status">${opts}</select>
      <span class="cv-row-date">${esc(c.when || "")}</span>
      <div class="cv-actions">
        <button class="cv-act" data-act="rename" type="button" title="Rename (F2)">&#9998;</button>
        <button class="cv-act" data-act="menu" type="button" title="More">&#8943;</button>
      </div>
    </article>`;
  }

  // Move an application along the funnel. Optimistic: the local row updates immediately, then the
  // server confirms (and hands back the follow-up date, which changes the "due" badge on reload).
  async function setCvStage(id, stage) {
    const rec = CV_RECS.find(r => r.id === Number(id));
    try {
      const out = await api(`/api/record/${id}/stage`, { stage });
      if (rec) {
        rec.stage = out.stage; rec.status = out.status;
        rec.follow_up_due = out.follow_up_due; rec.follow_up_is_due = false;
      }
    } catch { /* re-render below repaints server truth on next load */ }
    renderCvManager();
  }

  // A gentle, honest nudge above the list: applications you applied to that are due for a check-in.
  // It never messages anyone, it just reminds you; clicking one opens that application.
  function followupBannerHTML() {
    const due = CV_FOLLOWUPS || [];
    if (!due.length || CV_QUERY) return "";
    const items = due.slice(0, 6).map(f =>
      `<button class="fu-item" data-fu="${f.id}" type="button">${esc(f.role)}${f.company ? ` <span class="fu-co">at ${esc(f.company)}</span>` : ""}</button>`).join("");
    const n = due.length;
    return `<div class="fu-banner"><div class="fu-lead"><span class="fu-dot"></span>`
      + `<b>${n} application${n > 1 ? "s" : ""}</b> ${n > 1 ? "are" : "is"} worth a quick follow-up</div>`
      + `<div class="fu-list">${items}</div></div>`;
  }

  let CV_EXPANDED = false, CV_QUERY = "";
  function renderCvManager() {
    const root = $("#cvManager");
    const more = $("#cvsMoreCvs");
    if (!CV_RECS.length) {
      // Fresh install: three faint example rows show what this list becomes, and under
      // them a plain line with the single next step, no card or headline (Kofi, 10-27; it
      // replaced the quiet line from 10-09 once the empty page read as blank). The examples are
      // hidden from screen readers and can't be clicked, so nobody mistakes them for real resumes.
      const ex = [["Data Analyst", "Stripe", 88], ["Business Intelligence Analyst", "Capital One", 81],
        ["Product Analyst", "Spotify", 76]];
      const rows = ex.map(([role, co, pct]) => `<div class="cv-card cv-row cvs-ex-row">
          <span class="cv-row-ic"><svg viewBox="0 0 16 16"><path d="M4 1.5h5l3 3v10H4z" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M9 1.5v3h3" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M6 8.2h4M6 10.6h4" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/></svg></span>
          <div class="cv-row-main"><div class="cv-row-name">${role}</div><div class="cv-row-sub">${co}  \u00b7  Classic  \u00b7  ${pct}% match</div></div>
          <span class="cvs-ex-tag">Example</span><span class="cv-row-date">Today</span>
        </div>`).join("");
      root.innerHTML = `<div class="cvs-empty-ex">
        <div class="cvs-ex-rows" aria-hidden="true">${rows}</div>
        <div class="cvs-ex-foot">
          <div class="cvs-ex-s">One per job, each matched to its description.</div>
          <button class="btn btn-primary btn-sm" id="cvsFirstTailor" type="button">Tailor your first resume</button>
        </div>
      </div>`;
      // Same path as a template click: preview the first template, then "Use this template".
      $("#cvsFirstTailor").addEventListener("click", () => {
        const t = CV_TEMPLATES.find(x => x.featured) || CV_TEMPLATES[0];
        t ? openTplPreview(t.name) : openTemplates("cvs");
      });
      if (more) more.hidden = true;
      return;
    }
    let recs = [...CV_RECS].sort((a, b) => cvSortKey(a) - cvSortKey(b));
    if (CV_QUERY) recs = recs.filter(r => (`${r.role} ${r.company || ""}`).toLowerCase().includes(CV_QUERY));
    if (!recs.length) {
      root.innerHTML = `<div class="empty">No resumes match &ldquo;${esc(CV_QUERY)}&rdquo;.</div>`;
      if (more) more.hidden = true;
      return;
    }
    const LIMIT = 10;
    const truncate = !CV_QUERY && !CV_EXPANDED && recs.length > LIMIT;
    const shown = truncate ? recs.slice(0, LIMIT) : recs;
    root.innerHTML = followupBannerHTML()
      + `<section class="cvf" data-folder=""><div class="cv-grid">${shown.map(cvCardHTML).join("")}</div></section>`;
    if (more) {
      if (!CV_QUERY && recs.length > LIMIT) {
        more.hidden = false;
        more.textContent = CV_EXPANDED ? "Show fewer" : `More Resumes (${recs.length - LIMIT}) \u2192`;
      } else more.hidden = true;
    }
    wireCvManager(root);
  }

  async function cvMeta(id, patch) {
    const rec = CV_RECS.find(r => r.id === Number(id));
    try {
      const out = await api(`/api/record/${id}/meta`, patch);
      if (rec) { rec.role = out.role; rec.folder = out.folder; rec.sort = out.sort; }
    } catch (e) { /* the re-render below repaints the server truth either way */ }
    renderCvManager();
  }

  function startRename(card) {
    const h = card.querySelector(".cv-name-line");
    if (!h || card.querySelector("input")) return;
    const old = h.textContent;
    const input = document.createElement("input");
    input.className = "fld cv-rename";
    input.value = old;
    h.replaceWith(input);
    input.focus(); input.select();
    let done = false;
    const commit = (save) => {
      if (done) return; done = true;
      const val = input.value.trim();
      if (save && val && val !== old) cvMeta(card.dataset.id, { name: val });
      else renderCvManager();
    };
    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") commit(true);
      if (e.key === "Escape") commit(false);
    });
    input.addEventListener("blur", () => commit(true));
    input.addEventListener("click", (e) => e.stopPropagation());
  }

  function openCvMenu(card, btn) {
    document.querySelector(".cv-menu")?.remove();
    const id = card.dataset.id;
    const cur = (CV_RECS.find(r => r.id === Number(id)) || {}).folder || "";
    const menu = document.createElement("div");
    menu.className = "cv-menu";
    menu.innerHTML = `
      <button data-m="rename" type="button">Rename</button>
      <div class="cv-menu-sep"></div>
      <button data-m="delete" class="danger" type="button">Delete&#8230;</button>`;
    document.body.appendChild(menu);
    const r = btn.getBoundingClientRect();
    menu.style.left = Math.min(r.left, window.innerWidth - 200) + "px";
    menu.style.top = (r.bottom + 6) + "px";
    const close = () => { menu.remove(); document.removeEventListener("click", close); };
    setTimeout(() => document.addEventListener("click", close), 0);
    menu.addEventListener("click", async (e) => {
      const b = e.target.closest("button"); if (!b) return;
      if (b.dataset.m === "rename") startRename(card);
      if (b.dataset.m === "move") cvMeta(id, { folder: b.dataset.f });
      if (b.dataset.m === "delete") {
        const rec = CV_RECS.find(x => x.id === Number(id));
        if (confirm(`Delete "${rec ? rec.role : "this resume"}"? This removes the saved resume, cover letter, and answers. It can't be undone.`)) {
          try { await api(`/api/record/${id}/delete`, {}); } catch {}
          loadCvs();
        }
      }
    });
  }

  function wireCvManager(root) {
    root.querySelectorAll(".cv-card").forEach(card => {
      card.addEventListener("click", (e) => {
        if (e.target.closest(".cv-act, input, .cv-stage")) return;
        if (card.classList.contains("was-dragged")) { card.classList.remove("was-dragged"); return; }
        openPackage(card.dataset.id);
      });
      card.addEventListener("keydown", (e) => {
        if (e.target.tagName === "INPUT") return;
        if (e.key === "F2") { e.preventDefault(); startRename(card); }
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openPackage(card.dataset.id); }
      });
    });
    root.querySelectorAll('[data-act="rename"]').forEach(b =>
      b.addEventListener("click", () => startRename(b.closest(".cv-card"))));
    root.querySelectorAll('[data-act="menu"]').forEach(b =>
      b.addEventListener("click", () => openCvMenu(b.closest(".cv-card"), b)));
    root.querySelectorAll(".cv-stage").forEach(sel => {
      sel.addEventListener("click", (e) => e.stopPropagation());   // don't open the package
      sel.addEventListener("change", (e) => setCvStage(sel.dataset.id, e.target.value));
    });
    root.querySelectorAll(".fu-item").forEach(b =>
      b.addEventListener("click", () => openPackage(b.dataset.fu)));
    wireCvDrag(root);
  }

  function startFolderRename(sec, nameEl, old) {
    if (sec.querySelector(".cvf-rename")) return;
    const input = document.createElement("input");
    input.className = "fld cvf-rename";
    input.value = old;
    nameEl.replaceWith(input);
    input.focus(); input.select();
    let done = false;
    const commit = async (save) => {
      if (done) return; done = true;
      const val = input.value.trim();
      if (save && val && val !== old) await api("/api/folders", { rename_from: old, rename_to: val });
      loadCvs();
    };
    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") commit(true);
      if (e.key === "Escape") commit(false);
    });
    input.addEventListener("blur", () => commit(true));
  }

  $("#newFolderBtn")?.addEventListener("click", () => {
    if (document.querySelector(".cvf-new")) { document.querySelector(".cvf-new input").focus(); return; }
    const wrap = document.createElement("div");
    wrap.className = "cvf-new";
    wrap.innerHTML = `<input class="fld" placeholder="New folder name, Enter to create" aria-label="New folder name" />`;
    $("#cvManager").prepend(wrap);
    const input = wrap.querySelector("input");
    input.focus();
    let done = false;
    const finish = async (save) => {
      if (done) return; done = true;
      const name = input.value.trim();
      wrap.remove();
      if (save && name) { await api("/api/folders", { add: name }); loadCvs(); }
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") finish(true);
      if (e.key === "Escape") finish(false);
    });
    input.addEventListener("blur", () => finish(true));
  });

  let TPL_ORIGIN = "cvs";
  function openTemplates(origin) {
    TPL_ORIGIN = origin || "cvs";
    const l = $("#tplBackLbl");
    if (l) l.textContent = TPL_ORIGIN === "builder" ? "Back"
                         : TPL_ORIGIN === "newcv" ? "Home" : T("nav.cvs", "Resumes");
    loadTemplates(); showView("templates");
  }
  $("#cvsMoreTpl")?.addEventListener("click", () => openTemplates("cvs"));
  $("#cvsMoreCvs")?.addEventListener("click", () => { CV_EXPANDED = !CV_EXPANDED; renderCvManager(); });
  $("#cvsSearch")?.addEventListener("input", (e) => { CV_QUERY = e.target.value.trim().toLowerCase(); renderCvManager(); });
  $("#tplBack")?.addEventListener("click", () => {
    if (TPL_ORIGIN === "builder") showView("builder");
    else if (TPL_ORIGIN === "newcv") { loadDashboard(); showView("dashboard"); }
    else { loadCvs(); showView("cvs"); }
  });

  // -------- pointer drag: GPU-composited follow, grab-offset, gap insertion line.
  function wireCvDrag(root) {
    let line = root.querySelector(".cv-drop-line");
    if (!line) { line = document.createElement("div"); line.className = "cv-drop-line"; root.appendChild(line); }
    root.querySelectorAll(".cv-card").forEach(card => {
      card.addEventListener("pointerdown", (e) => {
        if (e.button !== 0 || e.target.closest(".cv-act, input")) return;
        const rect = card.getBoundingClientRect();
        const grabDX = e.clientX - rect.left, grabDY = e.clientY - rect.top;
        const sx = e.clientX, sy = e.clientY;
        let preview = null, target = null, side = "before";
        const clearFolder = () => root.querySelectorAll(".drop-folder")
          .forEach(el => el.classList.remove("drop-folder"));
        const showLine = (el, before) => {
          const r = el.getBoundingClientRect(), rr = root.getBoundingClientRect();
          const y = (before ? r.top : r.bottom) - rr.top;
          line.style.transform = `translate3d(${r.left - rr.left}px, ${y - 1.5}px, 0)`;
          line.style.width = r.width + "px";
          line.style.height = "3px";
          line.classList.add("show");
        };
        const move = (ev) => {
          if (!preview) {
            if (Math.hypot(ev.clientX - sx, ev.clientY - sy) < 6) return;
            preview = card.cloneNode(true);
            preview.className = "cv-card cv-tile cv-drag-preview";
            preview.style.width = rect.width + "px";
            preview.style.height = rect.height + "px";
            document.body.appendChild(preview);
            card.classList.add("drag-src");
            document.body.classList.add("cv-dragging");
          }
          // translate3d = GPU compositing, no per-frame layout (the smooth-follow fix).
          preview.style.transform = `translate3d(${ev.clientX - grabDX}px, ${ev.clientY - grabDY}px, 0) scale(1.04)`;
          clearFolder(); line.classList.remove("show"); target = null;
          const els = document.elementsFromPoint(ev.clientX, ev.clientY);
          const head = els.find(el => el.classList && el.classList.contains("cvf-head"));
          const zone = els.find(el => el.classList && el.classList.contains("cvf-empty"));
          if (head) { head.classList.add("drop-folder"); return; }
          if (zone) { zone.classList.add("drop-folder"); return; }
          // Nearest card to the pointer -> the line always shows a valid, steady slot
          // (aiming for a card edge that only exists when hovered is what flickered).
          let best = null, bestD = Infinity;
          root.querySelectorAll(".cv-card").forEach(el => {
            if (el === card || el === preview) return;
            const r = el.getBoundingClientRect();
            const d = Math.abs(ev.clientY - (r.top + r.height / 2));
            if (d < bestD) { bestD = d; best = el; }
          });
          if (best) {
            const r = best.getBoundingClientRect();
            side = ev.clientY < r.top + r.height / 2 ? "before" : "after";
            target = best;
            showLine(best, side === "before");
          }
        };
        const up = (ev) => {
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", up);
          if (!preview) return;
          preview.classList.add("drop");
          const pv = preview; setTimeout(() => pv.remove(), 130);
          card.classList.remove("drag-src");
          document.body.classList.remove("cv-dragging");
          card.classList.add("was-dragged");     // swallow the click this drag ends with
          clearFolder(); line.classList.remove("show");
          const els = document.elementsFromPoint(ev.clientX, ev.clientY);
          const head = els.find(el => el.classList && el.classList.contains("cvf-head"));
          const zone = els.find(el => el.classList && el.classList.contains("cvf-empty"));
          const id = Number(card.dataset.id);
          const groups = cvGroups();
          if (head || zone) {
            const folder = (head || zone).closest(".cvf").dataset.folder;
            const list = groups.get(folder) || [];
            const end = list.length ? cvSortKey(list[list.length - 1]) + 1 : 0;
            cvMeta(id, { folder, sort: end });
            return;
          }
          if (target) {
            const folder = target.closest(".cvf").dataset.folder;
            const list = (groups.get(folder) || []).filter(r => r.id !== id);
            const idx = list.findIndex(r => r.id === Number(target.dataset.id));
            const at = side === "before" ? idx : idx + 1;
            const prev = at > 0 ? cvSortKey(list[at - 1]) : null;
            const next = at < list.length ? cvSortKey(list[at]) : null;
            const sort = prev === null && next === null ? 0
                       : prev === null ? next - 1
                       : next === null ? prev + 1
                       : (prev + next) / 2;
            cvMeta(id, { folder, sort });
          }
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
      });
    });
  }

  // The Home box: typing fades the page furniture; Enter (or the button) sends the text.
  // A pasted posting or a link starts a tailoring session, the exact JD-gate path New
  // Resume uses; anything else is a request for the assistant (agentAsk below), which
  // states a plan first. The server decides (agent/home.py); the client only guesses the
  // obvious cases so a posting never waits on a parse. Esc clears and brings the landing back.
  const looksLikeJd = (t) => /^(https?:\/\/|www\.)\S+$/i.test(t) || t.length > 1500 ||
    (t.split("\n").filter(l => l.trim()).length >= 3 && /responsibilit|requirement|qualification/i.test(t));
  (function () {
    const box = $("#homePrompt");
    if (!box) return;
    const dash = $("#view-dashboard");
    const sync = () => dash.classList.toggle("home-typing", !!box.value.trim());
    const tailor = (text) => {
      openBuilder();
      $("#jdInput").value = text;
      $("#jdStart").click();
    };
    const start = async () => {
      const text = box.value.trim();
      if (!text) { box.focus(); return; }   // the button did nothing at all on an empty box
      box.value = ""; sync();
      if (looksLikeJd(text)) { tailor(text); return; }
      openAgent();
      const r = await agentAsk(text);
      if (r && r.kind !== "agent") { tailor(text); return; }   // the server read it as a posting
    };
    // The suggestion list (a combobox listbox): it opens under the box when the box is focused
    // AND empty, and closes on typing, blur, or Escape. Arrows move the active row; Enter or a
    // click fills the box with that row's (translated) text and sends it. "See examples" focuses
    // the box and opens the same list. No example text ever sits inside the box itself.
    const list = $("#homeSuggest");
    const rows = list ? [...list.querySelectorAll(".home-suggest-row")] : [];
    let active = -1;
    const setActive = (i) => {
      active = i;
      rows.forEach((r, k) => { r.classList.toggle("is-active", k === i); r.setAttribute("aria-selected", k === i ? "true" : "false"); });
      if (i >= 0) box.setAttribute("aria-activedescendant", rows[i].id);
      else box.removeAttribute("aria-activedescendant");
    };
    const isOpen = () => !!list && !list.hidden;
    const openList = () => {
      if (!list || box.value) return;
      list.hidden = false; box.setAttribute("aria-expanded", "true"); setActive(-1);
    };
    const closeList = () => {
      if (!list) return;
      list.hidden = true; box.setAttribute("aria-expanded", "false"); setActive(-1);
    };
    const pick = (i) => { box.value = rows[i].textContent.trim(); closeList(); sync(); start(); };
    rows.forEach((r, i) => {
      r.addEventListener("mousedown", (e) => e.preventDefault());   // keep focus, so blur does not close it first
      r.addEventListener("mouseenter", () => setActive(i));
      r.addEventListener("click", () => pick(i));
    });
    box.addEventListener("focus", openList);
    box.addEventListener("blur", closeList);
    box.addEventListener("input", () => { sync(); if (box.value) closeList(); else openList(); });
    box.addEventListener("keydown", (e) => {
      if (isOpen()) {
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          const n = rows.length, d = e.key === "ArrowDown" ? 1 : -1;
          setActive(active < 0 ? (d > 0 ? 0 : n - 1) : (active + d + n) % n);
          return;
        }
        if (e.key === "Escape") { e.preventDefault(); closeList(); return; }
        if (e.key === "Enter" && !e.shiftKey && active >= 0) { e.preventDefault(); pick(active); return; }
      }
      if (e.key === "Escape") { box.value = ""; sync(); box.blur(); return; }
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); start(); }
    });
    $("#homeGo")?.addEventListener("click", start);
    $("#homeExamples")?.addEventListener("click", () => { box.focus(); openList(); });
  })();

  // (The old next-best-action hero card is gone: the prompt IS the home now.)

  // Roles you can actually take, first. This strip called itself "best-fit" and was
  // .slice(0, 4) of whatever arrived most recently, so an F-1 student in Chicago who needs
  // H-1B sponsorship was shown London, Stockholm, and Remote-Poland as their best fits.
  // Those aren't weak matches, they're impossible: no CV wins them.
  //
  // Sponsorship is the ranking signal because it's the one that makes a role reachable at
  // all, and a known sponsor is a FACT from official disclosure data (sourcing/sponsors.py),
  // not a guess. Nothing is hidden: the full feed and its filters live in Jobs. This only
  // decides which four are worth the space here.
  const US_HINT = /\b(USA?|United States|Remote\s*[-,]?\s*US|[A-Z]{2},\s*(US|USA)?|Alabama|Arizona|California|Colorado|Connecticut|Florida|Georgia|Illinois|Indiana|Massachusetts|Michigan|Minnesota|Missouri|New York|North Carolina|Ohio|Oregon|Pennsylvania|Texas|Virginia|Washington|Chicago|Boston|Seattle|Austin|Denver|Atlanta|Dallas|Houston|Miami|Philadelphia|Phoenix|San Francisco|San Jose|Los Angeles|New York City|NYC)\b/i;
  const looksUS = (j) => US_HINT.test(j.location || "");
  const eligibilityScore = (j) => {
    let s = 0;
    if ((j.visa || []).length) s += 4;      // a known sponsor, from official filings
    if (j.sponsor) s += 2;
    if (looksUS(j)) s += 3;                 // reachable on the visa the sponsor files for
    if (j.is_new) s += 1;
    return s;
  };
  const rankForReachability = (jobs) =>
    [...jobs].sort((a, b) => eligibilityScore(b) - eligibilityScore(a));

  // The job list's FIRST download. On a slow link it takes minutes (2026-10-14: ~10 MB at
  // 10 to 60 KB/s), so Home and Jobs say so calmly, with the size so far, poll a light status
  // endpoint, and reload on their own when it lands. A failure after the engine's retries says
  // so in one line with a Retry button. "No roles yet" is kept for a board that is truly empty.
  let FEED_DL_TIMER = null;
  const FEED_DL_WAITERS = new Set();
  const mbText = (b) => (b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB");
  function feedDownloadText(st) {
    const got = st && st.bytes ? mbText(st.bytes) : "";
    const of = got && st.total ? ` of ${mbText(st.total)}` : "";
    return got ? `Downloading today's jobs\u2026 ${got}${of}` : "Downloading today's jobs\u2026";
  }
  const feedFailed = (st) => !!(st && st.configured && !st.downloading && !st.has_cache && st.failing);
  function feedLoadingHtml(st) {
    return `<div class="feed-dl" role="status"><span class="feed-dl-dot" aria-hidden="true"></span>` +
      `<span data-feed-dl>${esc(feedDownloadText(st))}</span>` +
      `<span class="feed-dl-sub">This can take a few minutes on a slow connection. The list appears here on its own.</span></div>`;
  }
  function feedFailHtml() {
    return `<div class="feed-dl is-failed" role="alert"><span>Couldn't download the job list. Check your connection and try again.</span>` +
      `<button class="btn btn-ghost btn-sm" data-feed-retry type="button">Retry</button></div>`;
  }
  // Wire any Retry button inside `root`; `then` reloads the screen that owns it.
  function wireFeedRetry(root, then) {
    root.querySelectorAll("[data-feed-retry]").forEach(b => b.addEventListener("click", async () => {
      b.disabled = true;
      try { await api("/api/jobs/feed-retry", {}); } catch { /* the status poll reports it */ }
      then();
    }));
  }
  // Poll /api/jobs/feed-status every 2.5 s while the download runs, updating every progress line
  // on screen; when it ends (landed or failed) call each waiter once.
  function watchFeedDownload(onDone) {
    if (onDone) FEED_DL_WAITERS.add(onDone);
    if (FEED_DL_TIMER) return;
    const tick = async () => {
      FEED_DL_TIMER = null;
      let st = null;
      try { st = await (await fetch("/api/jobs/feed-status")).json(); } catch { /* engine busy */ }
      if (st && st.downloading) {
        document.querySelectorAll("[data-feed-dl]").forEach(n => { n.textContent = feedDownloadText(st); });
        FEED_DL_TIMER = setTimeout(tick, 2500);
        return;
      }
      if (!st) { FEED_DL_TIMER = setTimeout(tick, 5000); return; }
      const waiters = [...FEED_DL_WAITERS];
      FEED_DL_WAITERS.clear();
      waiters.forEach(fn => { try { fn(); } catch { /* one screen's reload never stops another */ } });
    };
    FEED_DL_TIMER = setTimeout(tick, 2500);
  }

  // Home: a small curated peek at the freshest roles (full feed lives in Jobs).
  async function loadHomeToday() {
    const el = $("#homeToday");
    if (!el) return;
    try {
      const r = await api("/api/jobs?home=1");   // server leaves out roles closed to a visa holder
      const foot = $(".home-roles-foot");
      if (r.loading) { if (foot) foot.hidden = true; el.innerHTML = feedLoadingHtml(r.feed); watchFeedDownload(loadHomeToday); return; }
      // The list scrolls under the fixed prompt, so it can hold more than a peek.
      const jobs = rankForReachability(r.jobs || []).slice(0, 12);
      if (foot) foot.hidden = jobs.length < 3;
      if (!jobs.length && feedFailed(r.feed)) { el.innerHTML = feedFailHtml(); wireFeedRetry(el, loadHomeToday); return; }
      if (!jobs.length) { el.innerHTML = `<div class="empty small">No roles yet, open <b>Jobs</b> to add sources.</div>`; return; }
      // The same card language as the Jobs tab (logo, framed card, company then role, badges), so
      // a job reads the same everywhere in the app. The old strip drew a bare first letter and
      // ran "Axon·Scottsdale" together with no spacing (Kofi, 2026-10-08).
      el.innerHTML = jobs.map(j => {
        const f = freshness(j);
        const visas = (j.visa || []).map(v => badgeName(v.code || v.label)).filter(Boolean);
        // The Migrate Mate card, measured against theirs side by side (Kofi, 2026-10-08):
        // a 64px white framed logo tile top-left; company, title, location beside it; a row of
        // category tags; then a facts row (salary | work mode | type) with the sponsorship chips
        // on the right. Built from the same helpers the Jobs tab uses, so the two agree.
        const facts = [j.salary && String(j.salary).trim() || "Salary TBD", workMode(j), employmentType(j)].filter(Boolean);
        const tags = roleTags(j);
        return `<button class="feed-row today-card" data-sid="${esc(j.source_id)}" type="button">
          <div class="tc-head">
            ${companyAvatar(j)}
            <div class="fr-main">
              <div class="tc-co">${esc(j.company)}</div>
              <div class="tc-title">${esc(j.title)}</div>
              <div class="tc-loc">${esc(usLocation(j.location) || "Location not listed")}</div>
            </div>
            ${f.label ? `<span class="tc-fresh">${esc(f.label)}</span>` : ""}
          </div>
          ${tags ? `<div class="tc-tags">${tags}</div>` : ""}
          <div class="tc-foot">
            <div class="tc-facts">${facts.map((x, i) => `<span class="${i === 0 ? "tc-pay" : ""}">${esc(x)}</span>`).join('<span class="tc-sep"></span>')}</div>
            <div class="tc-chips">${visaBadges(j, { primary: true })}</div>
          </div>
        </button>`;
      }).join("");
      // A card OPENS the role (Jobs, with it selected in the detail pane), where "Tailor & apply"
      // is the explicit action. It used to start tailoring on the click (2026-10-20).
      el.querySelectorAll("[data-sid]").forEach(b =>
        b.addEventListener("click", () => openJobOnBoard(jobs.find(j => j.source_id === b.dataset.sid))));
    } catch { el.innerHTML = ""; }
  }

  /* ---------------------------------------------------------------- application package */
  let PKG_ID = null;
  let PKG_APPLY_URL = "";
  let PKG_FILL_URL = "", PKG_CV_PATH = "";   // the in-app fill: this package's link + resume file

  let PKG_ORIGIN = "dashboard";
  function openPackage(id) {
    PKG_ORIGIN = document.querySelector(".view.is-active")?.id?.replace("view-", "") || "dashboard";
    const lbl = $("#pkgBackLbl");
    // The back link names the page it returns to, as the sidebar names it (the rail says Home).
    if (lbl) lbl.textContent = PKG_ORIGIN === "cvs" ? T("nav.cvs", "Resumes")
      : PKG_ORIGIN === "autoapply" ? T("nav.autoapply", "Auto-apply") : "Home";
    PKG_ID = id; showView("package"); loadPackage(id);
  }

  // The package page IS the resume preview, so the side panel never shows it a second time: the
  // panel drops its document, and closes if it was open on the resume tab (from a build earlier).
  function pkgQuietPanel() {
    if (!window.Panel) return;
    Panel.setCv("", { context: "package" });
    if (Panel.isOpen("cv")) Panel.close();
  }

  function setPkgStatus(status) {
    const applied = status === "applied";
    const s = $("#pkgStatus");
    s.className = "pkg-status " + (applied ? "applied" : "ready");
    s.textContent = applied ? "Applied" : "Ready to submit";
    $("#pkgApplied").textContent = applied ? "Mark as not applied" : "Mark as applied";
  }

  function pkgCoverage(cov) {
    const chip = (t, c) => `<span class="cov-chip ${c}">${esc(t)}</span>`;
    let html = "";
    (cov.present || []).forEach(t => html += chip(t, "on"));
    (cov.missing_supported || []).forEach(t => html += chip(t, "sup"));
    (cov.missing || []).forEach(t => html += chip(t, "off"));
    return html || `<span class="pkg-hint">No coverage detail saved for this application.</span>`;
  }

  async function loadPackage(id) {
    let p;
    try { p = await api("/api/record/" + id); }
    catch {
      // Clear every field so a failed load never shows the previously-viewed record's data.
      $("#pkgRole").textContent = "Couldn't load this application.";
      $("#pkgPdf").hidden = true; $("#pkgNoPdf").hidden = false;
      $("#pkgDownload").hidden = true;
      if ($("#pkgDownloadDocx")) $("#pkgDownloadDocx").hidden = true;
      $("#pkgCov").innerHTML = ""; $("#pkgCovNum").textContent = "";
      $("#pkgVisaCard").hidden = true; $("#pkgScrCard").hidden = true;
      $("#pkgCl").value = ""; pkgSetPdfLink(false);
      return;
    }
    $("#pkgRole").textContent = (p.role || "Untitled") + (p.company ? " · " + p.company : "");
    loadVariantCard();
    // The application link comes from the package itself (apply_url); the box only shows
    // when no link is known. Reset on every package, so one job's link never carries over.
    PKG_FILL_URL = p.apply_url || p.job_url || p.url || "";
    PKG_CV_PATH = p.cv_path || "";
    $("#pkgAssistUrl").value = PKG_FILL_URL;
    $("#pkgAssistUrl").hidden = !window.tailorShell || !!PKG_FILL_URL;
    $("#pkgAssistNote").textContent = "";
    $("#pkgFillHereBtn").hidden = true;
    document.querySelectorAll("#pkgSubmitCard .assist-learn").forEach(b => b.remove());
    setPkgStatus(p.status);

    if (p.pdf) {
      const src = p.pdf + "&v=" + Date.now() + "&preview=1#toolbar=0&navpanes=0&view=FitH";
      $("#pkgPdf").src = src; $("#pkgPdf").hidden = false; $("#pkgNoPdf").hidden = true;
      $("#pkgDownload").href = p.pdf; $("#pkgDownload").hidden = false;
      // ATS-friendly Word export of the same tailored CV, built on the fly from the record.
      const dx = $("#pkgDownloadDocx");
      if (dx && PKG_ID) { dx.href = "/api/cv.docx?rid=" + PKG_ID; dx.hidden = false; }
      pkgQuietPanel();
    } else {
      $("#pkgPdf").hidden = true; $("#pkgNoPdf").hidden = false;
      $("#pkgDownload").hidden = true;
      if ($("#pkgDownloadDocx")) $("#pkgDownloadDocx").hidden = true;
      pkgQuietPanel();
    }

    const cov = p.coverage || {};
    $("#pkgCovNum").textContent = (cov.ratio != null ? cov.ratio + "%" : "");
    $("#pkgCov").innerHTML = pkgCoverage(cov);

    const visa = p.visa || [];
    $("#pkgVisaCard").hidden = !visa.length;
    $("#pkgVisa").innerHTML = visa.map(v =>
      `<span class="vbadge vb-${esc(v.code)}" title="${esc(visaTitle(v))}">${esc(VISA_LABELS[v.code] || v.code)}</span>`).join("");

    $("#pkgCl").value = p.cover_letter || "";
    $("#pkgClMsg").textContent = "";
    renderClReview(null);                      // clear any prior record's letter check
    pkgSetPdfLink(!!(p.cover_letter || "").trim());
    // Guided apply: an application should be apply-ready. Auto-draft the cover letter the
    // first time a package is opened without one, so there's nothing extra to click.
    if (!(p.cover_letter || "").trim() && p.status !== "applied") draftCover(true);

    PKG_SCR = (p.screening || []).map(qa => ({ ...qa }));
    $("#pkgScrCard").hidden = !PKG_SCR.length;
    renderPkgScreening();

    const sub = p.submission || {};
    $("#pkgTier").className = "pkg-tier-pill " + (sub.tier || "assisted");
    $("#pkgTier").textContent = sub.label || "Assisted, you review and click submit";
    $("#pkgSubmitMsg").textContent = (sub.result && sub.result.message) || "";
    PKG_APPLY_URL = p.apply_url || "";
    $("#pkgSubmitBtn").textContent = sub.tier === "auto" ? "Auto-submit"
      : (PKG_APPLY_URL ? "Open posting & apply →" : "Submit");
    // Desktop app: ONE primary button. Assisted sites get "Fill application" (it opens the
    // application here and fills it); the auto-submit button stays only where the site's own
    // API is verified, and then Fill is the quiet secondary.
    const shellFill = !!window.tailorShell;
    $("#pkgFillBtn").hidden = !shellFill;
    $("#pkgSubmitBtn").hidden = shellFill && sub.tier !== "auto";
    $("#pkgFillBtn").className = "btn btn-sm " + (shellFill && sub.tier === "auto" ? "btn-ghost" : "btn-primary");
  }

  // Screening answers: each one is editable here. A saved edit lands on this application AND in
  // Your answers, so every later form reuses it (unless the question is about this company).
  // A knock-out question (one an ATS auto-rejects on) keeps its visible mark.
  let PKG_SCR = [];
  function renderPkgScreening() {
    $("#pkgScr").innerHTML = PKG_SCR.map((qa, i) =>
      `<div class="pkg-qa${qa.knockout ? " pkg-qa-knockout" : ""}" data-i="${i}">
         <div class="pkg-q">${esc(qa.question)}${qa.knockout
           ? ` <span class="pkg-knockout" title="Employers auto-reject on this question. Check the answer yourself.">auto-reject question, check this one</span>`
           : ""}</div>
         <div class="pkg-a">${esc(qa.answer)}</div>
         <div class="pkg-qa-acts"><button class="link-btn" type="button" data-scr="edit">${esc(I18N.t("aa.edit", "Edit"))}</button>
           <span class="pkg-hint" data-scr-note></span></div></div>`).join("");
  }
  $("#pkgScr")?.addEventListener("click", async e => {
    const b = e.target.closest("[data-scr]");
    if (!b) return;
    const box = b.closest(".pkg-qa"), i = Number(box.dataset.i), act = b.dataset.scr;
    if (act === "edit") {
      box.querySelector(".pkg-a").outerHTML = `<textarea class="fld pkg-a-edit" rows="3">${esc(PKG_SCR[i].answer || "")}</textarea>`;
      box.querySelector(".pkg-qa-acts").innerHTML = `<button class="btn btn-primary btn-sm" type="button" data-scr="save">${esc(I18N.t("ans.save", "Save"))}</button>
        <button class="btn btn-ghost btn-sm" type="button" data-scr="cancel">${esc(I18N.t("ans.cancel", "Cancel"))}</button><span class="pkg-hint" data-scr-note></span>`;
      box.querySelector("textarea").focus();
      return;
    }
    if (act === "cancel") { renderPkgScreening(); return; }
    if (act === "save" && PKG_ID) {
      const answer = box.querySelector("textarea").value.trim();
      b.disabled = true;
      try {
        const r = await api(`/api/record/${PKG_ID}/screening`, { index: i, answer });
        PKG_SCR = (r.screening || PKG_SCR);
        renderPkgScreening();
        const note = $(`#pkgScr .pkg-qa[data-i="${i}"] [data-scr-note]`);
        if (note) pkgFlash(note, r.reused ? I18N.t("ans.reused", "Saved. Later forms reuse it.") : I18N.t("ans.savedHere", "Saved for this application."), 3500);
      } catch (err) {
        b.disabled = false;
        const note = box.querySelector("[data-scr-note]");
        if (note) note.textContent = err.message || I18N.t("profile.answers.failed", "Couldn't save.");
      }
    }
  });
  $("#pkgAnsOpen")?.addEventListener("click", () => openAnswers("package"));

  function pkgFlash(elm, msg, ms = 2500) {
    elm.textContent = msg;
    setTimeout(() => { if (elm.textContent === msg) elm.textContent = ""; }, ms);
  }

  // Show the "PDF ↗" link when a cover letter exists; cache-bust so an edited letter
  // re-renders. The server builds the matching PDF on demand.
  function pkgSetPdfLink(show) {
    const a = $("#pkgClPdf"), dx = $("#pkgClDocx");
    if (show && PKG_ID) {
      a.href = `/api/record/${PKG_ID}/cover_letter.pdf?v=${Date.now()}`; a.hidden = false;
      if (dx) { dx.href = `/api/record/${PKG_ID}/cover_letter.docx?v=${Date.now()}`; dx.hidden = false; }
    } else { a.hidden = true; if (dx) dx.hidden = true; }
  }

  $("#pkgBack").addEventListener("click", () => {
    if (PKG_ORIGIN === "cvs") { loadCvs(); showView("cvs"); }
    else if (PKG_ORIGIN === "autoapply") { loadAutoApply(); showView("autoapply"); }
    else { loadDashboard(); showView("dashboard"); }
  });
  $("#pkgExport").addEventListener("click", () => { if (PKG_ID) window.location = `/api/record/${PKG_ID}/export`; });

  async function loadVariantCard() {
    try {
      const [tpl, prof] = await Promise.all([api("/api/templates"), api("/api/profile")]);
      const sel = $("#pkgVarTpl");
      sel.innerHTML = (tpl.templates || []).map(x =>
        `<option value="${esc(x.name)}">${esc(x.display_name || x.name)}</option>`).join("");
      const orgs = [];
      (prof.experience || []).forEach(e => { const o = (e.org || "").trim(); if (o && !orgs.includes(o)) orgs.push(o); });
      $("#pkgVarRoles").innerHTML = orgs.map(o =>
        `<label class="pe-dec"><input type="checkbox" data-var-org="${esc(o)}" checked> ${esc(o)}</label>`).join("")
        || `<span class="prof-dim">No saved roles yet.</span>`;
    } catch { $("#pkgVarNote").textContent = "Couldn't load templates or roles."; }
  }

  $("#pkgVarBtn")?.addEventListener("click", async () => {
    const note = $("#pkgVarNote");
    const template = $("#pkgVarTpl").value;
    const exclude = [...document.querySelectorAll("[data-var-org]")]
      .filter(cb => !cb.checked).map(cb => cb.dataset.varOrg);
    const btn = $("#pkgVarBtn"); btn.disabled = true;
    note.textContent = "Building the variant…";
    try {
      showView("builder"); $("#jdGate").hidden = true;
      await withWorking(async () => { applyState(await api(`/api/record/${PKG_ID}/variant`, { template, exclude_orgs: exclude })); });
      note.textContent = "";
    } catch (err) {
      note.textContent = err.message || "Couldn't build the variant.";
      showView("package");
    } finally {
      btn.disabled = false;
    }
  });

  // ------------------------------------------------ the ONE apply path (desktop app)
  // Fill application on the package page and on the Auto-apply page (row and drawer)
  // all go here: open the application in the in-app browser and run the best fill (site recipe,
  // then the AI plan, then the resume upload). The shell asks the engine before it touches the
  // site's submit button: only a site on the verified auto-submit list is ever clicked
  // (submit.policy.apply_decision). Everywhere else it stops, and the submit button is scrolled
  // into view and outlined for the person.
  async function applyInPane(opts) {
    if (!window.tailorShell || !window.tailorShell.assistApply) return null;
    try { return await window.tailorShell.assistApply(opts); } catch { return null; }
  }

  // One plain sentence for whatever the apply path reported. Same words on every page.
  function applyMessage(r) {
    if (!r || r.ok === false) {
      const why = r && r.reason;
      if (why === "linkedin" || why === "fill-by-hand") {
        return "This site doesn't allow automated filling. It's open in the Browser tab, apply there yourself.";
      }
      if (why === "blocked") {
        return r.blocker === "captcha"
          ? "This page has a captcha. Solve it, then click Fill this page again."
          : "This site wants you to sign in first. Sign in in the Browser tab, then click Fill this page again.";
      }
      if (why === "no-page") return "Nothing is open in the Browser tab yet.";
      if (why === "not-a-form") {
        // The job page, not the application: nothing was typed (a search box is never filled).
        const btn = r.apply ? `\u201c${r.apply}\u201d` : "Apply";
        return `This is the job page, not the application. Click ${btn} on the page first, then click Fill this page again.`;
      }
      if (why === "no-fields") return "No form on this page yet. Open the application form, then click Fill this page again.";
      if (why === "bad-url") return "This application has no link. Paste the application link first.";
      return "Couldn't fill this page. It's open in the Browser tab, fill it by hand.";
    }
    if (r.submitted) {
      return r.confirmed ? "Submitted, and the site confirmed it."
        : "Submitted. The page showed no confirmation, so check the Browser tab once.";
    }
    const bits = [];
    if (r.filled) bits.push(r.filled + " field" + (r.filled === 1 ? "" : "s"));
    if (r.resume_attached) bits.push("resume attached");
    let msg = bits.length ? "Filled " + bits.join(", ") + "." : "Nothing to fill on this page.";
    if (r.reason === "captcha") msg += " This form has a captcha, so the submit is yours.";
    else if (r.reason === "login") msg += " The site wants a sign-in. Sign in, then click Fill this page again.";
    else if (r.reason === "submit-covered") {
      msg += " A banner" + (r.covered_by ? " (" + r.covered_by + ")" : "") + " covers the submit button. Close it first.";
    }
    if (r.next_page) msg += " Click Next on the site when ready, the next page fills itself.";
    if (r.pending_upload && !r.resume_attached) msg += " Attach your resume PDF yourself.";
    if (r.left && r.left.length) {
      msg += " Left for you: " + r.left.slice(0, 3).join(", ") + (r.left.length > 3 ? ", and " + (r.left.length - 3) + " more." : ".");
    }
    msg += r.submit_highlighted
      ? " Check it and click Submit on the site, it's outlined in orange."
      : " Check it and click Submit on the site.";
    return msg;
  }

  async function pkgFill(here) {
    if (!window.tailorShell || !PKG_ID) return;
    const note = $("#pkgAssistNote");
    const url = (PKG_FILL_URL || $("#pkgAssistUrl").value || "").trim();
    if (!here && !/^https?:\/\//i.test(url)) {
      $("#pkgAssistUrl").hidden = false; $("#pkgAssistUrl").focus();
      note.textContent = "Paste the application link first.";
      return;
    }
    const btn = here ? $("#pkgFillHereBtn") : $("#pkgFillBtn");
    btn.disabled = true;
    note.textContent = "Filling…";
    const r = await applyInPane({ url, recordId: PKG_ID, resumePath: PKG_CV_PATH, submit: false, here });
    btn.disabled = false;
    note.textContent = applyMessage(r);
    if (r && r.ok !== false) { $("#pkgFillHereBtn").hidden = false; offerLearn(note); }
  }
  $("#pkgFillBtn")?.addEventListener("click", () => pkgFill(false));
  $("#pkgFillHereBtn")?.addEventListener("click", () => pkgFill(true));
  $("#pkgAssistUrl")?.addEventListener("input", e => { PKG_FILL_URL = e.target.value.trim(); });

  // Learn from corrections: after a fill, a one-click way to save what the person changed on
  // the form into their profile (only the fields SponsorJobs itself filled are watched).
  function offerLearn(note) {
    if (!window.tailorShell || !window.tailorShell.assistLearn || !note) return;
    const old = note.parentElement && note.parentElement.querySelector(".assist-learn");
    if (old) old.remove();
    const b = document.createElement("button");
    b.type = "button"; b.className = "btn btn-ghost btn-sm assist-learn";
    b.textContent = "Save what I changed to my profile";
    b.addEventListener("click", async () => {
      b.disabled = true;
      const r = await window.tailorShell.assistLearn();
      b.disabled = false;
      if (!r || r.ok === false) { note.textContent = "Couldn't save your corrections."; return; }
      note.textContent = r.learned && r.learned.length
        ? "Saved to your profile: " + r.learned.join(", ") + "."
        : "No corrections to save yet. Change a field SponsorJobs filled, then try again.";
      if (r.learned && r.learned.length) loadProfile().catch(() => {});
    });
    note.insertAdjacentElement("afterend", b);
  }

  // Page-by-page fill progress from the desktop shell, including the pages it fills by itself
  // after the person presses Next on the site: "Page 2 filled: 9 fields. 2 left for you."
  if (window.tailorShell && window.tailorShell.onAutoApplyProgress) {
    window.tailorShell.onAutoApplyProgress((info) => {
      if (!info || !info.message) return;
      if (info.agent) {
        aaAgentBar(info);
        if (info.state === "ready" || info.state === "submitted" || info.state === "stopped") {
          if ($("#view-autoapply")?.classList.contains("is-active")) loadAutoApply();
        }
        return;
      }
      const bar = $("#bbFill");
      if (bar) {
        let text = info.message;
        if (!info.wall && info.left && info.left.length) {
          text += " " + info.left.slice(0, 4).join(", ") + (info.left.length > 4 ? ", and more." : ".");
        }
        bar.textContent = text;
        bar.classList.toggle("is-wall", !!info.wall);
        bar.hidden = false;
      }
      const note = $("#pkgAssistNote");
      if (note && PKG_ID && String(info.record_id) === String(PKG_ID) && (info.page > 1 || info.wall)) {
        note.textContent = info.message;
      }
    });
  }

  // The engine saw the site's "application received" page after the person submitted.
  if (window.tailorShell && window.tailorShell.onApplied) {
    window.tailorShell.onApplied((info) => {
      const note = $("#pkgAssistNote");
      if (note) {
        note.textContent = info && info.matched
          ? "The site confirmed your application. Marked as applied."
          : "The site confirmed your application. No saved package matched this page, so mark it applied yourself if you built one.";
      }
      if (info && info.matched && $("#view-autoapply")?.classList.contains("is-active")) loadAutoApply();
    });
  }

  $("#pkgSubmitBtn").addEventListener("click", async () => {
    if (!PKG_ID) return;
    const btn = $("#pkgSubmitBtn"); btn.disabled = true;
    try {
      const r = await api(`/api/record/${PKG_ID}/submit`, {});   // routes by the per-site policy
      if (r.status === "auto_submitted") {
        setPkgStatus("applied");
        $("#pkgSubmitMsg").textContent = r.message || "Auto-submitted.";
      } else {
        // Assisted: the CV + cover letter are prepared, open the posting so the person
        // clicks Apply on the site (LinkedIn etc.), where the extension can autofill.
        // Only ever open a real web posting, never a javascript:/data: scheme from a feed.
        if (/^https?:\/\//i.test(PKG_APPLY_URL || "")) window.open(PKG_APPLY_URL, "_blank", "noopener");
        $("#pkgSubmitMsg").textContent = PKG_APPLY_URL
          ? "Opened the posting, click Apply there. Your resume and cover letter are ready to attach."
          : (r.message || "Marked for your on-site submit.");
      }
    } catch { $("#pkgSubmitMsg").textContent = "Couldn't reach the app to submit."; }
    btn.disabled = false;
  });

  $("#pkgApplied").addEventListener("click", async () => {
    if (!PKG_ID) return;
    const applied = $("#pkgStatus").classList.contains("applied");
    try {
      const r = await api(`/api/record/${PKG_ID}/status`, { status: applied ? "ready" : "applied" });
      setPkgStatus(r.status);
    } catch { pkgFlash($("#pkgMsg"), "Couldn't update status."); }
  });

  $("#pkgDelete").addEventListener("click", async () => {
    if (!PKG_ID) return;
    const role = ($("#pkgRole").textContent || "this application").trim();
    if (!confirm(`Delete “${role}”? This removes the saved resume, cover letter, and answers for it. This can't be undone.`)) return;
    try {
      await api(`/api/record/${PKG_ID}/delete`, {});
      await loadDashboard();
      showView("dashboard");
    } catch { pkgFlash($("#pkgMsg"), "Couldn't delete."); }
  });

  $("#pkgClCopy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("#pkgCl").value); pkgFlash($("#pkgClMsg"), "Copied."); }
    catch { pkgFlash($("#pkgClMsg"), "Select the text to copy."); }
  });

  $("#pkgClSave").addEventListener("click", async () => {
    if (!PKG_ID) return;
    try {
      const r = await api(`/api/record/${PKG_ID}/cover_letter`, { text: $("#pkgCl").value });
      pkgSetPdfLink(!!($("#pkgCl").value || "").trim());
      renderClReview(r.review, r.skills_used);
      pkgFlash($("#pkgClMsg"), r.pdf ? "Saved · PDF updated." : "Saved.");
    } catch { pkgFlash($("#pkgClMsg"), "Couldn't save."); }
  });

  // Honest craft check on the cover letter (hook, names the company, length, no unbacked claim),
  // plus the real experiences it leans on, mirroring the resume's pre-send review.
  const CL_VERDICT = { ready: ["Ready to send", "good"], review: ["Worth a tweak", "mid"],
    check: ["Check before sending", "low"] };
  function renderClReview(r, skillsUsed) {
    const el = $("#pkgClReview");
    if (!el) return;
    if (!r || !r.checks) { el.hidden = true; el.innerHTML = ""; return; }
    const v = CL_VERDICT[r.verdict] || ["Letter check", "mid"];
    const rows = (r.checks || []).map(c => {
      const items = (c.items || []).length
        ? `<div class="ps-items">${c.items.map(t => `<span class="ps-term ps-${esc(c.level)}">${esc(t)}</span>`).join("")}</div>`
        : "";
      const ic = c.level === "flag" ? "!" : c.level === "warn" ? "~" : c.level === "pass" ? "✓" : "i";
      return `<div class="ps-row ps-lvl-${esc(c.level)}"><span class="ps-ic">${ic}</span>`
        + `<div class="ps-body"><div class="ps-label">${esc(c.label)}</div>${items}</div></div>`;
    }).join("");
    const grounds = (skillsUsed || []).slice(0, 12);
    const draws = grounds.length
      ? `<div class="cl-draws"><span class="cl-draws-lbl">Draws on:</span> ${grounds.map(s => `<span class="fd-gap">${esc(s)}</span>`).join("")}</div>`
      : "";
    el.hidden = false;
    el.innerHTML = `<div class="ps-head ps-${esc(v[1])}"><span class="ps-verdict">${esc(v[0])}</span>`
      + `<span class="ps-headline">${esc(r.headline || "")}</span></div>${rows}${draws}`;
  }

  let coverDrafting = false;
  async function draftCover(auto) {
    const id = PKG_ID;
    if (!id || coverDrafting) return;
    coverDrafting = true;
    const btn = $("#pkgClGen"), label = btn.textContent;
    btn.disabled = true; btn.textContent = "Drafting…";
    $("#pkgClMsg").textContent = auto ? "Drafting your cover letter from your profile…"
                                      : "Writing a cover letter from your profile…";
    try {
      const r = await api(`/api/record/${id}/cover_letter`, {});
      if (PKG_ID !== id) return;                       // navigated away mid-draft
      $("#pkgCl").value = r.cover_letter || "";
      pkgSetPdfLink(!!(r.cover_letter || "").trim());
      renderClReview(r.review, r.skills_used);
      pkgFlash($("#pkgClMsg"), auto ? "Cover letter ready, review and edit." : "Drafted, review and edit.", 4000);
    } catch {
      if (PKG_ID === id) pkgFlash($("#pkgClMsg"), "Couldn't draft, the app needs your API key + internet.", 5000);
    } finally { btn.disabled = false; btn.textContent = label; coverDrafting = false; }
  }
  $("#pkgClGen").addEventListener("click", () => draftCover(false));

  /* ---------------------------------------------------------------- browser extension */
  // The extension is how we help on LinkedIn and Indeed, which forbid bots (§7). It ships
  // with the app, but Chrome cannot be made to install an unpacked extension from an
  // installer (its own security rule), so there is a manual step until the Web Store
  // listing exists. That makes it the APP's job to notice it is missing and say so:
  // otherwise the person browses LinkedIn, sees no badges, and concludes the product is
  // broken. Shown on Jobs, because that is where they are about to go looking.
  async function paintExtension() {
    const el = $("#extCard");
    if (!el) return;
    let s;
    try { s = await api("/api/extension/status"); } catch { el.hidden = true; return; }
    if (!s || s.connected) { el.hidden = true; return; }   // installed: say nothing
    el.hidden = false;
    el.innerHTML = `<div class="ext-in">
        <div class="ext-txt">
          <div class="ext-h">Get sponsor badges on LinkedIn and Indeed</div>
          <div class="ext-s">Those sites don't allow bots, so SponsorJobs helps from inside your
            browser instead. The extension ships with the app, Chrome just needs you to point
            at it once.</div>
        </div>
        <button class="btn btn-primary" id="extHow">How</button>
      </div>`;
    $("#extHow").addEventListener("click", () => {
      // The exact steps, with the real path. "Browse to your install folder" is where a
      // non-technical person gives up, so we hand them the path to copy.
      const folder = s.folder || "the extension folder next to the app";
      el.innerHTML = `<div class="ext-steps">
          <div class="ext-h">Three steps, once</div>
          <ol>
            <li>Open Chrome and go to <b>chrome://extensions</b></li>
            <li>Turn on <b>Developer mode</b>, top right</li>
            <li>Click <b>Load unpacked</b> and choose this folder:</li>
          </ol>
          <code class="ext-path">${esc(folder)}</code>
          <div class="ext-actions">
            <button class="btn btn-ghost btn-sm" id="extCopy">Copy folder path</button>
            <button class="btn btn-ghost btn-sm" id="extHide">Not now</button>
          </div>
        </div>`;
      $("#extCopy").addEventListener("click", async (e) => {
        try { await navigator.clipboard.writeText(folder); e.target.textContent = "Copied"; }
        catch { e.target.textContent = "Select the path above and copy it"; }
      });
      $("#extHide").addEventListener("click", () => { el.hidden = true; });
    });
  }

  /* ---------------------------------------------------------------- watchlist / sourced jobs */
  let LAST_JOBS = [];
  const VISA_SET = new Set();   // multi-select sponsorship codes; "SPONSORED" = has any visa badge
  let JF_LEVEL = "";            // "" | "entry"
  let JF_DATE = 0;             // max posting age in days (0 = any time)
  let JF_SEARCH = "", JF_LOC = "", JF_REMOTE = "";   // live client-side filters (title/company, location, work type)
  let JF_PAY = 0;              // minimum annual pay in dollars (0 = any)
  let JF_SORT = "";            // "" (most recent) | "pay" (top paid)
  let JF_SAVED = false;        // when true, show only the user's bookmarked roles
  let JF_SPONSORED = false;    // when true, show only roles whose posting STATES visa sponsorship

  // The badge's own honest basis (from sourcing/sponsors.py) is the tooltip when present,
  // so hovering any badge explains it's a historical signal, not a guarantee. Falls back
  // to label+detail for older payloads.
  function visaTitle(v) {
    // The honest caveat lives here, on the badge itself, instead of a board-level disclaimer:
    // hovering any badge shows its basis and that it's a signal, not a guarantee.
    const base = v.basis || (v.label + (v.detail ? " · " + v.detail : ""));
    return base + " · A signal from public government filings, not a guarantee.";
  }

  // EVERY role shows a sponsorship status (froghire/Glassdoor pattern), so the row is consistent
  // and honest: coloured badges when the employer has a public sponsorship record, else a neutral
  // "No sponsorship record" (we advertise US roles to international students and label each one's
  // sponsorship status rather than hiding the ones we can't vouch for).
  // What a badge CODE is called on screen: the VISA, the way students search for it and the
  // way the best US board (Migrate Mate) labels it: Green Card, STEM OPT, H-1B. The government
  // record (PERM filings, E-Verify enrollment) is the SOURCE, named in the hover, not the label
  // (decided with Kofi, 2026-10-08, after reading Migrate Mate's cards).
  const BADGE_NAME = { "H-1B": "H-1B", "GREEN-CARD": "Green Card", "STEM-OPT": "STEM OPT",
    "CAP-EXEMPT": "Cap-exempt", "E-3": "E-3", "H-1B1": "H-1B1", "TN": "TN" };
  const badgeName = (code) => BADGE_NAME[code] || code;
  function visaBadges(j, opts) {
    const v = j.visa || [];
    // Card mode (Migrate Mate's convention): the ad's stance, then ONE visa chip and "+N" for
    // the rest, so the card stays clean; the detail shows every chip.
    const compact = !!(opts && opts.primary);
    // What the AD says, read from its own text (sourcing/adstance.py), quoted on hover so the
    // student sees the proof. Surveyed on 1,500 live ads (2026-10-08): ads that sponsor say
    // "Visa sponsorship is available" and almost never name the visa, so the chip uses the
    // ad's words; and a third of ads that mention sponsorship say they will NOT, which is the
    // most useful fact on the page for an international student, so it gets the louder chip.
    // Four stances: offered, not_offered, restricted (the ad requires US citizenship, a green
    // card, or a security clearance: closed to a visa holder whatever the badges say), unknown.
    const quote = j.ad_sentence ? ` The ad says: \u201c${esc(j.ad_sentence)}\u201d` : "";
    const stated = j.ad_stance === "offered"
      ? `<span class="vbadge vb-stated" title="This job ad says visa sponsorship is available for the role (for a US new hire that usually means H-1B, but the ad does not name the visa).${quote}">Sponsorship available</span>`
      : j.ad_stance === "not_offered"
        ? `<span class="vbadge vb-no" title="This job ad says it will not sponsor a visa for the role.${quote}">No sponsorship</span>`
        : j.ad_stance === "restricted"
          ? `<span class="vbadge vb-no vb-restricted" title="This job ad requires US citizenship, permanent residency, or a security clearance, so it is closed to a visa holder.${quote}">Citizenship or clearance required</span>`
          : "";
    if (!v.length) {
      return stated || `<span class="vbadge vb-none" title="We have no public government record that this employer has sponsored a work visa, and the ad does not say. They still might, it's worth asking.">No sponsorship record</span>`;
    }
    const chips = v.map(x =>
      `<span class="vbadge vb-spon vb-${esc(x.code)}" title="${esc(visaTitle(x))}">${esc(badgeName(x.code))}</span>`);
    if (compact && chips.length > 1) {
      const rest = v.slice(1).map(x => badgeName(x.code)).join(", ");
      return stated + chips[0] + `<span class="vbadge vb-more" title="${esc(rest)}">+${chips.length - 1}</span>`;
    }
    return stated + chips.join("");
  }

  // Nationality-based visas an H-1B sponsor can usually also support (E-3/H-1B1/TN). Shown
  // muted and clearly labelled, because they're a propensity signal gated on the candidate's
  // nationality, not an approval-backed badge. Rendered only in the detail, to avoid clutter.
  function natVisaBadges(j) {
    const nv = j.nationality_visas || [];
    if (!nv.length) return "";
    return `<span class="nat-visa-lead">By nationality:</span>` + nv.map(v =>
      `<span class="vbadge vb-nat" title="${esc(v.basis || v.label)}">${esc(v.code)}</span>`
    ).join("");
  }

  // ---- job-card enrichers -------------------------------------------------------------
  // Every company gets a real logo via our own /api/logo proxy, with a stable brand-coloured
  // MONOGRAM (same name -> same hue) painted underneath as the instant, always-there fallback
  // for any logo that doesn't resolve. The proxy means the browser talks only to our server.
  function hashHue(s) {
    let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h % 360;
  }
  function initials(name) {
    const words = (name || "").replace(/[^A-Za-z0-9 ]/g, " ").split(/\s+/).filter(Boolean);
    if (!words.length) return "?";
    return (words[0][0] + (words[1] ? words[1][0] : "")).toUpperCase();
  }
  function companyAvatar(j) {
    const co = j.company || "?";
    const hue = hashHue(co.toLowerCase());
    const mono = `<span class="av-mono" style="background:hsl(${hue} 62% 90%);color:hsl(${hue} 55% 32%)">${esc(initials(co))}</span>`;
    // The proxy 404s on a miss, so onerror cleanly reveals the monogram underneath.
    const img = `<img class="av-img" src="/api/logo?company=${encodeURIComponent(co)}" alt=""`
      + ` loading="lazy" onload="this.style.opacity=1" onerror="this.remove()" />`;
    return `<div class="fr-logo">${mono}${img}</div>`;
  }

  // Aggregator credit (docs/feed.md, "Aggregator terms"). Remotive and Remote OK let us share
  // their listings on condition that each one names them and links back to the listing, Remote OK
  // with a FOLLOW link, so the detail link carries rel="noopener" only, never "nofollow". Company
  // boards need no credit. The card is a <button>, so it names the source as plain text (a nested
  // link would be invalid markup) and the detail view carries the link.
  const VIA_SOURCES = {
    remotive: { name: "Remotive", url: "https://remotive.com" },
    remoteok: { name: "Remote OK", url: "https://remoteok.com" },
  };
  function viaSource(j) { return VIA_SOURCES[String(j.source || "").toLowerCase()] || null; }
  function viaText(a) { return I18N.t("job.via", "via {source}").replace("{source}", a.name); }
  function viaFact(j) {
    const a = viaSource(j);
    return a ? `<span class="fr-fact fr-via">${esc(viaText(a))}</span>` : "";
  }
  function viaLink(j) {
    const a = viaSource(j);
    if (!a) return "";
    return `<div class="fd-via"><a href="${esc(j.url || a.url)}" target="_blank" rel="noopener">${esc(viaText(a))}</a></div>`;
  }

  // Category chips like the competitor's ("Data Engineering", "Healthcare"), derived from the
  // title so the row says what KIND of role it is at a glance. Heuristic, capped at 2, and
  // silent when nothing matches (never a wrong guess dressed as fact).
  const ROLE_CATS = [
    [/\b(data (scien|eng|analyst)|machine learning|\bml\b|\bai\b|analytics)\b/i, "Data & AI"],
    [/\b(software|developer|engineer|programmer|full[- ]?stack|backend|frontend|devops|sre|platform|cloud)\b/i, "Engineering"],
    [/\b(product manager|product owner|\bpm\b|product ops)\b/i, "Product"],
    [/\b(design|ux|ui|research)\b/i, "Design & Research"],
    [/\b(nurse|clinical|health|medical|physician|therapist|dietitian|pharmac)\b/i, "Healthcare"],
    [/\b(teacher|faculty|professor|lecturer|instruct|education|tutor)\b/i, "Education"],
    [/\b(account|finance|financ|audit|tax|controller|fp&a)\b/i, "Finance & Accounting"],
    [/\b(sales|account executive|business development|\bbdr\b|\bsdr\b)\b/i, "Sales"],
    [/\b(marketing|growth|content|seo|brand|social media)\b/i, "Marketing"],
    [/\b(operations|logistics|supply chain|program manager|project manager)\b/i, "Operations"],
    [/\b(legal|attorney|counsel|paralegal|compliance)\b/i, "Legal & Compliance"],
    [/\b(customer|support|success|service)\b/i, "Customer"],
    [/\b(security|infosec|cyber)\b/i, "Security"],
    [/\b(hr|people|recruit|talent)\b/i, "People & HR"],
  ];
  // The feed carries ranked function tags read from the whole ad (sourcing/jobtags.py). Two show
  // on the card and the rest fold into a "+N" that lists them on hover, like Migrate Mate; the
  // title-only buckets below are the fallback for rows built before tags existed.
  function roleTags(j, max) {
    let tags = Array.isArray(j.tags) ? j.tags.filter(Boolean) : [];
    if (!tags.length) {
      const t = j.title || "";
      for (const [re, label] of ROLE_CATS) { if (re.test(t)) tags.push(label); if (tags.length >= 2) break; }
    }
    const show = max || 2;
    const head = tags.slice(0, show).map(c => `<span class="fr-tag">${esc(c)}</span>`).join("");
    const rest = tags.slice(show);
    return head + (rest.length ? `<span class="fr-tag fr-tag-more" title="${esc(rest.join(", "))}">+${rest.length}</span>` : "");
  }

  // Employment type from the title (Internship / Contract / Part-time), defaulting to Full-time
  // like the competitor does. It's a coarse but useful signal students filter on.
  function employmentType(j) {
    const t = (j.title || "").toLowerCase();
    if (/\b(intern|internship|co-?op|summer 20\d\d|new grad|graduate program)\b/.test(t)) return "Internship / New-grad";
    if (/\b(contract|contractor|temp|temporary|seasonal|freelance)\b/.test(t)) return "Contract";
    if (/\bpart[- ]?time\b/.test(t)) return "Part-time";
    return "Full-time";
  }

  // Salary if a feed carried one (Adzuna/JSearch), else the honest "Salary TBD" the
  // competitor shows on the (many) postings that don't publish pay.
  function salaryLabel(j) { return (j.salary || "").trim() || "Salary TBD"; }

  // Work-mode label: On-site / Remote / Hybrid, title-cased for the meta row.
  function workMode(j) {
    const r = (j.remote || "").toLowerCase();
    return r === "remote" ? "Remote" : r === "hybrid" ? "Hybrid" : r === "onsite" ? "On-site" : "";
  }

  // Every visa the row can be filtered by: the approval-backed badges PLUS the by-nationality
  // ones (E-3/H-1B1/TN), so a filter chip for "TN" catches roles where TN is a nationality
  // option even if it isn't a standalone badge.
  function jobVisaCodes(j) {
    return [...(j.visa || []), ...(j.nationality_visas || [])].map(v => v.code);
  }

  // This is a US board, so when a posting lists several locations (e.g. "London; New York, NY;
  // San Francisco, CA"), show the US one, not whichever came first. Strips a trailing
  // ", United States" for brevity. Falls back to the original string if nothing looks US.
  // One place, one spelling, on every card: the US part of a multi-place listing, without the
  // country the feeds tack on in a dozen spellings ("Redmond, WA,US, US", "San Jose, California,
  // United States of America", "USA - Remote", "Remote-USA"). A listing that is only the country
  // reads "United States"; only "Remote" plus the country reads "Remote (US)".
  const US_WORD = "(?:united states(?: of america)?|u\\.s\\.a\\.?|u\\.s\\.?|usa|us)";
  const US_TAIL = new RegExp("(?:^|[\\s,;|/\u2013\u2014-]+)" + US_WORD + "\\s*$", "i");
  const US_HEAD = new RegExp("^" + US_WORD + "\\s*(?:[,;|/:\u2013\u2014-]\\s*)+", "i");
  function usLocation(loc) {
    const s = (loc || "").trim();
    if (!s) return "Not listed";
    const parts = s.split(";").map(p => p.trim()).filter(Boolean);
    const us = parts.find(p => /\b(united states|usa|u\.s\.?)\b/i.test(p))
      || parts.find(p => /,\s*[A-Z]{2}\b/.test(p) && !/\b(UK|BC|ON|QC)\b/.test(p));
    let t = (us || parts[0] || s).trim(), prev;
    do { prev = t; t = t.replace(US_TAIL, "").trim(); } while (t && t !== prev);
    t = t.replace(US_HEAD, "").replace(/^[\s,;|/\u2013\u2014-]+|[\s,;|/\u2013\u2014-]+$/g, "").trim();
    if (!t) return "United States";
    if (/^remote$/i.test(t)) return "Remote (US)";
    return t.replace(/\s*,\s*/g, ", ");
  }

  let SELECTED_SID = null;
  // A role opened from elsewhere (a Home card): Jobs shows it selected, at the top of the list when
  // it is not on the current page, until the person picks another role or changes the search.
  let JOB_FOCUS = null;
  function openJobOnBoard(job) {
    if (!job) return;
    JOB_FOCUS = job;
    SELECTED_SID = job.source_id;
    navTo("jobs");
  }

  // Freshness from the EMPLOYER'S real posting time, like LinkedIn/Indeed. < 24h = new / early
  // applicant (72% of offers go to first-5-day applicants), < 48h = still early. Honest per-job
  // time -- never our own crawl time, which is identical for every row in a batch and so lies.
  function fromEpoch(ms) {
    const s = (Date.now() - ms) / 1000;
    if (s < 0) return { label: "", cls: "" };
    const label = s < 86400 ? "New " + relAgo(s) : relAgo(s);   // green "New" for the first day
    const cls = s < 86400 ? "hot" : (s < 172800 ? "warm" : "");
    return cls ? { label, cls, flag: "Early applicant" } : { label, cls: "" };
  }
  function freshness(j) {
    const posted = j.posted_at || "";
    // 1) A precise posting timestamp (carries a clock) -> the employer's real per-job age. This is
    //    what the big boards show, and what makes "New 11m ago" honest and DIFFERENT per job.
    const pt = tsUTC(posted);
    if (pt != null) return fromEpoch(pt);
    // 2) A date-only posting -> calendar label (today / yesterday / Aug 10). No fake hour count.
    if (/^\d{4}-\d{2}-\d{2}$/.test(posted)) {
      const [y, m, d] = posted.split("-").map(Number);
      const days = daysSince(y, m, d);
      if (days <= 0) return { label: ago(posted), cls: "hot", flag: "Early applicant" };
      if (days === 1) return { label: ago(posted), cls: "warm", flag: "Early applicant" };
      return { label: ago(posted), cls: "" };
    }
    // 3) No usable posting time at all -> fall back to when WE surfaced it (first_seen). Last resort,
    //    because it's a batch time; only used when the source gave us no posting time to begin with.
    const seen = tsUTC(j.first_seen);
    if (seen != null) return fromEpoch(seen);
    return { label: "", cls: "" };
  }

  // Filtering + pagination are SERVER-side now (see feedParams/loadPage). The client only holds
  // the current page. This keeps a single source of truth for filtering and lets the board browse
  // tens of thousands without shipping them all to the client.

  // Human labels for the active-filter chips + the sponsorship checkbox list.
  const VISA_LABELS = { SPONSORED: "Sponsors a visa", "H-1B": "H-1B", "GREEN-CARD": "Green Card",
    "STEM-OPT": "STEM OPT", "E-3": "E-3", "H-1B1": "H-1B1", "TN": "TN" };
  const DATE_LABELS = { 1: "Past 24 hours", 7: "Past week", 30: "Past month" };
  const REMOTE_LABELS = { remote: "Remote", hybrid: "Hybrid", onsite: "On-site" };

  // Show every applied filter as a removable chip (LinkedIn/Glassdoor pattern), so the person
  // always sees what's narrowing the list and can clear any one, or all, in a click.
  function renderActiveFilters() {
    const box = $("#activeFilters");
    if (!box) return;
    const chips = [];
    if (JF_DATE) chips.push({ k: "date", label: DATE_LABELS[JF_DATE] });
    if (JF_REMOTE) chips.push({ k: "remote", label: REMOTE_LABELS[JF_REMOTE] || JF_REMOTE });
    if (JF_LEVEL === "entry") chips.push({ k: "level", label: "Entry-level" });
    if (JF_SPONSORED) chips.push({ k: "sponsored", label: "Ad offers sponsorship" });
    [...VISA_SET].forEach(v => chips.push({ k: "visa:" + v, label: VISA_LABELS[v] || v }));
    if (JF_PAY) chips.push({ k: "pay", label: "$" + Math.round(JF_PAY / 1000) + "k+ pay" });
    if (JF_LOC) chips.push({ k: "loc", label: `"${JF_LOC}"` });
    if (!chips.length) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    box.innerHTML = chips.map(c =>
      `<button class="af-chip" data-clear="${esc(c.k)}" type="button">${esc(c.label)} <span class="af-x">×</span></button>`
    ).join("") + `<button class="af-clear" data-clear="all" type="button">Clear all</button>`;
    box.querySelectorAll("[data-clear]").forEach(b =>
      b.addEventListener("click", () => clearFilter(b.dataset.clear)));
  }

  // Reset one filter (or all) and re-sync the matching control, then repaint.
  function clearFilter(key) {
    if (key === "all") {
      VISA_SET.clear(); JF_LEVEL = ""; JF_DATE = 0; JF_REMOTE = ""; JF_LOC = ""; JF_PAY = 0; JF_SAVED = false; JF_SPONSORED = false;
    } else if (key === "date") { JF_DATE = 0; }
    else if (key === "remote") { JF_REMOTE = ""; }
    else if (key === "level") { JF_LEVEL = ""; }
    else if (key === "sponsored") { JF_SPONSORED = false; }
    else if (key === "loc") { JF_LOC = ""; }
    else if (key === "pay") { JF_PAY = 0; }
    else if (key.startsWith("visa:")) { VISA_SET.delete(key.slice(5)); }
    syncFilterControls();
    loadPage(1);
  }

  // Push the current filter state back onto the inputs (after a chip removal / clear-all).
  function syncFilterControls() {
    const dd = $("#jfDate"); if (dd) dd.value = String(JF_DATE);
    const rm = $("#jfRemote"); if (rm) rm.value = JF_REMOTE;
    const pay = $("#jfPay"); if (pay) pay.value = String(JF_PAY);
    const sort = $("#jfSort"); if (sort) sort.value = JF_SORT;
    const loc = $("#jfLocation"); if (loc) loc.value = JF_LOC;
    const entry = $("#jfEntry"); if (entry) entry.setAttribute("aria-pressed", JF_LEVEL === "entry" ? "true" : "false");
    const spon = $("#jfSponsored"); if (spon) spon.setAttribute("aria-pressed", JF_SPONSORED ? "true" : "false");
    const saved = $("#jfSaved"); if (saved) saved.setAttribute("aria-pressed", JF_SAVED ? "true" : "false");
    document.querySelectorAll("#jfVisaDD input[data-visa]").forEach(cb => { cb.checked = VISA_SET.has(cb.dataset.visa); });
    updateVisaSummary();
  }

  // The sponsorship dropdown label reflects how many options are selected.
  function updateVisaSummary() {
    const s = $("#jfVisaSummary");
    if (s) s.textContent = VISA_SET.size ? `Sponsorship (${VISA_SET.size})` : "Sponsorship";
  }


  let JOBS_PAGE = 1;
  let FEED_TOTAL = 0;       // filtered total the server reports (drives the pager + count)
  let FEED_ALLTOTAL = 0;    // unfiltered board size (the big "N live roles" number)
  const JOBS_PER_PAGE = 30; // page size requested from the server
  // A windowed run of page numbers around the current page, so a long list doesn't spray 40 buttons.
  function pageWindow(cur, pages) {
    let s = Math.max(1, cur - 3); const e = Math.min(pages, s + 6); s = Math.max(1, e - 6);
    const w = []; for (let i = s; i <= e; i++) w.push(i); return w;
  }
  // Server-driven pager: pages computed from the server's filtered total, not the loaded page.
  function pagerHtml() {
    const total = FEED_TOTAL;
    const pages = Math.max(1, Math.ceil(total / JOBS_PER_PAGE));
    const start = (JOBS_PAGE - 1) * JOBS_PER_PAGE;
    const shown = LAST_JOBS.length;
    if (pages <= 1) return `<div class="feed-pager"><span class="fp-count">${total} role${total === 1 ? "" : "s"}</span></div>`;
    const nums = pageWindow(JOBS_PAGE, pages).map(n => n === JOBS_PAGE
      ? `<span class="fp-num is-cur">${n}</span>`
      : `<button class="fp-num" data-page="${n}" type="button">${n}</button>`).join("");
    return `<div class="feed-pager">
      <span class="fp-count">${start + 1} to ${start + shown} of ${total.toLocaleString()}</span>
      <div class="fp-nav">
        <button class="fp-btn" data-page="${JOBS_PAGE - 1}" ${JOBS_PAGE <= 1 ? "disabled" : ""} type="button">Prev</button>
        ${nums}
        <button class="fp-btn" data-page="${JOBS_PAGE + 1}" ${JOBS_PAGE >= pages ? "disabled" : ""} type="button">Next</button>
      </div>
    </div>`;
  }
  // Any filter beyond the Saved toggle (search, location, facets): the Saved list's empty state
  // says "nothing saved yet" only when nothing else is narrowing it.
  function jfNarrowed() {
    return !!(JF_SEARCH || JF_LOC || JF_REMOTE || JF_LEVEL || JF_DATE || JF_PAY || JF_SPONSORED || VISA_SET.size);
  }
  // Render the current SERVER page (LAST_JOBS already IS the page; filtering + slicing happen
  // server-side now, so the board can browse tens of thousands with a small payload each time).
  function paintFeed() {
    const jobs = LAST_JOBS;
    const list = $("#feedList");
    if (!list) return;
    if (JOB_FOCUS && !jobs.some(j => j.source_id === JOB_FOCUS.source_id)) jobs.unshift(JOB_FOCUS);
    renderActiveFilters();
    renderNote();
    // No roles (no results, an empty Saved list): no detail pane to fill, so the message gets
    // the full width instead of sitting beside a large empty "pick a role" box.
    $("#feed")?.classList.toggle("is-empty", !jobs.length);
    if (!jobs.length) {
      list.innerHTML = FEED_DL ? feedLoadingHtml(FEED_DL)
        : FEED_FAIL ? feedFailHtml()
        : FEED_LOADED && JF_SAVED && !jfNarrowed()
        ? `<div class="empty small">No saved jobs yet. Press Save on a role to keep it here.</div>`
        : FEED_LOADED
        ? `<div class="empty small">No roles match your filters. <button class="link-btn" data-clear="all" type="button">Clear filters</button></div>`
        : jobSkeleton();
      wireFeedRetry(list, () => { FEED_FAIL = false; FEED_DL = {}; paintFeed(); loadPage(JOBS_PAGE); });
      $("#feedDetail").innerHTML = "";
      list.querySelector("[data-clear]")?.addEventListener("click", () => clearFilter("all"));
      return;
    }
    list.innerHTML = jobs.map(j => {
      const f = freshness(j);
      const isNew = j.is_new ? `<span class="badge-new">new</span>` : "";
      const wm = workMode(j);
      const tags = roleTags(j);
      const visas = visaBadges(j);
      return `<button class="feed-row${j.source_id === SELECTED_SID ? " is-active" : ""}${j.saved ? " is-saved" : ""}${j.excluded ? " is-excluded" : ""}" data-sid="${esc(j.source_id)}" type="button">
        ${j.saved ? `<span class="fr-saved" title="Saved" aria-label="Saved">★</span>` : ""}
        ${companyAvatar(j)}
        <div class="fr-main">
          <div class="fr-top">
            <div class="fr-co-name">${esc(j.company)}</div>
            ${f.label ? `<span class="fr-fresh ${f.cls}">${f.cls ? "● " : ""}${esc(f.label)}</span>` : ""}
          </div>
          <div class="fr-role">${esc(j.title)} ${isNew}</div>
          <div class="fr-loc">${esc(usLocation(j.location))}</div>
          <div class="fr-tags">${tags}</div>
          <div class="fr-meta">
            <span class="fr-fact">${esc(salaryLabel(j))}</span>
            <span class="fr-fact">${esc(wm || "On-site")}</span>
            <span class="fr-fact">${esc(employmentType(j))}</span>
            ${j.entry_level ? `<span class="fr-fact fr-fact-entry">Entry-level</span>` : ""}
            ${viaFact(j)}
            ${j.excluded ? `<span class="fr-fact fr-fact-never" title="${esc(I18N.t("jobs.neverTip", "On your Never apply to list. Auto-apply skips it."))}">${esc(I18N.t("jobs.never", "Never apply"))}</span>`
              : j.priority ? `<span class="fr-fact">${esc(I18N.t("aa.priority", "Priority"))}</span>` : ""}
          </div>
          <div class="fr-visas">${visas}</div>
        </div>
      </button>`;
    }).join("") + pagerHtml();
    list.querySelectorAll("[data-sid]").forEach(b =>
      b.addEventListener("click", () => { if (JOB_FOCUS && b.dataset.sid !== JOB_FOCUS.source_id) JOB_FOCUS = null; selectJob(b.dataset.sid); }));
    list.querySelectorAll(".feed-pager [data-page]").forEach(b =>
      b.addEventListener("click", () => {
        const p = Number(b.dataset.page);
        const pages = Math.max(1, Math.ceil(FEED_TOTAL / JOBS_PER_PAGE));
        if (p >= 1 && p <= pages) { loadPage(p); list.scrollTop = 0; }
      }));
    const want = JOB_FOCUS ? JOB_FOCUS.source_id : SELECTED_SID;
    const stillThere = jobs.some(j => j.source_id === want);
    selectJob(stillThere ? want : jobs[0].source_id);
    if (JOB_FOCUS) list.querySelector(".feed-row.is-active")?.scrollIntoView({ block: "nearest" });
  }

  function wireDetailStick(det) {
    if (det.dataset.stickWired) return;
    det.dataset.stickWired = "1";
    det.addEventListener("scroll", () => {
      const head = det.querySelector(".fd-head"), bar = det.querySelector("#fdStick");
      if (!head || !bar) return;
      bar.classList.toggle("is-on", det.scrollTop > head.offsetTop + head.offsetHeight - 8);
    }, { passive: true });
  }

  function selectJob(sid) {
    SELECTED_SID = sid;
    const j = LAST_JOBS.find(x => x.source_id === sid);
    const list = $("#feedList");
    if (list) list.querySelectorAll(".feed-row").forEach(r => r.classList.toggle("is-active", r.dataset.sid === sid));
    const det = $("#feedDetail");
    if (!det || !j) return;
    wireDetailStick(det);
    det.scrollTop = 0;
    const f = freshness(j);
    const wm = workMode(j);
    const tags = roleTags(j, 9);   // the detail shows them all
    det.innerHTML = `
      <div class="fd-stick" id="fdStick"><div class="fd-stick-in">
        ${companyAvatar(j)}
        <div class="fd-stick-main">
          <div class="fd-stick-t">${esc(j.title)}</div>
          <div class="fd-stick-c">${esc(j.company)} <span>·</span> ${esc(usLocation(j.location))}</div>
        </div>
        <button class="btn btn-primary btn-sm" data-fd-tailor="${esc(j.source_id)}" type="button">Tailor &amp; apply →</button>
      </div></div>
      <div class="fd-head">
        <div class="fd-headrow">
          ${companyAvatar(j)}
          <div class="fd-headmain">
            <div class="fd-title">${esc(j.title)}</div>
            <div class="fd-co">${esc(j.company)} <span>·</span> ${esc(usLocation(j.location))}</div>
            ${viaLink(j)}
          </div>
        </div>
        ${tags ? `<div class="fr-tags fd-tags">${tags}</div>` : ""}
        <div class="fd-facts">
          <span class="fr-fact">${esc(salaryLabel(j))}</span>
          ${wm ? `<span class="fr-fact">${esc(wm)}</span>` : ""}
          <span class="fr-fact">${esc(employmentType(j))}</span>
          ${j.entry_level ? `<span class="fr-fact fr-fact-entry">Entry-level</span>` : ""}
        </div>
        <div class="fd-payins" id="fdPayInsight" hidden></div>
        ${j.ad_stance === "offered" ? `<div class="fd-adnote">The ad says: \u201c${esc(j.ad_sentence || "visa sponsorship is available")}\u201d For a US new hire that usually means H-1B; the ad does not name the visa. The badges below are the employer's government record.</div>` : ""}
        ${j.ad_stance === "not_offered" ? `<div class="fd-adnote fd-adnote-no">The ad says: \u201c${esc(j.ad_sentence || "no visa sponsorship")}\u201d This role is not open to someone who needs a visa.</div>` : ""}
        ${j.ad_stance === "restricted" ? `<div class="fd-adnote fd-adnote-no">The ad says: \u201c${esc(j.ad_sentence || "US citizenship or a security clearance is required")}\u201d This role requires US citizenship, permanent residency, or a security clearance, so it is closed to a visa holder whatever the employer's record says.</div>` : ""}
        <div class="fd-meta">${f.label ? `<span class="fr-fresh ${f.cls}">${f.cls ? "● " : ""}${esc(f.label)}</span>` : ""}${visaBadges(j)}</div>
        ${(j.nationality_visas || []).length ? `<div class="fd-natvisa">${natVisaBadges(j)}</div>` : ""}
      </div>
      <div class="fd-match" id="fdMatch"></div>
      <div class="fd-actions">
        <button class="btn btn-primary" data-fd-tailor="${esc(j.source_id)}" type="button">Tailor &amp; apply →</button>
        <a class="btn btn-ghost" href="${esc(j.url)}" target="_blank" rel="noopener">View posting ↗</a>
        <button class="btn btn-ghost fd-save ${j.saved ? "is-saved" : ""}" data-fd-save="${esc(j.source_id)}" type="button" aria-pressed="${j.saved ? "true" : "false"}">${j.saved ? "★ Saved" : "☆ Save"}</button>
        <button class="btn btn-ghost" data-fd-dismiss="${esc(j.source_id)}" type="button">Dismiss</button>
      </div>
      <div class="fd-jd" id="fdJd"><span class="muted">Loading the full description…</span></div>
      <div class="fd-company" id="fdCompany"></div>`;
    det.querySelectorAll("[data-fd-tailor]").forEach(b => b.addEventListener("click", e => tailorJob(e.currentTarget.dataset.fdTailor)));
    det.querySelector("[data-fd-save]")?.addEventListener("click", e => toggleSave(e.currentTarget, j));
    det.querySelector("[data-fd-dismiss]")?.addEventListener("click", async e => {
      const s = e.currentTarget.dataset.fdDismiss;
      await api("/api/jobs/dismiss", { source_id: s });
      SELECTED_SID = null;
      loadPage(JOBS_PAGE);              // re-fetch the page so counts + backfill stay correct
      skipWhy({ source_id: s, title: j.title, company: j.company, location: j.location, salary: j.salary });
    });
    loadJobDetail(sid);
  }

  // Bookmark / un-bookmark. Optimistic: flip the button now, persist in the background. We send the
  // job snapshot so a kitchen-sourced role can be saved even though it isn't in the local DB.
  async function toggleSave(btn, j) {
    const now = btn.getAttribute("aria-pressed") !== "true";
    btn.setAttribute("aria-pressed", now ? "true" : "false");
    btn.classList.toggle("is-saved", now);
    btn.textContent = now ? "★ Saved" : "☆ Save";
    if (j) j.saved = now;
    // keep the matching card's bookmark in sync without a full reload (source_ids contain ':',
    // which breaks a CSS attribute selector, so match by iterating).
    const card = [...document.querySelectorAll("#feedList .feed-row")].find(r => r.dataset.sid === j.source_id);
    card?.classList.toggle("is-saved", now);
    try {
      await api("/api/jobs/save", { source_id: j.source_id, saved: now,
        job: now ? { source_id: j.source_id, source: j.source, company: j.company, title: j.title,
          location: j.location, remote: j.remote, url: j.url, posted_at: j.posted_at,
          salary: j.salary } : null });
      if (!now && JF_SAVED) loadPage(JOBS_PAGE);   // un-saved inside the Saved view -> drop it out
    } catch (e) {                                   // revert on failure
      const back = !now;
      btn.setAttribute("aria-pressed", back ? "true" : "false");
      btn.classList.toggle("is-saved", back);
      btn.textContent = back ? "★ Saved" : "☆ Save";
      if (j) j.saved = back;
      card?.classList.toggle("is-saved", back);
      showMsg("Couldn't save that just now, try again.");
    }
  }

  // Lazily fetch the full JD + pre-tailor match for the selected role (list stays light).
  const DETAIL_CACHE = {};
  // A profile change alters the pre-tailor "match" (computed server-side from the current
  // profile), so drop every cached detail and refresh whatever role is open right now.
  function invalidateJobDetail() {
    for (const k in DETAIL_CACHE) delete DETAIL_CACHE[k];
    if (SELECTED_SID) loadJobDetail(SELECTED_SID);
  }
  async function loadJobDetail(sid) {
    let data = DETAIL_CACHE[sid];
    if (!data) {
      try { data = await api("/api/jobs/detail?source_id=" + encodeURIComponent(sid)); DETAIL_CACHE[sid] = data; }
      catch { if (SELECTED_SID === sid) renderJd(""); return; }
    }
    if (SELECTED_SID !== sid) return;   // the person clicked another role while we fetched
    renderMatch(data.match, data.job);
    renderJd(data.jd, data.match, data.terms);
    // Some sources (Adzuna) only license a short PREVIEW of the posting, so the JD ends abruptly.
    // Be honest about it and point to the full text rather than showing a broken mid-word cutoff.
    if (data.jd_preview) {
      const el = $("#fdJd");
      const j = LAST_JOBS.find(x => x.source_id === SELECTED_SID) || {};
      if (el) el.insertAdjacentHTML("beforeend",
        `<div class="jd-preview-note">This source shares only a preview of this posting. `
        + `${j.url ? `<a href="${esc(j.url)}" target="_blank" rel="noopener">Open the full description ↗</a>` : "Open the posting for the full description."}</div>`);
    }
    renderCompany(data.job);
    // The list row often has no pay (feeds rarely publish it); the detail fetch can recover a
    // figure from the posting body, so reflect it in the header instead of leaving "Salary TBD".
    const sal = ((data.job || {}).salary || "").trim();
    if (sal) { const f = $("#feedDetail .fd-facts .fr-fact"); if (f) f.textContent = sal; }
    renderPayInsight(data.pay_insight);
  }

  // A LinkedIn-Premium-style pay read, from our OWN board: how this role's stated pay compares to
  // the median for its role family here. Honest and inspectable (shows the median + sample size),
  // and shown ONLY when the role states pay and its family has enough priced roles to compare.
  function renderPayInsight(ins) {
    const el = $("#fdPayInsight");
    if (!el) return;
    if (!ins || !ins.family || !ins.median) { el.hidden = true; el.innerHTML = ""; return; }
    const money = "$" + Math.round(ins.median / 1000) + "k";
    const word = { above: "above", around: "around", below: "below" }[ins.verdict] || "around";
    const cls = ins.verdict === "above" ? "pi-above" : ins.verdict === "below" ? "pi-below" : "pi-around";
    el.hidden = false;
    el.innerHTML = `<span class="pi-dot ${cls}"></span>${word === "around" ? "Around" : word[0].toUpperCase() + word.slice(1)} the typical `
      + `<b>${money}</b> for ${esc(ins.family)} roles here <span class="pi-n">(${ins.count.toLocaleString()} with pay listed)</span>`;
  }

  // "About the company" strip, our honest, local answer to LinkedIn's company panel. We can't
  // (and won't fake) their social feed, so instead we lead with the fact an international student
  // actually needs: this employer's public visa-sponsorship track record, plus industry and where
  // they file from. All from the USCIS/DOL data we already hold; shown only when we matched them.
  function renderCompany(job) {
    const el = $("#fdCompany");
    if (!el) return;
    const s = (job || {}).sponsor;
    if (!s) {
      // No totals record, but the year-by-year history (ten years) or the LCA wages may still
      // know this employer; the depth loader adds the heading only when something comes back.
      el.innerHTML = `<div class="fd-depth" id="fdDepth"></div>`;
      if ((job || {}).company) loadEmployerDepth(job, job.company);
      return;
    }
    const name = s.matched_name || (job || {}).company || "this employer";
    const facts = [];
    if (s.industry) facts.push(esc(s.industry));
    if (s.state) facts.push("Files from " + esc(s.state));
    const track = [];
    if (s.h1b_approvals > 0) track.push(`<b>${s.h1b_approvals.toLocaleString()}</b> approved H-1B petition${s.h1b_approvals === 1 ? "" : "s"}${s.fy_range ? " (" + esc(s.fy_range) + ")" : ""}`);
    if (s.perm_certs > 0) track.push(`<b>${s.perm_certs.toLocaleString()}</b> certified green-card (PERM) case${s.perm_certs === 1 ? "" : "s"}`);
    if (s.e_verify) track.push("enrolled in <b>E-Verify</b> (required for a STEM-OPT hire)");
    const line = track.length
      ? `Public U.S. filings show ${name} with ${listJoin(track)}. A track record from government data, not a promise this role sponsors.`
      : `We have no public sponsorship record for ${name} yet. They still might sponsor, it's worth asking.`;
    el.innerHTML = `
      <div class="fd-company-h">About ${esc(name)}</div>
      ${facts.length ? `<div class="fd-company-meta">${facts.join(" · ")}</div>` : ""}
      <p class="fd-company-track">${line}</p>
      <div class="fd-depth" id="fdDepth"></div>`;
    loadEmployerDepth(job, name);
  }

  // The per-employer depth behind the badge (the H1BGrader / Ellis layer, from the same public
  // data, all local): H-1B approvals AND denials by fiscal year with PERM alongside, the three
  // flags, what this employer paid for this title at this worksite (DOL LCA filings), and an
  // offer check that puts a salary against those filings and the weighted lottery. Fetched after
  // the strip renders so a slow lookup never holds the job detail; stale answers are dropped.
  const empT = (k, f) => (window.I18N ? I18N.t(k, f) : f);
  const empMoney = (x) => "$" + Math.round(Number(x || 0)).toLocaleString();
  const empK = (x) => "$" + Math.round(Number(x || 0) / 1000) + "k";
  const ROMAN = { 1: "I", 2: "II", 3: "III", 4: "IV" };
  async function loadEmployerDepth(job, name) {
    const sid = (job || {}).source_id;
    const company = (job || {}).company || name;
    if (!company) return;
    const enc = encodeURIComponent(company);
    const q = "?title=" + encodeURIComponent((job || {}).title || "") + "&location=" + encodeURIComponent((job || {}).location || "");
    let hist = null, wages = null;
    try {
      [hist, wages] = await Promise.all([
        api("/api/sponsors/" + enc + "/history"),
        api("/api/sponsors/" + enc + "/wages" + q),
      ]);
    } catch { return; }
    if (SELECTED_SID !== sid) return;
    const el = $("#fdDepth");
    if (!el) return;
    const known = (hist && hist.matched) || (wages && wages.found);
    const strip = el.closest("#fdCompany");
    if (!known && strip && !strip.querySelector(".fd-company-h")) { el.innerHTML = ""; return; }
    const head = (strip && !strip.querySelector(".fd-company-h"))
      ? `<div class="fd-company-h">About ${esc((hist && hist.matched_name) || company)}</div>` : "";
    el.innerHTML = head + renderFlags(hist) + renderHistory(hist) + renderWages(wages, job) + renderOfferBox(job, wages);
    wireOfferCheck(el, job);
  }

  function renderFlags(h) {
    const f = (h || {}).flags || {};
    const chips = [];
    if (f.h1b_dependent) chips.push(`<span class="fd-flag fd-flag-warn" title="${esc(empT("emp.depTip", "The employer attested on its DOL LCA filings that it is H-1B dependent: H-1B workers are 15% or more of its US workforce. It then owes extra recruitment and non-displacement attestations for some hires. A fact from the filings, not a judgement."))}">${esc(empT("emp.dependent", "H-1B dependent"))}</span>`);
    if (f.willful_violator) chips.push(`<span class="fd-flag fd-flag-bad" title="${esc(empT("emp.wvTip", "The employer attested on its DOL LCA filings that it was previously found to be a willful violator of H-1B wage or working-condition rules. From the filings themselves."))}">${esc(empT("emp.violator", "Willful violator"))}</span>`);
    if (f.cap_exempt) chips.push(`<span class="fd-flag" title="${esc(empT("emp.capTip", "Guessed from the employer's name and industry (higher education, research, teaching hospitals skip the H-1B lottery). A heuristic, not verified."))}">${esc(empT("emp.capExempt", "Cap-exempt (likely)"))}</span>`);
    return chips.length ? `<div class="fd-flags">${chips.join("")}</div>` : "";
  }

  // Year-by-year bars, plain CSS (no chart library): one column per fiscal year, approvals in
  // the good hue with denials stacked on top in the flag hue, PERM as a small text line below.
  // Each column carries the exact numbers in its title; the footer gives the 5-year read.
  function renderHistory(h) {
    const years = ((h || {}).years || []).filter(y => y.approvals || y.denials || y.perm);
    if (!years.length) return "";
    const peak = Math.max(1, ...years.map(y => y.approvals + y.denials));
    const cols = years.map(y => {
      const total = y.approvals + y.denials;
      const hA = Math.round(100 * y.approvals / peak), hD = Math.max(total ? 1 : 0, Math.round(100 * y.denials / peak));
      const tip = `FY${y.fy}: ${y.approvals.toLocaleString()} approved (${y.initial_approvals.toLocaleString()} new, ${y.continuing_approvals.toLocaleString()} continuing), ${y.denials.toLocaleString()} denied` + (y.perm ? `, ${y.perm.toLocaleString()} PERM certified` : "");
      return `<div class="fd-bar" title="${esc(tip)}">
        <div class="fd-bar-col">${y.denials ? `<span class="fd-bar-den" style="height:${hD}%"></span>` : ""}${y.approvals ? `<span class="fd-bar-app" style="height:${hA}%"></span>` : ""}</div>
        <div class="fd-bar-lbl">${String(y.fy).slice(2)}</div>
        ${y.perm ? `<div class="fd-bar-perm">${y.perm.toLocaleString()}</div>` : `<div class="fd-bar-perm"></div>`}
      </div>`;
    }).join("");
    const rate = h.denial_rate_5y == null ? "" : `, ${(h.denial_rate_5y * 100).toFixed(h.denial_rate_5y < 0.1 ? 1 : 0)}% ${empT("emp.denied", "denied")}`;
    const anyPerm = years.some(y => y.perm);
    return `<div class="fd-hist">
      <div class="fd-hist-h">${esc(empT("emp.history", "H-1B petitions by fiscal year"))} <span class="fd-hist-sub">${esc(h.fy_span || "")}</span></div>
      <div class="fd-bars">${cols}</div>
      <div class="fd-hist-foot">
        <span class="fd-key"><i class="fd-key-app"></i>${esc(empT("emp.approved", "approved"))}</span>
        <span class="fd-key"><i class="fd-key-den"></i>${esc(empT("emp.denied", "denied"))}</span>
        ${anyPerm ? `<span class="fd-key">${esc(empT("emp.permRow", "number under a year: PERM green-card certifications"))}</span>` : ""}
        <span class="fd-hist-5y">${esc(empT("emp.last5", "Last 5 years"))}: <b>${(h.approvals_5y || 0).toLocaleString()}</b> ${esc(empT("emp.approved", "approved"))}${rate}</span>
      </div>
    </div>`;
  }

  function renderWages(res, job) {
    const w = (res || {}).wages;
    if (!w) return "";
    const where = w.city ? `${w.city}, ${w.state}` : (w.state ? w.state : "");
    const lvl = w.top_level ? `, ${empT("emp.mostly", "mostly Level")} ${ROMAN[w.top_level]}` : "";
    const fys = (w.fiscal_years || []);
    const span = fys.length ? (fys.length === 1 ? `FY${fys[0]}` : `FY${fys[0]} to ${fys[fys.length - 1]}`) : "";
    return `<div class="fd-wages">
      <span class="fd-wages-line">${esc(empT("emp.paid", "This employer paid"))} <b>${empK(w.wage_min)} to ${empK(w.wage_max)}</b> (${esc(empT("emp.median", "median"))} <b>${empK(w.wage_median)}</b>) ${esc(empT("emp.for", "for"))} ${esc(w.title_label)}${where ? " in " + esc(where) : ""}, ${w.n.toLocaleString()} ${esc(empT("emp.filings", "filings"))}${lvl}.</span>
      <span class="fd-wages-src">${esc(w.scope_note || "")} ${esc(empT("emp.lcaSrc", "DOL LCA filings"))}${span ? ", " + esc(span) : ""}.</span>
    </div>`;
  }

  function renderOfferBox(job, wages) {
    const j = job || {};
    return `<details class="fd-offer">
      <summary>${esc(empT("emp.offerCheck", "Offer check"))} <span class="fd-offer-sub">${esc(empT("emp.offerSub", "put a salary against these filings and the H-1B lottery"))}</span></summary>
      <form class="fd-offer-form" data-offer-form>
        <input class="fld" name="offer" inputmode="numeric" placeholder="${esc(empT("emp.offerPh", "Offer salary, e.g. 125000"))}" required />
        <input class="fld" name="title" value="${esc(j.title || "")}" placeholder="${esc(empT("emp.titlePh", "Job title"))}" />
        <input class="fld fd-offer-short" name="city" value="${esc(((wages || {}).city) || "")}" placeholder="${esc(empT("emp.cityPh", "City"))}" />
        <input class="fld fd-offer-tiny" name="state" value="${esc(((wages || {}).state) || "")}" placeholder="${esc(empT("emp.statePh", "ST"))}" maxlength="2" />
        <button class="btn btn-ghost btn-sm" type="submit">${esc(empT("emp.offerBtn", "Check my offer"))}</button>
      </form>
      <div class="fd-offer-out" data-offer-out></div>
    </details>`;
  }

  function wireOfferCheck(root, job) {
    const form = root.querySelector("[data-offer-form]");
    const out = root.querySelector("[data-offer-out]");
    if (!form || !out) return;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const body = { company: (job || {}).company || "", offer: fd.get("offer"), title: fd.get("title"),
        city: fd.get("city"), state: fd.get("state"), location: (job || {}).location || "" };
      out.innerHTML = `<span class="muted">${esc(empT("emp.checking", "Checking against public filings…"))}</span>`;
      let r;
      try { r = await api("/api/sponsors/offer-check", body); }
      catch (err) { out.innerHTML = `<span class="fd-offer-err">${esc((err && err.message) || empT("emp.offerErr", "Couldn't check that offer."))}</span>`; return; }
      out.innerHTML = renderOfferResult(r);
    });
  }

  function renderOfferResult(r) {
    if (!r) return "";
    if (r.error) return `<span class="fd-offer-err">${esc(r.error)}</span>`;
    const w = r.wages;
    const pos = { below_range: empT("emp.belowRange", "below every filing on record"), below_median: empT("emp.belowMedian", "below the median"),
      above_median: empT("emp.aboveMedian", "above the median"), above_range: empT("emp.aboveRange", "above every filing on record") }[r.position] || "";
    const head = w
      ? `<div class="fd-offer-head"><b>${empMoney(r.offer)}</b> ${empT("emp.is", "is")} ${esc(pos)} (${empK(w.wage_min)} to ${empK(w.wage_max)}, ${esc(empT("emp.median", "median"))} ${empK(w.wage_median)}, ${w.n.toLocaleString()} ${esc(empT("emp.filings", "filings"))}).</div>`
      : `<div class="fd-offer-head"><b>${empMoney(r.offer)}</b>: ${esc(empT("emp.noWages", "no LCA wage filings matched this employer and title."))}</div>`;
    const lvl = r.likely_level
      ? `<div class="fd-offer-level"><span class="fd-offer-pill">${esc(empT("emp.likelyLevel", "Likely Level"))} ${ROMAN[r.likely_level]}</span>${r.odds ? ` <span class="fd-offer-odds">${esc(empT("emp.odds", "about"))} <b>${r.odds.percent}%</b> ${esc(empT("emp.oddsTail", "lottery odds at this level"))} (${r.odds.entries} ${esc(empT("emp.entries", "entries"))})</span>` : ""}<div class="fd-offer-basis">${esc(r.level_basis || "")}</div></div>`
      : r.level_ceiling && r.odds
        ? `<div class="fd-offer-level"><span class="fd-offer-pill">${esc(empT("emp.atMostLevel", "At most Level"))} ${ROMAN[r.level_ceiling]}</span> <span class="fd-offer-odds">${esc(empT("emp.atMost", "at most about"))} <b>${r.odds.percent}%</b> ${esc(empT("emp.oddsTail", "lottery odds at this level"))} (${r.odds.entries} ${esc(empT("emp.entries", "entries"))})</span><div class="fd-offer-basis">${esc(r.level_basis || "")}</div></div>`
        : (r.level_basis ? `<div class="fd-offer-basis">${esc(r.level_basis)}</div>` : "");
    const onLevel = r.likely_level || r.level_ceiling;
    const by = r.odds && r.odds.by_level
      ? `<div class="fd-offer-bylevel">${[1, 2, 3, 4].map(l => `<span class="${l === onLevel ? "is-on" : ""}">${ROMAN[l]} ${Math.round(r.odds.by_level[l] * 100)}%</span>`).join("")}</div>` : "";
    const pts = (r.talking_points || []).map(p => `<li>${esc(p)}</li>`).join("");
    return `${head}${lvl}${by}
      <div class="fd-offer-pts-h">${esc(empT("emp.talk", "Three things to say"))}</div>
      <ul class="fd-offer-pts">${pts}</ul>
      <div class="fd-offer-note">${esc(r.note || "")}</div>`;
  }

  // "a, b and c" -- keeps the sponsorship sentence reading naturally for 1, 2 or 3 facts.
  function listJoin(xs) {
    if (xs.length <= 1) return xs.join("");
    return xs.slice(0, -1).join(", ") + " and " + xs[xs.length - 1];
  }

  // Verdict band -> label + colour tier (reuses the good/mid/low tokens).
  const VERDICT = { strong: ["Strong fit", "good"], good: ["Good fit", "good"],
    moderate: ["Moderate fit", "mid"], weak: ["Weak fit", "low"] };

  // An HONEST, multi-dimensional read (not a bare keyword %): a verdict weighted toward the role's
  // MUST-HAVES, must-have gaps called out apart from nice-to-haves, a seniority heads-up, and the
  // eligibility flag that matters most to an international student -- whether the employer sponsors.
  function renderMatch(m, job) {
    const el = $("#fdMatch");
    if (!el) return;
    if (!m) { el.innerHTML = `<div class="fd-match-none">Add your resume, or complete your profile in the chat, and this shows how you match this role and every other one: skills you have and skills the ad asks for.</div>`; return; }
    if (!m.total) { el.innerHTML = `<div class="fd-match-none">No specific skills called out in this posting, tailoring will still align your resume to it.</div>`; return; }
    const pct = Math.round((m.ratio || 0) * 100);
    const v = VERDICT[m.verdict];
    const label = v ? v[0] : `${pct}% match`;
    const cls = v ? v[1] : (pct >= 60 ? "good" : pct >= 35 ? "mid" : "low");
    const reqMiss = (m.required_missing || []).slice(0, 8);
    const optMiss = (m.optional_missing || []).slice(0, 8);
    const gaps = reqMiss.concat(optMiss).slice(0, 8);   // upskill plans the must-haves first
    // Eligibility: for an int'l student, "this employer has no sponsorship record" is a real fit
    // signal worth flagging honestly (don't burn an application on a likely dead-end). When there
    // IS a record the green badges above already say so, so we only add the downside note.
    const j = job || LAST_JOBS.find(x => x.source_id === SELECTED_SID) || {};
    const hasSponsor = ((j.visa || j.visa_badges || []).length > 0) || !!j.sponsor;
    el.innerHTML = `
      <div class="fd-match-top">
        <span class="fd-match-score ${cls}">${esc(label)}</span>
        <span class="fd-match-sub">covers ${m.covered} of ${m.total} skills${m.required_total ? ` &middot; ${m.required_covered}/${m.required_total} must-haves` : ""}</span>
      </div>
      ${m.seniority ? `<div class="fd-fit-note fit-${esc(m.seniority.level)}">${esc(m.seniority.note)}</div>` : ""}
      ${!hasSponsor ? `<div class="fd-fit-note fit-warn">No public sponsorship record for this employer, worth confirming before you apply.</div>` : ""}
      ${reqMiss.length ? `<div class="fd-gaps"><span class="fd-gaps-lbl">Missing must-haves:</span> ${reqMiss.map(g => `<span class="fd-gap fd-gap-req">${esc(g)}</span>`).join("")}</div>` : ""}
      ${optMiss.length ? `<div class="fd-gaps"><span class="fd-gaps-lbl">Nice-to-haves to weave in:</span> ${optMiss.map(g => `<span class="fd-gap">${esc(g)}</span>`).join("")}</div>` : ""}
      ${gaps.length ? `<button class="fd-upskill-btn" data-upskill type="button">How do I close these gaps? →</button>
        <div class="fd-upskill" id="fdUpskill"></div>` : ""}`;
    el.querySelector("[data-upskill]")?.addEventListener("click", e => loadUpskill(gaps, e.currentTarget, reqMiss));
  }

  // On-demand: turn the skill gaps into a short, PRIORITIZED learning plan. Runs the model only when
  // asked, so it costs nothing unless used. Smarter than a flat list: must-haves (what a screener
  // gates on) come first and are labelled, each step carries an honest effort estimate, and a
  // "builds on..." note appears when a gap is a short hop from something they already have.
  async function loadUpskill(gaps, btn, required) {
    const box = $("#fdUpskill");
    const j = LAST_JOBS.find(x => x.source_id === SELECTED_SID) || {};
    btn.disabled = true; btn.textContent = "Building your plan…";
    try {
      const r = await api("/api/jobs/upskill", { gaps, role: j.title || "", required: required || [] });
      const plan = (r && r.plan) || [];
      if (!plan.length) {
        box.innerHTML = `<div class="us-empty">Couldn't build a plan right now, try again in a moment.</div>`;
        btn.disabled = false; btn.textContent = "How do I close these gaps? →";
        return;
      }
      const mustN = plan.filter(p => p.priority === "must-have").length;
      const lead = mustN
        ? `Start with the ${mustN} must-have${mustN > 1 ? "s" : ""}, that is what a screener gates on.`
        : "How to close each gap, fastest first.";
      box.innerHTML = `<div class="us-lead">${esc(lead)}</div>` + plan.map(p =>
        `<div class="us-item${p.priority === "must-have" ? " us-must" : ""}">
          <div class="us-head">
            <span class="us-skill">${esc(p.skill)}</span>
            ${p.priority === "must-have" ? `<span class="us-tag us-tag-must">Must-have</span>` : ""}
            ${p.effort ? `<span class="us-effort">${esc(p.effort)}</span>` : ""}
          </div>
          <div class="us-how">${esc(p.how)}</div>
          ${p.leverage ? `<div class="us-lev">Builds on what you have: ${esc(p.leverage)}</div>` : ""}
          ${p.resource ? `<div class="us-res">${esc(p.resource)}</div>` : ""}
        </div>`).join("");
      btn.remove();
    } catch (e) {
      btn.disabled = false; btn.textContent = "How do I close these gaps? →";
      box.innerHTML = `<div class="us-empty">${esc((e && e.message) || "Something went wrong.")}</div>`;
    }
  }

  // A posting HAS structure ("About the company", "The role", "Responsibilities",
  // "Qualifications"). We threw it away and rendered one undivided block of prose, so the
  // reader did the parsing that the posting had already done. A heading is a short line
  // that doesn't end like a sentence and has something under it.
  const looksHeading = (line, next) => {
    const t = line.trim();
    return t.length > 2 && t.length <= 52 && !/[.!?,;:]$/.test(t)
      && !/^[•\-*·]/.test(t) && /[A-Za-z]/.test(t)
      && !/:\s+\S/.test(t)               // "Label: value" is a key-value line, not a heading
      && !!(next || "").trim();
  };

  function jdSections(text) {
    const lines = text.split("\n");
    const out = [];
    let cur = { head: "", body: [] };
    lines.forEach((line, i) => {
      if (looksHeading(line, lines[i + 1]) && (cur.body.length || cur.head)) {
        out.push(cur);
        cur = { head: line.trim(), body: [] };
      } else if (looksHeading(line, lines[i + 1]) && !cur.head) {
        cur.head = line.trim();
      } else {
        cur.body.push(line);
      }
    });
    out.push(cur);
    return out.filter(s => s.head || s.body.join("").trim());
  }

  // Mark the JD's own key terms IN the prose. We already extract them and were showing them as
  // chips above the posting, which asks the reader to hold a list in their head and then hunt for
  // it. Marking them in place is the same information doing the work itself. Two tones, the way
  // froghire/LinkedIn do it: terms the profile already backs get a quiet "you have this" green,
  // and gaps, the terms that decide whether this role is worth applying to, get the emphasis.
  // Longer terms are marked first so "data science" wins over a bare "data".
  function markTerms(html, present, missing, neutral) {
    const marks = [
      ...(missing || []).map(t => [t, "jd-gap"]),
      ...(present || []).map(t => [t, "jd-have"]),
      ...(neutral || []).map(t => [t, "jd-key"]),
    ].filter(([t]) => String(t).trim().length >= 2)
     .sort((a, b) => String(b[0]).length - String(a[0]).length);
    marks.forEach(([term, cls]) => {
      const t = String(term).trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      // Skip anything already inside a <mark …>…</mark> so a shorter term can't re-tag a longer hit.
      const rx = new RegExp("(?<![\\w>])(" + t + ")(?![\\w<])(?![^<]*</mark>)", "gi");
      html = html.replace(rx, `<mark class="${cls}">$1</mark>`);
    });
    return html;
  }

  const isBullet = (line) => /^\s*[•*\-·▪‣]\s+/.test(line);
  const stripBullet = (line) => line.replace(/^\s*[•*\-·▪‣]\s+/, "").trim();

  // Render one JD paragraph. A "Label: value" line (Location:, Remote Type:, Job Family Group:,
  // Application Deadline:, ...) gets its LABEL bolded, the way LinkedIn does, so the scannable
  // facts stand out; everything else is plain prose. Key terms are still marked inside the value.
  function renderJdLine(text, present, missing, neutral) {
    const m = text.match(/^([A-Z][A-Za-z0-9 &/()\-.]{1,30}):(\s*)(.*)$/);
    if (m && m[1].trim().split(/\s+/).length <= 5) {
      const val = m[3] ? " " + markTerms(esc(m[3]), present, missing, neutral) : "";
      return `<b class="jd-lbl">${esc(m[1])}:</b>${val}`;
    }
    return markTerms(esc(text), present, missing, neutral);
  }

  // Turn a section's lines into HTML: runs of bullet lines become a real <ul>, everything else is
  // a paragraph. A marker sitting alone on its line (some stored feeds split "•" from its text)
  // is folded into the line beneath it so it still reads as one bullet.
  function jdBody(lines, present, missing, neutral) {
    const rows = [];
    for (let i = 0; i < lines.length; i++) {
      let ln = lines[i];
      // A marker alone on its line: fold its text up, SKIPPING any blank lines between them
      // (some feeds format a list as "* \n\ntext", e.g. Abbott), so it reads as one bullet.
      if (/^\s*[•*\-·▪‣]\s*$/.test(ln)) {
        let k = i + 1;
        while (k < lines.length && !(lines[k] || "").trim()) k++;
        if (k < lines.length) { ln = "• " + lines[k].trim(); i = k; }
      }
      rows.push(ln);
    }
    const out = [];
    let bullets = [];
    const flush = () => {
      if (!bullets.length) return;
      out.push(`<ul class="jd-list">${bullets.map(b =>
        `<li>${markTerms(esc(b), present, missing, neutral)}</li>`).join("")}</ul>`);
      bullets = [];
    };
    rows.forEach(ln => {
      if (isBullet(ln)) { bullets.push(stripBullet(ln)); return; }
      if (!ln.trim()) return;                 // blank lines are spacing, not a break in a bullet run
      flush();
      out.push(`<p>${renderJdLine(ln.trim(), present, missing, neutral)}</p>`);
    });
    flush();
    return out.join("");
  }

  function renderJd(jd, match, terms) {
    const el = $("#fdJd");
    if (!el) return;
    jd = (jd || "").trim();
    if (!jd) {
      el.innerHTML = '<span class="muted">This board didn\'t include the full text, open the posting to read it. You can still tailor from the title and company.</span>';
      return;
    }
    const present = (match || {}).present || [], missing = (match || {}).missing || [];
    // No profile yet: every skill the ad asks for is still underlined (neutral), so the JD
    // reads as a skills list from the first launch; with a profile the underlines turn
    // green / red. Nobody is forced to upload a resume to get a readable description.
    const neutral = (present.length || missing.length) ? [] : (terms || []);
    const legend = (present.length || missing.length)
      ? `<div class="jd-legend"><span class="jd-key"><span class="jd-swatch have"></span>On your resume</span>`
        + `<span class="jd-key"><span class="jd-swatch gap"></span>Asked for, not on your resume</span></div>`
      : neutral.length
        ? `<div class="jd-legend"><span class="jd-key"><span class="jd-swatch key"></span>Skills this ad asks for</span>`
          + `<span class="jd-legend-hint">Add your resume or complete your profile and these turn green (you have it) or red (you don't).</span></div>`
        : "";
    el.innerHTML = legend + jdSections(jd).map(s => {
      const body = jdBody(s.body, present, missing, neutral);
      return `<section class="jd-sec">
        ${s.head ? `<h4 class="jd-sec-h">${markTerms(esc(s.head), present, missing, neutral)}</h4>` : ""}
        ${body ? `<div class="jd-sec-b">${body}</div>` : ""}
      </section>`;
    }).join("");
  }

  // Whole calendar days since a YYYY-MM-DD date, in the viewer's local zone (0 = today, 1 = yesterday).
  function daysSince(y, m, d) {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return Math.round((today - new Date(y, m - 1, d)) / 86400000);
  }
  // A date-only posting (today / yesterday / "Aug 10") rather than an hour count. Job feeds give a
  // DATE with no clock, so measuring it in hours is misleading: a role posted anytime today reads
  // "25h ago" the moment the clock passes midnight. Calendar days tell the truth.
  function calendarAgo(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    const days = daysSince(y, m, d);
    if (days <= 0) return "today";
    if (days === 1) return "yesterday";
    if (days < 7) return days + "d ago";
    const then = new Date(y, m - 1, d), now = new Date();
    const opts = then.getFullYear() === now.getFullYear()
      ? { month: "short", day: "numeric" }
      : { month: "short", day: "numeric", year: "numeric" };
    return then.toLocaleDateString(undefined, opts);
  }
  // Seconds -> short relative label. Shared by ago() and freshness() so both read identically.
  function relAgo(s) {
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + "m ago";
    if (s < 129600) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }
  // Parse a timestamp as UTC epoch ms, or null if it carries no clock. SQLite's CURRENT_TIMESTAMP
  // gives "YYYY-MM-DD HH:MM:SS" with no zone marker, which JS would otherwise misread as local time.
  function tsUTC(v) {
    if (!v) return null;
    let s = String(v).trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;              // date only -> no precise clock
    s = s.replace(" ", "T");
    if (!/[Zz]|[+-]\d{2}:?\d{2}$/.test(s)) s += "Z";            // assume UTC when unmarked
    const t = new Date(s).getTime();
    return isNaN(t) ? null : t;
  }
  function ago(iso) {
    if (!iso) return "never";
    if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) return calendarAgo(iso);   // date-only -> calendar label
    return relAgo(Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000));
  }

  let FEED_POLL = null;
  let FEED_SOURCE = "local";   // "central" when the list came from the hosted kitchen, else local
  let FEED_LOADED = false;     // false until the first server response, so the skeleton shows
  // When the newest job ARRIVED (server's newest_at). The board shows this, not the build time:
  // builds and polls carry on regardless, so "updated 2m ago" sat over a month of no new postings
  // in August 2026.
  let FEED_NEWEST = null;
  const STALE_AFTER_MS = 24 * 3600000; // nothing new for a day: the only time the note turns red
  const QUIET_AFTER_MS = 6 * 3600000; // the feed rebuilds every 3h; two empty builds is a fault
  let FEED_REQ = 0;           // request counter, to ignore out-of-order responses
  let FEED_OK = false;         // have we ever loaded a trustworthy (non-degraded) board?
  let FEED_RECONNECTING = false;
  let FEED_CRAWLING = false;     // the engine is filling an empty local store for the first time
  let FEED_CRAWL_ERROR = "";     // the last first-open crawl failed and nothing is stored yet
  let FEED_CRAWL_TIMER = null;
  let FEED_DL = null;          // the first download's status while the list is still downloading
  let FEED_UPDATING = null;    // a NEWER list downloading while the cached one is shown (2026-10-20)
  let FEED_FAIL = false;       // that download failed and nothing is cached: show Retry
  let FEED_RETRY = null;       // pending fast-retry timer while the hosted feed is unreachable
  let FEED_RETRY_N = 0;        // retry attempt count, for capped exponential backoff
  let FEED_SINCE = 0;          // when the current reconnect window started (ms), 0 when connected
  const COLD_GRACE_MS = 45000; // on a COLD start during an outage, show "connecting" and retry this
                               // long before revealing the stale local fallback (better than nothing)
  function feedReconnectElapsed() { return FEED_SINCE ? Date.now() - FEED_SINCE : 0; }

  // Turn the current filter state into query params. All filtering + pagination is server-side
  // now (so the board can browse tens of thousands with a tiny payload each page).
  function feedParams(page) {
    const p = new URLSearchParams();
    if (JF_SEARCH) p.set("q", JF_SEARCH);
    if (JF_LOC) p.set("loc", JF_LOC);
    if (JF_DATE) p.set("days", String(JF_DATE));
    if (JF_REMOTE) p.set("remote", JF_REMOTE);
    if (JF_LEVEL) p.set("level", JF_LEVEL);
    if (VISA_SET.size) p.set("visa", [...VISA_SET].join(","));
    if (JF_PAY) p.set("pay", String(JF_PAY));
    if (JF_SORT) p.set("sort", JF_SORT);
    if (JF_SPONSORED) p.set("sponsored", "1");
    if (JF_SAVED) p.set("saved", "1");
    p.set("page", String(page || 1));
    p.set("per_page", String(JOBS_PER_PAGE));
    return p.toString();
  }

  // The single entry point for load, filter change, and page change: fetch ONE filtered page
  // from the server and render it.
  async function loadPage(page) {
    JOBS_PAGE = Math.max(1, page || 1);
    const my = ++FEED_REQ;
    try {
      const r = await api("/api/jobs?" + feedParams(JOBS_PAGE));
      if (my !== FEED_REQ) return;                 // a newer request already superseded this
      // The first download of the list is still running: show its progress and reload when it
      // lands (never the empty "no roles" pane, never the stale local fallback).
      if (r.loading) {
        clearReconnect();
        FEED_DL = r.feed || {}; FEED_FAIL = false;
        LAST_JOBS = []; FEED_TOTAL = 0; FEED_ALLTOTAL = 0; FEED_LOADED = false;
        paintFeed();
        watchFeedDownload(() => loadPage(JOBS_PAGE));
        return;
      }
      FEED_DL = null;
      checkFeedUpdate();
      // The download failed (after the engine's retries) and nothing is cached: say so with a
      // Retry, rather than "connecting" forever over an empty board.
      FEED_FAIL = !!(r.degraded && feedFailed(r.feed) && !(r.jobs || []).length);
      if (FEED_FAIL) {
        clearReconnect();
        LAST_JOBS = []; FEED_TOTAL = 0; FEED_ALLTOTAL = 0; FEED_LOADED = true;
        paintFeed();
        return;
      }
      // A DEGRADED response = the hosted feed was unreachable (e.g. a deploy restart) and the server
      // fell back to its small stale local copy. Never flash that stale copy over a live board:
      //  - if we already have a good board, KEEP it and reconnect in the background;
      //  - if this is a COLD start (nothing good yet), show a "connecting" state and retry for a
      //    grace period, and only reveal the local fallback if the outage outlasts it.
      if (r.degraded) {
        if (FEED_OK) { scheduleReconnect(); return; }
        if (feedReconnectElapsed() < COLD_GRACE_MS) { scheduleReconnect(); paintFeed(); return; }
        // grace exceeded and still no live feed -> fall through and show local (better than nothing)
      }
      clearReconnect();
      FEED_SOURCE = r.source || "local";
      FEED_NEWEST = r.newest_at || null;
      FEED_TOTAL = r.count != null ? r.count : (r.jobs || []).length;
      FEED_ALLTOTAL = r.total != null ? r.total : FEED_TOTAL;
      LAST_JOBS = r.jobs || [];
      FEED_LOADED = true;
      if (!r.degraded) FEED_OK = true;             // we have a trustworthy board to fall back on
      // First-open crawl: the store was empty, the engine is filling it in the background. Re-poll
      // every 15s so rows appear as boards come in, then drop back to the normal 2-min poll.
      FEED_CRAWLING = !!r.crawling;
      FEED_CRAWL_ERROR = (!r.crawling && !FEED_TOTAL && r.crawl_error) ? String(r.crawl_error) : "";
      if (FEED_CRAWL_TIMER) { clearTimeout(FEED_CRAWL_TIMER); FEED_CRAWL_TIMER = null; }
      if (FEED_CRAWLING) FEED_CRAWL_TIMER = setTimeout(() => { FEED_CRAWL_TIMER = null; loadPage(JOBS_PAGE); }, 15000);
      paintFeed();
    } catch {
      if (my !== FEED_REQ) return;
      // Transient fetch failure (engine busy, brief drop). Keep the last good board if we have one;
      // on a cold start, keep showing "connecting" and retry through the grace window rather than
      // blanking. Only give up to an empty pane if we truly have nothing after the grace window.
      if (FEED_OK || feedReconnectElapsed() < COLD_GRACE_MS) { scheduleReconnect(); paintFeed(); return; }
      FEED_LOADED = true; LAST_JOBS = []; FEED_TOTAL = 0; paintFeed();
    }
  }

  // While the hosted feed is unreachable, keep the last good board and retry faster than the 2-min
  // poll, so a deploy-time restart never degrades what the user sees. Capped exponential backoff with
  // full jitter (5s, 10s, 20s ... cap 60s, randomized) so a fleet of clients doesn't stampede the
  // kitchen the instant it comes back (the standard thundering-herd mitigation).
  function scheduleReconnect() {
    FEED_RECONNECTING = true;
    if (!FEED_SINCE) FEED_SINCE = Date.now();      // start (or keep) the reconnect window clock
    renderNote();                                  // swap the note to "reconnecting", keep the count
    if (FEED_RETRY) return;                         // one timer at a time
    const capped = Math.min(5000 * 2 ** FEED_RETRY_N, 60000);
    const delay = capped / 2 + Math.random() * (capped / 2);   // full jitter over [cap/2, cap]
    FEED_RETRY_N++;
    FEED_RETRY = setTimeout(() => { FEED_RETRY = null; loadPage(JOBS_PAGE); }, delay);
  }
  function clearReconnect() {
    FEED_RECONNECTING = false;
    FEED_RETRY_N = 0;
    FEED_SINCE = 0;
    if (FEED_RETRY) { clearTimeout(FEED_RETRY); FEED_RETRY = null; }
  }

  // The board is showing the cached list while a newer one downloads (the app was closed for days):
  // say "updating to today's list" instead of an alarming "no new jobs for 5d", and swap in the
  // new list on its own when it lands, without a reload (Kofi, 2026-10-20).
  async function checkFeedUpdate() {
    if (FEED_UPDATING) return;
    let st = null;
    try { st = await (await fetch("/api/jobs/feed-status")).json(); } catch { return; }
    if (!st || !st.downloading) return;
    FEED_UPDATING = st; renderNote();
    watchFeedDownload(() => { FEED_UPDATING = null; loadPage(JOBS_PAGE); });
  }

  function renderNote() {
    const el = $("#jobsNote");
    if (!el) return;
    el.classList.remove("feed-note-quiet", "feed-note-stale");   // only the age line below sets them
    const total = FEED_ALLTOTAL, shown = FEED_TOTAL;
    const count = shown === total
      ? `${total.toLocaleString()} live role${total === 1 ? "" : "s"}`
      : `${shown.toLocaleString()} of ${total.toLocaleString()} role${total === 1 ? "" : "s"}`;
    // While reconnecting, say so instead of flashing a stale count or a frozen timestamp. On a cold
    // start (no good board yet) there's no count to keep, so just show "connecting to the live feed".
    // The list pane says it once (downloading, or the failure with Retry): keep the note quiet.
    if (FEED_DL || FEED_FAIL) { el.textContent = ""; return; }
    if (FEED_RECONNECTING) {
      el.textContent = FEED_OK ? `${count} · reconnecting` : "connecting to the live feed";
      return;
    }
    if (FEED_CRAWL_ERROR) {
      el.textContent = I18N.t("jobs.crawlError", "couldn't fetch jobs from the boards, will retry in a few minutes");
      el.title = FEED_CRAWL_ERROR;
      return;
    }
    el.title = "";
    if (FEED_UPDATING) {
      el.textContent = `${count} · ${I18N.t("jobs.updating", "updating to today's list")}`;
      return;
    }
    if (FEED_CRAWLING) {
      el.textContent = total
        ? `${count} · ${I18N.t("jobs.crawling", "fetching more from the job boards")}`
        : I18N.t("jobs.crawlingFirst", "fetching jobs from the job boards for the first time, this takes a few minutes");
      return;
    }
    // The age of the NEWEST JOB, never the age of the last rebuild. If nothing new has arrived for
    // a while, say so: a quiet board that admits it beats a fresh-looking one that isn't.
    const newest = tsUTC(FEED_NEWEST);
    const quiet = newest == null ? null : Math.max(0, Date.now() - newest);
    const isQuiet = quiet != null && quiet > QUIET_AFTER_MS;
    // Quiet is a muted fact; only a board with nothing new for a whole day (and no newer list
    // downloading, handled above) is worth the red.
    el.classList.toggle("feed-note-quiet", isQuiet);
    el.classList.toggle("feed-note-stale", quiet != null && quiet > STALE_AFTER_MS);
    if (quiet == null) el.textContent = `${count} · updates on its own`;
    else if (isQuiet) el.textContent = `${count} · no new jobs for ${spanShort(quiet / 1000)}`;
    else el.textContent = `${count} · newest job ${relAgo(quiet / 1000)} · updates on its own`;
  }

  // "7h" / "3d": a LENGTH of time, where relAgo gives a point in the past.
  function spanShort(s) {
    if (s < 172800) return Math.max(1, Math.round(s / 3600)) + "h";
    return Math.round(s / 86400) + "d";
  }

  // Shimmer placeholder rows while the first feed loads, so the pane never looks broken/stuck.
  function jobSkeleton() {
    return `<div class="feed-skeleton">` +
      Array.from({ length: 6 }, () => `<div class="sk-row"><div class="sk-logo"></div>` +
        `<div class="sk-lines"><span class="sk-l w60"></span><span class="sk-l w85"></span>` +
        `<span class="sk-l w40"></span></div></div>`).join("") + `</div>`;
  }

  // Entering the Jobs view: paint stored roles instantly, then poll gently in the
  // background (official ATS feeds only) so the list stays fresh with no Refresh button.
  async function loadJobsFeed() {
    loadVisaStatus();
    await stratOnJobsOpen();      // saved searches: paint the rail, apply the default one once
    await loadPage(1);
    startFeedPoll();
  }
  // Gently refresh the current page on a timer so the board stays fresh with no button; preserve
  // the reader's scroll position across the refresh so it never yanks them.
  function startFeedPoll() {
    stopFeedPoll();
    FEED_POLL = setInterval(() => {
      const list = $("#feedList");
      const sc = list ? list.scrollTop : 0;
      loadPage(JOBS_PAGE).then(() => { const l = $("#feedList"); if (l) l.scrollTop = sc; });
      stratPoll();                 // saved-search counts ride the same timer
    }, 120000);
  }
  function stopFeedPoll() { if (FEED_POLL) { clearInterval(FEED_POLL); FEED_POLL = null; } }

  async function loadVisaStatus() {
    // Just triggers the one-time auto-load of public sponsor data when it isn't present yet.
    // There's no board-level disclaimer any more; the honest basis is on each badge's tooltip.
    try {
      const s = await api("/api/sponsors/status");
      if (!s.employers) autoLoadVisaData();
    } catch { /* sponsor data is optional; badges just won't populate until it loads */ }
  }

  // First visit (or empty data): pull the public H-1B sponsor data automatically,
  // in the background, then re-tag the jobs. No manual click needed.
  async function autoLoadVisaData() {
    try {
      const r = await api("/api/sponsors/refresh", {});
      showVisaNotice(r);
      await loadVisaStatus();
      loadPage(JOBS_PAGE || 1);
    } catch { /* sponsor data is optional; badges just won't populate until it loads */ }
  }

  // One quiet, dismissible line under the board count that says what the H-1B data covers, in
  // the API's own words (USCIS stopped publishing yearly files after FY2023; newer years arrive
  // with the quarterly data snapshot; the span of years loaded). Years USCIS never published are
  // the normal state of the world, not an error: only a real failure (offline, blocked) takes the
  // error colour, and it still reads as a line, never a modal.
  function boardNotice() {
    let el = $("#jobsNotice");
    if (el) return el;
    const after = $("#jobsNote");
    if (!after) return null;
    el = document.createElement("p");
    el.className = "feed-note feed-notice";
    el.id = "jobsNotice";
    el.hidden = true;
    after.insertAdjacentElement("afterend", el);
    return el;
  }
  function showBoardNotice(text, isError) {
    const el = boardNotice();
    if (!el || !text) return;
    el.classList.toggle("is-error", !!isError);
    el.innerHTML = `<span class="fn-text">${esc(text)}</span>`
      + `<button class="fn-x" type="button" aria-label="${esc(I18N.t("job.dismiss", "Dismiss"))}" title="${esc(I18N.t("job.dismiss", "Dismiss"))}">×</button>`;
    el.querySelector(".fn-x").addEventListener("click", () => { el.hidden = true; });
    el.hidden = false;
  }
  function showVisaNotice(r) {
    if (!r) return;
    const errors = Array.isArray(r.errors) ? r.errors : [];
    if (errors.length) {
      const years = errors.map(e => "FY" + e.fy).join(", ");
      const why = errors[0] && errors[0].error ? ` ${errors[0].error}` : "";
      showBoardNotice(I18N.t("job.visaErr", "Couldn't download H-1B data for {years}.").replace("{years}", years) + why, true);
      return;
    }
    if (r.note) showBoardNotice(r.note, false);
  }

  async function tailorJob(sid) {
    resetBuilder();
    showView("builder");
    $("#jdGate").hidden = true;
    const wk = working("Loading the role…");
    try {
      const st = await api("/api/session/start_job", { source_id: sid });
      wk.remove();
      applyState(st);
      $("#composerInput").focus();
    } catch (e) { wk.remove(); if (!e.reason || !OUTAGE[e.reason]) bubble("agent", `<span style="color:var(--flag)">${esc(e.message)}</span>`); }
  }

  // Go to a page by its nav name. A destination that moved still lands in the right place: the
  // Review page folded into Auto-apply (2026-10), so an old "review" link (a stale button, a
  // notification, a saved deep link) opens Auto-apply on its To review tile.
  const NAV_MOVED = { review: ["autoapply", "review"] };
  function navTo(dest, tile) {
    const moved = NAV_MOVED[dest];
    if (moved) { dest = moved[0]; tile = tile || moved[1]; }
    if (dest === "autoapply" && tile) AA_WANT = tile;
    document.querySelector(`.nav-item[data-nav="${dest}"]`)?.click();
  }
  let AA_WANT = null;   // the tile Auto-apply should open on next (set by navTo)
  document.querySelectorAll("[data-nav-to]").forEach(b =>
    b.addEventListener("click", () => navTo(b.dataset.navTo, b.dataset.aaTile)));
  // Faceted filters. Every change jumps back to page 1 (a new server query) so you're never
  // stranded on a page that no longer exists. Text inputs are debounced so we fetch once the
  // person pauses typing, not on every keystroke.
  const applyFilters = () => { JOB_FOCUS = null; loadPage(1); };
  let FILTER_DEBOUNCE = null;
  const applyFiltersDebounced = () => {
    clearTimeout(FILTER_DEBOUNCE);
    FILTER_DEBOUNCE = setTimeout(applyFilters, 300);
  };
  $("#jfSearch")?.addEventListener("input", (e) => { JF_SEARCH = e.target.value.trim(); applyFiltersDebounced(); });
  $("#jfLocation")?.addEventListener("input", (e) => { JF_LOC = e.target.value.trim(); applyFiltersDebounced(); });
  $("#jfRemote")?.addEventListener("change", (e) => { JF_REMOTE = e.target.value; applyFilters(); });
  $("#jfDate")?.addEventListener("change", (e) => { JF_DATE = Number(e.target.value) || 0; applyFilters(); });
  $("#jfPay")?.addEventListener("change", (e) => { JF_PAY = Number(e.target.value) || 0; applyFilters(); });
  $("#jfSort")?.addEventListener("change", (e) => { JF_SORT = e.target.value; applyFilters(); });
  // Sponsorship is MULTI-select: each checkbox toggles a code in VISA_SET (OR within the facet).
  document.querySelectorAll("#jfVisaDD input[data-visa]").forEach(cb =>
    cb.addEventListener("change", () => {
      if (cb.checked) VISA_SET.add(cb.dataset.visa); else VISA_SET.delete(cb.dataset.visa);
      updateVisaSummary(); applyFilters();
    }));
  // Close the sponsorship dropdown when clicking outside it.
  document.addEventListener("click", (e) => {
    const dd = $("#jfVisaDD");
    if (dd && dd.open && !dd.contains(e.target)) dd.open = false;
  });
  $("#jfEntry")?.addEventListener("click", (e) => {
    JF_LEVEL = JF_LEVEL === "entry" ? "" : "entry";
    e.currentTarget.setAttribute("aria-pressed", JF_LEVEL === "entry" ? "true" : "false");
    applyFilters();
  });
  $("#jfSponsored")?.addEventListener("click", (e) => {
    JF_SPONSORED = !JF_SPONSORED;
    e.currentTarget.setAttribute("aria-pressed", JF_SPONSORED ? "true" : "false");
    applyFilters();
  });
  $("#jfSaved")?.addEventListener("click", (e) => {
    JF_SAVED = !JF_SAVED;
    e.currentTarget.setAttribute("aria-pressed", JF_SAVED ? "true" : "false");
    applyFilters();
  });

  /* ---------------------------------------------------------------- CV preview render */
  // An editable text slot on the page: click, type, and it saves on blur (see the focusout
  // handler). Everything on the page is one of these except bullets and dates, which have
  // their own controls. The whole page is editable (Kofi, 2026-10-08); the server refuses an
  // edit only when it would push the resume onto a second page.
  function ed(ref, text, cls, placeholder) {
    const empty = !text;
    return `<span class="${cls || ""} cv-ed${empty ? " cv-ed-empty" : ""}" contenteditable="true" spellcheck="false"`
      + ` data-field="${ref}" data-ph="${esc(placeholder || "")}">${esc(text || "")}</span>`;
  }
  function renderPreview(p, templateSections) {
    p = p || {};
    // null = the template's section list is unknown (an empty builder); then every section the
    // profile has is shown. With a list, only those sections appear, in the page's own order.
    const shows = (name) => !templateSections || templateSections.includes(name);
    const id = p.identity || {};
    const name = $(".cv-name");
    name.innerHTML = ed("id-name", id.name, "", "Your name");
    name.toggleAttribute("data-empty", !id.name);
    const cc = $(".cv-contact");
    cc.innerHTML = [
      ed("id-address", id.address || id.city, "", "City, ST"),
      ed("id-phone", id.phone, "", "phone"),
      ed("id-email", id.email, "", "email"),
      ed("id-linkedin", id.linkedin, "", "LinkedIn URL"),
      ed("id-github", id.github, "", "GitHub URL"),
    ].join('<span class="cv-sep"> · </span>');
    cc.toggleAttribute("data-empty", false);

    const nproj = (p.projects || []).length;
    const body = $("#cvBody");
    body.innerHTML = "";
    const parts = {
      summary: () => section("Summary", `<div class="cv-inline">${ed("summary", p.summary, "", "A line on your professional focus")}</div>`),
      education: () => (p.education || []).length ? section("Education", eduHTML(p)) : null,
      skills: () => Object.keys(p.skills || {}).length ? section("Skills", skillsHTML(p)) : null,
      projects: () => nproj ? section("Projects", projHTML(p)) : null,
      experience: () => (p.experience || []).length ? section("Experience", expHTML(p, nproj)) : null,
      extracurricular: () => (p.extracurricular || []).length ? section("Extracurricular", extraHTML(p)) : null,
      interests: () => p.interests ? section("Additional Information", `<div class="cv-inline"><b>Interests:</b> ${ed("interests", p.interests)}</div>`) : null,
    };
    const order = templateSections || ["summary", "education", "skills", "projects", "experience", "extracurricular", "interests"];
    for (const name of order) {
      if (!parts[name] || !shows(name)) continue;
      if (name === "summary" && !templateSections && !p.summary) continue;
      const node = parts[name]();
      if (node) body.appendChild(node);
    }
    wireFlags();
  }
  function section(title, inner) {
    const s = el("section", "cv-sec");
    s.innerHTML = `<div class="cv-sec-h">${title}</div><hr class="cv-rule"/>${inner}`;
    return s;
  }
  function eduHTML(p) {
    return (p.education || []).map((e, i) => `<div class="cv-block">
      <div class="cv-row">${ed(`edu-${i}-school`, e.school, "cv-org", "School")}${ed(`edu-${i}-location`, e.location, "cv-loc", "City, ST")}</div>
      <div class="cv-row">${ed(`edu-${i}-degree`, e.degree, "cv-degree", "Degree")}<span class="cv-dates">${datePicker(e.date, `edu-${i}`, true, e.dates_placeholder)}</span></div>
      ${e.courses ? `<ul class="cv-ul"><li>Courses: ${ed(`edu-${i}-courses`, e.courses)}</li></ul>` : ""}</div>`).join("");
  }
  function skillsHTML(p) {
    // The whole line is one slot ("Label: items"), so the label can be renamed too.
    return Object.entries(p.skills || {}).map(([k, v], i) =>
      `<div class="cv-inline"><span class="cv-ed" contenteditable="true" spellcheck="false" data-field="skill-${i}"><b>${esc(k)}:</b> ${esc(v)}</span></div>`).join("");
  }
  function bulletList(bullets, refPrefix) {
    const b = (bullets || []).map((t, bi) =>
      `<li contenteditable="true" data-ref="${refPrefix}-b${bi}">${esc(t)}</li>`).join("");
    return b ? `<ul class="cv-ul">${b}</ul>` : "";
  }
  /* -------- month/year date picker (job-portal style) for the edit view -------- */
  const DP_MONTHS = ["Jan", "Feb", "March", "April", "May", "June", "July",
                     "Aug", "Sept", "Oct", "Nov", "Dec"];
  function dpMonthOpts(sel) {
    return ['<option value=""></option>'].concat(DP_MONTHS.map(m =>
      `<option${m === sel ? " selected" : ""}>${m}</option>`)).join("");
  }
  function dpYearOpts(sel) {
    const top = new Date().getFullYear() + 1, o = ['<option value=""></option>'];
    for (let y = top; y >= 1975; y--) o.push(`<option${String(y) === String(sel) ? " selected" : ""}>${y}</option>`);
    return o.join("");
  }
  function dpParse(str) {
    const one = s => { const m = (s || "").match(/([A-Za-z]{3,9})\.?\s*((?:19|20)\d\d)/);
      if (m) return { mon: m[1], yr: m[2] }; const y = (s || "").match(/(?:19|20)\d\d/);
      return { mon: "", yr: y ? y[0] : "" }; };
    // Split on the RANGE separator only (a dash, an en dash, "to"), never on spaces: splitting
    // on spaces tore "Sept 2015" into "Sept" and "2015", so every date dropdown in the editor
    // showed empty over a correctly dated page (2026-10-07). Month names are matched to the
    // picker's own spellings (Sep/Sept, March/Mar) so a parsed date actually selects an option.
    const parts = String(str || "").split(/\s*(?:[-\u2013\u2014]|\bto\b)\s*/);
    const canon = m => { const k = (m || "").slice(0, 3).toLowerCase();
      const hit = DP_MONTHS.find(x => x.slice(0, 3).toLowerCase() === k); return hit || ""; };
    const s = one(parts[0]); s.mon = canon(s.mon); let e = { mon: "", yr: "" }, present = false;
    if (parts[1]) { if (/present|current|now|ongoing/i.test(parts[1])) present = true; else { e = one(parts[1]); e.mon = canon(e.mon); } }
    return { sm: s.mon, sy: s.yr, em: e.mon, ey: e.yr, present };
  }
  function datePicker(dateStr, dref, single, flagged) {
    const d = dpParse(dateStr), cls = "cv-datepick" + (flagged ? " cv-dp-flagged" : "");
    if (single) {
      return `<span class="${cls}" data-dref="${dref}" data-single="1"` +
        `><select class="dp-m">${dpMonthOpts(d.sm)}</select><select class="dp-y">${dpYearOpts(d.sy)}</select></span>`;
    }
    return `<span class="${cls}" data-dref="${dref}"` +
      `><select class="dp-sm">${dpMonthOpts(d.sm)}</select><select class="dp-sy">${dpYearOpts(d.sy)}</select` +
      `><span class="dp-sep"></span` +
      `><select class="dp-em"${d.present ? " disabled" : ""}>${dpMonthOpts(d.em)}</select><select class="dp-ey"${d.present ? " disabled" : ""}>${dpYearOpts(d.ey)}</select` +
      `><label class="dp-pres-l"><input type="checkbox" class="dp-pres"${d.present ? " checked" : ""}/>Present</label></span>`;
  }
  function dpValue(span) {
    const g = s => { const n = span.querySelector(s); return n ? n.value : ""; };
    if (span.dataset.single) { const m = g(".dp-m"), y = g(".dp-y"); return (m && y) ? `${m} ${y}` : (y || ""); }
    const sm = g(".dp-sm"), sy = g(".dp-sy"), pres = span.querySelector(".dp-pres").checked;
    const start = (sm && sy) ? `${sm} ${sy}` : (sy || "");
    const end = pres ? "Present" : (() => { const em = g(".dp-em"), ey = g(".dp-ey"); return (em && ey) ? `${em} ${ey}` : (ey || ""); })();
    return !start ? "" : (end ? `${start} - ${end}` : start);
  }
  function projHTML(p) {
    return (p.projects || []).map((x, gi) => `<div class="cv-block${x.suggested ? " cv-suggested" : ""}">
      <div class="cv-row"><span class="cv-org">${ed(`ent-${gi}-org`, x.org, "", "Project")}${x.suggested ? ' <span class="cv-sugg-badge">Suggested · replace before finalizing</span>' : ""}</span>${ed(`ent-${gi}-location`, x.location, "cv-loc", "")}</div>
      <div class="cv-row">${ed(`ent-${gi}-r0-title`, x.title, "cv-role", "Your role")}<span class="cv-dates">${datePicker(x.dates, `proj-${gi}`, false, x.dates_placeholder)}</span></div>
      ${bulletList(x.bullets, `g${gi}-r0`)}</div>`).join("");
  }
  function expHTML(p, nproj) {
    return (p.experience || []).map((x, ei) => {
      const roles = (x.roles || [x]).map((r, ri) => {
        const title = r.title_suggested
          ? `<span class="cv-role"><span class="cv-flag" data-role="${ei}-${ri}" data-sugg="${esc(r.title)}" data-orig="${esc(r.title_original || "")}">${esc(r.title)}</span></span>`
          : ed(`ent-${nproj + ei}-r${ri}-title`, r.title, "cv-role", "Job title");
        return `<div class="cv-row">${title}<span class="cv-dates">${datePicker(r.dates, `exp-${ei}-${ri}`, false, r.dates_placeholder)}</span></div>
                ${bulletList(r.bullets, `g${nproj + ei}-r${ri}`)}`;
      }).join("");
      return `<div class="cv-block"><div class="cv-row">${ed(`ent-${nproj + ei}-org`, x.org, "cv-org", "Company")}${ed(`ent-${nproj + ei}-location`, x.location, "cv-loc", "City, ST")}</div>${roles}</div>`;
    }).join("");
  }
  function extraHTML(p) {
    return (p.extracurricular || []).map((x, xi) => `<div class="cv-block">
      <div class="cv-row">${ed(`extra-${xi}-title`, x.title, "cv-role", "Activity")}<span class="cv-dates">${datePicker(x.date, `extra-${xi}`, false, x.dates_placeholder)}</span></div>
      ${bulletList(x.bullets, `x${xi}`)}</div>`).join("");
  }

  /* -------- bullet editing: persist on blur, then re-render the real PDF so the
     preview stays the source of truth (edit panel is left as-is to avoid disruption) */
  // Save on blur. Any text on the page. If the server refused the edit (it would make a second
  // page), the text snaps back to what it was and the person is told why, once.
  async function saveEdit(el, url, ref) {
    const text = el.textContent;
    if (text === el.dataset.last) return;
    try {
      const st = await api(url, { ref, text });
      if (st && st.edit_ok === false) {
        el.textContent = el.dataset.last || "";
        toast(st.edit_error || "That change was not kept.");
        return;
      }
      el.dataset.last = text;
      el.classList.toggle("cv-ed-empty", !text.trim());
      if (st && st.pdf) { $("#pdfFrame").src = st.pdf + "&preview=1#toolbar=0&navpanes=0&view=FitH"; $("#downloadBtn").href = st.pdf; }
    } catch (err) { /* non-fatal for the preview */ }
  }
  const paperEl = document.getElementById("paper");
  paperEl.addEventListener("focusin", (e) => {
    const el = e.target.closest("li[data-ref], .cv-ed");
    if (el && el.dataset.last === undefined) el.dataset.last = el.textContent;
  });
  paperEl.addEventListener("focusout", (e) => {
    const li = e.target.closest("li[data-ref]");
    if (li) return saveEdit(li, "/api/session/bullet", li.dataset.ref);
    const f = e.target.closest(".cv-ed[data-field]");
    if (f) return saveEdit(f, "/api/session/field", f.dataset.field);
  });
  // Enter finishes an edit (these are single fields, not paragraphs).
  paperEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.closest(".cv-ed, li[data-ref]")) { e.preventDefault(); e.target.blur(); }
  });

  /* -------- date picker: save on change, re-render the real PDF, clear the red flag -- */
  $("#cvBody").addEventListener("change", async (e) => {
    const span = e.target.closest(".cv-datepick");
    if (!span) return;
    const pres = span.querySelector(".dp-pres");
    if (pres) span.querySelectorAll(".dp-em.dp-ey").forEach(s => { s.disabled = pres.checked; });
    const text = dpValue(span);
    if (!text) return;
    try {
      const st = await api("/api/session/date", { ref: span.dataset.dref, text });
      if (st && st.pdf) {
        $("#pdfFrame").src = st.pdf + "&preview=1#toolbar=0&navpanes=0&view=FitH";
        $("#downloadBtn").href = st.pdf;
        span.classList.remove("cv-dp-flagged");   // a real date replaces the placeholder
      }
    } catch (err) { /* non-fatal for the preview */ }
  });

  /* -------- flagged title confirm -------- */
  const pop = $("#titlePop");
  let popRole = null;
  function wireFlags() {
    document.querySelectorAll(".cv-flag").forEach(f => {
      f.onclick = (ev) => {
        popRole = f.dataset.role;
        pop.querySelector('[data-dec="keep"]').textContent = `Keep “${f.dataset.sugg}”`;
        pop.querySelector('[data-dec="revert"]').textContent = f.dataset.orig
          ? `Use my title “${f.dataset.orig}”` : "Use my original title";
        const rect = f.getBoundingClientRect();
        pop.style.left = Math.min(rect.left, innerWidth - 232) + "px";
        pop.style.top = (rect.bottom + 6) + "px";
        pop.hidden = false;
        ev.stopPropagation();
      };
    });
  }
  document.addEventListener("click", (e) => { if (!pop.contains(e.target)) pop.hidden = true; });
  pop.querySelectorAll(".title-pop-b").forEach(b => b.addEventListener("click", async () => {
    const dec = b.dataset.dec;
    let text = "";
    if (dec === "custom") { text = prompt("Enter the job title to use:") || ""; if (!text.trim()) return; }
    pop.hidden = true;
    await withWorking(async () => applyState(await api("/api/session/title", { id: popRole, decision: dec, text })));
  }));

  /* ---------------------------------------------------------------- chat */
  const chat = $("#chatScroll");
  function bubble(role, html) {
    const m = el("div", "msg " + role);
    m.innerHTML = `<div class="msg-avatar">${role === "agent" ? "S" : "Y"}</div><div class="msg-bubble">${html}</div>`;
    chat.appendChild(m); chat.scrollTop = chat.scrollHeight; return m;
  }
  function working(label) {
    const m = el("div", "msg agent");
    m.innerHTML = `<div class="msg-avatar">S</div><div class="msg-bubble"><span class="spin"></span>${label || "Working…"}</div>`;
    chat.appendChild(m); chat.scrollTop = chat.scrollHeight; return m;
  }
  async function withWorking(fn, label) {
    const w = working(label);
    // A failure the outage screen already explains (no key, limit reached, offline) is not
    // repeated as a chat bubble: the person read the same sentence twice (2026-10-07).
    try { await fn(); } catch (e) { if (!e.reason || !OUTAGE[e.reason]) bubble("agent", `<span style="color:var(--flag)">${esc(e.message)}</span>`); }
    finally { w.remove(); }
  }

  /* ---------------------------------------------------------------- state -> UI */
  const steps = { details: 0, draft: 1, review: 2 };
  function setStep(name) {
    document.querySelectorAll(".step").forEach(s => {
      const on = s.dataset.step === name;
      s.classList.toggle("is-active", on);
      s.classList.toggle("is-done", steps[s.dataset.step] < (steps[name] ?? 0));
    });
  }
  function setCoverage(pct) {
    $("#coverageNum").textContent = pct + "%";
    $("#coverageFill").style.width = pct + "%";
  }

  // Server-provided chat text (LLM replies, JD/feed-derived role & company) is PLAIN
  // TEXT, escape it and keep line breaks. bubble() inserts its arg as raw HTML, so
  // passing these through unescaped is an injection sink (a sourced job title/company
  // containing markup would otherwise execute, stored XSS).
  //
  // Exception: our OWN copy emphasises a word with <b>, and escaping everything made those
  // tags show up as literal text in the chat. So we escape first, then re-allow ONLY a bare
  // <b>/</b>. A bare <b> carries no attributes, so it cannot execute; anything else (an
  // <img onerror=…>, or even <b onmouseover=…>) fails this exact match and stays escaped.
  // Model-written text sometimes carries em or en dashes; the copy rule (tailor-ux) bans them in
  // anything the person reads, so they become a comma or a plain hyphen here, at the one place
  // every agent message passes through.
  const unDash = (s) => String(s == null ? "" : s)
    .replace(/\s*\u2014\s*/g, ", ")                 // em dash joins clauses: a comma reads right
    .replace(/(\d)\s*\u2013\s*(\d)/g, "$1-$2")     // en dash in a range: 2015-2016
    .replace(/\s*\u2013\s*/g, ", ");
  const chatText = (s) => esc(unDash(s))
    .replace(/&lt;b&gt;/g, "<b>")
    .replace(/&lt;\/b&gt;/g, "</b>")
    .replace(/\n/g, "<br>");

  // One-click autonomous build for a returning user (their saved profile → tailored CV).
  function renderAutobuild() {
    if (document.getElementById("autobuildAction")) return;   // show once
    const wrap = el("div", "autobuild-action");
    wrap.id = "autobuildAction";
    wrap.innerHTML = `<button class="btn btn-primary" type="button">⚡ Tailor from my profile, build it for me</button>
      <span class="autobuild-hint">Uses your saved profile. You can still chat to tweak it after.</span>`;
    wrap.querySelector("button").addEventListener("click", () => {
      wrap.remove();
      sendTurn("just build it from my saved profile", "⚡ Build it from my saved profile");
    });
    chat.appendChild(wrap); chat.scrollTop = chat.scrollHeight;
  }

  function applyState(st) {
    // The CV workspace lives in the side panel now. A NEW pdf (fresh build or
    // recompile after an edit) reveals the panel once - Claude-Code behavior: the
    // artifact appears when the work is done, and the chat stays in the center.
    if (st && st.pdf && window.Panel) {
      Panel.noteBuild(st.pdf);
    }
    if (!st) return;
    paintTemplateUsed(st);
    paintLiveNav(st);
    (st.messages || []).forEach(m => bubble("agent", chatText(m)));
    if (st.question) bubble("agent", chatText(st.question));
    if (st.can_autobuild) renderAutobuild();
    else document.getElementById("autobuildAction")?.remove();
    if (st.role) $("#builderRole").textContent = st.role + (st.company ? " · " + st.company : "");
    if (st.step) setStep(st.step);
    if (st.preview) renderPreview(st.preview, st.template_sections || null);

    if (st.coverage) {
      $("#coverage").hidden = false;
      setCoverage(st.coverage.ratio);
      if (Array.isArray(st.keyword_actions)) KW_ACTIONS = st.keyword_actions;
      renderCovDetail(st.coverage);
    }
    if (st.review) renderReview(st.review);
    if (st.pdf) {
      $("#pdfFrame").src = st.pdf + "&preview=1#toolbar=0&navpanes=0&view=FitH";
      const dl = $("#downloadBtn"); dl.href = st.pdf;
      // No export while a flagged placeholder project remains.
      dl.hidden = Boolean(st.blocked_finalize);
      $("#editToggle").hidden = false;
      showPdf();   // the preview IS the compiled PDF, always land here after a build
    }
    if (st.phase === "review") {
      const blocked = Boolean(st.blocked_finalize);
      $("#acceptBtn").disabled = blocked;
      if (blocked) {
        const names = (st.suggested_projects || []).join(", ");
        $("#acceptNote").innerHTML =
          `⚠ This resume has a suggested placeholder project (${esc(names)}). Replace it with a real ` +
          `project, build one, or <a href="#" id="removeSuggested">remove it</a> before saving or exporting.`;
        const rm = $("#removeSuggested");
        if (rm) rm.addEventListener("click", async (e) => {
          e.preventDefault();
          await withWorking(async () => applyState(await api("/api/session/remove_suggested", {})),
            "Removing…");
        });
      } else {
        $("#acceptNote").textContent = st.one_page === false
          ? I18N.t("builder.acceptFit", "Fit to one page, review and accept.")
          : I18N.t("builder.acceptSave", "Looks good? Accept to save it to Resumes.");
      }
    }
  }

  // Honest pre-send check. The headline is the fabrication guard: any skill on the resume the
  // profile cannot back is called out first (it is the claim that costs an interview). Opportunities
  // and honest gaps ride along as guidance, and the one-page fit is noted.
  // The pre-send review panel is gone (Kofi, 2026-10-08: "remove it"). The ONE check that
  // protects the person stays, as a single warning line shown only when it fires: a skill on
  // the page that their own profile cannot back, which is the claim that unravels in an
  // interview. Everything else (gaps, fit, verdicts) is the preview's job to show, not a panel's.
  function renderReview(r) {
    const el = $("#presendReview");
    if (!el) return;
    const honesty = (r && r.checks || []).find(c => c.id === "honesty" && c.level === "flag");
    if (!honesty) { el.hidden = true; el.innerHTML = ""; return; }
    el.hidden = false;
    el.innerHTML = `<div class="ps-row ps-lvl-flag"><span class="ps-ic">!</span>`
      + `<div class="ps-body"><div class="ps-label">${esc(honesty.label)}</div>`
      + `<div class="ps-items">${(honesty.items || []).map(t => `<span class="ps-term ps-flag">${esc(t)}</span>`).join("")}</div></div></div>`;
  }

  /* "Add this keyword" (the Rezi / Huntr action, on our engine). Every missing term gets an
     Add; the server asks the model whether the person's own profile supports it and drafts a
     rewrite of ONE bullet (or one new line when the page has room). The card shows the
     evidence and the before/after with Accept / Ignore; Accept splices it surgically,
     recompiles, and refreshes the PDF and the report. An added term moves to "In resume"
     with an undo link. The term list is server truth (st.keyword_actions). */
  let KW_ACTIONS = [];
  let KW_PENDING = null;
  const kwT = (k, f) => (window.I18N ? I18N.t(k, f) : f);

  function renderCovDetail(cov) {
    const present = (cov.present || []).slice(0, 24);
    const have = (cov.missing_supported || []).slice(0, 20);
    const missing = (cov.missing || []).slice(0, 20);
    const applied = new Set(KW_ACTIONS.map(a => String(a.term || "").toLowerCase()));
    const hit = t => `<span class="term hit">${esc(t)}`
      + (applied.has(String(t).toLowerCase())
        ? ` <a href="#" class="kw-undo" data-term="${esc(t)}">${esc(kwT("kw.undo", "undo"))}</a>` : "")
      + `</span>`;
    const miss = t => `<span class="term miss">${esc(t)}<button type="button" class="kw-add" data-term="${esc(t)}">${esc(kwT("kw.add", "Add"))}</button></span>`;
    let html = `<span class="cd-lbl">${esc(kwT("kw.inResume", "In resume"))}</span>`
      + (present.map(hit).join("") || `<span class="term miss"></span>`);
    if (have.length) html += `<span class="cd-lbl">${esc(kwT("kw.youHave", "You have, not shown"))}</span>` + have.map(miss).join("");
    html += `<span class="cd-lbl">${esc(kwT("kw.missing", "Missing"))}</span>`
      + (missing.map(miss).join("") || `<span class="term hit">${esc(kwT("kw.none", "none"))}</span>`);
    $("#covDetail").innerHTML = html;
  }
  $("#covInfo").addEventListener("click", () => { $("#covDetail").hidden = !$("#covDetail").hidden; });

  function kwCard(html) {
    const card = $("#kwCard");
    if (!card) return;
    card.innerHTML = html;
    card.hidden = !html;
    if (html) { $("#covDetail").hidden = false; }
  }
  const kwIgnoreBtn = () => `<div class="kw-actions"><button class="btn btn-ghost btn-sm" type="button" id="kwIgnore">${esc(kwT("kw.ignore", "Ignore"))}</button></div>`;

  async function kwPreview(term, btn) {
    if (btn) { btn.disabled = true; btn.textContent = kwT("kw.working", "Working"); }
    let p;
    try { p = await api("/api/cv/live/keyword/preview", { term }); }
    catch (err) {
      kwCard(`<div class="kw-term">${esc(term)}</div><p class="kw-note">${esc(err.message || kwT("kw.failed", "Could not add this keyword."))}</p>` + kwIgnoreBtn());
      p = null;
    }
    if (btn) { btn.disabled = false; btn.textContent = kwT("kw.add", "Add"); }
    if (!p) return;
    KW_PENDING = p;
    if (!p.supported) {
      kwCard(`<div class="kw-term">${esc(term)}</div>`
        + `<p class="kw-note">${esc(kwT("kw.unsupported", "Not supported by your profile"))}. ${esc(kwT("kw.unsupportedHint", "Add it to a role or project in your profile, then try again."))}</p>`
        + kwIgnoreBtn());
      return;
    }
    if (!p.ok) {
      kwCard(`<div class="kw-term">${esc(term)}</div><p class="kw-note">${esc(p.reason || kwT("kw.failed", "Could not add this keyword."))}</p>` + kwIgnoreBtn());
      return;
    }
    const ev = p.evidence || {};
    const beforeBlock = p.kind === "new"
      ? `<div><div class="kw-lbl">${esc(kwT("kw.newBullet", "New bullet under {label}").replace("{label}", p.label || ""))}</div></div>`
      : `<div><div class="kw-lbl">${esc(kwT("kw.before", "Before"))}</div><div class="kw-line">${esc(p.before)}</div></div>`;
    kwCard(`<div class="kw-term">${esc(term)}</div>`
      + `<div class="kw-ev">${esc(kwT("kw.evidence", "From your profile"))}: <b>${esc(ev.label || p.label || "")}</b> &ldquo;${esc(ev.quote || "")}&rdquo;</div>`
      + beforeBlock
      + `<div><div class="kw-lbl">${esc(kwT("kw.after", "After"))}</div><div class="kw-line after">${esc(p.draft)}</div></div>`
      + `<div class="kw-actions"><button class="btn btn-sm" type="button" id="kwAccept">${esc(kwT("kw.accept", "Accept"))}</button>`
      + `<button class="btn btn-ghost btn-sm" type="button" id="kwIgnore">${esc(kwT("kw.ignore", "Ignore"))}</button></div>`);
  }

  async function kwApply() {
    const p = KW_PENDING;
    if (!p || !p.ok) return;
    const btn = $("#kwAccept");
    if (btn) { btn.disabled = true; btn.textContent = kwT("kw.working", "Working"); }
    let st;
    try { st = await api("/api/cv/live/keyword/apply", { term: p.term, draft: p.draft }); }
    catch (err) {
      kwCard(`<div class="kw-term">${esc(p.term)}</div><p class="kw-note">${esc(err.message || kwT("kw.failed", "Could not add this keyword."))}</p>` + kwIgnoreBtn());
      return;
    }
    if (!st.ok) {
      kwCard(`<div class="kw-term">${esc(p.term)}</div><p class="kw-note">${esc(st.reason || kwT("kw.failed", "Could not add this keyword."))}</p>` + kwIgnoreBtn());
      return;
    }
    KW_PENDING = null;
    kwCard("");
    applyState(st);           // new PDF, new coverage, the term now under "In resume" with undo
  }

  async function kwUndo(term) {
    let st;
    try { st = await api("/api/cv/live/keyword/undo", { term }); }
    catch (err) {
      kwCard(`<div class="kw-term">${esc(term)}</div><p class="kw-note">${esc(err.message || kwT("kw.failed", "Could not add this keyword."))}</p>` + kwIgnoreBtn());
      return;
    }
    kwCard("");
    applyState(st);
  }

  $("#covDetail").addEventListener("click", (e) => {
    const add = e.target.closest(".kw-add");
    const undo = e.target.closest(".kw-undo");
    if (add) { e.preventDefault(); kwPreview(add.dataset.term, add); }
    else if (undo) { e.preventDefault(); kwUndo(undo.dataset.term); }
  });
  $("#kwCard")?.addEventListener("click", (e) => {
    if (e.target.closest("#kwAccept")) kwApply();
    else if (e.target.closest("#kwIgnore")) { KW_PENDING = null; kwCard(""); }
  });

  /* The preview is ALWAYS the real compiled PDF (zero drift). "Edit bullets" opens a
     secondary editor; each edit re-renders the PDF, so what you download is what the
     PDF shows. */
  function showPdf() {
    $("#pdfPlaceholder").hidden = true;
    $("#paperWrap").hidden = true;
    $("#pdfFrame").hidden = false;
    $("#editToggle").classList.remove("is-active");
    $("#editToggle").textContent = "Edit bullets";
  }
  function showEdit() {
    $("#pdfPlaceholder").hidden = true;
    $("#pdfFrame").hidden = true;
    $("#paperWrap").hidden = false;
    $("#editToggle").classList.add("is-active");
    $("#editToggle").textContent = "Done editing";
  }
  $("#editToggle").addEventListener("click", () => {
    if ($("#paperWrap").hidden) showEdit(); else showPdf();
  });

  /* ---------------------------------------------------------------- flow control */
  function resetBuilder() {
    chat.innerHTML = ""; setStep("details"); setCoverage(0);
    $("#coverage").hidden = true; $("#editToggle").hidden = true;
    $("#covDetail").hidden = true;
    $("#acceptBtn").disabled = true;
    $("#downloadBtn").hidden = true;
    $("#acceptNote").textContent = "Answer a few questions and I’ll draft this for you.";
    $("#pdfFrame").removeAttribute("src");
    // Before a build there's no PDF yet, show the placeholder, never a drifting HTML CV.
    $("#pdfPlaceholder").hidden = false;
    $("#pdfFrame").hidden = true; $("#paperWrap").hidden = true;
    $("#builderRole").textContent = "New Resume";
    renderPreview({});
  }

  // The template chosen in the picker (null = server default). Drives the New CV session.
  let SELECTED_TEMPLATE = null;

  // Restore a CV that's already in progress. The server has held it in _SESSION the whole
  // time; only the browser forgot. Without this, leaving the builder and returning wiped
  // the work ("it disappears like we were not working on anything").
  async function restoreSession() {
    let st;
    try { st = await api("/api/session/state"); } catch { return false; }
    if (!st || !st.active) return false;
    resetBuilder();
    $("#jdGate").hidden = true;          // a session exists, don't ask for the JD again
    (st.history || []).forEach(t => bubble(t.role === "user" ? "user" : "agent",
                                           chatText(t.content || "")));
    applyState(st);
    return true;
  }

  // The server picks the template from the JD it just read, so nothing is asked up front.
  // This names what it used, in the header, where it's visible without being a decision.
  function paintTemplateUsed(st) {
    const wrap = $("#builderTpl");
    if (!wrap) return;
    const label = st && st.template_label;
    if (!label) { wrap.hidden = true; return; }
    $("#builderTplName").textContent = label;
    wrap.hidden = false;
  }

  // New CV means NEW. Work in progress is reached from the sidebar entry, so this never
  // has to guess which of the two the person meant.
  function openBuilder(template) {
    if (typeof template === "string") SELECTED_TEMPLATE = template;
    if (AGENT.on) setAgentMode(false);   // the builder's own header and composer are back
    resetBuilder();
    showView("builder");
    $("#jdInput").value = "";
    $("#jdGate").hidden = false;
    $("#jdInput").focus();
  }

  async function resumeBuilder() {
    if (AGENT.on) setAgentMode(false);
    showView("builder");
    if (!(await restoreSession())) openBuilder();
  }

  // Template-first: New Resume opens the template gallery (choose a look, preview it),
  // then "Use this template" leads to the JD. The home prompt above stays JD-first for
  // when you already have the posting in hand.
  $("#newCvBtn").addEventListener("click", () => openTemplates("newcv"));
  // "change" goes to the picker, where "Use this template" comes straight back here.
  $("#builderTplChange")?.addEventListener("click", () => openTemplates("builder"));
  $("#jdCancel").addEventListener("click", () => showView("dashboard"));
  $("#backBtn").addEventListener("click", () => {
    if (AGENT.on) setAgentMode(false);   // Back from the assistant returns Home too
    loadDashboard(); showView("dashboard");
  });

  $("#jdStart").addEventListener("click", async () => {
    const jd = $("#jdInput").value.trim();
    if (!jd) { $("#jdInput").focus(); return; }
    $("#jdGate").hidden = true;
    await withWorking(async () => {
      applyState(await api("/api/session/start", { jd, template: SELECTED_TEMPLATE }));
      $("#composerInput").focus();
    }, "Reading the job description…");
  });

  /* ---------------------------------------------------------------- template picker */
  // Group by who each template is FOR. The research is unanimous that ATS-safe CVs all
  // converge on the same single-column shape, so asking someone to choose on looks is
  // asking them to judge a difference that isn't there. The label carries the real one.
  function groupByCategory(templates) {
    const byCat = new Map();
    templates.forEach(t => {
      const c = t.category || "General";
      if (!byCat.has(c)) byCat.set(c, []);
      byCat.get(c).push(t);
    });
    return byCat;
  }

  async function loadTemplates() {
    let d = { templates: [], default: "shetty" };
    try { d = await api("/api/templates"); } catch { /* keep empty */ }
    CV_TEMPLATES = d.templates || [];
    // Word's "More templates" page: a muted heading per group, then a wide grid of cards.
    // Grouped by WHO the template is for (its manifest category), in manifest order.
    const groups = new Map();
    CV_TEMPLATES.forEach(t => {
      const k = t.category || "General";
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(t);
    });
    $("#tplGrid").innerHTML = [...groups].map(([cat, list]) =>
      `<section class="tpl-group"><h2 class="tpl-group-h">${esc(cat)}</h2>
        <div class="tpl-grid">${list.map(cvsTplCard).join("")}</div></section>`).join("")
      || `<div class="empty">No templates found.</div>`;
    document.querySelectorAll("#tplGrid [data-use]").forEach(el =>
      el.addEventListener("click", () => openTplPreview(el.dataset.use)));
  }

  /* ---- Template preview: look before you commit --------------------------------
     Clicking a template used to jump straight into "paste a JD", giving no chance to
     inspect the layout first. Research on gallery/thumbnail UX is consistent: a click
     should PREVIEW, and committing (here, "Use this template") is a separate explicit
     step. So a click opens this lightbox with the full sample render; Esc / backdrop /
     the X dismiss it, and the arrows (or ArrowLeft/Right) compare neighbours without
     leaving. */
  let TPL_IDX = -1;
  function openTplPreview(name) {
    const i = CV_TEMPLATES.findIndex(t => t.name === name);
    if (i < 0) { openBuilder(name); return; }   // no metadata to preview: old path
    TPL_IDX = i;
    // Show the window FIRST, then load the PDF: the viewer sizes its page to the frame it
    // starts in, and a frame inside a hidden window is 0 x 0, so the page came up at a tiny
    // zoom in a sea of grey (Kofi, 2026-10-26). Stepping with the arrows never had the bug,
    // because the window was already on screen.
    $("#tplModal").hidden = false;
    renderTplModal();
    document.addEventListener("keydown", tplModalKeys);
    $("#tplModalUse").focus();
  }
  function renderTplModal() {
    const t = CV_TEMPLATES[TPL_IDX];
    if (!t) return;
    $("#tplModalName").textContent = t.display_name || t.name;
    // The sample is an IMAGE rendered by the engine, not the PDF in Chromium's viewer: inside a
    // frame that viewer ignored every fit option (FitH, Fit, page-width all drew the page at
    // three quarters of the frame in a sea of grey) and only honoured a zoom percentage, which
    // differs per screen. Asked at about twice the paper's width, so it is sharp on Retina.
    // Two frames after unhiding, so the paper has its real size to measure.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const img = $("#tplModalFrame");
      if ($("#tplModal").hidden || CV_TEMPLATES[TPL_IDX] !== t || !img) return;
      const w = Math.ceil((img.clientWidth || 600) * Math.min(2, window.devicePixelRatio || 1) / 100) * 100;
      img.src = `/api/templates/${encodeURIComponent(t.name)}/thumb.png?w=${w}`;
    }));
    $("#tplModalPos").textContent = CV_TEMPLATES.length > 1
      ? `${TPL_IDX + 1} of ${CV_TEMPLATES.length}` : "";
    const solo = CV_TEMPLATES.length <= 1;
    $("#tplPrev").hidden = solo;
    $("#tplNext").hidden = solo;
  }
  function tplStep(delta) {
    if (CV_TEMPLATES.length < 2) return;
    TPL_IDX = (TPL_IDX + delta + CV_TEMPLATES.length) % CV_TEMPLATES.length;
    renderTplModal();
  }
  function closeTplPreview() {
    $("#tplModal").hidden = true;
    $("#tplModalFrame").src = "about:blank";   // stop the background PDF rendering
    document.removeEventListener("keydown", tplModalKeys);
  }
  function useTplFromPreview() {
    const t = CV_TEMPLATES[TPL_IDX];
    closeTplPreview();
    if (t) openBuilder(t.name);
  }
  function tplModalKeys(e) {
    if (e.key === "Escape") closeTplPreview();
    else if (e.key === "ArrowLeft") tplStep(-1);
    else if (e.key === "ArrowRight") tplStep(1);
    else if (e.key === "Enter") { e.preventDefault(); useTplFromPreview(); }
  }
  $("#tplModalUse")?.addEventListener("click", useTplFromPreview);
  $("#tplModalX")?.addEventListener("click", closeTplPreview);
  $("#tplPrev")?.addEventListener("click", () => tplStep(-1));
  $("#tplNext")?.addEventListener("click", () => tplStep(1));
  // Click on the backdrop (outside the card) closes; clicks inside do not.
  $("#tplModal")?.addEventListener("click", (e) => { if (e.target.id === "tplModal") closeTplPreview(); });

  // No section chips: the preview above already SHOWS the sections and the name already
  // states the shape, redundant secondary metadata that users scan straight past
  // (progressive disclosure: it stays available in "Open preview"). best_for earns its
  // place because it says who the template is for, which the preview cannot show.
  const tplCard = (t) => `<div class="tpl-card" role="button" tabindex="0" data-use="${esc(t.name)}" title="${esc(t.best_for || t.display_name)}">
      <div class="tpl-prev"><img class="tpl-preview" loading="lazy" draggable="false"
        alt="${esc(t.display_name)} layout" src="/api/templates/${encodeURIComponent(t.name)}/thumb.png?w=600"></div>
      <div class="tpl-name">${esc(t.display_name)}</div>
    </div>`;

  const composerInput = $("#composerInput");
  function autoGrow() {
    composerInput.style.height = "auto";
    composerInput.style.height = Math.min(composerInput.scrollHeight, 160) + "px";
  }
  composerInput.addEventListener("input", autoGrow);
  // Enter submits; Shift+Enter inserts a newline (for multi-row paste/typing).
  composerInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("#composer").requestSubmit();
    }
  });

  let composerBusy = false;   // the session is single-threaded state; block concurrent turns
  async function sendTurn(text, echoHtml) {
    if (composerBusy || !text) return;
    composerBusy = true;
    if (echoHtml) bubble("user", echoHtml);
    try {
      await withWorking(async () => applyState(await api("/api/session/answer", { text })), "…");
    } finally { composerBusy = false; }
  }
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const val = composerInput.value.trim();
    if (!val) return;
    composerInput.value = ""; autoGrow();
    if (AGENT.on) { agentAsk(val); return; }       // assistant mode: not a resume-session turn
    sendTurn(val, esc(val).replace(/\n/g, "<br>"));
  });

  /* ---------------------------------------------------------------- assistant (Home agent)
     The same chat surface as the resume builder, with the header and composer relabelled.
     Nothing here touches the resume session: a reply is a bubble plus buttons, and Go ahead
     runs the one guarded Auto-apply run on the server. Back returns Home and restores the
     builder's own header (openBuilder/resumeBuilder call setAgentMode(false) first). */
  const AGENT = { on: false, change: false, busy: false };
  const agentT = (k, f) => (window.I18N ? I18N.t(k, f) : f);
  // The builder's own words, kept from the page so leaving assistant mode restores them exactly.
  const BUILDER_WORDS = {
    kicker: $("#view-builder .builder-kicker")?.textContent || "New Resume",
    sub: $("#view-builder .chat-head-sub")?.textContent || "",
    placeholder: composerInput.placeholder,
  };
  function setAgentMode(on) {
    AGENT.on = on; AGENT.change = false;
    const view = $("#view-builder");
    view.classList.toggle("agent-mode", on);
    const kicker = view.querySelector(".builder-kicker");
    if (kicker) kicker.textContent = on ? agentT("agent.kicker", "Assistant") : BUILDER_WORDS.kicker;
    $("#builderRole").textContent = on ? "" : "New Resume";
    $("#steps").hidden = on;
    $("#builderTpl").hidden = true;
    const sub = view.querySelector(".chat-head-sub");
    if (sub) sub.textContent = on ? agentT("agent.sub", "Your assistant. Ask for a search, a resume, or an update.")
                                  : BUILDER_WORDS.sub;
    composerInput.placeholder = on ? agentT("agent.placeholder", "Ask for a search, a resume, or an update.")
                                   : BUILDER_WORDS.placeholder;
    $("#uploadBtn").hidden = on;
  }
  function openAgent() {
    resetBuilder();
    setAgentMode(true);
    $("#jdGate").hidden = true;
    showView("builder");
    composerInput.focus();
  }
  // A reply: the text as a bubble (plain text, escaped), then its buttons underneath.
  function agentReply(r) {
    bubble("agent", chatText(r.text || ""));
    const acts = (r.buttons || []).filter(b => b && b.id);
    if (!acts.length) return;
    const wrap = el("div", "agent-acts");
    const labels = { go: agentT("agent.go", "Go ahead"), change: agentT("agent.change", "Change"),
                     open_autoapply: agentT("agent.open", "Open Auto-apply"),
                     find: agentT("agent.find", "Find matching roles"),
                     add_resume: agentT("agent.addResume", "Add my resume"),
                     add_details: agentT("agent.addDetails", "Add the details") };
    acts.forEach(b => {
      const btn = el("button", "btn btn-sm " + (b.id === "go" ? "btn-primary" : "btn-ghost"));
      btn.type = "button"; btn.textContent = labels[b.id] || b.label || b.id;
      btn.addEventListener("click", () => { wrap.remove(); agentAct(b.id, r); });
      wrap.appendChild(btn);
    });
    chat.appendChild(wrap); chat.scrollTop = chat.scrollHeight;
  }
  async function agentAct(id, r) {
    if (id === "go") {
      bubble("user", esc(agentT("agent.go", "Go ahead")));
      await withWorking(async () => agentReply(await api("/api/agent/go", {})),
                        agentT("agent.tailoring", "Tailoring. This takes a few minutes."));
    } else if (id === "change") {
      bubble("agent", esc(agentT("agent.changeQ", "What should I change? The titles, how many, or where.")));
      AGENT.change = true; composerInput.focus();
    } else if (id === "open_autoapply") {
      document.querySelector('[data-nav="autoapply"]')?.click();
    } else if (id === "add_resume") {
      document.querySelector('[data-nav="cvs"]')?.click();
    } else if (id === "add_details") {
      openProfileDetails();   // the same details step as Auto-apply's "Add the details"
    } else if (id === "find") {
      const titles = ((r && r.intent && r.intent.titles) || []).join(", ");
      agentAsk(titles ? `Find ${titles} roles for me` : "Find roles for me");
    }
  }
  // One turn: the person's words, a working bubble, the reply. Returns the server answer so
  // the Home box can fall back to a tailoring session when the server read a posting.
  async function agentAsk(text) {
    if (AGENT.busy || !text) return null;
    AGENT.busy = true;
    bubble("user", esc(text).replace(/\n/g, "<br>"));
    const change = AGENT.change; AGENT.change = false;
    let out = null;
    try {
      await withWorking(async () => {
        out = await api("/api/agent/ask", { text, change });
        if (out.kind === "agent") agentReply(out);
      }, agentT("agent.thinking", "Thinking..."));
    } finally { AGENT.busy = false; }
    return out;
  }

  /* ONE upload button, any document (CV, transcript, or certificate). Local
     extraction + OCR; the server reads it and figures out what it is. */
  $("#uploadBtn").addEventListener("click", () => $("#uploadFile").click());
  $("#uploadFile").addEventListener("change", async (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = "";                 // allow re-uploading the same file
    if (!file) return;
    bubble("user", "📄 " + esc(file.name));
    const form = new FormData();
    form.append("file", file);
    await withWorking(async () => {
      const res = await fetch("/api/session/upload", { method: "POST", body: form });
      const st = await res.json();
      if (!res.ok) { bubble("agent", esc(st.error || "Couldn't read that file.")); return; }
      applyState(st);
    }, "Reading your document…");
  });

  $("#acceptBtn").addEventListener("click", async () => {
    $("#acceptBtn").disabled = true;
    try {
      await withWorking(async () => {
        const r = await api("/api/session/accept", {});   // {} forces a POST
        if (r && r.ok === false) {   // blocked by a flagged placeholder project
          bubble("agent", esc(r.reason || "Resolve the flagged placeholder before saving."));
          return;
        }
        await loadDashboard();
        showView("dashboard");
      }, "Saving your resume…");
    } finally {
      // Always re-enable, otherwise any transient failure leaves Accept dead forever.
      $("#acceptBtn").disabled = false;
    }
  });

  /* ---------------------------------------------------------------- my profile */
  const ID_FIELDS = {
    name: "idName", email: "idEmail", phone: "idPhone", address: "idAddress",
    linkedin: "idLinkedin", github: "idGithub", blog: "idBlog",
  };
  const ID_LABELS = {
    name: "Name", email: "Email", phone: "Phone", address: "Address",
    linkedin: "LinkedIn", github: "GitHub", blog: "Website",
  };
  let PROF_IDENTITY = {};   // last-loaded identity, so Cancel can restore without a refetch

  // The contact card wears two faces: a read-only "here's what I know" summary (the
  // default, so the profile feels like memory, not a form) and the inputs, revealed
  // only when you click Edit to correct something.
  function renderIdentityKnown(identity) {
    const id = identity || {};
    const isUrl = k => k === "linkedin" || k === "github" || k === "blog";
    const known = Object.keys(ID_FIELDS).filter(k => (id[k] || "").trim());
    const rows = known.map(k => {
      const v = id[k].trim();
      const val = isUrl(k)
        ? `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a>`
        : esc(v);
      return `<div class="prof-known-row"><span class="prof-known-k">${ID_LABELS[k]}</span>`
        + `<span class="prof-known-v">${val}</span></div>`;
    });
    const missing = Object.keys(ID_FIELDS).filter(k => !(id[k] || "").trim());
    let html = rows.join("");
    if (!rows.length) {
      html = `<p class="prof-note">Nothing here yet, upload your resume and I'll fill it in.</p>`;
    } else if (missing.length) {
      html += `<p class="prof-note">I'll ask for the rest (${missing.map(k => (k === "linkedin" || k === "github") ? ID_LABELS[k] : ID_LABELS[k].toLowerCase()).join(", ")}) if a resume needs it.</p>`;
    }
    $("#idKnown").innerHTML = html;
  }

  function setIdentityMode(editing) {
    $("#identityForm").hidden = !editing;
    $("#idKnown").hidden = editing;
  }

  function profSection(title, html) {
    return `<div class="prof-card"><div class="prof-card-h">${esc(title)}</div>${html}</div>`;
  }

  /* ------------------------------------------------- editable profile sections
     The sections used to be read-only ("edited while you tailor a CV"), which left
     bad saved data unfixable without a whole CV session, the Amazon case study
     needed SQL surgery three times (obs #15). Now the profile screen IS the editor:
     every section is inputs, Save writes both saved bags atomically server-side. */
  let PROF_STATE = null;

  function peInput(sec, i, key, val, ph, wide) {
    return `<input class="fld pe-in${wide ? " pe-wide" : ""}" data-sec="${sec}" data-i="${i}" data-k="${key}"
            value="${esc(val || "")}" placeholder="${esc(ph)}" spellcheck="false">`;
  }

  function peBullets(sec, i, bullets) {
    return `<textarea class="fld pe-bullets" data-sec="${sec}" data-i="${i}" data-k="bullets" rows="3"
            placeholder="One bullet per line…" spellcheck="false">${esc((bullets || []).join("\n"))}</textarea>`;
  }

  function peRowShell(sec, i, inner) {
    return `<div class="pe-row">${inner}
      <button class="btn btn-ghost btn-sm pe-del" data-del="${sec}" data-i="${i}" type="button" title="Remove">Remove</button>
    </div>`;
  }

  function buildProfileEditor(p) {
    const flat = [];
    (p.experience || []).forEach(e => (e.roles || [e]).forEach(r => flat.push({
      org: e.org || e.company || "", title: r.title || "", dates: r.dates || e.dates || "",
      location: e.location || "", bullets: r.bullets || [],
    })));
    PROF_STATE = {
      summary: p.summary || "",
      experience: flat,
      education: (p.education || []).map(ed => ({
        school: ed.school || "", degree: ed.degree || "", date: ed.date || "",
        location: ed.location || "",
        courses: typeof ed.courses === "string" ? ed.courses : (ed.courses || []).join(", "),
      })),
      projects: (p.projects || []).map(pr => ({
        org: pr.org || pr.title || "", link: pr.link || pr.url || "",
        location: pr.location || "", dates: pr.dates || "", bullets: pr.bullets || [],
      })),
      extracurricular: (p.extracurricular || []).map(x => ({
        title: x.title || "", date: x.date || "", bullets: x.bullets || [],
      })),
      interests: p.interests || "",
      skills_input: p.skills_input || [],
      declined: p.declined || [],
    };
    renderProfileEditor();
  }

  function renderProfileEditor() {
    const s = PROF_STATE;
    const card = (title, body, addSec) => `<div class="prof-card pe-card">
      <div class="prof-card-h">${title}${addSec ? `<button class="btn btn-ghost btn-sm pe-add" data-add="${addSec}" type="button">+ Add</button>` : ""}</div>
      ${body}</div>`;

    const parts = [];
    parts.push(card("Summary",
      `<textarea class="fld pe-bullets" data-sec="summary" rows="3"
        placeholder="A short professional summary (summary-led templates open with it)…"
        spellcheck="false">${esc(s.summary)}</textarea>`));
    parts.push(card("Experience", s.experience.map((r, i) => peRowShell("experience", i,
      peInput("experience", i, "org", r.org, "Company") +
      peInput("experience", i, "title", r.title, "Job title") +
      peInput("experience", i, "dates", r.dates, "Feb 2018 - May 2020") +
      peInput("experience", i, "location", r.location, "City, ST") +
      peBullets("experience", i, r.bullets))).join(""), "experience"));
    parts.push(card("Education", s.education.map((ed, i) => peRowShell("education", i,
      peInput("education", i, "school", ed.school, "School") +
      peInput("education", i, "degree", ed.degree, "Degree") +
      peInput("education", i, "date", ed.date, "Sept 2024 - Dec 2025") +
      peInput("education", i, "location", ed.location, "City, ST") +
      peInput("education", i, "courses", ed.courses, "Courses (comma-separated)", true))).join(""), "education"));
    parts.push(card("Projects", s.projects.map((pr, i) => peRowShell("projects", i,
      peInput("projects", i, "org", pr.org, "Project name") +
      peInput("projects", i, "link", pr.link, "https://github.com/…") +
      peInput("projects", i, "location", pr.location, "Tech (shows right-aligned)") +
      peInput("projects", i, "dates", pr.dates, "Dates (optional)") +
      peBullets("projects", i, pr.bullets))).join(""), "projects"));
    parts.push(card("Extracurricular", s.extracurricular.map((x, i) => peRowShell("extracurricular", i,
      peInput("extracurricular", i, "title", x.title, "Team Captain, Intramural Soccer") +
      peInput("extracurricular", i, "date", x.date, "Dates (optional)") +
      peBullets("extracurricular", i, x.bullets))).join(""), "extracurricular"));
    parts.push(card("Interests",
      `<input class="fld pe-in pe-wide" data-sec="interests" value="${esc(s.interests)}"
        placeholder="Competitive soccer and basketball; building apps…" spellcheck="false">`));
    parts.push(card("Stated skills",
      `<textarea class="fld pe-bullets" data-sec="skills_input" rows="3" spellcheck="false"
        placeholder="Comma-separated skills, the Skills section is packed from these at build…">${esc(s.skills_input.join(", "))}</textarea>`));
    parts.push(card("Skipped on purpose",
      `<div class="pe-declined">` +
      ["github", "blog", "linkedin", "projects", "courses", "address", "extracurricular", "interests"]
        .map(k => `<label class="pe-dec"><input type="checkbox" data-dec="${k}"
          ${s.declined.includes(k) ? "checked" : ""}> ${k}</label>`).join("") +
      `</div><p class="prof-note">Checked items are never asked about again.</p>`));
    parts.push(`<div class="pe-foot">
      <button class="btn btn-primary" id="peSave" type="button">Save profile sections</button>
      <span class="prof-saved" id="peSaved"></span></div>`);

    const root = $("#profSections");
    root.innerHTML = parts.join("");

    root.querySelectorAll(".pe-in, .pe-bullets").forEach(el => el.addEventListener("input", () => {
      const sec = el.dataset.sec, i = el.dataset.i, k = el.dataset.k;
      const val = el.value;
      if (sec === "summary") PROF_STATE.summary = val;
      else if (sec === "interests") PROF_STATE.interests = val;
      else if (sec === "skills_input") PROF_STATE.skills_input = val.split(",").map(x => x.trim()).filter(Boolean);
      else if (k === "bullets") PROF_STATE[sec][i].bullets = val.split("\n").map(x => x.trim()).filter(Boolean);
      else PROF_STATE[sec][i][k] = val;
    }));
    root.querySelectorAll("[data-dec]").forEach(cb => cb.addEventListener("change", () => {
      const k = cb.dataset.dec;
      PROF_STATE.declined = PROF_STATE.declined.filter(d => d !== k);
      if (cb.checked) PROF_STATE.declined.push(k);
    }));
    root.querySelectorAll(".pe-del").forEach(b => b.addEventListener("click", () => {
      PROF_STATE[b.dataset.del].splice(Number(b.dataset.i), 1);
      renderProfileEditor();
    }));
    root.querySelectorAll(".pe-add").forEach(b => b.addEventListener("click", () => {
      const blank = { experience: { org: "", title: "", dates: "", location: "", bullets: [] },
                      education: { school: "", degree: "", date: "", location: "", courses: "" },
                      projects: { org: "", link: "", location: "", dates: "", bullets: [] },
                      extracurricular: { title: "", date: "", bullets: [] } }[b.dataset.add];
      PROF_STATE[b.dataset.add].push(blank);
      renderProfileEditor();
    }));
    $("#peSave").addEventListener("click", async () => {
      const btn = $("#peSave"), note = $("#peSaved");
      btn.disabled = true; note.textContent = "Saving…";
      try {
        const res = await fetch("/api/profile/sections", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(PROF_STATE),
        });
        const r = await res.json().catch(() => ({}));
        if (!res.ok || r.error) throw new Error(r.error || ("HTTP " + res.status));
        note.textContent = "Saved. Every new resume builds from this.";
        paintAccount(r);
      } catch (err) {
        note.textContent = err.message;
      } finally {
        btn.disabled = false;
      }
    });
  }

  function renderProfileSections(p) {
    const parts = [];
    const roleEntries = (e) => e.roles || [e];
    if ((p.experience || []).length) {
      parts.push(profSection("Experience", p.experience.map(e => {
        const org = esc(e.org || e.company || "");
        const meta = [e.location, e.dates].filter(Boolean).map(esc).join(" · ");
        const roles = roleEntries(e).map(r => {
          const bullets = (r.bullets || []).map(b => `<li>${esc(b)}</li>`).join("");
          return `<div class="prof-role"><b>${esc(r.title || "")}</b>${r.dates && r.dates !== e.dates ? ` <span class="prof-dim">${esc(r.dates)}</span>` : ""}${bullets ? `<ul>${bullets}</ul>` : ""}</div>`;
        }).join("");
        return `<div class="prof-entry"><div class="prof-entry-h"><span>${org}</span><span class="prof-dim">${meta}</span></div>${roles}</div>`;
      }).join("")));
    }
    if ((p.education || []).length) {
      parts.push(profSection("Education", p.education.map(ed =>
        `<div class="prof-entry"><div class="prof-entry-h"><span>${esc(ed.school || "")}</span><span class="prof-dim">${[ed.location, ed.date].filter(Boolean).map(esc).join(" · ")}</span></div>
         <div class="prof-dim">${esc(ed.degree || "")}</div>${ed.courses ? `<div class="prof-courses">${esc(typeof ed.courses === "string" ? ed.courses : (ed.courses || []).join(", "))}</div>` : ""}</div>`
      ).join("")));
    }
    const skills = p.skills || {};
    if (Object.keys(skills).length) {
      parts.push(profSection("Skills", `<div class="prof-skills">${Object.entries(skills).map(([k, v]) =>
        `<div class="prof-skline"><b>${esc(k)}:</b> ${esc(v)}</div>`).join("")}</div>`));
    }
    if ((p.projects || []).length) {
      parts.push(profSection("Projects", p.projects.map(pr =>
        `<div class="prof-entry"><div class="prof-entry-h"><span>${esc(pr.title || pr.org || "")}</span><span class="prof-dim">${esc(pr.dates || "")}</span></div>
         ${(pr.bullets || []).length ? `<ul>${pr.bullets.map(b => `<li>${esc(b)}</li>`).join("")}</ul>` : ""}</div>`
      ).join("")));
    }
    if ((p.extracurricular || []).length) {
      parts.push(profSection("Extracurricular", p.extracurricular.map(x =>
        `<div class="prof-entry"><div class="prof-entry-h"><span>${esc(x.title || "")}</span><span class="prof-dim">${esc(x.date || "")}</span></div>
         ${(x.bullets || []).length ? `<ul>${x.bullets.map(b => `<li>${esc(b)}</li>`).join("")}</ul>` : ""}</div>`
      ).join("")));
    }
    if ((p.interests || "").trim()) {
      parts.push(profSection("Interests", `<div class="prof-dim">${esc(p.interests)}</div>`));
    }
    return parts.join("");
  }

  /* The sidebar-foot identity. Signed in, the foot shows the account's email and the menu head
     shows the profile name over that email; signed out, the profile name (or "Your profile") and
     "Stored on this computer", with "Sign in" in the menu. `var` on purpose: paintAccount can run
     before the account block below has set these. */
  var ACCT_EMAIL = null, ACCT_PLAN = null, ACCT_PROFILE_NAME = "";
  function paintAccount(p) {
    ACCT_PROFILE_NAME = (((p || {}).identity || {}).name || "").trim();
    paintIdentity();
    loadPhoto();
  }
  function paintIdentity() {
    const t = (k, f) => (window.I18N ? I18N.t(k, f) : f);
    const generic = t("acct.profileName", "Your profile");
    const nameEl = $("#acctName");
    if (nameEl) { nameEl.textContent = ACCT_EMAIL || ACCT_PROFILE_NAME || generic; nameEl.title = ACCT_EMAIL || ""; }
    const mn = $("#acctMenuName"); if (mn) mn.textContent = ACCT_PROFILE_NAME || (ACCT_EMAIL ? t("acct.signedInAs", "Signed in") : generic);
    const sub = $("#acctMenuSub"); if (sub) sub.textContent = ACCT_EMAIL || t("acct.profileSub", "Stored on this computer");
    const si = $("#menuSignin"), so = $("#menuSignout");
    if (si) si.hidden = !!ACCT_EMAIL;
    if (so) so.hidden = !ACCT_EMAIL;
  }
  // The profile picture lives in the data folder and never leaves the machine. A missing photo
  // shows the silhouette placeholder; a cache-busting query makes a new upload show at once.
  function loadPhoto() {
    const img = $("#acctPhoto"); if (!img) return;
    const probe = new Image();
    probe.onload = () => { img.src = probe.src; img.hidden = false; };
    probe.onerror = () => { img.hidden = true; img.removeAttribute("src"); };
    probe.src = "/api/profile/photo?v=" + Date.now();
  }
  $("#menuPhoto")?.addEventListener("click", () => { closeAcctMenu(); $("#acctPhotoFile")?.click(); });
  $("#acctPhotoFile")?.addEventListener("change", async (e) => {
    const file = e.target.files[0]; if (!file) return;
    try {
      const fd = new FormData(); fd.append("file", file);
      const res = await fetch("/api/profile/photo", { method: "POST", body: fd });
      const r = await res.json().catch(() => ({}));
      if (!res.ok || r.error) throw new Error(r.error || ("HTTP " + res.status));
      loadPhoto(); toast("Photo updated.");
    } catch (err) { toast(err.message || "Couldn't save that photo."); }
    finally { e.target.value = ""; }
  });

  async function loadProfile() {
    let p;
    try { p = await api("/api/profile"); }
    catch { p = { has_profile: false, identity: {} }; }
    paintAccount(p);
    PROF_IDENTITY = p.identity || {};
    for (const [key, id] of Object.entries(ID_FIELDS)) $("#" + id).value = PROF_IDENTITY[key] || "";
    renderIdentityKnown(PROF_IDENTITY);
    // Read-only by default when we already know something; brand-new users see the
    // inputs directly so they're never stuck behind an Edit click.
    const hasIdentity = Object.keys(ID_FIELDS).some(k => (PROF_IDENTITY[k] || "").trim());
    $("#idEdit").hidden = !hasIdentity;
    setIdentityMode(!hasIdentity);
    buildProfileEditor(p);
    $("#profEmpty").hidden = !!p.has_profile;
    $("#profSecNote").hidden = true;
    $("#idSaved").textContent = "";
    paintAnswers(p.answers || {});
    loadLinkedinReview();
  }

  /* ---- LinkedIn review: the extension reads the person's OWN profile page while they have
     it open (read-only, never clicks or edits) and the app scores it. This paints the stored
     review: score ring, 14 sections with a reason and a fix, the drafted headline / About with
     Copy. "Re-score" re-runs the score on the stored page and writes the two drafts (one model
     call). The person applies every change on LinkedIn by hand. */
  const LI_RING_C = 2 * Math.PI * 52;
  function paintLinkedinReview(d) {
    const has = !!(d && d.has_review);
    $("#liEmpty").hidden = has;
    $("#liBody").hidden = !has;
    $("#liRescore").hidden = !has;
    if (!has) return;
    const score = Math.max(0, Math.min(100, Number(d.score) || 0));
    $("#liScore").textContent = String(score);
    $("#liRingFg").style.strokeDashoffset = String(LI_RING_C * (1 - score / 100));
    const who = $("#liWho");
    who.innerHTML = d.url
      ? `<a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.name || d.url)}</a>`
      : esc(d.name || "");
    const role = (d.review && d.review.target_role) || "";
    $("#liTarget").textContent = role
      ? I18N.t("li.target", "Scored for: {role}").replace("{role}", role) : "";
    $("#liAt").textContent = d.at
      ? I18N.t("li.scored", "Scored {when}").replace("{when}", new Date(d.at * 1000).toLocaleString()) : "";
    const rows = ((d.review && d.review.sections) || []).map(sec => {
      const sc = Math.max(0, Math.min(100, Number(sec.score) || 0));
      return `<div class="li-row">`
        + `<div class="li-row-l">${esc(sec.label)}<span class="li-row-w">${esc(String(sec.weight))} ${esc(I18N.t("li.weight", "pts"))}</span></div>`
        + `<div class="li-bar"><i style="width:${sc}%"></i></div>`
        + `<div class="li-row-s">${sc}/100</div>`
        + `<div class="li-row-why">${esc(sec.reason)} <b>${esc(I18N.t("li.fix", "Fix:"))}</b> ${esc(sec.fix)}</div>`
        + `</div>`;
    });
    $("#liSections").innerHTML = rows.join("");
    const dr = d.drafts || null;
    $("#liDrafts").hidden = !dr;
    if (dr) {
      $("#liHeadline").textContent = dr.headline || "";
      $("#liAbout").textContent = dr.about || "";
      const flags = (dr.flags || []).map(f => `${f.field}: ${(f.figures || []).join(", ")}`).join("; ");
      $("#liFlags").hidden = !flags;
      $("#liFlags").textContent = flags
        ? I18N.t("li.flags", "Check these figures before you paste, they are not in your saved profile: {list}").replace("{list}", flags) : "";
    }
    const note = $("#liNote");
    const msg = d.draft_error || (!dr ? I18N.t("li.noDrafts", "Press Re-score to draft a headline and About from your saved profile.") : "");
    note.hidden = !msg;
    note.textContent = msg;
  }
  async function loadLinkedinReview() {
    let d = null;
    try { d = await api("/api/profile/linkedin-review"); } catch { d = null; }
    paintLinkedinReview(d);
  }
  $("#liRescore").addEventListener("click", async () => {
    const btn = $("#liRescore");
    btn.disabled = true;
    const was = btn.textContent;
    btn.textContent = I18N.t("li.rescoring", "Scoring...");
    try { paintLinkedinReview(await api("/api/profile/linkedin-review", {})); }
    catch (e) { const n = $("#liNote"); n.hidden = false; n.textContent = e.message || String(e); }
    btn.disabled = false;
    btn.textContent = was;
  });
  document.querySelectorAll(".li-copy").forEach(b => b.addEventListener("click", async () => {
    const txt = $("#" + b.dataset.copy).textContent;
    const was = b.textContent;
    try { await navigator.clipboard.writeText(txt); b.textContent = I18N.t("li.copied", "Copied"); }
    catch { b.textContent = I18N.t("li.copyFailed", "Select and copy"); }
    setTimeout(() => { b.textContent = was; }, 1800);
  }));

  /* ---- application answers: the profile card is a door to Your answers (openAnswers). */
  function paintAnswers(a) {
    const el = $("#profAnsSum");
    if (!el) return;
    const bits = [];
    if (a.work_auth_us) bits.push(I18N.t("ans.sumAuth", "Authorized in the US: {v}").replace("{v}", a.work_auth_us === "Yes" ? I18N.t("ans.yes", "Yes") : I18N.t("ans.no", "No")));
    if (a.needs_sponsorship) bits.push(I18N.t("ans.sumSponsor", "Sponsorship: {v}").replace("{v}",
      { Yes: I18N.t("ans.sponsorNowShort", "now"), Future: I18N.t("ans.sponsorFutureShort", "in the future"), No: I18N.t("ans.no", "No") }[a.needs_sponsorship] || ""));
    el.textContent = bits.length ? bits.join(" · ") : I18N.t("ans.sub", "Answer once. Every application reuses it.");
  }
  $("#profAnsOpen")?.addEventListener("click", () => openAnswers("profile"));

  $("#idEdit").addEventListener("click", () => { setIdentityMode(true); $("#idEdit").hidden = true; });
  $("#idCancel").addEventListener("click", () => {
    for (const [key, id] of Object.entries(ID_FIELDS)) $("#" + id).value = PROF_IDENTITY[key] || "";
    setIdentityMode(false);
    $("#idEdit").hidden = false;
    $("#idSaved").textContent = "";
  });

  // Profile-first: drop a resume, we parse it into the whole profile.
  $("#profCvFile")?.addEventListener("change", async e => {
    const file = e.target.files[0];
    if (!file) return;
    const msg = $("#profImportMsg"), btn = $("#profUploadBtn");
    msg.hidden = false; msg.className = "prof-import-msg working";
    msg.textContent = "Reading your resume and filling your profile…";
    if (btn) btn.textContent = "Reading…";
    try {
      const fd = new FormData(); fd.append("file", file);
      const res = await fetch("/api/profile/from_cv", { method: "POST", body: fd });
      const r = await res.json().catch(() => ({}));
      if (!res.ok || r.error) throw new Error(r.error || ("HTTP " + res.status));
      const s = r.summary || {};
      const plur = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
      msg.className = "prof-import-msg ok";
      msg.textContent = `Done${s.name ? ", " + s.name : ""}: pulled ${plur(s.roles || 0, "role")}, `
        + `${plur(s.skills || 0, "skill group")}, and ${plur(s.education || 0, "degree")}. `
        + "Review below and edit anything.";
      await loadProfile();
      invalidateJobDetail();   // match now reflects the freshly-imported profile
      if ($("#view-autoapply")?.classList.contains("is-active")) loadAutoApply();
      if ((r.missing || []).length) openProfileDetails(r.missing);   // the same step as the wizard
    } catch (err) {
      msg.className = "prof-import-msg err";
      msg.textContent = "Couldn't build your profile, " + err.message;
    } finally {
      if (btn) btn.textContent = "Upload resume";
      e.target.value = "";   // allow re-uploading the same file
    }
  });

  $("#identityForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {};
    for (const [key, id] of Object.entries(ID_FIELDS)) body[key] = $("#" + id).value.trim();
    try {
      await api("/api/profile/identity", body);
      invalidateJobDetail();   // identity feeds the match; keep the open role in sync
      PROF_IDENTITY = body;
      renderIdentityKnown(PROF_IDENTITY);
      setIdentityMode(false);          // drop back to the read-only "known" face
      $("#idEdit").hidden = false;
      $("#idSaved").textContent = "Saved, used by autofill, your resume letterhead, and cover letters.";
    } catch { $("#idSaved").textContent = "Couldn't save."; }
    setTimeout(() => { $("#idSaved").textContent = ""; }, 4000);
  });

  /* ------------------------------------------------------------- inbox (§5)
     Read-first: only application mail (verifications + recruiter replies) comes back. Lives on
     the Auto-apply page's Today panel (it was on the Review page). Buttons are the consent:
     Complete verification visits ONE link; replies are drafted for YOU to send from your own
     mail app. SponsorJobs never sends. The row shows only when Gmail can actually be used. */
  async function loadInboxStatus() {
    const row = $("#aaInboxRow"), st = $("#inboxStatus"), btn = $("#inboxScan");
    if (!row) return;
    try {
      const s = await api("/api/inbox/status");
      INBOX_CONNECTED = !!s.configured;
      // No Google libraries, or no OAuth client file: nothing the person can do from here, so
      // the row stays hidden rather than showing a setting that cannot work.
      row.hidden = !(s.libs && (s.configured || s.creds));
      st.textContent = s.configured
        ? T("aa.inbox.on", "Connected, read-only") + (s.auto_verify ? T("aa.inbox.auto", ", verifies for you") : "")
        : T("aa.inbox.first", "Scan once to sign in to Google");
      btn.disabled = false;
    } catch { row.hidden = true; }
  }

  function renderInbox(r) {
    const parts = [];
    if (r.verifications.length) {
      parts.push(`<div class="inbox-h">Email verifications (${r.verifications.length})</div>`);
      r.verifications.forEach((v, i) => {
        const done = v.auto && v.auto.ok;
        parts.push(`<div class="inbox-row">
          <div class="inbox-meta"><b>${esc(v.subject || "Verification")}</b> · ${esc(v.from)}</div>
          ${done ? `<span class="inbox-ok">Verified automatically</span>`
                 : v.link ? `<button class="btn btn-primary btn-sm" data-verify="${esc(v.link)}" type="button">Complete verification</button>`
                          : `<span class="prof-dim">No link found, open the email yourself.</span>`}
          <span class="prof-dim" data-verify-note="${i}"></span>
        </div>`);
      });
    }
    if (r.recruiters.length) {
      parts.push(`<div class="inbox-h">Recruiter replies (${r.recruiters.length})</div>`);
      r.recruiters.forEach((m, i) => {
        const mailto = "mailto:" + encodeURIComponent(m.from) +
          "?subject=" + encodeURIComponent("Re: " + (m.subject || "")) +
          "&body=" + encodeURIComponent(m.reply || "");
        parts.push(`<div class="inbox-row inbox-reply">
          <div class="inbox-meta"><b>${esc(m.sender || m.from)}</b> · ${esc(m.subject || "")}</div>
          <div class="prof-dim">${esc(m.snippet || "")}</div>
          <textarea class="fld pe-bullets" data-reply="${i}" rows="5">${esc(m.reply || "")}</textarea>
          <div class="pkg-cl-foot">
            <button class="btn btn-ghost btn-sm" data-copy-reply="${i}" type="button">Copy</button>
            <a class="btn btn-primary btn-sm" data-mailto="${i}" href="${mailto}">Open in your email app</a>
          </div>
        </div>`);
      });
    }
    if (Array.isArray(r.suggestions)) { INBOX_SUGG = r.suggestions; renderInboxStrip(); }
    if (INBOX_SUGG.length) {
      parts.push(`<div class="inbox-row"><div class="inbox-meta">${esc(T("aa.inbox.sugg", "{n} updates for jobs on your board.").replace("{n}", INBOX_SUGG.length))}</div>
        <button class="btn btn-ghost btn-sm" type="button" data-nav-go="tracker">${esc(T("aa.inbox.openTracker", "Open Tracker"))}</button></div>`);
    }
    if (!parts.length) parts.push(`<p class="prof-dim">${esc(T("aa.inbox.none", "Nothing application-related in the last scan ({n} messages checked).").replace("{n}", r.scanned))}</p>`);
    $("#inboxResults").innerHTML = parts.join("");
    $("#aaInboxSec").hidden = false;
    $("#inboxResults").querySelector("[data-nav-go]")?.addEventListener("click", () => navTo("tracker"));

    document.querySelectorAll("[data-verify]").forEach(b => b.addEventListener("click", async () => {
      b.disabled = true; b.textContent = "Verifying…";
      try {
        const out = await api("/api/inbox/verify", { url: b.dataset.verify });
        b.textContent = out.ok ? "Verified" : "Couldn't verify";
      } catch { b.textContent = "Couldn't verify"; b.disabled = false; }
    }));
    document.querySelectorAll("[data-copy-reply]").forEach(b => b.addEventListener("click", () => {
      const ta = document.querySelector(`[data-reply="${b.dataset.copyReply}"]`);
      navigator.clipboard.writeText(ta.value);
      b.textContent = "Copied"; setTimeout(() => b.textContent = "Copy", 1500);
    }));
    // Keep each mailto in sync with the edited draft.
    document.querySelectorAll("[data-reply]").forEach(ta => ta.addEventListener("input", () => {
      const i = ta.dataset.reply;
      const a = document.querySelector(`[data-mailto="${i}"]`);
      if (!a) return;
      const href = a.getAttribute("href").split("&body=")[0];
      a.setAttribute("href", href + "&body=" + encodeURIComponent(ta.value));
    }));
  }

  $("#inboxScan")?.addEventListener("click", async () => {
    const btn = $("#inboxScan"), st = $("#inboxStatus");
    btn.disabled = true; st.textContent = T("aa.inbox.reading", "Reading application mail\u2026");
    try {
      const r = await api("/api/inbox/scan", {});
      st.textContent = T("aa.inbox.scanned", "Scanned {n} application messages").replace("{n}", r.scanned);
      renderInbox(r);
    } catch (e) {
      st.textContent = e.message || T("aa.inbox.fail", "Scan failed");
    } finally {
      btn.disabled = false;
    }
  });
  $("#aaInboxClose")?.addEventListener("click", () => { $("#aaInboxSec").hidden = true; });

  /* ---------------------------------------------------------------- Tracker (the job board)
     Huntr / Teal style, local-first: one stage per job you saved, tailored or applied to, on the
     SAME rows the Jobs page uses (sourcing/board.py, /api/tracker/*). Kanban with plain HTML5
     drag and drop, a table view, and a side panel per card (notes saved on blur, contacts, a
     follow-up date, the timeline, links to the tailored resume and cover letter). The funnel at
     the top is computed server-side so the numbers match the claims export. */
  const TRK_STAGES = ["saved", "approved", "applied", "interviewing", "offer", "rejected", "archived"];
  const TRK_STAGE_EN = { saved: "Saved", approved: "Approved", applied: "Applied", interviewing: "Interviewing",
    offer: "Offer", rejected: "Rejected", archived: "Archived" };
  const trkT = (k, f) => (window.I18N ? I18N.t(k, f) : f);
  const trkStageName = (s) => trkT("tracker.stage." + s, TRK_STAGE_EN[s] || s);
  const TRK_VIEW_KEY = "tailor-tracker-view";
  let TRK_CARDS = [], TRK_METRICS = null, TRK_VIEW = "board", TRK_OPEN = null;
  try { TRK_VIEW = localStorage.getItem(TRK_VIEW_KEY) === "table" ? "table" : "board"; } catch { TRK_VIEW = "board"; }

  // "2026-10-05" -> "Oct 5". Built as a LOCAL date on purpose: new Date("2026-10-05") is UTC
  // midnight, which renders as the day before anywhere west of Greenwich.
  function trkDay(iso) {
    if (!iso) return "";
    const [y, m, d] = String(iso).slice(0, 10).split("-").map(Number);
    if (!y || !m || !d) return String(iso).slice(0, 10);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  async function loadTracker() {
    const [board, metrics] = await Promise.all([
      api("/api/tracker/board").catch(() => null),
      api("/api/tracker/metrics").catch(() => null),
    ]);
    if (board) TRK_CARDS = TRK_STAGES.flatMap(s => board.columns[s] || []);
    TRK_METRICS = metrics;
    renderTrackerMetrics();
    renderTracker();
    trkSetDot(metrics ? metrics.due_count : (board ? board.due_count : 0));
    loadInboxSuggestions();
  }

  async function trkReloadCards() {
    try {
      const board = await api("/api/tracker/board");
      TRK_CARDS = TRK_STAGES.flatMap(s => board.columns[s] || []);
      trkSetDot(board.due_count);
      renderTracker();
    } catch { /* keep what is on screen */ }
  }

  // The dot on the sidebar entry: a follow-up is due today or earlier.
  function trkSetDot(n) {
    const dot = $("#trackerNavDot");
    if (dot) dot.hidden = !(n > 0);
  }
  async function refreshTrackerDot() {
    try { const m = await api("/api/tracker/metrics"); trkSetDot(m.due_count); } catch { /* offline: no dot */ }
  }

  function trkSetView(v) {
    TRK_VIEW = v;
    try { localStorage.setItem(TRK_VIEW_KEY, v); } catch { /* private window */ }
    $("#trkViewBoard")?.classList.toggle("is-on", v === "board");
    $("#trkViewTable")?.classList.toggle("is-on", v === "table");
    renderTracker();
  }

  function renderTracker() {
    const board = $("#trkBoard"), table = $("#trkTable"), empty = $("#trkEmpty");
    if (!board || !table) return;
    const none = !TRK_CARDS.length;
    if (empty) empty.hidden = true;                // an empty tracker shows the board, with hints
    board.hidden = !none && TRK_VIEW !== "board";
    table.hidden = none || TRK_VIEW !== "table";
    if (none || TRK_VIEW === "board") renderTrackerBoard(); else renderTrackerTable();
  }

  // Sponsorship chips, quiet when there is nothing to say (a card has no room for "no record").
  function trkChips(c) {
    if ((c.visa || []).length) return visaBadges(c, { primary: true });
    if (c.sponsorship_stated) return `<span class="vbadge vb-stated">${esc(trkT("tracker.sponsorStated", "Sponsorship available"))}</span>`;
    return "";
  }

  function trkCardHtml(c) {
    const rec = c.record;
    const meta = [];
    if (c.applied_at) meta.push(`<span>${esc(trkT("tracker.appliedOn", "Applied"))} ${esc(trkDay(c.applied_at))}</span>`);
    if (c.follow_up_at) meta.push(`<span class="${c.follow_up_due ? "trk-due" : ""}">${esc(trkT("tracker.followUpShort", "Follow up"))} ${esc(trkDay(c.follow_up_at))}</span>`);
    if (rec && rec.pdf) meta.push(`<a href="${esc(rec.pdf)}" target="_blank" rel="noopener">${esc(trkT("tracker.resume", "Resume"))}${rec.count > 1 ? ` (${rec.count})` : ""}</a>`);
    if (c.has_notes) meta.push(`<span>${esc(trkT("tracker.hasNotes", "Notes"))}</span>`);
    const chips = trkChips(c);
    // How it was sent, or why it came back (submit/stages.py). Only said where it adds news:
    // an auto-submit, or an approved application that is back with the person.
    const st = rec && rec.state === "failed"
      ? `<div class="trk-card-state needs" title="${esc(rec.state_detail || "")}">${esc(trkT("tracker.needsYou", "Needs you"))}</div>`
      : rec && rec.state === "submitted" && rec.how === "auto" && c.stage === "applied"
      ? `<div class="trk-card-state">${esc(trkT("tracker.submittedForYou", "Submitted for you"))}</div>` : "";
    return `<div class="trk-card" draggable="true" data-sid="${esc(c.source_id)}" role="button" tabindex="0">
      <div class="trk-card-co">${esc(c.company)}</div>
      <div class="trk-card-t">${esc(c.title)}</div>
      ${st}
      ${chips ? `<div class="trk-card-chips">${chips}</div>` : ""}
      ${meta.length ? `<div class="trk-card-meta">${meta.join("")}</div>` : ""}
    </div>`;
  }

  // The board's columns (2026-10-23, after Huntr's Wishlist / Applied / Interview / Offer /
  // Rejected): Saved, Applied, Interviewing, Offer, Closed. "Ready to apply" (approved for
  // auto-apply) shows only while something waits there; Closed holds rejected and archived jobs.
  const TRK_COLS = [
    { key: "saved", stages: ["saved"], hint: ["tracker.hintSaved", "Jobs you save from the Jobs page"] },
    { key: "approved", stages: ["approved"], optional: true, hint: ["tracker.hintApproved", "Approved for auto-apply"] },
    { key: "applied", stages: ["applied"], hint: ["tracker.hintApplied", "Moves here when you apply"] },
    { key: "interviewing", stages: ["interviewing"], hint: ["tracker.hintInterview", "Drag a card here when they call"] },
    { key: "offer", stages: ["offer"], hint: ["tracker.hintOffer", "The goal"] },
    { key: "rejected", stages: ["rejected", "archived"], name: ["tracker.closed", "Closed"], hint: ["tracker.hintClosed", "Rejections and jobs you pass on"] },
  ];
  function trkFitBoard() {
    const host = $("#trkBoard"); if (!host || host.hidden || !host.offsetParent) return;
    host.style.height = Math.max(360, window.innerHeight - host.getBoundingClientRect().top - 44) + "px";
    const view = host.closest(".view"), spill = view ? view.scrollHeight - view.clientHeight : 0;
    if (spill > 0) host.style.height = Math.max(360, host.offsetHeight - spill) + "px";   // never scroll the page
  }
  window.addEventListener("resize", trkFitBoard);
  function renderTrackerBoard() {
    const host = $("#trkBoard");
    const none = !TRK_CARDS.length;
    const cols = TRK_COLS.filter(c => !c.optional || TRK_CARDS.some(x => c.stages.includes(x.stage)));
    host.style.setProperty("--trk-cols", cols.length);
    host.innerHTML = cols.map(col => {
      const cards = TRK_CARDS.filter(c => col.stages.includes(c.stage));
      const name = col.name ? trkT(col.name[0], col.name[1]) : trkStageName(col.key);
      return `<div class="trk-col" data-stage="${col.key}">
        <div class="trk-col-h"><span>${esc(name)}</span><span class="trk-col-n">${cards.length}</span></div>
        <div class="trk-col-cards">${cards.length ? cards.map(trkCardHtml).join("") : `<div class="trk-col-hint">${esc(trkT(col.hint[0], col.hint[1]))}</div>`}</div>
      </div>`;
    }).join("") + (none ? `<div class="trk-welcome">
        <div class="trk-welcome-h">${esc(trkT("tracker.welcomeH", "Every application, in one place"))}</div>
        <p class="trk-welcome-p">${esc(trkT("tracker.welcomeP", "Save a job or tailor a resume and it lands in Saved. Drag it along as you hear back."))}</p>
        <div class="trk-welcome-acts"><button class="btn btn-primary" type="button" data-go="jobs">${esc(trkT("tracker.findJobs", "Find jobs"))}</button>
          <button class="btn btn-ghost" type="button" data-go="autoapply">${esc(trkT("tracker.seeAuto", "See auto-apply"))}</button></div></div>` : "");
    host.querySelectorAll("[data-go]").forEach(b => b.addEventListener("click", () => document.querySelector(`[data-nav="${b.dataset.go}"]`)?.click()));
    requestAnimationFrame(trkFitBoard);
    host.querySelectorAll(".trk-card").forEach(card => {
      card.addEventListener("click", (e) => { if (e.target.closest("a")) return; openTrackerPanel(card.dataset.sid); });
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openTrackerPanel(card.dataset.sid); }
      });
      card.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/plain", card.dataset.sid);
        e.dataTransfer.effectAllowed = "move";
        card.classList.add("is-dragging");
      });
      card.addEventListener("dragend", () => card.classList.remove("is-dragging"));
    });
    host.querySelectorAll(".trk-col").forEach(col => {
      col.addEventListener("dragover", (e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; col.classList.add("is-over"); });
      col.addEventListener("dragleave", (e) => { if (!col.contains(e.relatedTarget)) col.classList.remove("is-over"); });
      col.addEventListener("drop", (e) => {
        e.preventDefault(); col.classList.remove("is-over");
        const sid = e.dataTransfer.getData("text/plain");
        const c = TRK_CARDS.find(x => x.source_id === sid);
        // Closed holds rejected AND archived: an archived card dropped back there stays archived.
        if (sid && !(c && col.dataset.stage === "rejected" && c.stage === "archived")) trkMove(sid, col.dataset.stage);
      });
    });
  }

  function trkStageSelect(c, attr) {
    return `<select class="fld trk-stage-sel" ${attr} aria-label="Stage">${TRK_STAGES.map(s =>
      `<option value="${s}"${s === c.stage ? " selected" : ""}>${esc(trkStageName(s))}</option>`).join("")}</select>`;
  }

  function renderTrackerTable() {
    const host = $("#trkTable");
    const order = (c) => TRK_STAGES.indexOf(c.stage);
    const rows = [...TRK_CARDS].sort((a, b) => order(a) - order(b) || String(b.stage_at).localeCompare(String(a.stage_at)));
    host.innerHTML = `<table class="trk-table"><thead><tr>
        <th>${esc(trkT("tracker.company", "Company"))}</th><th>${esc(trkT("tracker.role", "Role"))}</th><th>${esc(trkT("tracker.stageCol", "Stage"))}</th>
        <th>${esc(trkT("tracker.appliedOn", "Applied"))}</th><th>${esc(trkT("tracker.followUp", "Follow-up"))}</th><th>${esc(trkT("tracker.resume", "Resume"))}</th>
      </tr></thead><tbody>${rows.map(c => `<tr data-sid="${esc(c.source_id)}">
        <td>${esc(c.company)}</td><td>${esc(c.title)}</td><td>${trkStageSelect(c, `data-stage-for="${esc(c.source_id)}"`)}</td>
        <td>${esc(trkDay(c.applied_at))}</td>
        <td class="${c.follow_up_due ? "trk-due" : ""}">${esc(trkDay(c.follow_up_at))}</td>
        <td>${c.record && c.record.pdf ? `<a href="${esc(c.record.pdf)}" target="_blank" rel="noopener">${esc(trkT("tracker.open", "Open"))}</a>` : ""}</td>
      </tr>`).join("")}</tbody></table>`;
    host.querySelectorAll("tbody tr").forEach(tr => tr.addEventListener("click", (e) => {
      if (e.target.closest("select, a")) return;
      openTrackerPanel(tr.dataset.sid);
    }));
    host.querySelectorAll("[data-stage-for]").forEach(s => s.addEventListener("change", () => trkMove(s.dataset.stageFor, s.value)));
  }

  // The numbers as ONE slim strip above the board (2026-10-23): the board is the page, so the
  // funnel and the 12-week chart (now a small bar row on the right) take one line, not half the
  // screen. Same numbers as before, same weekly goal you can edit in place.
  function renderTrackerMetrics() {
    const host = $("#trkMetrics");
    if (!host) return;
    const m = TRK_METRICS;
    if (!m) { host.innerHTML = ""; return; }
    const goal = m.goal || { target: 0, done: 0, pct: 0 };
    const weeks = m.weeks || [];
    const peak = Math.max(5, ...weeks.map(w => w.count));
    const any = (m.applied_total || 0) > 0;
    const bars = weeks.map((w, i) => `<i class="${i === weeks.length - 1 ? "is-now" : ""}" title="${esc(trkT("tracker.weekOf", "Week of"))} ${esc(trkDay(w.week))}: ${w.count}" style="height:${Math.max(2, Math.round(w.count / peak * 28))}px"></i>`).join("");
    const median = m.median_days_to_interview;
    host.innerHTML = `
      <div class="trk-st trk-st-goal"><b class="num">${m.applied_week}</b>
        <label class="trk-goal-lbl">${esc(trkT("tracker.ofGoal", "of"))} <input class="fld trk-goal-in" id="trkGoal" type="number" min="0" max="500" value="${goal.target}" aria-label="${esc(trkT("tracker.goal", "Weekly goal"))}"> ${esc(trkT("tracker.thisWeekShort", "this week"))}</label>
        <span class="trk-goal-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${goal.target ? goal.pct : 0}"><span class="trk-goal-fill" style="width:${goal.target ? goal.pct : 0}%"></span></span></div>
      <div class="trk-st"><b class="num">${m.applied_total}</b>${esc(trkT("tracker.appliedLow", "applied"))}</div>
      <div class="trk-st"><b class="num">${m.interviews}</b>${esc(m.interviews === 1 ? trkT("tracker.interviewLow", "interview") : trkT("tracker.interviewsLow", "interviews"))}${any ? ` <span class="trk-st-f">${m.pct_interview}%</span>` : ""}</div>
      <div class="trk-st"><b class="num">${m.offers}</b>${esc(m.offers === 1 ? trkT("tracker.offerLow", "offer") : trkT("tracker.offersLow", "offers"))}</div>
      ${median != null ? `<div class="trk-st"><b class="num">${esc(String(median))}</b>${esc(trkT("tracker.daysToFirst", "days to first interview"))}</div>` : ""}
      ${any ? `<div class="trk-st trk-st-spark">${esc(trkT("tracker.weeks12", "12 weeks"))}<span class="trk-spark">${bars}</span></div>`
            : `<div class="trk-st trk-st-spark trk-st-hint">${esc(trkT("tracker.fillsIn", "Your weekly numbers fill in as you apply"))}</div>`}`;
    $("#trkGoal")?.addEventListener("change", async (e) => {
      const target = Math.max(0, parseInt(e.target.value, 10) || 0);
      try {
        await api("/api/tracker/goal", { target });
        TRK_METRICS = await api("/api/tracker/metrics");
        renderTrackerMetrics();
      } catch { toast(trkT("tracker.saveFail", "Couldn't save that, try again.")); }
    });
  }

  async function trkMove(sid, stage, note) {
    const card = TRK_CARDS.find(c => c.source_id === sid);
    if (!card || card.stage === stage) return;
    const prev = card.stage;
    card.stage = stage;             // optimistic: the card lands where it was dropped
    renderTracker();
    try {
      const out = await api("/api/tracker/move", { source_id: sid, stage, note: note || "" });
      Object.assign(card, out.job);
      trkSetDot(out.due_count);
      TRK_METRICS = await api("/api/tracker/metrics").catch(() => TRK_METRICS);
      renderTrackerMetrics();
      renderTracker();
      if (TRK_OPEN === sid) openTrackerPanel(sid);
    } catch {
      card.stage = prev;
      renderTracker();
      toast(trkT("tracker.moveFail", "Couldn't move that job, try again."));
    }
  }

  function closeTrackerPanel() {
    TRK_OPEN = null;
    const panel = $("#trkPanel"), scrim = $("#trkScrim");
    if (panel) { panel.hidden = true; panel.innerHTML = ""; }
    if (scrim) scrim.hidden = true;
  }

  async function openTrackerPanel(sid) {
    let j;
    try { j = await api("/api/tracker/job?source_id=" + encodeURIComponent(sid)); }
    catch { toast(trkT("tracker.loadFail", "Couldn't open that job.")); return; }
    try { INBOX_CONNECTED = !!(await api("/api/inbox/status")).configured; } catch { /* keep what we knew */ }
    TRK_OPEN = sid;
    const panel = $("#trkPanel"), scrim = $("#trkScrim");
    if (!panel) return;
    const contacts = (j.contacts || []).map(c => `<div class="trk-contact">
        <div class="trk-contact-who"><b>${esc(c.name)}</b>${c.role ? ` <span class="prof-dim">${esc(c.role)}</span>` : ""}
          <div class="trk-contact-links">${c.email ? `<a href="mailto:${esc(c.email)}">${esc(c.email)}</a>` : ""}${c.linkedin ? `<a href="${esc(c.linkedin)}" target="_blank" rel="noopener">LinkedIn</a>` : ""}</div>
        </div>
        <button class="btn btn-danger-ghost btn-sm" data-rm-contact="${c.id}" type="button" aria-label="Remove contact">&times;</button>
      </div>`).join("") || `<p class="prof-dim">${esc(trkT("tracker.noContacts", "No contacts yet."))}</p>`;
    const files = (j.records || []).map(r => `<div class="trk-file">
        <span>${esc(r.role || trkT("tracker.resume", "Resume"))} <span class="prof-dim">${esc(trkDay(r.when))}</span></span>
        <span class="trk-file-links">${r.pdf ? `<a href="${esc(r.pdf)}" target="_blank" rel="noopener">${esc(trkT("tracker.resume", "Resume"))}</a>` : ""}${r.cover_letter ? `<a href="${esc(r.cover_letter)}" target="_blank" rel="noopener">${esc(trkT("tracker.coverLetter", "Cover letter"))}</a>` : ""}</span>
      </div>`).join("") || `<p class="prof-dim">${esc(trkT("tracker.noFiles", "No tailored resume for this job yet."))}</p>`;
    const timeline = (j.activity || []).map(a => `<div class="trk-tl-row">
        <span class="trk-tl-dot" aria-hidden="true"></span>
        <span class="trk-tl-what">${a.to_stage ? esc(trkStageName(a.to_stage)) : esc(trkT("tracker.removed", "Taken off the board"))}${a.note ? ` <span class="prof-dim">${esc(a.note)}</span>` : ""}</span>
        <span class="trk-tl-when">${esc(trkDay(a.at))}</span>
      </div>`).join("");
    const chips = trkChips(j);
    panel.innerHTML = `
      <div class="trk-p-head">
        <div class="trk-p-head-main">
          <div class="trk-card-co">${esc(j.company)}${j.location ? ` · ${esc(j.location)}` : ""}</div>
          <h2 class="trk-p-title">${esc(j.title)}</h2>
          ${chips ? `<div class="trk-card-chips">${chips}</div>` : ""}
        </div>
        <button class="btn btn-ghost btn-sm" id="trkPanelClose" type="button" aria-label="Close">&times;</button>
      </div>
      <div class="trk-p-row">
        ${trkStageSelect(j, 'id="trkPanelStage"')}
        ${j.url ? `<a class="btn btn-ghost btn-sm" href="${esc(j.url)}" target="_blank" rel="noopener">${esc(trkT("tracker.posting", "Job posting"))}</a>` : ""}
      </div>
      <div class="trk-p-sec">
        <div class="trk-p-h">${esc(trkT("tracker.followUp", "Follow-up"))}</div>
        <div class="trk-p-row"><input class="fld" id="trkPanelFu" type="date" value="${esc(j.follow_up_at || "")}"><span class="prof-dim ${j.follow_up_due ? "trk-due" : ""}">${j.follow_up_due ? esc(trkT("tracker.dueNow", "Due now")) : ""}</span></div>
      </div>
      <div class="trk-p-sec">
        <div class="trk-p-h">${esc(trkT("tracker.notes", "Notes"))}</div>
        <textarea class="fld trk-notes" id="trkPanelNotes" rows="4" placeholder="${esc(trkT("tracker.notesPh", "Anything worth remembering about this one."))}">${esc(j.notes || "")}</textarea>
        <span class="prof-dim" id="trkPanelNotesState"></span>
      </div>
      <div class="trk-p-sec">
        <div class="trk-p-h">${esc(trkT("tracker.contacts", "Contacts"))}</div>
        <div id="trkPanelContacts">${contacts}</div>
        <form class="trk-contact-form" id="trkContactForm">
          <input class="fld" name="name" placeholder="${esc(trkT("tracker.cName", "Name"))}" required autocomplete="off">
          <input class="fld" name="role" placeholder="${esc(trkT("tracker.cRole", "Role"))}" autocomplete="off">
          <input class="fld" name="email" type="email" placeholder="${esc(trkT("tracker.cEmail", "Email"))}" autocomplete="off">
          <input class="fld" name="linkedin" type="url" placeholder="${esc(trkT("tracker.cLinkedin", "LinkedIn URL"))}" autocomplete="off">
          <button class="btn btn-ghost btn-sm trk-contact-add" type="submit">${esc(trkT("tracker.addContact", "Add contact"))}</button>
        </form>
      </div>
      ${trkMailSectionHtml(j)}
      <div class="trk-p-sec">
        <div class="trk-p-h">${esc(trkT("tracker.files", "Files"))}</div>
        ${files}
      </div>
      <div class="trk-p-sec">
        <div class="trk-p-h">${esc(trkT("tracker.timeline", "Timeline"))}</div>
        <div class="trk-tl">${timeline}</div>
      </div>`;
    panel.hidden = false;
    if (scrim) scrim.hidden = false;
    $("#trkPanelClose").addEventListener("click", closeTrackerPanel);
    trkBindMail(sid, j);
    $("#trkPanelStage").addEventListener("change", (e) => trkMove(sid, e.target.value));
    $("#trkPanelFu").addEventListener("change", async (e) => {
      try {
        const out = await api("/api/tracker/followup", { source_id: sid, date: e.target.value });
        trkSetDot(out.due_count);
        trkReloadCards();
      } catch { toast(trkT("tracker.saveFail", "Couldn't save that, try again.")); }
    });
    const notes = $("#trkPanelNotes"), state = $("#trkPanelNotesState");
    let lastNotes = notes.value;
    notes.addEventListener("blur", async () => {
      if (notes.value === lastNotes) return;
      try {
        await api("/api/tracker/note", { source_id: sid, notes: notes.value });
        lastNotes = notes.value;
        const card = TRK_CARDS.find(c => c.source_id === sid);
        if (card) card.has_notes = !!notes.value.trim();
        state.textContent = trkT("tracker.saved", "Saved");
        setTimeout(() => { state.textContent = ""; }, 1500);
      } catch { state.textContent = trkT("tracker.saveFail", "Couldn't save that, try again."); }
    });
    $("#trkContactForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const body = { source_id: sid, name: fd.get("name"), role: fd.get("role"), email: fd.get("email"), linkedin: fd.get("linkedin") };
      try { await api("/api/tracker/contact", body); openTrackerPanel(sid); }
      catch { toast(trkT("tracker.saveFail", "Couldn't save that, try again.")); }
    });
    panel.querySelectorAll("[data-rm-contact]").forEach(b => b.addEventListener("click", async () => {
      try { await api("/api/tracker/contact/remove", { source_id: sid, id: Number(b.dataset.rmContact) }); openTrackerPanel(sid); }
      catch { toast(trkT("tracker.saveFail", "Couldn't save that, try again.")); }
    }));
  }

  /* ---------------------------------------------------------------- From your inbox
     Stage suggestions built from the person's OWN mail (inbox/suggestions.py): a message that
     names an employer on the board and reads like an interview invite, a rejection, an offer,
     an assessment or an application confirmation. One strip, shown on the Tracker and inside
     the Review page's inbox card. Move <stage> is the person's click; Not this settles it so
     the same mail never asks again. Nothing moves on its own. */
  let INBOX_SUGG = [], INBOX_CONNECTED = false;
  const INBOX_KIND_EN = { interview_invite: "Interview invite", rejection: "Rejection", offer: "Offer",
    assessment: "Assessment", application_received: "Application received" };

  async function loadInboxSuggestions() {
    try { const d = await api("/api/inbox/suggestions"); INBOX_SUGG = d.suggestions || []; }
    catch { INBOX_SUGG = []; }
    renderInboxStrip();
  }

  function inboxSuggRow(sg) {
    const kind = trkT("inbox.kind." + sg.kind, INBOX_KIND_EN[sg.kind] || sg.kind);
    return `<div class="trk-inbox-row" data-sugg-row="${esc(sg.id)}">
      <div class="trk-inbox-who">
        <div><span class="trk-inbox-co">${esc(sg.company)}</span>${sg.title ? ` <span class="prof-dim">${esc(sg.title)}</span>` : ""}<span class="trk-inbox-kind">${esc(kind)}</span></div>
        <div class="trk-inbox-subj" title="${esc(sg.subject)}">${esc(sg.sender || sg.from || "")}${sg.subject ? ` &middot; ${esc(sg.subject)}` : ""}</div>
        ${sg.reason ? `<div class="trk-inbox-why">${esc(sg.reason)}</div>` : ""}
      </div>
      <div class="trk-inbox-acts">
        <button class="btn btn-ghost btn-sm" type="button" data-sugg-accept="${esc(sg.id)}">${esc(trkT("tracker.suggest", "Move to {stage}").replace("{stage}", trkStageName(sg.stage)))}</button>
        <button class="btn btn-ghost btn-sm" type="button" data-sugg-dismiss="${esc(sg.id)}">${esc(trkT("inbox.strip.notThis", "Not this"))}</button>
      </div>
    </div>`;
  }

  function renderInboxStrip() {
    const hosts = [$("#trkInbox")].filter(Boolean);
    hosts.forEach(host => {
      // The Tracker strip gets its own Scan button when Gmail is connected, and hides entirely
      // when there is nothing.
      if (!INBOX_SUGG.length && !INBOX_CONNECTED) { host.hidden = true; host.innerHTML = ""; return; }
      host.hidden = false;
      host.innerHTML = `<div class="trk-inbox-head">
          <span class="trk-inbox-h">${esc(trkT("inbox.strip.title", "From your inbox"))}${INBOX_SUGG.length ? ` (${INBOX_SUGG.length})` : ""}</span>
          ${INBOX_CONNECTED ? `<button class="btn btn-ghost btn-sm" type="button" data-sugg-scan>${esc(trkT("inbox.strip.scan", "Scan inbox"))}</button>` : ""}
        </div>
        ${INBOX_SUGG.length ? INBOX_SUGG.map(inboxSuggRow).join("") : `<div class="trk-inbox-empty">${esc(trkT("inbox.strip.empty", "Nothing new from your inbox. Scan to check for interview invites, offers and rejections for jobs on your board."))}</div>`}`;
      host.querySelectorAll("[data-sugg-accept]").forEach(b => b.addEventListener("click", () => inboxSuggDecide(b.dataset.suggAccept, "accept")));
      host.querySelectorAll("[data-sugg-dismiss]").forEach(b => b.addEventListener("click", () => inboxSuggDecide(b.dataset.suggDismiss, "dismiss")));
      host.querySelector("[data-sugg-scan]")?.addEventListener("click", async (e) => {
        const b = e.currentTarget; b.disabled = true; b.textContent = trkT("inbox.strip.scanning", "Scanning...");
        try { const r = await api("/api/inbox/scan", { draft: false }); INBOX_SUGG = r.suggestions || []; renderInboxStrip(); }
        catch (err) { toast(err.message || trkT("inbox.strip.fail", "Couldn't do that, try again.")); b.disabled = false; b.textContent = trkT("inbox.strip.scan", "Scan inbox"); }
      });
    });
  }

  async function inboxSuggDecide(id, action) {
    const sg = INBOX_SUGG.find(x => x.id === id);
    if (!sg) return;
    document.querySelectorAll(`[data-sugg-row="${CSS.escape(id)}"] button`).forEach(b => b.disabled = true);
    try {
      const out = await api(`/api/inbox/suggestions/${encodeURIComponent(id)}/${action}`, {});
      INBOX_SUGG = out.suggestions || INBOX_SUGG.filter(x => x.id !== id);
      renderInboxStrip();
      if (action === "accept") {
        toast(trkT("inbox.strip.moved", "Moved {company} to {stage}").replace("{company}", sg.company).replace("{stage}", trkStageName(sg.stage)));
        trkSetDot(out.due_count);
        trkReloadCards();
        TRK_METRICS = await api("/api/tracker/metrics").catch(() => TRK_METRICS);
        renderTrackerMetrics();
        if (TRK_OPEN === sg.source_id) openTrackerPanel(sg.source_id);
      }
    } catch (err) {
      renderInboxStrip();
      toast(err.message || trkT("inbox.strip.fail", "Couldn't do that, try again."));
    }
  }

  /* ---------------------------------------------------------------- Draft an email (Tracker panel)
     One model call per draft, grounded in the saved profile, the job and its stage
     (inbox/drafts.py). The draft lands in an editable box: Copy, or Open in Gmail (a compose
     link in the browser, shown when Gmail is connected; otherwise the mail app). The person
     sends it. Nothing is sent from the app. */
  const TRK_MAIL_TYPES = ["follow_up", "thank_you", "interview_reply", "referral", "negotiation", "decline"];
  const TRK_MAIL_EN = { follow_up: "Follow-up after applying", thank_you: "Thank-you after an interview",
    interview_reply: "Reply to an interview invite with availability", referral: "Ask a contact for a referral",
    negotiation: "Reply to an offer (negotiate)", decline: "Decline politely" };
  // The type that fits each stage best is pre-selected.
  const TRK_MAIL_DEFAULT = { saved: "referral", applied: "follow_up", interviewing: "thank_you", offer: "negotiation", rejected: "thank_you", archived: "follow_up" };

  function gmailComposeUrl(to, subject, body) {
    const q = [];
    if (to) q.push("to=" + encodeURIComponent(to));
    q.push("su=" + encodeURIComponent(subject || ""));
    q.push("body=" + encodeURIComponent(body || ""));
    return "https://mail.google.com/mail/?view=cm&" + q.join("&");
  }

  function trkMailSectionHtml(j) {
    const def = TRK_MAIL_DEFAULT[j.stage] || "follow_up";
    const types = TRK_MAIL_TYPES.map(t => `<option value="${t}"${t === def ? " selected" : ""}>${esc(trkT("tracker.emailType." + t, TRK_MAIL_EN[t]))}</option>`).join("");
    const contacts = (j.contacts || []).map(c => `<option value="${c.id}">${esc(c.name)}${c.email ? ` (${esc(c.email)})` : ""}</option>`).join("");
    return `<div class="trk-p-sec" id="trkMail">
        <div class="trk-p-h">${esc(trkT("tracker.email", "Email"))}</div>
        <div class="trk-mail-row">
          <select class="fld" id="trkMailType" aria-label="${esc(trkT("tracker.emailDraft", "Draft an email"))}">${types}</select>
          <select class="fld" id="trkMailContact" aria-label="${esc(trkT("tracker.contacts", "Contacts"))}"><option value="">${esc(trkT("tracker.emailNoContact", "No contact"))}</option>${contacts}</select>
          <button class="btn btn-ghost btn-sm" id="trkMailGo" type="button">${esc(trkT("tracker.emailDraft", "Draft an email"))}</button>
        </div>
        <div class="trk-mail-out" id="trkMailOut" hidden></div>
      </div>`;
  }

  function trkBindMail(sid, j) {
    const go = $("#trkMailGo"), out = $("#trkMailOut"), typeSel = $("#trkMailType"), contactSel = $("#trkMailContact");
    if (!go) return;
    go.addEventListener("click", async () => {
      const type = typeSel.value, contact_id = contactSel.value || null;
      if (type === "referral" && !contact_id) { toast(trkT("tracker.emailNeedContact", "Pick a contact for a referral.")); return; }
      go.disabled = true; const label = go.textContent; go.textContent = trkT("tracker.emailDrafting", "Drafting...");
      try {
        const d = await api(`/api/tracker/${encodeURIComponent(sid)}/draft-email`, { type, contact_id });
        out.hidden = false;
        out.innerHTML = `
          <input class="fld trk-mail-subj" id="trkMailSubj" value="${esc(d.subject || "")}" aria-label="${esc(trkT("tracker.emailSubject", "Subject"))}" placeholder="${esc(trkT("tracker.emailSubject", "Subject"))}">
          <textarea class="fld trk-mail-body" id="trkMailBody" rows="7">${esc(d.body || "")}</textarea>
          <div class="trk-mail-foot">
            <button class="btn btn-ghost btn-sm" id="trkMailCopy" type="button">${esc(trkT("tracker.emailCopy", "Copy"))}</button>
            ${INBOX_CONNECTED
              ? `<a class="btn btn-primary btn-sm" id="trkMailOpen" href="${esc(gmailComposeUrl(d.to, d.subject, d.body))}" target="_blank" rel="noopener">${esc(trkT("tracker.emailGmail", "Open in Gmail"))}</a>`
              : `<a class="btn btn-primary btn-sm" id="trkMailOpen" href="mailto:${encodeURIComponent(d.to || "")}?subject=${encodeURIComponent(d.subject || "")}&body=${encodeURIComponent(d.body || "")}">${esc(trkT("tracker.emailApp", "Open in your email app"))}</a>`}
            <span class="trk-mail-hint">${esc(trkT("tracker.emailHint", "You send it. Nothing is sent from here."))}</span>
          </div>`;
        const subj = $("#trkMailSubj"), body = $("#trkMailBody"), open = $("#trkMailOpen");
        const sync = () => {
          open.setAttribute("href", INBOX_CONNECTED
            ? gmailComposeUrl(d.to, subj.value, body.value)
            : `mailto:${encodeURIComponent(d.to || "")}?subject=${encodeURIComponent(subj.value)}&body=${encodeURIComponent(body.value)}`);
        };
        subj.addEventListener("input", sync); body.addEventListener("input", sync);
        $("#trkMailCopy").addEventListener("click", () => {
          navigator.clipboard.writeText(body.value);
          const b = $("#trkMailCopy"); b.textContent = trkT("tracker.emailCopied", "Copied");
          setTimeout(() => { b.textContent = trkT("tracker.emailCopy", "Copy"); }, 1500);
        });
      } catch (err) {
        toast(err.message || trkT("tracker.emailFail", "Couldn't draft that, try again."));
      } finally {
        go.disabled = false; go.textContent = label;
      }
    });
  }

  $("#trkViewBoard")?.addEventListener("click", () => trkSetView("board"));
  $("#trkViewTable")?.addEventListener("click", () => trkSetView("table"));
  $("#trkViewBoard")?.classList.toggle("is-on", TRK_VIEW === "board");
  $("#trkViewTable")?.classList.toggle("is-on", TRK_VIEW === "table");
  $("#trkScrim")?.addEventListener("click", closeTrackerPanel);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && TRK_OPEN) closeTrackerPanel(); });
  refreshTrackerDot();

  /* nav */
  document.querySelectorAll(".nav-item").forEach(n => n.addEventListener("click", () => {
    teardownCvi();   // leaving via the sidebar ends any in-progress live interview (no leaked meter or pane)
    document.querySelectorAll(".nav-item").forEach(x => x.classList.toggle("is-active", x === n));
    if (n.dataset.nav !== "jobs") stopFeedPoll();
    if (n.dataset.nav === "profile") { loadProfile(); showView("profile"); }
    else if (n.dataset.nav === "cvs") { loadCvs(); showView("cvs"); }
    else if (n.dataset.nav === "templates") { loadTemplates(); showView("templates"); }
    else if (n.dataset.nav === "jobs") { loadJobsFeed(); showView("jobs"); }
    else if (n.dataset.nav === "tracker") { loadTracker(); showView("tracker"); }
    else if (n.dataset.nav === "autoapply") { loadAutoApply(); showView("autoapply"); }
    else if (n.dataset.nav === "projects") { loadProjects(); showView("projects"); }
    else if (n.dataset.nav === "prep") { loadPreps(); showView("prep"); }
    else { loadDashboard(); showView("dashboard"); }
  }));

  // ---------------------------------------------------------------- Projects (defend-your-work gate)
  // A project can't reach the CV until you pass a defense test proving you genuinely understand it.
  let PROJECTS = [], DEFEND_ID = null, DEFEND_QS = [], BUILD = null;
  let QUIZ = null;   // Defend it: the open quiz (see startQuiz)
  async function loadProjects() {
    if ($("#projTest")) $("#projTest").hidden = true;
    if ($("#projBuild")) $("#projBuild").hidden = true;
    if ($("#projLearn")) $("#projLearn").hidden = true;
    if ($("#projQuiz")) $("#projQuiz").hidden = true;
    QUIZ = null;
    projSplit(false);
    if ($("#projList")) $("#projList").hidden = false;
    try { const d = await api("/api/projects"); PROJECTS = d.projects || []; } catch { PROJECTS = []; }
    if (MYPROJ) { if ($("#projIdeas")) $("#projIdeas").hidden = true; renderProjList(); }
    else openIdeas();                          // Projects opens on the ideas
  }
  // false: the Projects home (the ideas). true: the Your projects page (the list, drafts, verified).
  let MYPROJ = false;
  // The newest project still in progress, for the one Continue line on the ideas page.
  function projInProgress() {
    const going = PROJECTS.filter(p => (p.origin === "ai" ? p.stage !== "verified" : p.status !== "verified"));
    const t = (p) => Number(p.updated || p.updated_at || p.created || p.created_at || 0) || 0;
    return going.sort((a, b) => t(b) - t(a))[0] || null;
  }
  function paintProjContinue() {
    const box = $("#projContinue"); if (!box) return;
    const p = projInProgress();
    box.hidden = !p; if (!p) return;
    let what = "", note = "";
    if (p.origin === "ai") {
      const b = p.build || {}, lr = p.learn || {}, d = p.defense || {};
      if (p.stage === "building") { what = PT("projects.sBuilding", "Building"); note = (BUILD_LABEL[b.state] || BUILD_LABEL.queued)(b); }
      else if (p.stage === "failed") what = PT("projects.sStopped", "Build stopped");
      else if (p.stage === "learn") { what = PT("projects.sLearn", "Learn it"); if (lr.total) note = PT("projects.nLearned", "{n} of {m} learned").replace("{n}", lr.learned || 0).replace("{m}", lr.total); }
      else if (p.stage === "defend") { what = PT("projects.sDefend", "Defend it"); if (d.tried) note = PT("projects.lastQuiz", "Last try {s} of {m}. Pass is {p}.").replace("{s}", d.score).replace("{m}", d.total).replace("{p}", d.pass_mark); }
    } else what = PT("projects.draft", "Draft");
    box.innerHTML = `<span class="prep-last-k">${esc(PT("projects.inProgress", "In progress"))}</span>
      <span class="prep-last-role">${esc(p.title)}</span>
      <span class="prep-last-meta">${esc(what)}${note ? ` &middot; ${esc(note)}` : ""}</span>
      <span class="prep-last-score"></span>
      <span class="prep-last-go">${esc(PT("projects.continueLearn", "Continue"))} &rarr;</span>`;
    box.onclick = () => {
      if (p.origin === "ai" && p.stage === "learn") return startLearn(p);
      if (p.origin === "ai" && p.stage === "defend") return startQuiz(p);
      if (p.origin !== "ai") return startBuildGuide(p);
      showMyProjects();                        // building or stopped: its card shows the state
    };
  }
  function showMyProjects() { MYPROJ = true; if ($("#projIdeas")) $("#projIdeas").hidden = true; renderProjList(); }
  // Monochrome inline glyphs for the Projects screen (no assets, no colour).
  // <tech-logo> (2026-10-13). The idea tile is the Jobs logo tile: white, with the real logo of
  // the idea's main technology in its own brand colour (Simple Icons, vendored in
  // vendor/techlogos.js, so no request leaves the app). Pure functions; tests/test_tech_logos.py
  // runs this block under node.
  // Rank: frameworks and libraries (0) before platforms, databases and infra (1), before
  // languages (2), before the everywhere tools (3), so React beats JavaScript, FastAPI and Airflow
  // beat Python. Ties go to whichever the idea lists first.
  const TECH_ALIAS = {
    // 0: frameworks and libraries
    react: [0, "react", "react.js", "reactjs", "react native"], nextdotjs: [0, "next.js", "nextjs", "next"],
    vuedotjs: [0, "vue", "vue.js", "vuejs", "nuxt"], angular: [0, "angular", "angularjs"], svelte: [0, "svelte", "sveltekit"],
    nodedotjs: [0, "node", "node.js", "nodejs"], express: [0, "express", "express.js", "expressjs"],
    fastapi: [0, "fastapi"], flask: [0, "flask"], django: [0, "django", "django rest framework", "drf"],
    spring: [0, "spring", "spring boot", "springboot"], graphql: [0, "graphql"],
    tailwindcss: [0, "tailwind", "tailwind css", "tailwindcss"],
    apacheairflow: [0, "airflow", "apache airflow"], apachespark: [0, "spark", "pyspark", "apache spark", "spark sql"],
    apachekafka: [0, "kafka", "apache kafka"], pandas: [0, "pandas"], numpy: [0, "numpy"], polars: [0, "polars"],
    streamlit: [0, "streamlit"], plotly: [0, "plotly", "plotly dash", "dash"],
    jupyter: [0, "jupyter", "jupyter notebook", "jupyter notebooks", "jupyterlab", "notebooks"],
    scikitlearn: [0, "scikit-learn", "sklearn", "scikit learn", "scikit"], pytorch: [0, "pytorch", "torch"],
    tensorflow: [0, "tensorflow", "tf", "keras"], huggingface: [0, "hugging face", "huggingface", "transformers", "hugging face transformers"],
    langchain: [0, "langchain"], mlflow: [0, "mlflow"], anthropic: [0, "anthropic", "anthropic api", "claude", "claude api"],
    flutter: [0, "flutter"], selenium: [0, "selenium"], pytest: [0, "pytest"],
    // 1: platforms, databases and infra
    postgresql: [1, "postgres", "postgresql", "psql"], mysql: [1, "mysql"], sqlite: [1, "sqlite", "sqlite3"],
    mongodb: [1, "mongodb", "mongo"], redis: [1, "redis"], snowflake: [1, "snowflake"],
    googlebigquery: [1, "bigquery", "google bigquery"], databricks: [1, "databricks"], duckdb: [1, "duckdb"],
    elasticsearch: [1, "elasticsearch", "elastic"], docker: [1, "docker", "docker compose", "docker-compose"],
    kubernetes: [1, "kubernetes", "k8s"], terraform: [1, "terraform"], githubactions: [1, "github actions", "gh actions"],
    googlecloud: [1, "gcp", "google cloud", "google cloud platform", "cloud run"], nginx: [1, "nginx"], grafana: [1, "grafana"],
    android: [1, "android", "jetpack compose"], ios: [1, "ios", "swiftui", "uikit"], postman: [1, "postman"],
    // 2: languages
    python: [2, "python", "python3", "py"], javascript: [2, "javascript", "js", "es6"], typescript: [2, "typescript", "ts"],
    openjdk: [2, "java", "openjdk"], go: [2, "go", "golang"], rust: [2, "rust"], cplusplus: [2, "c++", "cpp"],
    dotnet: [2, ".net", "dotnet", "c#", "csharp", "asp.net", "asp.net core", ".net core"], kotlin: [2, "kotlin"],
    swift: [2, "swift"], r: [2, "r"], scala: [2, "scala"], gnubash: [2, "bash", "shell", "shell scripting"], linux: [2, "linux"],
    // 3: everywhere tools
    git: [3, "git"],
  };
  const TECH_INDEX = (() => {
    const m = new Map();
    for (const [slug, [rank, ...names]] of Object.entries(TECH_ALIAS)) for (const n of names) m.set(n, { slug, rank });
    return m;
  })();
  const techNorm = t => String(t || "").toLowerCase().replace(/\s+/g, " ").replace(/\s*\d+(\.\d+)*$/, "").trim();
  // The tech field is free text ("Python, FastAPI and PostgreSQL (Docker)"): split it into terms.
  const techTerms = s => String(s || "").split(/[,;|/+()\[\]\n]|\s&\s|\sand\s|\swith\s/i).map(techNorm).filter(Boolean);
  function techLookup(term) {
    const hit = TECH_INDEX.get(term);
    if (hit) return hit;
    // A longer phrase ("PostgreSQL database", "Apache Airflow DAGs"): the first known name of at
    // least 4 letters that stands alone as a word, so "r" and "go" never match inside words.
    let best = null, at = Infinity;
    for (const [n, v] of TECH_INDEX) {
      if (n.length < 4) continue;
      const i = ` ${term} `.indexOf(` ${n} `);
      if (i >= 0 && i < at) { at = i; best = v; }
    }
    return best;
  }
  // The main technology of an idea or a project: its `tech` text, then its skills, best rank wins.
  function mainTech(item, logos) {
    const lib = logos || (typeof window !== "undefined" && window.TECH_LOGOS) || {};
    const terms = [...techTerms(item?.tech), ...(item?.skills || []).map(techNorm)];
    let pick = null;
    terms.forEach((t, i) => {
      const hit = techLookup(t);
      if (hit && lib[hit.slug] && (!pick || hit.rank < pick.rank)) pick = { ...hit, i };
    });
    return pick ? { slug: pick.slug, ...lib[pick.slug] } : null;
  }
  // The brand hex as given (black reads fine on white); a near-white one gets a dark neutral.
  function logoFill(hex) {
    const c = String(hex || "").replace("#", "");
    if (!/^[0-9a-f]{6}$/i.test(c)) return "#3a3a3c";
    const lin = k => { const v = parseInt(c.slice(k, k + 2), 16) / 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    const lum = 0.2126 * lin(0) + 0.7152 * lin(2) + 0.0722 * lin(4);
    return lum > 0.85 ? "#3a3a3c" : `#${c.toUpperCase()}`;
  }
  // The white tile: the logo when the stack has one, else the fallback glyph in mid grey.
  function techTile(item, fallbackSVG, cls = "") {
    const lg = mainTech(item);
    if (!lg) return `<span class="tech-tile${cls ? ` ${cls}` : ""}" aria-hidden="true">${fallbackSVG}</span>`;
    return `<span class="tech-tile is-logo${cls ? ` ${cls}` : ""}" title="${esc(lg.title)}" data-tech="${esc(lg.slug)}">`
      + `<svg viewBox="0 0 24 24" role="img" aria-label="${esc(lg.title)}"><path fill="${logoFill(lg.hex)}" d="${lg.path}"/></svg></span>`;
  }
  // </tech-logo>

  const PROJ_ICO = {
    code: `<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 7.5 4 12l4.5 4.5M15.5 7.5 20 12l-4.5 4.5M13.5 5.5l-3 13"/></svg>`,
    seal: `<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l2.2 1.6 2.7-.1.8 2.6 2.2 1.6-.9 2.6.9 2.6-2.2 1.6-.8 2.6-2.7-.1L12 21l-2.2-1.6-2.7.1-.8-2.6-2.2-1.6.9-2.6-.9-2.6 2.2-1.6.8-2.6 2.7.1z"/><path d="m8.8 12.2 2.2 2.2 4.2-4.4"/></svg>`,
    plus: `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M8 3.5v9M3.5 8h9"/></svg>`,
    steps: `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"><path d="M8 2 2.5 5 8 8l5.5-3z"/><path d="m2.5 8 5.5 3 5.5-3M2.5 11 8 14l5.5-3"/></svg>`,
    talk: `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M2.5 4a1.5 1.5 0 0 1 1.5-1.5h8A1.5 1.5 0 0 1 13.5 4v5.5A1.5 1.5 0 0 1 12 11H7l-3 2.5V11a1.5 1.5 0 0 1-1.5-1.5z"/></svg>`,
    check: `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="m3.5 8.3 2.9 2.9 6.1-6.4"/></svg>`,
    bulb: `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" stroke-linecap="round"><path d="M8 1.8a4.2 4.2 0 0 0-2.5 7.6c.5.4.8.9.8 1.5v.3h3.4v-.3c0-.6.3-1.1.8-1.5A4.2 4.2 0 0 0 8 1.8Z"/><path d="M6.6 13.6h2.8M7 15h2"/></svg>`,
    download: `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2.5v8M4.8 7.5 8 10.7l3.2-3.2M3 13.5h10"/></svg>`,
    trash: `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5"/></svg>`,
  };
  function projCard(p) {
    if (p.origin === "ai") return aiProjCard(p);
    const verified = p.status === "verified";
    const steps = p.plan?.milestones?.length || 0;
    const doneN = Math.min((p.done || []).length, steps);
    const guideLabel = !steps ? "Start guide" : doneN >= steps ? "Guide" : "Continue guide";
    // The guide's progress, as a slim bar: only once a plan exists, and only on drafts.
    const prog = !verified && steps
      ? `<div class="proj-prog" title="${doneN} of ${steps} steps built">
           <span class="proj-prog-bar"><i style="width:${Math.round(doneN / steps * 100)}%"></i></span>
           <span class="proj-prog-n">${doneN}/${steps}</span></div>`
      : "";
    // A repo shows as a small GitHub mark beside the title; a private one carries a lock.
    const repo = p.repo_url
      ? `<a class="proj-gh ${p.repo_private ? "is-private" : ""}" href="${esc(p.repo_url)}" target="_blank" rel="noopener"
           title="${esc(p.repo_private ? PT("projects.privateRepo", "Private repo on GitHub") : PT("projects.publicRepo", "Repo on GitHub"))}"
           aria-label="${esc(p.repo_private ? PT("projects.privateRepo", "Private repo on GitHub") : PT("projects.publicRepo", "Repo on GitHub"))}">${GH_MARK}${p.repo_private ? ICO_LOCK : ""}</a>`
      : "";
    const meta = p.tech || "";
    return `<article class="proj-card ${verified ? "is-verified" : "is-draft"}" data-id="${esc(p.id)}">
      ${techTile(p, verified ? PROJ_ICO.seal : PROJ_ICO.code, "proj-card-mark")}
      <div class="proj-card-hd">
        <div class="proj-card-name"><span class="proj-card-title">${esc(p.title)}</span>
          <span class="proj-pill ${verified ? "ok" : ""}">${verified ? `${PROJ_ICO.check}Verified` : "Draft"}</span>${repo}</div>
        ${meta ? `<div class="proj-card-tech">${esc(meta)}</div>` : ""}
        ${p.summary ? `<div class="proj-card-sum" title="${esc(p.summary)}">${esc(p.summary)}</div>` : ""}
        ${prog}
      </div>
      <div class="proj-card-acts">
        ${verified
          ? `${p.repo_url && !p.repo_private
               ? ""
               : `<button class="btn btn-quiet btn-sm" data-act="publish" type="button">${esc(p.repo_url ? PT("projects.makePublic", "Make public") : "Publish to GitHub")}</button>`}
             <button class="btn btn-ghost btn-sm" data-act="tocv" type="button">Add to resume</button>`
          : `<button class="btn btn-quiet btn-sm" data-act="guide" type="button">${esc(guideLabel)}</button>
             <button class="btn btn-ghost btn-sm" data-act="defend" type="button">Defend it</button>`}
        <button class="proj-icon-btn" data-act="delete" type="button" aria-label="Delete ${esc(p.title)}" title="Delete">${PROJ_ICO.trash}</button>
      </div>
    </article>`;
  }
  // An AI-built project's card: one stage at a time, one main action. Building (progress),
  // Learn it (n of m), Defend it, Verified (Publish to GitHub, Add to resume).
  const BUILD_LABEL = {
    queued: () => PT("projects.bPlanning", "Planning files"),
    planning: () => PT("projects.bPlanning", "Planning files"),
    writing: (b) => b.total && b.done ? PT("projects.bWriting", "Writing code {n} of {m}").replace("{n}", b.done || 0).replace("{m}", b.total) : PT("projects.bWritingStart", "Writing code"),
    pushing: () => PT("projects.bPushing", "Pushing to GitHub"),
    preparing: () => PT("projects.bPreparing", "Preparing Learn it"),
    ready: () => PT("projects.bReady", "Ready"),
  };
  function aiBuildPct(b) {
    const st = b.state || "queued";
    if (st === "queued" || st === "planning") return 6;
    if (st === "writing") return 10 + Math.round(70 * ((b.done || 0) / Math.max(1, b.total || 1)));
    if (st === "pushing") return 86;
    if (st === "preparing") return 94;
    return 100;
  }
  function aiCardParts(p) {
    const stage = p.stage, b = p.build || {}, lr = p.learn || {};
    const btn = (act, label, main) => `<button class="btn ${main ? "btn-ghost" : "btn-quiet"} btn-sm" data-act="${act}" type="button">${esc(label)}</button>`;
    const bar = (pct, label, title) => `<div class="proj-prog proj-prog-ai" title="${esc(title || label)}">
        <span class="proj-prog-bar"><i style="width:${pct}%"></i></span><span class="proj-prog-n">${esc(label)}</span></div>`;
    if (stage === "building") {
      return { pill: PT("projects.sBuilding", "Building"), cls: "is-building",
        extra: bar(aiBuildPct(b), (BUILD_LABEL[b.state] || BUILD_LABEL.queued)(b)), acts: "" };
    }
    if (stage === "failed") {
      return { pill: PT("projects.sStopped", "Build stopped"), cls: "is-failed",
        extra: `<div class="proj-card-err">${esc(b.error || PT("projects.bFailed", "Something went wrong while building."))}</div>`,
        acts: btn("rebuild", PT("projects.tryAgain", "Try again"), true) };
    }
    if (stage === "learn") {
      const n = lr.learned || 0, m = lr.total || 0;
      return { pill: PT("projects.sLearn", "Learn it"), cls: "is-learn",
        extra: m ? bar(Math.round(n / m * 100), PT("projects.nLearned", "{n} of {m} learned").replace("{n}", n).replace("{m}", m)) : "",
        acts: btn("learn", n ? PT("projects.continueLearn", "Continue") : PT("projects.startLearn", "Start"), true) };
    }
    if (stage === "defend") {
      const d = p.defense || {}, tried = d.tried;
      // Answers kept in this browser mean a quiz in progress: Continue it.
      const going = (() => { try { return !!localStorage.getItem(`sj.quiz.${p.id}`); } catch { return false; } })();
      return { pill: PT("projects.sDefend", "Defend it"), cls: "is-defend",
        extra: tried ? `<div class="proj-card-note">${esc(PT("projects.lastQuiz", "Last try {s} of {m}. Pass is {p}.").replace("{s}", d.score).replace("{m}", d.total).replace("{p}", d.pass_mark))}</div>` : "",
        acts: btn("learn", PT("projects.review", "Review"), false)
          + btn("quiz", going ? PT("projects.continueLearn", "Continue") : tried ? PT("projects.retake", "Retake") : PT("projects.startLearn", "Start"), true) };
    }
    // Verified
    const credit = p.repo_url && !p.repo_private ? "" : `<label class="proj-credit"><input type="checkbox" data-act="credit" ${p.ai_credit ? "checked" : ""}>
        <span>${esc(PT("projects.aiCredit", "Say it was built with an AI pair-programmer"))}</span></label>`;
    return { pill: PT("projects.sVerified", "Verified"), cls: "is-verified", extra: credit,
      acts: (p.repo_url && !p.repo_private ? "" : btn("publish", p.repo_url ? PT("projects.makePublic", "Make public") : PT("projects.publish", "Publish to GitHub"), false))
        + btn("tocv", PT("projects.addResume", "Add to resume"), true) };
  }
  function aiProjCard(p) {
    const verified = p.stage === "verified";
    const part = aiCardParts(p);
    const repo = p.repo_url
      ? `<a class="proj-gh ${p.repo_private ? "is-private" : ""}" href="${esc(p.repo_url)}" target="_blank" rel="noopener"
           title="${esc(p.repo_private ? PT("projects.privateRepo", "Private repo on GitHub") : PT("projects.publicRepo", "Repo on GitHub"))}"
           aria-label="${esc(p.repo_private ? PT("projects.privateRepo", "Private repo on GitHub") : PT("projects.publicRepo", "Repo on GitHub"))}">${GH_MARK}${p.repo_private ? ICO_LOCK : ""}</a>`
      : "";
    const dl = p.has_files && p.stage !== "building"
      ? `<button class="proj-icon-btn is-neutral" data-act="download" type="button" aria-label="${esc(PT("projects.download", "Download the files"))}" title="${esc(PT("projects.download", "Download the files"))}">${PROJ_ICO.download}</button>` : "";
    return `<article class="proj-card is-ai ${part.cls}" data-id="${esc(p.id)}">
      ${techTile(p, verified ? PROJ_ICO.seal : PROJ_ICO.code, "proj-card-mark")}
      <div class="proj-card-hd">
        <div class="proj-card-name"><span class="proj-card-title">${esc(p.title)}</span>
          <span class="proj-pill ${verified ? "ok" : ""}">${verified ? PROJ_ICO.check : ""}${esc(part.pill)}</span>${repo}</div>
        ${p.tech ? `<div class="proj-card-tech">${esc(p.tech)}</div>` : ""}
        ${p.summary ? `<div class="proj-card-sum" title="${esc(p.summary)}">${esc(p.summary)}</div>` : ""}
        ${part.extra}
      </div>
      <div class="proj-card-acts">
        ${part.acts}${dl}
        <button class="proj-icon-btn" data-act="delete" type="button" aria-label="Delete ${esc(p.title)}" title="Delete">${PROJ_ICO.trash}</button>
      </div>
    </article>`;
  }
  // While a build runs, the list polls its progress. The job runs on the engine, so leaving the
  // screen and coming back simply picks the progress up again.
  let PROJ_POLL = null;
  function pollBuilds() {
    clearTimeout(PROJ_POLL); PROJ_POLL = null;
    if (!PROJECTS.some(p => p.stage === "building")) return;
    PROJ_POLL = setTimeout(async () => {
      PROJ_POLL = null;
      if (!$("#view-projects")?.classList.contains("is-active")) return;   // picked up on the next visit
      const before = new Map(PROJECTS.map(p => [p.id, p.stage]));
      try { PROJECTS = (await api("/api/projects")).projects || PROJECTS; } catch { /* keep polling */ }
      if ($("#projList") && !$("#projList").hidden) renderProjList();
      PROJECTS.forEach(p => { if (before.get(p.id) === "building" && p.stage === "learn") toast(PT("projects.builtToast", "{t} is built. Learn it next.").replace("{t}", p.title)); });
      pollBuilds();
    }, 1500);
  }
  async function rebuildProject(p, btn) {
    if (btn) btn.disabled = true;
    try { await api(`/api/projects/${p.id}/build`, {}); } catch (err) { toast(err.message || PT("projects.startFailed", "Couldn't start the project.")); }
    loadProjects();
  }
  async function setAiCredit(p, on) {
    try { await api(`/api/projects/${p.id}/settings`, { ai_credit: !!on }); p.ai_credit = !!on; }
    catch (err) { toast(err.message || "Couldn't save."); }
  }

  // ---- Learn it and Defend it share one page shape (owner decision 2026-10-13, after HackerRank,
  // Codecademy, DataCamp, LeetCode, CodeSignal, Exercism and the Bluebook test app): the code and
  // the question sit side by side, the PAGE never scrolls, each pane scrolls on its own, and the
  // action bar at the bottom of the right pane never moves.
  let LEARN = null;
  function projSplit(on) {
    $("#view-projects")?.classList.toggle("is-split", !!on);
    const pg = $("#projProg");
    if (pg && !on) { pg.hidden = true; pg.innerHTML = ""; }
  }
  // A page of its own: "< Projects" above the page title, the header's buttons hidden.
  function projPage(title) {
    const back = $("#projIdeasBack"); if (back) back.hidden = false;
    const pt = $("#projPageTitle"); if (pt) pt.textContent = title;
    const acts = $("#projHeadActs"); if (acts) acts.hidden = true;
    projSplit(true);
  }
  async function startLearn(p) {
    if (!p) return;
    LEARN = { id: p.id, title: p.title, steps: [], learned: new Set(), i: 0, pick: null, fb: {}, unfold: {}, codeOff: false };
    ["#projList", "#projForm", "#projIdeas", "#projTest", "#projBuild", "#projQuiz"].forEach(s => { if ($(s)) $(s).hidden = true; });
    QUIZ = null;
    $("#projLearn").hidden = false;
    projPage(PT("projects.learnTitle", "Learn {t}").replace("{t}", p.title));
    $("#projLearnCard").innerHTML = `<div class="empty">${esc(PT("projects.learnLoading", "Preparing your walkthrough…"))}</div>`;
    try {
      const d = await api(`/api/projects/${p.id}/learn`, {});
      if (!LEARN || LEARN.id !== p.id) return;
      LEARN.steps = d.steps || []; LEARN.learned = new Set(d.learned || []);
      const first = LEARN.steps.findIndex((_, i) => !LEARN.learned.has(i));
      LEARN.i = first < 0 ? LEARN.steps.length - 1 : first;
      renderLearn();
    } catch (err) { $("#projLearnCard").innerHTML = `<div class="empty">${esc(err.message || PT("projects.learnFailed", "Couldn't prepare the walkthrough right now."))}</div>`; }
  }
  function leaveLearn() { LEARN = null; $("#projLearn").hidden = true; projSplit(false); $("#projList").hidden = false; loadProjects(); }
  // <code-lang> The highlight.js language for an excerpt, from its file name (null = plain text).
  const CODE_LANGS = { py: "python", pyw: "python", pyi: "python", js: "javascript", mjs: "javascript", cjs: "javascript",
    jsx: "javascript", ts: "typescript", tsx: "typescript", mts: "typescript", json: "json", yml: "yaml", yaml: "yaml",
    sh: "bash", bash: "bash", zsh: "bash", go: "go", java: "java", sql: "sql", toml: "ini", ini: "ini", cfg: "ini",
    md: "markdown", markdown: "markdown", css: "css", html: "xml", htm: "xml", xml: "xml", svg: "xml" };
  function codeLang(path) {
    const name = String(path || "").split("/").pop().toLowerCase();
    if (name === "dockerfile" || name.startsWith("dockerfile.") || name.endsWith(".dockerfile")) return "dockerfile";
    if (name === "makefile") return "makefile";
    const dot = name.lastIndexOf(".");
    return dot >= 0 ? (CODE_LANGS[name.slice(dot + 1)] || null) : null;
  }
  // </code-lang>
  // Control-flow keywords take the second keyword colour, as VS Code paints them.
  const CODE_CTL = new Set(["if", "elif", "else", "for", "while", "do", "return", "break", "continue", "try", "except",
    "finally", "raise", "throw", "catch", "switch", "case", "default", "import", "from", "export", "yield", "await",
    "with", "goto", "pass", "select", "go", "defer", "fallthrough"]);
  // The excerpt as one HTML string per line. highlight.js escapes the text it is given; its output is
  // then parsed inert and REBUILT from text nodes and hljs class names only, so nothing in the file can
  // become markup, and a token that spans lines is closed and reopened at each line break.
  function codeLines(code, lang) {
    const src = String(code || ""), plain = () => src.split("\n").map(esc);
    const hl = window.hljs;
    if (!lang || !hl || !hl.getLanguage(lang)) return plain();
    let html;
    try { html = hl.highlight(src, { language: lang, ignoreIllegals: true }).value; } catch { return plain(); }
    const tpl = document.createElement("template"); tpl.innerHTML = html;
    const out = [""], stack = [];
    const put = (t) => { out[out.length - 1] += t; };
    const walk = (node) => node.childNodes.forEach(n => {
      if (n.nodeType === 3) {
        n.nodeValue.split("\n").forEach((part, i) => {
          if (i) { put("</span>".repeat(stack.length)); out.push(stack.map(c => `<span class="${c}">`).join("")); }
          put(esc(part));
        });
      } else if (n.nodeType === 1) {
        let cls = String(n.getAttribute("class") || "").split(/\s+/).filter(c => /^(hljs-[a-z_-]+|[a-z]+_+)$/.test(c));
        if (cls.includes("hljs-keyword") && CODE_CTL.has(n.textContent.trim())) cls.push("kw-ctl");
        cls = cls.join(" ");
        stack.push(cls); put(`<span class="${cls}">`); walk(n); put("</span>"); stack.pop();
      }
    });
    walk(tpl.content);
    return out.length === src.split("\n").length ? out : plain();
  }
  // <code-fold> Pure helpers for the code panel; tests/test_learn_quiz_split.py runs this block under node.
  // An excerpt longer than FOLD_OVER lines shows its focus plus FOLD_CTX lines of context, with a
  // "Show lines a to b" row above and below that opens in place.
  const FOLD_OVER = 60, FOLD_CTX = 6;
  function foldPlan(start, end, focus, open) {
    const f = Array.isArray(focus) && focus.length === 2 ? focus : [start, end];
    if (end - start + 1 <= FOLD_OVER) return { from: start, to: end, above: null, below: null };
    let from = Math.max(start, f[0] - FOLD_CTX), to = Math.min(end, f[1] + FOLD_CTX);
    if (open && open.top) from = start;
    if (open && open.bottom) to = end;
    return { from, to, above: from > start ? [start, from - 1] : null, below: to < end ? [to + 1, end] : null };
  }
  // "line 22" or "lines 4 to 8" in prose, kept only when the lines are inside the excerpt.
  function lineRefs(text, start, end) {
    const out = [], re = /\b(lines?)\s+(\d+)(?:\s*(?:to|-|and|through)\s*(\d+))?/gi;
    let m;
    while ((m = re.exec(String(text || "")))) {
      const a = +m[2], b = m[3] ? +m[3] : a;
      if (a <= b && a >= start && b <= end) out.push({ i: m.index, len: m[0].length, a, b });
    }
    return out;
  }
  // </code-fold>
  // <quiz-state> Pure helpers for the saved quiz; tests/test_learn_quiz_split.py runs this block under node.
  // Answers, marks for review and the current question are kept in this browser per attempt.
  function quizRestore(saved, attempt, total) {
    const same = !!saved && saved.attempt === attempt;
    const pad = (a, v) => { const o = Array.isArray(a) ? a.slice(0, total) : []; while (o.length < total) o.push(v); return o; };
    return { answers: pad(same ? saved.answers : null, null), flags: pad(same ? saved.flags : null, false).map(Boolean),
      i: same ? Math.min(Math.max(0, saved.i | 0), Math.max(0, total - 1)) : 0 };
  }
  function quizSnapshot(Q) { return { attempt: Q.attempt, answers: Q.answers, flags: Q.flags, i: Q.i }; }
  function quizTally(answers, flags) {
    const left = answers.filter(a => a == null || (Array.isArray(a) && !a.length)).length;
    return { answered: answers.length - left, left, marked: (flags || []).filter(Boolean).length,
      canSubmit: answers.length > 0 && left === 0 };
  }
  // </quiz-state>
  const LANG_NAMES = { python: "Python", javascript: "JavaScript", typescript: "TypeScript", json: "JSON", yaml: "YAML",
    bash: "Shell", go: "Go", java: "Java", sql: "SQL", markdown: "Markdown", css: "CSS", xml: "HTML",
    dockerfile: "Dockerfile", makefile: "Makefile" };
  function langLabel(path) {
    const l = codeLang(path), ext = String(path || "").toLowerCase().split(".").pop();
    if (l === "ini") return ext === "toml" ? "TOML" : "INI";
    return LANG_NAMES[l] || PT("projects.plainText", "Plain text");
  }
  const SVG_GRID = `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><rect x="2" y="2" width="5" height="5" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.3"/><rect x="9" y="2" width="5" height="5" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.3"/><rect x="2" y="9" width="5" height="5" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.3"/><rect x="9" y="9" width="5" height="5" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>`;
  const SVG_MARK = `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M4.5 2.5h7v11L8 10.8l-3.5 2.7Z" fill="var(--mark-fill, none)" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>`;
  // Prose with its line references as small chips; hovering or focusing one flashes those lines.
  function chipify(text, start, end) {
    const t = String(text || ""), refs = lineRefs(t, start, end);
    let out = "", at = 0;
    refs.forEach(r => {
      out += esc(t.slice(at, r.i));
      // Punctuation right after a chip stays on its line.
      const tail = (t.slice(r.i + r.len).match(/^[.,;:)!?]+/) || [""])[0];
      out += `<span class="ln-ref"><button type="button" class="ln-chip" data-a="${r.a}" data-b="${r.b}">${esc(t.substr(r.i, r.len))}</button>${esc(tail)}</span>`;
      at = r.i + r.len + tail.length;
    });
    return out + esc(t.slice(at));
  }
  // The code panel: a VS Code style tab (file, lines, language) over the coloured excerpt with its
  // line numbers. Focus lines get a faint band and a gutter marker; long excerpts fold around them.
  function codePanel(s, { focus = null, unfold = {}, toggle = true, codeOff = false } = {}) {
    const raw = String(s.code || "").split("\n"), lines = codeLines(s.code, codeLang(s.path));
    const start = +s.start || 1, end = start + raw.length - 1;
    const f = Array.isArray(focus) && focus.length === 2 ? focus : [start, end];
    const band = !(f[0] <= start && f[1] >= end);
    const plan = foldPlan(start, end, f, unfold);
    const fold = (rng, where) => rng ? `<button type="button" class="cp-fold" data-fold="${where}">${esc(PT("projects.showLines", "Show lines {a} to {b}").replace("{a}", rng[0]).replace("{b}", rng[1]))}</button>` : "";
    let body = fold(plan.above, "top");
    for (let n = plan.from; n <= plan.to; n++) {
      const k = n - start;
      body += `<span class="ln${band && n >= f[0] && n <= f[1] ? " is-focus" : ""}" data-n="${n}">${raw[k] ? lines[k] : " "}</span>`;
    }
    body += fold(plan.below, "bottom");
    const path = String(s.path || ""), name = path.split("/").pop(), dir = path.slice(0, path.length - name.length);
    const span = PT("projects.lines", "lines {a} to {b}").replace("{a}", start).replace("{b}", end);
    return `<section class="cp ${codeOff ? "is-off" : ""}" role="region" aria-label="${esc(PT("projects.codeRegion", "Code"))}">
      <div class="cp-tab"><span class="cp-file" title="${esc(path)}">${dir ? `<span class="cp-dir">${esc(dir)}</span>` : ""}${esc(name)}</span>
        <span class="cp-meta">${esc(span)}</span><span class="cp-lang">${esc(langLabel(path))}</span>
        ${toggle ? `<button type="button" class="cp-toggle" aria-expanded="${!codeOff}">${esc(codeOff ? PT("projects.showCode", "Show code") : PT("projects.hideCode", "Hide code"))}</button>` : ""}</div>
      <pre class="cp-body learn-code" tabindex="0" aria-label="${esc(path + ", " + span)}"><code>${body}</code></pre>
    </section>`;
  }
  // Wire a rendered code panel: scroll the first focus line to about a third from the top, open
  // folds in place, hide or show the code when stacked, and flash lines from the prose's chips.
  function wireCode(root, st, redraw) {
    const body = root.querySelector(".cp-body"); if (!body) return;
    const first = body.querySelector(".ln.is-focus");
    if (first) body.scrollTop = Math.max(0, first.offsetTop - body.clientHeight / 3);
    root.querySelectorAll(".cp-fold").forEach(b => b.addEventListener("click", () => {
      const top = b.dataset.fold === "top", h = body.scrollHeight, y = body.scrollTop;
      st.unfold = Object.assign({}, st.unfold, { [b.dataset.fold]: true });
      redraw();
      const nb = root.querySelector(".cp-body");
      if (nb) nb.scrollTop = top ? y + (nb.scrollHeight - h) : y;
    }));
    root.querySelector(".cp-toggle")?.addEventListener("click", () => {
      st.codeOff = !st.codeOff;
      const cp = root.querySelector(".cp"), t = root.querySelector(".cp-toggle");
      cp?.classList.toggle("is-off", st.codeOff);
      if (t) { t.setAttribute("aria-expanded", String(!st.codeOff)); t.textContent = st.codeOff ? PT("projects.showCode", "Show code") : PT("projects.hideCode", "Hide code"); }
    });
    const flash = (a, b) => {
      let el = root.querySelector(`.cp-body .ln[data-n="${a}"]`);
      if (!el && root.querySelector(".cp-fold")) { st.unfold = { top: true, bottom: true }; redraw(); el = root.querySelector(`.cp-body .ln[data-n="${a}"]`); }
      if (!el) return;
      if (st.codeOff) root.querySelector(".cp-toggle")?.click();
      const pre = root.querySelector(".cp-body");
      root.querySelectorAll(".cp-body .ln.is-flash").forEach(x => x.classList.remove("is-flash"));
      for (let n = a; n <= b; n++) { const x = root.querySelector(`.cp-body .ln[data-n="${n}"]`); if (x) { void x.offsetWidth; x.classList.add("is-flash"); } }
      const top = el.offsetTop, bottom = top + el.offsetHeight * (b - a + 1);
      if (top < pre.scrollTop || bottom > pre.scrollTop + pre.clientHeight) {
        const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
        pre.scrollTo({ top: Math.max(0, top - pre.clientHeight / 3), behavior: reduce ? "auto" : "smooth" });
      }
    };
    root.querySelectorAll(".ln-chip").forEach(c => {
      const go = () => flash(+c.dataset.a, +c.dataset.b);
      c.addEventListener("mouseenter", go); c.addEventListener("focus", go); c.addEventListener("click", go);
    });
  }
  // One lettered option row, shared by the Learn it check and the quiz: a native radio (one answer)
  // or checkbox (select all that apply), the letter, the text, and the drawn tick.
  function optRow(o, k, { name = "opt", on = false, multi = false, cls = "", disabled = false } = {}) {
    return `<label class="learn-opt ${on ? "is-pick" : ""} ${cls} ${disabled ? "is-off" : ""}" data-k="${k}">`
      + `<input class="opt-in" type="${multi ? "checkbox" : "radio"}" name="${name}" value="${k}" ${on ? "checked" : ""} ${disabled ? "disabled" : ""}>`
      + `<span class="quiz-key" aria-hidden="true">${"ABCDE"[k] || k + 1}</span><span class="opt-t">${esc(o)}</span>`
      + `<span class="opt-tick ${multi ? "is-box" : ""}" aria-hidden="true"></span></label>`;
  }
  function syncOpts(root, isOn) {
    root.querySelectorAll(".learn-opt").forEach(o => {
      const on = isOn(+o.dataset.k), inp = o.querySelector("input");
      o.classList.toggle("is-pick", on); if (inp) inp.checked = on;
    });
  }
  // A text field or a native control that uses the key itself; radios and checkboxes are not.
  const typing = (t) => !!t.closest?.("textarea, select, input:not([type=radio]):not([type=checkbox])");
  function renderLearn() {
    const L = LEARN; if (!L) return;
    const s = L.steps[L.i]; if (!s) return;
    const n = L.steps.length, done = L.learned.has(L.i), allDone = L.learned.size >= n;
    const frontier = L.steps.findIndex((_, k) => !L.learned.has(k));
    // Progress in the header: "Step 3 of 6" and the dots (done steps and the next open one revisit).
    const pg = $("#projProg");
    pg.hidden = false;
    pg.innerHTML = `<span class="prog-n">${esc(PT("projects.stepOf", "Step {n} of {m}").replace("{n}", L.i + 1).replace("{m}", n))}</span>
      <ol class="step-dots" aria-label="${esc(PT("projects.steps", "Steps"))}">${L.steps.map((st, k) => {
        const d = L.learned.has(k), cur = k === L.i, open = (d || k === frontier) && !cur;
        return `<li><button type="button" class="step-dot ${d ? "is-done" : ""} ${cur ? "is-cur" : ""}" data-k="${k}" ${open ? "" : "disabled"}
          aria-label="${esc(PT("projects.stepN", "Step {n}").replace("{n}", k + 1))}: ${esc(st.title)}${d ? `, ${esc(PT("projects.learned", "learned"))}` : ""}" ${cur ? 'aria-current="step"' : ""}></button></li>`;
      }).join("")}</ol>`;
    pg.querySelectorAll(".step-dot").forEach(b => b.addEventListener("click", () => { L.i = +b.dataset.k; L.pick = null; L.unfold = {}; renderLearn(); }));
    const c = s.check || {}, fb = L.fb[L.i], chip = (t) => chipify(t, s.start, s.end);
    const opts = c.kind === "line"
      ? `<input class="fld learn-line" id="learnLine" type="text" autocomplete="off" aria-labelledby="learnQ" placeholder="${esc(PT("projects.oneLine", "One line is enough"))}" ${done ? "disabled" : ""}>`
      : `<div class="learn-opts quiz-opts" role="radiogroup" aria-labelledby="learnQ">${(c.options || []).map((o, k) => optRow(o, k,
          { name: `learn-${L.i}`, on: L.pick === k || (fb && fb.ok && fb.k === k), cls: fb && fb.k === k ? (fb.ok ? "is-right" : "is-wrong") : "", disabled: done })).join("")}</div>`;
    const primary = done
      ? (L.i < n - 1 ? `<button class="btn btn-primary btn-sm" id="learnNext" type="button">${esc(PT("projects.nextStep", "Next step"))}</button>`
        : allDone ? `<button class="btn btn-primary btn-sm" id="learnDefend" type="button">${esc(PT("projects.defendIt", "Defend it"))}</button>`
        : `<button class="btn btn-primary btn-sm" id="learnNextOpen" type="button">${esc(PT("projects.nextOpen", "Next unlearned step"))}</button>`)
      : `<button class="btn btn-primary btn-sm" id="learnCheck" type="button" ${c.kind !== "line" && L.pick == null ? "disabled" : ""}>${esc(PT("projects.check", "Check"))}</button>`;
    // "Why this way" stays open when the right pane has the room (700px or more), otherwise it is a
    // one-line disclosure. Measured from the host before drawing, so it opens without an animation.
    const host = $("#projLearnCard");
    const paneH = host.clientHeight - (host.clientWidth < 700 && !L.codeOff ? 0.4 * innerHeight + 14 : 0);
    if (L.whyOpen == null) L.whyOpen = paneH >= 700;
    const draw = () => {
      $("#projLearnCard").innerHTML = `<div class="sx">
        ${codePanel(s, { focus: s.focus, unfold: L.unfold, codeOff: L.codeOff })}
        <section class="sx-main" role="region" aria-labelledby="learnH">
          <div class="sx-scroll">
            <h2 class="sx-h" id="learnH">${esc(s.title)}</h2>
            ${s.explain ? `<h3 class="sx-k">${esc(PT("projects.whatItDoes", "What it does"))}</h3><p class="sx-p">${chip(s.explain)}</p>` : ""}
            ${s.why ? `<details class="sx-why" ${L.whyOpen ? "open" : ""}><summary class="sx-k">${esc(PT("projects.whyWay", "Why this way"))}</summary><p>${chip(s.why)}</p></details>` : ""}
            <div class="sx-check">
              <h3 class="sx-k">${esc(PT("projects.quickCheck", "Quick check"))}</h3>
              <p class="sx-q" id="learnQ">${chip(c.q || "")}</p>
              ${opts}
              <p class="sx-fb" id="learnFb" aria-live="polite"></p>
            </div>
          </div>
          <div class="sx-bar">
            <button class="btn btn-quiet btn-sm" id="learnPrev" type="button" ${L.i === 0 ? "disabled" : ""}>${esc(PT("projects.prevStepFull", "Previous step"))}</button>
            <div class="sx-bar-r">${primary}</div>
          </div>
        </section></div>`;
      wireCode($("#projLearnCard"), L, draw);
    };
    draw();
    const card = $("#projLearnCard");
    card.querySelector(".sx-why")?.addEventListener("toggle", (e) => { L.whyOpen = e.target.open; });
    const fbEl = $("#learnFb");
    if (fb && fbEl) {
      const line = fb.ok ? `${PT("projects.right", "Right.")} ${fb.why || fb.explain || ""}`
        : fb.why ? `${PT("projects.notQuiteShort", "Not quite.")} ${fb.why}` : PT("projects.notQuite", "Not quite. Have another look at the code.");
      fbEl.className = `sx-fb ${fb.ok ? "ok" : "no"}`;
      const put = () => { fbEl.innerHTML = chip(line.trim()); wireCode(card, L, draw); };
      if (fb.fresh) { fb.fresh = false; requestAnimationFrame(put); } else put();   // a fresh line is announced
    }
    card.querySelectorAll(".opt-in").forEach(inp => inp.addEventListener("change", () => learnPick(+inp.value)));
    $("#learnLine")?.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); checkLearn(); } });
    $("#learnCheck")?.addEventListener("click", checkLearn);
    $("#learnPrev")?.addEventListener("click", () => { if (L.i > 0) { L.i -= 1; L.pick = null; L.unfold = {}; renderLearn(); } });
    $("#learnNext")?.addEventListener("click", () => { L.i += 1; L.pick = null; L.unfold = {}; renderLearn(); });
    $("#learnNextOpen")?.addEventListener("click", () => { const k = L.steps.findIndex((_, j) => !L.learned.has(j)); if (k >= 0) { L.i = k; L.pick = null; L.unfold = {}; renderLearn(); } });
    $("#learnDefend")?.addEventListener("click", () => { const p = PROJECTS.find(x => x.id === L.id) || { id: L.id, title: L.title }; $("#projLearn").hidden = true; LEARN = null; startQuiz(p); });
  }
  function learnPick(k) {
    const L = LEARN; if (!L || L.learned.has(L.i)) return;
    const card = $("#projLearnCard");
    L.pick = k; delete L.fb[L.i];
    syncOpts(card, j => j === k);
    card.querySelectorAll(".learn-opt").forEach(o => o.classList.remove("is-wrong", "is-right"));
    const chk = $("#learnCheck"); if (chk) chk.disabled = false;
    const f = $("#learnFb"); if (f) { f.textContent = ""; f.className = "sx-fb"; }
  }
  // A to D (or 1 to 4) picks a quick-check option; Enter checks, then moves on.
  document.addEventListener("keydown", (e) => {
    if (!LEARN || $("#projLearn")?.hidden || e.metaKey || e.ctrlKey || e.altKey || typing(e.target)) return;
    const s = LEARN.steps[LEARN.i], c = s && s.check;
    if (!c) return;
    if (e.key === "Enter") {
      if (e.target.closest?.("button, a, summary")) return;
      const go = ["#learnCheck", "#learnNext", "#learnDefend", "#learnNextOpen"].map(x => $(x)).find(b => b && !b.disabled);
      if (go) { e.preventDefault(); go.click(); }
      return;
    }
    if (c.kind === "line" || LEARN.learned.has(LEARN.i)) return;
    const key = e.key.toLowerCase(), k = "abcde".indexOf(key) >= 0 ? "abcde".indexOf(key) : "12345".indexOf(key);
    if (key.length === 1 && k >= 0 && k < (c.options || []).length) { e.preventDefault(); learnPick(k); }
  });
  async function checkLearn() {
    const L = LEARN; if (!L) return;
    const s = L.steps[L.i], line = s.check?.kind === "line";
    const answer = line ? ($("#learnLine")?.value || "").trim() : L.pick;
    if (line ? !answer : answer == null) return;
    const btn = $("#learnCheck"); if (btn) btn.disabled = true;
    try {
      const d = await api(`/api/projects/${L.id}/learn/${L.i}`, { answer });
      L.fb[L.i] = { ok: !!d.correct, explain: d.explain || "", why: d.why || "", k: line ? null : answer, fresh: true };
      L.learned = new Set(d.learned || []);
      const pj = PROJECTS.find(x => x.id === L.id); if (pj && d.project) Object.assign(pj, d.project);
      L.pick = line || d.correct ? null : answer;
      renderLearn();
      if (line && !d.correct) { const f = $("#learnLine"); if (f) { f.value = answer; f.focus(); } }
      else (d.correct ? $("#learnNext") || $("#learnDefend") || $("#learnNextOpen") : $("#projLearnCard .opt-in:checked"))?.focus();
    } catch (err) { toast(err.message || "Couldn't check that."); if (btn) btn.disabled = false; }
  }

  // ---- Defend it: a 20-question quiz on THIS project. The questions come in the attempt's own
  // order with shuffled options and no answer key; answers and marks for review are kept in this
  // browser as they are picked, so leaving and coming back resumes. One question per view, a
  // review grid, then the result.
  const QUIZ_KEY = (id) => `sj.quiz.${id}`;
  function quizSaved(id) { try { return JSON.parse(localStorage.getItem(QUIZ_KEY(id)) || "null"); } catch { return null; } }
  function quizSave() {
    const Q = QUIZ; if (!Q || !Q.attempt) return;
    try { localStorage.setItem(QUIZ_KEY(Q.id), JSON.stringify(quizSnapshot(Q))); } catch { /* private window */ }
  }
  function quizForget(id) { try { localStorage.removeItem(QUIZ_KEY(id)); } catch { /* private window */ } }
  const TOPIC_LABELS = () => ({ parts: PT("projects.topicParts", "Parts"), why: PT("projects.topicWhy", "Design"),
    tradeoffs: PT("projects.topicTradeoffs", "Trade-offs"), testing: PT("projects.topicTesting", "Testing"),
    scale: PT("projects.topicScale", "Scale"), extend: PT("projects.topicExtend", "Extending"), code: PT("projects.topicCode", "Reading code") });
  const LETTERS = "ABCDE";
  const letters = (a) => a == null ? "" : Array.isArray(a) ? a.map(i => LETTERS[i]).join(", ") : LETTERS[a];
  async function startQuiz(p) {
    if (!p) return;
    QUIZ = { id: p.id, title: p.title, attempt: null, qs: [], total: 0, pass: 0, answers: [], flags: [], i: 0, view: "q",
      result: null, fromReview: false, filter: "all", navOpen: false, codeOff: false, unfold: {}, sel: null, showAll: false };
    ["#projList", "#projForm", "#projIdeas", "#projTest", "#projBuild", "#projLearn"].forEach(s => { if ($(s)) $(s).hidden = true; });
    LEARN = null;
    $("#projQuiz").hidden = false;
    projPage(PT("projects.defendTitle", "Defend {t}").replace("{t}", p.title));
    const pg = $("#projProg"); if (pg) { pg.hidden = true; pg.innerHTML = ""; }
    $("#projQuizCard").innerHTML = `<div class="empty">${esc(PT("projects.quizLoading", "Preparing your quiz…"))}</div>`;
    try {
      const d = await api(`/api/projects/${p.id}/quiz`, {});
      if (!QUIZ || QUIZ.id !== p.id) return;
      Object.assign(QUIZ, { attempt: d.attempt, qs: d.questions || [], total: d.total || 0, pass: d.pass_mark || 0 });
      Object.assign(QUIZ, quizRestore(quizSaved(p.id), d.attempt, QUIZ.total));
      quizSave();
      renderQuiz();
    } catch (err) { $("#projQuizCard").innerHTML = `<div class="empty">${esc(err.message || PT("projects.quizFailed", "Couldn't open the quiz right now."))}</div>`; }
  }
  function leaveQuiz() { QUIZ = null; $("#projQuiz").hidden = true; projSplit(false); $("#projList").hidden = false; loadProjects(); }
  // The header on a question: "Question 7 of 20", the 3px bar, the Questions grid and the mark.
  function quizHead() {
    const Q = QUIZ, pg = $("#projProg"); if (!Q || !pg) return;
    if (Q.view !== "q") { pg.hidden = true; pg.innerHTML = ""; return; }
    const label = PT("projects.quizN", "Question {n} of {m}").replace("{n}", Q.i + 1).replace("{m}", Q.total);
    const frac = (Q.i + 1) / Q.total, marked = !!Q.flags[Q.i];
    const sq = Q.qs.map((q, k) => {
      const a = Q.answers[k] != null, f = !!Q.flags[k];
      const lbl = `${PT("projects.quizN", "Question {n} of {m}").replace("{n}", k + 1).replace("{m}", Q.total)}, ${a ? PT("projects.answered", "answered") : PT("projects.notAnsweredLow", "not answered")}${f ? `, ${PT("projects.markedLow", "marked")}` : ""}`;
      return `<button type="button" class="qnav-sq ${a ? "is-ans" : ""} ${f ? "is-flag" : ""} ${k === Q.i ? "is-cur" : ""}" data-k="${k}" aria-label="${esc(lbl)}" ${k === Q.i ? 'aria-current="true"' : ""}>${k + 1}</button>`;
    }).join("");
    pg.hidden = false;
    pg.innerHTML = `<span class="prog-n">${esc(label)}</span>
      <span class="quiz-rail-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(frac * 100)}" aria-label="${esc(label)}"><i style="width:${(frac * 100).toFixed(1)}%"></i></span>
      <div class="qnav">
        <button type="button" class="btn btn-ghost btn-sm hd-btn" id="quizNavBtn" aria-haspopup="dialog" aria-expanded="${Q.navOpen}" aria-controls="quizNav" title="${esc(PT("projects.questions", "Questions"))}">${SVG_GRID}<span class="hd-lbl">${esc(PT("projects.questions", "Questions"))}</span></button>
        <div class="qnav-pop" id="quizNav" role="dialog" aria-label="${esc(PT("projects.questions", "Questions"))}" ${Q.navOpen ? "" : "hidden"}>
          <div class="qnav-grid">${sq}</div>
          <div class="qnav-key"><span><i class="qk is-ans"></i>${esc(PT("projects.answeredCap", "Answered"))}</span><span><i class="qk"></i>${esc(PT("projects.notAnswered", "Not answered"))}</span><span><i class="qk is-flag"></i>${esc(PT("projects.markedCap", "Marked"))}</span></div>
        </div>
      </div>
      <button type="button" class="btn btn-ghost btn-sm hd-btn qflag ${marked ? "is-on" : ""}" id="quizFlag" aria-pressed="${marked}" title="${esc(PT("projects.markReview", "Mark for review"))} (F)">${SVG_MARK}<span class="hd-lbl">${esc(marked ? PT("projects.markedCap", "Marked") : PT("projects.mark", "Mark"))}</span></button>`;
    $("#quizNavBtn")?.addEventListener("click", (e) => { e.stopPropagation(); quizNav(!Q.navOpen); });
    pg.querySelectorAll(".qnav-sq").forEach(b => b.addEventListener("click", () => { Q.navOpen = false; quizGo(+b.dataset.k); }));
    $("#quizFlag")?.addEventListener("click", quizFlag);
  }
  function quizNav(open) {
    const Q = QUIZ; if (!Q) return;
    Q.navOpen = !!open;
    const pop = $("#quizNav"), btn = $("#quizNavBtn");
    if (pop) pop.hidden = !Q.navOpen;
    if (btn) btn.setAttribute("aria-expanded", String(Q.navOpen));
    if (Q.navOpen) (pop?.querySelector(".qnav-sq.is-cur") || pop?.querySelector(".qnav-sq"))?.focus();
  }
  function quizFlag() {
    const Q = QUIZ; if (!Q || Q.view !== "q") return;
    Q.flags[Q.i] = !Q.flags[Q.i]; quizSave(); quizHead();
    $("#quizFlag")?.focus();
  }
  document.addEventListener("click", (e) => { if (QUIZ && QUIZ.navOpen && !e.target.closest?.(".qnav")) quizNav(false); });
  function renderQuiz() {
    const Q = QUIZ; if (!Q) return;
    quizHead();
    if (Q.view === "review") return renderQuizReview();
    if (Q.view === "result") return renderQuizResult();
    const q = Q.qs[Q.i]; if (!q) return;
    const last = Q.i === Q.total - 1, pick = Q.answers[Q.i], multi = q.type === "multi";
    const picked = (k) => multi ? Array.isArray(pick) && pick.includes(k) : pick === k;
    const stem = q.code ? chipify(q.q, q.start, q.end) : esc(q.q);
    const hint = multi ? `<p class="quiz-hint" id="quizHint"><b>${esc(PT("projects.selectAll", "Select all that apply."))}</b> ${esc(PT("projects.multiRule", "It counts only when you pick every right option and no wrong one."))}</p>` : "";
    const right = Q.fromReview
      ? `${last ? "" : `<button class="btn btn-quiet btn-sm" id="quizNext" type="button">${esc(PT("projects.quizNext", "Next"))}</button>`}<button class="btn btn-primary btn-sm" id="quizToReview" type="button">${esc(PT("projects.backToReview", "Back to review"))}</button>`
      : `<button class="btn btn-primary btn-sm" id="${last ? "quizToReview" : "quizNext"}" type="button">${esc(last ? PT("projects.quizToReview", "Review answers") : PT("projects.quizNext", "Next"))}</button>`;
    const draw = () => {
      $("#projQuizCard").innerHTML = `<div class="sx ${q.code ? "" : "is-solo"}">
        ${q.code ? codePanel(q, { focus: stemFocus(q), unfold: Q.unfold, codeOff: Q.codeOff }) : ""}
        <section class="sx-main" role="region" aria-labelledby="quizQ">
          <div class="sx-scroll">
            <h2 class="sx-h quiz-q ${multi ? "has-hint" : ""}" id="quizQ">${stem}</h2>
            ${hint}
            <div class="learn-opts quiz-opts" role="${multi ? "group" : "radiogroup"}" aria-labelledby="quizQ" ${multi ? 'aria-describedby="quizHint"' : ""}>${q.options.map((o, k) => optRow(o, k, { name: `quiz-${Q.i}`, on: picked(k), multi })).join("")}</div>
          </div>
          <div class="sx-bar">
            <button class="btn btn-quiet btn-sm" id="quizPrev" type="button" ${Q.i === 0 ? "disabled" : ""}>${esc(PT("projects.prevStep", "Previous"))}</button>
            <div class="sx-bar-r">${right}</div>
          </div>
        </section></div>`;
      wireCode($("#projQuizCard"), Q, draw);
    };
    draw();
    const card = $("#projQuizCard");
    card.querySelectorAll(".opt-in").forEach(inp => inp.addEventListener("change", () => quizSet(+inp.value, inp.checked)));
    $("#quizPrev")?.addEventListener("click", () => quizGo(Q.i - 1));
    $("#quizNext")?.addEventListener("click", () => quizGo(Q.i + 1));
    $("#quizToReview")?.addEventListener("click", () => { Q.view = "review"; Q.fromReview = false; quizSave(); renderQuiz(); });
  }
  // One answer replaces the pick; select all that apply keeps a sorted list (empty = unanswered).
  function quizSet(k, on) {
    const Q = QUIZ; if (!Q || Q.view !== "q") return;
    const multi = Q.qs[Q.i]?.type === "multi";
    if (multi) {
      const cur = new Set(Array.isArray(Q.answers[Q.i]) ? Q.answers[Q.i] : []);
      if (on) cur.add(k); else cur.delete(k);
      Q.answers[Q.i] = cur.size ? [...cur].sort((a, b) => a - b) : null;
    } else Q.answers[Q.i] = k;
    quizSave();
    const a = Q.answers[Q.i];
    syncOpts($("#projQuizCard"), j => multi ? Array.isArray(a) && a.includes(j) : j === a);
  }
  function quizPick(k) {
    const Q = QUIZ; if (!Q) return;
    const a = Q.answers[Q.i], multi = Q.qs[Q.i]?.type === "multi";
    quizSet(k, multi ? !(Array.isArray(a) && a.includes(k)) : true);
  }
  // A to D (or 1 to 4) picks, F marks for review, Esc closes the Questions grid; on the result,
  // the arrow keys or J and K move through the misses.
  document.addEventListener("keydown", (e) => {
    if (!QUIZ || $("#projQuiz")?.hidden || e.metaKey || e.ctrlKey || e.altKey || typing(e.target)) return;
    const Q = QUIZ;
    if (e.key === "Escape" && Q.navOpen) { e.preventDefault(); quizNav(false); $("#quizNavBtn")?.focus(); return; }
    if (Q.view === "result") {
      const dir = { ArrowDown: 1, j: 1, J: 1, ArrowUp: -1, k: -1, K: -1 }[e.key];
      if (dir) { e.preventDefault(); resultMove(dir); }
      return;
    }
    if (Q.view !== "q") return;
    const key = e.key.toLowerCase();
    if (key === "f") { e.preventDefault(); quizFlag(); return; }
    const k = "abcd".indexOf(key) >= 0 ? "abcd".indexOf(key) : "1234".indexOf(key);
    if (key.length === 1 && k >= 0 && k < (Q.qs[Q.i]?.options.length || 0)) { e.preventDefault(); quizPick(k); }
  });
  // A question that names lines of its excerpt ("line 6") focuses them.
  function stemFocus(q) { const r = q.code ? lineRefs(q.q, q.start, q.end)[0] : null; return r ? [r.a, r.b] : null; }
  function quizGo(i) {
    const Q = QUIZ; if (!Q || i < 0 || i >= Q.total) return;
    Q.i = i; Q.view = "q"; Q.unfold = {}; quizSave(); renderQuiz();
  }
  function renderQuizReview() {
    const Q = QUIZ, t = quizTally(Q.answers, Q.flags), f = Q.filter || "all";
    const tiles = Q.qs.map((q, k) => {
      const a = Q.answers[k], open = a == null, fl = !!Q.flags[k];
      if ((f === "open" && !open) || (f === "flag" && !fl)) return "";
      const lbl = `${PT("projects.quizN", "Question {n} of {m}").replace("{n}", k + 1).replace("{m}", Q.total)}, ${open ? PT("projects.notAnsweredLow", "not answered") : letters(a)}${fl ? `, ${PT("projects.markedLow", "marked")}` : ""}`;
      return `<li><button type="button" class="rv-tile ${open ? "is-open" : ""} ${fl ? "is-flag" : ""}" data-k="${k}" aria-label="${esc(lbl)}">
        <span class="rv-top"><span class="rv-n">${k + 1}</span>${fl ? `<span class="rv-mark">${SVG_MARK}</span>` : ""}</span>
        <span class="rv-a">${esc(open ? PT("projects.notAnswered", "Not answered") : letters(a))}</span></button></li>`;
    }).join("");
    const chipB = (id, label) => `<button type="button" class="rv-chip ${f === id ? "is-on" : ""}" data-f="${id}" aria-pressed="${f === id}">${esc(label)}</button>`;
    $("#projQuizCard").innerHTML = `<div class="sx is-solo is-review">
      <section class="sx-main" role="region" aria-labelledby="rvH">
        <div class="sx-scroll">
          <h2 class="sx-h" id="rvH">${esc(PT("projects.reviewH", "Review your answers"))}</h2>
          <p class="rv-sum">${esc(PT("projects.rvSum", "{a} answered, {l} left, {m} marked").replace("{a}", t.answered).replace("{l}", t.left).replace("{m}", t.marked))}</p>
          <div class="rv-chips" role="group" aria-label="${esc(PT("projects.rvShow", "Show"))}">${chipB("all", PT("projects.rvAll", "All"))}${chipB("open", PT("projects.rvOpen", "Unanswered"))}${chipB("flag", PT("projects.rvFlag", "Marked"))}</div>
          ${tiles ? `<ol class="rv-grid">${tiles}</ol>` : `<p class="rv-none">${esc(PT("projects.rvNone", "Nothing here."))}</p>`}
        </div>
        <div class="sx-bar">
          <button class="btn btn-quiet btn-sm" id="quizRevBack" type="button">${esc(PT("projects.prevStep", "Previous"))}</button>
          <div class="sx-bar-r">${t.canSubmit ? "" : `<span class="quiz-open-n" id="quizLeft">${esc(PT("projects.answerMore", "Answer {n} more to submit").replace("{n}", t.left))}</span>`}
            <button class="btn btn-primary btn-sm" id="quizSubmit" type="button" ${t.canSubmit ? "" : 'disabled aria-describedby="quizLeft"'}>${esc(PT("projects.quizSubmit", "Submit"))}</button></div>
        </div>
      </section></div>`;
    const card = $("#projQuizCard");
    card.querySelectorAll(".rv-tile").forEach(b => b.addEventListener("click", () => { Q.fromReview = true; quizGo(+b.dataset.k); }));
    card.querySelectorAll(".rv-chip").forEach(b => b.addEventListener("click", () => { Q.filter = b.dataset.f; renderQuizReview(); card.querySelector(`.rv-chip[data-f="${Q.filter}"]`)?.focus(); }));
    $("#quizRevBack")?.addEventListener("click", () => quizGo(Q.total - 1));
    $("#quizSubmit")?.addEventListener("click", submitQuiz);
  }
  async function submitQuiz() {
    const Q = QUIZ; if (!Q || !quizTally(Q.answers, Q.flags).canSubmit) return;
    const btn = $("#quizSubmit"); if (btn) btn.disabled = true;
    try {
      const r = await api(`/api/projects/${Q.id}/quiz/submit`, { attempt: Q.attempt, answers: Q.answers });
      quizForget(Q.id);
      Q.result = r; Q.view = "result"; Q.sel = null; Q.showAll = false;
      const pj = PROJECTS.find(x => x.id === Q.id); if (pj && r.project) Object.assign(pj, r.project);
      renderQuiz();
    } catch (err) { toast(err.message || PT("projects.quizFailed", "Couldn't open the quiz right now.")); if (btn) btn.disabled = false; }
  }
  // The result rows: misses only by default, all 20 on request (a right one is your own answer).
  function resultRows() {
    const Q = QUIZ, r = Q.result || {}, wrong = new Map((r.wrong || []).map(w => [w.n, w]));
    const items = Array.isArray(r.items) && r.items.length === Q.qs.length ? r.items : null;
    const all = Q.qs.map((q, k) => {
      const w = wrong.get(k + 1), idx = (t) => q.options.indexOf(t);
      const right = w ? (Array.isArray(w.correct) ? w.correct.map(idx).filter(i => i >= 0) : idx(w.correct)) : Q.answers[k];
      return { n: k + 1, q, w, ok: !w, topic: (items && items[k].topic) || (w && w.topic) || "", you: Q.answers[k], right };
    });
    const misses = all.filter(x => !x.ok);
    return { all, misses, list: Q.showAll || !misses.length ? all : misses };
  }
  function resultMove(dir) {
    const Q = QUIZ, { list } = resultRows(); if (!list.length) return;
    const at = Math.max(0, list.findIndex(x => x.n === Q.sel));
    Q.sel = list[Math.min(list.length - 1, Math.max(0, at + dir))].n;
    renderQuizResult();
    $(`#projQuizCard .rs-row[data-n="${Q.sel}"]`)?.focus();
  }
  function renderQuizResult() {
    const Q = QUIZ, r = Q.result || {}, ok = !!r.passed, T = TOPIC_LABELS();
    const { all, misses, list } = resultRows();
    if (!list.some(x => x.n === Q.sel)) Q.sel = list.length ? list[0].n : null;
    const topics = (r.topics || []).map(t => `${T[t.topic] || t.topic} ${PT("projects.quizScore", "{s} of {m}").replace("{s}", t.right).replace("{m}", t.total)}`).join(", ");
    const row = (x) => `<li><button type="button" class="rs-row ${x.n === Q.sel ? "is-sel" : ""}" data-n="${x.n}" ${x.n === Q.sel ? 'aria-current="true"' : ""}>
        <span class="rs-l1"><span class="rs-n">Q${x.n}</span>${x.topic ? `<span class="rs-t">${esc(T[x.topic] || x.topic)}</span>` : ""}</span>
        <span class="rs-l2">${x.ok ? `<span class="rs-ok">${esc(PT("projects.youRight", "You: {a}, right").replace("{a}", letters(x.you)))}</span>`
          : esc(PT("projects.youVsRight", "You: {a}, Right: {b}").replace("{a}", x.you == null ? "-" : letters(x.you)).replace("{b}", letters(x.right)))}</span></button></li>`;
    const sel = all.find(x => x.n === Q.sel);
    const opt = (i, cls) => `<li class="${cls}"><span class="rs-letter">${LETTERS[i]}</span>${esc(sel.q.options[i])}</li>`;
    let detail = "";
    if (sel) {
      const you = sel.you == null ? [] : Array.isArray(sel.you) ? sel.you : [sel.you];
      const right = Array.isArray(sel.right) ? sel.right : sel.right == null ? [] : [sel.right];
      const multi = sel.q.type === "multi";
      detail = `${sel.q.code ? codePanel(sel.q, { focus: stemFocus(sel.q), toggle: false }) : ""}
        <div class="rs-text">
          <p class="rs-meta">${esc(PT("projects.quizN", "Question {n} of {m}").replace("{n}", sel.n).replace("{m}", Q.total))}${sel.topic ? `, ${esc(T[sel.topic] || sel.topic)}` : ""}</p>
          <h2 class="sx-h" id="rsQ">${sel.q.code ? chipify(sel.q.q, sel.q.start, sel.q.end) : esc(sel.q.q)}</h2>
          <div class="quiz-miss-a"><span class="quiz-miss-k">${esc(multi ? PT("projects.yourAnswers", "Your answers") : PT("projects.yourAnswer", "Your answer"))}</span>
            ${you.length ? `<ul class="quiz-miss-list">${you.map(i => opt(i, right.includes(i) ? (sel.ok ? "is-ok" : "is-kept") : "is-no")).join("")}</ul>` : `<span class="is-no">${esc(PT("projects.notAnswered", "Not answered"))}</span>`}</div>
          ${sel.ok ? `<p class="rs-good">${esc(PT("projects.correctAnswer", "Correct"))}</p>` : `<div class="quiz-miss-a"><span class="quiz-miss-k">${esc(PT("projects.correctAnswer", "Correct"))}</span><ul class="quiz-miss-list">${right.map(i => opt(i, "is-ok")).join("")}</ul></div>`}
          ${sel.w && sel.w.explain ? `<p class="quiz-miss-x">${esc(sel.w.explain)}</p>` : ""}
        </div>`;
    }
    const verdict = ok ? PT("projects.quizPassed", "Passed. {t} is verified.").replace("{t}", Q.title)
      : PT("projects.quizMissed", "You need {p} to pass. Go over the ones you missed, then retake it.").replace("{p}", r.pass_mark);
    $("#projQuizCard").innerHTML = `<div class="sx is-result">
      <div class="rs-band ${ok ? "is-pass" : ""}">
        <div class="quiz-score-l"><span class="quiz-score-n">${esc(PT("projects.quizScore", "{s} of {m}").replace("{s}", r.score).replace("{m}", r.total))}</span>
          <span class="quiz-score-t">${esc(verdict)}</span>
          ${topics ? `<span class="rs-topics">${esc(topics)}</span>` : ""}</div>
        <button class="btn btn-primary btn-sm" id="quizMain" type="button">${esc(ok ? PT("projects.addResume", "Add to resume") : PT("projects.retake", "Retake"))}</button>
      </div>
      <div class="rs-md">
        <section class="rs-list" role="region" aria-labelledby="rsListH">
          <div class="rs-list-h"><span id="rsListH">${esc(Q.showAll || !misses.length ? PT("projects.allQuestions", "All questions") : PT("projects.missedN", "Missed {n}").replace("{n}", misses.length))}</span>
            ${misses.length ? `<button type="button" class="btn btn-quiet btn-sm" id="rsAll" aria-pressed="${Q.showAll}">${esc(Q.showAll ? PT("projects.showMisses", "Show misses") : PT("projects.showAllN", "Show all {n}").replace("{n}", Q.total))}</button>` : ""}</div>
          <ol class="rs-rows">${list.map(row).join("")}</ol>
        </section>
        <section class="rs-main ${sel && sel.q.code ? "has-code" : ""}" role="region" aria-labelledby="rsQ">${detail}</section>
      </div></div>`;
    const card = $("#projQuizCard");
    wireCode(card, Q, renderQuizResult);
    card.querySelectorAll(".rs-row").forEach(b => b.addEventListener("click", () => { Q.sel = +b.dataset.n; renderQuizResult(); $(`#projQuizCard .rs-row[data-n="${Q.sel}"]`)?.focus(); }));
    $("#rsAll")?.addEventListener("click", () => { Q.showAll = !Q.showAll; renderQuizResult(); $("#rsAll")?.focus(); });
    card.querySelector(".rs-row.is-sel")?.scrollIntoView({ block: "nearest" });
    $("#quizMain")?.addEventListener("click", async (e) => {
      if (!ok) { const p = { id: Q.id, title: Q.title }; startQuiz(p); return; }
      e.target.disabled = true;
      try { const res = await fetch(`/api/projects/${Q.id}/tocv`, { method: "POST" }); toast(res.ok ? PT("projects.addedResume", "Added to your resume material.") : PT("projects.cantAdd", "Couldn't add it to your resume.")); }
      catch { toast(PT("projects.cantAdd", "Couldn't add it to your resume.")); }
      leaveQuiz();
    });
  }
  function renderProjList() {
    const root = $("#projList"); if (!root) return;
    const formOpen = !$("#projForm")?.hidden || !$("#projIdeas")?.hidden;
    const acts = $("#projHeadActs");
    // The ideas gallery is its own page: "Project ideas" under a back link, no header buttons.
    const ideasOpen = !$("#projIdeas")?.hidden;
    // The ideas are the Projects home: "Projects", no back link, a "Your projects N" button and
    // New project in the header. The list is its own page: "Your projects" under a back link.
    const back = $("#projIdeasBack"); if (back) back.hidden = ideasOpen;
    const pt = $("#projPageTitle");
    if (pt) pt.textContent = ideasOpen ? PT("projects.title", "Projects") : PT("projects.mine", "Your projects");
    const mine = $("#projMine"); if (mine) mine.hidden = !ideasOpen || !PROJECTS.length;
    if ($("#projMineN")) $("#projMineN").textContent = String(PROJECTS.length);
    if (ideasOpen) { if (acts) acts.hidden = false; projHeadAccent(true);   // the ideas own the accent (Start)
      root.innerHTML = ""; paintProjContinue(); return; }
    if (!PROJECTS.length) {
      // The header buttons repeat the card's actions, so they step aside while it shows:
      // one primary action per screen.
      if (acts) acts.hidden = !formOpen;
      projHeadAccent(formOpen);
      root.innerHTML = formOpen ? "" : projEmptyHTML();
      $("#projEmptyAdd")?.addEventListener("click", () => openProjForm("#projForm", "#projTitle"));
      $("#projEmptySuggest")?.addEventListener("click", () => { MYPROJ = false; openIdeas(); });
      return;
    }
    if (acts) acts.hidden = false;
    projHeadAccent(formOpen);
    const ver = PROJECTS.filter(p => p.status === "verified").length;
    const drafts = PROJECTS.length - ver;
    root.innerHTML = `<div class="proj-count">${drafts} ${drafts === 1 ? "draft" : "drafts"} &middot; ${ver} verified</div>`
      + PROJECTS.map(projCard).join("");
    root.querySelectorAll(".proj-card").forEach(card => {
      const p = PROJECTS.find(x => x.id === card.dataset.id);
      card.querySelector('[data-act="defend"]')?.addEventListener("click", () => startDefend(p));
      card.querySelector('[data-act="quiz"]')?.addEventListener("click", () => startQuiz(p));
      card.querySelector('[data-act="learn"]')?.addEventListener("click", () => startLearn(p));
      card.querySelector('[data-act="rebuild"]')?.addEventListener("click", (e) => rebuildProject(p, e.target));
      card.querySelector('[data-act="credit"]')?.addEventListener("change", (e) => setAiCredit(p, e.target.checked));
      card.querySelector('[data-act="download"]')?.addEventListener("click", () => { window.location = `/api/projects/${p.id}/download.zip`; });
      card.querySelector('[data-act="guide"]')?.addEventListener("click", () => startBuildGuide(p));
      card.querySelector('[data-act="tocv"]')?.addEventListener("click", () => addToCv(p));
      card.querySelector('[data-act="publish"]')?.addEventListener("click", (e) => publishToGithub(p, e.target));
      card.querySelector('[data-act="delete"]')?.addEventListener("click", async () => {
        await fetch(`/api/projects/${p.id}`, { method: "DELETE" }); loadProjects();
      });
    });
    pollBuilds();
  }
  // One accent per screen: while a form is open its submit is the primary, so the header's
  // New project drops to an outline button until the form closes.
  function projHeadAccent(formOpen) {
    const b = $("#projNew"); if (!b) return;
    b.classList.toggle("btn-primary", !formOpen); b.classList.toggle("btn-ghost", formOpen);
  }
  // First run: one focal card. A glyph, a few words, the path as a wordless stepper, one
  // primary action and one quiet one. The header's own buttons step aside while it shows.
  function projEmptyHTML() {
    const node = (i, ico, t) => `<li class="proj-flow-step" style="--i:${i}"><span class="proj-flow-dot">${ico}</span><span class="proj-flow-t">${t}</span></li>`;
    return `<section class="proj-empty" aria-labelledby="projEmptyH">
      <span class="proj-empty-mark" aria-hidden="true">${PROJ_ICO.code.replace(/width="16" height="16"/, 'width="26" height="26"')}<span class="proj-empty-badge">${PROJ_ICO.check}</span></span>
      <h2 class="proj-empty-h" id="projEmptyH">Show what you can build</h2>
      <p class="proj-empty-sub">A project you can explain is proof employers trust.</p>
      <ol class="proj-flow" aria-label="How it works">
        ${node(0, PROJ_ICO.plus, "Add")}${node(1, PROJ_ICO.steps, "Build")}${node(2, PROJ_ICO.talk, "Defend")}${node(3, PROJ_ICO.check, "Verified")}
      </ol>
      <div class="proj-empty-acts">
        <button class="btn btn-primary" id="projEmptyAdd" type="button">${PROJ_ICO.plus}Add a project</button>
        <button class="btn btn-quiet" id="projEmptySuggest" type="button">${PROJ_ICO.bulb}Suggest an idea</button>
      </div>
    </section>`;
  }
  function openProjForm(formSel, focusSel) {
    ["#projForm", "#projIdeas"].forEach(sel => { const f = $(sel); if (f) f.hidden = sel !== formSel; });
    renderProjList();
    $(focusSel)?.focus();
  }
  $("#projNew")?.addEventListener("click", () => { MYPROJ = true; const f = $("#projForm"); if (f) { f.hidden = !f.hidden; if (!f.hidden) { $("#projIdeas") && ($("#projIdeas").hidden = true); $("#projTitle")?.focus(); } renderProjList(); } });
  $("#projCancel")?.addEventListener("click", () => { const f = $("#projForm"); if (f) { f.reset(); f.hidden = true; } renderProjList(); });
  $("#projForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#projMsg"), setMsg = (t, cls) => { if (msg) { msg.textContent = t; msg.className = "proj-msg " + cls; msg.hidden = false; } };
    const title = $("#projTitle")?.value.trim() || "", summary = $("#projSummary")?.value.trim() || "", tech = $("#projTech")?.value.trim() || "";
    if (!title) return setMsg("Give the project a title.", "err");
    try {
      await api("/api/projects", { title, summary, tech });
      $("#projForm").reset(); $("#projForm").hidden = true; loadProjects();
    } catch (err) { setMsg(err.message || "Couldn't add the project.", "err"); }
  });

  // Project ideas: one click, no typing. The target role comes from what the app already knows
  // (last tailored job, else the profile's latest title); the chip lets the person change it.
  // Each idea is GitHub-shaped: skills it proves (gaps marked +), a repo name, and real
  // example repos from GitHub search. Start creates the draft, plus a private repo if connected.
  const PT = (k, f) => (window.I18N ? I18N.t(k, f) : f);
  // ideas is the flat list shown; batches holds the size of each batch (the first from the role or
  // search, then one per "More ideas"), so each batch is ranked on its own. filt is client-side only.
  let IDEAS = { role: "", source: "", query: "", ideas: [], batches: [], github: { connected: false }, seq: 0,
                cap: null, filt: { cat: "" }, moreBusy: false };
  let SAVED_IDEAS = null;   // the local bookmark list, loaded once per session
  const ICO_BOOKMARK = `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"><path d="M4 2.5h8v11L8 10.8 4 13.5z"/></svg>`;
  const ICO_X = `<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>`;
  const ICO_CHEV = `<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m4.5 6 3.5 3.5L11.5 6"/></svg>`;
  const ideaKey = s => String(s?.repo || s?.title || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "project";
  const isSavedIdea = s => (SAVED_IDEAS || []).some(x => ideaKey(x) === ideaKey(s));
  const GH_MARK = `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>`;
  const ICO_LOCK = `<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><rect x="3.5" y="7" width="9" height="6.5" rx="1.5"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"/></svg>`;
  const ICO_REPO = `<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"><path d="M3.5 2.5h8.5v9H4.75A1.25 1.25 0 0 0 3.5 12.75z"/><path d="M3.5 12.75A1.25 1.25 0 0 0 4.75 14h7.25v-2.5"/></svg>`;
  const ICO_STAR = `<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"><path d="m8 1.8 1.9 3.9 4.2.6-3 3 .7 4.2L8 11.5l-3.8 2 .7-4.2-3-3 4.2-.6z"/></svg>`;
  const ICO_EDIT = `<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M10.5 3 13 5.5 6 12.5H3.5V10z"/></svg>`;
  const kStars = n => n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\.0$/, "")}k` : String(n || 0);

  function openIdeas() {
    const box = $("#projIdeas"); if (!box) return;
    $("#projForm") && ($("#projForm").hidden = true);
    box.hidden = false; renderProjList();
    if (SAVED_IDEAS === null) loadSavedIdeas();
    if (!IDEAS.ideas.length) loadIdeas({}); else { renderIdeas(); refreshIdeasGitHub(); }
  }
  // GitHub state for the toolbar and Start. The ideas payload carries it; Settings tells us when
  // it changes (connect or disconnect), and reopening the gallery re-reads it.
  async function refreshIdeasGitHub() {
    try {
      const gh = await fetch("/api/github/status").then(r => r.json());
      IDEAS.github = { connected: !!gh.configured, login: gh.login || "" };
      renderIdeaGitHub();
    } catch { /* keep what we have */ }
  }
  document.addEventListener("tailor:github", refreshIdeasGitHub);
  async function openGitHubSettings() {
    await openSettings();
    document.querySelector('[data-set="connections"]')?.click();
    const f = $("#ghForm");
    if (f && f.hidden && !IDEAS.github?.connected) { f.hidden = false; if ($("#ghMsg")) $("#ghMsg").hidden = true; }
    f?.scrollIntoView?.({ block: "nearest" });
    $("#ghToken")?.focus();
  }
  // The toolbar's GitHub slot: an outline Connect button while not connected, a quiet state once it is.
  function renderIdeaGitHub() {
    const box = $("#projIdeasGh"); if (!box) return;
    const gh = IDEAS.github || {};
    if (gh.connected) {
      const who = gh.login ? `${PT("projects.ghConnectedAs", "Connected as")} ${gh.login}` : PT("projects.ghConnected", "GitHub connected");
      box.innerHTML = `<span class="proj-gh-on" title="${esc(who)}">${GH_MARK}<span>${esc(gh.login || PT("projects.ghConnected", "GitHub connected"))}</span></span>`;
    } else {
      box.innerHTML = `<button class="btn btn-ghost proj-gh-btn" id="projIdeasConnect" type="button">${GH_MARK}${esc(PT("projects.ghConnect", "Connect GitHub"))}</button>`;
      $("#projIdeasConnect")?.addEventListener("click", openGitHubSettings);
    }
  }
  function closeIdeas() { const box = $("#projIdeas"); if (box) box.hidden = true; renderProjList(); }
  const skelCards = (n, hero) => Array.from({ length: n }, (_, i) => `<div class="proj-idea is-skel${hero && !i ? " is-hero" : ""}" style="--i:${i}" aria-hidden="true"><b class="tech-tile"></b><i></i><i></i><i></i><i></i></div>`).join("");
  async function loadIdeas(body) {
    const seq = ++IDEAS.seq;
    const grid = $("#projIdeasGrid");
    renderTargetChip(body.role != null ? body.role : IDEAS.role, true);
    if (grid) grid.innerHTML = skelCards(4, true);
    const more = $("#projIdeasMoreRow"); if (more) more.hidden = true;
    grid?.setAttribute("aria-busy", "true");
    try {
      const d = await api("/api/projects/ideas", body);
      if (seq !== IDEAS.seq) return;   // a newer request superseded this one
      const ideas = d.ideas || [];
      IDEAS = { ...IDEAS, role: d.role || "", source: d.source || "", query: d.query || "", ideas,
                batches: d.batches && d.batches.length ? d.batches : [ideas.length],
                github: d.github || { connected: false }, cap: d.cap || IDEAS.cap, moreBusy: false };
      const q = $("#projIdeasQ"); if (q && document.activeElement !== q) q.value = IDEAS.query;
      renderIdeas();
    } catch (err) {
      if (seq !== IDEAS.seq) return;
      if (err.data?.code === "ideas_cap") {
        // The day's batches are spent: keep what is on screen, say so once, and park the controls.
        IDEAS.cap = err.data.cap || IDEAS.cap;
        if (IDEAS.ideas.length) { renderIdeas({ still: true }); return; }
      }
      renderTargetChip(IDEAS.role, false);
      renderIdeaCap();
      if (grid) grid.innerHTML = `<p class="proj-ideas-empty">${esc(err.message || PT("projects.noIdeas", "No ideas came back. Try another role."))}</p>`;
    } finally { grid?.removeAttribute("aria-busy"); }
  }
  // More ideas: another batch for the same role and search, told what is already on screen so it
  // does not repeat (the server also drops repeats by title and repo name). A skeleton row stands
  // in while it loads; the cards already shown do not move or re-animate.
  async function loadMoreIdeas() {
    if (IDEAS.moreBusy || !IDEAS.ideas.length || capSpent()) return;
    const seq = IDEAS.seq, grid = $("#projIdeasGrid");
    IDEAS.moreBusy = true;
    const btn = $("#projIdeasMore"); if (btn) btn.disabled = true;
    const skel = `<div class="proj-ideas-skelrow" data-more-skel aria-hidden="true">${skelCards(3, false)}</div>`;
    const legend = grid?.querySelector(".proj-ideas-legend");
    if (legend) legend.insertAdjacentHTML("beforebegin", skel); else grid?.insertAdjacentHTML("beforeend", skel);
    grid?.setAttribute("aria-busy", "true");
    try {
      const d = await api("/api/projects/ideas", { role: IDEAS.role, query: IDEAS.query, exclude: IDEAS.ideas.map(s => s.title) });
      if (seq !== IDEAS.seq) return;
      IDEAS.cap = d.cap || IDEAS.cap;
      const seen = new Set(IDEAS.ideas.flatMap(s => [String(s.title || "").toLowerCase(), ideaKey(s)]));
      const fresh = (d.ideas || []).filter(s => !seen.has(String(s.title || "").toLowerCase()) && !seen.has(ideaKey(s)));
      const before = IDEAS.batches.length;
      if (fresh.length) { IDEAS.ideas = IDEAS.ideas.concat(fresh); IDEAS.batches = IDEAS.batches.concat([fresh.length]); }
      else toast(PT("projects.noMoreIdeas", "No new ideas came back. Try a search."));
      IDEAS.moreBusy = false;
      renderIdeas({ still: before });
      grid?.removeAttribute("aria-busy");
    } catch (err) {
      grid?.removeAttribute("aria-busy");
      if (seq !== IDEAS.seq) return;
      IDEAS.moreBusy = false;
      if (err.data?.code === "ideas_cap") IDEAS.cap = err.data.cap || IDEAS.cap;
      else toast(err.message || PT("projects.noIdeas", "No ideas came back. Try another role."));
      renderIdeas({ still: true });
    }
  }
  const capSpent = () => !!IDEAS.cap && IDEAS.cap.left <= 0;
  // The daily cap: one quiet line, and the controls that would ask for a new batch rest until
  // tomorrow. Cached sets, filters, saved ideas and Start all keep working.
  function renderIdeaCap() {
    const spent = capSpent(), line = $("#projIdeasCap");
    if (line) {
      line.hidden = !spent;
      line.textContent = spent ? PT("projects.capReached", "You've reached today's {n} idea batches. More tomorrow.").replace("{n}", IDEAS.cap.limit) : "";
    }
    const q = $("#projIdeasQ"); if (q) q.disabled = spent;
    const more = $("#projIdeasMore"); if (more) more.disabled = spent || IDEAS.moreBusy;
  }
  function renderTargetChip(role, busy) {
    const box = $("#projTargetBox"); if (!box) return;
    const src = IDEAS.source === "job" ? PT("projects.fromJob", "From your last resume")
      : IDEAS.source === "profile" ? PT("projects.fromProfile", "From your profile") : "";
    box.innerHTML = `<button class="proj-target-chip" id="projTargetChip" type="button" title="${esc(PT("projects.changeRole", "Change the role"))}" ${busy ? "disabled" : ""}>
        <span class="proj-target-k">${esc(PT("projects.target", "Target"))}</span>
        <span class="proj-target-v">${esc(role || PT("projects.addRole", "Add a role"))}</span>${ICO_EDIT}</button>
      ${src && !busy ? `<span class="proj-target-src">${esc(src)}</span>` : ""}`;
    $("#projTargetChip")?.addEventListener("click", editTarget);
  }
  // Inline edit: the chip becomes a field. Enter fetches ideas for the new role, Esc puts it back.
  function editTarget() {
    const box = $("#projTargetBox"); if (!box) return;
    box.innerHTML = `<form class="proj-target-form" id="projTargetForm">
        <span class="proj-target-k">${esc(PT("projects.target", "Target"))}</span>
        <input class="proj-target-in" id="projTargetIn" type="text" value="${esc(IDEAS.role)}" placeholder="${esc(PT("projects.rolePh", "e.g. Data Engineer"))}" aria-label="${esc(PT("projects.target", "Target"))}" autocomplete="off" spellcheck="false">
      </form>`;
    const inp = $("#projTargetIn"); inp?.focus(); inp?.select();
    let done = false;
    const finish = (role) => {
      if (done) return; done = true;
      const r = (role || "").trim();
      if (r && r.toLowerCase() !== (IDEAS.role || "").toLowerCase()) loadIdeas({ role: r });
      else renderTargetChip(IDEAS.role, false);
    };
    $("#projTargetForm")?.addEventListener("submit", e => { e.preventDefault(); finish(inp.value); });
    inp?.addEventListener("keydown", e => { if (e.key === "Escape") { e.preventDefault(); finish(""); } });
    inp?.addEventListener("blur", () => finish(""));
  }
  // The ideas gallery (2026-10-13 layout; 2026-10 harmony pass: the cards use the Jobs card frame).
  // Layout (E): rank the ideas by how many of the job's missing skills each one covers, so the best
  // fit leads as a wide hero with a fit ring, and the rest follow in a bento with a fit meter.
  // Ties fall back to the title, so the order never shuffles.
  function rankIdeas(ideas) {
    const all = new Set();
    ideas.forEach(s => (s.gaps || []).forEach(g => all.add(g.toLowerCase())));
    const total = all.size;
    const ranked = ideas.map((s, i) => ({ s, i, fit: new Set((s.gaps || []).map(g => g.toLowerCase())).size, n: (s.skills || []).length }))
      .sort((a, b) => b.fit - a.fit || (total ? 0 : b.n - a.n)
        || String(a.s.title || "").localeCompare(String(b.s.title || "")));
    return { ranked, total };
  }
  // The rows after the hero: pairs first (the higher ranked get more room), then threes.
  function bentoSpans(r) {
    const rows = [];
    if (r === 1) rows.push(1);
    else if (r % 3 === 1) { rows.push(2, 2); r -= 4; }
    else if (r % 3 === 2) { rows.push(2); r -= 2; }
    for (; r > 0; r -= 3) rows.push(3);
    return rows.flatMap(n => Array(n).fill(6 / n));
  }
  function fitText(fit, total, n) {
    const fill = (t) => t.replace("{n}", fit).replace("{total}", total).replace("{k}", n);
    if (total && !fit) return PT("projects.buildsOnSkills", "Builds on skills you already have");
    return fill(total === 1 ? PT("projects.coversGap", "Covers {n} of your 1 gap")
      : total ? PT("projects.coversGaps", "Covers {n} of your {total} gaps")
      : PT("projects.skillsToShow", "{k} skills to show"));
  }
  // Fit (2026-10, harmony pass): the hero keeps a ring in the app's own `.li-ring` grammar (a
  // --border track, a --good arc, no gradient or glow); the smaller cards say it in plain words.
  function fitRing(fit, total, n) {
    const num = total ? fit : n, den = total || 5, frac = Math.max(0, Math.min(1, num / den));
    const R = 42, C = 2 * Math.PI * R;
    return `<div class="proj-ring" role="img" aria-label="${esc(fitText(fit, total, n))}">
      <svg viewBox="0 0 100 100" aria-hidden="true" fill="none">
        <circle class="proj-ring-track" cx="50" cy="50" r="${R}"/>
        <circle class="proj-ring-arc" cx="50" cy="50" r="${R}" stroke-dasharray="${(C * frac).toFixed(2)} ${C.toFixed(2)}" transform="rotate(-90 50 50)"/>
      </svg>
      <span class="proj-ring-n"><span><b>${num}</b><small>/${den}</small></span></span>
    </div>`;
  }
  function fitMeter(fit, total, n) {
    return `<p class="proj-fit">${esc(fitText(fit, total, n))}</p>`;
  }

  // Each idea gets a category (a small deterministic keyword score over its title, pitch, skills
  // and tech). It sits where the Jobs card puts the company: a neutral 40px tile with the category
  // glyph, and the category name as the line above the title. No per-category colour.
  const IDEA_CATS = {
    data:   { k: ["data", "dashboard", "analytics", "pandas", "sql", "chart", "tracker", "insight", "etl", "warehouse", "spark", "tableau", "power bi", "excel", "dbt", "visuali", "statistic", "streamlit", "dataset"],
              label: ["projects.catData", "Data and analytics"],
              ico: `<path d="m4 16 4.6-4.8 3.6 3 7.3-7.7"/><path d="M15 6.5h4.5V11"/>` },
    web:    { k: ["api", "rest", "web", "react", "node", "fastapi", "flask", "django", "frontend", "backend", "graphql", "typescript", "javascript", "next", "vue", "html", "css", "express", "full stack"],
              label: ["projects.catWeb", "Web and APIs"],
              ico: `<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.4 2.6 3.6 5.4 3.6 8.5S14.4 17.9 12 20.5M12 3.5C9.6 6.1 8.4 8.9 8.4 12s1.2 5.9 3.6 8.5"/>` },
    devops: { k: ["docker", "kubernetes", "ci", "cd", "devops", "pipeline", "airflow", "terraform", "deploy", "monitor", "alert", "aws", "gcp", "azure", "cloud", "infra", "github actions", "observab", "linux", "nightly"],
              label: ["projects.catDevops", "DevOps and CI"],
              ico: `<path d="M7.2 8C4.9 8 3 9.8 3 12s1.9 4 4.2 4c3.4 0 6.2-8 9.6-8C19.1 8 21 9.8 21 12s-1.9 4-4.2 4c-3.4 0-6.2-8-9.6-8Z"/>` },
    ml:     { k: ["machine learning", "ml", "model", "nlp", "spacy", "pytorch", "tensorflow", "llm", "ai", "classif", "predict", "neural", "embedding", "scikit", "sklearn", "vision", "recommend", "matcher", "keyword"],
              label: ["projects.catMl", "Machine learning"],
              ico: `<path d="M12 2.8 13.9 9l6.3 1.9-6.3 1.9L12 19.1l-1.9-6.3L3.8 10.9 10.1 9Z"/><path d="M19 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8Z"/>` },
    mobile: { k: ["mobile", "ios", "android", "swift", "kotlin", "flutter", "react native", "expo", "app store", "swiftui", "jetpack"],
              label: ["projects.catMobile", "Mobile"],
              ico: `<rect x="6.5" y="2.5" width="11" height="19" rx="2.6"/><path d="M10.5 18.5h3"/>` },
    tools:  { k: ["cli", "tool", "script", "automation", "bot", "scheduler", "command", "extension", "plugin", "parser", "library", "sdk", "rust", "go", "bash"],
              label: ["projects.catTools", "Tools and CLI"],
              ico: `<rect x="2.5" y="4" width="19" height="16" rx="2.6"/><path d="m7 9.5 3 2.5-3 2.5M12.5 15h4.5"/>` },
  };
  function ideaCat(s) {
    const hay = (f, w) => ({ text: ` ${String(f || "").toLowerCase()} `, w });
    const fields = [hay(s.title, 3), hay(s.pitch, 1), hay((s.skills || []).join(" , "), 2), hay(s.tech, 2), hay(s.search, 1)];
    let best = "tools", top = 0;
    for (const [cat, def] of Object.entries(IDEA_CATS)) {
      let score = 0;
      for (const k of def.k) {
        // Short keys (ml, ai, ci, go) must stand alone as words; longer ones may be a stem.
        const re = k.length <= 3 ? new RegExp(`[^a-z]${k}[^a-z]`) : null;
        for (const f of fields) if (re ? re.test(f.text) : f.text.includes(k)) score += f.w;
      }
      if (score > top) { top = score; best = cat; }
    }
    return best;
  }
  const catGlyph = (cat, cls) => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${IDEA_CATS[cat].ico}</svg>`;

  function ideaCard(s, i, opt = {}) {
    const { hero = false, span = 6, fit = 0, total = 0, compact = false } = opt;
    const gaps = new Set((s.gaps || []).map(g => g.toLowerCase()));
    // The skills the job wants and the profile lacks lead the row: they are the reason to build it.
    // They wear the Jobs page's must-have gap chip; the rest wear its plain tag.
    const sk = (s.skills || []).map((k, j) => ({ k, j, gap: gaps.has(k.toLowerCase()) })).sort((a, b) => b.gap - a.gap || a.j - b.j);
    const chips = sk.map(({ k, gap }) => gap
      ? `<li class="proj-skill is-gap" title="${esc(PT("projects.gapHint", "The job asks for this. Your profile doesn't show it yet."))}">${PROJ_ICO.plus}${esc(k)}</li>`
      : `<li class="proj-skill">${esc(k)}</li>`).join("");
    // Example repos in GitHub's own grammar, all neutral: repo icon, owner/name, stars. (The language
    // was dropped in the harmony pass: it truncated the repo names in the hero's three columns.)
    const ex = (s.examples || []).filter(e => /^https:\/\/github\.com\//.test(e.url || ""));
    const exHTML = ex.length ? `<div class="proj-idea-ex"><span class="proj-idea-ex-k">${esc(PT("projects.examples", "Examples"))}</span>
        ${ex.map(e => {
          const [own, name] = String(e.full_name || "").split("/");
          return `<a class="proj-ex" href="${esc(e.url)}" target="_blank" rel="noopener" title="${esc(e.description || e.full_name)}">${ICO_REPO}<span class="proj-ex-n"><span class="proj-ex-o">${esc(own || "")}/</span>${esc(name || "")}</span><span class="proj-ex-s">${ICO_STAR}${kStars(e.stars)}</span></a>`;
        }).join("")}
      </div>` : "";
    const n = (s.skills || []).length;
    const cat = ideaCat(s), [lk, lf] = IDEA_CATS[cat].label;
    const saved = isSavedIdea(s), saveLbl = saved ? PT("projects.savedIdea", "Saved") : PT("projects.saveIdea", "Save idea");
    const pills = `${hero ? `<span class="proj-idea-pill is-best">${esc(total ? PT("projects.bestMatch", "Best match") : PT("projects.topPick", "Top pick"))}</span>` : ""}<button class="proj-idea-save" type="button" data-save aria-pressed="${saved}" aria-label="${esc(saveLbl)}" title="${esc(saveLbl)}">${ICO_BOOKMARK}</button>`;
    // The Jobs card head: a 40px tile, the "company" line (here the category), the title under it,
    // and the pills top-right where Jobs shows "New 4h ago".
    const head = `<div class="proj-idea-hd">
        ${techTile(s, catGlyph(cat, ""), "proj-idea-logo")}
        <div class="proj-idea-main">
          <div class="proj-idea-top"><span class="proj-idea-cat">${esc(PT(lk, lf))}</span><span class="proj-idea-tags">${pills}</span></div>
          <h3 class="proj-idea-t">${esc(s.title)}</h3>
        </div>
      </div>`;
    const ft = `<div class="proj-idea-ft">
        <span class="proj-idea-repo" title="${esc(PT("projects.repoName", "Repo name"))}">${ICO_REPO}${esc(s.repo || "")}</span>
        <button class="btn ${hero ? "btn-primary" : "btn-ghost"} btn-sm" data-start type="button">${esc(PT("projects.start", "Start this project"))}</button>
      </div>`;
    if (hero) {
      return `<article class="proj-idea is-hero${opt.still ? " is-still" : ""}" data-i="${i}" data-key="${esc(ideaKey(s))}" data-cat="${cat}" style="--i:0">
        ${head}
        <div class="proj-idea-body">
          <div class="proj-hero-main">
            ${s.pitch ? `<p class="proj-idea-p">${esc(s.pitch)}</p>` : ""}
            ${chips ? `<ul class="proj-skills">${chips}</ul>` : ""}
            ${exHTML}
          </div>
          <aside class="proj-hero-fit">
            ${fitRing(fit, total, n)}
            <p class="proj-hero-fit-t">${esc(fitText(fit, total, n))}</p>
          </aside>
          ${ft}
        </div>
      </article>`;
    }
    return `<article class="proj-idea${span <= 2 ? " is-tight" : ""}${opt.still ? " is-still" : ""}" data-i="${i}" data-key="${esc(ideaKey(s))}" data-cat="${cat}" style="--i:${opt.order || 0};--span:${span}">
      ${head}
      <div class="proj-idea-body">
        ${s.pitch ? `<p class="proj-idea-p">${esc(s.pitch)}</p>` : ""}
        ${compact ? "" : fitMeter(fit, total, n)}
        ${chips ? `<ul class="proj-skills">${chips}</ul>` : ""}
        ${exHTML}
        ${ft}
      </div>
    </article>`;
  }
  // opt.still: true keeps every card still (a filter or a cap change); a number keeps the first
  // that many batches still, so only an appended batch animates in.
  function renderIdeas(opt = {}) {
    renderTargetChip(IDEAS.role, false);
    const grid = $("#projIdeasGrid"); if (!grid) return;
    renderIdeaGitHub();
    renderIdeaCap();
    const qline = $("#projIdeasQuery");
    if (qline) {
      qline.hidden = !IDEAS.query;
      qline.innerHTML = IDEAS.query ? `<span>${esc(PT("projects.resultsFor", "Results for"))} <b>${esc(IDEAS.query)}</b></span>
        <button class="proj-q-clear" id="projIdeasQClear" type="button" aria-label="${esc(PT("projects.clearSearch", "Clear search"))}" title="${esc(PT("projects.clearSearch", "Clear search"))}">${ICO_X}</button>` : "";
      $("#projIdeasQClear")?.addEventListener("click", () => {
        const q = $("#projIdeasQ"); if (q) q.value = "";
        loadIdeas({ role: IDEAS.role });   // the role's own set, from the cache: no new batch
      });
    }
    renderSavedIdeas();
    renderIdeaFilters();
    renderIdeaGrid(opt);
  }
  // The category filter is client-side only: it narrows the ideas already loaded and never calls
  // the model. It is the Jobs filter select ("Any time", "Any type"), listing the categories present.
  const ideaPasses = s => !IDEAS.filt.cat || ideaCat(s) === IDEAS.filt.cat;
  function renderIdeaFilters() {
    const sel = $("#projIdeasCat"); if (!sel) return;
    const cats = Object.keys(IDEA_CATS).filter(c => IDEAS.ideas.some(s => ideaCat(s) === c));
    if (IDEAS.filt.cat && !cats.includes(IDEAS.filt.cat)) IDEAS.filt.cat = "";
    sel.innerHTML = `<option value="">${esc(PT("projects.anyCategory", "Any category"))}</option>`
      + cats.map(c => `<option value="${c}">${esc(PT(IDEA_CATS[c].label[0], IDEA_CATS[c].label[1]))}</option>`).join("");
    sel.value = IDEAS.filt.cat;
    sel.disabled = IDEAS.ideas.length < 2;
  }
  $("#projIdeasCat")?.addEventListener("change", (e) => { IDEAS.filt.cat = e.currentTarget.value; renderIdeaGrid({ still: true }); });
  // Each batch is ranked on its own: the first batch leads with the hero, later batches flow in
  // the bento grid under it, so appending never reshuffles what the person is already reading.
  function renderIdeaGrid(opt = {}) {
    const grid = $("#projIdeasGrid"); if (!grid) return;
    const moreRow = $("#projIdeasMoreRow");
    if (moreRow) moreRow.hidden = !IDEAS.ideas.length;
    if (!IDEAS.ideas.length) { grid.innerHTML = `<p class="proj-ideas-empty">${esc(PT("projects.noIdeas", "No ideas came back. Try another role."))}</p>`; return; }
    const sizes = IDEAS.batches.length ? IDEAS.batches.slice() : [IDEAS.ideas.length];
    const used = sizes.reduce((a, b) => a + b, 0);
    if (used < IDEAS.ideas.length) sizes.push(IDEAS.ideas.length - used);
    const { total } = rankIdeas(IDEAS.ideas);
    let html = "", start = 0, shown = 0;
    sizes.forEach((n, b) => {
      const batch = IDEAS.ideas.slice(start, start + n).map((s, j) => ({ s, i: start + j })).filter(x => ideaPasses(x.s));
      start += n;
      if (!batch.length) return;
      const ranked = rankIdeas(batch.map(x => x.s)).ranked.map(r => ({ ...r, i: batch[r.i].i }));
      const hero = b === 0, still = opt.still === true || (typeof opt.still === "number" && b < opt.still);
      const spans = bentoSpans(hero ? ranked.length - 1 : ranked.length);
      html += ranked.map((r, k) => ideaCard(r.s, r.i, { hero: hero && k === 0, span: (hero ? spans[k - 1] : spans[k]) || 6,
        fit: r.fit, total, order: k, still })).join("");
      shown += ranked.length;
    });
    const hasGap = IDEAS.ideas.some(s => ideaPasses(s) && (s.gaps || []).length);
    grid.innerHTML = (shown ? html : `<p class="proj-ideas-empty">${esc(PT("projects.noMatch", "No ideas match these filters."))}</p>`)
      + (hasGap ? `<p class="proj-ideas-legend"><span class="proj-skill is-gap" aria-hidden="true">${PROJ_ICO.plus}</span>${esc(PT("projects.newForYou", "A skill the job asks for that your profile doesn't show yet"))}</p>` : "");
    bindIdeaCards(grid, IDEAS.ideas);
  }
  function bindIdeaCards(root, list) {
    root.querySelectorAll(".proj-idea:not(.is-skel)").forEach(card => {
      const s = list[+card.dataset.i];
      card.querySelector("[data-start]")?.addEventListener("click", (e) => startIdea(s, e.currentTarget));
      card.querySelector("[data-save]")?.addEventListener("click", (e) => toggleSaveIdea(s, e.currentTarget));
    });
  }

  // Saved ideas: a local bookmark list (data/saved_ideas.json), shown as a collapsible row at the
  // top of the gallery. They survive refreshes, searches and restarts; Start works as anywhere.
  const SAVED_OPEN_KEY = "tailor-saved-ideas-open";
  const savedOpen = () => { try { return localStorage.getItem(SAVED_OPEN_KEY) !== "0"; } catch { return true; } };
  async function loadSavedIdeas() {
    try { SAVED_IDEAS = (await api("/api/projects/ideas/saved")).saved || []; }
    catch { SAVED_IDEAS = SAVED_IDEAS || []; }
    renderSavedIdeas(); syncSaveToggles();
  }
  function renderSavedIdeas() {
    const box = $("#projSaved"); if (!box) return;
    const list = SAVED_IDEAS || [];
    box.hidden = !list.length;
    if (!list.length) { box.innerHTML = ""; return; }
    const open = savedOpen();
    box.innerHTML = `<button class="proj-saved-hd" id="projSavedToggle" type="button" aria-expanded="${open}" aria-controls="projSavedGrid">
        <span class="proj-ideas-h">${esc(PT("projects.savedIdeas", "Saved ideas"))}</span><span class="proj-saved-n">${list.length}</span>${ICO_CHEV}</button>
      <div class="proj-ideas-grid proj-saved-grid" id="projSavedGrid"${open ? "" : " hidden"}>
        ${list.map((s, i) => ideaCard(s, i, { span: 3, compact: true, still: true })).join("")}
      </div>`;
    $("#projSavedToggle")?.addEventListener("click", () => {
      try { localStorage.setItem(SAVED_OPEN_KEY, savedOpen() ? "0" : "1"); } catch {}
      renderSavedIdeas();
    });
    bindIdeaCards($("#projSavedGrid"), list);
  }
  function syncSaveToggles() {
    document.querySelectorAll("#projIdeasGrid .proj-idea[data-key]").forEach(card => {
      const btn = card.querySelector("[data-save]"); if (!btn) return;
      const on = (SAVED_IDEAS || []).some(x => ideaKey(x) === card.dataset.key);
      const lbl = on ? PT("projects.savedIdea", "Saved") : PT("projects.saveIdea", "Save idea");
      btn.setAttribute("aria-pressed", String(on)); btn.setAttribute("aria-label", lbl); btn.title = lbl;
    });
  }
  async function toggleSaveIdea(s, btn) {
    if (!s) return;
    if (btn) btn.disabled = true;
    try {
      const r = isSavedIdea(s)
        ? await fetch(`/api/projects/ideas/saved?repo=${encodeURIComponent(ideaKey(s))}`, { method: "DELETE" }).then(x => x.json())
        : await api("/api/projects/ideas/saved", { idea: s });
      if (r.error) throw new Error(r.error);
      SAVED_IDEAS = r.saved || [];
      renderSavedIdeas(); syncSaveToggles();
    } catch (err) {
      toast(err.message || PT("projects.saveFailed", "Couldn't save the idea."));
    } finally { if (btn && btn.isConnected) btn.disabled = false; }
  }
  // Start while GitHub is not connected asks first, on the card itself: connect (the Settings flow)
  // or start here without a repo. Nothing is created until one of the two is picked.
  function askGitHubFirst(s, btn) {
    const ft = btn?.closest(".proj-idea-ft"); if (!ft) return startIdea(s, btn, true);
    if (ft.querySelector(".proj-idea-ghask")) return;
    const kids = [...ft.children];
    kids.forEach(k => { k.hidden = true; });
    ft.insertAdjacentHTML("beforeend", `<div class="proj-idea-ghask" role="group" aria-label="${esc(PT("projects.ghPrompt", "Connect GitHub to create the repo"))}">
        <span class="proj-idea-ghask-t">${GH_MARK}${esc(PT("projects.ghPrompt", "Connect GitHub to create the repo"))}</span>
        <span class="proj-idea-ghask-a">
          <button class="btn btn-quiet btn-sm" type="button" data-nogh>${esc(PT("projects.buildNoGh", "Build without GitHub"))}</button>
          <button class="btn btn-ghost btn-sm" type="button" data-gh>${GH_MARK}${esc(PT("projects.connect", "Connect"))}</button>
        </span>
      </div>`);
    const ask = ft.querySelector(".proj-idea-ghask");
    const close = () => { ask?.remove(); kids.forEach(k => { k.hidden = false; }); };
    ask.querySelector("[data-gh]")?.addEventListener("click", () => { close(); openGitHubSettings(); });
    ask.querySelector("[data-nogh]")?.addEventListener("click", () => { close(); startIdea(s, btn, true); });
    ask.addEventListener("keydown", e => { if (e.key === "Escape") { e.preventDefault(); close(); btn.focus(); } });
    ask.querySelector("[data-gh]")?.focus();
  }
  async function startIdea(s, btn, local = false) {
    if (!s) return;
    if (!local && !IDEAS.github?.connected) return askGitHubFirst(s, btn);
    const label = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = PT("projects.starting", "Starting…"); }
    try {
      // The AI builds it (one package). The card shows the progress; the build carries on if
      // the person leaves the screen.
      const d = await api("/api/projects/start", { idea: s, local: !!local });
      toast(d.project?.target === "github" ? PT("projects.buildingRepo", "Building it. It goes to a private repo on GitHub.")
        : PT("projects.buildingLocal", "Building it here. Download the files any time."));
      const box = $("#projIdeas"); if (box) box.hidden = true;
      MYPROJ = true; loadProjects();            // the new project, building, on Your projects
    } catch (err) {
      toast(err.message || PT("projects.startFailed", "Couldn't start the project."));
      if (btn) { btn.disabled = false; btn.textContent = label; }
    }
  }
  $("#projSuggestBtn")?.addEventListener("click", () => { const box = $("#projIdeas"); if (box && !box.hidden) closeIdeas(); else openIdeas(); });
  $("#projIdeasBack")?.addEventListener("click", () => {
    if (QUIZ) leaveQuiz(); else if (LEARN) leaveLearn();
    else { MYPROJ = false; const f = $("#projForm"); if (f) { f.reset(); f.hidden = true; } openIdeas(); }   // the list goes back to the ideas
  });
  $("#projMine")?.addEventListener("click", showMyProjects);
  // Esc goes back too, unless a field inside the gallery (the Target chip, an inline ask) took it.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || e.defaultPrevented) return;
    if (LEARN && !$("#projLearn")?.hidden && $("#view-projects")?.classList.contains("is-active")
        && !typing(e.target)) { e.preventDefault(); leaveLearn(); return; }
    if (QUIZ && !$("#projQuiz")?.hidden && $("#view-projects")?.classList.contains("is-active")) { e.preventDefault(); leaveQuiz(); return; }
    // The ideas are the Projects home, so Esc there does nothing; on Your projects it goes home.
    if (!MYPROJ || !$("#view-projects")?.classList.contains("is-active") || typing(e.target)) return;
    if (!$("#projList") || $("#projList").hidden) return;
    if (document.querySelector(".modal:not([hidden]), dialog[open]")) return;
    e.preventDefault(); $("#projIdeasBack")?.click();
  });
  $("#projIdeasMore")?.addEventListener("click", loadMoreIdeas);
  $("#projIdeasSearch")?.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = ($("#projIdeasQ")?.value || "").replace(/\s+/g, " ").trim();
    if (q.toLowerCase() === (IDEAS.query || "").toLowerCase()) return;
    if (q && capSpent()) return;
    loadIdeas(q ? { role: IDEAS.role, query: q } : { role: IDEAS.role });
  });

  // Guided build-and-teach loop: an ordered plan that teaches as you build, with a per-step coach.
  async function startBuildGuide(p) {
    if (!p) return;
    BUILD = { id: p.id, title: p.title, milestones: p.plan?.milestones || [], done: new Set(p.done || []) };
    $("#projBuildTitle").textContent = `Build: ${p.title}`;
    $("#projList").hidden = true; $("#projForm").hidden = true; $("#projIdeas").hidden = true; $("#projBuild").hidden = false;
    $("#projBuildFoot").hidden = true;
    if (BUILD.milestones.length) { renderPlan(); }
    else {
      $("#projPlan").innerHTML = `<div class="empty">Building your step-by-step plan…</div>`;
      try {
        const d = await api(`/api/projects/${p.id}/plan`, {});   // {} body -> POST
        BUILD.milestones = d.plan?.milestones || []; BUILD.done = new Set(d.done || []);
        BUILD.milestones.length ? renderPlan() : ($("#projPlan").innerHTML = `<div class="empty">Couldn't build a plan right now.</div>`);
      } catch { $("#projPlan").innerHTML = `<div class="empty">Couldn't build a plan right now.</div>`; }
    }
  }
  function renderPlan() {
    const root = $("#projPlan"); if (!root) return;
    root.innerHTML = BUILD.milestones.map((m, i) => {
      const done = BUILD.done.has(i);
      return `<div class="proj-ms ${done ? "done" : ""}" data-i="${i}">
        <div class="proj-ms-hd">
          <label class="proj-ms-check"><input type="checkbox" data-step ${done ? "checked" : ""}><span class="proj-ms-num">${i + 1}</span></label>
          <div class="proj-ms-title">${esc(m.title || "")}</div>
        </div>
        <div class="proj-ms-body">
          ${m.build ? `<p class="proj-ms-line"><b>Build</b> ${esc(m.build)}</p>` : ""}
          ${m.learn ? `<p class="proj-ms-line proj-ms-learn"><b>Learn</b> ${esc(m.learn)}</p>` : ""}
          ${m.check ? `<p class="proj-ms-line proj-ms-check-line"><b>Check</b> ${esc(m.check)}</p>` : ""}
          <div class="proj-ms-coach">
            <button class="btn btn-ghost btn-xs" data-coach type="button">I'm stuck on this step</button>
            <div class="proj-coach-box" hidden>
              <textarea class="fld proj-coach-q" rows="2" placeholder="What are you stuck on? I'll explain, not do it for you."></textarea>
              <button class="btn btn-sm" data-coach-ask type="button">Ask</button>
              <div class="proj-coach-a" hidden></div>
            </div>
          </div>
        </div>
      </div>`;
    }).join("");
    root.querySelectorAll(".proj-ms").forEach(el => {
      const i = +el.dataset.i;
      el.querySelector("[data-step]")?.addEventListener("change", (e) => toggleStep(i, e.target.checked, el));
      const coachBtn = el.querySelector("[data-coach]"), box = el.querySelector(".proj-coach-box");
      coachBtn?.addEventListener("click", () => { box.hidden = !box.hidden; if (!box.hidden) el.querySelector(".proj-coach-q")?.focus(); });
      el.querySelector("[data-coach-ask]")?.addEventListener("click", () => askCoach(i, el));
    });
    updateBuildFoot();
  }
  async function toggleStep(i, done, el) {
    done ? BUILD.done.add(i) : BUILD.done.delete(i);
    el.classList.toggle("done", done);
    updateBuildFoot();
    try { await api(`/api/projects/${BUILD.id}/step`, { index: i, done: !!done }); } catch {}
  }
  function updateBuildFoot() {
    // Offer the defend gate once every step is checked off (they can also defend from the card).
    const all = BUILD.milestones.length > 0 && BUILD.done.size >= BUILD.milestones.length;
    $("#projBuildFoot").hidden = !all;
  }
  async function askCoach(i, el) {
    const q = el.querySelector(".proj-coach-q")?.value.trim() || "";
    const ansBox = el.querySelector(".proj-coach-a"), btn = el.querySelector("[data-coach-ask]");
    if (!q) return;
    btn.disabled = true; btn.textContent = "Thinking…";
    ansBox.hidden = false; ansBox.textContent = "…";
    try {
      const d = await api(`/api/projects/${BUILD.id}/coach`, { index: i, question: q });
      ansBox.textContent = d.answer || "No answer came back.";
    } catch (err) { ansBox.textContent = err.message || "Couldn't answer right now."; }
    finally { btn.disabled = false; btn.textContent = "Ask"; }
  }
  $("#projBuildBack")?.addEventListener("click", () => { $("#projBuild").hidden = true; $("#projList").hidden = false; loadProjects(); });
  $("#projBuildDefend")?.addEventListener("click", () => { const p = PROJECTS.find(x => x.id === BUILD.id); $("#projBuild").hidden = true; startDefend(p || { id: BUILD.id, title: BUILD.title }); });

  async function startDefend(p) {
    if (!p) return;
    DEFEND_ID = p.id;
    $("#projTestTitle").textContent = `Defend: ${p.title}`;
    $("#projResult").hidden = true; $("#projSubmit").hidden = true;
    $("#projQuestions").innerHTML = `<div class="empty">Preparing your questions…</div>`;
    $("#projList").hidden = true; $("#projForm").hidden = true; $("#projTest").hidden = false;
    try {
      const t = await api(`/api/projects/${p.id}/test`, {});   // {} body -> POST
      DEFEND_QS = t.questions || [];
      $("#projQuestions").innerHTML = DEFEND_QS.map((q, i) => `
        <div class="proj-q">
          <div class="proj-q-t">${i + 1}. ${esc(q)}</div>
          <textarea class="fld proj-q-a" rows="3" data-i="${i}" placeholder="Answer in your own words, as you would in an interview…"></textarea>
        </div>`).join("");
      $("#projSubmit").hidden = false;
    } catch (err) {
      $("#projQuestions").innerHTML = `<div class="empty">Couldn't generate the test right now.</div>`;
    }
  }
  $("#projTestBack")?.addEventListener("click", () => { $("#projTest").hidden = true; $("#projList").hidden = false; loadProjects(); });
  $("#projSubmit")?.addEventListener("click", async () => {
    const answers = DEFEND_QS.map((q, i) => ({ q, a: $(`.proj-q-a[data-i="${i}"]`)?.value.trim() || "" }));
    const btn = $("#projSubmit"); btn.disabled = true; btn.textContent = "Reviewing…";
    try { renderDefendResult(await api(`/api/projects/${DEFEND_ID}/grade`, { answers })); }
    catch (err) { const b = $("#projResult"); b.hidden = false; b.innerHTML = `<div class="proj-verdict fail">Couldn't review this right now.</div>`; }
    finally { btn.disabled = false; btn.textContent = "Submit for review"; }
  });
  function renderDefendResult(r) {
    const box = $("#projResult"); box.hidden = false;
    const per = (r.per_question || []).map((v, i) => `<li class="pv-${esc(v.verdict || "")}"><b>Q${i + 1}:</b> ${esc(v.verdict || "")}${v.note ? `: ${esc(v.note)}` : ""}</li>`).join("");
    const gaps = (r.gaps || []).length ? `<div class="proj-gaps"><div class="proj-gaps-h">To close before it is resume-ready:</div><ul>${r.gaps.map(g => `<li>${esc(g)}</li>`).join("")}</ul></div>` : "";
    box.innerHTML = `
      <div class="proj-verdict ${r.pass ? "pass" : "fail"}">${r.pass ? "&#10003; Verified. You can defend this, so it is ready for your resume." : "Not yet. Strengthen your understanding and try again."}</div>
      ${r.summary ? `<p class="proj-verdict-sum">${esc(r.summary)}</p>` : ""}
      <ul class="proj-per">${per}</ul>${gaps}
      <div class="proj-result-foot">
        ${r.pass ? `<button class="btn btn-primary btn-sm" id="projResultCv" type="button">Add to Resume</button>` : `<button class="btn btn-primary btn-sm" id="projRetry" type="button">Try again</button>`}
        <button class="btn btn-ghost btn-sm" id="projResultBack" type="button">Back to projects</button>
      </div>`;
    $("#projResultCv")?.addEventListener("click", async () => { await fetch(`/api/projects/${DEFEND_ID}/tocv`, { method: "POST" }); toast("Added to your resume material."); $("#projTest").hidden = true; $("#projList").hidden = false; loadProjects(); });
    $("#projRetry")?.addEventListener("click", () => startDefend(PROJECTS.find(x => x.id === DEFEND_ID)));
    $("#projResultBack")?.addEventListener("click", () => { $("#projTest").hidden = true; $("#projList").hidden = false; loadProjects(); });
  }
  async function addToCv(p) {
    try { const r = await fetch(`/api/projects/${p.id}/tocv`, { method: "POST" }); toast(r.ok ? "Added to your resume material." : "Pass the understanding test first."); }
    catch { toast("Couldn't add to resume."); }
  }
  async function publishToGithub(p, btn) {
    const label = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "Publishing…"; }
    try {
      await api(`/api/projects/${p.id}/publish`, {});   // {} body -> POST
      toast(p.repo_url ? PT("projects.nowPublic", "Your repo is public now.") : "Published to GitHub.");
      loadProjects();   // re-render: the GitHub mark beside the title loses its lock
    } catch (err) {
      toast(err.message || "Couldn't publish to GitHub.");
      if (btn) { btn.disabled = false; btn.textContent = label; }
    }
  }

  // ---------------------------------------------------------------- Interview prep (pre-interview)
  // Two rounds, two entry paths (approved 2026-10-02). Round 1 is a HireVue-style one-way recorded
  // interview, scored locally. Round 2 is one live interview with the Tavus interviewer. The person
  // can run the full mock (Round 1, then Round 2) or either round on its own.
  // Entry (2026-10): the landing shows the choices first. Start on any opens that choice's setup
  // page (role, JD, optional resume), pre-filled from the newest tailored resume or Tracker job.
  const T = (k, f) => I18N.t(k, f);
  let PREPS = [], PREP_APP = null, GUIDED = false, PREP_MODE = null, PREP_FILL = null;
  const blankPrepApp = () => ({ role: "", jd: "", cv: "", cvName: "", recordId: null, company: "" });
  async function loadPreps() {
    $("#prepLive")?.classList.remove("is-report"); $("#prepScreen")?.classList.remove("is-report");
    if ($("#prepScreen")) $("#prepScreen").hidden = true;
    if ($("#prepLive")) $("#prepLive").hidden = true;
    if ($("#prepList")) $("#prepList").hidden = false;
    GUIDED = false; hideGuidedProgress();   // returning to the entry ends any guided flow
    // The setup page re-fills from the latest job on entry, unless the person typed their own
    // role or JD this session: what they typed stays until they change it.
    if (!(PREP_APP && PREP_APP.typed)) PREP_APP = null;
    showPrepChoices();
    paintRound2Tile();         // the Live tile says when a live interviewer key is missing
    paintPastReports();
    try { const d = await api("/api/preps"); PREPS = d.preps || []; } catch { PREPS = []; }
  }
  // The page head: "Interview prep" on the landing (and during a round), the choice's own title
  // with an "Interview prep" back link on its setup page.
  const SETUP_TITLE = {
    full: ["prep.setupFull", "Set up your full mock interview"],
    rec: ["prep.setupRec", "Set up your recorded interview"],
    live: ["prep.setupLive", "Set up your live interview"],
    past: ["prep.past", "Past interviews"],
  };
  function paintPrepHead(mode) {
    const setup = !!mode;
    if ($("#prepBack")) $("#prepBack").hidden = !setup;
    if ($("#prepTitle")) $("#prepTitle").hidden = setup;
    if ($("#prepSub")) $("#prepSub").hidden = setup;
    const st = $("#prepSetupTitle");
    if (st) { st.hidden = !setup; st.textContent = setup ? T(SETUP_TITLE[mode][0], SETUP_TITLE[mode][1]) : ""; }
    if ($("#prepPastLink")) $("#prepPastLink").hidden = setup || !PAST_CARDS.length;
  }
  // Past interviews (Kofi, 2026-10-23): a report is never lost once the person leaves it, and the
  // landing stays three big choices. It carries only ONE line for the newest report (one click to
  // "how did I do?"); the whole list lives on its own page behind the header button, so a long
  // history never crowds the choices. Search and filters appear once the list is long.
  let PAST_CARDS = [], PAST_KIND = "all", PAST_Q = "";
  const pastWhen = (t) => t ? new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "";
  const pastKind = (c) => c.round === 2 ? T("prep.pastLive", "Live") : T("prep.pastRec", "Recorded");
  async function paintPastReports() {
    try { PAST_CARDS = (await api("/api/interview/reports")).cards || []; } catch { PAST_CARDS = []; }
    const n = PAST_CARDS.length, link = $("#prepPastLink"), last = $("#prepLast");
    if ($("#prepPastN")) $("#prepPastN").textContent = String(n);
    if (link) link.hidden = !n || !!PREP_MODE;
    if (last) {
      const c = PAST_CARDS[0];
      last.hidden = !c;
      if (c) {
        last.innerHTML = `<span class="prep-last-k">${esc(T("prep.lastInterview", "Last interview"))}</span>
          <span class="prep-last-role">${esc(c.role || T("prep.pastRole", "Interview"))}</span>
          <span class="prep-last-meta">${esc(pastKind(c))} &middot; ${esc(pastWhen(c.created))}</span>
          <span class="prep-last-score">${c.score}<span>/100</span></span>
          <span class="prep-last-go">${esc(T("prep.openReport", "Open report"))} &rarr;</span>`;
        last.onclick = () => openPastReport(c);
      }
    }
    fitPrepTiles();
  }
  // The two round tiles grow to fill the window, so the landing uses the screen without scrolling.
  function fitPrepTiles() {
    const tiles = [...document.querySelectorAll("#prepTiles .prep-tile")], view = $("#view-prep");
    const ch = $("#prepChoices");
    if (!tiles.length || !view || !ch || ch.hidden || view.hidden || !view.offsetParent) return;
    tiles.forEach(t => { t.style.height = ""; });
    const below = ch.getBoundingClientRect().bottom - tiles[0].getBoundingClientRect().bottom;
    const want = window.innerHeight - tiles[0].getBoundingClientRect().top - below - 96;
    tiles.forEach(t => { t.style.height = Math.max(t.offsetHeight, Math.min(620, want)) + "px"; });
    const spill = view.scrollHeight - view.clientHeight;
    if (spill > 0) tiles.forEach(t => { t.style.height = Math.max(240, t.offsetHeight - spill) + "px"; });
  }
  window.addEventListener("resize", () => fitPrepTiles());
  function showPastPage() {
    PREP_MODE = "past"; paintPrepHead("past");
    if ($("#prepChoices")) $("#prepChoices").hidden = true;
    if ($("#prepSetup")) $("#prepSetup").hidden = true;
    const page = $("#prepPastPage"); if (!page) return;
    page.hidden = false;
    const long = PAST_CARDS.length > 8;
    page.innerHTML = `${long ? `<div class="prep-past-tools">
        <input class="input prep-past-q" id="prepPastQ" type="search" placeholder="${esc(T("prep.pastSearch", "Search by role or company"))}" value="${esc(PAST_Q)}">
        <div class="prep-past-chips">${[["all", T("prep.pastAllKinds", "All")], ["live", T("prep.pastLive", "Live")], ["rec", T("prep.pastRec", "Recorded")]]
          .map(([k, l]) => `<button class="rv-chip${PAST_KIND === k ? " is-on" : ""}" type="button" data-kind="${k}">${esc(l)}</button>`).join("")}</div>
      </div>` : ""}
      <div class="prep-past-list" id="prepPastList"></div>`;
    const paint = () => {
      const q = PAST_Q.trim().toLowerCase();
      const rows = PAST_CARDS.filter(c => (PAST_KIND === "all" || (PAST_KIND === "live") === (c.round === 2))
        && (!q || `${c.role} ${c.company}`.toLowerCase().includes(q)));
      const list = $("#prepPastList");
      list.innerHTML = rows.length ? rows.map((c, k) => `
        <button class="prep-past-row" type="button" data-past="${k}">
          <span class="prep-past-kind">${esc(pastKind(c))}</span>
          <span class="prep-past-role">${esc(c.role || T("prep.pastRole", "Interview"))}${c.company ? ` <span class="prep-past-co">${esc(c.company)}</span>` : ""}</span>
          <span class="prep-past-date">${esc(pastWhen(c.created))}</span>
          <span class="prep-past-score ${c.passed ? "ok" : "no"}">${c.score}<span>/100</span></span>
          <span class="prep-past-go">&rarr;</span>
        </button>`).join("") : `<p class="prep-past-none">${esc(T("prep.pastNone", "No interviews match."))}</p>`;
      list.querySelectorAll("[data-past]").forEach(b => b.addEventListener("click", () => openPastReport(rows[+b.dataset.past])));
    };
    $("#prepPastQ")?.addEventListener("input", (e) => { PAST_Q = e.target.value; paint(); });
    page.querySelectorAll("[data-kind]").forEach(b => b.addEventListener("click", () => {
      PAST_KIND = b.dataset.kind; page.querySelectorAll("[data-kind]").forEach(x => x.classList.toggle("is-on", x === b)); paint(); }));
    paint();
  }
  $("#prepPastLink")?.addEventListener("click", showPastPage);
  async function openPastReport(c) {
    if (!c) return;
    REPORT_FROM_PAST = PREP_MODE === "past"; REPORT_WHEN = c.created || null; REPORT_ROLE = c.role || "";
    try {
      if (c.round === 2) {
        const d = await api(`/api/interviews/${encodeURIComponent(c.id)}`);
        R2_CTX = { prep_id: c.prep_id || (d.interview || {}).prep_id };
        $("#prepList").hidden = true; $("#prepLive").hidden = false;
        $("#liveCount").textContent = T("r2.title", "Round 2, live interview"); $("#liveTier").textContent = "";
        renderR2Report(d.report || {}, c.id);
      } else {
        const d = await api(`/api/screens/${encodeURIComponent(c.id)}`);
        SCREEN = d.screen || d; SCR_PREP = { id: c.prep_id || SCREEN.prep_id, role: c.role || "", company: c.company || "" };
        $("#prepList").hidden = true; $("#prepScreen").hidden = false;
        renderR1Report(SCREEN.result || {});
      }
    } catch (err) { toast(err.message || T("prep.pastFail", "Couldn't open that report.")); }
  }
  function showPrepChoices() {
    PREP_MODE = null; paintPrepHead(null);
    if ($("#prepChoices")) $("#prepChoices").hidden = false;
    if ($("#prepSetup")) $("#prepSetup").hidden = true;
    if ($("#prepPastPage")) $("#prepPastPage").hidden = true;
    fitPrepTiles();
  }
  // The newest job the person touched (tailored resume or Tracker move), to pre-fill the setup.
  async function prepPrefill() {
    try { return await api("/api/preps/prefill"); } catch { return null; }
  }
  // The setup page's side panel: the steps this interview will take, in its own words.
  const SETUP_STEPS = {
    rec: [["prep.stRec1", "Camera and mic check", "prep.stRec1d", "See and hear yourself first."],
          ["prep.stRec2", "One practice question", "prep.stRec2d", "Not scored. Settle in."],
          ["prep.stRec3", "Five questions from this job", "prep.stRec3d", "30 seconds to think, 2 minutes to answer, one retake each."],
          ["prep.stRep", "Your report", "prep.stRepd", "A score, what to fix first, and every answer reviewed."]],
    live: [["prep.stLive1", "Camera and mic check", "prep.stLive1d", "See yourself before you join."],
           ["prep.stLive2", "Join", "prep.stLive2d", "One click. The interviewer greets you, and your time starts then."],
           ["prep.stLive3", "About 20 minutes", "prep.stLive3d", "Questions from this job, with follow-ups like a real interviewer."],
           ["prep.stRep", "Your report", "prep.stRepd", "A score, what to fix first, and every answer reviewed."]],
    full: [["prep.stFull1", "Round 1, recorded", "prep.stFull1d", "Five timed questions on camera."],
           ["prep.stFull2", "Score 70 to go on", "prep.stFull2d", "Passing Round 1 opens the live round."],
           ["prep.stFull3", "Round 2, live", "prep.stFull3d", "About 20 minutes with a live interviewer."],
           ["prep.stRep", "Your report", "prep.stRepd", "A score, what to fix first, and every answer reviewed."]],
  };
  const SETUP_FOOT = {
    rec: ["prep.ftRec", "Recordings stay on this computer. You can delete them any time."],
    live: ["prep.ftLive", "Nothing is used from your pass until the interviewer is on screen."],
    full: ["prep.ftRec", "Recordings stay on this computer. You can delete them any time."],
  };
  function paintSetupSide(mode) {
    const box = $("#prepSetupSide"); if (!box || !SETUP_STEPS[mode]) return;
    box.innerHTML = `<div class="prep-side-h">${esc(T("prep.whatHappens", "What happens"))}</div>
      ${SETUP_STEPS[mode].map(([k, t, kd, d], i) => `<div class="prep-side-step"><i>${i + 1}</i><div><b>${esc(T(k, t))}</b>${esc(T(kd, d))}</div></div>`).join("")}
      <p class="prep-side-foot">${esc(T(SETUP_FOOT[mode][0], SETUP_FOOT[mode][1]))}</p>`;
  }
  async function openPrepSetup(mode) {
    PREP_MODE = mode; paintPrepHead(mode); paintSetupSide(mode);
    if ($("#prepChoices")) $("#prepChoices").hidden = true;
    if ($("#prepPastPage")) $("#prepPastPage").hidden = true;
    if ($("#prepSetup")) $("#prepSetup").hidden = false;
    const msg = $("#prepSetupMsg"); if (msg) msg.hidden = true;
    setStartEnabled(true);
    if (!PREP_APP) {                        // first setup since landing: start from the latest job
      const app = PREP_APP = blankPrepApp();
      renderPrepOpen();
      const f = await prepPrefill();
      // Fill even if they stepped back meanwhile (the next setup shows it), but never overwrite
      // what the person typed while it loaded, nor a newer entry from a later visit.
      if (!f || PREP_APP !== app || app.typed) return;
      PREP_FILL = { role: f.role || "", jd: f.jd || "" };
      Object.assign(app, { role: f.role || "", jd: f.jd || "", company: f.company || "",
        recordId: f.record_id ?? null });
      if (f.cv && !app.cv) { app.cv = f.cv; app.cvName = f.cv_name || ""; }
      if (!PREP_MODE) return;               // back on the choices; the next setup shows it
    }
    renderPrepOpen();
  }
  $("#prepBack")?.addEventListener("click", showPrepChoices);
  $("#prepFullMock")?.addEventListener("click", () => openPrepSetup("full"));
  $("#tileRound1")?.addEventListener("click", () => openPrepSetup("rec"));
  $("#tileRound2")?.addEventListener("click", () => openPrepSetup("live"));

  // The setup fields: role + JD (required) + the resume you used (optional).
  function renderPrepOpen() {
    const box = $("#prepOpen"); if (!box) return;
    if (!PREP_APP) PREP_APP = blankPrepApp();
    const cvLine = PREP_APP.cvName
      ? `<span class="prep-open-cvname is-set" id="prepOpenCvName">${esc(PREP_APP.cvName)}</span>
         <button class="btn btn-ghost btn-sm prep-open-rm" id="prepOpenCvRm" type="button">${esc(T("prep.removeCv", "Remove"))}</button>`
      : `<span class="prep-open-cvname" id="prepOpenCvName">${esc(T("prep.cvHint", "PDF, DOCX, or an image. Optional, it sharpens the questions."))}</span>`;
    box.innerHTML = `<label class="prep-open-lab" for="prepOpenRole">${esc(T("prep.lblRole", "Role"))}</label>
      <input class="fld" id="prepOpenRole" type="text" autocomplete="off" value="${esc(PREP_APP.role || "")}" placeholder="${esc(T("prep.rolePh", "Role you're interviewing for, e.g. Data Analyst"))}">
      <label class="prep-open-lab" for="prepOpenJd">${esc(T("prep.lblJd", "Job description"))}</label>
      <textarea class="fld" id="prepOpenJd" rows="9" placeholder="${esc(T("prep.jdPh", "Paste the job description"))}">${esc(PREP_APP.jd || "")}</textarea>
      <span class="prep-open-lab">${esc(T("prep.lblCv", "Resume"))}</span>
      <div class="prep-open-cv">
        <label class="btn btn-ghost btn-sm prep-open-attach"><input type="file" id="prepOpenCvFile" accept=".pdf,.docx,.doc,.txt,.png,.jpg,.jpeg" hidden> ${esc(T("prep.attachCv", "Attach the resume you used"))}</label>
        ${cvLine}
      </div>`;
    $("#prepOpenRole")?.addEventListener("input", (e) => { PREP_APP.role = e.target.value; PREP_APP.typed = true; });
    $("#prepOpenJd")?.addEventListener("input", (e) => { PREP_APP.jd = e.target.value; PREP_APP.typed = true; });
    $("#prepOpenCvRm")?.addEventListener("click", () => { PREP_APP.cv = ""; PREP_APP.cvName = ""; renderPrepOpen(); });
    $("#prepOpenCvFile")?.addEventListener("change", async (e) => {
      const f = e.target.files && e.target.files[0]; if (!f) return;
      const nameEl = $("#prepOpenCvName"); if (nameEl) nameEl.textContent = T("prep.reading", "Reading") + " " + f.name + "...";
      const fd = new FormData(); fd.append("file", f);
      try {
        const r = await fetch("/api/preps/attach-cv", { method: "POST", body: fd });
        const d = await r.json(); if (!r.ok) throw new Error(d.error || T("prep.cantRead", "Couldn't read that file."));
        PREP_APP.cv = d.text || ""; PREP_APP.cvName = (d.name || f.name) + (d.text ? "" : " " + T("prep.noText", "(no readable text found)"));
        renderPrepOpen();
      } catch (err) { if (nameEl) nameEl.textContent = err.message || T("prep.cantRead", "Couldn't read that file."); }
    });
  }
  function setStartEnabled(on) { const b = $("#prepStart"); if (b) b.disabled = !on; }
  // A quiet note on the Live tile when no live interview is available. The tile still works: it
  // leads to the gate screen with the passes (or the packs, when a pass's interviews are used up).
  async function paintRound2Tile() {
    const st = await round2Status();
    const el = $("#tileRound2Note"); if (!el) return;
    el.hidden = !st || st.available || st.reason === "unreachable";
    el.textContent = st && st.reason === "no_minutes"
      ? T("r2.tileNoMinutes", "Interviews used up") : T("r2.tileNoPass", "Included with a pass");
  }
  // "Start interview": validate, build a prep for the role, then start the chosen interview. The
  // full mock runs Round 1 first; a pass carries the same prep into Round 2.
  async function startFromSetup() {
    const msg = $("#prepSetupMsg");
    const show = (t, err) => { if (msg) { msg.textContent = t; msg.classList.toggle("is-err", !!err); msg.hidden = false; } };
    const role = (PREP_APP?.role || "").trim(), jd = (PREP_APP?.jd || "").trim();
    if (!role) { show(T("prep.needRole", "Type the role you're interviewing for."), true); $("#prepOpenRole")?.focus(); return; }
    if (!jd) { show(T("prep.needJd", "Paste the job description so we can build the interview."), true); $("#prepOpenJd")?.focus(); return; }
    const body = { role, jd, cv: PREP_APP.cv || "" };
    // Still the pre-filled job: link the saved application so its prep (and history) is reused.
    if (PREP_APP.recordId != null && PREP_FILL && role === PREP_FILL.role.trim() && jd === PREP_FILL.jd.trim()) {
      body.record_id = PREP_APP.recordId; body.company = PREP_APP.company || "";
    }
    const mode = PREP_MODE;
    setStartEnabled(false); show(T("prep.preparing", "Preparing your questions..."));
    let prep = null;
    try { prep = (await api("/api/preps", body)).prep; }
    catch (err) { show(err.message || T("prep.cantPrepare", "Couldn't prepare questions right now."), true); }
    setStartEnabled(true);
    if (!prep || PREP_MODE !== mode) return;
    if (msg) msg.hidden = true;
    paintPrepHead(null);                     // the rounds run under the plain "Interview prep" head
    GUIDED = mode === "full";
    if (GUIDED) showGuidedProgress(1);
    if (mode === "live") startRound2(prep.id, null, true); else startScreen(prep);
  }
  $("#prepStart")?.addEventListener("click", startFromSetup);
  // Full mock: Round 1 first; a pass leads to Round 2. A 2-step tracker shows where you are.
  function showGuidedProgress(step) {
    const el = $("#prepProgress"); if (!el) return;
    el.hidden = false;
    el.querySelectorAll(".prep-progress-step").forEach(s => {
      const n = Number(s.dataset.step);
      s.classList.toggle("is-active", n === step);
      s.classList.toggle("is-done", n < step);
    });
  }
  function hideGuidedProgress() { const el = $("#prepProgress"); if (el) el.hidden = true; }

  // ---------------------------------------------------------------- Round 1: one-way recorded interview
  // Mirrors the candidate flow of a HireVue-style one-way video interview, screen by screen:
  // landing, notice, device check, unlimited practice, the real questions, "All done", the report.
  // Screens 3 to 6 sit on a plain light surface like the real thing (.r1-light). Recordings stay on
  // this computer; there is no facial analysis, ever.
  const R1_QUESTIONS = 5;
  let SCREEN = null, SCR_PREP = null, SCR_EXTRA = false;
  let SCR_DRILL = null, SCR_REC_T0 = 0, SCR_SECS = {};   // report card: re-drill questions + measured take lengths
  let SCR_STREAM = null, SCR_REC = null, SCR_CHUNKS = [], SCR_TIMER = null, SCR_PREPT = null, SCR_COUNT = null;
  let SCR_PRACTICE = null, SCR_UPLOADS = {}, SCR_TR = {}, SCR_RETAKES = {}, SCR_PHASE = "idle", SCR_CUR = null;
  const fmtClock = (x) => `${Math.floor(x / 60)}:${String(x % 60).padStart(2, "0")}`;
  function startScreen(prep, opts) {
    SCR_PREP = prep; SCREEN = null; SCR_EXTRA = false; SCR_UPLOADS = {}; SCR_TR = {}; SCR_RETAKES = {}; SCR_PRACTICE = null;
    SCR_DRILL = (opts && Array.isArray(opts.drill) && opts.drill.length) ? opts.drill : null; SCR_SECS = {};
    $("#prepList").hidden = true; $("#prepScreen").hidden = false;
    renderR1Landing();
  }
  // Internal resume-variant labels ("Area Manager, A: experience-first") never belong in
  // the interview UI; the person is interviewing for the role, not one of our CV variants.
  function prepRole(p) { return String((p && p.role) || "").replace(/,\s*[A-Za-z0-9]+:\s.+$/, "").trim(); }
  function scrHead(count, timer) { $("#scrCount").textContent = count || ""; $("#scrTimer").textContent = timer || ""; }
  function r1Title(p) { return esc(prepRole(p)) + (p.company ? " " + esc(T("r1.at", "at")) + " " + esc(p.company) : ""); }
  function r1Kinds(p) {
    const kinds = [...new Set((p.questions || []).map(q => String(q.type || q.kind || "").trim()).filter(Boolean))];
    return kinds.length ? kinds.join(", ") : T("r1.kindsDefault", "Behavioral and role-specific questions");
  }
  // Screen 1: the landing. Company and role, what to expect, one action.
  function renderR1Landing() {
    const p = SCR_PREP;
    scrHead("", "");
    $("#scrStage").innerHTML = `
      <div class="scr-intro"><div class="scr-welcome">
        <div class="r1-eyebrow">${esc(T("r1.eyebrow", "Round 1, recorded interview"))}</div>
        <h2 class="scr-welcome-h">${r1Title(p)}</h2>
        <p class="scr-welcome-sub">${SCR_DRILL
          ? esc(T("rc.drillFacts", "Re-drill: {n} questions from your report card.").replace("{n}", String(SCR_DRILL.length)))
          : esc(T("r1.landingFacts", "5 questions, about 20 minutes.")) + " " + esc((k => k ? k.charAt(0).toUpperCase() + k.slice(1) : "")(r1Kinds(p))) + "."}</p>
        <div class="scr-welcome-acts">
          <button class="btn btn-primary" id="scrBegin" type="button">${esc(T("r1.getStarted", "Get started"))}</button>
          <button class="btn btn-ghost" id="scrBail" type="button">${esc(T("r1.notNow", "Not now"))}</button>
        </div>
        ${SCR_DRILL ? `<ol class="rc-goals-list rc-drill-list">${SCR_DRILL.map(q => `<li>${esc(q.q)}</li>`).join("")}</ol>` : ""}
        <p class="scr-disclaimer">${esc(T("r1.disclaimer", "Practice for a HireVue-style one-way video interview. Not affiliated with HireVue, Inc."))}</p>
      </div></div>`;
    $("#scrBegin")?.addEventListener("click", renderR1Notice);
    $("#scrBail")?.addEventListener("click", quitScreen);
  }
  // Screen 2: the notice. Where recordings go, how to delete them, no facial analysis, and the
  // extra-time option (doubles the clocks), which replaced the old Realistic/Relaxed switch.
  function renderR1Notice() {
    scrHead("", "");
    $("#scrStage").innerHTML = `
      <div class="scr-intro"><div class="scr-welcome">
        <h2 class="scr-ready-h">${esc(T("r1.noticeH", "Before you begin"))}</h2>
        <ul class="r1-notice">
          <li>${esc(T("r1.notice1", "Your recordings stay on this computer. Nothing is uploaded."))}</li>
          <li>${esc(T("r1.notice2", "You can delete them from the report at any time."))}</li>
          <li>${esc(T("r1.notice3", "There is no facial analysis. Only what you say is scored."))}</li>
        </ul>
        <label class="r1-check"><input type="checkbox" id="scrExtra" ${SCR_EXTRA ? "checked" : ""}>
          <span><b>${esc(T("r1.extraTime", "I need extra time"))}</b> <span class="r1-check-d">${esc(T("r1.extraTimeD", "Doubles the prep and answer clocks on every question."))}</span></span></label>
        <div class="scr-welcome-acts">
          <button class="btn btn-primary" id="scrNoticeGo" type="button">${esc(T("r1.continue", "Continue"))}</button>
          <button class="btn btn-ghost" id="scrNoticeBack" type="button">${esc(T("r1.back", "Back"))}</button>
          <span class="prep-step1-msg" id="scrNoticeMsg" hidden></span>
        </div>
      </div></div>`;
    $("#scrExtra")?.addEventListener("change", (e) => { SCR_EXTRA = !!e.target.checked; });
    $("#scrNoticeBack")?.addEventListener("click", renderR1Landing);
    $("#scrNoticeGo")?.addEventListener("click", async () => {
      const btn = $("#scrNoticeGo"), msg = $("#scrNoticeMsg");
      if (SCREEN && SCREEN.config && !!SCREEN.config.extra_time === SCR_EXTRA && SCREEN.i == null) { goDevices(); return; }   // back from the device check: same screen
      btn.disabled = true;
      const body = { prep_id: SCR_PREP.id, extra_time: SCR_EXTRA, questions: R1_QUESTIONS, ...(SCR_DRILL ? { drill: SCR_DRILL } : {}) };
      try { SCREEN = await api("/api/screens", body); }
      catch (err) { btn.disabled = false; if (msg) { msg.textContent = err.message || T("r1.cantStart", "Couldn't start the interview."); msg.hidden = false; } return; }
      SCR_PRACTICE = SCREEN.practice || null;
      goDevices();
    });
  }
  // Screen 3: device check. Camera and mic turn on here; the person confirms they can see and
  // hear themselves. Tips in the real product's wording.
  async function goDevices() {
    if (!navigator.mediaDevices?.getUserMedia) { toast(T("r1.noCamera", "This browser can't reach your camera. Try Chrome or Edge.")); renderR1Notice(); return; }
    if (!SCR_STREAM) {
      try { SCR_STREAM = await navigator.mediaDevices.getUserMedia({ video: true, audio: true }); }
      catch { toast(T("r1.allowCamera", "Camera and mic access is needed. Please allow them and try again.")); renderR1Notice(); return; }
    }
    renderR1Devices();
  }
  function r1Connection() {
    const on = navigator.onLine !== false;
    return `<span class="r1-conn ${on ? "ok" : "warn"}">${esc(on ? T("r1.connOk", "Connection: looks good") : T("r1.connOff", "Connection: you appear to be offline"))}</span>`;
  }
  function renderR1Devices() {
    scrHead("", "");
    $("#scrStage").innerHTML = `
      <div class="r1-light"><div class="scr-ready">
        <div class="scr-ready-cam">
          <div class="scr-video-wrap"><video class="scr-video" id="scrPreview" autoplay muted playsinline></video></div>
          <div class="rec-meter" title="${esc(T("r1.micLevel", "Mic level"))}"><span class="rec-meter-ic" aria-hidden="true">&#127908;</span><span class="rec-meter-track"><i class="rec-meter-fill" id="scrMeter"></i></span></div>
          ${r1Connection()}
        </div>
        <div class="scr-ready-side">
          <h2 class="scr-ready-h">${esc(T("r1.devicesH", "Check your camera and microphone"))}</h2>
          <p class="scr-ready-sub">${esc(T("r1.devicesSub", "Make sure you can see yourself and the bar moves when you talk."))}</p>
          <ul class="r1-tips">
            <li>${esc(T("r1.tip1", "Have the light in front of you, not behind."))}</li>
            <li>${esc(T("r1.tip2", "Find a spot free from distractions and noise."))}</li>
            <li>${esc(T("r1.tip3", "Don't worry about eye contact, just be natural."))}</li>
          </ul>
          <div class="scr-ready-acts">
            <button class="r1-btn r1-btn-primary" id="scrToPractice" type="button">${esc(T("r1.toPractice", "Continue to a practice question"))}</button>
            <button class="r1-btn r1-btn-ghost" id="scrBackNotice" type="button">${esc(T("r1.back", "Back"))}</button>
          </div>
        </div>
      </div></div>`;
    $("#scrPreview").srcObject = SCR_STREAM;
    startMicMeter(SCR_STREAM);
    $("#scrToPractice")?.addEventListener("click", () => renderR1Question({ practice: true }));
    $("#scrBackNotice")?.addEventListener("click", () => { stopMicMeter(); renderR1Notice(); });
  }
  // Screens 4 and 5: a question, in the exact real format. `practice` runs the same screen with the
  // practice prompt: unscored, never saved. The real questions upload after each take.
  function renderR1Question(opts) {
    const practice = !!(opts && opts.practice);
    const qs = SCREEN.questions || [];
    const idx = practice ? -1 : SCREEN.i;
    const q = practice ? (SCR_PRACTICE || { text: "", kind: "practice" }) : qs[idx];
    const n = qs.length, cfg = SCREEN.config || {};
    SCR_CUR = { practice, idx, q };
    SCR_PHASE = "prep";
    const left = practice ? null : retakesLeft(idx);
    const progress = practice ? T("r1.practiceLabel", "Practice question") : T("r1.progress", "Question {n} of {total}").replace("{n}", idx + 1).replace("{total}", n);
    scrHead("", "");
    $("#scrStage").innerHTML = `
      <div class="r1-light r1-q-screen">
        <div class="r1-top">
          <span class="r1-progress">${esc(progress)}</span>
          <span class="r1-prep" id="r1Prep">${esc(T("r1.prepTime", "Prep time"))} <b>${fmtClock(Number(cfg.prep_s || 30))}</b></span>
          <span class="r1-status" id="r1Status"><i class="r1-status-dot"></i>${esc(T("r1.notRecording", "Not Recording"))}</span>
        </div>
        <div class="r1-body">
          <div class="r1-left">
            ${practice ? `<span class="r1-tag">${esc(T("r1.practiceTag", "Practice, not scored or saved"))}</span>` : ""}
            <h2 class="r1-q">${esc(q.text || q.q || "")}</h2>
            <p class="r1-q-help" id="r1Help">${esc(T("r1.qHelp", "Use the prep time to think. Recording starts when it ends, or sooner if you choose."))}</p>
          </div>
          <div class="r1-cam">
            <div class="scr-video-wrap">
              <video class="scr-video" id="scrVideo" autoplay muted playsinline></video>
              <span class="r1-answer-clock" id="r1Answer" hidden>${fmtClock(Number(cfg.answer_s || 120))}</span>
              <div class="r1-count" id="r1Count" hidden></div>
            </div>
            <div class="rec-meter" title="${esc(T("r1.micLevel", "Mic level"))}"><span class="rec-meter-ic" aria-hidden="true">&#127908;</span><span class="rec-meter-track"><i class="rec-meter-fill" id="scrMeter"></i></span></div>
          </div>
        </div>
        <div class="r1-acts" id="r1Acts">
          <button class="r1-btn r1-btn-primary" id="r1Start" type="button">${esc(T("r1.startRecording", "Start Recording"))}</button>
        </div>
        <p class="r1-foot" id="r1Foot">${(!practice && Number(cfg.retakes ?? 1) > 0) ? esc(T("r1.retakesNote", "{n} retake per question").replace("{n}", String(cfg.retakes ?? 1))) : ""}</p>
      </div>`;
    $("#scrVideo").srcObject = SCR_STREAM;
    startMicMeter(SCR_STREAM);
    $("#r1Start")?.addEventListener("click", beginCountdown);
    startPrepClock(Number(cfg.prep_s || 30));
  }
  function retakesLeft(idx) {
    const cfg = SCREEN.config || {};
    if (!(idx in SCR_RETAKES)) SCR_RETAKES[idx] = Number(cfg.retakes ?? 1);
    return SCR_RETAKES[idx];
  }
  function startPrepClock(secs) {
    stopPrepClock(); let s = secs;
    const el = $("#r1Prep");
    SCR_PREPT = setInterval(() => {
      s--;
      if (el) el.innerHTML = `${esc(T("r1.prepTime", "Prep time"))} <b>${fmtClock(Math.max(0, s))}</b>`;
      if (s <= 0) { stopPrepClock(); if (SCR_PHASE === "prep") beginCountdown(); }
    }, 1000);
  }
  function stopPrepClock() { if (SCR_PREPT) { clearInterval(SCR_PREPT); SCR_PREPT = null; } }
  // The 3, 2, 1 overlay before the red dot comes on.
  function beginCountdown() {
    if (SCR_PHASE !== "prep") return;
    SCR_PHASE = "countdown"; stopPrepClock();
    const prep = $("#r1Prep"); if (prep) prep.hidden = true;
    const start = $("#r1Start"); if (start) start.disabled = true;
    const help = $("#r1Help"); if (help) help.textContent = T("r1.getReady", "Get ready...");
    const ov = $("#r1Count"); let n = 3;
    if (ov) { ov.hidden = false; ov.textContent = String(n); }
    if (SCR_COUNT) clearInterval(SCR_COUNT);
    SCR_COUNT = setInterval(() => {
      n--;
      if (n <= 0) { clearInterval(SCR_COUNT); SCR_COUNT = null; if (ov) ov.hidden = true; beginRecording(); return; }
      if (ov) ov.textContent = String(n);
    }, 1000);
  }
  function scrMime() {
    const c = ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm", "video/mp4"];
    return (window.MediaRecorder && c.find(t => MediaRecorder.isTypeSupported(t))) || "";
  }
  function beginRecording() {
    SCR_CHUNKS = [];
    const mt = scrMime();
    try { SCR_REC = mt ? new MediaRecorder(SCR_STREAM, { mimeType: mt }) : new MediaRecorder(SCR_STREAM); }
    catch { toast(T("r1.cantRecord", "This browser can't record here.")); SCR_PHASE = "prep"; return; }
    SCR_REC.ondataavailable = (e) => { if (e.data && e.data.size) SCR_CHUNKS.push(e.data); };
    SCR_REC.onstop = onR1Stopped;
    SCR_REC.start();
    SCR_REC_T0 = Date.now();                                 // report card: the take's real length
    SCR_PHASE = "recording";
    const st = $("#r1Status"); if (st) { st.classList.add("is-rec"); st.innerHTML = `<i class="r1-status-dot"></i>${esc(T("r1.recording", "Recording"))}`; }
    const help = $("#r1Help"); if (help) help.textContent = T("r1.answerHelp", "Answer in your own words. Stop when you're done, or the clock will stop for you.");
    $("#r1Acts").innerHTML = `<button class="r1-btn r1-btn-stop" id="r1Stop" type="button">${esc(T("r1.stopRecording", "I'm done, stop recording"))}</button>`;
    $("#r1Stop")?.addEventListener("click", stopRecording);
    startAnswerClock(Number((SCREEN.config || {}).answer_s || 120));
  }
  function stopRecording() { if (SCR_REC && SCR_REC.state === "recording") { try { SCR_REC.stop(); } catch (e) {} } }
  // The answer clock counts down; at zero the take is submitted for you, like the real thing.
  function startAnswerClock(cap) {
    stopAnswerClock(); let s = cap;
    const el = $("#r1Answer"); if (el) { el.hidden = false; el.textContent = fmtClock(cap); el.classList.remove("warn"); }
    SCR_TIMER = setInterval(() => {
      s--;
      if (el) { el.textContent = fmtClock(Math.max(0, s)); el.classList.toggle("warn", s <= 15); }
      if (s <= 0) { stopAnswerClock(); stopRecording(); }
    }, 1000);
  }
  function stopAnswerClock() { if (SCR_TIMER) { clearInterval(SCR_TIMER); SCR_TIMER = null; } }
  async function onR1Stopped() {
    stopAnswerClock();
    SCR_PHASE = "review";
    const st = $("#r1Status"); if (st) { st.classList.remove("is-rec"); st.innerHTML = `<i class="r1-status-dot"></i>${esc(T("r1.notRecording", "Not Recording"))}`; }
    const clock = $("#r1Answer"); if (clock) clock.hidden = true;
    const cur = SCR_CUR; if (!cur) return;
    const blob = new Blob(SCR_CHUNKS, { type: SCR_CHUNKS[0]?.type || "video/webm" });
    if (cur.practice) {
      // Practice: nothing is saved. Offer another, or start the interview.
      const help = $("#r1Help"); if (help) help.textContent = T("r1.practiceDone", "That was practice. Nothing was saved.");
      $("#r1Acts").innerHTML = `
        <button class="r1-btn r1-btn-ghost" id="r1Another" type="button">${esc(T("r1.anotherPractice", "Do another practice question"))}</button>
        <button class="r1-btn r1-btn-primary" id="r1StartReal" type="button">${esc(T("r1.startInterview", "Start the interview"))}</button>`;
      $("#r1Another")?.addEventListener("click", async () => {
        const b = $("#r1Another"); if (b) b.disabled = true;
        try { const d = await api(`/api/screens/${SCREEN.id}/practice`); if (d && d.text) SCR_PRACTICE = d; } catch (e) { /* reuse the current prompt */ }
        renderR1Question({ practice: true });
      });
      $("#r1StartReal")?.addEventListener("click", () => { SCREEN.i = 0; renderR1Question({}); });
      return;
    }
    // A real question: upload this take now (the server keeps the latest), transcribe behind the
    // scenes, and offer the one retake or the next question. There is no back button.
    const idx = cur.idx, n = (SCREEN.questions || []).length, last = idx >= n - 1;
    SCR_SECS[idx] = SCR_REC_T0 ? Math.round((Date.now() - SCR_REC_T0) / 100) / 10 : null; SCR_REC_T0 = 0;
    uploadTake(idx, blob, SCR_SECS[idx]);
    const left = retakesLeft(idx);
    const help = $("#r1Help"); if (help) help.textContent = T("r1.saved", "Saved on this computer.");
    $("#r1Acts").innerHTML = `
      ${left > 0 ? `<button class="r1-btn r1-btn-ghost" id="r1Retake" type="button">${esc(T("r1.retake", "Retake ({n} left)").replace("{n}", String(left)))}</button>` : ""}
      <button class="r1-btn r1-btn-primary" id="r1Next" type="button">${esc(last ? T("r1.finish", "Finish interview") : T("r1.next", "Next question"))}</button>`;
    $("#r1Retake")?.addEventListener("click", () => { SCR_RETAKES[idx] = Math.max(0, left - 1); renderR1Question({}); });
    $("#r1Next")?.addEventListener("click", () => { if (last) renderR1Done(); else { SCREEN.i = idx + 1; renderR1Question({}); } });
  }
  function uploadTake(idx, blob, secs) {
    const fd = new FormData(); fd.append("file", blob, `answer_${idx}.webm`);
    if (secs) fd.append("seconds", String(secs));             // report card: words per minute
    const up = fetch(`/api/screens/${SCREEN.id}/answers/${idx}`, { method: "POST", body: fd }).then(async (r) => {
      if (r.status === 409) { SCR_RETAKES[idx] = 0; throw new Error(T("r1.noRetakes", "No retakes left on this question; your earlier take stands.")); }
      if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error || T("r1.uploadFailed", "Couldn't save the recording.")); }
      return r.json().catch(() => ({}));
    });
    SCR_UPLOADS[idx] = up;
    SCR_TR[idx] = up.then(() => fetch(`/api/screens/${SCREEN.id}/answers/${idx}/transcribe`, { method: "POST" }).then(r => r.json()).catch(() => ({ available: false })));
    up.catch((e) => toast(e.message));
  }
  // Screen 6: "All done". Wait for the uploads and transcripts, then score. If this computer can't
  // transcribe, ask for the words of each answer here (the question screens stay pure).
  async function renderR1Done() {
    stopPrepClock(); stopAnswerClock(); stopMicMeter(); stopScrStream();
    scrHead("", "");
    $("#scrStage").innerHTML = `
      <div class="r1-light r1-done">
        <h2 class="scr-ready-h">${esc(T("r1.allDone", "All done"))}</h2>
        <p class="scr-ready-sub" id="r1DoneSub">${esc(T("r1.doneSub", "Saving and scoring your answers on this computer. This takes a minute."))}</p>
        <div class="r1-spinner" id="r1Spin" aria-hidden="true"></div>
        <div id="r1DoneBody"></div>
      </div>`;
    const n = (SCREEN.questions || []).length;
    const results = await Promise.all(Array.from({ length: n }, (_, i) => (SCR_TR[i] || Promise.resolve({ available: false })).then(x => x, () => ({ available: false }))));
    const need = results.map((tr, i) => ({ i, tr })).filter(({ tr }) => !tr || tr.available === false || !(tr.transcript || tr.text));
    if (!need.length) return scoreR1();
    const spin = $("#r1Spin"); if (spin) spin.hidden = true;
    const sub = $("#r1DoneSub"); if (sub) sub.textContent = T("r1.typeSub", "This computer couldn't transcribe some answers. Type what you said so they can be scored.");
    $("#r1DoneBody").innerHTML = need.map(({ i }) => `
      <div class="r1-type">
        <div class="r1-type-q">${i + 1}. ${esc((SCREEN.questions[i] || {}).text || "")}</div>
        <textarea class="fld r1-ta" data-i="${i}" rows="3" placeholder="${esc(T("r1.typePh", "Your answer, in words"))}"></textarea>
      </div>`).join("") + `<div class="r1-acts"><button class="r1-btn r1-btn-primary" id="r1Score" type="button">${esc(T("r1.scoreBtn", "Score my interview"))}</button></div>`;
    $("#r1Score")?.addEventListener("click", async () => {
      const tas = [...document.querySelectorAll(".r1-ta")];
      if (tas.some(t => !t.value.trim())) { toast(T("r1.typeAll", "Add the words for every answer first.")); return; }
      $("#r1Score").disabled = true;
      try { for (const t of tas) await api(`/api/screens/${SCREEN.id}/answers/${t.dataset.i}/transcript`, { transcript: t.value.trim(), seconds: SCR_SECS[t.dataset.i] || null }); }
      catch (err) { toast(err.message || T("r1.cantSave", "Couldn't save.")); $("#r1Score").disabled = false; return; }
      scoreR1();
    });
  }
  async function scoreR1() {
    const sub = $("#r1DoneSub"); if (sub) sub.textContent = T("r1.scoring", "Scoring...");
    const spin = $("#r1Spin"); if (spin) spin.hidden = false;
    const body = $("#r1DoneBody"); if (body) body.innerHTML = "";
    try { const d = await api(`/api/screens/${SCREEN.id}/score`, {}); const r = (d && d.result) || d; SCREEN.result = r; renderR1Report(r); }
    catch (err) { if (spin) spin.hidden = true; if (sub) sub.textContent = err.message || T("r1.cantScore", "Couldn't score this interview."); }
  }
  // Screen 7: the report. Back on the app theme.
  function renderR1Report(r) {
    r = r || {};
    SCR_PHASE = "report"; scrLeaveAsk(false);
    const passed = !!r.passed;
    const items = (r.answers || []).map((a, k) => {
      const i = a.i ?? k, paf = a.per_answer_feedback || {};
      return { i, q: a.text || a.q || a.question || ((SCREEN.questions || [])[i] || {}).text || "",
        said: a.transcript ? highlightDisfluencies(a.transcript) : "", competency: a.competency || "",
        score: a.score ?? paf.score, feedback: a.feedback || paf.feedback || "",
        extra: deliveryReadout(a.delivery_metrics || a.delivery), coach: true };
    });
    $("#prepScreen").classList.add("is-report");
    $("#scrStage").innerHTML = reportA2Html(r, {
      kind: T("rep.kindRec", "Recorded interview"), role: (SCR_PREP && prepRole(SCR_PREP)) || "", company: (SCR_PREP && SCR_PREP.company) || "",
      when: REPORT_WHEN || (SCREEN && SCREEN.created), items, round: 1,
      actions: `<button class="btn btn-ghost btn-sm" id="scrReport" type="button">${esc(T("r1.docx", "Download report (.docx)"))}</button>
        <button class="btn btn-danger-ghost btn-sm" id="scrDelete" type="button">${esc(T("r1.deleteRec", "Delete recordings"))}</button>`,
      gate: true });
    REPORT_WHEN = null;
    wireReportA2(r, { prepId: SCR_PREP && SCR_PREP.id, currentId: SCREEN && SCREEN.id,
      leave: () => { stopPrepClock(); stopAnswerClock(); stopScrStream(); SCR_PHASE = "idle"; $("#prepScreen").classList.remove("is-report"); },
      back: quitScreen, again: async () => { const p = await prepById(SCR_PREP && SCR_PREP.id); $("#prepScreen").classList.remove("is-report"); startScreen(p); },
      coach: (i, btn) => coachScreenAnswer(SCREEN.id, i, btn) });
    $("#scrReport")?.addEventListener("click", () => { window.location = `/api/screens/${SCREEN.id}/report.docx`; });
    $("#scrDelete")?.addEventListener("click", async () => {
      if (!confirm(T("r1.deleteConfirm", "Delete this interview and its recordings? This can't be undone."))) return;
      try { await fetch(`/api/screens/${SCREEN.id}/delete`, { method: "POST" }); } catch {}
      toast(T("r1.deleted", "Deleted.")); quitScreen();
    });
    if (GUIDED) renderR1Next(passed);
  }
  // Full mock only: after Round 1, where next. A pass with a live interviewer available goes to
  // Round 2; a pass without one meets the gate; a miss offers another go.
  async function renderR1Next(passed) {
    const box = $("#r1Gate"); if (!box) return;
    if (!passed) {
      box.innerHTML = `<div class="scr-unlock"><b>${esc(T("r1.tryAgainH", "Round 2 opens at 70."))}</b>
        <button class="btn btn-primary btn-sm" id="r1Again" type="button">${esc(T("r1.tryAgain", "Try Round 1 again"))}</button></div>`;
      $("#r1Again")?.addEventListener("click", () => startScreen(SCR_PREP));
      return;
    }
    const st = await round2Status();
    if (st && st.available) {
      box.innerHTML = `<div class="scr-unlock"><b>${esc(T("r1.unlocked", "Round 2 unlocked."))}</b> ${esc(T("r1.unlockedD", "A live interview with the Tavus interviewer, 20 minutes."))}
        <button class="btn btn-primary btn-sm" id="scrToLive" type="button">${esc(T("r2.start", "Start Round 2"))}</button></div>`;
      $("#scrToLive")?.addEventListener("click", () => startRound2(SCR_PREP.id, SCREEN.id, false));
    } else {
      box.innerHTML = r2GateHtml(st, "inline");
      wireR2Gate(() => quitScreen());
    }
  }
  // Delivery readout: pace, length, and fillers, from the local transcript metrics.
  function deliveryReadout(d) {
    if (!d) return "";
    const wpm = d.wpm || 0, fillers = d.fillers || 0, secs = d.speak_sec || 0, words = d.words || 0;
    const paceCls = (wpm && (wpm < 110 || wpm > 170)) ? "mid" : "high";
    const paceNote = !wpm ? T("r1.pace", "pace") : wpm < 110 ? T("r1.paceSlow", "a little slow") : wpm > 170 ? T("r1.paceFast", "a little fast") : T("r1.paceGood", "good pace");
    const lenCls = (words < 40 || secs > 120) ? "mid" : "high";
    const fillCls = fillers >= 5 ? "low" : fillers >= 3 ? "mid" : "high";
    const hedges = d.hedges || 0;
    const hedgeChip = hedges >= 2 ? `<span class="scr-deliv-chip low" title="${esc(T("r1.hedgeTip", "Interviewers hear hedging as uncertainty. State it directly."))}">${hedges} ${esc(T("r1.hedges", "hedging phrases"))}</span>` : "";
    const ownChip = d.ownership === "we" ? `<span class="scr-deliv-chip mid" title="${esc(T("r1.weTip", "You said 'we' a lot. Name what YOU did."))}">${esc(T("r1.weChip", "mostly 'we', own your part"))}</span>` : "";
    return `<div class="scr-deliv">
      <span class="scr-deliv-chip ${paceCls}">${wpm} wpm, ${esc(paceNote)}</span>
      <span class="scr-deliv-chip ${lenCls}">${words} ${esc(T("r1.words", "words"))}, ~${secs}s</span>
      <span class="scr-deliv-chip ${fillCls}">${fillers} ${esc(fillers === 1 ? T("r1.filler", "filler") : T("r1.fillers", "fillers"))}</span>
      ${hedgeChip}${ownChip}
    </div>`;
  }
  // Highlight the fillers and hedges inside a transcript, so the person SEES exactly where they
  // wobbled. Mirrors the on-device lists in interview/transcribe.py. One pass, longest phrase first.
  const FILLER_WORDS = ["um", "uh", "er", "like", "you know", "basically", "actually", "literally",
    "kind of", "sort of", "i mean", "just", "so yeah"];
  const HEDGE_WORDS = ["i think", "i guess", "i suppose", "maybe", "probably", "hopefully",
    "i feel like", "perhaps", "i'm not sure"];
  function highlightDisfluencies(text) {
    const hedges = new Set(HEDGE_WORDS.map(w => w.toLowerCase()));
    const all = [...new Set([...HEDGE_WORDS, ...FILLER_WORDS])].sort((a, b) => b.length - a.length);
    const pattern = all.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/ /g, "\\s+")).join("|");
    const re = new RegExp("\\b(" + pattern + ")\\b", "gi");
    return esc(text || "").replace(re, (m) => {
      const cls = hedges.has(m.toLowerCase().replace(/\s+/g, " ")) ? "scr-mark-hedge" : "scr-mark-filler";
      return `<mark class="${cls}">${m}</mark>`;
    });
  }
  async function coachScreenAnswer(sid, idx, btn) {
    const box = $(`#scrCoach${idx}`); if (!box) return;
    btn.disabled = true; box.hidden = false;
    box.innerHTML = `<div class="empty">${esc(T("r1.coachWorking", "Building a stronger version from your profile..."))}</div>`;
    let c;
    try { const d = await api(`/api/screens/${sid}/answers/${idx}/coach`, {}); c = d.coaching || d || {}; }
    catch (err) { box.innerHTML = `<div class="empty">${esc(err.message || T("r1.coachFailed", "Couldn't strengthen this answer right now."))}</div>`; btn.disabled = false; return; }
    const star = c.star || {};
    const starRows = [["Situation", star.situation], ["Task", star.task], ["Action", star.action], ["Result", star.result]]
      .filter(([, v]) => v && String(v).trim())
      .map(([k, v]) => `<div class="scr-star-row"><span class="scr-star-k">${k}</span>${esc(v)}</div>`).join("");
    box.innerHTML = `
      ${c.assessment ? `<p class="scr-coach-assess">${esc(c.assessment)}</p>` : ""}
      ${c.tighter ? `<div class="scr-coach-block"><div class="scr-coach-h">${esc(T("r1.strongerH", "A stronger version"))}</div><p>${esc(c.tighter)}</p></div>` : ""}
      ${starRows ? `<div class="scr-coach-block"><div class="scr-coach-h">${esc(T("r1.asStar", "As STAR"))}</div>${starRows}</div>` : ""}
      ${(c.improve || []).length ? `<div class="scr-coach-block"><div class="scr-coach-h">${esc(T("r1.fixes", "Fixes"))}</div><ul>${c.improve.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
      ${c.honesty ? `<p class="prep-honesty">${esc(T("r1.keepAccurate", "Keep it accurate:"))} ${esc(c.honesty)}</p>` : ""}
      <p class="scr-note">${esc(T("r1.coachNote", "Built from your real profile. Use it as a model, then say it in your own words."))}</p>`;
    btn.disabled = false; btn.textContent = T("r1.refreshStronger", "Refresh stronger version");
  }
  // Live mic-level meter (cosmetic, reassuring "we can hear you"): Web Audio RMS on the stream.
  let SCR_AUDIO = null, SCR_METER_RAF = null;
  function startMicMeter(stream) {
    stopMicMeter();
    try {
      const AC = window.AudioContext || window.webkitAudioContext; if (!AC || !stream) return;
      SCR_AUDIO = new AC();
      const analyser = SCR_AUDIO.createAnalyser(); analyser.fftSize = 512;
      SCR_AUDIO.createMediaStreamSource(stream).connect(analyser);
      const data = new Uint8Array(analyser.frequencyBinCount);
      const tick = () => {
        analyser.getByteTimeDomainData(data);
        let sum = 0; for (let i = 0; i < data.length; i++) { const v = (data[i] - 128) / 128; sum += v * v; }
        const el = $("#scrMeter"); if (el) el.style.width = Math.min(100, Math.round(Math.sqrt(sum / data.length) * 260)) + "%";
        SCR_METER_RAF = requestAnimationFrame(tick);
      };
      tick();
    } catch (e) { /* the meter is cosmetic; never let it break recording */ }
  }
  function stopMicMeter() {
    if (SCR_METER_RAF) { cancelAnimationFrame(SCR_METER_RAF); SCR_METER_RAF = null; }
    if (SCR_AUDIO) { try { SCR_AUDIO.close(); } catch (e) {} SCR_AUDIO = null; }
  }
  function stopScrStream() { stopMicMeter(); if (SCR_STREAM) { SCR_STREAM.getTracks().forEach(t => t.stop()); SCR_STREAM = null; } }
  function quitScreen() {
    scrLeaveAsk(false);
    stopPrepClock(); stopAnswerClock(); if (SCR_COUNT) { clearInterval(SCR_COUNT); SCR_COUNT = null; }
    if (SCR_REC && SCR_REC.state === "recording") { SCR_REC.onstop = null; try { SCR_REC.stop(); } catch {} }
    SCR_PHASE = "idle"; stopScrStream();
    $("#prepScreen").hidden = true; $("#prepList").hidden = false;
    loadPreps();
  }
  // Quit right after recording an answer used to leave at once. Once any answer is recorded (or
  // one is recording) it asks first; before that, and on the report, Quit just leaves.
  function scrHasAnswers() {
    return SCR_PHASE !== "report" && (SCR_PHASE === "recording" || Object.keys(SCR_UPLOADS).length > 0);
  }
  function scrLeaveAsk(on) {
    const bar = $("#scrLeave"); if (!bar) return;
    bar.hidden = !on;
    if (on) $("#scrLeaveStay")?.focus();
  }
  $("#scrQuit")?.addEventListener("click", () => { if (scrHasAnswers()) scrLeaveAsk(true); else quitScreen(); });
  $("#scrLeaveGo")?.addEventListener("click", () => { scrLeaveAsk(false); quitScreen(); });
  $("#scrLeaveStay")?.addEventListener("click", () => { scrLeaveAsk(false); $("#scrQuit")?.focus(); });
  $("#scrLeave")?.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); scrLeaveAsk(false); } });

  // ---------------------------------------------------------------- Round 2: live interview (Tavus)
  // One 20-minute conversation with the Tavus interviewer, in the app's own video pane. Paid only
  // (Kofi, 2026-10-14): it runs on a pass or a pack, on the company key held by the broker. Nobody
  // adds their own Tavus key.
  let R2 = null, LIVE_STREAM = null, R2_CTX = null, CVI_CLOCK = null, CVI_PREV_W = "", CVI_UNBILLED = 0;
  async function round2Status() {
    try { return await api("/api/interview/round2/status"); } catch { return null; }
  }
  async function startRound2(prepId, screenId, skip) {
    R2_CTX = { prep_id: prepId, screen_id: screenId || "", skip_screen: !!skip };
    if ($("#prepScreen")) $("#prepScreen").hidden = true;
    if ($("#prepList")) $("#prepList").hidden = true;
    $("#prepLive").hidden = false;
    if (GUIDED) showGuidedProgress(2);
    $("#liveCount").textContent = T("r2.title", "Round 2, live interview"); $("#liveTier").textContent = "";
    const st = await round2Status();
    if (!st || !st.available) { renderR2Gate(st); return; }
    renderR2GreenRoom(st);
  }
  // The gate: live interviews are paid. No pass: the passes on sale (names, prices and interview
  // counts all from the broker, never hard-coded here). A pass whose interviews are used up: the
  // extra-interview packs. The broker unreachable: say so and offer to check again.
  function r2PackLabel(p) {
    const n = Number(p.interviews) || 1;
    const label = n === 1 ? T("r2.buyOne", "Buy 1 interview") : T("r2.buyMany", "Buy {n} interviews").replace("{n}", String(n));
    return p.price_label ? `${label}, ${p.price_label}` : label;
  }
  function r2PassLabel(p) {
    const name = PASS_NAMES[p.id] ? PASS_NAMES[p.id]() : String(p.id || "");
    const n = Number(p.interviews) || 0;
    const ivs = n === 1 ? T("r2.passOneIv", "1 live interview") : T("r2.passNIv", "{n} live interviews").replace("{n}", String(n));
    return [name, p.price_label, n ? ivs : ""].filter(Boolean).join(", ");
  }
  function r2GateHtml(st, mode) {
    const wrap = mode === "inline" ? "scr-unlock r2-gate-inline" : "scr-intro r2-gate";
    const finish = `<button class="btn btn-ghost btn-sm" id="r2Finish" type="button">${esc(T("r2.finishHere", "Finish here"))}</button>`;
    if (st && st.reason === "no_minutes") {
      const packs = (st.can_buy && Array.isArray(st.packs)) ? st.packs : [];
      return `<div class="${wrap}">
      <div class="r2-gate-main">
        <h3 class="r2-gate-h">${esc(T("r2.usedUpH", "You've used your pass's live interviews."))}</h3>
        <p class="r2-gate-d">${esc(packs.length ? T("r2.usedUpDPaid", "Buy more, they never expire.") : T("r2.usedUpNoBuyPaid", "Add another pass for more live interviews."))}</p>
        <div class="scr-welcome-acts">
          ${packs.map((p, i) => `<button class="btn ${i === 0 ? "btn-primary" : "btn-ghost"} btn-sm r2-buy" data-pack="${esc(p.id)}" type="button">${esc(r2PackLabel(p))}</button>`).join("")}
          ${packs.length ? "" : `<button class="btn btn-primary btn-sm" id="r2SeePasses" type="button">${esc(T("r2.seePasses", "See passes"))}</button>`}
          ${finish}
        </div>
        <p class="r2-gate-d" id="r2BuyMsg" hidden></p>
      </div></div>`;
    }
    if (!st || st.reason === "unreachable") {
      return `<div class="${wrap}">
      <div class="r2-gate-main">
        <h3 class="r2-gate-h">${esc(T("r2.offH", "The live interviewer isn't reachable right now"))}</h3>
        <p class="r2-gate-d">${esc(T("r2.offD", "Live interviews run on our service. Check your connection, then try again."))}</p>
        <div class="scr-welcome-acts">
          <button class="btn btn-primary btn-sm" id="r2Retry" type="button">${esc(T("r2.checkAgain", "Check again"))}</button>
          ${finish}
        </div>
      </div></div>`;
    }
    // No pass: the passes that include live interviews, filled in from the broker's offers.
    return `<div class="${wrap}">
      <div class="r2-gate-main">
        <h3 class="r2-gate-h">${esc(T("r2.passH", "Live interviews come with a pass"))}</h3>
        <p class="r2-gate-d">${esc(T("r2.passD", "Twenty minutes with a live interviewer who asks, listens and follows up, then a scored report. Paid once, no auto-renew."))}</p>
        <div class="scr-welcome-acts" id="r2PassActs">
          <button class="btn btn-ghost btn-sm" id="r2SeePasses" type="button">${esc(T("r2.seePasses", "See passes"))}</button>
          ${finish}
        </div>
        <p class="r2-gate-d" id="r2BuyMsg" hidden></p>
      </div></div>`;
  }
  // The passes on sale, as buttons ahead of "See passes". Nothing on sale (billing off, or the
  // broker briefly unreachable): "See passes" alone, which opens the same panel as Upgrade.
  async function fillR2Passes() {
    const acts = $("#r2PassActs"); if (!acts) return;
    let o = null;
    try { o = await api("/api/billing/offers"); } catch { return; }
    const passes = ((o && o.passes) || []).filter(p => p && p.id);
    if (!passes.length || !document.body.contains(acts)) return;
    acts.insertAdjacentHTML("afterbegin", passes.map((p, i) =>
      `<button class="btn ${i === 0 ? "btn-primary" : "btn-ghost"} btn-sm r2-pass" data-pass="${esc(p.id)}" type="button">${esc(r2PassLabel(p))}</button>`).join(""));
    acts.querySelectorAll(".r2-pass").forEach(b => b.addEventListener("click", () => buyR2Pass(b.dataset.pass, b)));
  }
  async function buyR2Pass(passId, btn) {
    if (!ACCT_EMAIL) { openSignin({ kind: "pass", id: passId }); return; }   // sign in first, then Stripe
    if (btn) btn.disabled = true;
    try {
      if (await doCheckout(passId)) r2AfterCheckout();
    } finally { if (btn) btn.disabled = false; }
  }
  // After a checkout opens: say where to finish, and a "Check again" that re-reads the status
  // without a restart.
  function r2AfterCheckout() {
    const msg = $("#r2BuyMsg");
    const say = (t) => { if (msg) { msg.textContent = t; msg.hidden = false; } };
    say(T("r2.buyOpened", "Finish paying in the checkout, then check again."));
    if (!$("#r2Recheck") && msg) {
      msg.insertAdjacentHTML("afterend", `<div class="scr-welcome-acts"><button class="btn btn-ghost btn-sm" id="r2Recheck" type="button">${esc(T("r2.checkAgain", "Check again"))}</button></div>`);
      $("#r2Recheck")?.addEventListener("click", async () => {
        if (window.tailorShell) window.tailorShell.closeBrowser();
        const st = await round2Status();
        paintRound2Tile();
        if (st && st.available) renderR2GreenRoom(st); else say(T("r2.notYet", "Not there yet. It can take a moment after paying."));
      });
    }
  }
  function wireR2Gate(onFinish) {
    $("#r2Finish")?.addEventListener("click", onFinish);
    $("#r2SeePasses")?.addEventListener("click", () => openUpgrade());
    $("#r2Retry")?.addEventListener("click", async () => {
      const st = await round2Status();
      paintRound2Tile();
      if (st && st.available) renderR2GreenRoom(st);
      else { const g = document.querySelector(".r2-gate, .r2-gate-inline"); if (g) { g.outerHTML = r2GateHtml(st, g.classList.contains("r2-gate-inline") ? "inline" : "page"); wireR2Gate(onFinish); } }
    });
    document.querySelectorAll(".r2-buy").forEach(b => b.addEventListener("click", () => buyInterviews(b.dataset.pack, b)));
    fillR2Passes();
  }
  // Buy a prepaid interview pack: the broker mints a Stripe Checkout, we open it, and the
  // credits land once Stripe confirms. "Check again" re-reads the status without a restart.
  async function buyInterviews(pack, btn) {
    const msg = $("#r2BuyMsg");
    const say = (t) => { if (msg) { msg.textContent = t; msg.hidden = false; } };
    if (!ACCT_EMAIL) { openSignin({ kind: "pack", id: pack }); return; }   // sign in first, then Stripe
    if (btn) btn.disabled = true;
    try {
      const d = await api("/api/interview/round2/buy", { pack });
      if (window.tailorShell) window.tailorShell.openBrowser(d.url);
      else fetch("/api/open?u=" + encodeURIComponent(d.url)).catch(() => window.open(d.url, "_blank"));
      r2AfterCheckout();
    } catch (err) {
      if (err.reason === "sign_in_required") { if (btn) btn.disabled = false; openSignin({ kind: "pack", id: pack }); return; }
      if (err.data && err.data.offer === "passes") openUpgrade();   // no active pass: show the passes
      say(err.message || T("r2.cantBuy", "Couldn't start checkout."));
    }
    if (btn) btn.disabled = false;
  }
  function renderR2Gate(st) {
    $("#liveStage").innerHTML = r2GateHtml(st, "page");
    wireR2Gate(quitLive);
  }
  // Green room: preview yourself, then Join. The interviewer runs its own camera and mic in the pane.
  function renderR2GreenRoom(st) {
    const mins = st && st.minutes_left != null ? Number(st.minutes_left) : null;
    $("#liveStage").innerHTML = `
      <div class="zoom-greenroom">
        <div class="zoom-gr-preview">
          <video class="zoom-gr-video" id="grVideo" autoplay muted playsinline></video>
          <div class="zoom-gr-off" id="grOff">${esc(T("r2.previewOff", "Your camera preview will appear here"))}</div>
        </div>
        <div class="zoom-gr-side">
          <div class="zoom-gr-eyebrow">${esc(T("r2.aboutToJoin", "You are about to join"))}</div>
          <h2 class="zoom-gr-h">${esc(T("r2.title", "Round 2, live interview"))}</h2>
          <p class="zoom-gr-sub">${esc(T("r2.greenSub", "20 minutes with a live interviewer. They ask, you answer, and they follow up like a real interview."))}</p>
          ${R2_CTX && R2_CTX.skip_screen ? `<p class="scr-warm" style="display:inline-block">${esc(T("r2.skippedR1", "Straight to Round 2, no Round 1 first."))}</p>` : ""}
          <p class="zoom-gr-note">${esc(T("r2.note", "Your camera and mic stay on this computer."))}${mins != null ? " " + esc(r2InterviewsLeft(mins)) : ""}</p>
          <div class="zoom-gr-acts">
            <button class="btn btn-primary" id="liveBegin" type="button">${esc(T("r2.join", "Join interview"))}</button>
            <button class="btn btn-ghost" id="liveBail" type="button">${esc(T("r1.notNow", "Not now"))}</button>
          </div>
        </div>
      </div>`;
    $("#liveBegin")?.addEventListener("click", beginLive);
    $("#liveBail")?.addEventListener("click", quitLive);
    (async () => {
      try {
        LIVE_STREAM = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
        const v = $("#grVideo"), off = $("#grOff");
        if (v) { v.srcObject = LIVE_STREAM; }
        if (off) off.hidden = true;
      } catch (e) { /* no preview; Join still works (the pane has its own camera) */ }
    })();
  }
  async function beginLive() {
    if (CVI_CLOCK) return;                                   // already in a live interview; ignore re-entry
    const btn = $("#liveBegin"); if (btn) btn.disabled = true;
    let r, data;
    try {
      r = await fetch("/api/interviews/cvi/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(R2_CTX) });
      data = await r.json().catch(() => ({}));
    } catch (e) { toast(T("r2.cantStart", "Couldn't start the live interview.")); if (btn) btn.disabled = false; return; }
    if (r.status === 403) { toast(T("r2.notPassed", "Pass Round 1 first to unlock Round 2.")); quitLive(); return; }
    if (r.status === 402 || data.error === "round2_unavailable") { stopLiveStream(); renderR2Gate(await round2Status()); return; }
    if (!r.ok) { toast(data.error || T("r2.cantStart", "Couldn't start the live interview.")); if (btn) btn.disabled = false; return; }
    R2 = data;
    renderCviStage(data);
  }
  // ---- The live call, in our own page, with a timekeeper (Kofi, 2026-10-09) -------------------
  // A real interviewer with a day of candidates keeps time: they let an answer run its course,
  // then thank the person and move on, and they close on time. The AI interviewer cannot see a
  // clock, so this page is its watch. The call runs in Daily's frame inside our page (not Tavus's
  // own page in the side pane), which lets us hear who is speaking and pass TIME NOTES to the
  // interviewer over the call's data channel (Tavus Interactions Protocol). Nobody is cut off
  // mid-sentence by a timer: at the answer budget the interviewer moves on at the next pause, and
  // only if no pause comes does it step in politely, the way a person would.
  let CVI_CALL = null, CVI_TK = null, CVI_HEARD = false, CVI_T = null, CVI_WAIT = null;
  function cviSend(eventType, props) {
    if (!CVI_CALL || !R2) return;
    try {
      CVI_CALL.sendAppMessage({ message_type: "conversation", event_type: eventType,
                                conversation_id: R2.conversation_id, properties: props || {} }, "*");
    } catch (e) { /* the call may be closing */ }
  }
  const cviNote = (text) => cviSend("conversation.append_llm_context", { context: `TIME NOTE: ${text}` });
  const cviSay = (text) => cviSend("conversation.respond", { text: `TIME NOTE: ${text}` });

  // The timekeeper itself lives in timekeeper.js (pure, tested with a simulated clock).
  const startTimekeeper = (t) => window.SJTimekeeper.create(t, {
    note: cviNote, say: cviSay, end: () => endCvi(), now: () => Date.now(),
    later: (fn, ms) => setTimeout(fn, ms) });

  function renderCviStage(s) {
    const t = s.timing || { total: 1200, answer_nudge: 180, answer_step_in: 240, pause_wait: 15,
                            last_question: 930, candidate_questions: 1110, close_wait: 20, hard_end: 1230,
                            think_grace: 10, answer_grace: 60, minutes: 20 };
    stopLiveStream();   // the call takes the camera and mic itself; free the green-room preview
    // Our own call screen, not Daily's ready-made page in a box: that page brought its own
    // "Are you ready to join?" lobby (a second green room, scrolling inside the frame), and the
    // minutes a person spent in it could outlive the room's short-lived entry token, so their
    // Join was refused and the interview ended (Kofi, 2026-10-23). The call object joins the
    // moment our Join is clicked, and we draw the interviewer to fill the stage.
    $("#prepLive").classList.add("in-call");
    $("#liveStage").innerHTML =
      `<div class="zoom-call" id="cviCall">
        <video class="zoom-call-remote" id="cviRemote" autoplay playsinline></video>
        <audio id="cviAudio" autoplay></audio>
        <div class="zoom-call-wait" id="cviWait"><div class="zoom-call-wait-row"><span class="zoom-call-spin"></span>${esc(T("r2.connecting", "Connecting to your interviewer..."))}</div><div class="zoom-call-wait-sub">${esc(T("r2.connectingSub", "Nothing to click. They will greet you in a moment, and your time starts then."))}</div></div>
        <div class="zoom-call-top">
          <span class="zoom-live"><i></i> LIVE</span>
          <span class="zoom-title">${esc(T("r2.title", "Round 2, live interview"))}</span>
          <span class="zoom-mins" id="cviClockWrap">${esc(T("r2.timeLeft", "Time left"))}: <b id="cviMins">${fmtClock(t.total)}</b></span>
        </div>
        <video class="zoom-call-self" id="cviSelf" autoplay muted playsinline></video>
        <div class="zoom-call-bar">
          <button class="zoom-ctrl" id="cviMic" type="button">${esc(T("r2.mute", "Mute"))}</button>
          <button class="zoom-ctrl" id="cviCam" type="button">${esc(T("r2.camOff", "Stop video"))}</button>
          <button class="zoom-ctrl zoom-leave" id="cviEnd" type="button">${esc(T("r2.end", "End interview"))}</button>
        </div>
      </div>`;
    fitCviCall();
    $("#cviEnd")?.addEventListener("click", () => endCvi());
    if (window.Daily) {
      // Nothing counts until the interviewer is on screen: not the clock, not the timekeeper, and
      // not the pass. Joining, connecting, or a call that never gets going costs the person nothing.
      CVI_HEARD = false; CVI_T = t;
      CVI_CALL = window.Daily.createCallObject({ subscribeToTracksAutomatically: true });
      CVI_WAIT = setTimeout(() => cviDropped("no interviewer"), 90000);
      CVI_CALL.on("app-message", (e) => { if (CVI_TK) CVI_TK.onEvent(e && e.data); });
      CVI_CALL.on("track-started", cviTrack);
      CVI_CALL.on("track-stopped", cviTrack);
      CVI_CALL.on("error", (e) => cviDropped((e && e.errorMsg) || "error"));
      CVI_CALL.on("left-meeting", () => cviDropped("left"));
      $("#cviMic")?.addEventListener("click", (ev) => {
        const on = !CVI_CALL.localAudio(); CVI_CALL.setLocalAudio(on);
        ev.currentTarget.classList.toggle("off", !on);
        ev.currentTarget.textContent = on ? T("r2.mute", "Mute") : T("r2.unmute", "Unmute");
      });
      $("#cviCam")?.addEventListener("click", (ev) => {
        const on = !CVI_CALL.localVideo(); CVI_CALL.setLocalVideo(on);
        ev.currentTarget.classList.toggle("off", !on);
        ev.currentTarget.textContent = on ? T("r2.camOff", "Stop video") : T("r2.camOn", "Start video");
      });
      // The broker hands a room URL with the meeting token in ?t=; the call object takes it apart.
      let url = s.join_url, token;
      try { const u = new URL(s.join_url); token = u.searchParams.get("t") || undefined; u.searchParams.delete("t"); url = u.toString(); } catch (e) {}
      CVI_CALL.join({ url, token }).catch((e) => cviDropped((e && e.message) || "join"));
    } else if (window.tailorShell) {
      window.tailorShell.openBrowser(s.join_url);          // fallback: the old side-pane call, no timekeeper
    } else {
      window.open(s.join_url, "_blank");
    }
    CVI_UNBILLED = 0;
    const t0 = Date.now();
    CVI_CLOCK = setInterval(() => {
      if (CVI_CALL && !CVI_HEARD) return;                  // still connecting: free
      const now = CVI_TK ? CVI_TK.tick() : (Date.now() - t0) / 1000;
      const left = Math.max(0, Math.round(t.total - now));
      const wrap = $("#cviClockWrap");
      if (wrap) wrap.innerHTML = left > 0
        ? `${esc(T("r2.timeLeft", "Time left"))}: <b id="cviMins">${fmtClock(left)}</b>`
        : `<b>${esc(T("r2.wrappingUp", "Wrapping up"))}</b>`;
      if (!CVI_TK && now >= t.hard_end) { endCvi(); return; }
      // On the plan, report time used every 30 s so the broker meters it (capped at 20 minutes
      // per interview there, so the wrap-up is never charged).
      if (s.source === "plan" && ++CVI_UNBILLED >= 30) {
        const secs = CVI_UNBILLED; CVI_UNBILLED = 0;
        fetch("/api/interviews/cvi/heartbeat", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ seconds: secs, interview_id: s.interview_id }) })
          .then(r => r.json()).then(d => { if (d && d.stop && CVI_CLOCK) endCvi(); }).catch(() => {});
      }
    }, 1000);
  }
  // The call fills the window below the page header, so nothing on the page scrolls.
  function fitCviCall() {
    const el = $("#cviCall"); if (!el) return;
    el.style.height = Math.max(380, window.innerHeight - el.getBoundingClientRect().top - 20) + "px";
    // The view's own bottom padding and footer sit below the call: take back whatever still spills.
    const view = el.closest(".view");
    const spill = view ? view.scrollHeight - view.clientHeight : 0;
    if (spill > 0) el.style.height = Math.max(380, el.offsetHeight - spill) + "px";
  }
  window.addEventListener("resize", fitCviCall);
  // Each track goes to its own element: the interviewer fills the stage, you sit in the corner.
  function cviTrack(e) {
    if (!CVI_CALL || !e || !e.participant) return;
    const p = e.participant, local = !!p.local;
    const tr = p.tracks || {};
    const v = (tr.video && tr.video.state === "playable") ? tr.video.persistentTrack : null;
    const a = (tr.audio && tr.audio.state === "playable") ? tr.audio.persistentTrack : null;
    if (local) {
      const self = $("#cviSelf");
      if (self) { self.srcObject = v ? new MediaStream([v]) : null; self.hidden = !v; }
      return;
    }
    const remote = $("#cviRemote"), audio = $("#cviAudio");
    if (remote) remote.srcObject = v ? new MediaStream([v]) : null;
    if (audio) audio.srcObject = a ? new MediaStream([a]) : null;
    if (v && !CVI_HEARD) {                                 // the interviewer is here: the interview starts now
      CVI_HEARD = true; clearTimeout(CVI_WAIT);
      CVI_TK = startTimekeeper(CVI_T);
      const w = $("#cviWait"); if (w) w.hidden = true;
    }
  }
  // The call closed without our End button. Once the interviewer has been on screen this is the
  // interview ending (Tavus's own time limit, a drop that never came back): score what was said.
  // Before that, there is nothing to score: say so plainly and offer to try again.
  function cviDropped(why) {
    if (!R2 || !CVI_CALL) return;
    if (CVI_HEARD) { endCvi(); return; }
    console.warn("live interview: call closed before the interviewer appeared:", why);
    endCvi({ early: why });
  }
  function teardownCvi() {                                 // idempotent: safe to call from any exit path
    if (CVI_CLOCK) { clearInterval(CVI_CLOCK); CVI_CLOCK = null; }
    $("#prepLive")?.classList.remove("in-call");
    clearTimeout(CVI_WAIT);
    CVI_TK = null;
    if (CVI_CALL) { const c = CVI_CALL; CVI_CALL = null; c.leave().catch(() => {}).finally(() => { try { c.destroy(); } catch (e) {} }); }
    if (window.tailorShell) window.tailorShell.closeBrowser();
    if (CVI_PREV_W) {                                      // restore the panel width an old-style call widened
      document.documentElement.style.setProperty("--panel-w", CVI_PREV_W);
      CVI_PREV_W = "";
      window.dispatchEvent(new Event("resize"));
    }
  }
  // "End interview": close the pane, then score the conversation into the report.
  async function endCvi(opt) {
    teardownCvi();
    const id = R2 && R2.interview_id;
    if (id && R2.source === "plan" && CVI_UNBILLED > 0) {   // meter the last few seconds too
      const secs = CVI_UNBILLED; CVI_UNBILLED = 0;
      fetch("/api/interviews/cvi/heartbeat", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seconds: secs, interview_id: id }) }).catch(() => {});
    }
    R2 = null;
    if (!id) { quitLive(); return; }
    $("#liveStage").innerHTML = `<div class="scr-intro"><div class="scr-welcome"><h2 class="scr-ready-h">${esc(T("r2.ended", "Interview ended"))}</h2><p class="scr-ready-sub">${esc(T("r2.scoring", "Scoring your interview..."))}</p></div></div>`;
    try { const d = await api("/api/interviews/cvi/end", { interview_id: id }); REPORT_ROLE = ((d && d.interview) || {}).role || ""; renderR2Report((d && d.report) || {}, id); }
    catch (err) {
      if ((err.data && err.data.error === "no_transcript") || (opt && opt.early)) { renderCviNoStart(); return; }
      toast(err.message || T("r2.cantScore", "Couldn't score the interview.")); quitLive();
    }
  }
  // Nothing was said, so there is no report. Never drop the person back on the prep list in
  // silence: tell them what happened and let them go straight back in.
  function renderCviNoStart() {
    $("#liveStage").innerHTML = `<div class="scr-intro"><div class="scr-welcome">
      <h2 class="scr-ready-h">${esc(T("r2.noStartH", "The call closed before your interview began"))}</h2>
      <p class="scr-ready-sub">${esc(T("r2.noStartSub", "Nothing was said, so there is nothing to score. You can join again now."))}</p>
      <div class="zoom-gr-acts" style="justify-content:center;margin-top:16px">
        <button class="btn btn-primary" id="cviRetry" type="button">${esc(T("r2.tryAgain", "Join again"))}</button>
        <button class="btn btn-ghost" id="cviBack" type="button">${esc(T("r1.backToPrep", "Back to prep"))}</button>
      </div></div></div>`;
    $("#cviRetry")?.addEventListener("click", async () => renderR2GreenRoom(await round2Status()));
    $("#cviBack")?.addEventListener("click", quitLive);
  }
  function renderR2Report(rep, interviewId) {
    rep = rep || {};
    if (GUIDED) showGuidedProgress(3);                       // both rounds done
    const items = (rep.answers || []).map((a, k) => ({ i: k, q: a.q || "", said: a.transcript ? highlightDisfluencies(a.transcript) : "",
      competency: a.competency || "", score: a.score, feedback: a.feedback || "", extra: "", coach: false }));
    $("#prepLive").classList.add("is-report");
    $("#liveStage").innerHTML = reportA2Html(rep, {
      kind: T("rep.kindLive", "Live interview"), role: (R2_CTX && R2_CTX.role) || REPORT_ROLE || "", company: "",
      when: REPORT_WHEN || Date.now() / 1000, items, round: 2,
      transcript: rep.transcript ? r2TranscriptHtml(rep.transcript) : "", actions: "" });
    REPORT_WHEN = null; REPORT_ROLE = "";
    wireReportA2(rep, { prepId: R2_CTX && R2_CTX.prep_id, currentId: interviewId,
      leave: () => { teardownCvi(); R2 = null; stopLiveStream(); $("#prepLive").hidden = true; $("#prepLive").classList.remove("is-report"); },
      back: quitLive, again: () => { $("#prepLive").classList.remove("is-report"); startRound2(R2_CTX && R2_CTX.prep_id, "", true); } });
  }
  // The live transcript is a list of turns; show it as a dialogue, fillers highlighted.
  function r2TranscriptHtml(tr) {
    if (typeof tr === "string") return highlightDisfluencies(tr);
    if (!Array.isArray(tr)) return "";
    return tr.map(t => `<span class="rc-turn ${t.role === "interviewer" ? "iv" : "me"}">${esc(t.role === "interviewer" ? T("rc.interviewer", "Interviewer") : T("rc.you", "You"))}:</span> ${t.role === "interviewer" ? esc(t.content || "") : highlightDisfluencies(t.content || "")}`).join("<br>");
  }
  function stopLiveStream() { if (LIVE_STREAM) { LIVE_STREAM.getTracks().forEach(t => t.stop()); LIVE_STREAM = null; } }
  function quitLive() {
    teardownCvi();                                          // every exit path ends the live interview cleanly
    R2 = null; stopLiveStream(); $("#prepLive").hidden = true; $("#prepList").hidden = false; loadPreps();
  }
  $("#liveQuit")?.addEventListener("click", quitLive);

  // ---------------------------------------------------------------- Report card (both rounds)
  // The structured card the engine stores next to the overall score (interview/report.py):
  // delivery summary, a per-question table with STAR badges, strengths, the three weakest
  // moments with a one-line drill each and a Re-drill button that runs a Round 1 recorded
  // session with just those questions, and the past cards for this job with the score trend.
  // The prep checklist (goals) shows on the start screen of both rounds. Practice only.
  function rcPaceWord(p) {
    return p === "slow" ? T("rc.paceSlow", "slow") : p === "fast" ? T("rc.paceFast", "fast")
      : p === "normal" ? T("rc.paceNormal", "normal") : T("rc.paceUnknown", "no pace yet");
  }
  function rcStarBadges(row) {
    const parts = [["situation", "S", T("rc.situation", "Situation")], ["task", "T", T("rc.task", "Task")],
                   ["action", "A", T("rc.action", "Action")], ["result", "R", T("rc.result", "Result")]];
    return `<span class="rc-star">${parts.map(([k, l, name]) => {
      const v = row.star ? row.star[k] : null;
      const cls = v === true ? "on" : v === false ? "off" : "na";
      return `<i class="rc-star-b ${cls}" title="${esc(name)}">${l}</i>`; }).join("")}</span>`;
  }
  function rcCoverage(c) {
    if (c == null) return `<span class="rc-cov na">${esc(T("rc.notRated", "not rated"))}</span>`;
    return `<span class="rc-cov c${c}">${esc(T("rc.coverageOf", "{n} of 3").replace("{n}", String(c)))}</span>`;
  }
  // ---- The interview report (design A2, Kofi 2026-10-23) ---------------------------------------
  // Numbers first, then what to fix, then the detail in tabs. The page stays grey like the rest of
  // the app; the only colour is status, and every colour comes with a word ("Needs work", "Too
  // fast"), never colour alone, so it reads the same to someone who is colour-blind.
  let REPORT_WHEN = null, REPORT_ROLE = "", REPORT_FROM_PAST = false;
  function a2Level(n) {
    n = Number(n) || 0;
    return n >= 70 ? ["good", T("rep.strong", "Strong")] : n >= 50 ? ["mid", T("rep.gettingThere", "Getting there")] : ["flag", T("rep.needsWork", "Needs work")];
  }
  const a2First = (s) => { const m = String(s || "").match(/^(.+?[.:])\s/); return m ? m[1] : String(s || ""); };
  function reportA2Html(rep, o) {
    const card = rep.report_card || {}, d = card.delivery || {}, rca = {};
    (card.answers || []).forEach(a => { rca[a.i] = a; });
    const [lvl, lvlWord] = a2Level(rep.score);
    const mins = Math.max(1, Math.round((Number(rep.duration_s) || Number(d.total_seconds) || 0) / 60));
    const when = o.when ? new Date(o.when * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "";
    const pace = d.pace === "fast" ? `<b class="a2-flag">${esc(T("rep.tooFast", "Too fast."))}</b> `
      : d.pace === "slow" ? `<b class="a2-flag">${esc(T("rep.tooSlow", "A little slow."))}</b> `
      : d.pace === "normal" ? `<b class="a2-good">${esc(T("rep.goodPace", "Good pace."))}</b> ` : "";
    const fpm = Number(d.fillers_per_minute) || 0;
    const full = (card.answers || []).filter(a => a.star && a.star.situation && a.star.task && a.star.action && a.star.result).length;
    const stat = (k, v, unit, note) => `<div class="a2-stat"><div class="a2-eyebrow">${esc(k)}</div><div class="a2-stat-v">${v}<small>${unit}</small></div><div class="a2-stat-n">${note}</div></div>`;
    const stars = (st) => st ? `<span class="a2-star">${[["situation", "S"], ["task", "T"], ["action", "A"], ["result", "R"]].map(([k, l]) => `<i class="${st[k] ? "on" : ""}" title="${esc(k)}">${l}</i>`).join("")}</span>` : "";
    const fix = (rep.improvements || []).slice(0, 3).map(x => { const f = a2First(x); return `<li><div class="a2-fix-h">${esc(f)}</div>${x.length > f.length ? `<div class="a2-fix-d">${esc(x.slice(f.length).trim())}</div>` : ""}</li>`; }).join("");
    const skills = (rep.competencies || []).map(c => { const [l] = a2Level(c.score);
      return `<div class="a2-skill"><span>${esc(c.name)}</span><span class="a2-bar"><b class="a2-${l}" style="width:${Math.max(4, Math.min(100, Number(c.score) || 0))}%"></b></span><span class="a2-${l} num">${c.score}</span></div>`; }).join("");
    const qs = o.items.map((it, k) => { const [l, w] = a2Level(it.score); const rc = rca[it.i] || rca[k] || {};
      return `<div class="a2-q${k === 0 ? " is-open" : ""}" data-q="${k}">
        <button class="a2-q-row" type="button"><span class="a2-qn num">${String(k + 1).padStart(2, "0")}</span><span class="a2-qt">${esc(it.q)}</span>
          <span class="a2-qc">${esc(it.competency)}</span>
          <span class="a2-qs">${it.score != null ? `<i class="a2-dot a2-${l}"></i><span class="a2-${l} a2-qw">${esc(w)}</span><b class="num">${it.score}</b><small>/100</small>` : ""}</span>
          <span class="a2-chev" aria-hidden="true"></span></button>
        <div class="a2-q-body"><div><div class="a2-eyebrow">${esc(T("rep.youSaid", "What you said"))}</div><p class="a2-said">${it.said || ""}</p>${stars(rc.star)}${it.extra || ""}</div>
          <div><div class="a2-eyebrow">${esc(T("rep.feedback", "Feedback"))}</div><p class="a2-fb">${esc(it.feedback || rc.evidence || "")}</p>
          ${it.coach ? `<button class="btn btn-ghost btn-sm" data-coach="${it.i}" type="button">${esc(T("r1.stronger", "See a stronger version"))}</button><div class="scr-coach" id="scrCoach${it.i}" hidden></div>` : ""}</div></div>
      </div>`; }).join("");
    const nWeak = (card.weakest || []).length;
    const tabs = [["q", `${esc(T("rep.tabQuestions", "Questions"))} <span>${o.items.length}</span>`], o.transcript ? ["t", esc(T("r2.transcript", "Transcript"))] : null,
      ["g", esc(T("goals.afterTitle", "What to cover next time"))], ["h", esc(T("rep.tabProgress", "Progress"))]].filter(Boolean);
    return `<div class="a2">
      <button class="btn btn-ghost btn-back a2-back" id="a2Back" type="button">&lsaquo; ${esc(REPORT_FROM_PAST ? T("prep.past", "Past interviews") : T("prep.title", "Interview prep"))}</button>
      <div class="a2-head"><div><div class="a2-eyebrow">${esc(o.kind)}${when ? ` &middot; ${esc(when)}` : ""} &middot; ${mins} ${esc(T("rep.min", "min"))}</div>
        <h2 class="a2-title">${esc(o.role || T("prep.pastRole", "Interview"))}${o.company ? ` <span>${esc(o.company)}</span>` : ""}</h2></div>
        <span class="a2-pill a2-pill-${rep.passed ? "good" : "flag"}">${esc(rep.passed ? T("rep.passed", "Passed") : T("rep.below", "Below the bar"))}</span></div>
      <div class="a2-stats">
        ${stat(T("rep.overall", "Overall"), `<span class="num">${rep.score ?? 0}</span>`, "/100", `<b class="a2-${lvl}">${esc(lvlWord)}.</b> ${esc(T("rep.barIs", "The bar is {n}").replace("{n}", String(rep.threshold || 70)))}`)}
        ${stat(T("rep.pace", "Pace"), `<span class="num">${Math.round(Number(d.avg_wpm) || 0)}</span>`, ` ${esc(T("rep.wpm", "wpm"))}`, `${pace}${esc(T("rep.aim", "Aim for 140 to 160"))}${d.any_estimated ? " *" : ""}`)}
        ${stat(T("rep.fillers", "Fillers"), `<span class="num">${fpm}</span>`, `/${esc(T("rep.min", "min"))}`, `${fpm > 6 ? `<b class="a2-flag">${esc(T("rep.manyFillers", "Many."))}</b> ` : ""}${esc(T("rep.fillersTotal", "{n} in {m} minutes").replace("{n}", String(d.total_fillers || 0)).replace("{m}", String(mins)))}`)}
        ${stat(T("rep.stories", "Complete stories"), `<span class="num">${full}</span>`, ` ${esc(T("rep.of", "of"))} ${o.items.length}`, esc(T("rep.starFull", "Situation, Task, Action, Result")))}
      </div>
      ${rep.why ? `<p class="a2-why">${esc(rep.why)}</p>` : ""}
      <div class="a2-two">
        ${fix ? `<div class="a2-card"><div class="a2-lab">${esc(T("rep.fixFirst", "Fix these first"))}</div><ol class="a2-fix">${fix}</ol></div>` : ""}
        ${skills ? `<div class="a2-card"><div class="a2-lab">${esc(T("rep.bySkill", "By skill"))}</div>${skills}</div>` : ""}
      </div>
      <div class="a2-tabs" role="tablist">${tabs.map(([k, l], i) => `<button class="a2-tab${i === 0 ? " is-on" : ""}" type="button" data-tab="${k}">${l}</button>`).join("")}</div>
      <div class="a2-pane" data-pane="q"><div class="a2-card a2-qs-list">${qs}</div></div>
      ${o.transcript ? `<div class="a2-pane" data-pane="t" hidden><div class="a2-card a2-tr">${o.transcript}</div></div>` : ""}
      <div class="a2-pane" data-pane="g" hidden><div class="rc-goals" id="repGoals" hidden></div></div>
      <div class="a2-pane" data-pane="h" hidden><div class="rc-sec rc-hist" id="rcHistory" hidden></div><p class="a2-none" id="a2NoHist">${esc(T("rep.noHistory", "Your first report for this job. Practice again to see your progress here."))}</p></div>
      ${o.gate ? `<div id="r1Gate"></div>` : ""}
      <div class="a2-acts">
        ${nWeak ? `<button class="btn btn-primary btn-sm" id="rcRedrill" type="button">${esc(nWeak === 1 ? T("rc.redrillOne", "Re-drill this question") : T("rep.redrillWeakest", "Practice the weakest {n} again").replace("{n}", String(nWeak)))}</button>` : ""}
        <button class="btn btn-ghost btn-sm" id="a2Again" type="button">${esc(T("rep.practiceAgain", "Practice again"))}</button>
        ${o.actions || ""}
        <span class="prep-step1-msg" id="rcRedrillMsg" hidden></span>
      </div>
      <p class="scr-disclaimer">${esc(rep.disclaimer || T("r1.reportDisclaimer", "Practice feedback, not a hiring decision."))}${d.any_estimated ? ` * ${esc(T("rc.estimated", "Estimated from the words, not timed."))}` : ""}</p>
    </div>`;
  }
  function wireReportA2(rep, ctx) {
    const root = document.querySelector(".a2"); if (!root) return;
    root.querySelectorAll(".a2-q-row").forEach(b => b.addEventListener("click", () => b.parentElement.classList.toggle("is-open")));
    root.querySelectorAll(".a2-tab").forEach(b => b.addEventListener("click", () => {
      root.querySelectorAll(".a2-tab").forEach(x => x.classList.toggle("is-on", x === b));
      root.querySelectorAll(".a2-pane").forEach(p => { p.hidden = p.dataset.pane !== b.dataset.tab; });
    }));
    if (ctx.coach) root.querySelectorAll("[data-coach]").forEach(b => b.addEventListener("click", () => ctx.coach(b.dataset.coach, b)));
    $("#a2Back")?.addEventListener("click", () => { const past = REPORT_FROM_PAST; REPORT_FROM_PAST = false; ctx.back(); if (past) showPastPage(); });
    $("#a2Again")?.addEventListener("click", () => { REPORT_FROM_PAST = false; ctx.again(); });
    loadPrepGoals(ctx.prepId, $("#repGoals"));
    wireReportCard(rep.report_card, ctx);
    // The Progress tab: the job's score history, when there is more than this one report.
    const hist = $("#rcHistory");
    if (hist) new MutationObserver(() => { const n = $("#a2NoHist"); if (n) n.hidden = !hist.hidden; }).observe(hist, { attributes: true, attributeFilter: ["hidden"] });
  }
  function reportCardHtml(card) {
    if (!card || !Array.isArray(card.answers) || !card.answers.length) return "";
    const d = card.delivery || {};
    const rows = card.answers.map((r, k) => `
      <tr>
        <td class="rc-q"><span class="rc-n">${k + 1}.</span> ${esc(r.q || "")}${r.evidence ? `<div class="rc-ev">${esc(r.evidence)}</div>` : ""}</td>
        <td>${rcCoverage(r.coverage)}</td>
        <td>${rcStarBadges(r)}</td>
        <td class="rc-num">${Math.round(Number(r.seconds) || 0)}${r.seconds_estimated ? "<sup>*</sup>" : ""}</td>
        <td class="rc-num">${Math.round(Number(r.wpm) || 0)}</td>
        <td class="rc-num">${Number(r.fillers) || 0}</td>
      </tr>`).join("");
    const weak = (card.weakest || []).map((w, k) => `
      <li class="rc-weak-item">
        <div class="rc-weak-q">${k + 1}. ${esc(w.q || "")}</div>
        <div class="rc-weak-drill">${esc(w.drill || "")}</div>
      </li>`).join("");
    const nWeak = (card.weakest || []).length;
    return `
      <div class="rc">
        <div class="rc-h">${esc(T("rc.title", "Report card"))}</div>
        <div class="rc-tiles">
          <div class="rc-tile"><div class="rc-tile-v">${Math.round(Number(d.avg_wpm) || 0)} <span class="rc-pace ${esc(d.pace || "unknown")}">${esc(rcPaceWord(d.pace))}</span></div>
            <div class="rc-tile-l">${esc(T("rc.avgPace", "Average pace"))}, ${esc(T("rc.wpmUnit", "words a minute"))}. ${esc(T("rc.guideline", "Guideline: 140 to 160."))}</div></div>
          <div class="rc-tile"><div class="rc-tile-v">${Number(d.fillers_per_minute) || 0}</div>
            <div class="rc-tile-l">${esc(T("rc.fillersPerMin", "Fillers a minute"))}</div></div>
          <div class="rc-tile"><div class="rc-tile-v">${Number(d.answers) || 0}</div>
            <div class="rc-tile-l">${esc(T("rc.answered", "Answers"))}, ${Math.round(Number(d.total_seconds) || 0)}s</div></div>
        </div>
        <div class="rc-table-wrap"><table class="rc-table">
          <thead><tr><th>${esc(T("rc.colQuestion", "Question"))}</th><th>${esc(T("rc.colCoverage", "Coverage"))}</th><th>${esc(T("rc.colStar", "STAR"))}</th><th>${esc(T("rc.colSeconds", "Seconds"))}</th><th>${esc(T("rc.colWpm", "WPM"))}</th><th>${esc(T("rc.colFillers", "Fillers"))}</th></tr></thead>
          <tbody>${rows}</tbody>
        </table></div>
        ${d.any_estimated ? `<p class="rc-note">* ${esc(T("rc.estimated", "Estimated from the words, not timed."))}</p>` : ""}
        ${(card.strengths || []).length ? `<div class="rc-sec"><div class="prep-fb-h">${esc(T("rc.strengths", "Strengths"))}</div><ul class="rc-list">${card.strengths.map(s => `<li>${esc(s)}</li>`).join("")}</ul></div>` : ""}
        ${nWeak ? `<div class="rc-sec rc-weak">
          <div class="prep-fb-h">${esc(nWeak === 1 ? T("rc.practiceAgainOne", "Practice this one again") : T("rc.practiceAgain", "Practice these again"))}</div>
          <ol class="rc-weak-list">${weak}</ol>
          <div class="rc-weak-acts">
            <button class="btn btn-primary btn-sm" id="rcRedrill" type="button">${esc(nWeak === 1 ? T("rc.redrillOne", "Re-drill this question") : T("rc.redrill", "Re-drill these questions"))}</button>
            <span class="prep-step1-msg" id="rcRedrillMsg" hidden></span>
          </div></div>` : ""}
        <div class="rc-sec rc-hist" id="rcHistory" hidden></div>
      </div>`;
  }
  async function prepById(id) {
    let p = (PREPS || []).find(x => x.id === id);
    if (p) return p;
    try { const d = await api("/api/preps"); PREPS = d.preps || []; } catch { PREPS = PREPS || []; }
    return (PREPS || []).find(x => x.id === id) || { id };
  }
  // Wire the card's Re-drill and load the job's history. `ctx`: {prepId, currentId, leave} where
  // leave() closes the current round's surface before the drill opens the Round 1 one.
  function wireReportCard(card, ctx) {
    if (!card || !ctx || !ctx.prepId) return;
    $("#rcRedrill")?.addEventListener("click", async () => {
      const btn = $("#rcRedrill"), msg = $("#rcRedrillMsg");
      const drill = (card.weakest || []).map(w => ({ q: w.q, competency: w.competency || "" })).filter(w => w.q);
      if (!drill.length) return;
      btn.disabled = true; if (msg) { msg.textContent = T("rc.redrillStarting", "Setting up the drill..."); msg.hidden = false; }
      const prep = await prepById(ctx.prepId);
      if (!prep || !prep.id) { btn.disabled = false; if (msg) msg.textContent = T("rc.redrillFailed", "Couldn't start the drill."); return; }
      GUIDED = false; hideGuidedProgress();     // a drill is a side step, not the full mock
      if (typeof ctx.leave === "function") ctx.leave();
      startScreen(prep, { drill });
    });
    renderCardHistory(ctx.prepId, ctx.currentId);
  }
  async function renderCardHistory(prepId, currentId) {
    const box = $("#rcHistory"); if (!box) return;
    let h;
    try { h = await api(`/api/interview/history?prep_id=${encodeURIComponent(prepId)}`); } catch { return; }
    const cards = (h && h.cards) || [];
    if (!cards.length) return;
    const tr = h.trend || {};
    const trend = tr.count <= 1 ? T("rc.trendOne", "Your first report card for this job.")
      : tr.direction === "up" ? T("rc.trendUp", "Up {d} since your first attempt. Best so far: {b}.").replace("{d}", String(Math.abs(tr.delta))).replace("{b}", String(tr.best))
      : tr.direction === "down" ? T("rc.trendDown", "Down {d} since your first attempt. Best so far: {b}.").replace("{d}", String(Math.abs(tr.delta))).replace("{b}", String(tr.best))
      : T("rc.trendFlat", "Holding steady. Best so far: {b}.").replace("{b}", String(tr.best));
    const max = Math.max(100, ...cards.map(c => Number(c.score) || 0));
    box.innerHTML = `
      <div class="prep-fb-h">${esc(T("rc.history", "Past report cards for this job"))}</div>
      <div class="rc-trend">${esc(trend)}</div>
      <div class="rc-hist-bars">${cards.map(c => `<span class="rc-hist-bar ${c.id === currentId ? "is-cur" : ""} ${c.passed ? "ok" : ""}" style="height:${Math.max(6, Math.round((Number(c.score) || 0) / max * 48))}px" title="${esc(String(c.score))}"></span>`).join("")}</div>
      <ul class="rc-hist-list">${cards.slice().reverse().map(c => `
        <li class="${c.id === currentId ? "is-cur" : ""}">
          <span class="rc-hist-score">${c.score}</span>
          <span class="rc-hist-what">${esc(c.round === 2 ? T("rc.histRound2", "Round 2") : T("rc.histRound1", "Round 1"))}${c.drill ? `, ${esc(T("rc.histDrill", "drill"))}` : ""}${c.id === currentId ? ` (${esc(T("rc.histThis", "this one"))})` : ""}</span>
          <span class="rc-hist-when">${esc(fmtDate(c.created))}</span>
        </li>`).join("")}</ul>`;
    box.hidden = false;
  }
  // The prep checklist on the start screen: likely questions, what to mention, company facts.
  // The prep checklist (likely questions, what to mention, the company facts). It shows AFTER an
  // interview, in the report, never before: a real interview hands you no answer sheet, so the
  // practice one doesn't either (Kofi, 2026-10-23).
  async function loadPrepGoals(prepId, box) {
    if (!box || !prepId) return;
    const TITLE = T("goals.afterTitle", "What to cover next time");
    box.hidden = false;
    box.innerHTML = `<div class="rc-goals-h">${esc(TITLE)}</div><p class="rc-goals-loading">${esc(T("goals.loading", "Building your prep checklist..."))}</p>`;
    let g;
    try { g = (await api(`/api/preps/${encodeURIComponent(prepId)}/goals`)).goals; }
    catch { box.innerHTML = `<div class="rc-goals-h">${esc(TITLE)}</div><p class="rc-goals-loading">${esc(T("goals.failed", "Couldn't build the checklist right now."))}</p>`; return; }
    if (!g) { box.hidden = true; return; }
    const col = (h, items, empty) => `<div class="rc-goals-col"><div class="prep-fb-h">${esc(h)}</div>${(items || []).length ? `<ol class="rc-goals-list">${items.map(x => `<li>${esc(x)}</li>`).join("")}</ol>` : `<p class="rc-note">${esc(empty)}</p>`}</div>`;
    box.innerHTML = `<div class="rc-goals-h">${esc(TITLE)}</div>
      <div class="rc-goals-grid">
        ${col(T("goals.questions", "Likely questions"), g.questions, "")}
        ${col(T("goals.mentions", "Make sure to mention"), g.mentions, "")}
        ${col(T("goals.facts", "What the posting says about them"), g.company_facts, T("goals.noFacts", "The posting says nothing about the company. Check their site before you go."))}
      </div>`;
  }

  // Whole 20-minute interviews from the pass's minutes (one interview is 20 minutes on the broker).
  function r2InterviewsLeft(mins) {
    const n = Math.floor(Number(mins) / 20);
    return n === 1 ? T("r2.oneLeft", "1 interview left.") : T("r2.nLeft", "{n} interviews left.").replace("{n}", String(n));
  }

  /* ---------------------------------------------------------------- Connect your AI (API key) */
  let AI_CONFIGURED = false;
  let MANAGED_AI = false;            // official edition, company AI reachable: never ask for a key
  // Provider picker: Claude (recommended default) or OpenAI. Only the transport differs;
  // the whole app is provider-agnostic behind /api/apikey + AI_PROVIDER.
  const AI_PROVIDERS = {
    // The key page opens in the person's own browser (shell routes these hosts there), where
    // they are already signed in; sign-in cannot work inside the app's built-in browser.
    anthropic: {
      label: "Claude", place: "Paste your key (sk-ant-…)", prefix: "sk-ant-",
      title: "Connect Claude to write for you",
      keyUrl: "https://platform.claude.com/settings/keys",
      note: "New to Anthropic? Add $5 of credit under Billing first, or the key won't work.",
    },
    openai: {
      label: "OpenAI", place: "Paste your key (sk-…)", prefix: "sk-",
      title: "Connect OpenAI to write for you",
      keyUrl: "https://platform.openai.com/api-keys",
      note: "Add billing under Settings first, or the key won't work.",
    },
  };
  let AI_PROVIDER_SEL = "anthropic";
  function renderAiProvider(p) {
    AI_PROVIDER_SEL = (p === "openai") ? "openai" : "anthropic";
    const info = AI_PROVIDERS[AI_PROVIDER_SEL];
    if ($("#aiTitle")) $("#aiTitle").textContent = info.title;
    if ($("#aiSteps")) $("#aiSteps").innerHTML =
      `<a class="btn btn-ghost" href="${info.keyUrl}" target="_blank" rel="noopener">Get a key &#8599;</a>`
      + `<p class="ai-note-line">${esc(info.note)}</p>`;
    if ($("#aiKeyInput")) $("#aiKeyInput").placeholder = info.place;
    document.querySelectorAll("#aiProvider .ai-prov-b").forEach(b =>
      b.classList.toggle("is-on", b.dataset.provider === AI_PROVIDER_SEL));
  }
  document.querySelectorAll("#aiProvider .ai-prov-b").forEach(b =>
    b.addEventListener("click", () => renderAiProvider(b.dataset.provider)));

  async function loadApiKeyStatus() {
    try {
      const s = await api("/api/apikey");
      AI_CONFIGURED = !!s.configured;
      if (s.provider) AI_PROVIDER_SEL = s.provider;
      $("#aiDot")?.classList.toggle("on", AI_CONFIGURED);
      const lbl = $("#aiChipLabel"); if (lbl) lbl.textContent = AI_CONFIGURED ? "AI connected" : "Connect AI";
      const mk = $("#aiMasked"); if (mk) mk.textContent = s.masked || "";
      const sl = $("#setKeyLabel"); if (sl) sl.textContent = `${s.provider_label || "Anthropic"} API key`;
      // Official edition with the managed AI up: no key talk anywhere. The key stays reachable
      // under Settings as an optional advanced choice.
      MANAGED_AI = !!s.managed;
      // The official app sells SponsorJobs' AI and interviewer; bringing your own key is what the
      // open-source edition is for (Kofi, 2026-10-10). So in the official app every own-key option
      // (Settings tab, menu item, Upgrade button, interview gate) is hidden, not just played down.
      document.documentElement.classList.toggle("is-managed", MANAGED_AI);
      const gk = $("#menuGetKey"); if (gk) gk.hidden = MANAGED_AI;
      if (MANAGED_AI) {
        if (sl) sl.textContent = "Advanced: your own AI key";
        const st = $("#setKeyState");
        if (st && !s.configured) st.textContent = "Optional. SponsorJobs' AI is included.";
      }
      return s;
    } catch { return { configured: false }; }
  }
  function openAiModal(replace) {
    const setup = $("#aiSetup"), conn = $("#aiConnected");
    renderAiProvider(AI_PROVIDER_SEL);
    if (AI_CONFIGURED && !replace) { setup.hidden = true; conn.hidden = false; }
    else { setup.hidden = false; conn.hidden = true; $("#aiMsg").hidden = true; $("#aiKeyInput").value = ""; }
    $("#aiModal").hidden = false;
    if (!setup.hidden) setTimeout(() => $("#aiKeyInput").focus(), 40);
  }
  function closeAiModal() { $("#aiModal").hidden = true; }
  function aiFlash(msg, ok) { const m = $("#aiMsg"); m.hidden = false; m.textContent = msg; m.className = "ai-msg " + (ok ? "ok" : "err"); }
  $("#aiChip")?.addEventListener("click", () => openAiModal(false));

  /* outage screen actions */
  $("#downClose")?.addEventListener("click", hideOutage);
  $("#downFixBtn")?.addEventListener("click", () => {
    hideOutage();
    // Paywall -> the plans panel; every other outage with a button -> add-your-key.
    if (lastOutageReason === "upgrade_required") openUpgrade();
    else openAiModal(true);
  });
  $("#downRetry")?.addEventListener("click", async () => {
    const btn = $("#downRetry");
    btn.disabled = true; btn.textContent = "Checking…";
    try {
      await api("/api/whereami");        // cheap round-trip: is the app reachable again?
      hideOutage();
      if (lastCall) await api(lastCall.url, lastCall.body).catch(() => {});
    } catch { /* the outage screen stays up and re-states the reason */ }
    btn.disabled = false; btn.textContent = "Try again";
  });

  /* ------------------------------------------------- account menu + settings */
  const acctBtn = $("#acctBtn"), acctMenu = $("#acctMenu");
  function closeAcctMenu() {
    if (!acctMenu) return;
    acctMenu.hidden = true;
    closeLangMenu();                 // the Language flyout is a child of this menu
    acctBtn?.classList.remove("is-open");
    acctBtn?.setAttribute("aria-expanded", "false");
  }
  function openAcctMenu() {
    if (!acctMenu || !acctBtn) return;
    const r = acctBtn.getBoundingClientRect();
    acctMenu.hidden = false;
    // Sit above the row, left-aligned to it, clamped so a collapsed rail can't push it off.
    acctMenu.style.left = Math.max(8, r.left) + "px";
    acctMenu.style.bottom = (window.innerHeight - r.top + 6) + "px";
    acctBtn.classList.add("is-open");
    acctBtn.setAttribute("aria-expanded", "true");
  }
  acctBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    acctMenu?.hidden ? openAcctMenu() : closeAcctMenu();
  });
  document.addEventListener("click", (e) => {
    // Keep the menu open while interacting with its Language flyout (a sibling element).
    if (acctMenu && !acctMenu.hidden && !acctMenu.contains(e.target)
        && !(langMenu && langMenu.contains(e.target))) closeAcctMenu();
  });
  // Direct open: the profile nav-item was removed on purpose (one door policy), so the
  // account menu and Auto-apply's "Add the details" both come through here.
  function openProfilePage() {
    loadProfile();
    showView("profile");
    // No nav item owns this page, so none stays lit (the last page's item did).
    document.querySelectorAll(".nav-item").forEach(x => x.classList.remove("is-active"));
  }
  $("#menuProfile")?.addEventListener("click", () => { closeAcctMenu(); openProfilePage(); });

  // ---- Language picker (opens from the account menu; uses the client-side i18n layer) ----
  const langMenu = $("#langMenu"), langList = $("#langList");
  function renderLangList() {
    if (!langList || !window.I18N) return;
    const cur = I18N.getLang();
    langList.innerHTML = I18N.langs.map(l =>
      `<button class="acct-menu-item lang-opt" data-lang="${l.code}" role="menuitemradio" aria-checked="${l.code === cur}" type="button">
         <span class="lang-check">${l.code === cur ? "&#10003;" : ""}</span>${esc(l.name)}
       </button>`).join("");
    langList.querySelectorAll("[data-lang]").forEach(b => b.addEventListener("click", async () => {
      await I18N.setLang(b.dataset.lang);
      renderLangList();
      closeAcctMenu();               // selecting a language closes the whole menu, like Claude
    }));
  }
  // Claude-style side flyout: pops out to the RIGHT of the account menu, aligned with the
  // "Language" row, and the account menu stays open behind it.
  function openLangMenu() {
    const item = $("#menuLanguage");
    if (!langMenu || !acctMenu || acctMenu.hidden || !item) return;
    renderLangList();
    langMenu.hidden = false;
    langMenu.style.bottom = "auto";
    const am = acctMenu.getBoundingClientRect(), li = item.getBoundingClientRect();
    langMenu.style.left = (am.right + 4) + "px";
    langMenu.style.top = li.top + "px";
    const lw = langMenu.offsetWidth, lh = langMenu.offsetHeight;
    // flip to the left of the account menu if there isn't room on the right
    if (am.right + 4 + lw > window.innerWidth - 8) langMenu.style.left = Math.max(8, am.left - lw - 4) + "px";
    // lift up if it would run past the bottom edge
    if (li.top + lh > window.innerHeight - 8) langMenu.style.top = Math.max(8, window.innerHeight - 8 - lh) + "px";
    item.classList.add("is-open");
  }
  function closeLangMenu() { if (langMenu) langMenu.hidden = true; $("#menuLanguage")?.classList.remove("is-open"); }
  $("#menuLanguage")?.addEventListener("click", (e) => {
    e.stopPropagation();
    (langMenu && !langMenu.hidden) ? closeLangMenu() : openLangMenu();
  });
  // Hover opens it (Claude-like); hovering any OTHER account item closes it again.
  $("#menuLanguage")?.addEventListener("mouseenter", openLangMenu);
  acctMenu?.querySelectorAll(".acct-menu-item").forEach(it => {
    if (it.id !== "menuLanguage") it.addEventListener("mouseenter", closeLangMenu);
  });

  // Upgrade: the passes (Job Hunt Pass 30 days, Season Pass 90 days). One payment each, nothing
  // auto-renews. Each "Get the pass" button asks our server for a one-time Stripe Checkout and
  // opens it. During a pass the panel shows what is left and the extra-interview packs.
  const upgModal = $("#upgModal");
  const PASS_NAMES = { pass30: () => T("upg.pass30Name", "Job Hunt Pass"), pass90: () => T("upg.pass90Name", "Season Pass") };
  function fmtDate(sec) {
    try { return new Date(Number(sec) * 1000).toLocaleDateString(I18N.getLang ? I18N.getLang() : undefined, { month: "short", day: "numeric" }); }
    catch { return ""; }
  }
  async function loadOffers() {
    let o = null;
    try { o = await api("/api/billing/offers"); } catch { return; }
    (o.passes || []).forEach(p => { const el = $("#upgPrice_" + p.id); if (el && p.price_label) el.textContent = p.price_label; });
    const cur = o.current || {}, curEl = $("#upgCurrent");
    const onPass = !!(cur.tier && PASS_NAMES[cur.tier] && cur.pass_until);
    if (curEl) {
      curEl.hidden = !onPass;
      if (onPass) curEl.textContent = T("upg.current", "{pass} until {date}: {i} live interviews and {p} tailored packages left. Buying another adds to it.")
        .replace("{pass}", PASS_NAMES[cur.tier]()).replace("{date}", fmtDate(cur.pass_until))
        .replace("{i}", String(cur.interviews_left ?? 0)).replace("{p}", String(cur.packages_left ?? 0));
    }
    const packs = onPass ? (o.packs || []) : [];
    const box = $("#upgPacks"), btns = $("#upgPackBtns");
    if (box) box.hidden = !packs.length;
    if (btns) {
      btns.innerHTML = packs.map(p => `<button class="btn btn-ghost btn-sm upg-pack" data-pack="${esc(p.id)}" type="button">${esc(r2PackLabel(p))}</button>`).join("");
      btns.querySelectorAll(".upg-pack").forEach(b => b.addEventListener("click", () => buyPack(b.dataset.pack, b)));
    }
  }
  async function buyPack(pack, btn) {
    if (!ACCT_EMAIL) { closeUpgrade(); openSignin({ kind: "pack", id: pack }); return; }   // sign in first
    if (btn) btn.disabled = true;
    try {
      const d = await api("/api/interview/round2/buy", { pack });
      if (window.tailorShell) { window.tailorShell.openBrowser(d.url); watchCheckout(); }
      else window.location.href = d.url;
    } catch (e) {
      if (e.reason === "sign_in_required") { closeUpgrade(); openSignin({ kind: "pack", id: pack }); }
      else if (!e.reason) toast(e.message || T("r2.cantBuy", "Couldn't start checkout."));
    }
    if (btn) btn.disabled = false;
  }
  function openUpgrade() {
    closeAcctMenu();
    if (upgModal) upgModal.hidden = false;
    loadOffers();
    paintPlan();
  }
  function closeUpgrade() { if (upgModal) upgModal.hidden = true; }
  $("#menuUpgrade")?.addEventListener("click", openUpgrade);
  // "Get an API key" opens the connect-AI picker in setup mode, so you choose Claude or
  // OpenAI first; the "get your key" step then links to that provider's console.
  $("#menuGetKey")?.addEventListener("click", () => { closeAcctMenu(); openAiModal(true); });
  $("#upgClose")?.addEventListener("click", closeUpgrade);
  upgModal?.addEventListener("click", (e) => { if (e.target === upgModal) closeUpgrade(); });
  // Free stays unlimited on the person's own AI key.
  $("#upgOwnKey")?.addEventListener("click", () => { closeUpgrade(); openAiModal(true); });

  function toast(msg) {
    let t = $("#toast");
    if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; document.body.appendChild(t); }
    t.textContent = msg; t.classList.add("show");
    clearTimeout(t._timer); t._timer = setTimeout(() => t.classList.remove("show"), 2600);
  }

  // Buying, the production way (claude.ai, ChatGPT, Notion, Linear): signing in is its OWN screen,
  // never a form inside the plans panel. "Get the pass" signed in -> straight to Stripe Checkout,
  // where the broker prefills the email. Signed out -> the plans panel closes, the sign-in screen
  // opens with "Sign in to get your Job Hunt Pass", and a successful sign-in carries on to Stripe
  // for that same pass. A 401 sign_in_required from checkout routes to the same screen.
  let subscribing = false;
  let pendingBuy = null;                          // {kind: "pass"|"pack", id} waiting on a sign-in
  const PENDING_KEY = "sj-pending-buy";           // survives the web build's same-tab Google trip
  function rememberBuy(b) {
    pendingBuy = b;
    try { b ? sessionStorage.setItem(PENDING_KEY, JSON.stringify(b)) : sessionStorage.removeItem(PENDING_KEY); } catch {}
  }
  try { pendingBuy = JSON.parse(sessionStorage.getItem(PENDING_KEY) || "null"); } catch { pendingBuy = null; }

  // Close the Stripe pane by itself once the payment lands (Kofi, 2026-10-11). The newer shell
  // also notices Stripe's return address, but installed copies may predate that, so the page
  // watches the account instead: when the pass end date or the interview balance grows, the
  // purchase went through. Gives up quietly after 15 minutes.
  let CHECKOUT_WATCH = null;
  async function accountSnapshot() {
    try {
      const o = await api("/api/billing/offers");
      const c = (o && o.current) || {};
      return { until: Number(c.pass_until || 0), left: Number(c.interviews_left || 0) };
    } catch { return null; }
  }
  async function watchCheckout() {
    if (CHECKOUT_WATCH) clearInterval(CHECKOUT_WATCH);
    const before = await accountSnapshot();
    if (!before) return;
    const t0 = Date.now();
    CHECKOUT_WATCH = setInterval(async () => {
      if (Date.now() - t0 > 15 * 60 * 1000) { clearInterval(CHECKOUT_WATCH); CHECKOUT_WATCH = null; return; }
      const now = await accountSnapshot();
      if (!now || (now.until <= before.until && now.left <= before.left)) return;
      clearInterval(CHECKOUT_WATCH); CHECKOUT_WATCH = null;
      if (window.tailorShell && window.tailorShell.closeBrowser) window.tailorShell.closeBrowser();
      handleCheckoutResult("success");
    }, 3000);
  }

  function openCheckoutUrl(url) {
    // Desktop: Stripe opens in the person's own browser (the shell routes checkout.stripe.com
    // out), so close the Passes window and say where to finish; watchCheckout() notices the pass
    // and confirms it here. Web build: the same tab; Stripe returns to ?checkout=.
    if (window.tailorShell) {
      window.tailorShell.openBrowser(url);
      closeUpgrade();
      toast(T("upg.finishInBrowser", "Finish paying in your browser. Your pass shows up here on its own."));
    } else window.location.href = url;
  }
  async function doCheckout(passId) {
    try {
      const r = await api("/api/billing/checkout", { pass: passId });
      if (r && r.url) { openCheckoutUrl(r.url); watchCheckout(); return true; }
      toast(T("upg.cantStart", "Couldn't start checkout. Please try again."));
    } catch (e) {
      if (e.reason === "sign_in_required") { closeUpgrade(); openSignin({ kind: "pass", id: passId }); return; }
      if (!e.reason) toast(e.message || T("upg.cantStart", "Couldn't start checkout. Please try again."));
    }
  }
  async function subscribe(passId, btn) {
    if (subscribing) return;
    if (!ACCT_EMAIL) {                            // signed out: sign in first, then Stripe
      closeUpgrade();
      openSignin({ kind: "pass", id: passId });
      return;
    }
    subscribing = true;
    if (btn) { btn.disabled = true; btn.textContent = T("upg.starting", "Starting..."); }
    try { await doCheckout(passId); }
    finally {
      subscribing = false;
      if (btn) { btn.disabled = false; btn.textContent = T("upg.get", "Get the pass"); }
    }
  }
  document.querySelectorAll(".upg-plan .upg-cta[data-pass]").forEach((b) =>
    b.addEventListener("click", () => subscribe(b.dataset.pass, b)));

  // ---- The sign-in screen ----
  const signinEl = $("#signinScreen");
  const SI_STEPS = ["signinStart", "signinCode", "signinGoogleWait"];
  let siEmail = "", siResendAt = 0, siTimer = null, siPoll = null, siBusy = false;
  function siStep(id) {
    SI_STEPS.forEach(s => { const el = $("#" + s); if (el) el.hidden = s !== id; });
    if (id !== "signinGoogleWait") stopGooglePoll();
  }
  function siErr(id, msg) { const el = $("#" + id); if (!el) return; el.textContent = msg || ""; el.hidden = !msg; }
  function buyLine(b) {
    if (!b) return "";
    if (b.kind === "pack") return T("signin.whyPack", "Sign in to buy extra live interviews");
    const name = PASS_NAMES[b.id] ? PASS_NAMES[b.id]() : T("upg.pass30Name", "Job Hunt Pass");
    return T("signin.whyPass", "Sign in to get your {pass}").replace("{pass}", name);
  }
  function openSignin(buy) {
    closeAcctMenu();
    rememberBuy(buy || null);
    const why = $("#signinWhy");
    if (why) { why.textContent = buyLine(buy); why.hidden = !buy; }
    siErr("signinErr", ""); siErr("signinCodeErr", "");
    siStep("signinStart");
    if (signinEl) signinEl.hidden = false;
    setTimeout(() => $("#signinEmail")?.focus(), 30);
  }
  function closeSignin() {
    if (signinEl) signinEl.hidden = true;
    stopGooglePoll(); clearInterval(siTimer);
    rememberBuy(null);
  }
  $("#signinClose")?.addEventListener("click", closeSignin);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && signinEl && !signinEl.hidden) closeSignin(); });
  $("#menuSignin")?.addEventListener("click", () => openSignin(null));

  // Step 1: email -> a 6-digit code.
  async function sendCode() {
    const r = await fetch("/api/account/email/start", { method: "POST", headers: { "Content-Type": "application/json" },
                                                        body: JSON.stringify({ email: siEmail }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) throw new Error(d.error || T("signin.sendFailed", "Couldn't send the code. Try again."));
    siResendAt = Date.now() + 30000;
    tickResend();
  }
  function tickResend() {
    const b = $("#signinResend"); if (!b) return;
    clearInterval(siTimer);
    const paint = () => {
      const left = Math.ceil((siResendAt - Date.now()) / 1000);
      if (left > 0) { b.disabled = true; b.textContent = T("signin.resendIn", "Resend code in {s} s").replace("{s}", String(left)); }
      else { b.disabled = false; b.textContent = T("signin.resend", "Resend code"); clearInterval(siTimer); }
    };
    paint(); siTimer = setInterval(paint, 1000);
  }
  $("#signinEmailForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = ($("#signinEmail")?.value || "").trim();
    siErr("signinErr", "");
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { siErr("signinErr", T("signin.badEmail", "Enter a valid email address.")); return; }
    const go = $("#signinEmailGo");
    go.disabled = true; go.textContent = T("signin.sending", "Sending code...");
    siEmail = email;
    try {
      await sendCode();
      $("#signinSentTo").textContent = email;
      clearDigits(); siErr("signinCodeErr", ""); $("#signinCodeStatus").hidden = true;
      siStep("signinCode");
      digits()[0]?.focus();
    } catch (err) { siErr("signinErr", err.message); }
    finally { go.disabled = false; go.textContent = T("signin.continue", "Continue with email"); }
  });
  $("#signinResend")?.addEventListener("click", async () => {
    siErr("signinCodeErr", "");
    try { await sendCode(); const s = $("#signinCodeStatus"); s.textContent = T("signin.resent", "New code sent."); s.hidden = false; clearDigits(); digits()[0]?.focus(); }
    catch (err) { siErr("signinCodeErr", err.message); }
  });
  $("#signinBack")?.addEventListener("click", () => { clearInterval(siTimer); siStep("signinStart"); $("#signinEmail")?.focus(); });

  // Step 2: the 6 boxes. Typing advances, Backspace goes back, a paste fills them all, and the
  // sixth digit submits on its own.
  const digits = () => Array.from(document.querySelectorAll("#signinOtp .signin-digit"));
  function clearDigits() { digits().forEach(d => { d.value = ""; }); $("#signinOtp")?.classList.remove("is-bad"); }
  function fillFrom(i, text) {
    const ds = digits(), chars = String(text).replace(/\D/g, "").split("");
    for (let k = 0; k < chars.length && i + k < ds.length; k++) ds[i + k].value = chars[k];
    const next = Math.min(i + chars.length, ds.length - 1);
    ds[next].focus();
    maybeVerify();
  }
  digits().forEach((d, i) => {
    d.addEventListener("input", () => {
      $("#signinOtp")?.classList.remove("is-bad"); siErr("signinCodeErr", "");
      const v = d.value.replace(/\D/g, "");
      if (v.length > 1) { d.value = ""; fillFrom(i, v); return; }   // autofill / fast typing
      d.value = v;
      if (v && i < 5) digits()[i + 1].focus();
      maybeVerify();
    });
    d.addEventListener("keydown", (e) => {
      if (e.key === "Backspace" && !d.value && i > 0) { const p = digits()[i - 1]; p.value = ""; p.focus(); e.preventDefault(); }
      else if (e.key === "ArrowLeft" && i > 0) { digits()[i - 1].focus(); e.preventDefault(); }
      else if (e.key === "ArrowRight" && i < 5) { digits()[i + 1].focus(); e.preventDefault(); }
    });
    d.addEventListener("paste", (e) => {
      const t = (e.clipboardData || window.clipboardData)?.getData("text") || "";
      if (!/\d/.test(t)) return;
      e.preventDefault(); fillFrom(i, t);
    });
    d.addEventListener("focus", () => d.select());
  });
  async function maybeVerify() {
    const code = digits().map(d => d.value).join("");
    if (code.length !== 6 || siBusy) return;
    siBusy = true;
    const otp = $("#signinOtp"), st = $("#signinCodeStatus");
    otp?.classList.add("is-busy"); digits().forEach(d => d.disabled = true);
    st.textContent = T("signin.verifying", "Checking the code..."); st.hidden = false;
    try {
      const r = await fetch("/api/account/email/verify", { method: "POST", headers: { "Content-Type": "application/json" },
                                                          body: JSON.stringify({ email: siEmail, code }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.error) {
        st.hidden = true;
        otp?.classList.add("is-bad");
        siErr("signinCodeErr", d.error_code === "expired" ? T("signin.expired", "That code has expired. Send a new one.")
                              : d.error_code === "bad_code" ? T("signin.badCode", "That code isn't right. Check the email and try again.")
                              : (d.error || T("signin.failed", "Couldn't sign you in. Try again.")));
        digits().forEach(x => x.disabled = false);
        clearDigits(); otp?.classList.add("is-bad"); digits()[0]?.focus();
        return;
      }
      await signedIn();
    } catch (err) {
      st.hidden = true; siErr("signinCodeErr", T("signin.failed", "Couldn't sign you in. Try again."));
      digits().forEach(x => x.disabled = false);
    } finally {
      siBusy = false; otp?.classList.remove("is-busy"); digits().forEach(x => x.disabled = false);
    }
  }

  // Signed in (by code or Google): refresh who we are, then carry on with what they came for.
  async function signedIn() {
    const buy = pendingBuy;
    if (signinEl) signinEl.hidden = true;
    stopGooglePoll(); clearInterval(siTimer);
    rememberBuy(null);
    await loadAccount();
    if (buy && buy.kind === "pass") { toast(T("signin.toStripe", "Signed in. Opening checkout...")); await doCheckout(buy.id); }
    else if (buy && buy.kind === "pack") { toast(T("signin.toStripe", "Signed in. Opening checkout...")); await buyPack(buy.id); }
    else toast(T("signin.done", "Signed in as {email}").replace("{email}", ACCT_EMAIL || ""));
  }
  async function adoptToken(token) {
    try { await api("/api/account/adopt", { token }); await signedIn(); }
    catch (e) { openSignin(pendingBuy); siErr("signinErr", e.message || T("signin.googleFailed", "Google sign-in didn't finish. Please try again.")); }
  }

  // Continue with Google. Desktop: the person's OWN browser (Google refuses sign-in inside
  // embedded browsers; RFC 8252), which lands on this engine's callback page and adopts the token;
  // this screen notices by polling the account. Web build: the same tab, back to ?signin=ok.
  function stopGooglePoll() { clearInterval(siPoll); siPoll = null; }
  $("#signinGoogle")?.addEventListener("click", async () => {
    siErr("signinErr", "");
    // Google opens in the person's own browser, which cannot carry this device's token, so a new
    // Google email would start a NEW account. A pass bought on this device before it had an email
    // would then be left behind: steer that case to the email code, which keeps it.
    if (!ACCT_EMAIL && ACCT_PLAN && PASS_NAMES[ACCT_PLAN]) {
      siErr("signinErr", T("signin.keepPass", "This device has a pass that isn't linked to an email yet. Sign in with your email so the pass stays with you."));
      $("#signinEmail")?.focus();
      return;
    }
    try {
      if (window.tailorShell) {
        await api("/api/account/google/open", {});
        siStep("signinGoogleWait");
        stopGooglePoll();
        const started = Date.now();
        siPoll = setInterval(async () => {
          if (Date.now() - started > 10 * 60 * 1000) { stopGooglePoll(); return; }
          try {
            const s = await fetch("/api/account/status").then(r => r.json());
            if (s && s.email) { stopGooglePoll(); await signedIn(); }
          } catch { /* keep waiting */ }
        }, 2000);
      } else {
        const r = await api("/api/account/google/url?via=tab");
        window.location.href = r.url;
      }
    } catch (e) { siErr("signinErr", e.message || T("signin.googleFailed", "Google sign-in didn't finish. Please try again.")); }
  });
  $("#signinGoogleBack")?.addEventListener("click", () => siStep("signinStart"));
  // An older path: Google finished inside the side pane and the shell hands us the token.
  // The broker returns #error=cancelled (the person backed out at Google) or #error=signin_failed.
  function googleErrorText(why) {
    return why === "cancelled" ? T("signin.googleCancelled", "Google sign-in was cancelled. Nothing changed.")
                               : T("signin.googleFailed", "Google sign-in didn't finish. Please try again.");
  }
  window.tailorShell?.onSigninReturn?.((data) => {
    if (data && data.token) adoptToken(data.token);
    else { openSignin(pendingBuy); siErr("signinErr", googleErrorText(data && data.error)); }
  });

  // Sign out: this device forgets the email account and goes back to a fresh free account.
  $("#menuSignout")?.addEventListener("click", async () => {
    closeAcctMenu();
    try { await api("/api/account/signout", {}); toast(T("signin.signedOut", "Signed out.")); }
    catch (e) { if (!e.reason) toast(e.message || T("signin.signoutFailed", "Couldn't sign out. Try again.")); }
    await loadAccount();
  });

  // Who is signed in, and on which plan: the account foot shows the email (or the pass name
  // under it), the menu shows Sign in or the email + Sign out. Refreshed on load, after sign-in
  // and after checkout.
  // Where the plan shows (Kofi, 2026-10-11; the claude.ai / ChatGPT pattern): a mark on the
  // avatar, the plan and what is left in the account menu, the menu item becomes "Your pass",
  // and the plans panel marks the current one.
  let ACCT_STATUS = null;
  function paintPlan() {
    const s = ACCT_STATUS || {};
    const onPass = !!(s.plan && PASS_NAMES[s.plan] && s.pass_until);
    const name = onPass ? PASS_NAMES[s.plan]() : T("upg.freeName", "Free");
    const until = onPass ? fmtDate(s.pass_until) : "";
    const el = $("#acctPlan"); if (el) el.textContent = name;
    const dot = $("#acctPassDot"); if (dot) dot.hidden = !onPass;
    // The live round is paid: a quiet "Pass" on the Live tile and the Full mock (which includes
    // it) for anyone without one, gone once they have a pass.
    document.querySelectorAll("[data-pass-tag]").forEach(t => { t.hidden = onPass; });
    const btn = $("#acctBtn"); if (btn) btn.title = onPass ? `${name} until ${until}` : T("upg.freePlan", "Free plan");
    const box = $("#acctMenuPlan");
    if (box) {
      box.hidden = false;
      box.classList.toggle("is-pass", onPass);
      $("#acctMenuPlanName").textContent = onPass
        ? T("acct.planUntil", "{plan} until {date}").replace("{plan}", name).replace("{date}", until)
        : T("upg.freePlan", "Free plan");
      // Unknown (no account yet, or the account service can't be reached) says nothing rather
      // than "0 left", which read as used up on a brand-new install (QA 2026-10-14).
      const left = $("#acctMenuPlanLeft");
      const known = onPass || s.packages_left != null;
      left.hidden = !known;
      left.textContent = !known ? ""
        : onPass
        ? T("acct.planLeft", "{i} live interviews and {p} tailored packages left").replace("{i}", String(s.interviews_left ?? 0)).replace("{p}", String(s.packages_left ?? 0))
        : T("acct.freeLeft", "{p} tailored packages left this month").replace("{p}", String(s.packages_left));
    }
    const lbl = $("#menuUpgradeLbl"); if (lbl) lbl.textContent = onPass ? T("acct.yourPass", "Your pass") : T("acct.passes", "Get a pass");
    document.querySelectorAll(".upg-plan").forEach(card => {
      const b = card.querySelector(".upg-cta[data-pass]"); if (!b) return;
      const current = onPass && b.dataset.pass === s.plan;
      card.classList.toggle("is-current-plan", current);
      b.textContent = current ? T("upg.addDays", "Add 30 more days").replace("30", s.plan === "pass90" ? "90" : "30") : T("upg.get", "Get the pass");
    });
  }
  async function loadAccount() {
    try {
      const s = await api("/api/account/status");
      ACCT_STATUS = s || null;
      ACCT_EMAIL = (s && s.email) || null;
      ACCT_PLAN = (s && s.plan) || null;
    } catch (e) { /* leave the current labels */ }
    paintIdentity();
    paintPlan();
  }
  function showWelcomePass() {
    const s = ACCT_STATUS || {};
    if (!(s.plan && PASS_NAMES[s.plan])) return;
    const m = $("#welcomePass"); if (!m) return;
    $("#welcomePassBadge").textContent = PASS_NAMES[s.plan]();
    $("#welcomePassH").textContent = T("upg.welcomeH", "You're on the {plan}").replace("{plan}", PASS_NAMES[s.plan]());
    $("#welcomePassSub").textContent = T("upg.welcomeSub", "Active until {date}. It ends on its own, there is nothing to cancel.").replace("{date}", fmtDate(s.pass_until));
    $("#welcomePassList").innerHTML = [
      T("upg.welcome1", "{i} live interviews with a real-time interviewer").replace("{i}", String(s.interviews_left ?? 0)),
      T("upg.welcome2", "{p} tailored packages on our best AI model").replace("{p}", String(s.packages_left ?? 0)),
      T("upg.welcome3", "Everything in Free, as before"),
    ].map(x => `<li>${esc(x)}</li>`).join("");
    m.hidden = false;
  }
  $("#welcomePassGo")?.addEventListener("click", () => { $("#welcomePass").hidden = true; });
  window.tailorShell?.onReturned?.(() => loadAccount());
  const loadPlanBadge = loadAccount;
  document.addEventListener("i18n:changed", paintIdentity);
  loadAccount();

  // Returning from Stripe Checkout. Close the plans panel and confirm in THIS window (not inside
  // the pane). Two ways in: the desktop shell hands us the result after closing the pane, and the
  // web build gets it as a ?checkout= query param on the app URL.
  let checkoutHandled = false;
  async function handleCheckoutResult(status) {
    if (checkoutHandled) return;                 // guard against both paths firing
    checkoutHandled = true;
    closeUpgrade();
    if (status === "success") {
      try { await loadPlanBadge(); } catch (e) { /* best effort */ }
      showWelcomePass();
    } else if (status === "cancel") {
      toast(T("upg.canceled", "Checkout canceled. Nothing was charged."));
    }
    setTimeout(() => { checkoutHandled = false; }, 1500);
  }
  window.tailorShell?.onCheckoutReturn?.(handleCheckoutResult);   // desktop: pane -> here
  (function handleCheckoutReturn() {                              // web: ?checkout= on the URL
    const p = new URLSearchParams(location.search);
    const c = p.get("checkout");
    if (!c) return;
    handleCheckoutResult(c);
    p.delete("checkout");
    const q = p.toString();
    history.replaceState(null, "", location.pathname + (q ? "?" + q : ""));   // don't repeat on refresh
  })();

  // Web build: the Google callback page adopted the token and sent this tab back with
  // ?signin=ok (or ?signin=error). Carry on with what the person came for.
  (function handleSigninReturn() {
    const p = new URLSearchParams(location.search);
    const v = p.get("signin");
    if (!v) return;
    p.delete("signin");
    const q = p.toString();
    history.replaceState(null, "", location.pathname + (q ? "?" + q : ""));
    if (v === "ok") signedIn();
    else { openSignin(pendingBuy); siErr("signinErr", googleErrorText(v)); }
  })();

  const setModal = $("#setModal");
  async function openSettings() {
    closeAcctMenu();
    if (!setModal) return;
    setModal.hidden = false;
    applyTheme(themePref());                    // paint the segmented control
    const s = await loadApiKeyStatus();
    const st = $("#setKeyState");
    if (st) {
      st.textContent = s.configured
        ? `Connected, ${s.masked}`
        : (s.managed ? "Optional. SponsorJobs' AI is included."
                     : "Not connected. SponsorJobs needs your key to write anything.");
    }
    try {
      const cfg = await api("/api/submit/settings");
      if ($("#setAuto")) $("#setAuto").checked = !!cfg.autonomous;
      if ($("#setCap")) $("#setCap").value = (cfg.daily_cap ?? (cfg.rate || {}).cap) || 40;
    } catch { /* leave defaults */ }
    try {
      const w = await api("/api/whereami");
      if ($("#setDataDir")) $("#setDataDir").textContent = w.data_dir || "this machine";
    } catch { if ($("#setDataDir")) $("#setDataDir").textContent = "this machine"; }
    renderLockSettings();
    renderConnections();
    renderNotify();
    renderMemory();
  }
  function closeSettings() { if (setModal) setModal.hidden = true; ntStopPoll(); }

  // ---- Memory (P1): what SponsorJobs learned about you, and full local control over it ----
  async function renderMemory() {
    const list = $("#memList"); if (!list) return;
    let d;
    try { d = await api("/api/memory"); } catch { list.innerHTML = ""; return; }
    const count = $("#memCount");
    if (count) count.textContent = d.turns
      ? `${d.facts.length} fact${d.facts.length === 1 ? "" : "s"} from ${d.turns} turn${d.turns === 1 ? "" : "s"}.`
      : "Nothing remembered yet.";
    if (!d.facts.length) { list.innerHTML = ""; return; }
    list.innerHTML = d.facts.map(f => `
      <div class="mem-item" data-id="${f.id}">
        <span class="mem-type mem-${esc(f.type)}">${esc(f.type)}</span>
        <span class="mem-val">${esc(f.value || f.key)}</span>
        ${f.provenance ? `<span class="mem-prov">${esc(f.provenance)}</span>` : ""}
        <button class="mem-del" data-del title="Forget this" type="button">&times;</button>
      </div>`).join("");
    list.querySelectorAll(".mem-item").forEach(el => {
      el.querySelector("[data-del]")?.addEventListener("click", async () => {
        await fetch(`/api/memory/fact/${el.dataset.id}`, { method: "DELETE" });
        renderMemory();
      });
    });
  }
  $("#memAddBtn")?.addEventListener("click", async () => {
    const ta = $("#memAddText"), text = (ta?.value || "").trim();
    if (!text) { toast("Write something for me to remember."); return; }
    const btn = $("#memAddBtn"); btn.disabled = true; btn.textContent = "Saving…";
    try {
      await api("/api/memory/ingest", { text });
      if (ta) ta.value = "";
      toast("Remembered. I'll draw on it when it helps.");
      renderMemory();
    } catch (err) { toast(err.message || "Couldn't save that."); }
    finally { btn.disabled = false; btn.textContent = "Remember this"; }
  });
  $("#memAssessBtn")?.addEventListener("click", async () => {
    const text = ($("#memAddText")?.value || "").trim();
    const out = $("#memAssessOut"); if (!out) return;
    if (!text) { toast("Write something to check first."); return; }
    const btn = $("#memAssessBtn"); btn.disabled = true; btn.textContent = "Checking…";
    out.hidden = false; out.innerHTML = `<div class="empty">Reading it…</div>`;
    let a;
    try { a = (await api("/api/memory/assess", { text })).assessment || {}; }
    catch (err) { out.innerHTML = `<div class="empty">${esc(err.message || "Couldn't check that.")}</div>`; btn.disabled = false; btn.textContent = "Is this resume-worthy?"; return; }
    btn.disabled = false; btn.textContent = "Is this resume-worthy?";
    if (a.worthy) {
      out.innerHTML = `<div class="mem-assess-yes"><b>Yes, this belongs on your resume.</b> ${esc(a.reason || "")}
        ${a.competency ? `<span class="scr-comp">${esc(a.competency)}</span>` : ""}
        ${a.headline ? `<div class="mem-assess-head">Framed: ${esc(a.headline)}</div>` : ""}
        <button class="btn btn-primary btn-sm" id="memHighlight" type="button">Save as a highlight</button></div>`;
      $("#memHighlight")?.addEventListener("click", async () => {
        try { await api("/api/memory/ingest", { text, source: "highlight" });
          if ($("#memAddText")) $("#memAddText").value = "";
          out.hidden = true; toast("Saved as a highlight."); renderMemory();
        } catch (err) { toast(err.message || "Couldn't save."); }
      });
    } else {
      out.innerHTML = `<div class="mem-assess-no">${esc(a.reason || "Not quite resume-worthy yet, add a concrete action and a result.")}</div>`;
    }
  });
  $("#memAddFile")?.addEventListener("change", async (e) => {
    const file = e.target.files && e.target.files[0]; if (!file) return;
    const lab = document.querySelector('label[for="memAddFile"]');
    if (lab) { lab.textContent = "Reading…"; lab.style.pointerEvents = "none"; }
    try {
      const fd = new FormData(); fd.append("file", file, file.name);
      const r = await fetch("/api/memory/ingest", { method: "POST", body: fd });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || "Couldn't read that document.");
      toast(`Remembered "${file.name}". I'll draw on it when it helps.`);
      renderMemory();
    } catch (err) { toast(err.message || "Couldn't read that document."); }
    finally { if (lab) { lab.textContent = "Upload a document"; lab.style.pointerEvents = ""; } e.target.value = ""; }
  });
  $("#memExport")?.addEventListener("click", () => { window.location.href = "/api/memory/export"; });
  $("#memErase")?.addEventListener("click", async () => {
    if (!confirm("Erase everything SponsorJobs has learned about you? This clears your saved facts, the conversation history, and cannot be undone.")) return;
    try { await fetch("/api/memory/erase", { method: "POST" }); toast("Memory erased."); renderMemory(); }
    catch { toast("Couldn't erase memory."); }
  });

  // ---- Connections settings pane: Telegram (connect in-app), browser extension, Gmail ----
  function connPill(el, ok, onText, offText) {
    if (!el) return;
    el.textContent = ok ? (onText || "Connected") : (offText || "Not connected");
    el.classList.toggle("on", !!ok);
  }
  async function renderConnections() {
    try {
      const e = await fetch("/api/extension/status").then(r => r.json());
      connPill($("#extStatus"), e.connected, "Connected", "Not detected");
      if ($("#extDetail")) $("#extDetail").textContent = e.folder
        ? `Open chrome://extensions, turn on Developer mode, click "Load unpacked", and select: ${e.folder}`
        : "The extension ships beside the app; load it as an unpacked extension in your browser.";
    } catch { /* leave */ }
    try {
      const g = await fetch("/api/inbox/status").then(r => r.json());
      const alias = g.alias || "";
      const on = !!(g.configured || g.method);
      connPill($("#gmStatus"), on, alias ? alias : (g.email ? `Connected, ${g.email}` : "Connected"), "Not connected");
      if ($("#gmAlias")) { $("#gmAlias").textContent = alias ? "Stop using it" : "Get an address"; $("#gmAlias").dataset.on = alias ? "1" : ""; }
      const imapOn = g.method === "imap";
      if ($("#gmHelp")) { $("#gmHelp").textContent = imapOn ? "Disconnect your own inbox" : "Advanced: use your own inbox instead"; $("#gmHelp").dataset.on = imapOn ? "1" : ""; }
      if ($("#gmDetail")) { $("#gmDetail").textContent = alias
        ? "Applications use this address. Codes and links reach the agent; the full emails and recruiter replies land in your real inbox."
        : (imapOn ? "SponsorJobs reads only your applications' verification codes, links and recruiter replies. It never sends mail." : "");
        $("#gmDetail").hidden = !(alias || imapOn); }
    } catch { /* leave */ }
    try {
      const gh = await fetch("/api/github/status").then(r => r.json());
      connPill($("#ghStatus"), gh.configured, gh.login ? `Connected as ${gh.login}` : "Connected", "Not connected");
      if ($("#ghDisconnect")) $("#ghDisconnect").hidden = !gh.configured;
      if ($("#ghToggle")) $("#ghToggle").textContent = gh.configured ? "Manage" : "Connect";
    } catch { /* leave */ }
  }
  // The SponsorJobs address (docs/mail-alias.md): one click to take it, one to stop using it.
  $("#gmAlias")?.addEventListener("click", async () => {
    const b = $("#gmAlias"); b.disabled = true;
    try {
      if (b.dataset.on) {
        await fetch("/api/inbox/alias", { method: "DELETE" }).catch(() => {});
      } else {
        const r = await fetch("/api/inbox/alias", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        const d = await r.json().catch(() => ({}));
        if (!r.ok || d.ok === false) toast(d.error || "Could not create the address.");
      }
    } finally { b.disabled = false; }
    renderConnections();
  });
  // Email over IMAP with a Gmail app password: the advanced path for people who want their own
  // inbox read directly. Nothing leaves this machine. The link shows the form; Disconnect
  // forgets the saved password.
  $("#gmHelp")?.addEventListener("click", async (e) => {
    e.preventDefault();
    if ($("#gmHelp").dataset.on) {
      await fetch("/api/inbox/disconnect-imap", { method: "POST" }).catch(() => {});
      renderConnections(); return;
    }
    const f = $("#gmForm"); if (f) { f.hidden = !f.hidden; if (!f.hidden) $("#gmEmail")?.focus(); }
  });
  $("#gmCancel")?.addEventListener("click", () => { const f = $("#gmForm"); if (f) { f.reset(); f.hidden = true; } });
  $("#gmForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#gmMsg"), setMsg = (t, cls) => { if (msg) { msg.textContent = t; msg.className = "conn-msg " + cls; msg.hidden = false; } };
    const email = $("#gmEmail")?.value.trim() || "", pass = ($("#gmPass")?.value || "").replace(/\s+/g, "");
    if (!email || !pass) return setMsg("Enter your email and the app password.", "err");
    setMsg("Checking\u2026", "");
    const r = await fetch("/api/inbox/connect-imap", { method: "POST", headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({ email, app_password: pass }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) return setMsg(d.error || "Could not connect. Check the app password and that IMAP is on.", "err");
    setMsg("Connected.", "ok");
    $("#gmForm").reset(); $("#gmForm").hidden = true;
    renderConnections();
  });
  $("#tgCancel")?.addEventListener("click", () => { const f = $("#tgForm"); if (f) { f.reset(); f.hidden = true; } });
  $("#tgForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#tgMsg"), setMsg = (t, cls) => { if (msg) { msg.textContent = t; msg.className = "conn-msg " + cls; msg.hidden = false; } };
    const token = $("#tgToken")?.value.trim() || "", chat = $("#tgChat")?.value.trim() || "";
    if (!token || !chat) return setMsg("Enter both the bot token and your chat ID.", "err");
    const r = await fetch("/api/telegram", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token, chat_id: chat }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) return setMsg(d.error || "Couldn't connect Telegram.", "err");
    setMsg(d.bot ? `Connected to @${d.bot}.` : "Telegram connected.", "ok");
    $("#tgForm").reset(); $("#tgForm").hidden = true;
    renderNotify();
  });

  // ---- Notifications pane: Telegram (official bot via a link + QR, else your own bot in three
  // steps), job-match alerts, the away digest, daily cap and quiet hours. ----
  let NT_POLL = null, NT = { channel: "none" };
  function ntStopPoll() { if (NT_POLL) { clearInterval(NT_POLL); NT_POLL = null; } }
  function ntHours(sel, val) {
    if (!sel) return;
    if (!sel.options.length) {
      for (let h = 0; h < 24; h++) sel.add(new Option(String(h).padStart(2, "0") + ":00", String(h)));
    }
    sel.value = String(val);
  }
  async function renderNotify() {
    let d;
    try { d = await api("/api/notify/settings"); } catch { return; }
    NT = d;
    const on = d.channel !== "none";
    const label = d.channel === "official"
      ? (d.official.username ? T("notify.connectedAs", "Connected as") + " @" + d.official.username : T("notify.connected", "Connected"))
      : d.channel === "own" ? T("notify.ownBot", "Connected, your own bot") : T("notify.notConnected", "Not connected");
    connPill($("#ntStatus"), on, label, label);
    if ($("#ntPaused")) $("#ntPaused").hidden = !(d.official && d.official.paused);
    if ($("#ntConnect")) $("#ntConnect").hidden = on;
    if ($("#ntDisconnect")) $("#ntDisconnect").hidden = !on;
    if (on) { ntStopPoll(); if ($("#ntLink")) $("#ntLink").hidden = true; if ($("#tgForm")) $("#tgForm").hidden = true; }
    const p = d.prefs || {};
    if ($("#ntJobs")) $("#ntJobs").checked = !!p.job_alerts;
    if ($("#ntUpdates")) $("#ntUpdates").checked = !!p.app_updates;
    if ($("#ntContact")) $("#ntContact").checked = !!p.show_contact;
    document.querySelectorAll("#ntCap [data-cap]").forEach(b => b.classList.toggle("is-on", +b.dataset.cap === +p.daily_cap));
    ntHours($("#ntQuietStart"), p.quiet_start ?? 22);
    ntHours($("#ntQuietEnd"), p.quiet_end ?? 8);
    api("/api/submit/settings").then(s => { if ($("#ntApproveAll")) $("#ntApproveAll").checked = !!s.approve_all; }).catch(() => {});
  }
  function ntSave(patch) { api("/api/notify/settings", patch).then(renderNotify).catch(() => {}); }
  function ntShowOwnBot() {
    if ($("#ntLink")) $("#ntLink").hidden = true;
    const f = $("#tgForm");
    if (f) { f.hidden = false; if ($("#tgMsg")) $("#tgMsg").hidden = true; $("#tgToken")?.focus(); }
  }
  $("#ntConnect")?.addEventListener("click", async () => {
    let d = {};
    try { d = await api("/api/notify/telegram/link", {}); } catch { d = { ok: false, error: "unreachable" }; }
    if (!d.ok || !d.url) return ntShowOwnBot();     // official bot not available: your own bot instead
    if ($("#ntOpen")) $("#ntOpen").href = d.url;
    if ($("#ntQr")) { try { $("#ntQr").innerHTML = window.QR.svg(d.url, { px: 148 }); } catch { $("#ntQr").innerHTML = ""; } }
    if ($("#ntLink")) $("#ntLink").hidden = false;
    if ($("#ntWait")) $("#ntWait").textContent = T("notify.waiting", "Waiting for Telegram\u2026");
    ntStopPoll();
    const until = Date.now() + (Number(d.expires_in) || 600) * 1000;
    NT_POLL = setInterval(async () => {
      if (Date.now() > until) {
        ntStopPoll();
        if ($("#ntWait")) $("#ntWait").textContent = T("notify.expired", "The link expired. Connect again for a new one.");
        return;
      }
      try {
        const st = await api("/api/notify/telegram/status");
        if (st.linked) { ntStopPoll(); toast(T("notify.linked", "Telegram connected.")); renderNotify(); }
      } catch { /* keep waiting */ }
    }, 3000);
  });
  $("#ntDisconnect")?.addEventListener("click", async () => {
    ntStopPoll();
    try {
      if (NT.channel === "official") await api("/api/notify/telegram/unlink", {});
      else await fetch("/api/telegram/disconnect", { method: "POST" });
    } catch { /* re-render shows the truth */ }
    renderNotify();
  });
  $("#ntJobs")?.addEventListener("change", (e) => ntSave({ job_alerts: e.target.checked }));
  $("#ntUpdates")?.addEventListener("change", (e) => ntSave({ app_updates: e.target.checked }));
  $("#ntContact")?.addEventListener("change", (e) => ntSave({ show_contact: e.target.checked }));
  $("#ntApproveAll")?.addEventListener("change", async (e) => {   // off by default (CLAUDE.md 7)
    try { await api("/api/submit/settings", { approve_all: e.target.checked }); }
    catch { e.target.checked = !e.target.checked; }
  });
  document.querySelectorAll("#ntCap [data-cap]").forEach(b =>
    b.addEventListener("click", () => ntSave({ daily_cap: +b.dataset.cap })));
  $("#ntQuietStart")?.addEventListener("change", (e) => ntSave({ quiet_start: +e.target.value }));
  $("#ntQuietEnd")?.addEventListener("change", (e) => ntSave({ quiet_end: +e.target.value }));

  // Presence: the away digest is only for work done while you were NOT here. One quiet ping a
  // minute at most, and only on real input.
  let NT_SEEN = 0;
  function ntPresence() {
    const now = Date.now();
    if (now - NT_SEEN < 60000) return;
    NT_SEEN = now;
    fetch("/api/notify/presence", { method: "POST" }).catch(() => {});
  }
  ["pointerdown", "keydown"].forEach(ev => document.addEventListener(ev, ntPresence, { passive: true }));
  ntPresence();
  // GitHub connect (token), mirroring Telegram. Lets a verified project be published.
  $("#ghToggle")?.addEventListener("click", () => { const f = $("#ghForm"); if (f) { f.hidden = !f.hidden; if (!f.hidden) { if ($("#ghMsg")) $("#ghMsg").hidden = true; $("#ghToken")?.focus(); } } });
  $("#ghCancel")?.addEventListener("click", () => { const f = $("#ghForm"); if (f) { f.reset(); f.hidden = true; } });
  $("#ghForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#ghMsg"), setMsg = (t, cls) => { if (msg) { msg.textContent = t; msg.className = "conn-msg " + cls; msg.hidden = false; } };
    const token = $("#ghToken")?.value.trim() || "";
    if (!token) return setMsg("Paste a GitHub personal access token.", "err");
    const r = await fetch("/api/github", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) return setMsg(d.error || "Couldn't connect GitHub.", "err");
    setMsg(d.login ? `Connected as ${d.login}.` : "GitHub connected.", "ok");
    $("#ghForm").reset(); $("#ghForm").hidden = true;
    renderConnections();
    document.dispatchEvent(new CustomEvent("tailor:github"));   // the ideas toolbar and Start follow
  });
  $("#ghDisconnect")?.addEventListener("click", async () => {
    await fetch("/api/github/disconnect", { method: "POST" });
    const f = $("#ghForm"); if (f) { f.reset(); f.hidden = true; }
    renderConnections();
    document.dispatchEvent(new CustomEvent("tailor:github"));
  });
  $("#extHelp")?.addEventListener("click", () => { const d = $("#extDetail"); if (d) d.hidden = !d.hidden; });
  $("#gmHelp")?.addEventListener("click", () => { const d = $("#gmDetail"); if (d) d.hidden = !d.hidden; });
  $("#menuSettings")?.addEventListener("click", openSettings);
  // Help and support: a plain email to our support inbox, in the person's own mail app. The draft
  // carries only the app version, never profile data. The desktop shell accepts exactly this
  // address (shell-electron/links.js); a plain browser opens the mailto link itself.
  $("#menuHelp")?.addEventListener("click", async () => {
    closeAcctMenu();
    const shell = window.tailorShell;
    let version = "web";
    try { if (shell?.appVersion) version = (await shell.appVersion()) || version; } catch (e) { /* keep "web" */ }
    const url = "mailto:support@sponsorjobs.ai?subject=" + encodeURIComponent("SponsorJobs help")
      + "&body=" + encodeURIComponent("\n\nApp version: " + version);
    if (shell?.openSupportMail) shell.openSupportMail(url);
    else window.location.href = url;
  });
  $("#setClose")?.addEventListener("click", closeSettings);
  setModal?.addEventListener("click", (e) => { if (e.target === setModal) closeSettings(); });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    closeAcctMenu();
    closeLangMenu();
    if (setModal && !setModal.hidden) closeSettings();
    if (upgModal && !upgModal.hidden) closeUpgrade();
  });

  // ---------------------------------------------------------------- App Lock
  // A passcode for a shared machine. showLock() covers the app; the engine also refuses data
  // routes while locked, so this is a real gate. boot() runs once the app is unlocked.
  let LOCK_ENABLED = false, LOCK_BOOTED = false;
  function showLock() {
    const scr = $("#lockScreen"); if (!scr) return;
    LOCK_ENABLED = true;
    scr.hidden = false;
    const inp = $("#lockInput"); if (inp) { inp.value = ""; setTimeout(() => inp.focus(), 40); }
    const err = $("#lockErr"); if (err) err.hidden = true;
  }
  function hideLock() { const scr = $("#lockScreen"); if (scr) scr.hidden = true; }

  $("#lockForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const inp = $("#lockInput"), err = $("#lockErr"), btn = $("#lockUnlock");
    const pc = inp?.value || "";
    if (!pc) return;
    if (btn) btn.disabled = true;
    try {
      const r = await fetch("/api/lock/unlock", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: pc }),
      });
      if (r.ok) {
        hideLock();
        if (!LOCK_BOOTED) { LOCK_BOOTED = true; boot(); }
        else { loadDashboard(); api("/api/profile").then(paintAccount).catch(() => {}); }
      } else {
        if (err) { err.textContent = "That passcode is not correct."; err.hidden = false; }
        if (inp) { inp.value = ""; inp.focus(); }
      }
    } catch {
      if (err) { err.textContent = "Couldn't reach SponsorJobs. Try again."; err.hidden = false; }
    } finally { if (btn) btn.disabled = false; }
  });

  // "Lock" in the account menu. If no passcode is set, take them to Settings to set one.
  $("#menuLock")?.addEventListener("click", async () => {
    closeAcctMenu();
    let st = { enabled: false };
    try { st = await fetch("/api/lock/status").then(r => r.json()); } catch { /* offline */ }
    if (!st.enabled) { await openSettings(); document.querySelector('[data-set="lock"]')?.click(); return; }
    try { await fetch("/api/lock/lock", { method: "POST" }); } catch { /* ignore */ }
    showLock();
  });

  // ---- App-lock settings pane ----
  async function renderLockSettings() {
    let st; try { st = await fetch("/api/lock/status").then(r => r.json()); } catch { return; }
    LOCK_ENABLED = st.enabled;
    const state = $("#setLockState"), btn = $("#setLockBtn");
    if (state) state.textContent = st.enabled
      ? "On. SponsorJobs asks for your passcode when it starts, and your API key is encrypted at rest."
      : "Off. Anyone who opens SponsorJobs on this computer can use it and see your data.";
    if (btn) {
      btn.textContent = st.enabled ? "Change passcode" : "Set passcode";
      // Primary/filled when it's the pane's main action (off); recede to ghost once set.
      btn.classList.toggle("btn-primary", !st.enabled);
      btn.classList.toggle("btn-ghost", st.enabled);
    }
    if ($("#lockCurrent")) $("#lockCurrent").hidden = !st.enabled;
    if ($("#lockRemove")) $("#lockRemove").hidden = !st.enabled;
    if ($("#lockSave")) $("#lockSave").textContent = st.enabled ? "Change" : "Turn on";
  }
  function lockMsg(t, cls) { const m = $("#lockMsg"); if (m) { m.textContent = t; m.className = "lock-form-msg " + cls; m.hidden = false; } }
  $("#setLockBtn")?.addEventListener("click", () => {
    const f = $("#setLockForm"); if (!f) return;
    f.hidden = !f.hidden;
    if (!f.hidden) { if ($("#lockMsg")) $("#lockMsg").hidden = true; ($("#lockCurrent")?.hidden ? $("#lockNew") : $("#lockCurrent"))?.focus(); }
  });
  $("#lockCancel")?.addEventListener("click", () => { const f = $("#setLockForm"); if (f) { f.reset(); f.hidden = true; } });
  $("#setLockForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const newV = $("#lockNew")?.value || "", conf = $("#lockConfirm")?.value || "", cur = $("#lockCurrent")?.value || "";
    if (newV.length < 4) return lockMsg("Passcode must be at least 4 characters.", "err");
    if (newV !== conf) return lockMsg("The two passcodes don't match.", "err");
    const enabling = !LOCK_ENABLED;
    const url = enabling ? "/api/lock/set" : "/api/lock/change";
    const body = enabling ? { passcode: newV } : { current: cur, passcode: newV };
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) return lockMsg(d.error || "Couldn't set the passcode.", "err");
    lockMsg(enabling ? "App Lock is on." : "Passcode changed.", "ok");
    $("#setLockForm").reset(); $("#setLockForm").hidden = true;
    renderLockSettings();
  });
  $("#lockRemove")?.addEventListener("click", async () => {
    const cur = $("#lockCurrent")?.value || "";
    const r = await fetch("/api/lock/remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: cur }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) return lockMsg(d.error || "Couldn't turn off App Lock.", "err");
    lockMsg("App Lock is off.", "ok");
    $("#setLockForm").reset(); $("#setLockForm").hidden = true;
    renderLockSettings();
  });
  document.querySelectorAll("[data-set]").forEach(t => t.addEventListener("click", () => {
    document.querySelectorAll("[data-set]").forEach(x => x.classList.toggle("is-on", x === t));
    document.querySelectorAll("[data-pane]").forEach(p =>
      p.classList.toggle("is-on", p.dataset.pane === t.dataset.set));
  }));
  document.querySelectorAll("[data-theme-opt]").forEach(b =>
    b.addEventListener("click", () => setTheme(b.dataset.themeOpt)));
  $("#setKeyBtn")?.addEventListener("click", () => { closeSettings(); openAiModal(true); });
  $("#setAuto")?.addEventListener("change", (e) =>
    api("/api/submit/settings", { autonomous: e.target.checked }).catch(() => {}));
  $("#setCap")?.addEventListener("change", (e) => {
    const n = Math.max(1, Math.min(200, parseInt(e.target.value, 10) || 40));
    e.target.value = n;
    api("/api/submit/settings", { daily_cap: n }).catch(() => {});
  });
  $("#aiLater")?.addEventListener("click", closeAiModal);
  $("#aiDone")?.addEventListener("click", closeAiModal);
  $("#aiReplace")?.addEventListener("click", () => openAiModal(true));
  $("#aiSave")?.addEventListener("click", async () => {
    const key = $("#aiKeyInput").value.trim();
    if (!key) { aiFlash("Paste your key first.", false); return; }
    const btn = $("#aiSave"); btn.disabled = true; btn.textContent = "Verifying…";
    aiFlash(`Checking your key with ${AI_PROVIDERS[AI_PROVIDER_SEL].label}…`, true);
    try {
      const r = await api("/api/apikey", { key, provider: AI_PROVIDER_SEL });
      if (r.ok) { await loadApiKeyStatus(); closeAiModal(); toast("Connected. Try that again and it will work."); }
    } catch (e) { aiFlash(e.message || "Couldn't save the key.", false); }
    finally { btn.disabled = false; btn.textContent = "Connect"; }
  });
  $("#aiKeyInput")?.addEventListener("keydown", e => { if (e.key === "Enter") $("#aiSave").click(); });
  $("#aiKeyInput")?.addEventListener("paste", () => setTimeout(() => {
    const v = $("#aiKeyInput").value.trim();
    if (v.startsWith(AI_PROVIDERS[AI_PROVIDER_SEL].prefix) && !$("#aiSave").disabled) $("#aiSave").click();
  }, 0));
  window.__openAiKeyBox = () => {
    if (MANAGED_AI) { toast("SponsorJobs' AI is busy for a moment. Please try again."); return; }
    hideOutage(); openAiModal(true);
  };

  /* ---------------------------------------------------------------- auto-apply
     One page for the whole loop (tailor-ux 2026-10): run settings in the Jobs filter bar, four
     stage tiles with real counts (submit/stages.py via /api/autoapply/board), the selected tile
     as a row list, Up next (/api/autoapply/upnext: what the next run would tailor), a Today
     panel, and a detail drawer per application (/api/autoapply/item). The old Review page folded
     in here: approve (one or all), skip, open and edit the package, send the batch to Telegram,
     and fill the application, all on the same endpoints Review used. Only real data is shown;
     anything the app does not know is left out, never estimated. */
  const AA = { board: null, tile: null, next: null, open: null, item: null, back: null, runBusy: false };
  const AA_PREF_KEY = "sj_aa_focus";
  const csv = id => ($(id)?.value || "").split(",").map(s => s.trim()).filter(Boolean);
  const aaSponsor = () => $("#aaSponsorOnly")?.getAttribute("aria-pressed") === "true";
  const aaFocus = () => ({ titles: csv("#aaFocus"), locations: csv("#aaLocation"), sponsor_only: aaSponsor() });
  const aaCount = () => Number($("#aaCount")?.value) || 5;
  const fmtN = n => Number(n || 0).toLocaleString();
  const AA_TILE_NAME = { review: ["aa.tile.review", "To review"], waiting: ["aa.tile.waiting", "Waiting on you"],
    submitted: ["aa.tile.submitted", "Submitted"], needs: ["aa.tile.needs", "Needs you"] };
  const aaTileName = t => T(...(AA_TILE_NAME[t] || AA_TILE_NAME.review));
  // With the desktop agent (docs/apply-agent.md) one button applies: it opens the job, clicks
  // Apply, signs in or creates the account, fills every page and stops at Submit.
  const AA_HAS_AGENT = () => !!(window.tailorShell && window.tailorShell.agentRun);
  const AA_ACT = { review: ["aa.act.review", "Review"], fill: ["aa.act.fill", "Fill application"],
    signin: ["aa.act.signin", "Sign in and fill"], finish: ["aa.act.finish", "Open and finish"], view: ["aa.act.view", "View"] };
  const AA_AGENT_ACT = { fill: ["aa.act.apply", "Apply for me"], signin: ["aa.act.apply", "Apply for me"],
    finish: ["aa.act.finish", "Open and finish"] };
  const aaActName = a => T(...((AA_HAS_AGENT() && AA_AGENT_ACT[a]) || AA_ACT[a] || AA_ACT.view));
  const AA_REASON = { needs_signin: ["aa.reason.signin", "Needs your sign-in"],
    form_blocker: ["aa.reason.form", "A form question needs you"],
    site_error: ["aa.reason.site", "The site had an error"],
    posting_closed: ["aa.reason.closed", "The posting is closed"] };

  // Per-viewer convenience only: the focus fields come back as you left them.
  (function aaRestoreFocus() {
    let f = null;
    try { f = JSON.parse(localStorage.getItem(AA_PREF_KEY) || "null"); } catch { f = null; }
    if (!f) return;
    if ($("#aaFocus")) $("#aaFocus").value = f.titles || "";
    if ($("#aaLocation")) $("#aaLocation").value = f.locations || "";
    $("#aaSponsorOnly")?.setAttribute("aria-pressed", f.sponsor ? "true" : "false");
    if ($("#aaCount") && f.count) $("#aaCount").value = String(f.count);
  })();
  let AA_FOCUS_T = null;
  function aaSaveFocus() {
    try {
      localStorage.setItem(AA_PREF_KEY, JSON.stringify({ titles: $("#aaFocus").value, locations: $("#aaLocation").value,
        sponsor: aaSponsor(), count: aaCount() }));
    } catch { /* private window */ }
    // The engine keeps the same focus too, so the Autopilot tailors the list this page shows.
    clearTimeout(AA_FOCUS_T);
    AA_FOCUS_T = setTimeout(() => api("/api/submit/settings", { aa_focus: aaFocus() }).catch(() => {}), 800);
  }
  function aaRunLabel() {
    const b = $("#aaRun");
    if (b && !AA.runBusy) b.innerHTML = esc(T("aa.run", "Tailor & queue {n}").replace("{n}", aaCount())) + " &rarr;";
  }

  // "2h", "Yesterday", "Oct 12": when something last happened, short.
  function aaAgo(v) {
    const t = tsUTC(v) ?? (v ? new Date(String(v).slice(0, 10) + "T12:00:00").getTime() : null);
    if (t == null || isNaN(t)) return "";
    const s = (Date.now() - t) / 1000;
    if (s < 3600) return Math.max(1, Math.round(s / 60)) + "m";
    const d = new Date(t), now = new Date();
    if (d.toDateString() === now.toDateString()) return Math.round(s / 3600) + "h";
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return T("aa.yesterday", "Yesterday");
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  function aaClock(v) {
    const t = tsUTC(v);
    if (t == null) return "";
    const d = new Date(t), now = new Date();
    const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return T("aa.todayAt", "Today, {t}").replace("{t}", time);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + ", " + time;
  }

  async function loadAutoApply() {
    aaPaintAnswers();
    aaRunLabel();
    loadInboxStatus();
    let b;
    try { b = await api("/api/autoapply/board"); }
    catch (e) { $("#aaList").innerHTML = `<div class="aa-empty">${esc(e.message || T("aa.loadFail", "Couldn't load your applications."))}</div>`; return; }
    AA.board = b;
    const sel = $("#aaMinFit");
    if (sel && b.min_fit != null) {
      if (![...sel.options].some(o => Number(o.value) === b.min_fit)) sel.add(new Option(`Match ${b.min_fit}%+`, b.min_fit));
      sel.value = String(b.min_fit);
    }
    const want = AA_WANT; AA_WANT = null;
    AA.tile = want || (AA.tile && AA.board.counts[AA.tile] ? AA.tile : b.default);
    aaRender();
    aaLoadNext();
    aaLoadPrefs();
    if (AA.open != null) aaOpen(AA.open, { keep: true });   // refresh an open drawer
  }

  function aaRender() {
    const b = AA.board;
    if (!b) return;
    const c = b.counts;
    const sub = {
      review: c.review ? T("aa.sub.review", "Tailored, ready for a look") : T("aa.sub.reviewNone", "Nothing to review"),
      waiting: c.waiting ? T("aa.sub.waiting", "Approved, your click to send") : T("aa.sub.waitingNone", "Nothing waiting"),
      submitted: c.submitted ? T("aa.sub.submitted", "{w} this week, {a} sent for you").replace("{w}", b.week.sent).replace("{a}", b.week.for_you)
        : T("aa.sub.submittedNone", "Nothing sent yet"),
      needs: c.needs ? `<span class="aa-flag"></span>${esc(b.needs_summary)}` : esc(T("aa.sub.needsNone", "All clear")),
    };
    document.querySelectorAll("[data-aa-tile]").forEach(el => {
      const t = el.dataset.aaTile, on = t === AA.tile;
      el.classList.toggle("is-selected", on);
      el.setAttribute("aria-pressed", String(on));
      el.querySelector("[data-aa-num]").textContent = fmtN(c[t]);
      const s = el.querySelector("[data-aa-sub]");
      if (t === "needs") s.innerHTML = sub.needs; else s.textContent = sub[t];
    });
    aaRenderList();
    aaRenderToday();
  }

  function aaStageLine(it) {
    if (it.tile === "review") {
      return (b => b.missing_contact.length
        ? ["flag", T("aa.st.contact", "Add your {f} in Profile").replace("{f}", b.missing_contact.join(" and "))]
        : ["", T("aa.st.review", "Ready for a look")])(AA.board);
    }
    if (it.tile === "waiting") return ["ready", it.queued ? T("aa.st.queued", "Queued to send for you") : T("aa.st.waiting", "Approved, ready to fill")];
    if (it.tile === "needs") return ["flag", T(...(AA_REASON[it.reason] || AA_REASON.site_error))];
    const when = aaAgo(it.submitted_at);
    return ["", (it.how === "auto" ? T("aa.st.forYou", "Sent for you") : T("aa.st.byYou", "You sent it")) + (when ? " · " + when : "")];
  }

  // One sponsorship chip per row, like the mock and the Jobs card's lead chip: the employer's
  // first visa record, else what the ad says, else the honest "No sponsorship record". The
  // drawer shows every chip.
  function aaChip(it) {
    const v = it.visa || [];
    if (v.length) {
      const x = v[0], name = x.code === "H-1B" ? T("aa.h1b", "H-1B sponsor") : badgeName(x.code);
      return `<span class="vbadge vb-spon vb-${esc(x.code)}" title="${esc(visaTitle(x))}">${esc(name)}</span>`;
    }
    return visaBadges(it, { primary: true });
  }

  function aaRow(it) {
    const [cls, text] = aaStageLine(it);
    const loc = usLocation(it.location);
    const act = it.action;
    const primaryish = act === "fill" || act === "signin" || act === "finish";
    return `<div class="aa-row${AA.open === it.id ? " is-active" : ""}" data-id="${it.id}" role="button" tabindex="0" aria-label="${esc(it.role)}, ${esc(it.company)}">
      ${companyAvatar(it)}
      <div class="aa-row-main"><div class="aa-t">${esc(it.role || T("aa.role", "Role"))}</div>
        <div class="aa-s"><span class="co">${esc(it.company)}${it.location ? " · " + esc(loc) : ""}</span>${aaChip(it)}</div></div>
      <span class="aa-stage ${cls}"><span class="pip"></span><span class="aa-stage-x">${esc(text)}</span></span>
      <span class="aa-match">${it.match != null ? `${it.match}%<small>${esc(T("aa.match", "match"))}</small>` : ""}</span>
      <div class="aa-act"><button class="btn btn-ghost btn-sm${primaryish ? " aa-fillbtn" : ""}" type="button" data-act="${act}">${esc(aaActName(act))}</button></div>
    </div>`;
  }

  function aaRenderList() {
    const b = AA.board, t = AA.tile, items = b.tiles[t] || [];
    $("#aaSecTitle").textContent = aaTileName(t);
    $("#aaSecCount").textContent = items.length ? fmtN(items.length) : "";
    const all = $("#aaApproveAll"), tg = $("#aaTelegramSend");
    all.hidden = !(t === "review" && items.length > 1);
    all.textContent = T("aa.approveAll", "Approve all {n}").replace("{n}", items.length);
    tg.hidden = !(t === "review" && items.length && b.telegram);
    const list = $("#aaList");
    if (items.length) { list.classList.remove("is-empty"); list.innerHTML = items.map(aaRow).join(""); return; }
    list.classList.add("is-empty");
    if (!b.has_profile) {
      list.innerHTML = `<div class="aa-empty"><div><div class="aa-empty-t">${esc(T("aa.empty.noResume", "Add your resume to start"))}</div>
        <div class="aa-empty-s">${esc(T("aa.empty.noResumeSub", "Auto-apply tailors it to your best matches."))}</div></div>
        <button class="btn btn-ghost btn-sm" type="button" id="aaAddResume">${esc(T("aa.addResume", "Add your resume"))}</button></div>`;
      $("#aaAddResume").addEventListener("click", () => openResumeBox());
      return;
    }
    const EMPTY = {
      review: ["aa.empty.review", "Nothing to review. Tailor & queue tailors your best matches into here."],
      waiting: ["aa.empty.waiting", "Nothing waiting on you. Approve a tailored role and it moves here."],
      submitted: ["aa.empty.submitted", "Nothing sent yet."],
      needs: ["aa.empty.needs", "Nothing needs you."],
    };
    list.innerHTML = `<div class="aa-empty"><div class="aa-empty-s">${esc(T(...EMPTY[t]))}</div></div>`;
  }

  function aaRenderToday() {
    const b = AA.board, d = b.today || {};
    const cap = d.cap || 0, sent = d.count || 0;
    $("#aaCapSent").innerHTML = T("aa.capSent", "{n} sent for you of {cap}").replace("{n}", `<b>${fmtN(sent)}</b>`).replace("{cap}", fmtN(cap));
    $("#aaCapLeft").textContent = T("aa.capLeft", "{n} left today").replace("{n}", fmtN(d.remaining ?? cap));
    $("#aaMeter").style.width = cap ? Math.min(100, Math.round(sent / cap * 100)) + "%" : "0%";
    const sw = $("#aaAutonomous");
    sw.setAttribute("aria-checked", String(!!d.autonomous));
    aaRenderAutopilot(b.autopilot || {});
    $("#aaTgSub").textContent = b.telegram ? T("aa.tgOn", "Connected") : T("aa.tgOff", "Approve from your phone");
    $("#aaTgBtn").textContent = b.telegram ? T("aa.tgSettings", "Settings") : T("aa.tgConnect", "Connect");
    const rec = b.recent || [];
    $("#aaRecent").innerHTML = rec.length ? rec.map(it => `<button class="aa-rec" type="button" data-id="${it.id}">${companyAvatar(it)}
        <span class="aa-rec-m"><span class="aa-rec-t" title="${esc(it.role)}">${esc(it.role)}</span>
        <span class="aa-rec-s">${esc(it.company)} · ${esc(it.how === "auto" ? T("aa.forYou", "For you") : T("aa.byYou", "By you"))}</span></span>
        <span class="aa-rec-w">${esc(aaAgo(it.submitted_at))}</span></button>`).join("")
      : `<div class="aa-rec-none">${esc(T("aa.recentNone", "Nothing sent yet."))}</div>`;
    const all = $("#aaSeeAll");
    all.hidden = !b.counts.submitted || AA.tile === "submitted";
    all.innerHTML = esc(T("aa.seeAll", "See all {n} submitted").replace("{n}", fmtN(b.counts.submitted))) + " &rarr;";
  }

  // The Autopilot row: the switch, the per-day number typed in place (like the Tracker's weekly
  // goal), the packages left from the account (left out when unknown), or the pause reason.
  let AA_PKG_LEFT = null;
  function aaRenderAutopilot(ap) {
    const sw = $("#aaAutopilot"), sub = $("#aaApSub");
    if (!sw || !sub) return;
    sw.setAttribute("aria-checked", String(!!ap.on));
    const paused = ap.on && ap.paused && ap.paused.code;
    if (paused) {
      const one = ap.paused.code === "missing_details" && ap.paused.n === 1;   // "1 detail", not "1 details"
      sub.textContent = T("aa.apPaused." + ap.paused.code + (one ? "_one" : ""), ap.paused.text || "Paused.")
        .replace("{n}", String(ap.paused.n || ""));
      return;
    }
    const n = Math.max(1, Math.min(10, Number(ap.per_day) || 2));
    const [before, after] = T("aa.apSub", "Tailors new matches while you are away, up to {n} a day.").split("{n}");
    let html = esc(before) + `<input class="fld aa-ap-in" id="aaApPerDay" type="number" min="1" max="10" value="${n}" aria-label="${esc(T("aa.ap", "Autopilot"))}">` + esc(after || "");
    if (AA_PKG_LEFT != null) html += " " + esc(T("aa.apLeft", "{p} packages left on your pass.").replace("{p}", String(AA_PKG_LEFT)));
    sub.innerHTML = html;
    $("#aaApPerDay")?.addEventListener("change", async e => {
      const v = Math.max(1, Math.min(10, parseInt(e.target.value, 10) || n));
      e.target.value = String(v);
      try { await api("/api/submit/settings", { autopilot_per_day: v }); if (AA.board && AA.board.autopilot) AA.board.autopilot.per_day = v; }
      catch { toast(T("tracker.saveFail", "Couldn't save that, try again.")); }
    });
    if (AA_PKG_LEFT == null) {
      api("/api/account/status").then(s => {
        if (s && s.packages_left != null) { AA_PKG_LEFT = Number(s.packages_left); if (AA.board) aaRenderAutopilot(AA.board.autopilot || {}); }
      }).catch(() => {});
    }
  }
  $("#aaAutopilot")?.addEventListener("click", async e => {
    const sw = e.currentTarget, on = sw.getAttribute("aria-checked") !== "true";
    sw.setAttribute("aria-checked", String(on));
    try { await api("/api/submit/settings", { autopilot: on }); if (AA.board && AA.board.autopilot) { AA.board.autopilot.on = on; aaRenderAutopilot(AA.board.autopilot); } }
    catch { sw.setAttribute("aria-checked", String(!on)); }
  });

  function aaSelectTile(t) {
    if (!AA.board) return;
    AA.tile = t;
    aaRender();
  }
  document.querySelectorAll("[data-aa-tile]").forEach(el => el.addEventListener("click", () => aaSelectTile(el.dataset.aaTile)));
  $("#aaSeeAll")?.addEventListener("click", () => { aaSelectTile("submitted"); $("#aaList")?.scrollIntoView({ block: "nearest" }); });
  $("#aaTgBtn")?.addEventListener("click", async () => { await openSettings(); document.querySelector('[data-set="notify"]')?.click(); });
  $("#aaAutonomous")?.addEventListener("click", async e => {
    const sw = e.currentTarget, on = sw.getAttribute("aria-checked") !== "true";
    sw.setAttribute("aria-checked", String(on));
    try { await api("/api/submit/settings", { autonomous: on }); if (AA.board) AA.board.today.autonomous = on; }
    catch { sw.setAttribute("aria-checked", String(!on)); }
  });
  $("#aaRecent")?.addEventListener("click", e => { const r = e.target.closest("[data-id]"); if (r) aaOpen(Number(r.dataset.id)); });

  const aaFind = id => {
    const t = AA.board && AA.board.tiles;
    if (!t) return null;
    for (const k of Object.keys(t)) { const hit = t[k].find(x => x.id === id); if (hit) return hit; }
    return null;
  };

  // The ONE fill path on this page (row, drawer, and "Fill this page again"): the same in-app
  // fill as the package page. The shell asks the engine before it touches the site's submit, so
  // only a site on the verified auto-submit list is ever clicked, and here only with Autonomous
  // submit on; everywhere else it fills and stops for the person's click.
  async function aaFill(id, opts) {
    const it = (AA.item && AA.item.id === id) ? AA.item : aaFind(id);
    if (!it) return;
    const here = !!(opts && opts.here);
    if (!here && !/^https?:\/\//i.test(it.apply_url || "")) { aaNote(id, T("aa.noLink", "This application has no link. Open the package to add one.")); return; }
    if (!window.tailorShell) { window.open(it.apply_url, "_blank", "noopener"); return; }
    // The drawer is a modal, and the in-app browser hides under any modal: filling from the
    // drawer loaded and filled the site out of sight, so it looked like nothing happened
    // (Kofi, 10-27). Step out of the way first; the result goes on the bar above the page.
    if (AA.open === id) aaClose();
    const btn = opts && opts.btn && document.contains(opts.btn) && opts.btn.offsetParent ? opts.btn : null;
    if (btn) { btn.disabled = true; btn.dataset.label = btn.textContent; btn.textContent = T("aa.filling", "Filling…"); }
    const auto = it.lane === "auto" && !!(AA.board && AA.board.today && AA.board.today.autonomous);
    if (AA_HAS_AGENT() && !here) {
      if (btn) btn.textContent = T("aa.applying", "Applying\u2026");
      const r = await aaAgentRun(id, it, auto);
      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.label; }
      if (r && r.submitted) {
        await api(`/api/record/${id}/status`, { status: "applied" }).catch(() => {});
        loadAutoApply();
      }
      return;
    }
    const r = await applyInPane({ url: it.apply_url, recordId: id, resumePath: it.cv_path || "", submit: auto, here });
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.label; }
    aaNote(id, applyMessage(r), r && r.ok !== false && !r.submitted);
    if (r && r.submitted) {
      await api(`/api/record/${id}/status`, { status: "applied" }).catch(() => {});   // off the to-do
      loadAutoApply();
    }
  }
  // The agent applies by itself; its progress lands on the bar above the page (aaAgentBar).
  // Problems it cannot clear (captcha, a verification email) stop it and say so there.
  let AA_AGENT = null;                                  // { id } while a run is going
  async function aaAgentRun(id, it, auto) {
    if (AA_AGENT) { toast(T("aa.agentBusy", "One application at a time. Stop the current one first.")); return null; }
    AA_AGENT = { id };
    if (window.Panel && !window.Panel.isOpen("browser")) window.Panel.open("browser");
    aaAgentBar({ state: "running", message: T("aa.agent.start", "Opening the job") });
    let r = null;
    try {
      r = await window.tailorShell.agentRun({ recordId: id, url: it.apply_url, resumePath: it.cv_path || "",
                                             lane: it.lane || "assisted", autonomous: !!auto, company: it.company || "" });
    } catch { r = null; }
    AA_AGENT = null;
    console.warn("apply agent:", JSON.stringify(r));
    // A run that never started has no status line of its own; one that stopped already said why.
    if (!r) aaAgentBar({ state: "stopped", message: T("aa.agent.failed", "Could not run this one. Open it and apply by hand.") });
    else if (r.ok === false && /^(bad-url|linkedin|fill-by-hand|busy|no-page)$/.test(r.reason || "")) {
      aaAgentBar({ state: "stopped", message: r.reason === "busy"
        ? T("aa.agentBusy", "One application at a time. Stop the current one first.") : applyMessage(r) });
    }
    return r;
  }
  function aaAgentBar(info) {
    const bar = $("#bbFill");
    if (!bar) return;
    bar.innerHTML = "";
    const text = document.createElement("span");
    text.className = "bb-fill-text";
    text.textContent = info.message || "";
    bar.appendChild(text);
    const btn = (label, cls, fn) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "btn btn-sm " + cls; b.textContent = label;
      b.addEventListener("click", fn); bar.appendChild(b); return b;
    };
    if (info.state === "ask" && info.ask) {
      // The agent needs one answer: buttons for the page's options, else a box. Saved for next time.
      const ask = info.ask;
      const answer = async (value) => {
        const r = await fetch("/api/autoapply/agent/answer", { method: "POST", headers: { "Content-Type": "application/json" },
                              body: JSON.stringify({ id: ask.id || "", answer: value }) }).catch(() => null);
        if (!r || !r.ok) {
          // The engine assigns the id when it registers the question; find the open one by text.
          await fetch("/api/autoapply/agent/answer", { method: "POST", headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ question: ask.question, answer: value }) }).catch(() => null);
        }
        aaAgentBar({ state: "running", message: T("aa.agent.thanks", "Thanks. Carrying on") });
      };
      if ((ask.options || []).length) {
        for (const o of ask.options) btn(o, "btn-ghost bb-opt", () => answer(o));
      } else {
        const inp = document.createElement("input");
        inp.className = "fld bb-answer"; inp.type = "text"; inp.placeholder = T("aa.agent.answer", "Your answer");
        inp.addEventListener("keydown", (e) => { if (e.key === "Enter" && inp.value.trim()) answer(inp.value.trim()); });
        bar.appendChild(inp);
        btn(T("aa.agent.sendAnswer", "Answer"), "btn-primary bb-send", () => { if (inp.value.trim()) answer(inp.value.trim()); });
        setTimeout(() => inp.focus(), 50);
      }
    } else if (info.state === "ready" && info.record_id != null) {
      btn(T("aa.send", "Send"), "btn-primary bb-send", async (e) => {
        e.target.disabled = true;
        let r = null;
        try { r = await window.tailorShell.agentSend(info.record_id); } catch { r = null; }
        if (!r || r.ok === false) {
          e.target.disabled = false;
          aaAgentBar({ state: "ready", record_id: info.record_id,
                       message: r && r.reason === "captcha" ? T("aa.agent.captcha", "The submit hit a captcha. Solve it on the page, then press Send again.")
                              : T("aa.agent.nosend", "Could not find the Submit button. Press it on the page.") });
        } else {
          await api(`/api/record/${info.record_id}/status`, { status: "applied" }).catch(() => {});
          loadAutoApply();
        }
      });
    } else if (info.state === "running") {
      btn(T("aa.stop", "Stop"), "btn-ghost bb-stop", () => { try { window.tailorShell.agentStop(); } catch {} });
    }
    bar.classList.toggle("is-wall", info.state === "stopped");
    bar.classList.toggle("is-ready", info.state === "ready" || info.state === "ask");
    bar.hidden = false;
  }

  // Say what the fill did: in the drawer when it is open on this application; else on the bar
  // right above the page in the in-app browser, where the person is looking (a toast is gone
  // in under three seconds, too fast for "click Apply now first"); else a toast.
  function aaNote(id, msg, again) {
    if (AA.open === id && $("#aaDrNote")) {
      $("#aaDrNote").textContent = msg; $("#aaDrNote").hidden = !msg;
      const a = $("#aaDrAgain"); if (a && again && window.tailorShell) a.hidden = false;
      return;
    }
    const bar = $("#bbFill");
    if (msg && bar && window.Panel && window.Panel.isOpen("browser")) {
      bar.textContent = msg; bar.classList.remove("is-wall"); bar.hidden = false;
      return;
    }
    if (msg) toast(msg);
  }

  async function aaApprove(ids) {
    const r = await api("/api/review/approve", { approve: ids });
    const items = r.approved || [];
    const n = st => items.filter(a => a.stage === st).length;
    let msg;
    if (items.length === 1 && items[0].message) msg = items[0].message;
    else {
      const parts = [];
      if (n("submitted")) parts.push(T("aa.sum.sent", "{n} sent for you").replace("{n}", n("submitted")));
      if (n("approved")) parts.push(T("aa.sum.waiting", "{n} waiting on you").replace("{n}", n("approved")));
      if (n("failed")) parts.push(T("aa.sum.needs", "{n} need you").replace("{n}", n("failed")));
      msg = parts.length ? parts.join(", ") + "." : T("aa.sum.none", "Nothing changed.");
    }
    return msg;
  }
  async function aaSkip(id) {
    const it = (AA.item && AA.item.id === id) ? AA.item : aaFind(id);
    await api("/api/review/approve", { skip: [id] });
    if (AA.open === id) aaClose();
    loadAutoApply();
    if (it) skipWhy({ record_id: id, title: it.role, company: it.company, location: it.location, salary: it.pay });
    else toast(T("aa.skipped", "Skipped."));
  }

  $("#aaList")?.addEventListener("click", e => {
    const row = e.target.closest(".aa-row[data-id]");
    if (!row) return;
    const id = Number(row.dataset.id);
    const btn = e.target.closest("[data-act]");
    if (btn && /^(fill|signin|finish)$/.test(btn.dataset.act)) { aaFill(id, { btn }); return; }
    aaOpen(id);
  });
  $("#aaList")?.addEventListener("keydown", e => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches(".aa-row[data-id]")) { e.preventDefault(); aaOpen(Number(e.target.dataset.id)); }
  });
  $("#aaApproveAll")?.addEventListener("click", async e => {
    const ids = ((AA.board && AA.board.tiles.review) || []).map(x => x.id);
    if (!ids.length) return;
    const btn = e.currentTarget;
    btn.disabled = true;
    try { toast(await aaApprove(ids)); } catch (err) { toast(err.message || T("aa.fail", "Couldn't do that, try again.")); }
    btn.disabled = false;
    loadAutoApply();
  });
  $("#aaTelegramSend")?.addEventListener("click", async () => {
    try {
      const r = await api("/api/review/telegram/send", {});
      toast(T("aa.tgSent", "Sent {n} to your Telegram. Approve or skip them there.").replace("{n}", r.sent));
    } catch (e) { toast(e.message || T("aa.fail", "Couldn't do that, try again.")); }
  });

  /* ---- the drawer: one application, what will be sent, and where it stands ---- */
  const AA_IC = {
    doc: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h8l4 4v14H6z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M14 3v4h4M9 12h6M9 16h6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    mail: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="5.5" width="17" height="13" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M4 7l8 6 8-6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
    list: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 6h14M5 12h14M5 18h9" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    info: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M8 7.2v3.6M8 5.1v.1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  };
  function aaHint(it) {
    const site = it.site || "";
    if (it.tile === "review") {
      return it.lane === "auto"
        ? T("aa.hint.reviewAuto", "Approving sends it for you on {site}.").replace("{site}", site)
        : T("aa.hint.review", "Approving moves it to Waiting on you. Nothing is sent.");
    }
    if (it.tile === "submitted") return it.stage_detail || "";
    if (it.tile === "needs") return it.stage_detail || "";   // why it came back, and what to do
    if (!window.tailorShell) return T("aa.hint.web", "Opens the posting. Apply there with this package.");
    const auto = it.lane === "auto" && !!(AA.board && AA.board.today && AA.board.today.autonomous);
    if (auto) return T("aa.hint.auto", "Opens on {site}, fills it and sends it for you.").replace("{site}", site);
    return site ? T("aa.hint.fill", "Opens on {site} and fills it. You click Submit.").replace("{site}", site)
      : T("aa.hint.fillNoSite", "Opens the application and fills it. You click Submit.");
  }
  function aaDrawerActions(it) {
    const post = /^https?:\/\//i.test(it.posting_url || "")
      ? `<a class="btn btn-ghost" href="${esc(it.posting_url)}" target="_blank" rel="noopener">${esc(T("aa.viewPosting", "View posting"))} &#8599;</a>` : "";
    const skip = `<button class="btn btn-ghost" type="button" data-dr="skip">${esc(T("aa.skip", "Skip"))}</button>`;
    if (it.tile === "review") {
      return `<button class="btn btn-primary" type="button" data-dr="approve">${esc(T("aa.approve", "Approve"))}</button>
        <button class="btn btn-ghost" type="button" data-dr="edit">${esc(T("aa.editResume", "Edit resume"))}</button>${post}${skip}`;
    }
    if (it.tile === "waiting" || (it.tile === "needs" && it.reason !== "posting_closed")) {
      return `<button class="btn btn-primary" type="button" data-dr="fill">${esc(aaActName(it.action === "view" ? "fill" : it.action))} &rarr;</button>
        <button class="btn btn-ghost" type="button" data-dr="again" id="aaDrAgain" hidden>${esc(T("pkg.fillAgain", "Fill this page again"))}</button>${post}${skip}`;
    }
    if (it.tile === "needs") return `${post}${skip}`;
    return post;
  }
  function aaTimeline(it) {
    const li = (cls, t, m) => `<li class="${cls}"><span class="dot"></span><div class="t">${esc(t)}</div>${m ? `<div class="m">${esc(m)}</div>` : ""}</li>`;
    const out = [];
    if (it.picked) out.push(li("done", T("aa.tl.picked", "Picked from Jobs"), it.saved ? T("aa.tl.saved", "Saved job") : ""));
    out.push(li("done", T("aa.tl.tailored", "Tailored"), aaClock(it.created_at)));
    if (it.tile === "review") {
      out.push(li("now", T("aa.tl.approve", "Approve"), T("aa.tl.yourTurn", "Your turn")));
      out.push(li("later", T("aa.act.fill", "Fill application")));
      out.push(li("later", T("aa.tile.submitted", "Submitted")));
      return out.join("");
    }
    // A record marked applied straight from review was never approved: no step for it then.
    if (it.tile !== "submitted" || it.approved_at) out.push(li("done", T("aa.tl.approved", "Approved by you"), aaClock(it.approved_at)));
    if (it.tile === "waiting") {
      out.push(it.queued ? li("now", T("aa.tl.queued", "Queued to send"), T("aa.tl.forYou", "Sent for you when a slot opens"))
        : li("now", T("aa.act.fill", "Fill application"), T("aa.tl.yourTurn", "Your turn")));
      out.push(li("later", T("aa.tile.submitted", "Submitted")));
    } else if (it.tile === "needs") {
      out.push(li("now flag", T("aa.tl.back", "Came back to you"), T(...(AA_REASON[it.reason] || AA_REASON.site_error))));
      out.push(li("later", T("aa.tile.submitted", "Submitted")));
    } else {
      out.push(li("done", T("aa.tile.submitted", "Submitted"),
        (it.how === "auto" ? T("aa.forYou", "For you") : T("aa.byYou", "By you")) + (it.submitted_at ? " · " + aaClock(it.submitted_at) : "")));
    }
    return out.join("");
  }
  function aaTerms(r) {
    const shown = r.terms.slice(0, 8), more = r.terms.length - shown.length;
    return shown.map(t => `<span class="aa-term">${esc(t)}</span>`).join("")
      + (more > 0 ? `<span class="aa-term">+${more}</span>` : "")
      + r.missing.slice(0, 3).map(t => `<span class="aa-term miss" title="${esc(T("aa.termMiss", "The job asks for this; your resume does not show it"))}">${esc(t)}</span>`).join("");
  }
  function aaSent(it) {
    const r = it.resume || {}, l = it.letter || {}, scr = it.screening || [];
    const meta = [];
    if (r.pages) meta.push(T(r.pages === 1 ? "aa.page" : "aa.pages", r.pages === 1 ? "1 page" : "{n} pages").replace("{n}", r.pages));
    if (r.terms_total) meta.push(T("aa.terms", "{k} of {n} job terms").replace("{k}", `<b>${r.terms.length}</b>`).replace("{n}", r.terms_total));
    const cards = [];
    cards.push(`<div class="aa-item"><div class="aa-item-top"><span class="aa-fic">${AA_IC.doc}</span>
      <div class="aa-it-main"><div class="aa-it-t">${esc(T("aa.resume", "Tailored resume"))}</div>
      <div class="aa-it-m">${r.exists ? meta.join(" · ") : esc(T("aa.noPdf", "The PDF isn't on this computer"))}</div></div>
      <button class="aa-open" type="button" data-dr="open">${esc(T("aa.open", "Open"))}</button></div>
      ${r.terms && (r.terms.length || r.missing.length) ? `<div class="aa-terms">${aaTerms(r)}</div>` : ""}</div>`);
    const sent = it.tile === "submitted";
    const words = l.words ? T("aa.words", "{n} words").replace("{n}", l.words) : T("aa.noLetter", "Not written yet. Open it to draft one.");
    const quote = l.text ? esc(l.text.length > 420 ? l.text.slice(0, 420).replace(/\s+\S*$/, "") + "…" : l.text) : "";
    if (l.words || !sent) cards.push(`<div class="aa-item"><div class="aa-item-top"><span class="aa-fic">${AA_IC.mail}</span>
      <div class="aa-it-main"><div class="aa-it-t">${esc(T("aa.letter", "Cover letter"))}</div><div class="aa-it-m">${esc(words)}</div></div>
      <button class="aa-open" type="button" data-dr="open">${esc(T("aa.open", "Open"))}</button></div>
      ${quote ? `<p class="aa-quote">${quote}</p>` : ""}</div>`);
    if (scr.length) {
      cards.push(`<div class="aa-item"><div class="aa-item-top"><span class="aa-fic">${AA_IC.list}</span>
        <div class="aa-it-main"><div class="aa-it-t">${esc(T("aa.screening", "Screening answers"))}</div>
        <div class="aa-it-m">${esc(T(scr.length === 1 ? "aa.answer" : "aa.answers", scr.length === 1 ? "1 answer" : "{n} answers").replace("{n}", scr.length))}</div></div>
        <button class="aa-open" type="button" data-dr="answers">${esc(T("ans.title", "Your answers"))}</button>
        <button class="aa-open" type="button" data-dr="open">${esc(T("aa.edit", "Edit"))}</button></div>
        <dl class="aa-qa">${scr.slice(0, 6).map(q => `<dt>${esc(q.q)}</dt><dd>${esc(q.a)}</dd>`).join("")}</dl></div>`);
    }
    return cards.join("");
  }
  function aaDrawerHtml(it, loading) {
    const facts = [it.company, it.location ? usLocation(it.location) : "", it.type || "", it.pay || ""].filter(Boolean);
    const stagePill = { review: T("aa.tile.review", "To review"), waiting: it.queued ? T("aa.pill.queued", "Queued") : T("aa.pill.approved", "Approved"),
      submitted: T("aa.tile.submitted", "Submitted"), needs: T("aa.tile.needs", "Needs you") }[it.tile] || "";
    const lane = it.lane === "auto" ? T("aa.lane.auto", "Sent for you") : T("aa.lane.you", "Your click");
    const posted = it.posted_at ? aaAgo(it.posted_at) : "";
    const markable = it.tile === "waiting" || it.tile === "needs";
    return `<article class="aa-detail">
      <div class="fd-head">
        <div class="fd-headrow">${companyAvatar(it)}
          <div class="fd-headmain">
            <h2 class="fd-title" id="aaDrTitle">${esc(it.role || T("aa.role", "Role"))}</h2>
            <div class="fd-co">${facts.map(esc).join(" <span>&middot;</span> ")}</div>
            <div class="fd-meta">${it.match != null ? `<span class="aa-dr-match">${it.match}% <span>${esc(T("aa.match", "match"))}</span></span>` : ""}
              ${visaBadges(it)}<span class="aa-pill${it.tile === "needs" ? " flag" : ""}">${esc(stagePill)}</span></div>
          </div>
        </div>
        <div class="fd-actions">${aaDrawerActions(it)}</div>
        <p class="aa-hint">${esc(aaHint(it))}${markable ? ` <button class="link-btn aa-mark" type="button" data-dr="applied">${esc(T("aa.markApplied", "Sent it yourself? Mark as applied"))}</button>` : ""}</p>
        <p class="aa-dr-note" id="aaDrNote" hidden></p>
      </div>
      <div class="aa-dr-cols">
        <div><div class="aa-h">${esc(it.tile === "submitted" ? T("aa.wasSent", "What was sent") : T("aa.willSend", "What will be sent"))}</div>
          <div class="aa-sent">${loading ? `<div class="aa-item aa-loading">${esc(T("aa.loading", "Loading…"))}</div>` : aaSent(it)}</div></div>
        <aside class="aa-dr-side"><div class="aa-h">${esc(T("aa.progress", "Progress"))}</div>
          <ol class="aa-tl">${aaTimeline(it)}</ol>
          <div class="aa-facts">
            ${it.site ? `<div>${esc(T("aa.site", "Site"))} <b>${esc(it.site)}</b></div>` : ""}
            <div>${esc(T("aa.lane", "Lane"))} <b>${esc(lane)}</b></div>
            ${posted ? `<div>${esc(T("aa.posted", "Posted"))} <b>${esc(posted)}</b></div>` : ""}
          </div>
        </aside>
      </div>
      <div class="aa-dr-foot">${AA_IC.info}<span>${esc(T("aa.foot", "Sent for you only on verified sites. Everywhere else, you click Submit."))}</span></div>
    </article>`;
  }
  async function aaOpen(id, opts) {
    const keep = !!(opts && opts.keep);
    const row = aaFind(id);
    if (!keep) {
      if (!row) return;
      if (AA.open == null) AA.back = document.activeElement;
      AA.open = id; AA.item = null;
      $("#aaDrBody").innerHTML = aaDrawerHtml(row, true);
      $("#aaScrim").hidden = false; $("#aaDrawer").hidden = false;
      document.querySelectorAll("#aaList .aa-row").forEach(r => r.classList.toggle("is-active", Number(r.dataset.id) === id));
      $("#aaDrClose").focus();
    }
    let it;
    try { it = await api("/api/autoapply/item/" + id); }
    catch (e) { if (keep && !row) { aaClose(); } return; }
    if (AA.open !== id) return;                      // closed or moved on meanwhile
    AA.item = it;
    const note = $("#aaDrNote") && !$("#aaDrNote").hidden ? $("#aaDrNote").textContent : "";
    $("#aaDrBody").innerHTML = aaDrawerHtml(it, false);
    if (note) { $("#aaDrNote").textContent = note; $("#aaDrNote").hidden = false; }
  }
  function aaClose() {
    $("#aaScrim").hidden = true; $("#aaDrawer").hidden = true;
    AA.open = null; AA.item = null;
    document.querySelectorAll("#aaList .aa-row.is-active").forEach(r => r.classList.remove("is-active"));
    if (AA.back && document.contains(AA.back)) AA.back.focus();
    AA.back = null;
  }
  $("#aaDrClose")?.addEventListener("click", aaClose);
  $("#aaScrim")?.addEventListener("click", aaClose);
  document.addEventListener("keydown", e => {
    if (AA.open == null || $("#aaDrawer").hidden) return;
    if (e.key === "Escape") { e.preventDefault(); aaClose(); return; }
    if (e.key !== "Tab") return;
    // Keep focus inside the drawer while it is open.
    const f = [...$("#aaDrawer").querySelectorAll('button:not([disabled]):not([hidden]), a[href], [tabindex]:not([tabindex="-1"])')]
      .filter(el => el.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    else if (!$("#aaDrawer").contains(document.activeElement)) { e.preventDefault(); first.focus(); }
  });
  $("#aaDrBody")?.addEventListener("click", async e => {
    const b = e.target.closest("[data-dr]");
    if (!b || AA.open == null) return;
    const id = AA.open, act = b.dataset.dr;
    if (act === "fill") return aaFill(id, { btn: b });
    if (act === "again") return aaFill(id, { btn: b, here: true });
    if (act === "edit" || act === "open") { aaClose(); openPackage(id); return; }
    if (act === "answers") { aaClose(); openAnswers("autoapply"); return; }
    if (act === "skip") { b.disabled = true; try { await aaSkip(id); } catch (err) { b.disabled = false; toast(err.message); } return; }
    if (act === "approve") {
      b.disabled = true;
      try { const msg = await aaApprove([id]); aaNote(id, msg); await loadAutoApply(); }
      catch (err) { b.disabled = false; aaNote(id, err.message || T("aa.fail", "Couldn't do that, try again.")); }
      return;
    }
    if (act === "applied") {
      b.disabled = true;
      try { await api(`/api/record/${id}/status`, { status: "applied" }); await loadAutoApply(); }
      catch (err) { b.disabled = false; toast(err.message); }
    }
  });

  /* ---- Your answers: work authorization + the answer library (one page, a back link) ----
     The planner answers every visa / sponsorship question from the work authorization here,
     and reuses a saved answer on every later form. Nothing here is guessed or sent to a model. */
  const ANS = { data: null, country: "US", wa: null, origin: "autoapply", editing: null, adding: false };
  const ANS_CC = { US: "US", UK: "UK", CA: "Canada", AU: "Australia", EU: "EU" };
  const ANS_YN = [["Yes", "ans.yes", "Yes"], ["No", "ans.no", "No"]];
  const ANS_BACK = { autoapply: ["nav.autoapply", "Auto-apply"], profile: ["acct.myProfile", "My profile"],
                     package: ["ans.backApp", "Application"] };
  function waEntry(cc) {
    const c = (ANS.wa.countries = ANS.wa.countries || {});
    return (c[cc] = c[cc] || { status: "", authorized: "", sponsor_now: "", sponsor_future: "", auth_end: "" });
  }
  function ansStatusLabel(cc, code) {
    const c = (ANS.data.countries || []).find(x => x.code === cc);
    const st = c && c.statuses.find(x => x.code === code);
    return st ? T("ans.st." + code, st.label) : "";
  }
  async function aaPaintAnswers() {
    const sub = $("#aaAnsSub");
    if (!sub) return;
    let d;
    try { d = await api("/api/answers"); } catch { return; }
    ANS.data = d;
    const us = ((d.work_auth || {}).countries || {}).US || {};
    const bits = [];
    if (us.status) bits.push(ansStatusLabel("US", us.status));
    bits.push(T(d.total === 1 ? "ans.nSaved1" : "ans.nSaved", d.total === 1 ? "1 saved" : "{n} saved").replace("{n}", d.total || 0));
    sub.textContent = us.status || d.total ? bits.join(" · ") : T("ans.todaySub", "Work authorization and saved answers");
  }
  $("#aaAnsBtn")?.addEventListener("click", () => openAnswers("autoapply"));

  async function openAnswers(origin) {
    ANS.origin = origin || "autoapply";
    const [k, f] = ANS_BACK[ANS.origin] || ANS_BACK.autoapply;
    $("#ansBackLbl").textContent = T(k, f);
    ANS.editing = null; ANS.adding = false;
    $("#ansQ").value = "";
    showView("answers");
    window.scrollTo(0, 0);
    await ansLoad();
  }
  $("#ansBack")?.addEventListener("click", () => {
    if (ANS.origin === "profile") { loadProfile(); showView("profile"); }
    else if (ANS.origin === "package") { showView("package"); }
    else { loadAutoApply(); showView("autoapply"); }
  });
  async function ansLoad() {
    let d;
    try { d = await api("/api/answers"); }
    catch (e) { $("#ansList").innerHTML = `<div class="ans-empty">${esc(e.message)}</div>`; return; }
    ANS.data = d;
    ANS.wa = JSON.parse(JSON.stringify(d.work_auth || { countries: {}, relocate: "" }));
    // Open on the first country they have answered, US otherwise.
    const have = Object.keys(ANS.wa.countries || {});
    ANS.country = have.includes(ANS.country) ? ANS.country : (have[0] || "US");
    $("#ansEeo").checked = !!d.eeo_decline;
    $("#ansWaNote").textContent = "";
    paintWa();
    paintAnsList(d.answers || [], d.total || 0);
  }
  function paintWa() {
    const d = ANS.data, cc = ANS.country, e = waEntry(cc);
    $("#ansCountry").innerHTML = (d.countries || []).map(c =>
      `<button type="button" class="ans-seg-b${c.code === cc ? " is-on" : ""}" data-cc="${c.code}" aria-pressed="${c.code === cc}"
        title="${esc(T("ans.cc." + c.code, c.name))}">${esc(T("ans.ccShort." + c.code, ANS_CC[c.code] || c.code))}</button>`).join("");
    const c = (d.countries || []).find(x => x.code === cc) || { statuses: [] };
    $("#ansStatus").innerHTML = `<option value="">${esc(T("profile.answers.notSet", "Not set"))}</option>`
      + c.statuses.map(st => `<option value="${st.code}"${st.code === e.status ? " selected" : ""}>${esc(T("ans.st." + st.code, st.label))}</option>`).join("");
    const implied = (d.implied || []).includes(e.status);
    document.querySelectorAll("#ansWaCard .ans-yn").forEach(g => {
      const k = g.dataset.yn;
      const v = k === "relocate" ? (ANS.wa.relocate || "") : (e[k] || "");
      // A citizen or permanent resident is authorized and needs no sponsorship: shown, not asked.
      const fixed = implied && k !== "relocate";
      const shown = fixed ? (k === "authorized" ? "Yes" : "No") : v;
      g.innerHTML = ANS_YN.map(([val, key, fb]) =>
        `<button type="button" class="ans-seg-b${shown === val ? " is-on" : ""}" data-v="${val}" aria-pressed="${shown === val}"${fixed ? " disabled" : ""}>${esc(T(key, fb))}</button>`).join("");
    });
    const dated = (d.dated || []).includes(e.status);
    $("#ansEndWrap").hidden = !dated;
    $("#ansAuthEnd").value = dated ? (e.auth_end || "") : "";
  }
  $("#ansCountry")?.addEventListener("click", e => {
    const b = e.target.closest("[data-cc]");
    if (!b) return;
    ANS.country = b.dataset.cc;
    paintWa();
  });
  $("#ansStatus")?.addEventListener("change", e => {
    const ent = waEntry(ANS.country);
    ent.status = e.target.value;
    if ((ANS.data.implied || []).includes(ent.status)) {
      ent.authorized = "Yes"; ent.sponsor_now = "No"; ent.sponsor_future = "No"; ent.auth_end = "";
    }
    paintWa();
  });
  $("#ansWaCard")?.addEventListener("click", e => {
    const b = e.target.closest(".ans-yn [data-v]");
    if (!b || b.disabled) return;
    const k = b.closest(".ans-yn").dataset.yn, ent = waEntry(ANS.country);
    const cur = k === "relocate" ? ANS.wa.relocate : ent[k];
    const v = cur === b.dataset.v ? "" : b.dataset.v;          // click again to clear
    if (k === "relocate") ANS.wa.relocate = v; else ent[k] = v;
    if (k === "sponsor_now" && v === "Yes" && !ent.sponsor_future) ent.sponsor_future = "Yes";
    paintWa();
  });
  $("#ansAuthEnd")?.addEventListener("change", e => { waEntry(ANS.country).auth_end = e.target.value; });
  $("#ansWaSave")?.addEventListener("click", async () => {
    const btn = $("#ansWaSave"), note = $("#ansWaNote");
    btn.disabled = true;
    try {
      const r = await api("/api/answers/workauth", { work_auth: ANS.wa, eeo_decline: $("#ansEeo").checked });
      ANS.wa = JSON.parse(JSON.stringify(r.work_auth));
      ANS.data.work_auth = r.work_auth;
      paintWa();
      note.textContent = T("ans.waSaved", "Saved. Every form gets this answer.");
    } catch (err) { note.textContent = err.message || T("profile.answers.failed", "Couldn't save."); }
    btn.disabled = false;
    setTimeout(() => { note.textContent = ""; }, 4000);
  });

  function ansWhen(ts) {
    if (!ts) return "";
    const days = Math.floor((Date.now() / 1000 - ts) / 86400);
    return days <= 0 ? T("ans.today", "today") : days === 1 ? T("ans.yesterday", "yesterday")
      : T("ans.daysAgo", "{n} days ago").replace("{n}", days);
  }
  function ansEditor(row) {
    return `<div class="ans-row is-editing"${row ? ` data-key="${esc(row.key)}"` : ""}>
      <input class="fld ans-in-q" type="text" maxlength="300" value="${esc(row ? row.question : "")}" placeholder="${esc(T("ans.qPh", "Question, as forms ask it"))}" aria-label="${esc(T("ans.qPh", "Question, as forms ask it"))}" />
      <textarea class="fld ans-in-a" rows="3" maxlength="4000" placeholder="${esc(T("ans.aPh", "Your answer"))}" aria-label="${esc(T("ans.aPh", "Your answer"))}">${esc(row ? row.answer : "")}</textarea>
      <div class="ans-row-acts"><button class="btn btn-primary btn-sm" type="button" data-ans="save">${esc(T("ans.save", "Save"))}</button>
        <button class="btn btn-ghost btn-sm" type="button" data-ans="cancel">${esc(T("ans.cancel", "Cancel"))}</button>
        <span class="ans-note" data-ans-note></span></div></div>`;
  }
  function paintAnsList(rows, total) {
    $("#ansCount").textContent = total ? String(total) : "";
    const q = $("#ansQ").value.trim();
    const add = ANS.adding ? ansEditor(null) : "";
    if (!rows.length) {
      $("#ansList").innerHTML = add + (ANS.adding ? "" : `<div class="ans-empty">${esc(q
        ? T("ans.noMatch", "No saved answer matches that.")
        : T("ans.empty", "Nothing saved yet. Fix an answer on any application and it lands here."))}</div>`);
      return;
    }
    $("#ansList").innerHTML = add + rows.map(r => ANS.editing === r.key ? ansEditor(r) : `<div class="ans-row" data-key="${esc(r.key)}">
        <div class="ans-row-main">
          <div class="ans-q">${esc(r.question)}${r.source === "drafted" ? ` <span class="ans-tag" title="${esc(T("ans.draftedTip", "Drafted from your profile. Edit it to make it yours."))}">${esc(T("ans.drafted", "Drafted"))}</span>` : ""}</div>
          <div class="ans-a">${esc(r.answer)}</div>
          <div class="ans-meta">${esc(r.uses ? T(r.uses === 1 ? "ans.used1" : "ans.used", r.uses === 1 ? "Used once, {when}" : "Used {n} times, last {when}").replace("{n}", r.uses).replace("{when}", ansWhen(r.last_used)) : T("ans.notUsed", "Not used yet"))}</div>
        </div>
        <div class="ans-row-acts"><button class="btn btn-ghost btn-sm" type="button" data-ans="edit">${esc(T("aa.edit", "Edit"))}</button>
          <button class="btn btn-ghost btn-sm ans-del" type="button" data-ans="delete" aria-label="${esc(T("ans.delete", "Delete"))}">${esc(T("ans.delete", "Delete"))}</button></div>
      </div>`).join("");
  }
  async function ansRefresh() {
    let d;
    try { d = await api("/api/answers?q=" + encodeURIComponent($("#ansQ").value.trim())); } catch { return; }
    ANS.data.answers = d.answers; ANS.data.total = d.total;
    paintAnsList(d.answers || [], d.total || 0);
  }
  let ANS_T = 0;
  $("#ansQ")?.addEventListener("input", () => { clearTimeout(ANS_T); ANS_T = setTimeout(ansRefresh, 180); });
  $("#ansAdd")?.addEventListener("click", () => {
    ANS.adding = true; ANS.editing = null;
    paintAnsList(ANS.data.answers || [], ANS.data.total || 0);
    $("#ansList .ans-in-q")?.focus();
  });
  $("#ansList")?.addEventListener("click", async e => {
    const b = e.target.closest("[data-ans]");
    if (!b) return;
    const row = b.closest(".ans-row"), key = row && row.dataset.key, act = b.dataset.ans;
    if (act === "edit") {
      ANS.editing = key; ANS.adding = false;
      paintAnsList(ANS.data.answers || [], ANS.data.total || 0);
      $(`#ansList .is-editing .ans-in-a`)?.focus();
      return;
    }
    if (act === "cancel") { ANS.editing = null; ANS.adding = false; paintAnsList(ANS.data.answers || [], ANS.data.total || 0); return; }
    if (act === "delete") {
      b.disabled = true;
      try { await api("/api/answers/delete", { key }); toast(T("ans.deleted", "Deleted.")); } catch (err) { b.disabled = false; toast(err.message); return; }
      await ansRefresh();
      return;
    }
    if (act === "save") {
      const question = row.querySelector(".ans-in-q").value.trim(), answer = row.querySelector(".ans-in-a").value.trim();
      const note = row.querySelector("[data-ans-note]");
      if (!question || !answer) { note.textContent = T("ans.needBoth", "Add the question and your answer."); return; }
      b.disabled = true;
      try { await api("/api/answers", key ? { key, question, answer } : { question, answer }); }
      catch (err) { b.disabled = false; note.textContent = err.message; return; }
      ANS.editing = null; ANS.adding = false;
      await ansRefresh();
    }
  });

  /* ---- Up next: what the next run would tailor ---- */
  let AA_NEXT_REQ = 0, AA_NEXT_POLL = null, AA_NEXT_POLLS = 0;
  async function aaLoadNext(poll) {
    const host = $("#aaNext");
    if (!host) return;
    if (!poll) AA_NEXT_POLLS = 0;
    const my = ++AA_NEXT_REQ, f = aaFocus(), count = aaCount();
    if (AA.board && !AA.board.has_profile) { $("#aaNextSec").hidden = true; return; }
    $("#aaNextSec").hidden = false;
    if (!AA.next) host.innerHTML = `<div class="aa-empty"><div class="aa-empty-s">${esc(T("aa.nextLoading", "Finding your best matches…"))}</div></div>`;
    const q = new URLSearchParams({ count, titles: f.titles.join(","), locations: f.locations.join(","), sponsor_only: f.sponsor_only ? "1" : "" });
    let r;
    try { r = await api("/api/autoapply/upnext?" + q); }
    catch (e) { if (my === AA_NEXT_REQ) host.innerHTML = `<div class="aa-empty"><div class="aa-empty-s">${esc(e.message)}</div></div>`; return; }
    if (my !== AA_NEXT_REQ) return;   // a newer focus change is already on its way
    AA.next = r;
    // Roles whose description is still downloading are scored in the background: look again
    // shortly (a few times at most) so the list settles on the real best fits.
    clearTimeout(AA_NEXT_POLL);
    if (r.pending && AA_NEXT_POLLS < 8) { AA_NEXT_POLLS++; AA_NEXT_POLL = setTimeout(() => { if (my === AA_NEXT_REQ) aaLoadNext(true); }, 3000); }
    const jobs = r.jobs || [];
    const saved = jobs.filter(j => j.saved).length;
    $("#aaNextCount").textContent = jobs.length
      ? (saved ? T("aa.nextSaved", "{n}, saved roles first") : T("aa.nextOf", "{n} of {m} roles")).replace("{n}", jobs.length).replace("{m}", fmtN(r.matches))
      : "";
    if (!jobs.length && r.feed_loading) {
      // The job list is still on its first download: say so and look again when it lands.
      host.innerHTML = `<div class="aa-empty"><div class="aa-empty-s" data-feed-dl>${esc(feedDownloadText(null))}</div></div>`;
      watchFeedDownload(() => { AA.next = null; aaLoadNext(); });
      return;
    }
    if (!jobs.length && r.pending) {
      host.innerHTML = `<div class="aa-empty"><div class="aa-empty-s">${esc(T("aa.nextLoading", "Finding your best matches…"))}</div></div>`;
      return;
    }
    if (!jobs.length) {
      const why = !r.board && !r.matches ? T("aa.nextNoBoard", "No roles on your board yet. Open Jobs to load them.")
        // Already at the lowest match on offer: "lower the match" would point at nothing.
        : Number(r.min_fit) <= aaMinFitFloor()
        ? T("aa.nextNoneMin", "No new roles reach {p}% match. Broaden your focus to see more.").replace("{p}", r.min_fit)
        : T("aa.nextNone", "No new roles reach {p}% match. Lower the match or broaden your focus.").replace("{p}", r.min_fit);
      host.innerHTML = `<div class="aa-empty"><div class="aa-empty-s">${esc(why)}</div>
        <button class="link-btn" type="button" data-next="focus">${esc(T("aa.changeFocus", "Change focus"))}</button></div>`;
      return;
    }
    const star = '<svg width="11" height="11" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.8l1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.6l-3.8 2 .7-4.3-3.1-3 4.3-.6z" fill="currentColor"/></svg>';
    const foot = r.pending ? T("aa.nextChecking", "Checking {n} more roles for a better match…").replace("{n}", fmtN(r.pending))
      : jobs.length < count
      ? T("aa.nextShort", "Only {n} roles reach {p}% today. The next run tailors these.").replace("{n}", jobs.length).replace("{p}", r.min_fit)
      : T("aa.nextFoot", "Next run tailors these {n}.").replace("{n}", jobs.length);
    host.innerHTML = jobs.map(j => `<div class="aa-row" data-sid="${esc(j.source_id)}">
        ${companyAvatar(j)}
        <div class="aa-row-main"><div class="aa-t">${esc(j.title)}</div>
          <div class="aa-s"><span class="co">${esc(j.company)}${j.location ? " · " + esc(usLocation(j.location)) : ""}</span>${aaChip(j)}</div></div>
        <span class="aa-tag">${j.saved ? star + " " + esc(T("aa.saved", "Saved")) : j.priority ? esc(T("aa.priority", "Priority")) : ""}</span>
        <span class="aa-match">${j.fit != null ? `${j.fit}%<small>${esc(T("aa.match", "match"))}</small>` : ""}</span>
        <div class="aa-act"><button class="aa-x" type="button" data-skip="${esc(j.source_id)}" title="${esc(T("aa.skipNext", "Skip this role in the next run"))}" aria-label="${esc(T("aa.skipNext", "Skip this role in the next run"))}"><svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg></button></div>
      </div>`).join("")
      + `<div class="aa-more"><span>${esc(foot)}</span><button class="link-btn" type="button" data-next="focus">${esc(T("aa.changeFocus", "Change focus"))}</button></div>`;
  }
  $("#aaNext")?.addEventListener("click", async e => {
    if (e.target.closest('[data-next="focus"]')) { $("#aaFocus").focus(); $("#aaFocus").select(); return; }
    const x = e.target.closest("[data-skip]");
    if (!x) return;
    x.disabled = true;
    try {
      await api("/api/autoapply/skip", { source_id: x.dataset.skip });
      const j = ((AA.next && AA.next.jobs) || []).find(r => r.source_id === x.dataset.skip);
      x.closest(".aa-row")?.remove();
      aaLoadNext();
      if (j) skipWhy({ source_id: j.source_id, title: j.title, company: j.company, location: j.location, salary: j.salary });
    } catch (err) { x.disabled = false; toast(err.message); }
  });
  let AA_NEXT_T = null;
  const aaNextSoon = () => { aaSaveFocus(); clearTimeout(AA_NEXT_T); AA_NEXT_T = setTimeout(aaLoadNext, 400); };
  ["#aaFocus", "#aaLocation"].forEach(id => $(id)?.addEventListener("input", aaNextSoon));
  $("#aaSponsorOnly")?.addEventListener("click", e => {
    const b = e.currentTarget; b.setAttribute("aria-pressed", String(b.getAttribute("aria-pressed") !== "true"));
    aaSaveFocus(); aaLoadNext();
  });
  $("#aaCount")?.addEventListener("change", () => { aaSaveFocus(); aaRunLabel(); aaLoadNext(); });
  $("#aaMinFit")?.addEventListener("change", async e => {
    try { await api("/api/submit/settings", { aa_min_fit: Number(e.target.value) }); } catch { /* sent with the next run too */ }
    aaLoadNext();
  });
  document.addEventListener("click", e => { const h = $("#aaHow"); if (h && h.open && !h.contains(e.target)) h.open = false; });

  /* ---- Skip with a reason (one tap, optional) and what it teaches the picker ----
     AIApply's "decline with a reason" pattern: the skip happens at once, then a small bar asks
     why. Picking a reason is optional; it is stored locally (/api/autoapply/reason) and shapes
     the next runs (autoapply/learning.py). "Company" asks once whether to skip every role there.
     Everything learned shows under Preferences with an Undo. */
  const SW_REASONS = [["wrong_role", "aa.why.role", "Wrong role"], ["too_senior", "aa.why.senior", "Too senior"],
    ["too_junior", "aa.why.junior", "Too junior"], ["location", "aa.why.location", "Location"],
    ["pay_low", "aa.why.pay", "Pay too low"], ["company", "aa.why.company", "Company"],
    ["no_sponsorship", "aa.why.nospon", "No sponsorship"], ["other", "aa.why.other", "Other"]];
  const AA_LEVEL = { intern: ["aa.lvl.intern", "Intern"], entry: ["aa.lvl.entry", "Entry"], mid: ["aa.lvl.mid", "Mid"],
    senior: ["aa.lvl.senior", "Senior"], lead: ["aa.lvl.lead", "Lead"], manager: ["aa.lvl.manager", "Manager"] };
  const fmtPay = v => "$" + Math.round(Number(v) / 1000) + "k";
  function learnedLine(r) {
    const lvl = () => T(...(AA_LEVEL[r.value] || AA_LEVEL.mid));
    switch (r.kind) {
      case "role": return T("aa.learn.role", "Fewer roles like {v}").replace("{v}", r.value);
      case "senior": return T("aa.learn.senior", "Fewer roles above {v} level").replace("{v}", lvl());
      case "junior": return T("aa.learn.junior", "Fewer roles below {v} level").replace("{v}", lvl());
      case "location": return T("aa.learn.location", "Fewer roles in {v}").replace("{v}", usLocation(r.value));
      case "pay": return T("aa.learn.pay", "Only roles paying over {v}").replace("{v}", fmtPay(r.value));
      case "nospon": return T("aa.learn.nospon", "Leave out {v}, no sponsorship").replace("{v}", r.value);
      default: return "";
    }
  }
  const SW = { job: null, timer: null };
  function swHide() {
    const bar = $("#skipWhy");
    clearTimeout(SW.timer);
    if (bar) { bar.hidden = true; bar.innerHTML = ""; }
    SW.job = null;
  }
  function swLater(ms) {
    clearTimeout(SW.timer);
    SW.timer = setTimeout(() => {
      const bar = $("#skipWhy");
      if (bar && (bar.matches(":hover") || bar.contains(document.activeElement))) return swLater(2000);
      swHide();
    }, ms);
  }
  const SW_X = `<button class="sw-x" type="button" data-sw="close" aria-label="${esc(T("aa.why.close", "Close"))}"><svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg></button>`;
  // job: {source_id | record_id, title, company, location, salary}. Called right after a skip.
  // The lowest "Match N%+" the picker offers.
  function aaMinFitFloor() {
    const vals = [...($("#aaMinFit")?.options || [])].map(o => Number(o.value)).filter(n => n > 0);
    return vals.length ? Math.min(...vals) : 0;
  }
  function skipWhy(job) {
    const bar = $("#skipWhy");
    if (!bar || !job) return;
    SW.job = job;
    bar.innerHTML = `<span class="sw-t">${esc(T("aa.why.t", "Skipped. Why?"))}</span>
      <span class="sw-chips">${SW_REASONS.map(([k, key, fb]) => `<button class="sw-chip" type="button" data-why="${k}">${esc(T(key, fb))}</button>`).join("")}</span>${SW_X}`;
    bar.hidden = false;
    swLater(9000);
  }
  function swSay(html, ms) {
    const bar = $("#skipWhy");
    if (!bar) return;
    bar.innerHTML = `<span class="sw-t">${html}</span>${SW_X}`;
    swLater(ms || 2600);
  }
  async function swSend(reason, note) {
    const j = SW.job;
    if (!j) return;
    let r;
    try {
      r = await api("/api/autoapply/reason", { source_id: j.source_id || "", record_id: j.record_id ?? "",
        title: j.title || "", company: j.company || "", location: j.location || "", salary: j.salary || "", reason, note: note || "" });
    } catch (e) { swSay(esc(e.message || T("aa.fail", "Couldn't do that, try again."))); return; }
    aaPaintPrefs(r);
    AA.next = null; if (AA.board) aaLoadNext();
    if (r.ask_company) {
      const bar = $("#skipWhy");
      bar.innerHTML = `<span class="sw-t">${esc(T("aa.why.allAt", "Skip all roles at {c}?").replace("{c}", r.ask_company))}</span>
        <button class="btn btn-ghost btn-sm" type="button" data-sw="all">${esc(T("aa.why.skipAll", "Skip all"))}</button>
        <button class="link-btn" type="button" data-sw="one">${esc(T("aa.why.justOne", "Just this one"))}</button>${SW_X}`;
      SW.company = r.ask_company;
      swLater(12000);
      return;
    }
    const line = (r.new || []).map(learnedLine).filter(Boolean)[0];
    swSay(esc(T("aa.why.thanks", "Got it.")) + (line ? ` <span class="sw-sub">${esc(line)}</span>` : ""), line ? 4200 : 2200);
  }
  $("#skipWhy")?.addEventListener("click", async e => {
    const why = e.target.closest("[data-why]");
    if (why) {
      if (why.dataset.why === "other") {
        $("#skipWhy").innerHTML = `<span class="sw-t">${esc(T("aa.why.otherT", "Other"))}</span>
          <input class="sw-note" id="swNote" type="text" maxlength="200" autocomplete="off" placeholder="${esc(T("aa.why.notePh", "Add a note (optional)"))}" />
          <button class="btn btn-ghost btn-sm" type="button" data-sw="note">${esc(T("aa.why.save", "Save"))}</button>${SW_X}`;
        $("#swNote").focus();
        swLater(20000);
        return;
      }
      why.disabled = true;
      return swSend(why.dataset.why);
    }
    const act = e.target.closest("[data-sw]")?.dataset.sw;
    if (act === "close") return swHide();
    if (act === "note") return swSend("other", $("#swNote")?.value || "");
    if (act === "one") return swSay(esc(T("aa.why.thanks", "Got it.")));
    if (act === "all") {
      try {
        aaPaintPrefs(await api("/api/autoapply/company", { list: "exclude", name: SW.company, from_skip: true }));
        AA.next = null; if (AA.board) aaLoadNext();
        swSay(esc(T("aa.why.allDone", "You won't see {c} in Auto-apply again.").replace("{c}", SW.company)), 3200);
      } catch (err) { swSay(esc(err.message || T("aa.fail", "Couldn't do that, try again."))); }
    }
  });
  $("#skipWhy")?.addEventListener("keydown", e => {
    if (e.key === "Escape") { e.preventDefault(); swHide(); }
    if (e.key === "Enter" && e.target.id === "swNote") { e.preventDefault(); swSend("other", e.target.value); }
  });

  /* ---- Preferences: company lists, what was learned, and the visa filter ---- */
  async function aaLoadPrefs() {
    try { aaPaintPrefs(await api("/api/autoapply/prefs")); } catch { /* the panel keeps its last state */ }
  }
  function aaPaintPrefs(p) {
    if (!p || !$("#aaPfExclude")) return;
    const chips = (names, list) => names.map(n => `<span class="aa-pf-chip">${esc(n)}<button type="button" data-pf-rm="${list}" data-name="${esc(n)}" aria-label="${esc(T("aa.pf.remove", "Remove {c}").replace("{c}", n))}"><svg width="10" height="10" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></button></span>`).join("");
    $("#aaPfExclude").innerHTML = chips(p.exclude || [], "exclude");
    $("#aaPfPrio").innerHTML = chips(p.prioritize || [], "prioritize");
    const rules = p.learned || [];
    $("#aaPfLearned").innerHTML = rules.length
      ? rules.map(r => `<li><span>${esc(learnedLine(r))}</span><button class="link-btn" type="button" data-pf-forget="${esc(r.id)}">${esc(T("aa.pf.undo", "Undo"))}</button></li>`).join("")
      : `<li class="aa-pf-none">${esc(T("aa.pf.none", "Nothing yet. Pick a reason when you skip."))}</li>`;
    const v = $("#aaPfVisa");
    v.hidden = !p.needs_sponsorship;
    $("#aaPfVisaText").textContent = T("aa.pf.visa", "Hiding citizen-only and clearance roles");
  }
  $("#aaPrefs")?.addEventListener("click", async e => {
    const rm = e.target.closest("[data-pf-rm]"), fg = e.target.closest("[data-pf-forget]");
    try {
      if (rm) aaPaintPrefs(await api("/api/autoapply/company", { list: rm.dataset.pfRm, name: rm.dataset.name, remove: true }));
      else if (fg) aaPaintPrefs(await api("/api/autoapply/forget", { id: fg.dataset.pfForget }));
      else return;
      AA.next = null; aaLoadNext();
    } catch (err) { toast(err.message || T("aa.fail", "Couldn't do that, try again.")); }
  });
  [["#aaPfExcludeAdd", "exclude"], ["#aaPfPrioAdd", "prioritize"]].forEach(([id, list]) =>
    $(id)?.addEventListener("keydown", async e => {
      if (e.key !== "Enter") return;
      const name = e.target.value.trim();
      if (!name) return;
      e.preventDefault();
      try {
        aaPaintPrefs(await api("/api/autoapply/company", { list, name }));
        e.target.value = "";
        AA.next = null; aaLoadNext();
      } catch (err) { toast(err.message || T("aa.fail", "Couldn't do that, try again.")); }
    }));
  // "Change" goes straight to where the sponsorship answer lives (Your answers), not to the
  // profile page's card that only links there.
  $("#aaPfVisaChange")?.addEventListener("click", () => openAnswers("autoapply"));

  // An error the person can act on gets the action as a button next to it.
  const AA_ACTIONS = {
    add_resume: [() => T("aa.addResume", "Add your resume"), () => openResumeBox()],
    open_jobs: [() => T("aa.openJobs", "Open Jobs"), () => navTo("jobs")],
    complete_profile: [() => T("aa.addDetails", "Add the details"), () => openProfileDetails()],
  };
  function aaError(msg, e) {
    const d = (e && e.data) || {};
    const act = AA_ACTIONS[d.action];
    const missing = Array.isArray(d.missing) ? d.missing : [];
    msg.hidden = false;
    msg.className = "aa-msg err" + (act ? " has-act" : "");
    // Missing profile details: the count, then the engine's own questions as a list (they
    // already read "the dates for your project X"), and the button that opens the editor.
    const head = missing.length
      ? T("aa.missing", "Auto-apply needs {n} details first.").replace("{n}", missing.length)
      : (e.message || T("aa.runFail", "Couldn't run auto-apply."));
    msg.innerHTML = `<span>${esc(head)}`
      + (d.summary ? ` <span class="aa-msg-sub">${esc(T("aa.skippedSum", "Skipped {s}.").replace("{s}", d.summary))}</span>` : "")
      + (missing.length ? `<ul class="aa-msg-list">${missing.map(q => `<li>${esc(q)}</li>`).join("")}</ul>` : "") + `</span>`
      + (act ? `<button class="btn btn-ghost btn-sm" id="aaErrAct" type="button">${esc(act[0]())}</button>` : "");
    if (act) $("#aaErrAct").addEventListener("click", act[1]);
  }
  // Why the run tailored fewer roles than it considered: the ranking summary (below the fit
  // floor, no description) plus the per-role tailoring skips, grouped by reason.
  function aaRunReasons(r) {
    const parts = [];
    if (r.summary) parts.push(T("aa.skippedSum", "Skipped {s}.").replace("{s}", r.summary));
    const by = new Map();
    (r.skipped || []).forEach(s => by.set(s.reason || "", (by.get(s.reason || "") || 0) + 1));
    if (by.size) {
      const s = [...by].map(([reason, n]) =>
        `${n} ${n === 1 ? T("aa.role", "role") : T("aa.roles", "roles")}: ${reason}`).join("; ");
      parts.push(T("aa.notTailored", "Not tailored: {s}.").replace("{s}", s));
    }
    return parts.length ? ` <span class="aa-msg-sub">${esc(parts.join(" "))}</span>` : "";
  }
  $("#aaRun")?.addEventListener("click", async () => {
    const btn = $("#aaRun"), count = aaCount();
    const focus = aaFocus(), min_fit = Number($("#aaMinFit")?.value) || 60;
    const msg = $("#aaMsg");
    AA.runBusy = true; btn.disabled = true; btn.textContent = T("aa.tailoring", "Tailoring…");
    msg.hidden = false; msg.className = "aa-msg working";
    msg.textContent = T("aa.working", "Tailoring your {n} best matches. This can take a few minutes.").replace("{n}", count);
    try {
      const r = await api("/api/autoapply/run", { count, focus, min_fit });
      // Zero is told as zero, with every reason; "They are in To review" is only true for 1+.
      msg.className = r.queued ? "aa-msg ok" : "aa-msg err";
      msg.innerHTML = esc(r.queued ? T("aa.queued", "Queued {n}. They are in To review.").replace("{n}", r.queued)
                                   : T("aa.nothingQueued", "Nothing queued."))
        + aaRunReasons(r);
      AA_WANT = "review"; AA.next = null;
      loadAutoApply();
    } catch (e) { aaError(msg, e); }
    finally { AA.runBusy = false; btn.disabled = false; aaRunLabel(); }
  });

  /* ---------------------------------------------------------------- first run
     No welcome tour and no key gate (tailor-ux §6, 2026-10-06). The job board needs nothing, so a
     new install opens straight onto it with one dismissible banner offering to add a resume. The
     AI key is asked for only when an AI feature is first used (api() -> __openAiKeyBox). */
  const BANNER_OFF = "sj_first_banner_off";
  const wizDone = { resume: false };
  function wizMsg(el, text, cls) { el.hidden = false; el.className = "ai-msg " + cls; el.textContent = text; }
  function showWizPanel(name) {
    document.querySelectorAll("#wizard .wiz-panel").forEach(p => { p.hidden = p.dataset.panel !== name; });
    $("#wizard").hidden = false;
  }
  function openResumeBox() { $("#wizCvMsg").hidden = true; showWizPanel("resume"); }
  function closeResumeBox() { $("#wizard").hidden = true; }

  /* The details step: one input per field the saved profile lacks, in the gate's own words
     ("the dates for your project Gate Check"), saved by /api/profile/details. Shown right
     after an import that left gaps, and by "Add the details" on Auto-apply: ONE place to
     complete the profile. HARD fields first; SOFT ones (a project's dates or location) are
     marked Optional with a Skip that is remembered, so they are asked once. */
  let WIZ_SLOTS = [];
  const WIZ_HINT = { dates: "Jan 2024 - Mar 2024", date: "May 2024", location: "City, ST",
                     contact: "you@example.com", name: "Your name" };
  function renderDetailsStep(slots) {
    WIZ_SLOTS = slots;
    $("#wizDetails").innerHTML = slots.map((s, i) => `<div class="wiz-det">
        <label for="wizDet${i}">${esc(s.q.charAt(0).toUpperCase() + s.q.slice(1))}${s.soft ? ` <span class="wiz-det-opt">${esc(T("wiz.details.optional", "Optional"))}</span>` : ""}</label>
        <input class="fld" id="wizDet${i}" data-slot="${i}" placeholder="${esc(WIZ_HINT[s.key] || "")}" spellcheck="false" autocomplete="off">
        ${s.soft && s.decline ? `<label class="wiz-det-none"><input type="checkbox" data-none="${i}"> ${esc(T("wiz.details.skip", "Skip"))}</label>` : ""}
      </div>`).join("");
    $("#wizDetails input[data-slot]")?.focus();
  }
  async function openProfileDetails(slots) {
    if (!slots) { try { slots = (await api("/api/profile/missing")).missing || []; } catch { slots = []; } }
    if (!slots.length) { openProfilePage(); return; }   // nothing left to ask: the full editor
    $("#wizDetMsg").hidden = true;
    renderDetailsStep(slots);
    showWizPanel("details");
  }
  $("#wizDetSave")?.addEventListener("click", async () => {
    const msg = $("#wizDetMsg"), btn = $("#wizDetSave");
    const answers = [], declines = [];
    WIZ_SLOTS.forEach((s, i) => {
      const v = ($(`#wizDetails [data-slot="${i}"]`)?.value || "").trim();
      if ($(`#wizDetails [data-none="${i}"]`)?.checked) declines.push({ sec: s.sec, match: s.match, decline: s.decline });
      else if (v) answers.push({ sec: s.sec, key: s.key, match: s.match, value: v });
    });
    if (!answers.length && !declines.length) {
      wizMsg(msg, T("wiz.details.empty", "Fill in at least one detail, or Not now."), "err"); return;
    }
    btn.disabled = true;
    try {
      const r = await api("/api/profile/details", { answers, declines });
      if ((r.missing || []).length) {
        renderDetailsStep(r.missing);
        wizMsg(msg, T("wiz.details.more", "Saved. {n} to go.").replace("{n}", r.missing.length), "ok");
      } else {
        wizMsg(msg, T("wiz.details.done", "Saved. Your profile is complete."), "ok");
        await loadProfile();
        invalidateJobDetail();
        if ($("#view-autoapply")?.classList.contains("is-active")) { $("#aaMsg").hidden = true; loadAutoApply(); }
        setTimeout(closeResumeBox, 900);
      }
    } catch (err) { wizMsg(msg, err.message, "err"); }
    finally { btn.disabled = false; }
  });
  function hideFirstBanner(forever) {
    $("#firstBanner").hidden = true;
    if (forever) try { localStorage.setItem(BANNER_OFF, "1"); } catch { /* private window */ }
  }
  $("#firstBannerAdd")?.addEventListener("click", openResumeBox);
  $("#firstBannerX")?.addEventListener("click", () => hideFirstBanner(true));
  document.querySelectorAll("[data-wiz=close]").forEach(b => b.addEventListener("click", closeResumeBox));
  $("#wizCvFile")?.addEventListener("change", async e => {
    const file = e.target.files[0]; if (!file) return;
    const msg = $("#wizCvMsg");
    wizMsg(msg, "Reading your resume and filling your profile…", "working");
    try {
      const fd = new FormData(); fd.append("file", file);
      const res = await fetch("/api/profile/from_cv", { method: "POST", body: fd });
      const r = await res.json().catch(() => ({}));
      if (!res.ok || r.error) throw new Error(r.error || ("HTTP " + res.status));
      wizDone.resume = true;
      wizMsg(msg, `Got it${(r.summary || {}).name ? ", " + r.summary.name : ""}, your profile is filled.`, "ok");
      await loadProfile();
      invalidateJobDetail();   // match now reflects the freshly-imported profile
      if ($("#view-autoapply")?.classList.contains("is-active")) loadAutoApply();
      hideFirstBanner(true);
      // Gaps the import could not read (a project's dates) are asked now, not at the first build.
      if ((r.missing || []).length) openProfileDetails(r.missing);
      else setTimeout(closeResumeBox, 900);
    } catch (err) { wizMsg(msg, "Couldn't read that file, " + err.message, "err"); }
    finally { e.target.value = ""; }
  });

  /* boot, runs only once the app is unlocked (see the App Lock gate below) */
  async function boot() {
    renderPreview({});
    loadDashboard();
    api("/api/profile").then(paintAccount).catch(() => {});   // sidebar identity, from turn one
    loadApiKeyStatus();                                       // paints the sidebar chip, asks nothing
    const prof = await api("/api/profile").catch(() => ({}));
    let off = false;
    try { off = !!localStorage.getItem(BANNER_OFF) || !!localStorage.getItem("tailor_onboarded_v1"); } catch { /* */ }
    if (!prof.has_profile) {
      // A new install lands on the live jobs: the thing that works with no setup at all.
      if (!off) { document.querySelector('.nav-item[data-nav="jobs"]')?.click(); $("#firstBanner").hidden = false; }
    }
  }
  // App Lock gate: if a passcode is set and we're locked, show the lock screen and boot only
  // after a correct passcode. If there's no lock (the default), boot straight away.
  (async () => {
    try {
      const st = await fetch("/api/lock/status").then(r => r.json());
      if (st.enabled && !st.unlocked) { showLock(); return; }
    } catch { /* no lock endpoint / offline: boot normally */ }
    LOCK_BOOTED = true;
    boot();
  })();
  /* ================================================================ Saved searches (strategies)
     A named copy of the filter bar: apply in one click, open the same search on the other boards
     (plain URLs the person's own browser opens; we never drive or read LinkedIn, Handshake or
     any other site), and a "new since you last looked" digest from OUR feed. Server side:
     sourcing/strategies.py + /api/strategies. Kept in one block so it lifts out cleanly. */
  const ST = (k, f) => (window.I18N ? I18N.t(k, f) : f);
  const stFmt = (k, f, vars) => ST(k, f).replace(/\{(\w+)\}/g, (_, n) => (vars && n in vars) ? vars[n] : "{" + n + "}");
  let STRATS = [];                 // the saved strategies, as the server lists them (default first)
  let STRAT_ACTIVE = null;         // id of the strategy whose filters are on the board, or null
  let STRAT_DEFAULT_APPLIED = false;   // the default is applied once per session, not on every visit
  let STRAT_FORM_MODE = "new";     // "new" | "rename"
  const STRAT_NOTIFIED_KEY = "sj-strat-notified";   // {id: newest_at} already announced

  async function apiMethod(url, method, body) {
    const r = await fetch(url, { method, headers: { "Content-Type": "application/json" },
                                 body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || data.error) throw new Error(data.error || ("HTTP " + r.status));
    return data;
  }

  // The filter bar as a strategy's stored shape (the same facets /api/jobs takes).
  function stratCurrentFilters() {
    return { q: JF_SEARCH, loc: JF_LOC, days: JF_DATE, remote: JF_REMOTE, visa: [...VISA_SET],
             level: JF_LEVEL, pay: JF_PAY, sponsored: JF_SPONSORED };
  }
  // Push a strategy's filters onto the bar (and the search box, which syncFilterControls skips).
  function stratApplyFilters(f) {
    f = f || {};
    JF_SEARCH = f.q || ""; JF_LOC = f.loc || ""; JF_DATE = Number(f.days) || 0; JF_REMOTE = f.remote || "";
    VISA_SET.clear(); (f.visa || []).forEach(v => VISA_SET.add(v));
    JF_LEVEL = f.level || ""; JF_PAY = Number(f.pay) || 0; JF_SPONSORED = !!f.sponsored; JF_SAVED = false;
    const sb = $("#jfSearch"); if (sb) sb.value = JF_SEARCH;
    syncFilterControls();
  }
  function stratById(id) { return STRATS.find(s => s.id === id) || null; }

  function paintStratRail() {
    const list = $("#stratList");
    if (!list) return;
    if (!STRATS.length) {
      list.innerHTML = `<span class="strat-empty">${esc(ST("strat.none", "No saved searches yet. Set your filters, then save them to get a daily count of new sponsor jobs."))}</span>`;
    } else {
      list.innerHTML = STRATS.map(s => {
        const n = Number(s.new_count) || 0;
        return `<button class="strat-pill${s.id === STRAT_ACTIVE ? " is-active" : ""}" data-strat="${s.id}" type="button" title="${esc(s.name)}">
          ${s.is_default ? `<span class="strat-pill-star" title="${esc(ST("strat.isDefault", "Default"))}" aria-label="${esc(ST("strat.isDefault", "Default"))}">★</span>` : ""}
          <span class="strat-pill-name">${esc(s.name)}</span>
          <span class="strat-badge" ${n ? "" : "hidden"} title="${esc(ST("strat.newBadgeTitle", "new since you last looked"))}">${n}</span>
        </button>`;
      }).join("");
      list.querySelectorAll("[data-strat]").forEach(b =>
        b.addEventListener("click", () => stratActivate(Number(b.dataset.strat))));
    }
    const act = $("#stratActions");
    const cur = stratById(STRAT_ACTIVE);
    if (act) {
      act.hidden = !cur;
      if (cur) {
        $("#stratActiveName").textContent = cur.name;
        const d = $("#stratDefault"); if (d) d.hidden = !!cur.is_default;
      }
      const links = $("#stratLinks"); if (links && !cur) { links.hidden = true; links.innerHTML = ""; }
    }
    paintNavJobsBadge();
  }

  function paintNavJobsBadge() {
    const b = $("#navJobsBadge");
    if (!b) return;
    const total = STRATS.reduce((a, s) => a + (Number(s.new_count) || 0), 0);
    b.hidden = !total;
    b.textContent = total > 99 ? "99+" : String(total);
  }

  async function stratLoad() {
    try { STRATS = (await api("/api/strategies")).strategies || []; }
    catch { STRATS = []; }
    paintStratRail();
    stratMaybeNotify();
  }

  // Jobs page opened: load the rail; the first time, a default strategy (if any) becomes the board.
  async function stratOnJobsOpen() {
    await stratLoad();
    if (STRAT_DEFAULT_APPLIED) return;
    STRAT_DEFAULT_APPLIED = true;
    const def = STRATS.find(s => s.is_default);
    if (def) await stratActivate(def.id, { noReload: true });
  }

  // Apply a strategy: its filters go on the bar, the board reloads, and its "new since you last
  // looked" roles show in the New today section; the person has now looked, so the server's
  // cutoff moves and the badge clears (the section itself stays until they move on).
  async function stratActivate(id, opts) {
    const st = stratById(id);
    if (!st) return;
    STRAT_ACTIVE = id;
    stratApplyFilters(st.filters);
    paintStratRail();
    if (!(opts && opts.noReload)) loadPage(1);
    let d = null;
    try { d = await api(`/api/strategies/${id}/new`); } catch { d = null; }
    paintStratNew(d);
    if (d && d.count > 0) {
      try { await apiMethod(`/api/strategies/${id}/seen`, "POST", {}); } catch { /* badge clears next poll */ }
      st.new_count = 0; st.newest_at = null;
      paintStratRail();
    }
  }

  function stratDeactivate() {
    STRAT_ACTIVE = null;
    paintStratNew(null);
    paintStratRail();
  }

  function paintStratNew(d) {
    const sec = $("#stratNew"), list = $("#stratNewList"), cnt = $("#stratNewCount");
    if (!sec || !list) return;
    if (!d || !STRAT_ACTIVE) { sec.hidden = true; list.innerHTML = ""; return; }
    sec.hidden = false;
    cnt.textContent = String(d.count || 0);
    const jobs = d.jobs || [];
    if (!jobs.length) {
      list.innerHTML = `<div class="strat-new-more">${esc(ST("strat.nothingNew", "Nothing new since you last looked."))}</div>`;
      return;
    }
    list.innerHTML = jobs.map(j => `<button class="strat-new-row" data-new-sid="${esc(j.source_id)}" type="button">
        <span class="strat-new-co">${esc(j.company)}</span>
        <span class="strat-new-role">${esc(j.title)}</span>
        <span class="strat-new-loc">${esc(usLocation(j.location))}</span>
      </button>`).join("") + (d.count > jobs.length
        ? `<div class="strat-new-more">${esc(stFmt("strat.more", "and {n} more in the list below", { n: d.count - jobs.length }))}</div>` : "");
    list.querySelectorAll("[data-new-sid]").forEach(b => b.addEventListener("click", () => {
      const sid = b.dataset.newSid;
      const j = jobs.find(x => x.source_id === sid);
      if (j && !LAST_JOBS.find(x => x.source_id === sid)) LAST_JOBS.unshift(j);   // the detail pane reads LAST_JOBS
      selectJob(sid);
    }));
  }

  // Save / rename form.
  function stratOpenForm(mode) {
    STRAT_FORM_MODE = mode;
    const f = $("#stratForm"), n = $("#stratName");
    if (!f || !n) return;
    const cur = stratById(STRAT_ACTIVE);
    n.value = mode === "rename" && cur ? cur.name : (JF_SEARCH ? JF_SEARCH + (JF_LOC ? " in " + JF_LOC : "") : "");
    f.hidden = false;
    $("#stratSaveBtn").hidden = true;
    n.focus(); n.select();
  }
  function stratCloseForm() {
    const f = $("#stratForm"); if (f) f.hidden = true;
    const b = $("#stratSaveBtn"); if (b) b.hidden = false;
  }
  $("#stratSaveBtn")?.addEventListener("click", () => { stratOpenForm("new"); stratAskNotifyPermission(); });
  $("#stratFormCancel")?.addEventListener("click", stratCloseForm);
  $("#stratForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = ($("#stratName").value || "").trim();
    if (!name) return;
    try {
      if (STRAT_FORM_MODE === "rename" && STRAT_ACTIVE) {
        await apiMethod(`/api/strategies/${STRAT_ACTIVE}`, "POST", { name });
      } else {
        const r = await apiMethod("/api/strategies", "POST", { name, filters: stratCurrentFilters(), is_default: !STRATS.length });
        STRAT_ACTIVE = r.strategy.id;
      }
      stratCloseForm();
      await stratLoad();
      if (STRAT_FORM_MODE !== "rename") paintStratNew({ count: 0, jobs: [] });
    } catch (err) { toast(err.message); }
  });
  $("#stratUpdate")?.addEventListener("click", async (e) => {
    if (!STRAT_ACTIVE) return;
    try {
      await apiMethod(`/api/strategies/${STRAT_ACTIVE}`, "POST", { filters: stratCurrentFilters() });
      await stratLoad();
      const b = e.currentTarget; const was = b.textContent;
      b.textContent = ST("strat.updated", "Updated"); setTimeout(() => { b.textContent = was; }, 1500);
    } catch (err) { toast(err.message); }
  });
  $("#stratRename")?.addEventListener("click", () => stratOpenForm("rename"));
  $("#stratDefault")?.addEventListener("click", async () => {
    if (!STRAT_ACTIVE) return;
    try { await apiMethod(`/api/strategies/${STRAT_ACTIVE}/default`, "POST", {}); await stratLoad(); }
    catch (err) { toast(err.message); }
  });
  $("#stratDelete")?.addEventListener("click", async () => {
    if (!STRAT_ACTIVE) return;
    if (!window.confirm(ST("strat.deleteConfirm", "Delete this saved search?"))) return;
    try { await apiMethod(`/api/strategies/${STRAT_ACTIVE}`, "DELETE"); }
    catch (err) { toast(err.message); return; }
    stratDeactivate();
    await stratLoad();
  });
  $("#stratClear")?.addEventListener("click", () => {
    stratDeactivate();
    JF_SEARCH = ""; const sb = $("#jfSearch"); if (sb) sb.value = "";
    clearFilter("all");            // resets every other facet and reloads page 1
  });
  $("#stratNewDismiss")?.addEventListener("click", async () => {
    if (STRAT_ACTIVE) { try { await apiMethod(`/api/strategies/${STRAT_ACTIVE}/seen`, "POST", {}); } catch { /* best effort */ } }
    paintStratNew({ count: 0, jobs: [] });
    await stratLoad();
  });

  // "Open on other boards": the server builds one plain search URL per board from the strategy's
  // keywords and location; the SHELL hands them to the person's own browser (one tab each) or, in
  // a plain browser, window.open does. Pop-up blockers may keep all but the first, so the same
  // links are also listed to click one by one. Nothing is fetched or read from those sites.
  $("#stratOpenBoards")?.addEventListener("click", async () => {
    if (!STRAT_ACTIVE) return;
    let urls = [];
    try { urls = (await api(`/api/strategies/${STRAT_ACTIVE}/open-urls`)).urls || []; }
    catch (err) { toast(err.message); return; }
    const box = $("#stratLinks");
    if (box) {
      box.hidden = false;
      box.innerHTML = `<span>${esc(ST("strat.openHint", "Each search opens in your own browser. If only one tab opened, your browser blocked the rest: click them here."))}</span>` +
        urls.map(u => `<a href="${esc(u.url)}" target="_blank" rel="noopener noreferrer">${esc(u.label)}</a>` +
          (u.note === "login" ? ` <span class="strat-link-note">(${esc(ST("strat.handshakeNote", "needs your Handshake login"))})</span>` : "")).join("");
    }
    const plain = urls.map(u => u.url);
    if (window.tailorShell && window.tailorShell.openLinksExternal) window.tailorShell.openLinksExternal(plain);
    else plain.forEach(u => window.open(u, "_blank", "noopener"));
  });

  // Digest notification: once per (strategy, newest arrival), so a count that has not changed
  // is never announced twice. Desktop shell first; a plain browser uses the Notification API
  // (permission asked when the person saves a search, a real gesture). No email (see the TODO
  // in ui/app.py next to the strategies routes).
  function stratReadNotified() { try { return JSON.parse(localStorage.getItem(STRAT_NOTIFIED_KEY) || "{}") || {}; } catch { return {}; } }
  function stratAskNotifyPermission() {
    if (window.tailorShell && window.tailorShell.notify) return;
    try { if ("Notification" in window && Notification.permission === "default") Notification.requestPermission().catch(() => {}); } catch { /* not available */ }
  }
  function stratMaybeNotify() {
    const seen = stratReadNotified();
    const fresh = STRATS.filter(s => (Number(s.new_count) || 0) > 0 && s.newest_at && seen[String(s.id)] !== s.newest_at);
    if (!fresh.length) return;
    fresh.forEach(s => { seen[String(s.id)] = s.newest_at; });
    try { localStorage.setItem(STRAT_NOTIFIED_KEY, JSON.stringify(seen)); } catch { /* fine */ }
    const n = fresh.reduce((a, s) => a + (Number(s.new_count) || 0), 0);
    const title = ST("strat.notifyTitle", "New sponsor jobs");
    const body = fresh.length === 1
      ? stFmt("strat.notifyOne", "{n} new for {name}", { n, name: fresh[0].name })
      : stFmt("strat.notifyMany", "{n} new across {k} saved searches", { n, k: fresh.length });
    if (window.tailorShell && window.tailorShell.notify) { window.tailorShell.notify(title, body); return; }
    try { if ("Notification" in window && Notification.permission === "granted") new Notification(title, { body }); } catch { /* not available */ }
  }
  // Counts refresh with the board's own poll while Jobs is open, and on a slow timer from any
  // page so the sidebar badge and the notification do not depend on the person sitting on Jobs.
  function stratPoll() { stratLoad(); }
  setTimeout(stratPoll, 4000);
  setInterval(stratPoll, 600000);

})();

/* ---------------- the side panel v2: icon rail · drag divider · CV workspace ----
   The right icon rail is the panel's permanent handle (document = CV workspace,
   globe = browser): content sits BESIDE it, so nothing can overlap. The CV tab now
   hosts the whole preview workspace (coverage, edit bullets, accept) relocated from
   the builder, so the center stays a clean chat while the artifact lives here -
   revealed when a build completes, at 100%, with the viewer chrome stripped. */
(function () {
  const shell = window.tailorShell || null;
  const body = document.body;
  const railCv = document.getElementById("railCvBtn");
  const railBrowser = document.getElementById("railBrowserBtn");
  const panel = document.getElementById("sidePanel");
  const tabCv = document.getElementById("spTabCv");
  const tabBrowser = document.getElementById("spTabBrowser");
  const bodyCv = document.getElementById("spBodyCv");
  const bodyBrowser = document.getElementById("spBodyBrowser");
  const slot = document.getElementById("browserSlot");
  const urlBox = document.getElementById("bbUrl");
  const back = document.getElementById("bbBack");
  const fwd = document.getElementById("bbFwd");
  const dl = document.getElementById("spDownload");

  let tab = "cv";
  let lastPdf = "";

  // The pane is a NATIVE view drawn above the whole page, so a pop-up can never cover it: in 0.1.0
  // the pane sat on top of the first-run box, whose dimmed backdrop covered only the pane's own
  // header, so the browser looked cut off at the top. While any pop-up is showing, hide the pane.
  const OVERLAYS = ".ai-modal, .wiz, .tpl-modal, [aria-modal=true]";
  const overlayOpen = () => [...document.querySelectorAll(OVERLAYS)]
    .some((el) => el.getClientRects().length > 0);
  let sentBounds = "";
  function sendBounds() {
    if (!shell) return;
    let rect = { x: 0, y: 0, width: 0, height: 0 };
    if (body.classList.contains("panel-open") && tab === "browser" && !overlayOpen()) {
      const r = slot.getBoundingClientRect();
      rect = { x: r.x, y: r.y, width: r.width, height: r.height };
    }
    const key = JSON.stringify(rect);
    if (key === sentBounds) return;
    sentBounds = key;
    shell.setBounds(rect);
  }
  // A zoom change (View > Zoom In/Out) resizes the page in CSS pixels: re-send the slot.
  window.addEventListener("resize", () => requestAnimationFrame(sendBounds));
  let boundsQueued = false;
  new MutationObserver(() => {
    if (boundsQueued) return;
    boundsQueued = true;
    requestAnimationFrame(() => { boundsQueued = false; sendBounds(); });
  }).observe(body, { subtree: true, attributes: true, attributeFilter: ["hidden", "class"] });

  function paintRail() {
    const open = body.classList.contains("panel-open");
    railCv.classList.toggle("is-active", open && tab === "cv");
    railBrowser.classList.toggle("is-active", open && tab === "browser");
  }

  function setTab(name) {
    tab = name;
    tabCv.classList.toggle("is-active", name === "cv");
    tabBrowser.classList.toggle("is-active", name === "browser");
    bodyCv.hidden = name !== "cv";
    bodyBrowser.hidden = name !== "browser";
    dl.hidden = name !== "cv" || !dl.getAttribute("href");
    paintRail();
    requestAnimationFrame(sendBounds);
  }

  function open(name) {
    if (name) setTab(name);
    body.classList.add("panel-open");
    if (tab === "cv") railCv.classList.remove("has-doc");   // seen it
    paintRail();
    requestAnimationFrame(sendBounds);
  }
  function close() {
    body.classList.remove("panel-open");
    paintRail();
    requestAnimationFrame(sendBounds);
  }
  function railClick(name) {
    if (body.classList.contains("panel-open") && tab === name) close();
    else open(name);
  }
  railCv.addEventListener("click", () => railClick("cv"));
  railBrowser.addEventListener("click", () => railClick("browser"));
  document.getElementById("spClose").addEventListener("click", close);
  tabCv.addEventListener("click", () => setTab("cv"));
  tabBrowser.addEventListener("click", () => setTab("browser"));

  // -------- drag divider: remembers your width, min/max keeps both sides usable.
  const RAIL_W = 42;
  const saved = parseInt(localStorage.getItem("panelW") || "", 10);
  if (saved) document.documentElement.style.setProperty("--panel-w", saved + "px");
  const grip = document.getElementById("spResize");
  grip.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    grip.classList.add("dragging");
    grip.setPointerCapture(e.pointerId);
    const move = (ev) => {
      const sidebarW = (document.querySelector(".sidebar")?.offsetWidth) || 0;
      const maxW = Math.max(380, window.innerWidth - RAIL_W - sidebarW - 340);
      const w = Math.min(Math.max(window.innerWidth - ev.clientX - RAIL_W, 380), maxW);
      document.documentElement.style.setProperty("--panel-w", w + "px");
      sendBounds();
    };
    const up = (ev) => {
      grip.classList.remove("dragging");
      grip.releasePointerCapture(e.pointerId);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      const w = getComputedStyle(document.documentElement).getPropertyValue("--panel-w").trim();
      localStorage.setItem("panelW", parseInt(w, 10) || "");
      sendBounds();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });

  window.Panel = {
    open, close, setTab,
    isOpen(name) { return body.classList.contains("panel-open") && (!name || tab === name); },
    setContext(ctx) { panel.classList.toggle("ctx-package", ctx === "package"); },
    // Point the workspace at a PDF (package flow), or clear it.
    setCv(src, { reveal = false, download = "", context = "" } = {}) {
      if (context) this.setContext(context);
      const pdf = document.getElementById("pdfFrame");
      const empty = document.getElementById("pdfPlaceholder");
      if (src) {
        pdf.src = src;
        pdf.hidden = false; if (empty) empty.hidden = true;
        dl.setAttribute("href", download || src.split("&preview=1")[0]);
        if (reveal) open("cv");
        else { railCv.classList.add("has-doc"); if (tab === "cv") dl.hidden = false; }
      } else {
        pdf.removeAttribute("src");
        pdf.hidden = true; if (empty) empty.hidden = false;
        dl.hidden = true; dl.removeAttribute("href");
        railCv.classList.remove("has-doc");
      }
    },
    // A build finished (or a bullet edit recompiled): builder context; reveal ONCE
    // per new pdf so closing the panel is respected on later messages.
    noteBuild(pdfUrl) {
      this.setContext("builder");
      dl.setAttribute("href", pdfUrl);
      if (pdfUrl !== lastPdf) {
        lastPdf = pdfUrl;
        open("cv");
      } else {
        railCv.classList.add("has-doc");
      }
    },
  };

  // ---------------- browser tenant (desktop shell only)
  if (!shell) {
    document.getElementById("bbNoShell").hidden = false;
    slot.style.display = "none";
    document.querySelector("#spBodyBrowser .browser-bar").style.display = "none";
  } else {
    shell.onState((st) => {
      if (st.open) open("browser");
      if (document.activeElement !== urlBox) urlBox.value = st.url || "";
      back.disabled = !st.canGoBack;
      fwd.disabled = !st.canGoForward;
    });
    new ResizeObserver(sendBounds).observe(slot);
    window.addEventListener("resize", () => {
      const cur = parseInt(getComputedStyle(document.documentElement)
        .getPropertyValue("--panel-w"), 10);
      const sidebarW = (document.querySelector(".sidebar")?.offsetWidth) || 0;
      const maxW = Math.max(380, window.innerWidth - RAIL_W - sidebarW - 340);
      if (cur && cur > maxW) {
        document.documentElement.style.setProperty("--panel-w", maxW + "px");
      }
      sendBounds();
    });
    back.addEventListener("click", () => shell.goBack());
    fwd.addEventListener("click", () => shell.goForward());
    document.getElementById("bbReload").addEventListener("click", () => shell.reload());
    shell.onUpdateAvailable?.((info) => {
      const a = document.getElementById("updateNote");
      if (!a || !info || !info.url) return;
      a.href = info.url;
      a.textContent = `Version ${info.version} is out. Download it`;
      a.hidden = false;
      a.onclick = (e) => { if (shell.openReleasePage) { e.preventDefault(); shell.openReleasePage(info.url); } };
    });
    const out = document.getElementById("bbExternal");
    if (shell.openExternal) out.addEventListener("click", () => shell.openExternal());
    else out.hidden = true;                          // a 0.1.0 shell has no hand-off
    urlBox.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      let u = urlBox.value.trim();
      if (!u) return;
      if (!/^https?:\/\//i.test(u)) u = "https://" + u;
      shell.navigate(u);
      open("browser");
    });
  }
})();
