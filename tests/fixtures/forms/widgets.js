// Local stand-ins for the custom form widgets real ATS pages use, built to behave like the
// originals where it matters to a filler (test fixtures only; nothing here is a real site):
//   - ReactSelect: react-select v5 markup (select__control / select__single-value / an
//     <input role=combobox>), opens on mousedown, filters on input events only (a plain
//     el.value = x is ignored, like React), selects on option click, clears its search text on
//     blur, optional async options that arrive 150ms after typing ("Loading..." meanwhile).
//     Real-page options seen on Greenhouse's current boards (cfg.live): the placeholder HIDES
//     while there is search text, a loading indicator spins in the control while options load
//     (cfg.delayMs, e.g. a slow location search), and a hidden aria-hidden "required" mirror
//     input sits beside the control.
//   - WorkdayListbox: <button aria-haspopup=listbox data-automation-id=...> "Select One", opens
//     on CLICK only, menu portalled to <body> as ul[role=listbox] > li[role=option] > div[promptOption].
//   - WorkdayPrompt: input[data-uxi-widget-type=selectinput] search box; typing shows
//     div[role=option][data-automation-id=promptOption]; a pick adds a selectedItem pill.
//   - AriaRadioGroup / AriaCheckbox: div[role=radiogroup] > div[role=radio aria-checked], and
//     div[role=checkbox aria-checked], toggled by click (aria-checked updated a tick later).
//   - PhenomSelect / PrivacyDialog: Phenom's select and its "Data Privacy Agreement" dialog (Lam
//     Research), described where they are defined.
(function () {
  "use strict";
  let seq = 0;
  const h = (tag, attrs, kids) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "text") el.textContent = v; else el.setAttribute(k, v);
    }
    for (const c of kids || []) el.appendChild(c);
    return el;
  };

  function ReactSelect(host, cfg) {
    const id = cfg.id, n = seq++;
    const label = h("label", { id: id + "-label", for: id, text: cfg.label });
    const ph = h("div", { class: "select__placeholder css-1jqq78o-placeholder", text: "Select..." });
    const input = h("input", { id, role: "combobox", "aria-expanded": "false", "aria-autocomplete": "list",
                               "aria-labelledby": id + "-label", class: "select__input", autocomplete: "off",
                               type: "text", style: "width:40px" });
    if (cfg.required) input.setAttribute("aria-required", "true");
    const vc = h("div", { class: "select__value-container" }, [ph, h("div", { class: "select__input-container" }, [input])]);
    const indicators = h("div", { class: "select__indicators", text: "v" });
    const control = h("div", { class: "select__control" }, [vc, indicators]);
    const box = h("div", { class: "select__container select-shell" }, [control]);
    let spinner = null;
    if (cfg.live) {
      box.appendChild(h("input", { required: "", tabindex: "-1", "aria-hidden": "true", class: "css-1a0ro4n-requiredInput",
                                   style: "opacity:0;pointer-events:none;position:absolute;height:12px;width:100%", value: "" }));
    }
    host.appendChild(h("div", { class: "field" }, [label, box]));
    const st = { open: false, q: "", value: null, loading: false, async: !!cfg.async, timer: null };
    let menu = null;
    function opts() {
      if (st.async && !st.q) return [];
      const q = st.q.toLowerCase();
      return cfg.options.filter((o) => !q || o.toLowerCase().includes(q));
    }
    function render() {
      if (menu) { menu.remove(); menu = null; }
      if (cfg.live) {
        if (st.value || input.value) ph.remove();                 // react-select unmounts it
        else if (!ph.isConnected) vc.insertBefore(ph, vc.firstChild);
        if (st.loading && !spinner) { spinner = h("div", { class: "select__indicator select__loading-indicator", text: "..." }); indicators.insertBefore(spinner, indicators.firstChild); }
        if (!st.loading && spinner) { spinner.remove(); spinner = null; }
      }
      input.setAttribute("aria-expanded", st.open ? "true" : "false");
      input.removeAttribute("aria-controls");
      if (!st.open) return;
      const list = h("div", { class: "select__menu-list", role: "listbox", id: "react-select-" + n + "-listbox" });
      if (st.loading) list.appendChild(h("div", { class: "select__menu-notice--loading", text: "Loading..." }));
      else {
        const os = opts();
        if (!os.length) list.appendChild(h("div", { class: "select__menu-notice--no-options", text: st.async && !st.q ? "Start typing" : "No options" }));
        os.forEach((o, i) => {
          const od = h("div", { class: "select__option", id: "react-select-" + n + "-option-" + i, text: o });
          if (cfg.roles) { od.setAttribute("role", "option"); od.setAttribute("aria-selected", "false"); }
          od.addEventListener("click", () => choose(o));
          list.appendChild(od);
        });
      }
      menu = h("div", { class: "select__menu" }, [list]);
      menu.addEventListener("mousedown", (e) => e.preventDefault());
      box.appendChild(menu);
      input.setAttribute("aria-controls", list.id);
    }
    function choose(o) {
      st.value = o; st.open = false; st.q = "";
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, "");
      vc.querySelectorAll(".select__single-value").forEach((x) => x.remove());
      ph.style.display = "none";
      vc.insertBefore(h("div", { class: "select__single-value", text: o }), vc.firstChild);
      render();
      if (cfg.onChange) cfg.onChange(o);
    }
    control.addEventListener("mousedown", (e) => {
      if (st.open && e.target !== input) { st.open = false; } else { st.open = true; }
      render();
    });
    input.addEventListener("input", () => {
      st.q = input.value; st.open = true;
      if (st.async) {
        st.loading = true; render();
        clearTimeout(st.timer);
        st.timer = setTimeout(() => { st.loading = false; render(); }, cfg.delayMs || 150);
      } else render();
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { st.open = false; render(); }
    });
    input.addEventListener("blur", () => {
      st.open = false; st.q = "";
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, "");
      render();
    });
    return { get value() { return st.value; } };
  }

  function WorkdayListbox(host, cfg) {
    const id = "input-" + (seq++);
    const label = h("label", { for: id, "data-automation-id": "formLabel", text: cfg.label + "*" });
    const btn = h("button", { id, type: "button", "aria-haspopup": "listbox", "aria-expanded": "false",
                              "data-automation-id": cfg.aid, "aria-label": cfg.label + " Select One Required",
                              text: "Select One" });
    host.appendChild(h("div", { "data-automation-id": "formField-" + cfg.aid }, [label, btn]));
    let pop = null, value = null;
    function close() { if (pop) pop.remove(); pop = null; btn.setAttribute("aria-expanded", "false"); }
    btn.addEventListener("click", () => {
      if (pop) { close(); return; }
      const ul = h("ul", { role: "listbox", "aria-label": cfg.label });
      for (const o of cfg.options) {
        const li = h("li", { role: "option", "aria-selected": "false" }, [h("div", { "data-automation-id": "promptOption", text: o })]);
        li.addEventListener("click", () => { value = o; btn.textContent = o; close(); });
        ul.appendChild(li);
      }
      pop = h("div", { "data-automation-id": "popup", class: "wd-popup" }, [ul]);
      document.body.appendChild(pop);
      btn.setAttribute("aria-expanded", "true");
    });
    btn.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    return { get value() { return value; } };
  }

  function WorkdayPrompt(host, cfg) {
    const id = "input-" + (seq++);
    const label = h("label", { for: id, "data-automation-id": "formLabel", text: cfg.label });
    const input = h("input", { id, type: "text", "data-uxi-widget-type": "selectinput",
                               "data-automation-id": "searchBox", placeholder: "Search" });
    const pills = h("div", { "data-automation-id": "selectedItemList" });
    host.appendChild(h("div", { "data-automation-id": "multiSelectContainer" }, [label, pills, input]));
    let pop = null; const chosen = [];
    function show() {
      if (pop) pop.remove();
      const q = input.value.toLowerCase();
      const lb = h("div", { role: "listbox" });
      for (const o of cfg.options.filter((x) => x.toLowerCase().includes(q))) {
        const od = h("div", { role: "option", "data-automation-id": "promptOption", "data-automation-label": o, text: o });
        od.addEventListener("click", () => {
          chosen.push(o); pills.appendChild(h("div", { "data-automation-id": "selectedItem", text: o }));
          pop.remove(); pop = null;
        });
        lb.appendChild(od);
      }
      pop = h("div", { "data-automation-id": "popup", class: "wd-popup" }, [lb]);
      document.body.appendChild(pop);
    }
    input.addEventListener("click", show);
    input.addEventListener("input", show);
    input.addEventListener("keydown", (e) => { if (e.key === "Escape" && pop) { pop.remove(); pop = null; } });
    return { get value() { return chosen.slice(); } };
  }

  function AriaRadioGroup(host, cfg) {
    const lid = "rg-" + (seq++);
    const group = h("div", { role: "radiogroup", "aria-labelledby": lid });
    for (const o of cfg.options) {
      const r = h("div", { role: "radio", "aria-checked": "false", tabindex: "0", class: "chip", text: o });
      r.addEventListener("click", () => setTimeout(() => {
        group.querySelectorAll("[role=radio]").forEach((x) => x.setAttribute("aria-checked", x === r ? "true" : "false"));
      }, 20));
      group.appendChild(r);
    }
    host.appendChild(h("div", { class: "field" }, [h("div", { id: lid, class: "q", text: cfg.label }), group]));
    return { get value() { const c = group.querySelector("[aria-checked=true]"); return c ? c.textContent : null; } };
  }

  function AriaCheckbox(host, cfg) {
    const lid = "cb-" + (seq++);
    const cb = h("div", { role: "checkbox", "aria-checked": "false", tabindex: "0", "aria-labelledby": lid,
                          class: "box" });
    if (cfg.required) cb.setAttribute("aria-required", "true");
    cb.addEventListener("click", () => cb.setAttribute("aria-checked", cb.getAttribute("aria-checked") === "true" ? "false" : "true"));
    host.appendChild(h("div", { class: "field" }, [cb, h("span", { id: lid, text: cfg.label })]));
    return { get value() { return cb.getAttribute("aria-checked") === "true"; } };
  }

  // Phenom's select (careers.lamresearch.com, 2026-10-30; tests/fixtures/forms/phenom_lam.html
  // has the page itself): an <input role=combobox placeholder=Select aria-controls=list-N>; a
  // click opens ul#list-N[role=listbox] of button[role=option] in an overlay over the next
  // field; Escape, blur, Tab and a second click are ignored; a mousedown outside its own box
  // closes it; a pick sets the input's value and adds "Clear <label>".
  function PhenomSelect(host, cfg) {
    const n = seq++;
    const fid = "Job_Specific_Information_" + cfg.id;
    const wrap = h("div", { class: "field-3Ez-x", style: "position:relative" });
    if (cfg.note) wrap.appendChild(h("div", { class: "instruction-29usU", text: cfg.note }));
    wrap.appendChild(h("label", { id: fid + "_label", for: fid, class: "label-385LH required-3upJY" }, [h("span", { text: cfg.label })]));
    const input = h("input", { "aria-controls": "list-ph" + n, "aria-expanded": "false", "aria-labelledby": fid + "_label",
                               "aria-required": "true", role: "combobox", "aria-disabled": "false", id: "input-ph" + n,
                               placeholder: "Select", type: "text", class: "select-module_select-input__bO0s9" });
    const main = h("div", { class: "select-module_select-dropdown-main-wrapper__uq-6g", style: "position:relative;width:360px" },
                   [h("div", { class: "select-module_select-input-group__g64bR" }, [input])]);
    wrap.appendChild(h("div", { class: "select-module_select-wrapper__SyQtt", id: fid }, [main]));
    host.appendChild(wrap);
    let overlay = null, value = null;
    const setV = (v) => Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, v);
    const close = () => { if (overlay) overlay.remove(); overlay = null; input.setAttribute("aria-expanded", "false"); };
    input.addEventListener("click", () => {
      if (overlay) return;
      const ul = h("ul", { id: "list-ph" + n, role: "listbox", style: "list-style:none;margin:0;padding:0" });
      cfg.options.forEach((o, i) => {
        const b = h("button", { type: "button", role: "option", id: "list-ph" + n + "-option-" + i, title: o, "aria-selected": "false",
                                style: "display:block;width:100%;text-align:left;padding:10px 8px" }, [h("span", { text: o })]);
        b.addEventListener("click", () => {
          value = o; setV(o); close();
          if (cfg.onChange) cfg.onChange(o);
          if (!wrap.querySelector(".clear")) {
            const c = h("button", { type: "button", class: "clear", text: "Clear " + cfg.label });
            c.addEventListener("click", () => { value = null; setV(""); c.remove(); });
            main.after(c);
          }
        });
        ul.appendChild(h("li", { role: "presentation" }, [b]));
      });
      overlay = h("div", { class: "select-module_select-dropdown-overlay__G4p-2",
                           style: "position:absolute;left:0;top:36px;z-index:50;background:#fff;border:1px solid #999;width:360px" }, [ul]);
      main.appendChild(overlay);
      input.setAttribute("aria-expanded", "true");
    });
    document.addEventListener("mousedown", (e) => { if (overlay && !main.contains(e.target)) close(); });
    return { get value() { return value; } };
  }

  // Phenom's "Data Privacy Agreement" dialog (Lam Research opens it when the resume is attached):
  // role=dialog aria-modal, Cancel and I Acknowledge. Returns {open(), acked}.
  function PrivacyDialog(cfg) {
    const dlg = h("div", { role: "dialog", "aria-modal": "true", "aria-labelledby": "dlg-privacy-h", id: "privacyDialog",
                           style: "position:fixed;inset:0;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,.4);z-index:100" });
    const box = h("div", { style: "background:#fff;width:520px;padding:16px" }, [
      h("span", { id: "dlg-privacy-h", role: "heading", "aria-level": "2", text: "Data Privacy Agreement" }),
      h("p", { text: "In order to create an account, I acknowledge having read and understood the Job Application Data Privacy Notice. "
                   + "For US applicants only: as " + (cfg.company || "the company") + " is an E-Verify employer, I acknowledge the E-Verify participation poster." }),
    ]);
    const cancel = h("button", { type: "button", id: "cancelUploadResume", text: "Cancel" });
    const ack = h("button", { type: "button", id: "confirmUploadResume", text: "I Acknowledge" });
    box.appendChild(h("div", {}, [cancel, ack]));
    dlg.appendChild(box);
    document.body.appendChild(dlg);
    const st = { acked: false };
    cancel.addEventListener("click", () => { dlg.style.display = "none"; if (cfg.onCancel) cfg.onCancel(); });
    ack.addEventListener("click", () => { dlg.style.display = "none"; st.acked = true; if (cfg.onAck) cfg.onAck(); });
    return { open() { dlg.style.display = "flex"; }, get acked() { return st.acked; } };
  }

  window.FixtureWidgets = { ReactSelect, WorkdayListbox, WorkdayPrompt, AriaRadioGroup, AriaCheckbox, PhenomSelect, PrivacyDialog };
})();
