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
      p: "Free includes 3 tailored packages a month. A Job Hunt Pass or a Season Pass gives you many more, paid once with no auto-renew. Or add your own AI key for unlimited tailoring.",
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
  setRail(localStorage.getItem(RAIL_KEY) === "1");   // stays how you left it
  $("#railToggle")?.addEventListener("click",
    () => setRail(!appEl.classList.contains("is-rail")));

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
    strip.innerHTML = CV_TEMPLATES.map(cvsTplCard).join("");
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
    const sub = [esc(c.company || ""), c.coverage ? `${c.coverage}% match` : ""].filter(Boolean).join("  \u00b7  ");
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
      root.innerHTML = `<div class="empty">No resumes yet. Pick a template above to build your first one.</div>`;
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
                         : TPL_ORIGIN === "newcv" ? "Dashboard" : "Resumes";
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

  // The old Home hero/prompt wiring: typing fades the page furniture; Enter (or the
  // button) starts a tailoring session with the text as the opening message, reusing
  // the exact JD-gate path New CV uses. Esc clears and brings the landing back.
  (function () {
    const box = $("#homePrompt");
    if (!box) return;
    const dash = $("#view-dashboard");
    const sync = () => dash.classList.toggle("home-typing", !!box.value.trim());
    const start = () => {
      const text = box.value.trim();
      if (!text) return;
      openBuilder();
      $("#jdInput").value = text;
      $("#jdStart").click();
      box.value = ""; sync();
    };
    box.addEventListener("input", sync);
    box.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { box.value = ""; sync(); box.blur(); return; }
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); start(); }
    });
    $("#homeGo")?.addEventListener("click", start);
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

  // Home: a small curated peek at the freshest roles (full feed lives in Jobs).
  async function loadHomeToday() {
    const el = $("#homeToday");
    if (!el) return;
    try {
      const r = await api("/api/jobs");
      const jobs = rankForReachability(r.jobs || []).slice(0, 4);
      if (!jobs.length) { el.innerHTML = `<div class="empty small">No roles yet, open <b>Jobs</b> to add sources.</div>`; return; }
      el.innerHTML = jobs.map(j => {
        const f = freshness(j);
        return `<button class="today-row" data-sid="${esc(j.source_id)}" type="button">
          <div class="fr-ic">${esc((j.company || "?").slice(0, 1))}</div>
          <div class="fr-main"><div class="fr-role">${esc(j.title)}</div>
          <div class="fr-co">${esc(j.company)}<span>·</span>${esc(j.location || "Not listed")}${f.label ? '<span>·</span><span class="fr-fresh ' + f.cls + '">' + esc(f.label) + "</span>" : ""}${(j.visa || []).length ? '<span>·</span><span class="fr-visa" title="Employer&#39;s past USCIS/DOL filings: a signal they&#39;ve sponsored before, not a guarantee for this role.">' + esc((j.visa || []).map(v => v.code || v.label).filter(Boolean).join(", ")) + "</span>" : ""}</div></div>
          <span class="today-go">Tailor →</span>
        </button>`;
      }).join("");
      el.querySelectorAll("[data-sid]").forEach(b =>
        b.addEventListener("click", () => tailorJob(b.dataset.sid)));
    } catch { el.innerHTML = ""; }
  }

  /* ---------------------------------------------------------------- application package */
  let PKG_ID = null;
  let PKG_APPLY_URL = "";

  let PKG_ORIGIN = "dashboard";
  function openPackage(id) {
    PKG_ORIGIN = document.querySelector(".view.is-active")?.id?.replace("view-", "") || "dashboard";
    const lbl = $("#pkgBackLbl");
    if (lbl) lbl.textContent = PKG_ORIGIN === "cvs" ? "Saved Resumes" : "Dashboard";
    PKG_ID = id; showView("package"); loadPackage(id);
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
    $("#pkgAssistCard").hidden = !window.tailorShell;
    loadVariantCard();
    if (window.tailorShell && !$("#pkgAssistUrl").value) {
      $("#pkgAssistUrl").value = p.job_url || p.url || "";
    }
    setPkgStatus(p.status);

    if (p.pdf) {
      const src = p.pdf + "&v=" + Date.now() + "&preview=1#toolbar=0&navpanes=0&view=FitH";
      $("#pkgPdf").src = src; $("#pkgPdf").hidden = false; $("#pkgNoPdf").hidden = true;
      $("#pkgDownload").href = p.pdf; $("#pkgDownload").hidden = false;
      // ATS-friendly Word export of the same tailored CV, built on the fly from the record.
      const dx = $("#pkgDownloadDocx");
      if (dx && PKG_ID) { dx.href = "/api/cv.docx?rid=" + PKG_ID; dx.hidden = false; }
      window.Panel && Panel.setCv(src, { download: p.pdf, context: "package" });
    } else {
      $("#pkgPdf").hidden = true; $("#pkgNoPdf").hidden = false;
      $("#pkgDownload").hidden = true;
      if ($("#pkgDownloadDocx")) $("#pkgDownloadDocx").hidden = true;
      window.Panel && Panel.setCv("", { context: "package" });
    }

    const cov = p.coverage || {};
    $("#pkgCovNum").textContent = (cov.ratio != null ? cov.ratio + "%" : "");
    $("#pkgCov").innerHTML = pkgCoverage(cov);

    const visa = p.visa || [];
    $("#pkgVisaCard").hidden = !visa.length;
    $("#pkgVisa").innerHTML = visa.map(v =>
      `<span class="vbadge vb-${esc(v.code)}" title="${esc(visaTitle(v))}">${esc(v.code)}</span>`).join("");

    $("#pkgCl").value = p.cover_letter || "";
    $("#pkgClMsg").textContent = "";
    renderClReview(null);                      // clear any prior record's letter check
    pkgSetPdfLink(!!(p.cover_letter || "").trim());
    // Guided apply: an application should be apply-ready. Auto-draft the cover letter the
    // first time a package is opened without one, so there's nothing extra to click.
    if (!(p.cover_letter || "").trim() && p.status !== "applied") draftCover(true);

    const scr = p.screening || [];
    $("#pkgScrCard").hidden = !scr.length;
    // A knock-out question is one the ATS auto-rejects on (work authorization, years,
    // degree, salary...). It gets a visible mark so the person answers it themselves,
    // carefully, instead of trusting a draft.
    $("#pkgScr").innerHTML = scr.map(qa =>
      `<div class="pkg-qa${qa.knockout ? " pkg-qa-knockout" : ""}">
         <div class="pkg-q">${esc(qa.question)}${qa.knockout
           ? ` <span class="pkg-knockout" title="Employers auto-reject on this question. Check the answer yourself.">auto-reject question, check this one</span>`
           : ""}</div>
         <div class="pkg-a">${esc(qa.answer)}</div></div>`).join("");

    const sub = p.submission || {};
    $("#pkgTier").className = "pkg-tier-pill " + (sub.tier || "assisted");
    $("#pkgTier").textContent = sub.label || "Assisted, you review and click submit";
    $("#pkgSubmitMsg").textContent = (sub.result && sub.result.message) || "";
    PKG_APPLY_URL = p.apply_url || "";
    $("#pkgSubmitBtn").textContent = sub.tier === "auto" ? "Auto-submit"
      : (PKG_APPLY_URL ? "Open posting & apply →" : "Submit");
  }

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

  // AI auto-fill: the whole form, read and filled intelligently by the engine's brain (your personal
  // details are resolved locally and never sent to the AI). The submit click stays yours.
  function reportAutoApply(r) {
    const note = $("#pkgAssistNote");
    if (!r || r.ok === false) {
      note.textContent =
        r && r.reason === "linkedin" ? "LinkedIn is browse-by-hand only. SponsorJobs never automates LinkedIn." :
        r && r.reason === "blocked" ? (r.blocker === "captcha"
          ? "This page has a captcha, so I stopped, I won't try to get past it. Solve it, then fill again."
          : "This page needs you to sign in first. Sign in, then click AI-fill the open page.") :
        r && r.reason === "no-fields" ? "No form fields found on this page yet. Open the application form, then fill again." :
        (r && r.reason) === "This site is fill-by-hand only. Open it and apply yourself." ? r.reason :
        r && r.reason && r.reason.startsWith("Could not plan") ? r.reason :
        "Couldn't AI-fill this page. It's open in the browser, fill it by hand.";
      return;
    }
    const bits = [];
    if (r.filled) bits.push(r.filled + " field" + (r.filled === 1 ? "" : "s"));
    if (r.uploaded) bits.push("resume attached");
    note.textContent = (bits.length
      ? "Filled " + bits.join(", ") + "."
        + (r.pending_upload ? " Attach your resume PDF yourself." : "")
        + " Review every field, the submit click is yours."
      : "Nothing to fill on this step. Continue to the next page of the application and AI-fill again.");
  }

  $("#pkgAutoBtn")?.addEventListener("click", async () => {
    if (!window.tailorShell || !PKG_ID) return;
    const url = $("#pkgAssistUrl").value.trim();
    const note = $("#pkgAssistNote");
    if (!/^https?:\/\//i.test(url)) { note.textContent = "Paste the application page URL first (https://…)."; return; }
    const btn = $("#pkgAutoBtn"); btn.disabled = true;
    note.textContent = "Opening the application and AI-filling it…";
    const r = await window.tailorShell.autoApplyRun(url, PKG_ID);
    btn.disabled = false;
    reportAutoApply(r);
  });

  $("#pkgAutoHereBtn")?.addEventListener("click", async () => {
    if (!window.tailorShell || !PKG_ID) return;
    const btn = $("#pkgAutoHereBtn"); btn.disabled = true;
    $("#pkgAssistNote").textContent = "AI-filling the page that's open in the browser…";
    const r = await window.tailorShell.autoApplyFillCurrent(PKG_ID);
    btn.disabled = false;
    reportAutoApply(r);
  });

  $("#pkgAssistHereBtn")?.addEventListener("click", async () => {
    if (!window.tailorShell) return;
    const note = $("#pkgAssistNote");
    const btn = $("#pkgAssistHereBtn"); btn.disabled = true;
    note.textContent = "Filling the page that's open in the browser…";
    const r = await window.tailorShell.assistFillCurrent();
    btn.disabled = false;
    if (!r || r.ok === false) {
      note.textContent =
        r && r.reason === "linkedin" ? "LinkedIn is browse-by-hand only. SponsorJobs never automates LinkedIn." :
        r && r.reason === "no-page" ? "Nothing is open in the browser tab yet. Open the application page first." :
        r && r.reason === "no-profile" ? "No saved profile yet. Build a resume once, then fill." :
        "Couldn't fill this page.";
      return;
    }
    const bits = [];
    if (r.filled) bits.push(r.filled + " contact field" + (r.filled === 1 ? "" : "s"));
    if (r.screening) bits.push(r.screening + " screening answer" + (r.screening === 1 ? "" : "s"));
    if (r.eeo) bits.push("EEO set to decline");
    note.textContent = (bits.length
      ? "Filled " + bits.join(", ") + "." + (r.resume_upload ? " Attach your resume PDF yourself." : "")
        + " Review everything, the submit click is yours."
      : "Nothing to fill on this step. Continue to the next page of the application and fill again.");
  });

  $("#pkgAssistBtn")?.addEventListener("click", async () => {
    if (!window.tailorShell) return;
    const url = $("#pkgAssistUrl").value.trim();
    const note = $("#pkgAssistNote");
    if (!/^https?:\/\//i.test(url)) { note.textContent = "Paste the application page URL first (https://…)."; return; }
    const btn = $("#pkgAssistBtn"); btn.disabled = true;
    note.textContent = "Opening and filling…";
    const r = await window.tailorShell.assistFill(url);
    btn.disabled = false;
    if (!r || r.ok === false) {
      note.textContent =
        r && r.reason === "linkedin" ? "LinkedIn is browse-by-hand only. SponsorJobs never automates LinkedIn." :
        r && r.reason === "no-profile" ? "No saved profile yet. Build a resume once, then fill." :
        "Couldn't fill this page. It's open in the browser, fill it by hand.";
      return;
    }
    const bits = [];
    if (r.filled) bits.push(r.filled + " contact field" + (r.filled === 1 ? "" : "s"));
    if (r.screening) bits.push(r.screening + " screening answer" + (r.screening === 1 ? "" : "s"));
    if (r.eeo) bits.push("EEO set to decline");
    note.textContent = (bits.length
        ? "Filled " + bits.join(", ") + "." + (r.resume_upload ? " Attach your resume PDF yourself." : "")
          + " Review everything, the submit click is yours."
        : "Nothing to fill here. If the site wants a sign-in first (Amazon does), log in "
          + "in the Browser tab, walk to the application form, then click Fill the open page.");
  });

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
  function visaBadges(j) {
    const v = j.visa || [];
    // The POSTING itself states it sponsors (freehire enrichment) -- the strongest, most current
    // signal: the employer's declaration on THIS role. Shown first, distinct from the DOL-history
    // codes. When the posting states it but we hold no filing history, this replaces the neutral
    // "No sponsorship record" (the role is explicitly sponsor-friendly).
    const stated = j.sponsorship_stated === true
      ? `<span class="vbadge vb-stated" title="The job ad itself says the company offers visa sponsorship. The strongest, most current signal.">Sponsorship in ad</span>`
      : "";
    if (!v.length) {
      return stated || `<span class="vbadge vb-none" title="We have no public government record that this employer has sponsored a work visa. They still might, it's worth asking.">No sponsorship record</span>`;
    }
    return stated + v.map(x =>
      `<span class="vbadge vb-spon vb-${esc(x.code)}" title="${esc(visaTitle(x))}">${esc(x.code)}</span>`
    ).join("");
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
  function roleTags(j) {
    const t = j.title || "";
    const hits = [];
    for (const [re, label] of ROLE_CATS) { if (re.test(t)) hits.push(label); if (hits.length >= 2) break; }
    return hits.map(c => `<span class="fr-tag">${esc(c)}</span>`).join("");
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
  function usLocation(loc) {
    const s = (loc || "").trim();
    if (!s) return "Not listed";
    const parts = s.split(";").map(p => p.trim()).filter(Boolean);
    const us = parts.find(p => /\b(united states|usa|u\.s\.?)\b/i.test(p))
      || parts.find(p => /,\s*[A-Z]{2}\b/.test(p) && !/\b(UK|BC|ON|QC)\b/.test(p));
    return (us || parts[0] || s).replace(/,?\s*(united states|usa)\.?$/i, "").trim();
  }

  let SELECTED_SID = null;

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
  const VISA_LABELS = { SPONSORED: "Sponsors a visa", "H-1B": "H-1B", "GREEN-CARD": "Green card",
    "STEM-OPT": "STEM-OPT", "E-3": "E-3", "H-1B1": "H-1B1", "TN": "TN" };
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
    if (JF_SPONSORED) chips.push({ k: "sponsored", label: "Sponsorship in ad" });
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
  // Render the current SERVER page (LAST_JOBS already IS the page; filtering + slicing happen
  // server-side now, so the board can browse tens of thousands with a small payload each time).
  function paintFeed() {
    const jobs = LAST_JOBS;
    const list = $("#feedList");
    if (!list) return;
    renderActiveFilters();
    renderNote();
    if (!jobs.length) {
      list.innerHTML = FEED_LOADED
        ? `<div class="empty small">No roles match your filters. <button class="link-btn" data-clear="all" type="button">Clear filters</button></div>`
        : jobSkeleton();
      $("#feedDetail").innerHTML = `<div class="fd-empty">Pick a role on the left to read the full description here.</div>`;
      list.querySelector("[data-clear]")?.addEventListener("click", () => clearFilter("all"));
      return;
    }
    list.innerHTML = jobs.map(j => {
      const f = freshness(j);
      const isNew = j.is_new ? `<span class="badge-new">new</span>` : "";
      const wm = workMode(j);
      const tags = roleTags(j);
      const visas = visaBadges(j);
      return `<button class="feed-row${j.source_id === SELECTED_SID ? " is-active" : ""}${j.saved ? " is-saved" : ""}" data-sid="${esc(j.source_id)}" type="button">
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
          </div>
          <div class="fr-visas">${visas}</div>
        </div>
      </button>`;
    }).join("") + pagerHtml();
    list.querySelectorAll("[data-sid]").forEach(b =>
      b.addEventListener("click", () => selectJob(b.dataset.sid)));
    list.querySelectorAll(".feed-pager [data-page]").forEach(b =>
      b.addEventListener("click", () => {
        const p = Number(b.dataset.page);
        const pages = Math.max(1, Math.ceil(FEED_TOTAL / JOBS_PER_PAGE));
        if (p >= 1 && p <= pages) { loadPage(p); list.scrollTop = 0; }
      }));
    const stillThere = jobs.some(j => j.source_id === SELECTED_SID);
    selectJob(stillThere ? SELECTED_SID : jobs[0].source_id);
  }

  function selectJob(sid) {
    SELECTED_SID = sid;
    const j = LAST_JOBS.find(x => x.source_id === sid);
    const list = $("#feedList");
    if (list) list.querySelectorAll(".feed-row").forEach(r => r.classList.toggle("is-active", r.dataset.sid === sid));
    const det = $("#feedDetail");
    if (!det || !j) return;
    const f = freshness(j);
    const wm = workMode(j);
    const tags = roleTags(j);
    det.innerHTML = `
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
    det.querySelector("[data-fd-tailor]")?.addEventListener("click", e => tailorJob(e.currentTarget.dataset.fdTailor));
    det.querySelector("[data-fd-save]")?.addEventListener("click", e => toggleSave(e.currentTarget, j));
    det.querySelector("[data-fd-dismiss]")?.addEventListener("click", async e => {
      const s = e.currentTarget.dataset.fdDismiss;
      await api("/api/jobs/dismiss", { source_id: s });
      SELECTED_SID = null;
      loadPage(JOBS_PAGE);              // re-fetch the page so counts + backfill stay correct
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
    renderJd(data.jd, data.match);
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
    if (!s) { el.innerHTML = ""; return; }
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
      <p class="fd-company-track">${line}</p>`;
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
    if (!m) { el.innerHTML = `<div class="fd-match-none">Drop your resume in the chat and I'll instantly show how you match this role, and every other one.</div>`; return; }
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
  function markTerms(html, present, missing) {
    const marks = [
      ...(missing || []).map(t => [t, "jd-gap"]),
      ...(present || []).map(t => [t, "jd-have"]),
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
  function renderJdLine(text, present, missing) {
    const m = text.match(/^([A-Z][A-Za-z0-9 &/()\-.]{1,30}):(\s*)(.*)$/);
    if (m && m[1].trim().split(/\s+/).length <= 5) {
      const val = m[3] ? " " + markTerms(esc(m[3]), present, missing) : "";
      return `<b class="jd-lbl">${esc(m[1])}:</b>${val}`;
    }
    return markTerms(esc(text), present, missing);
  }

  // Turn a section's lines into HTML: runs of bullet lines become a real <ul>, everything else is
  // a paragraph. A marker sitting alone on its line (some stored feeds split "•" from its text)
  // is folded into the line beneath it so it still reads as one bullet.
  function jdBody(lines, present, missing) {
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
        `<li>${markTerms(esc(b), present, missing)}</li>`).join("")}</ul>`);
      bullets = [];
    };
    rows.forEach(ln => {
      if (isBullet(ln)) { bullets.push(stripBullet(ln)); return; }
      if (!ln.trim()) return;                 // blank lines are spacing, not a break in a bullet run
      flush();
      out.push(`<p>${renderJdLine(ln.trim(), present, missing)}</p>`);
    });
    flush();
    return out.join("");
  }

  function renderJd(jd, match) {
    const el = $("#fdJd");
    if (!el) return;
    jd = (jd || "").trim();
    if (!jd) {
      el.innerHTML = '<span class="muted">This board didn\'t include the full text, open the posting to read it. You can still tailor from the title and company.</span>';
      return;
    }
    const present = (match || {}).present || [], missing = (match || {}).missing || [];
    el.innerHTML = jdSections(jd).map(s => {
      const body = jdBody(s.body, present, missing);
      return `<section class="jd-sec">
        ${s.head ? `<h4 class="jd-sec-h">${markTerms(esc(s.head), present, missing)}</h4>` : ""}
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
  const QUIET_AFTER_MS = 6 * 3600000; // the feed rebuilds every 3h; two empty builds is a fault
  let FEED_REQ = 0;           // request counter, to ignore out-of-order responses
  let FEED_OK = false;         // have we ever loaded a trustworthy (non-degraded) board?
  let FEED_RECONNECTING = false;
  let FEED_CRAWLING = false;     // the engine is filling an empty local store for the first time
  let FEED_CRAWL_ERROR = "";     // the last first-open crawl failed and nothing is stored yet
  let FEED_CRAWL_TIMER = null;
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

  function renderNote() {
    const el = $("#jobsNote");
    if (!el) return;
    const total = FEED_ALLTOTAL, shown = FEED_TOTAL;
    const count = shown === total
      ? `${total.toLocaleString()} live role${total === 1 ? "" : "s"}`
      : `${shown.toLocaleString()} of ${total.toLocaleString()} role${total === 1 ? "" : "s"}`;
    // While reconnecting, say so instead of flashing a stale count or a frozen timestamp. On a cold
    // start (no good board yet) there's no count to keep, so just show "connecting to the live feed".
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
    el.classList.toggle("feed-note-quiet", isQuiet);
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
    } catch (e) { wk.remove(); bubble("agent", `<span style="color:var(--flag)">${esc(e.message)}</span>`); }
  }

  document.querySelectorAll("[data-nav-to]").forEach(b =>
    b.addEventListener("click", () => document.querySelector(`.nav-item[data-nav="${b.dataset.navTo}"]`)?.click()));
  // Faceted filters. Every change jumps back to page 1 (a new server query) so you're never
  // stranded on a page that no longer exists. Text inputs are debounced so we fetch once the
  // person pauses typing, not on every keystroke.
  const applyFilters = () => loadPage(1);
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
  function renderPreview(p) {
    p = p || {};
    const id = p.identity || {};
    const name = $(".cv-name");
    name.textContent = id.name || "Your Name";
    name.toggleAttribute("data-empty", !id.name);
    const contact = [id.address || id.city, id.phone, id.email, id.linkedin && "LinkedIn", id.github && "GitHub", id.blog && "Blog"]
      .filter(Boolean).join("  ·  ");
    const cc = $(".cv-contact");
    cc.textContent = contact || "city · email · links";
    cc.toggleAttribute("data-empty", !contact);

    const nproj = (p.projects || []).length;
    const body = $("#cvBody");
    body.innerHTML = "";
    if ((p.education || []).length) body.appendChild(section("Education", eduHTML(p)));
    if (Object.keys(p.skills || {}).length) body.appendChild(section("Skills", skillsHTML(p)));
    if (nproj) body.appendChild(section("Projects", projHTML(p)));
    if ((p.experience || []).length) body.appendChild(section("Experience", expHTML(p, nproj)));
    if ((p.extracurricular || []).length) body.appendChild(section("Extracurricular", extraHTML(p)));
    if (p.interests) body.appendChild(section("Additional Information",
      `<div class="cv-inline"><b>Interests:</b> ${esc(p.interests)}</div>`));
    wireFlags();
  }
  function section(title, inner) {
    const s = el("section", "cv-sec");
    s.innerHTML = `<div class="cv-sec-h">${title}</div><hr class="cv-rule"/>${inner}`;
    return s;
  }
  function eduHTML(p) {
    return (p.education || []).map((e, i) => `<div class="cv-block">
      <div class="cv-row"><span class="cv-org">${esc(e.school)}</span><span class="cv-loc">${esc(e.location)}</span></div>
      <div class="cv-row"><span class="cv-degree">${esc(e.degree)}</span><span class="cv-dates">${datePicker(e.date, `edu-${i}`, true, e.dates_placeholder)}</span></div>
      ${e.courses ? `<ul class="cv-ul"><li>Courses: ${esc(e.courses)}</li></ul>` : ""}</div>`).join("");
  }
  function skillsHTML(p) {
    return Object.entries(p.skills || {}).map(([k, v]) =>
      `<div class="cv-inline"><b>${esc(k)}:</b> ${esc(v)}</div>`).join("");
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
    const parts = String(str || "").split(/\s*[-, ]\s*/);
    const s = one(parts[0]); let e = { mon: "", yr: "" }, present = false;
    if (parts[1]) { if (/present|current|now|ongoing/i.test(parts[1])) present = true; else e = one(parts[1]); }
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
      <div class="cv-row"><span class="cv-org">${esc(x.org)}${x.suggested ? ' <span class="cv-sugg-badge">Suggested · replace before finalizing</span>' : ""}</span><span class="cv-loc">${esc(x.location || "")}</span></div>
      <div class="cv-row"><span class="cv-role">${esc(x.title)}</span><span class="cv-dates">${datePicker(x.dates, `proj-${gi}`, false, x.dates_placeholder)}</span></div>
      ${bulletList(x.bullets, `g${gi}-r0`)}</div>`).join("");
  }
  function expHTML(p, nproj) {
    return (p.experience || []).map((x, ei) => {
      const roles = (x.roles || [x]).map((r, ri) => {
        const title = r.title_suggested
          ? `<span class="cv-flag" data-role="${ei}-${ri}" data-sugg="${esc(r.title)}" data-orig="${esc(r.title_original || "")}">${esc(r.title)}</span>`
          : esc(r.title);
        return `<div class="cv-row"><span class="cv-role">${title}</span><span class="cv-dates">${datePicker(r.dates, `exp-${ei}-${ri}`, false, r.dates_placeholder)}</span></div>
                ${bulletList(r.bullets, `g${nproj + ei}-r${ri}`)}`;
      }).join("");
      return `<div class="cv-block"><div class="cv-row"><span class="cv-org">${esc(x.org)}</span><span class="cv-loc">${esc(x.location)}</span></div>${roles}</div>`;
    }).join("");
  }
  function extraHTML(p) {
    return (p.extracurricular || []).map((x, xi) => `<div class="cv-block">
      <div class="cv-row"><span class="cv-role">${esc(x.title)}</span><span class="cv-dates">${datePicker(x.date, `extra-${xi}`, false, x.dates_placeholder)}</span></div>
      ${bulletList(x.bullets, `x${xi}`)}</div>`).join("");
  }

  /* -------- bullet editing: persist on blur, then re-render the real PDF so the
     preview stays the source of truth (edit panel is left as-is to avoid disruption) */
  $("#cvBody").addEventListener("focusout", async (e) => {
    const li = e.target.closest("li[data-ref]");
    if (!li) return;
    try {
      const st = await api("/api/session/bullet", { ref: li.dataset.ref, text: li.textContent });
      if (st && st.pdf) { $("#pdfFrame").src = st.pdf + "&preview=1#toolbar=0&navpanes=0&view=FitH"; $("#downloadBtn").href = st.pdf; }
    } catch (err) { /* non-fatal for the preview */ }
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
    m.innerHTML = `<div class="msg-avatar">${role === "agent" ? "T" : "Y"}</div><div class="msg-bubble">${html}</div>`;
    chat.appendChild(m); chat.scrollTop = chat.scrollHeight; return m;
  }
  function working(label) {
    const m = el("div", "msg agent");
    m.innerHTML = `<div class="msg-avatar">T</div><div class="msg-bubble"><span class="spin"></span>${label || "Working…"}</div>`;
    chat.appendChild(m); chat.scrollTop = chat.scrollHeight; return m;
  }
  async function withWorking(fn, label) {
    const w = working(label);
    try { await fn(); } catch (e) { bubble("agent", `<span style="color:var(--flag)">${esc(e.message)}</span>`); }
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
  const chatText = (s) => esc(s)
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
    if (st.preview) renderPreview(st.preview);

    if (st.coverage) {
      $("#coverage").hidden = false;
      setCoverage(st.coverage.ratio);
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
          ? "Fit to one page, review and accept."
          : "Looks good? Accept to save it to your dashboard.";
      }
    }
  }

  // Honest pre-send check. The headline is the fabrication guard: any skill on the resume the
  // profile cannot back is called out first (it is the claim that costs an interview). Opportunities
  // and honest gaps ride along as guidance, and the one-page fit is noted.
  const REVIEW_VERDICT = { ready: ["Ready to send", "good"], review: ["Worth a look", "mid"],
    check: ["Check before sending", "low"] };
  function renderReview(r) {
    const el = $("#presendReview");
    if (!el) return;
    if (!r || !r.checks) { el.hidden = true; el.innerHTML = ""; return; }
    const v = REVIEW_VERDICT[r.verdict] || ["Pre-send check", "mid"];
    const rows = (r.checks || []).map(c => {
      const items = (c.items || []).length
        ? `<div class="ps-items">${c.items.map(t => `<span class="ps-term ps-${esc(c.level)}">${esc(t)}</span>`).join("")}</div>`
        : "";
      const icon = c.level === "flag" ? "!" : c.level === "warn" ? "~" : c.level === "pass" ? "✓" : "i";
      return `<div class="ps-row ps-lvl-${esc(c.level)}"><span class="ps-ic">${icon}</span>`
        + `<div class="ps-body"><div class="ps-label">${esc(c.label)}</div>${items}</div></div>`;
    }).join("");
    el.hidden = false;
    el.innerHTML =
      `<div class="ps-head ps-${esc(v[1])}"><span class="ps-verdict">${esc(v[0])}</span>`
      + `<span class="ps-headline">${esc(r.headline || "")}</span></div>${rows}`;
  }

  function renderCovDetail(cov) {
    const present = (cov.present || []).slice(0, 24);
    const missing = (cov.missing || []).slice(0, 20);
    $("#covDetail").innerHTML =
      `<span class="cd-lbl">In Resume</span>` + (present.map(t => `<span class="term hit">${esc(t)}</span>`).join("") || `<span class="term miss"></span>`) +
      `<span class="cd-lbl">Missing</span>` + (missing.map(t => `<span class="term miss">${esc(t)}</span>`).join("") || `<span class="term hit">none</span>`);
  }
  $("#covInfo").addEventListener("click", () => { $("#covDetail").hidden = !$("#covDetail").hidden; });

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
    resetBuilder();
    showView("builder");
    $("#jdInput").value = "";
    $("#jdGate").hidden = false;
    $("#jdInput").focus();
  }

  async function resumeBuilder() {
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
  $("#backBtn").addEventListener("click", () => { loadDashboard(); showView("dashboard"); });

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
    // Same card as the "Start from a template" strip, for one consistent look.
    $("#tplGrid").innerHTML = CV_TEMPLATES.map(cvsTplCard).join("")
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
    renderTplModal();
    $("#tplModal").hidden = false;
    document.addEventListener("keydown", tplModalKeys);
    $("#tplModalUse").focus();
  }
  function renderTplModal() {
    const t = CV_TEMPLATES[TPL_IDX];
    if (!t) return;
    $("#tplModalName").textContent = t.display_name || t.name;
    $("#tplModalFrame").src = t.preview_url + "#toolbar=0&navpanes=0&view=FitH";
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
      <div class="tpl-prev"><iframe class="tpl-preview" tabindex="-1" loading="lazy"
        title="${esc(t.display_name)} layout" src="${t.preview_url}#toolbar=0&navpanes=0&view=FitH"></iframe></div>
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
    sendTurn(val, esc(val).replace(/\n/g, "<br>"));
  });

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
      html += `<p class="prof-note">I'll ask for the rest (${missing.map(k => ID_LABELS[k].toLowerCase()).join(", ")}) if a resume needs it.</p>`;
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

  /* The sidebar-foot identity: initials + name, from the person's own profile. There is no
     account to sign into (BYO key, local-only), this is just "who this CV is for". */
  function paintAccount(p) {
    const name = (((p || {}).identity || {}).name || "").trim();
    const nameEl = $("#acctName"), avaEl = $("#acctAva");
    if (nameEl) nameEl.textContent = name || "Your profile";
    if (avaEl) {
      avaEl.textContent = name
        ? name.split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0]).join("")
        : "·";
    }
  }

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
  }

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

  /* ---------------------------------------------------------------- review & submit */
  /* ------------------------------------------------------------- inbox (§5)
     Read-first: only application mail (verifications + recruiter replies) comes
     back. Buttons are the consent: Complete verification visits ONE link; replies
     are drafted for YOU to send from your own mail app. SponsorJobs never sends. */
  async function loadInboxStatus() {
    const st = $("#inboxStatus"), btn = $("#inboxScan");
    try {
      const s = await api("/api/inbox/status");
      if (s.configured) {
        st.textContent = "Connected to your Gmail (read-only)." +
          (s.auto_verify ? " Autonomous verification is ON." : "");
        btn.disabled = false;
      } else if (!s.libs) {
        st.textContent = "Gmail support needs the Google client libraries: pip install google-api-python-client google-auth-oauthlib";
        btn.disabled = true;
      } else if (!s.creds) {
        st.textContent = "Not connected. Put your Google OAuth client file at " + s.creds_path + " (stays on this machine, git-ignored), then scan once to sign in.";
        btn.disabled = true;
      } else {
        st.textContent = "Almost there: first scan will open Google sign-in (read-only scope) and store the token locally.";
        btn.disabled = false;
      }
    } catch (e) {
      st.textContent = "Couldn't read inbox status.";
      btn.disabled = true;
    }
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
    if (!parts.length) parts.push(`<p class="prof-dim">Nothing application-related in the last scan (${r.scanned} messages checked, ${r.other} unrelated left untouched).</p>`);
    $("#inboxResults").innerHTML = parts.join("");

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
    btn.disabled = true; st.textContent = "Reading application mail (and drafting replies)…";
    try {
      const r = await api("/api/inbox/scan", {});
      st.textContent = `Scanned ${r.scanned} application-related message${r.scanned === 1 ? "" : "s"}.`;
      renderInbox(r);
    } catch (e) {
      st.textContent = e.message || "Scan failed.";
    } finally {
      btn.disabled = false;
    }
  });

  async function loadReview() {
    let s = {}, q = { pending: [], count: 0 };
    try { s = await api("/api/submit/settings"); } catch { /* keep defaults */ }
    try { q = await api("/api/review/queue"); } catch { /* keep empty */ }

    $("#rvwApproveAll").checked = !!s.approve_all;      // off by default
    const rate = s.rate || {};
    $("#rvwCap").textContent = rate.cap
      ? `${rate.remaining ?? rate.cap} of ${rate.cap} left today` : "";
    $("#rvwCount").textContent = q.count ? `${q.count} waiting` : "";
    $("#rvwTelegram").disabled = !s.telegram_configured;
    loadInboxStatus();
    $("#rvwTelegram").title = s.telegram_configured
      ? "Send this batch to your Telegram" : "Connect Telegram in Settings, Notifications, to enable";

    const empty = !q.pending.length;
    $("#rvwEmpty").hidden = !empty;
    $("#rvwList").innerHTML = q.pending.map(it => {
      const f = it.filled || {};
      const got = ["name", "email", "phone"].filter(k => f[k]);
      const laneCls = it.lane === "auto" ? "auto" : "assisted";
      return `<article class="rvw-card" data-id="${it.id}">
        <label class="rvw-pick"><input type="checkbox" class="rvw-item" checked data-id="${it.id}"></label>
        <div class="rvw-body">
          <div class="rvw-top">
            <h3>${esc(it.role || "Role")} · <span class="rvw-co">${esc(it.company || "Unknown")}</span></h3>
            <span class="pkg-tier-pill ${laneCls}">${it.lane === "auto" ? "Auto-submit" : "Assisted, your click"}</span>
          </div>
          <div class="rvw-meta">Filled: ${got.length ? got.join(", ") : "nothing yet"} ·
            ${it.screening_count} screening ${it.has_cover_letter ? "· cover letter ✓" : ""} ·
            ${it.coverage != null ? it.coverage + "% match" : ""}</div>
        </div>
        ${it.cv_url ? `<a class="rvw-cv" href="${it.cv_url}" target="_blank" rel="noopener">View Resume ↗</a>` : ""}
        ${it.lane !== "auto" && /^https?:\/\//i.test(it.url || "")
          ? `<button class="rvw-cv rvw-apply" type="button" data-apply="${esc(it.url)}"
                     data-cv="${esc(it.cv_path || "")}" data-id="${it.id}">Apply</button>`
          : ""}
      </article>`;
    }).join("");
  }

  // Apply means APPLY. The click is there so a person authorises each submission, not so
  // they can be shown a form they never asked to read: it fills, attaches the resume and
  // submits in one action. It stops short only where the site says no -- a captcha or a
  // login wall -- and then hands the open page back rather than trying to get past it.
  $("#rvwList")?.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-apply]");
    if (!btn) return;
    const { apply: url, cv, id } = btn.dataset;
    if (!window.tailorShell) { window.open(url, "_blank", "noopener"); return; }
    btn.disabled = true; btn.textContent = "Applying…";
    let r;
    try { r = await window.tailorShell.assistApply(url, cv || ""); } catch (err) { r = null; }
    btn.disabled = false; btn.textContent = "Apply";

    if (!r || r.ok === false) {
      rvwFlash(
        r && r.reason === "linkedin" ? "LinkedIn is browse-by-hand only, SponsorJobs never automates it."
        : r && r.reason === "no-profile" ? "No saved profile yet, build a resume once first."
        : "Couldn't open that application. It's in the Browser tab, apply by hand.");
      return;
    }
    if (r.submitted) {
      btn.textContent = r.confirmed ? "Applied ✓" : "Submitted";
      btn.disabled = true;
      api(`/api/record/${id}/status`, { status: "applied" }).catch(() => {});  // off the queue
      rvwFlash(r.confirmed
        ? "Submitted, and the site confirmed it."
        : "Submitted. No confirmation text on the page, so check the Browser tab once.");
      return;
    }
    // Not submitted: say WHY, in the site's terms, and leave the filled page open.
    rvwFlash(
      r.reason === "captcha" ? "This form uses a captcha, so it's yours to submit. "
        + "It's filled and open in the Browser tab."
      : r.reason === "login" ? "This site wants a sign-in first. Sign in in the Browser tab, "
        + "then use Fill the open page."
      : r.reason === "no-submit-button" ? "Filled it, but couldn't find the submit control, "
        + "so nothing was sent. Finish it in the Browser tab."
      : r.reason === "submit-covered" ? "Filled it, but something is covering the submit button"
        + (r.covered_by ? " (" + r.covered_by + ")" : "") + ", likely a cookie or consent "
        + "banner. Close it in the Browser tab and submit there."
      : "Filled it, but didn't submit. It's open in the Browser tab.");
  });

  function rvwSelected() {
    return [...document.querySelectorAll(".rvw-item:checked")].map(c => Number(c.dataset.id));
  }
  function rvwFlash(msg) {
    const m = $("#rvwMsg"); m.hidden = false; m.textContent = msg;
  }

  $("#rvwSelectAll")?.addEventListener("change", e => {
    document.querySelectorAll(".rvw-item").forEach(c => { c.checked = e.target.checked; });
  });
  $("#rvwApproveAll")?.addEventListener("change", async e => {
    try { await api("/api/submit/settings", { approve_all: e.target.checked }); }
    catch { e.target.checked = !e.target.checked; }
  });
  $("#rvwApprove")?.addEventListener("click", async () => {
    const ids = rvwSelected();
    if (!ids.length) { rvwFlash("Select at least one application to approve."); return; }
    try {
      const r = await api("/api/review/approve", { approve: ids });
      const ok = (r.approved || []).filter(a => a.ok).length;
      rvwFlash(`Approved ${ok}/${ids.length}. Auto-lane submits via the site’s API; assisted ones are marked for your on-site submit.`);
      loadReview();
    } catch (e) { rvwFlash("Couldn’t approve, " + e.message); }
  });
  $("#rvwTelegram")?.addEventListener("click", async () => {
    try {
      const r = await api("/api/review/telegram/send", {});
      rvwFlash(`Sent ${r.sent} application(s) to your Telegram. Tap Approve / Skip there, the app polls for your taps.`);
    } catch (e) { rvwFlash("Couldn’t send to Telegram, " + e.message); }
  });

  /* nav */
  document.querySelectorAll(".nav-item").forEach(n => n.addEventListener("click", () => {
    teardownCvi();   // leaving via the sidebar ends any in-progress live interview (no leaked meter or pane)
    document.querySelectorAll(".nav-item").forEach(x => x.classList.toggle("is-active", x === n));
    if (n.dataset.nav !== "jobs") stopFeedPoll();
    if (n.dataset.nav === "profile") { loadProfile(); showView("profile"); }
    else if (n.dataset.nav === "cvs") { loadCvs(); showView("cvs"); }
    else if (n.dataset.nav === "templates") { loadTemplates(); showView("templates"); }
    else if (n.dataset.nav === "jobs") { loadJobsFeed(); showView("jobs"); }
    else if (n.dataset.nav === "review") { loadReview(); showView("review"); }
    else if (n.dataset.nav === "autoapply") { loadAutoApply(); showView("autoapply"); }
    else if (n.dataset.nav === "projects") { loadProjects(); showView("projects"); }
    else if (n.dataset.nav === "prep") { loadPreps(); showView("prep"); }
    else { loadDashboard(); showView("dashboard"); }
  }));

  // ---------------------------------------------------------------- Projects (defend-your-work gate)
  // A project can't reach the CV until you pass a defense test proving you genuinely understand it.
  let PROJECTS = [], DEFEND_ID = null, DEFEND_QS = [], BUILD = null;
  async function loadProjects() {
    if ($("#projTest")) $("#projTest").hidden = true;
    if ($("#projBuild")) $("#projBuild").hidden = true;
    if ($("#projList")) $("#projList").hidden = false;
    try { const d = await api("/api/projects"); PROJECTS = d.projects || []; } catch { PROJECTS = []; }
    renderProjList();
  }
  function projCard(p) {
    const verified = p.status === "verified";
    const steps = p.plan?.milestones?.length || 0;
    const doneN = (p.done || []).length;
    const guideLabel = steps ? `Build guide (${doneN}/${steps})` : "Build guide";
    return `<article class="proj-card" data-id="${esc(p.id)}">
      <div class="proj-card-hd">
        <div class="proj-card-name">${esc(p.title)}
          <span class="proj-pill ${verified ? "ok" : ""}">${verified ? "&#10003; Verified" : "Draft"}</span></div>
        ${p.tech ? `<div class="proj-card-tech">${esc(p.tech)}</div>` : ""}
        ${p.summary ? `<div class="proj-card-sum">${esc(p.summary)}</div>` : ""}
      </div>
      <div class="proj-card-acts">
        ${verified
          ? `<button class="btn btn-primary btn-sm" data-act="tocv">Add to Resume</button>
             ${p.repo_url
               ? `<a class="btn btn-ghost btn-sm" href="${esc(p.repo_url)}" target="_blank" rel="noopener">View on GitHub</a>`
               : `<button class="btn btn-ghost btn-sm" data-act="publish">Publish to GitHub</button>`}`
          : `<button class="btn btn-ghost btn-sm" data-act="guide">${esc(guideLabel)}</button>
             <button class="btn btn-primary btn-sm" data-act="defend">Prove you understand it</button>`}
        <button class="btn btn-ghost btn-sm" data-act="delete">Delete</button>
      </div>
    </article>`;
  }
  function renderProjList() {
    const root = $("#projList"); if (!root) return;
    if (!PROJECTS.length) {
      root.innerHTML = `<div class="empty">No projects yet. Add one, then prove you can defend it, that is what makes it resume-ready.</div>`;
      return;
    }
    root.innerHTML = PROJECTS.map(projCard).join("");
    root.querySelectorAll(".proj-card").forEach(card => {
      const p = PROJECTS.find(x => x.id === card.dataset.id);
      card.querySelector('[data-act="defend"]')?.addEventListener("click", () => startDefend(p));
      card.querySelector('[data-act="guide"]')?.addEventListener("click", () => startBuildGuide(p));
      card.querySelector('[data-act="tocv"]')?.addEventListener("click", () => addToCv(p));
      card.querySelector('[data-act="publish"]')?.addEventListener("click", (e) => publishToGithub(p, e.target));
      card.querySelector('[data-act="delete"]')?.addEventListener("click", async () => {
        await fetch(`/api/projects/${p.id}`, { method: "DELETE" }); loadProjects();
      });
    });
  }
  $("#projNew")?.addEventListener("click", () => { const f = $("#projForm"); if (f) { f.hidden = !f.hidden; if (!f.hidden) $("#projTitle")?.focus(); } });
  $("#projCancel")?.addEventListener("click", () => { const f = $("#projForm"); if (f) { f.reset(); f.hidden = true; } });
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

  // Suggest-a-project: ideas grounded in the saved profile + a target role. Picking one
  // prefills the create form so it becomes a draft that still has to pass the defend gate.
  $("#projSuggestBtn")?.addEventListener("click", () => {
    const f = $("#projSuggestForm"); if (!f) return;
    $("#projForm") && ($("#projForm").hidden = true);
    f.hidden = !f.hidden; if (!f.hidden) $("#projTarget")?.focus();
  });
  $("#projSuggestCancel")?.addEventListener("click", () => {
    const f = $("#projSuggestForm"); if (f) { f.reset(); f.hidden = true; const l = $("#projSuggestList"); if (l) l.innerHTML = ""; }
  });
  $("#projSuggestForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#projSuggestMsg"), setMsg = (t, cls) => { if (msg) { msg.textContent = t; msg.className = "proj-msg " + cls; msg.hidden = !t; } };
    const list = $("#projSuggestList"), go = $("#projSuggestGo");
    const target = $("#projTarget")?.value.trim() || "";
    setMsg("", ""); if (list) list.innerHTML = `<div class="empty">Thinking of projects that fit you…</div>`;
    if (go) { go.disabled = true; go.textContent = "Thinking…"; }
    try {
      const d = await api("/api/projects/suggest", { target });
      renderSuggestions(d.suggestions || []);
    } catch (err) {
      if (list) list.innerHTML = "";
      setMsg(err.message || "Couldn't suggest projects right now.", "err");
    } finally { if (go) { go.disabled = false; go.textContent = "Suggest projects"; } }
  });
  function renderSuggestions(items) {
    const list = $("#projSuggestList"); if (!list) return;
    if (!items.length) { list.innerHTML = `<div class="empty">No suggestions came back. Try naming the target role more specifically.</div>`; return; }
    list.innerHTML = items.map((s, i) => `
      <article class="proj-sug" data-i="${i}">
        <div class="proj-sug-hd">
          <h3 class="proj-sug-t">${esc(s.title || "")}</h3>
          ${s.skill ? `<span class="proj-sug-skill">${esc(s.skill)}</span>` : ""}
        </div>
        ${s.summary ? `<p class="proj-sug-sum">${esc(s.summary)}</p>` : ""}
        ${s.tech ? `<p class="proj-sug-tech">${esc(s.tech)}</p>` : ""}
        ${s.why ? `<p class="proj-sug-why">${esc(s.why)}</p>` : ""}
        <button class="btn btn-primary btn-sm" data-build type="button">Build this</button>
      </article>`).join("");
    list.querySelectorAll(".proj-sug").forEach(card => {
      const s = items[+card.dataset.i];
      card.querySelector("[data-build]")?.addEventListener("click", () => buildSuggestion(s));
    });
  }
  function buildSuggestion(s) {
    if (!s) return;
    if ($("#projTitle")) $("#projTitle").value = s.title || "";
    if ($("#projSummary")) $("#projSummary").value = s.summary || "";
    if ($("#projTech")) $("#projTech").value = s.tech || "";
    const sf = $("#projSuggestForm"); if (sf) sf.hidden = true;
    const f = $("#projForm"); if (f) f.hidden = false;
    $("#projTitle")?.focus();
    toast("Prefilled. Build it for real, then prove you can defend it.");
  }

  // Guided build-and-teach loop: an ordered plan that teaches as you build, with a per-step coach.
  async function startBuildGuide(p) {
    if (!p) return;
    BUILD = { id: p.id, title: p.title, milestones: p.plan?.milestones || [], done: new Set(p.done || []) };
    $("#projBuildTitle").textContent = `Build: ${p.title}`;
    $("#projList").hidden = true; $("#projForm").hidden = true; $("#projSuggestForm").hidden = true; $("#projBuild").hidden = false;
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
    if (btn) { btn.disabled = true; btn.textContent = "Publishing…"; }
    try {
      const d = await api(`/api/projects/${p.id}/publish`, {});   // {} body -> POST
      toast("Published to GitHub.");
      loadProjects();   // re-render: the card now shows "View on GitHub"
    } catch (err) {
      toast(err.message || "Couldn't publish to GitHub.");
      if (btn) { btn.disabled = false; btn.textContent = "Publish to GitHub"; }
    }
  }

  // ---------------------------------------------------------------- Interview prep (pre-interview)
  // Two rounds, two entry paths (approved 2026-10-02). Round 1 is a HireVue-style one-way recorded
  // interview, scored locally. Round 2 is one live interview with the Tavus interviewer. The person
  // can run the full mock (Round 1, then Round 2) or either round on its own.
  const T = (k, f) => I18N.t(k, f);
  let PREPS = [], PREP_APP = null, GUIDED = false, PREP_STEP = 1;
  async function loadPreps() {
    if ($("#prepScreen")) $("#prepScreen").hidden = true;
    if ($("#prepLive")) $("#prepLive").hidden = true;
    if ($("#prepList")) $("#prepList").hidden = false;
    if ($("#prepTilesMsg")) $("#prepTilesMsg").hidden = true;
    GUIDED = false; hideGuidedProgress();   // returning to the entry ends any guided flow
    try { const d = await api("/api/preps"); PREPS = d.preps || []; } catch { PREPS = []; }
    // Simple manual entry: a fresh role + JD + CV form on every visit. Each visit goes through
    // step 1, so there's no "change the role" affordance on step 2, you just re-enter here.
    PREP_APP = { open: true, role: "", jd: "", cv: "", cvName: "" };
    showPrepStep(1);           // always land on step 1 (which role) when entering prep
    renderPrepEntry();
    renderStories();           // the STAR story bank stays on the landing
    const sug = $("#storySuggestions"); if (sug) { sug.hidden = true; sug.innerHTML = ""; }
    paintRound2Tile();         // the Round 2 tile says when a live interviewer key is missing
  }
  // Two-step entry: step 1 picks the role (it shapes the whole mock), step 2 picks the path.
  function showPrepStep(n) {
    PREP_STEP = n;
    if ($("#prepStep1")) $("#prepStep1").hidden = n !== 1;
    if ($("#prepStep2")) $("#prepStep2").hidden = n !== 2;
  }

  // Step 1: a simple manual form. Type the role you're interviewing for, paste the job description,
  // attach the CV you used, then Continue.
  function renderPrepEntry() {
    const ctx = $("#prepCtx"); if (!ctx) return;
    if (!(PREP_APP && PREP_APP.open)) PREP_APP = { open: true, role: "", jd: "", cv: "", cvName: "" };
    ctx.innerHTML = "";                 // no picker; the fields render below in #prepOpen
    renderPrepOpen();
    wirePrepContinue();
  }
  // Step 1 "Continue": validate the role is chosen, then reveal the path choices (step 2).
  function wirePrepContinue() {
    const btn = $("#prepContinue"); if (!btn) return;
    btn.onclick = () => {
      const msg = $("#prepStep1Msg");
      const showMsg = (t) => { if (msg) { msg.textContent = t; msg.hidden = false; } };
      if (PREP_APP && PREP_APP.open) {
        if (!(PREP_APP.role || "").trim()) { showMsg(T("prep.needRole", "Type the role you're interviewing for.")); $("#prepOpenRole")?.focus(); return; }
        if (!(PREP_APP.jd || "").trim()) { showMsg(T("prep.needJd", "Paste the job description so we can build the interview.")); $("#prepOpenJd")?.focus(); return; }
      } else if (!PREP_APP) { showMsg(T("prep.pickRole", "Pick a role.")); return; }
      if (msg) msg.hidden = true;
      showPrepStep(2); renderPrepStep2();
    };
  }
  // Step 2: confirm the chosen role, and enable the path choices.
  function renderPrepStep2() {
    const box = $("#prepChosen");
    if (box) {
      const label = prepRole(PREP_APP) || T("prep.yourRole", "Your role");
      box.innerHTML = `<span class="prep-chosen-lab">${esc(T("prep.interviewingFor", "Interviewing for"))}</span>
        <span class="prep-chosen-role">${esc(label)}</span>`;
    }
    setTilesEnabled(true);
    if ($("#prepTilesMsg")) $("#prepTilesMsg").hidden = true;
  }
  // The intake inputs: role + JD + the CV you used.
  function renderPrepOpen() {
    const box = $("#prepOpen"); if (!box) return;
    if (!(PREP_APP && PREP_APP.open)) PREP_APP = { open: true, role: "", jd: "", cv: "", cvName: "" };
    box.hidden = false;
    box.innerHTML = `<input class="fld" id="prepOpenRole" type="text" autocomplete="off" value="${esc(PREP_APP.role || "")}" placeholder="${esc(T("prep.rolePh", "Role you're interviewing for, e.g. Data Analyst"))}">
      <textarea class="fld" id="prepOpenJd" rows="4" placeholder="${esc(T("prep.jdPh", "Paste the job description"))}">${esc(PREP_APP.jd || "")}</textarea>
      <div class="prep-open-cv">
        <label class="btn btn-ghost btn-sm prep-open-attach"><input type="file" id="prepOpenCvFile" accept=".pdf,.docx,.doc,.txt,.png,.jpg,.jpeg" hidden> ${esc(T("prep.attachCv", "Attach the resume you used"))}</label>
        <span class="prep-open-cvname" id="prepOpenCvName">${PREP_APP.cvName ? esc(PREP_APP.cvName) : esc(T("prep.cvHint", "PDF, DOCX, or an image. Optional, it sharpens the questions."))}</span>
      </div>`;
    $("#prepOpenRole")?.addEventListener("input", (e) => { PREP_APP.role = e.target.value; });
    $("#prepOpenJd")?.addEventListener("input", (e) => { PREP_APP.jd = e.target.value; });
    $("#prepOpenCvFile")?.addEventListener("change", async (e) => {
      const f = e.target.files && e.target.files[0]; if (!f) return;
      const nameEl = $("#prepOpenCvName"); if (nameEl) nameEl.textContent = T("prep.reading", "Reading") + " " + f.name + "...";
      const fd = new FormData(); fd.append("file", f);
      try {
        const r = await fetch("/api/preps/attach-cv", { method: "POST", body: fd });
        const d = await r.json(); if (!r.ok) throw new Error(d.error || T("prep.cantRead", "Couldn't read that file."));
        PREP_APP.cv = d.text || ""; PREP_APP.cvName = d.name || f.name;
        if (nameEl) nameEl.textContent = PREP_APP.cvName + (PREP_APP.cv ? "" : " " + T("prep.noText", "(no readable text found)"));
      } catch (err) { if (nameEl) nameEl.textContent = err.message || T("prep.cantRead", "Couldn't read that file."); }
    });
  }
  function setTilesEnabled(on) {
    ["#tileRound1", "#tileRound2", "#prepFullMock"].forEach(s => { const b = $(s); if (b) { b.disabled = !on; b.classList.toggle("is-disabled", !on); } });
  }
  // A quiet note on the Round 2 tile when no live interviewer is set up. The tile still works:
  // it leads to the gate screen with the free-key steps.
  async function paintRound2Tile() {
    const st = await round2Status();
    const el = $("#tileRound2Note"); if (!el) return;
    el.hidden = !st || st.available;
    el.textContent = st && st.reason === "no_minutes"
      ? T("r2.tileNoMinutes", "Interviews used up") : T("r2.tileNoKey", "Needs a pass or a free Tavus key");
  }
  // Path click: build a prep for the role, then start the chosen round.
  async function startRoundFromTile(which) {
    const msg = $("#prepTilesMsg");
    const show = (t) => { if (msg) { msg.textContent = t; msg.hidden = false; } };
    if (!PREP_APP) { show(T("prep.pickRole", "Pick a role.")); return; }
    const role = (PREP_APP.role || "").trim();
    if (!role) { show(T("prep.needRole", "Type the role you're interviewing for.")); $("#prepOpenRole")?.focus(); return; }
    const body = { role, jd: (PREP_APP.jd || "").trim(), cv: PREP_APP.cv || "" };
    setTilesEnabled(false); show(T("prep.preparing", "Preparing your questions..."));
    let prep = null;
    try { prep = (await api("/api/preps", body)).prep; }
    catch (err) { show(err.message || T("prep.cantPrepare", "Couldn't prepare questions right now.")); }
    setTilesEnabled(true);
    if (!prep) return;
    if (msg) msg.hidden = true;
    if (GUIDED) showGuidedProgress(which);
    if (which === 1) startScreen(prep); else startRound2(prep.id, null, true);
  }
  $("#tileRound1")?.addEventListener("click", () => startRoundFromTile(1));
  $("#tileRound2")?.addEventListener("click", () => startRoundFromTile(2));
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
  $("#prepFullMock")?.addEventListener("click", () => { GUIDED = true; startRoundFromTile(1); });
  async function renderStories() {
    const list = $("#storyList"); if (!list) return;
    let stories = [];
    try { stories = (await api("/api/stories")).stories || []; } catch { stories = []; }
    list.innerHTML = stories.map(s => {
      const tags = (s.competencies || []).map(cbn => `<span class="scr-comp">${esc(cbn)}</span>`).join("");
      const star = [["S", s.situation], ["T", s.task], ["A", s.action], ["R", s.result]]
        .filter(([, v]) => (v || "").trim())
        .map(([k, v]) => `<div class="scr-star-row"><span class="scr-star-k">${k}</span>${esc(v)}</div>`).join("");
      return `<details class="story-card" data-sid="${esc(s.id)}"><summary><b>${esc(s.title)}</b> ${tags}</summary>
        <div class="story-body">${star || `<span class="scr-note">No STAR detail yet.</span>`}
          <div class="scr-acts"><button class="btn btn-danger-ghost btn-sm story-del" type="button">Delete</button></div></div></details>`;
    }).join("");
    list.querySelectorAll(".story-del").forEach(btn => btn.addEventListener("click", async (e) => {
      const sid = e.target.closest(".story-card")?.dataset.sid; if (!sid) return;
      if (!confirm("Delete this story?")) return;
      try { await fetch(`/api/stories/${sid}`, { method: "DELETE" }); } catch (_) {}
      renderStories();
    }));
  }
  $("#storyAddBtn")?.addEventListener("click", () => {
    const f = $("#storyForm"); if (f) { f.hidden = !f.hidden; if (!f.hidden) $("#storyTitle")?.focus(); }
  });

  // Beat the blank page: draft candidate STAR stories from the person's REAL profile. They review,
  // edit, and keep the ones they like; nothing is saved until they say so, and nothing is invented.
  $("#storySuggestBtn")?.addEventListener("click", async () => {
    const btn = $("#storySuggestBtn"), label = btn.textContent;
    btn.disabled = true; btn.textContent = "Reading your experience…";
    try {
      const r = await api("/api/stories/suggest", {});
      renderStorySuggestions(r.suggestions || [], r.note || "");
    } catch (e) { toast(e.message || "Couldn't draft stories right now."); }
    finally { btn.disabled = false; btn.textContent = label; }
  });
  function renderStorySuggestions(sug, note) {
    const box = $("#storySuggestions"); if (!box) return;
    box.hidden = false;
    if (!sug.length) {
      box.innerHTML = `<div class="empty">${esc(note || "No drafts yet. Add your experience to your profile, then try again.")}</div>`;
      return;
    }
    box.innerHTML = `<div class="sb-suggest-lead">Drafts from your real experience. Edit and keep the ones you like:</div>`
      + sug.map((s, i) => {
        const tags = (s.competencies || []).map(c => `<span class="scr-comp">${esc(c)}</span>`).join("");
        const star = [["S", s.situation], ["T", s.task], ["A", s.action], ["R", s.result]]
          .filter(([, v]) => (v || "").trim())
          .map(([k, v]) => `<div class="scr-star-row"><span class="scr-star-k">${k}</span>${esc(v)}</div>`).join("");
        return `<div class="sb-sug-card" data-i="${i}">
          <div class="sb-sug-hd"><b>${esc(s.title)}</b> ${tags}</div>
          <div class="sb-sug-body">${star || `<span class="scr-note">A starting point, add the details before you save.</span>`}</div>
          <div class="scr-acts"><button class="btn btn-primary btn-sm sb-sug-add" type="button">Add to bank</button>
            <button class="btn btn-ghost btn-sm sb-sug-dismiss" type="button">Dismiss</button></div>
        </div>`;
      }).join("");
    box.querySelectorAll(".sb-sug-add").forEach(b => b.addEventListener("click", async (e) => {
      const card = e.target.closest(".sb-sug-card"); const s = sug[Number(card.dataset.i)];
      try { await api("/api/stories", s); card.remove(); renderStories(); toast("Saved to your bank."); }
      catch (err) { toast(err.message || "Couldn't save that story."); }
    }));
    box.querySelectorAll(".sb-sug-dismiss").forEach(b =>
      b.addEventListener("click", (e) => e.target.closest(".sb-sug-card")?.remove()));
  }
  $("#storyCancel")?.addEventListener("click", () => { $("#storyForm").reset(); $("#storyForm").hidden = true; });
  $("#storyStructure")?.addEventListener("click", async () => {
    const text = $("#storyRough")?.value.trim();
    if (!text) { toast("Paste a few rough sentences first."); return; }
    const btn = $("#storyStructure"); btn.disabled = true; btn.textContent = "Structuring…";
    try {
      const star = (await api("/api/stories/structure", { text })).star || {};
      if (star.situation) $("#storyS").value = star.situation;
      if (star.task) $("#storyT").value = star.task;
      if (star.action) $("#storyA").value = star.action;
      if (star.result) $("#storyR").value = star.result;
    } catch (err) { toast(err.message || "Couldn't structure that."); }
    finally { btn.disabled = false; btn.textContent = "Structure into STAR"; }
  });
  $("#storyForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#storyMsg"), setMsg = (t) => { if (msg) { msg.textContent = t; msg.className = "proj-msg err"; msg.hidden = false; } };
    const title = $("#storyTitle")?.value.trim() || "";
    if (!title) return setMsg("Give the story a short title.");
    const competencies = ($("#storyComp")?.value || "").split(",").map(x => x.trim()).filter(Boolean);
    try {
      await api("/api/stories", {
        title, situation: $("#storyS")?.value.trim() || "", task: $("#storyT")?.value.trim() || "",
        action: $("#storyA")?.value.trim() || "", result: $("#storyR")?.value.trim() || "", competencies,
      });
      $("#storyForm").reset(); $("#storyForm").hidden = true; renderStories();
    } catch (err) { setMsg(err.message || "Couldn't save the story."); }
  });

  // ---------------------------------------------------------------- Round 1: one-way recorded interview
  // Mirrors the candidate flow of a HireVue-style one-way video interview, screen by screen:
  // landing, notice, device check, unlimited practice, the real questions, "All done", the report.
  // Screens 3 to 6 sit on a plain light surface like the real thing (.r1-light). Recordings stay on
  // this computer; there is no facial analysis, ever.
  const R1_QUESTIONS = 5;
  let SCREEN = null, SCR_PREP = null, SCR_EXTRA = false;
  let SCR_STREAM = null, SCR_REC = null, SCR_CHUNKS = [], SCR_TIMER = null, SCR_PREPT = null, SCR_COUNT = null;
  let SCR_PRACTICE = null, SCR_UPLOADS = {}, SCR_TR = {}, SCR_RETAKES = {}, SCR_PHASE = "idle", SCR_CUR = null;
  const fmtClock = (x) => `${Math.floor(x / 60)}:${String(x % 60).padStart(2, "0")}`;
  function startScreen(prep) {
    SCR_PREP = prep; SCREEN = null; SCR_EXTRA = false; SCR_UPLOADS = {}; SCR_TR = {}; SCR_RETAKES = {}; SCR_PRACTICE = null;
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
        <p class="scr-welcome-sub">${esc(T("r1.landingFacts", "5 questions, about 20 minutes."))} ${esc((k => k ? k.charAt(0).toUpperCase() + k.slice(1) : "")(r1Kinds(p)))}.</p>
        <div class="scr-welcome-acts">
          <button class="btn btn-primary" id="scrBegin" type="button">${esc(T("r1.getStarted", "Get started"))}</button>
          <button class="btn btn-ghost" id="scrBail" type="button">${esc(T("r1.notNow", "Not now"))}</button>
        </div>
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
      try { SCREEN = await api("/api/screens", { prep_id: SCR_PREP.id, extra_time: SCR_EXTRA, questions: R1_QUESTIONS }); }
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
    uploadTake(idx, blob);
    const left = retakesLeft(idx);
    const help = $("#r1Help"); if (help) help.textContent = T("r1.saved", "Saved on this computer.");
    $("#r1Acts").innerHTML = `
      ${left > 0 ? `<button class="r1-btn r1-btn-ghost" id="r1Retake" type="button">${esc(T("r1.retake", "Retake ({n} left)").replace("{n}", String(left)))}</button>` : ""}
      <button class="r1-btn r1-btn-primary" id="r1Next" type="button">${esc(last ? T("r1.finish", "Finish interview") : T("r1.next", "Next question"))}</button>`;
    $("#r1Retake")?.addEventListener("click", () => { SCR_RETAKES[idx] = Math.max(0, left - 1); renderR1Question({}); });
    $("#r1Next")?.addEventListener("click", () => { if (last) renderR1Done(); else { SCREEN.i = idx + 1; renderR1Question({}); } });
  }
  function uploadTake(idx, blob) {
    const fd = new FormData(); fd.append("file", blob, `answer_${idx}.webm`);
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
      try { for (const t of tas) await api(`/api/screens/${SCREEN.id}/answers/${t.dataset.i}/transcript`, { text: t.value.trim() }); }
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
    const passed = !!r.passed, cls = passed ? "ok" : "no";
    const answers = r.answers || [];
    scrHead(T("r1.reportCount", "Round 1 report"), "");
    $("#scrStage").innerHTML = `
      <div class="scr-report">
        <div class="scr-score ${cls}">${r.score ?? 0}<span>/100</span></div>
        <div class="mock-ready ${cls}">${esc(passed ? T("r1.passed", "Passed, the bar is 70") : T("r1.failed", "Below the bar of 70"))}</div>
        ${r.why ? `<p class="mock-ready-sum">${esc(r.why)}</p>` : ""}
        ${(r.improvements || []).length ? `<div class="mock-col"><div class="prep-fb-h">${esc(T("r1.doNext", "Do this next"))}</div><ul>${r.improvements.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
        ${(r.competencies || []).length ? `<div class="scr-comps"><div class="prep-fb-h">${esc(T("r1.byCompetency", "By competency"))}</div>
          ${r.competencies.map(cb => `<div class="scr-comp-row"><span class="scr-comp-name">${esc(cb.name)}</span><span class="scr-comp-bar"><i class="scr-comp-fill ${cb.score < 60 ? "low" : cb.score < 75 ? "mid" : "high"}" style="width:${Math.max(4, Math.min(100, Number(cb.score) || 0))}%"></i></span><span class="scr-comp-val">${cb.score}</span></div>`).join("")}</div>` : ""}
        <div class="scr-per">${answers.map((a, k) => {
          const i = a.i ?? k, fb = a.feedback || (a.per_answer_feedback || {}).feedback || "", sc = a.score ?? (a.per_answer_feedback || {}).score;
          const qtext = a.text || a.q || a.question || ((SCREEN.questions || [])[i] || {}).text || "";
          return `<div class="scr-per-item"><div class="scr-per-hd"><span class="scr-per-q">${k + 1}. ${esc(qtext)}</span>${sc != null ? `<span class="scr-per-score">${sc}/100</span>` : ""}</div>
            ${fb ? `<p>${esc(fb)}</p>` : ""}
            ${deliveryReadout(a.delivery_metrics || a.delivery)}
            ${a.transcript ? `<details class="scr-tr"><summary>${esc(T("r1.transcriptSum", "Your answer, with filler and hedging highlighted"))}</summary><p class="scr-tr-body">${highlightDisfluencies(a.transcript)}</p></details>` : ""}
            <button class="btn btn-ghost btn-sm scr-coach-btn" data-coach="${i}" type="button">${esc(T("r1.stronger", "See a stronger version"))}</button>
            <div class="scr-coach" id="scrCoach${i}" hidden></div></div>`; }).join("")}</div>
        <p class="scr-disclaimer">${esc(r.disclaimer || T("r1.reportDisclaimer", "Practice feedback, not a hiring decision."))}</p>
        <div id="r1Gate"></div>
        <div class="mock-next">
          <button class="btn btn-ghost btn-sm" id="scrReport" type="button">${esc(T("r1.docx", "Download report (.docx)"))}</button>
          <button class="btn btn-ghost btn-sm" id="scrDone" type="button">${esc(T("r1.backToPrep", "Back to prep"))}</button>
          <button class="btn btn-danger-ghost btn-sm" id="scrDelete" type="button">${esc(T("r1.deleteRec", "Delete recordings"))}</button>
        </div>
      </div>`;
    $("#scrStage").querySelectorAll(".scr-coach-btn").forEach(btn =>
      btn.addEventListener("click", () => coachScreenAnswer(SCREEN.id, btn.dataset.coach, btn)));
    $("#scrReport")?.addEventListener("click", () => { window.location = `/api/screens/${SCREEN.id}/report.docx`; });
    $("#scrDone")?.addEventListener("click", quitScreen);
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
      box.innerHTML = `<div class="scr-unlock"><b>${esc(T("r1.unlocked", "Round 2 unlocked."))}</b> ${esc(T("r1.unlockedD", "A live interview with the Tavus interviewer, 15 minutes."))}
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
    stopPrepClock(); stopAnswerClock(); if (SCR_COUNT) { clearInterval(SCR_COUNT); SCR_COUNT = null; }
    if (SCR_REC && SCR_REC.state === "recording") { SCR_REC.onstop = null; try { SCR_REC.stop(); } catch {} }
    SCR_PHASE = "idle"; stopScrStream();
    $("#prepScreen").hidden = true; $("#prepList").hidden = false; loadPreps();
  }
  $("#scrQuit")?.addEventListener("click", quitScreen);

  // ---------------------------------------------------------------- Round 2: live interview (Tavus)
  // One 15-minute conversation with the Tavus interviewer, in the app's own video pane. It runs on
  // the paid plan first (company key, held by the broker), then the person's own Tavus key.
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
  // The gate: Round 2 needs a live interviewer. The free-key steps, and a way to finish here.
  // When a paid plan's interviews are used up: buy a prepaid pack, use your own key, or finish.
  function r2PackLabel(p) {
    const n = Number(p.interviews) || 1;
    const label = n === 1 ? T("r2.buyOne", "Buy 1 interview") : T("r2.buyMany", "Buy {n} interviews").replace("{n}", String(n));
    return p.price_label ? `${label}, ${p.price_label}` : label;
  }
  function r2GateHtml(st, mode) {
    const noMin = st && st.reason === "no_minutes";
    if (noMin) {
      const packs = (st.can_buy && Array.isArray(st.packs)) ? st.packs : [];
      return `<div class="${mode === "inline" ? "scr-unlock r2-gate-inline" : "scr-intro r2-gate"}">
      <div class="r2-gate-main">
        <h3 class="r2-gate-h">${esc(T("r2.usedUpH", "You've used your pass's live interviews."))}</h3>
        <p class="r2-gate-d">${esc(packs.length ? T("r2.usedUpD", "Buy more, they never expire. Or use your own Tavus key.") : T("r2.usedUpNoBuy", "Add another pass, or use your own Tavus key."))}</p>
        <div class="scr-welcome-acts">
          ${packs.map((p, i) => `<button class="btn ${i === 0 ? "btn-primary" : "btn-ghost"} btn-sm r2-buy" data-pack="${esc(p.id)}" type="button">${esc(r2PackLabel(p))}</button>`).join("")}
          <button class="btn btn-ghost btn-sm" id="r2OpenSettings" type="button">${esc(T("r2.useOwnKey", "Use my own Tavus key"))}</button>
          <button class="btn btn-ghost btn-sm" id="r2Finish" type="button">${esc(T("r2.finishHere", "Finish here"))}</button>
        </div>
        <p class="r2-gate-d" id="r2BuyMsg" hidden></p>
      </div></div>`;
    }
    const steps = `<ol class="r2-steps">
        <li>${esc(T("r2.step1", "Create a free account at platform.tavus.io. The free tier gives 25 minutes a month, no card."))}</li>
        <li>${esc(T("r2.step2", "Create an API key there and copy it."))}</li>
        <li>${esc(T("r2.step3", "Paste it in Settings, under Live interviewer (Tavus)."))}</li>
      </ol>`;
    return `<div class="${mode === "inline" ? "scr-unlock r2-gate-inline" : "scr-intro r2-gate"}">
      <div class="r2-gate-main">
        <h3 class="r2-gate-h">${esc(T("r2.gateH", "Round 2 needs a live interviewer"))}</h3>
        <p class="r2-gate-d">${esc(noMin ? T("r2.gateNoMinutes", "Your Tavus minutes for this month are used up. They reset monthly.") : T("r2.gateNoKey", "The live interviewer runs on Tavus with your own free key."))}</p>
        ${noMin ? "" : steps}
        ${st && st.offer === "passes" ? `<p class="r2-gate-d">${esc(T("r2.passOffer", "Or get a pass: live interviews on our interviewer, paid once, no auto-renew."))}</p>` : ""}
        <div class="scr-welcome-acts">
          ${noMin ? "" : `<button class="btn btn-primary btn-sm" id="r2OpenSettings" type="button">${esc(T("r2.openSettings", "Open Settings"))}</button>`}
          ${st && st.offer === "passes" ? `<button class="btn btn-ghost btn-sm" id="r2SeePasses" type="button">${esc(T("r2.seePasses", "See passes"))}</button>` : ""}
          <a class="btn btn-ghost btn-sm" href="https://platform.tavus.io" target="_blank" rel="noopener">${esc(T("r2.getKey", "Get a free key"))}</a>
          <button class="btn btn-ghost btn-sm" id="r2Finish" type="button">${esc(T("r2.finishHere", "Finish here"))}</button>
        </div>
      </div></div>`;
  }
  function wireR2Gate(onFinish) {
    $("#r2OpenSettings")?.addEventListener("click", async () => { await openSettings(); document.querySelector('[data-set="ai"]')?.click(); });
    $("#r2Finish")?.addEventListener("click", onFinish);
    $("#r2SeePasses")?.addEventListener("click", () => openUpgrade());
    document.querySelectorAll(".r2-buy").forEach(b => b.addEventListener("click", () => buyInterviews(b.dataset.pack, b)));
  }
  // Buy a prepaid interview pack: the broker mints a Stripe Checkout, we open it, and the
  // credits land once Stripe confirms. "Check again" re-reads the status without a restart.
  async function buyInterviews(pack, btn) {
    const msg = $("#r2BuyMsg");
    const say = (t) => { if (msg) { msg.textContent = t; msg.hidden = false; } };
    if (btn) btn.disabled = true;
    try {
      const d = await api("/api/interview/round2/buy", { pack });
      if (window.tailorShell) window.tailorShell.openBrowser(d.url);
      else fetch("/api/open?u=" + encodeURIComponent(d.url)).catch(() => window.open(d.url, "_blank"));
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
    } catch (err) {
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
          <p class="zoom-gr-sub">${esc(T("r2.greenSub", "15 minutes with a live interviewer. They ask, you answer, and they follow up like a real interview."))}</p>
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
  function renderCviStage(s) {
    const total = Math.max(1, Number(s.max_minutes || 15)) * 60;
    stopLiveStream();   // Tavus runs its own camera/mic; free the green-room preview stream
    $("#liveStage").innerHTML =
      `<div class="zoom-cvi">
        <div class="zoom-topbar">
          <span class="zoom-title">${esc(T("r2.title", "Round 2, live interview"))}</span>
          <span class="zoom-live"><i></i> LIVE</span>
          <span class="zoom-meta">${esc(T("r2.onScreen", "Your interviewer is on screen"))}</span>
        </div>
        <p class="zoom-cvi-note">${esc(T("r2.speak", "Speak naturally. They will ask and follow up just like a real interview."))}</p>
        <div class="zoom-controls zoom-cvi-controls">
          <span class="zoom-mins">${esc(T("r2.timeLeft", "Time left"))}: <b id="cviMins">${fmtClock(total)}</b></span>
          <button class="zoom-ctrl zoom-leave" id="cviEnd" type="button">${esc(T("r2.end", "End interview"))}</button>
        </div>
      </div>`;
    $("#cviEnd")?.addEventListener("click", () => endCvi());
    if (window.tailorShell) {
      window.tailorShell.openBrowser(s.join_url);          // the interviewer opens in the app's video pane
      // Give the interviewer a large stage: widen the panel for the call, restore it on end. A big
      // value is clamped by the panel's own max-width, so this just means "as wide as the layout allows".
      CVI_PREV_W = getComputedStyle(document.documentElement).getPropertyValue("--panel-w").trim();
      document.documentElement.style.setProperty("--panel-w", "9999px");
      window.dispatchEvent(new Event("resize"));          // the panel clamps and re-measures its slot
    } else {
      window.open(s.join_url, "_blank");                   // browser dev: open in a new tab
      toast(T("r2.openedTab", "Your live interview opened in a new tab."));
    }
    let left = total; CVI_UNBILLED = 0;
    CVI_CLOCK = setInterval(() => {
      left--;
      const el = $("#cviMins"); if (el) el.textContent = fmtClock(Math.max(0, left));
      if (left <= 0) { endCvi(); return; }
      // On the plan, report time used every 30 s so the broker meters it; it says when to stop.
      if (s.source === "plan" && ++CVI_UNBILLED >= 30) {
        const secs = CVI_UNBILLED; CVI_UNBILLED = 0;
        fetch("/api/interviews/cvi/heartbeat", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ seconds: secs, interview_id: s.interview_id }) })
          .then(r => r.json()).then(d => { if (d && d.stop && CVI_CLOCK) endCvi(); }).catch(() => {});
      }
    }, 1000);
  }
  function teardownCvi() {                                 // idempotent: safe to call from any exit path
    if (CVI_CLOCK) { clearInterval(CVI_CLOCK); CVI_CLOCK = null; }
    if (window.tailorShell) window.tailorShell.closeBrowser();
    if (CVI_PREV_W) {                                      // restore the panel width the call widened
      document.documentElement.style.setProperty("--panel-w", CVI_PREV_W);
      CVI_PREV_W = "";
      window.dispatchEvent(new Event("resize"));
    }
  }
  // "End interview": close the pane, then score the conversation into the report.
  async function endCvi() {
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
    try { const d = await api("/api/interviews/cvi/end", { interview_id: id }); renderR2Report((d && d.report) || {}); }
    catch (err) { toast(err.message || T("r2.cantScore", "Couldn't score the interview.")); quitLive(); }
  }
  function renderR2Report(rep) {
    const passed = !!rep.passed, cls = passed ? "ok" : "no";
    const mins = rep.duration_s ? fmtClock(Math.round(rep.duration_s)) : "";
    if (GUIDED) showGuidedProgress(3);                       // both rounds done
    $("#liveStage").innerHTML = `
      <div class="scr-report">
        <div class="scr-score ${cls}">${rep.score ?? 0}<span>/100</span></div>
        <div class="mock-ready ${cls}">${esc(passed ? T("r2.passed", "Passed") : T("r2.failed", "Below the bar"))}${mins ? ` <span class="scr-per-score">${mins}</span>` : ""}</div>
        ${rep.why ? `<p class="mock-ready-sum">${esc(rep.why)}</p>` : ""}
        ${(rep.improvements || []).length ? `<div class="mock-col"><div class="prep-fb-h">${esc(T("r1.doNext", "Do this next"))}</div><ul>${rep.improvements.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
        ${(rep.competencies || []).length ? `<div class="scr-comps"><div class="prep-fb-h">${esc(T("r1.byCompetency", "By competency"))}</div>${rep.competencies.map(cb => `<div class="scr-comp-row"><span class="scr-comp-name">${esc(cb.name)}</span><span class="scr-comp-bar"><i class="scr-comp-fill ${cb.score < 60 ? "low" : cb.score < 75 ? "mid" : "high"}" style="width:${Math.max(4, Math.min(100, Number(cb.score) || 0))}%"></i></span><span class="scr-comp-val">${cb.score}</span></div>`).join("")}</div>` : ""}
        ${rep.transcript ? `<details class="scr-tr"><summary>${esc(T("r2.transcript", "Transcript"))}</summary><p class="scr-tr-body">${highlightDisfluencies(rep.transcript)}</p></details>` : ""}
        <p class="scr-disclaimer">${esc(rep.disclaimer || T("r1.reportDisclaimer", "Practice feedback, not a hiring decision."))}</p>
        <div class="mock-next">
          <button class="btn btn-primary btn-sm" id="liveDone" type="button">${esc(T("r1.backToPrep", "Back to prep"))}</button>
        </div>
      </div>`;
    $("#liveDone")?.addEventListener("click", quitLive);
  }
  function stopLiveStream() { if (LIVE_STREAM) { LIVE_STREAM.getTracks().forEach(t => t.stop()); LIVE_STREAM = null; } }
  function quitLive() {
    teardownCvi();                                          // every exit path ends the live interview cleanly
    R2 = null; stopLiveStream(); $("#prepLive").hidden = true; $("#prepList").hidden = false; loadPreps();
  }
  $("#liveQuit")?.addEventListener("click", quitLive);

  // ---- Settings: Live interviewer (Tavus). Key in, key out, and the status Round 2 reads.
  // Whole 15-minute interviews from the plan's minutes.
  function r2InterviewsLeft(mins) {
    const n = Math.floor(Number(mins) / 15);
    return n === 1 ? T("r2.oneLeft", "1 interview left.") : T("r2.nLeft", "{n} interviews left.").replace("{n}", String(n));
  }
  async function renderTavusSettings() {
    const st = await round2Status();
    const pill = $("#tvStatus"), state = $("#tvState"), btn = $("#tvToggle"), rev = $("#tvRevoke");
    if (!pill) return;
    const ok = !!(st && st.available);
    const keySet = !!(st && st.key_set);
    const n = st && st.minutes_left != null ? Math.floor(Number(st.minutes_left) / 15) : 0;
    connPill(pill, ok, T("tavus.ready", "Ready"),
      st && st.reason === "no_minutes" ? T("tavus.usedUp", "Used up") : T("tavus.noKey", "No key"));
    if (state) state.textContent = st && st.source === "plan"
      ? (n === 1 ? T("tavus.usingPlanOne", "Using your plan, 1 interview left.") : T("tavus.usingPlan", "Using your plan, {n} interviews left.").replace("{n}", String(n)))
      : st && st.source === "own_key"
        ? T("tavus.usingOwn", "Using your own Tavus key.")
        : keySet
          ? T("tavus.stateSet", "Your key is saved on this computer. Round 2 uses your own Tavus minutes.")
          : T("tavus.stateUnset", "Round 2 talks to a live interviewer on Tavus. It needs your own key.");
    if (btn) btn.textContent = keySet ? T("tavus.manage", "Manage") : T("tavus.addKey", "Add key");
    if (rev) rev.hidden = !keySet;
  }
  $("#tvToggle")?.addEventListener("click", () => { const f = $("#tvForm"); if (f) { f.hidden = !f.hidden; if (!f.hidden) { if ($("#tvMsg")) $("#tvMsg").hidden = true; $("#tvKey")?.focus(); } } });
  $("#tvCancel")?.addEventListener("click", () => { const f = $("#tvForm"); if (f) { f.reset(); f.hidden = true; } });
  async function saveTavusKey(key) {
    const r = await fetch("/api/avatar/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) throw new Error(d.error || T("tavus.cantSave", "Couldn't save the key."));
    return d;
  }
  $("#tvForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#tvMsg"), setMsg = (t, cls) => { if (msg) { msg.textContent = t; msg.className = "conn-msg " + cls; msg.hidden = false; } };
    const key = $("#tvKey")?.value.trim() || "";
    if (!key) return setMsg(T("tavus.pasteKey", "Paste your Tavus API key."), "err");
    try { await saveTavusKey(key); } catch (err) { return setMsg(err.message, "err"); }
    setMsg(T("tavus.saved", "Saved. Round 2 is ready."), "ok");
    $("#tvForm").reset(); $("#tvForm").hidden = true;
    renderTavusSettings(); paintRound2Tile();
  });
  $("#tvRevoke")?.addEventListener("click", async () => {
    try { await saveTavusKey(""); } catch (err) { toast(err.message); return; }
    const f = $("#tvForm"); if (f) { f.reset(); f.hidden = true; }
    toast(T("tavus.removed", "Key removed."));
    renderTavusSettings(); paintRound2Tile();
  });

  /* ---------------------------------------------------------------- Connect your AI (API key) */
  let AI_CONFIGURED = false;
  // Provider picker: Claude (recommended default) or OpenAI. Only the transport differs;
  // the whole app is provider-agnostic behind /api/apikey + AI_PROVIDER.
  const AI_PROVIDERS = {
    anthropic: {
      label: "Claude", place: "sk-ant-…", title: "Add your Claude key",
      steps: [
        'Open <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">console.anthropic.com</a> and sign in.',
        'Add <b>$5 of credit</b> under Billing <span class="ai-note">, the key won\'t work without it.</span>',
        'Create a key and <b>copy it</b> (it\'s shown only once).',
      ],
    },
    openai: {
      label: "OpenAI", place: "sk-…", title: "Add your OpenAI key",
      steps: [
        'Open <a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener">platform.openai.com</a> and sign in.',
        'Add <b>billing</b> under Settings <span class="ai-note">, the key won\'t work without credit.</span>',
        'Create a key and <b>copy it</b> (it\'s shown only once).',
      ],
    },
  };
  let AI_PROVIDER_SEL = "anthropic";
  function renderAiProvider(p) {
    AI_PROVIDER_SEL = (p === "openai") ? "openai" : "anthropic";
    const info = AI_PROVIDERS[AI_PROVIDER_SEL];
    if ($("#aiTitle")) $("#aiTitle").textContent = info.title;
    if ($("#aiSteps")) $("#aiSteps").innerHTML = info.steps.map(s => `<li>${s}</li>`).join("");
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
  $("#menuProfile")?.addEventListener("click", () => {
    closeAcctMenu();
    // Direct open: the profile nav-item was removed on purpose (one door policy),
    // which left this menu entry clicking a selector that matches nothing.
    loadProfile();
    showView("profile");
  });

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
    if (btn) btn.disabled = true;
    try {
      const d = await api("/api/interview/round2/buy", { pack });
      if (window.tailorShell) window.tailorShell.openBrowser(d.url);
      else window.location.href = d.url;
    } catch (e) { if (!e.reason) toast(e.message || T("r2.cantBuy", "Couldn't start checkout.")); }
    if (btn) btn.disabled = false;
  }
  function openUpgrade() {
    closeAcctMenu();
    if (upgModal) upgModal.hidden = false;
    loadOffers();
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

  // Checkout, try-first: if this install's account has no email yet, collect one (create account
  // or log in) before Stripe, so a paid plan follows the person to a new device. Then get a
  // Checkout Session URL from our server (which brokers Stripe) and go there.
  let subscribing = false, pendingTier = null, accountMode = "signup";   // pendingTier = the pass id
  const upgAccount = $("#upgAccount");

  async function doCheckout(passId) {
    try {
      const r = await api("/api/billing/checkout", { pass: passId });
      if (r && r.url) { window.location.href = r.url; return; }   // -> Stripe Checkout
      toast("Couldn't start checkout. Please try again.");
    } catch (e) {
      if (!e.reason) toast(e.message || "Couldn't start checkout.");   // api() already screened tagged reasons
    }
  }

  function setAccountMode(mode) {
    accountMode = mode;
    const signup = mode === "signup";
    $("#upgAccountH").textContent = signup ? T("upg.createAcct", "Create your account to get your pass") : T("upg.loginAcct", "Log in to continue");
    $("#upgAccountGo").textContent = signup ? "Create account & continue" : "Log in & continue";
    $("#upgAccountToggle").textContent = signup ? "Already have an account? Log in" : "Need an account? Create one";
    const m = $("#upgAccountMsg"); if (m) { m.hidden = true; m.textContent = ""; }
  }

  async function subscribe(tier, btn) {
    if (subscribing) return;
    subscribing = true;
    if (upgAccount) upgAccount.hidden = true;                   // reset the step each attempt
    if (btn) { btn.disabled = true; btn.textContent = T("upg.starting", "Starting..."); }
    try {
      const s = await api("/api/account/status").catch(() => ({}));
      if (s && s.email) { await doCheckout(tier); return; }      // already has an account -> straight to Stripe
      pendingTier = tier;                                        // no account yet -> reveal the create-account step
      if (upgAccount) { setAccountMode("signup"); upgAccount.hidden = false; $("#upgEmail")?.focus(); }
    } finally {
      subscribing = false;
      if (btn) { btn.disabled = false; btn.textContent = T("upg.get", "Get the pass"); }
    }
  }
  document.querySelectorAll(".upg-plan .upg-cta[data-pass]").forEach((b) =>
    b.addEventListener("click", () => subscribe(b.dataset.pass, b)));

  $("#upgAccountToggle")?.addEventListener("click", () =>
    setAccountMode(accountMode === "signup" ? "login" : "signup"));

  upgAccount?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = ($("#upgEmail")?.value || "").trim(), password = $("#upgPassword")?.value || "";
    const msg = $("#upgAccountMsg"), go = $("#upgAccountGo");
    const showMsg = (t) => { if (msg) { msg.textContent = t; msg.hidden = false; } };
    if (!email || password.length < 8) { showMsg("Enter an email and a password of at least 8 characters."); return; }
    go.disabled = true; go.textContent = "Working...";
    try {
      if (accountMode === "signup") {
        try {
          await api("/api/account/claim", { email, password });
          if (pendingTier) await doCheckout(pendingTier);
        } catch (e2) {
          if (/already exists/i.test(e2.message || "")) { setAccountMode("login"); showMsg("That email already has an account. Log in to continue."); }
          else showMsg(e2.message || "Couldn't create your account.");
        }
      } else {
        try {
          await api("/api/account/login", { email, password });
          if (pendingTier) await doCheckout(pendingTier); else { toast("Logged in."); closeUpgrade(); }
        } catch (e2) {
          showMsg(e2.message || "Couldn't log in.");
        }
      }
    } finally {
      go.disabled = false;
      go.textContent = accountMode === "signup" ? "Create account & continue" : "Log in & continue";
    }
  });

  // Continue with Google: open the broker's OAuth start; the shell (or the web redirect) hands back
  // a token, which we adopt as this install's account, then carry on to checkout.
  async function adoptToken(token) {
    try {
      await api("/api/account/adopt", { token });
      if (pendingTier) await doCheckout(pendingTier);
      else { closeUpgrade(); toast("Signed in."); await loadPlanBadge(); }
    } catch (e) { toast(e.message || "Sign-in failed."); }
  }
  $("#upgGoogle")?.addEventListener("click", async () => {
    try {
      const r = await api("/api/account/google/url");
      if (!r || !r.url) { toast("Google sign-in isn't set up yet."); return; }
      if (window.tailorShell) window.tailorShell.openBrowser(r.url);   // -> Google, in the pane
      else window.location.href = r.url;                               // web build: navigate there
    } catch (e) { if (!e.reason) toast(e.message || "Couldn't start Google sign-in."); }
  });
  window.tailorShell?.onSigninReturn?.((data) => {
    if (data && data.token) adoptToken(data.token);
    else toast("Google sign-in didn't complete. Please try again.");
  });

  // The tier shown on the profile foot (Free by default; the pass name while one is active).
  // Refresh it on load and after checkout.
  async function loadPlanBadge() {
    const el = $("#acctPlan"); if (!el) return;
    try {
      const s = await api("/api/account/status");
      if (s && s.plan) el.textContent = PASS_NAMES[s.plan] ? PASS_NAMES[s.plan]() : T("upg.freeName", "Free");
    } catch (e) { /* leave the current label */ }
  }
  loadPlanBadge();

  // Returning from Stripe Checkout. Close the plans panel and confirm in THIS window (not inside
  // the pane). Two ways in: the desktop shell hands us the result after closing the pane, and the
  // web build gets it as a ?checkout= query param on the app URL.
  let checkoutHandled = false;
  async function handleCheckoutResult(status) {
    if (checkoutHandled) return;                 // guard against both paths firing
    checkoutHandled = true;
    closeUpgrade();
    if (status === "success") {
      toast(T("upg.thanks", "Thank you. Your pass is active as soon as the payment clears."));
      try { await loadPlanBadge(); } catch (e) { /* best effort */ }
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

  // Web fallback for Google sign-in (the desktop shell uses onSigninReturn instead): the broker
  // redirects to our app URL with ?account=<token> (or ?signin=error).
  (function handleSigninReturn() {
    const p = new URLSearchParams(location.search);
    const token = p.get("account"), err = p.get("signin");
    if (token) adoptToken(token);
    else if (err === "error") toast("Google sign-in didn't complete. Please try again.");
    else return;
    p.delete("account"); p.delete("signin");
    const q = p.toString();
    history.replaceState(null, "", location.pathname + (q ? "?" + q : ""));
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
        : "Not connected. SponsorJobs needs your key to write anything.";
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
    renderTavusSettings();
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
    catch (err) { out.innerHTML = `<div class="empty">${esc(err.message || "Couldn't check that.")}</div>`; btn.disabled = false; btn.textContent = "Is this CV-worthy?"; return; }
    btn.disabled = false; btn.textContent = "Is this CV-worthy?";
    if (a.worthy) {
      out.innerHTML = `<div class="mem-assess-yes"><b>Yes, this belongs on your CV.</b> ${esc(a.reason || "")}
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
      out.innerHTML = `<div class="mem-assess-no">${esc(a.reason || "Not quite CV-worthy yet, add a concrete action and a result.")}</div>`;
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
      connPill($("#gmStatus"), g.configured, "Connected", "Not connected");
      if ($("#gmDetail")) $("#gmDetail").textContent = g.configured
        ? "Gmail is connected. SponsorJobs watches only for your applications' verification links and recruiter replies."
        : (g.libs ? `Add your Google OAuth client file, then authorize. SponsorJobs looks for it at: ${g.creds_path}`
                  : "Gmail needs a one-time Google authorization. In-app connect is coming; for now it's a manual setup.");
    } catch { /* leave */ }
    try {
      const gh = await fetch("/api/github/status").then(r => r.json());
      connPill($("#ghStatus"), gh.configured, gh.login ? `Connected as ${gh.login}` : "Connected", "Not connected");
      if ($("#ghDisconnect")) $("#ghDisconnect").hidden = !gh.configured;
      if ($("#ghToggle")) $("#ghToggle").textContent = gh.configured ? "Manage" : "Connect";
    } catch { /* leave */ }
  }
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
  });
  $("#ghDisconnect")?.addEventListener("click", async () => {
    await fetch("/api/github/disconnect", { method: "POST" });
    const f = $("#ghForm"); if (f) { f.reset(); f.hidden = true; }
    renderConnections();
  });
  $("#extHelp")?.addEventListener("click", () => { const d = $("#extDetail"); if (d) d.hidden = !d.hidden; });
  $("#gmHelp")?.addEventListener("click", () => { const d = $("#gmDetail"); if (d) d.hidden = !d.hidden; });
  $("#menuSettings")?.addEventListener("click", openSettings);
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
      if (r.ok) { await loadApiKeyStatus(); openAiModal(false); }
    } catch (e) { aiFlash(e.message || "Couldn't save the key.", false); }
    finally { btn.disabled = false; btn.textContent = "Verify & save"; }
  });
  $("#aiKeyInput")?.addEventListener("keydown", e => { if (e.key === "Enter") $("#aiSave").click(); });

  /* ---------------------------------------------------------------- auto-apply (Pro) */
  // Say how many of YOUR roles this can actually submit. The page advertised a "daily
  // submit cap: 40 of 40" next to a capability that fired on 0 of 133 real jobs, because
  // auto-submit needs a keyless candidate API and only Recruitee has one (§7) while the
  // feed was seeded with greenhouse, lever and ashby, all of which gate submission behind
  // the EMPLOYER's key. A cap of 40 over a reach of 0 reads as a promise; the real number
  // is the only honest thing to show, and it is also the number that tells the person
  // whether the paid tier is worth anything TO THEM.
  async function paintAutoReach() {
    const el = $("#aaReach");
    if (!el) return;
    let jobs = [];
    try { jobs = (await api("/api/jobs")).jobs || []; } catch { el.hidden = true; return; }
    const auto = jobs.filter(j => (j.source || "").toLowerCase() === "recruitee");
    el.hidden = false;
    el.textContent = auto.length
      ? `${auto.length} of your ${jobs.length} roles can be submitted automatically. The rest we fill in, and you click Apply.`
      : `None of your ${jobs.length} roles can be submitted automatically today: they need a site with a keyless application API. We tailor and fill every one, and you click Apply.`;
  }

  async function loadAutoApply() {
    try {
      const s = await api("/api/submit/settings");
      $("#aaAutonomous").checked = !!s.autonomous;
      const rate = s.rate || {}, left = rate.remaining != null ? rate.remaining : s.daily_cap;
      $("#aaCap").textContent = `Daily submit cap: ${left} of ${s.daily_cap} left today.`;
      paintAutoReach();
      $("#aaTg").innerHTML = s.telegram_configured
        ? `<span class="aa-dot go"></span> Telegram connected, you'll get a summary there.`
        : `<span class="aa-dot warn"></span> Telegram not connected, connect it for away-from-desk updates.`;
    } catch { /* leave defaults */ }
    loadReadyToApply();
  }

  // The apply TO-DO: built + reviewed applications still awaiting your on-site submit.
  async function loadReadyToApply() {
    const card = $("#aaReadyCard"), list = $("#aaReadyList");
    if (!card || !list) return;
    let r;
    try { r = await api("/api/records/ready"); } catch { card.hidden = true; return; }
    const items = (r && r.ready) || [];
    card.hidden = items.length === 0;
    $("#aaReadyCount").textContent = items.length ? `· ${items.length}` : "";
    list.innerHTML = items.map(it => {
      const lane = it.lane === "auto"
        ? `<span class="pkg-tier-pill auto">Auto-submit</span>`
        : `<span class="pkg-tier-pill assisted">Your click</span>`;
      const open = /^https?:\/\//i.test(it.apply_url || "")
        ? `<button class="btn btn-ghost btn-sm" data-ready-open="${esc(it.apply_url)}" type="button">Open application</button>`
        : `<button class="btn btn-ghost btn-sm" data-ready-view="${it.id}" type="button">View package</button>`;
      return `<div class="aa-ready-row"><span class="aa-ready-role">${esc(it.role)} · ${esc(it.company)}</span>`
        + `${lane}<span class="aa-ready-actions">${open}`
        + `<button class="btn btn-ghost btn-sm" data-ready-done="${it.id}" type="button">Mark applied</button></span></div>`;
    }).join("");
  }
  $("#aaReadyList")?.addEventListener("click", async e => {
    const open = e.target.closest("[data-ready-open]");
    if (open) { window.open(open.dataset.readyOpen, "_blank", "noopener"); return; }
    const view = e.target.closest("[data-ready-view]");
    if (view) { openPackage(Number(view.dataset.readyView)); return; }
    const done = e.target.closest("[data-ready-done]");
    if (done) {
      done.disabled = true;
      try { await api(`/api/record/${Number(done.dataset.readyDone)}/status`, { status: "applied" }); }
      catch { done.disabled = false; return; }
      loadReadyToApply();   // drop it from the to-do
    }
  });
  $("#aaAutonomous")?.addEventListener("change", async e => {
    try { await api("/api/submit/settings", { autonomous: e.target.checked }); }
    catch { e.target.checked = !e.target.checked; }
  });
  const csv = id => ($(id)?.value || "").split(",").map(s => s.trim()).filter(Boolean);
  $("#aaRun")?.addEventListener("click", async () => {
    const btn = $("#aaRun"), count = Number($("#aaCount").value) || 5;
    const focus = { titles: csv("#aaFocus"), locations: csv("#aaLocation"),
                    sponsor_only: !!$("#aaSponsorOnly")?.checked };
    const focused = focus.titles.length || focus.locations.length || focus.sponsor_only;
    const msg = $("#aaMsg"), res = $("#aaResults");
    btn.disabled = true; btn.textContent = "Tailoring…";
    msg.hidden = false; msg.className = "aa-msg working";
    msg.textContent = `Tailoring ${count} ${focused ? "matching" : "fresh"} roles, this can take a moment…`;
    res.innerHTML = "";
    try {
      const r = await api("/api/autoapply/run", { count, focus });
      msg.className = "aa-msg ok";
      msg.innerHTML = `Queued <b>${r.queued}</b> of ${r.considered}, review and approve them in <b>Review &amp; submit</b>.`;
      res.innerHTML = (r.tailored || []).map(t =>
        `<div class="aa-row"><span class="aa-row-role">${esc(t.role)} · ${esc(t.company)}</span>`
        + `<span class="chip ${t.coverage >= 50 ? "good" : "mid"} chip-ml"><span class="tick"></span>${t.coverage}% match</span></div>`).join("")
        + (r.skipped || []).map(s =>
          `<div class="aa-row skip"><span class="aa-row-role">${esc(s.role)}</span><span class="aa-skip">skipped, ${esc(s.reason)}</span></div>`).join("");
    } catch (e) { msg.className = "aa-msg err"; msg.textContent = e.message || "Couldn't run auto-apply."; }
    finally { btn.disabled = false; btn.textContent = "Tailor & queue →"; }
  });

  /* ---------------------------------------------------------------- first-run wizard */
  const ONBOARDED = "tailor_onboarded_v1";
  const wizDone = { ai: false, resume: false };

  function wizShow(panel) {
    document.querySelectorAll(".wiz-panel").forEach(p => p.hidden = p.dataset.panel !== panel);
    const order = ["welcome", "ai", "resume", "done"];
    const idx = order.indexOf(panel);
    document.querySelectorAll(".wiz-dot").forEach((d, i) => d.classList.toggle("on", i < idx));
    if (panel === "ai") setTimeout(() => $("#wizKey")?.focus(), 50);
  }
  function wizNext(from) {
    const steps = ["ai", "resume"];
    const start = from === "welcome" ? 0 : steps.indexOf(from) + 1;
    for (let i = start; i < steps.length; i++) if (!wizDone[steps[i]]) { wizShow(steps[i]); return; }
    // done
    const bits = [
      wizDone.ai ? "AI connected" : "AI not connected yet, add it any time from the sidebar",
      wizDone.resume ? "profile filled from your resume" : "no profile yet, you can build one as you tailor",
    ];
    $("#wizDoneSummary").textContent = bits.join(" · ") + ".";
    wizShow("done");
  }
  function closeWizard() { localStorage.setItem(ONBOARDED, "1"); $("#wizard").hidden = true; }
  async function openWizard() {
    const [ai, prof] = await Promise.all([loadApiKeyStatus(), api("/api/profile").catch(() => ({}))]);
    wizDone.ai = !!ai.configured;
    wizDone.resume = !!prof.has_profile;
    if (wizDone.ai && wizDone.resume) { localStorage.setItem(ONBOARDED, "1"); return false; }
    $("#wizard").hidden = false;
    wizShow("welcome");
    return true;
  }
  document.querySelectorAll("[data-wiz]").forEach(b => b.addEventListener("click", () => {
    const a = b.dataset.wiz;
    if (a === "skip") closeWizard();
    else if (a === "next") wizNext("welcome");
    else if (a === "skip-step") wizNext(b.closest(".wiz-panel").dataset.panel);
    else if (a === "finish-new") { closeWizard(); loadDashboard(); showView("dashboard"); $("#newCvBtn").click(); }
    else if (a === "finish-jobs") { closeWizard(); document.querySelector('.nav-item[data-nav="jobs"]')?.click(); }
  }));
  function wizMsg(el, text, cls) { el.hidden = false; el.className = "ai-msg " + cls; el.textContent = text; }
  $("#wizKeySave")?.addEventListener("click", async () => {
    const key = $("#wizKey").value.trim(), msg = $("#wizKeyMsg"), btn = $("#wizKeySave");
    if (!key) { wizMsg(msg, "Paste your key first.", "err"); return; }
    btn.disabled = true; btn.textContent = "Verifying…";
    wizMsg(msg, `Checking your key with ${AI_PROVIDERS[AI_PROVIDER_SEL].label}…`, "working");
    try {
      const r = await api("/api/apikey", { key, provider: AI_PROVIDER_SEL });
      if (r.ok) { wizDone.ai = true; await loadApiKeyStatus(); wizNext("ai"); }
    } catch (e) { wizMsg(msg, e.message || "Couldn't save the key.", "err"); }
    finally { btn.disabled = false; btn.textContent = "Verify & continue"; }
  });
  $("#wizKey")?.addEventListener("keydown", e => { if (e.key === "Enter") $("#wizKeySave").click(); });
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
      setTimeout(() => wizNext("resume"), 800);
    } catch (err) { wizMsg(msg, "Couldn't read that file, " + err.message, "err"); }
    finally { e.target.value = ""; }
  });

  /* boot, runs only once the app is unlocked (see the App Lock gate below) */
  async function boot() {
    renderPreview({});
    loadDashboard();
    api("/api/profile").then(paintAccount).catch(() => {});   // sidebar identity, from turn one
    if (!localStorage.getItem(ONBOARDED) && await openWizard()) return;   // wizard drives first run
    const s = await loadApiKeyStatus();
    if (!s.configured) openAiModal(false);
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
