// LinkedIn profile READER, pure logic. Given the DOM of the person's OWN profile page as it is
// currently rendered, it decides whether the page is their own profile and extracts what is
// visible: headline, About, location, experience, education, skills, licenses, open-to-work,
// custom URL, photo and banner presence, featured / recommendations / activity.
//
// It only READS. No scrolling, no clicks, no expanding of "see more", no edits, and nothing on
// anyone else's profile (CLAUDE.md §7: no automation of a logged-in LinkedIn account; this is
// the person reading their own page with a helper). Whatever is below the fold and not yet
// rendered is simply not seen, and the score says so honestly.
//
// Selectors are tolerant on purpose: LinkedIn's class names change on every redeploy, so each
// field is found by several signals (section anchor ids, heading text, data-* attributes, aria
// labels, visible text) with fallbacks. The code uses only a tiny DOM surface, tagName,
// getAttribute, childNodes/children, parentElement, so the unit tests can run it on a fixture
// HTML tree under node without a browser (tests/test_extension_profile.py).

(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.TailorProfileCore = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  const ELEMENT = 1, TEXT = 3;
  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG"]);

  const norm = (s) => String(s == null ? "" : s).replace(/\s+/g, " ").trim();
  const tag = (el) => String((el && el.tagName) || "").toUpperCase();
  const attr = (el, name) => {
    try { const v = el && el.getAttribute ? el.getAttribute(name) : null; return v == null ? "" : String(v); }
    catch { return ""; }
  };
  const cls = (el) => attr(el, "class");
  const hasClass = (el, part) => cls(el).indexOf(part) !== -1;
  const children = (el) => Array.from((el && el.children) || []);
  const childNodes = (el) => Array.from((el && el.childNodes) || []);

  // Visible text: LinkedIn pairs every visible line with a `.visually-hidden` twin for screen
  // readers, which doubles the text unless the twin is skipped.
  function hidden(el) {
    if (SKIP_TAGS.has(tag(el)) || hasClass(el, "visually-hidden") || hasClass(el, "a11y-text")) return true;
    try { return !!(el.getAttribute && el.getAttribute("hidden") != null); } catch { return false; }
  }
  function text(el) {
    const out = [];
    (function walk(n) {
      for (const c of childNodes(n)) {
        if (c.nodeType === TEXT) out.push(String(c.nodeValue || ""));
        else if (c.nodeType === ELEMENT && !hidden(c)) { out.push(" "); walk(c); out.push(" "); }
      }
    })(el);
    return norm(out.join(""));
  }

  // Depth-first over elements. `fn` returns true to stop descending into that element.
  function walk(el, fn) {
    for (const c of children(el)) {
      if (SKIP_TAGS.has(tag(c))) continue;
      if (fn(c) === true) continue;
      walk(c, fn);
    }
  }
  function all(el, pred) { const out = []; walk(el, (c) => { if (pred(c)) out.push(c); }); return out; }
  function first(el, pred) { return all(el, pred)[0] || null; }
  function byTag(el, t) { const T = t.toUpperCase(); return all(el, (c) => tag(c) === T); }
  function ancestor(el, pred, max) {
    let p = el && el.parentElement, n = 0;
    while (p && n++ < (max || 12)) { if (pred(p)) return p; p = p.parentElement; }
    return null;
  }
  const hasToken = (el, token) => (" " + cls(el) + " ").indexOf(" " + token + " ") !== -1;
  const isSectionLike = (el) => tag(el) === "SECTION" || attr(el, "data-view-name") === "profile-card"
    || hasToken(el, "pv-profile-card") || hasToken(el, "artdeco-card");

  // A profile section, by its anchor id (<div id="about"> sits inside the card), else by its
  // heading text, climbing to the enclosing card.
  function section(doc, ids, headingRe) {
    for (const id of ids) {
      const anchor = first(doc, (c) => attr(c, "id") === id);
      if (anchor) {
        if (isSectionLike(anchor)) return anchor;
        const card = ancestor(anchor, isSectionLike, 4);
        if (card) return card;
        return anchor.parentElement || anchor;
      }
    }
    const h = first(doc, (c) => /^H[1-4]$/.test(tag(c)) && headingRe.test(text(c)));
    if (h) return ancestor(h, isSectionLike, 8) || h.parentElement;
    return null;
  }

  // The visible "lines" of one list entry. LinkedIn renders each line as
  // <span aria-hidden="true">...</span> next to its hidden twin; when a redesign drops that
  // pattern, fall back to the leaf elements' text in document order.
  function lines(el) {
    let spans = all(el, (c) => attr(c, "aria-hidden") === "true" && tag(c) !== "SVG").map(text).filter(Boolean);
    if (!spans.length) {
      spans = all(el, (c) => children(c).length === 0 && !hidden(c)).map(text).filter(Boolean);
    }
    const out = [];
    for (const s of spans) if (!out.length || out[out.length - 1] !== s) out.push(s);
    return out;
  }

  // The entries of a section: its list items, skipping nested sub-items (handled by the
  // caller) and "show all" footers.
  function items(sec) {
    if (!sec) return [];
    const lis = byTag(sec, "li").filter((li) => !ancestor(li, (p) => tag(p) === "LI" && isInside(p, sec), 8));
    return lis.filter((li) => !/^show all\b|^see all\b/i.test(text(li)));
  }
  function isInside(el, container) { return !!ancestor(el, (p) => p === container, 40); }
  function subItems(li) {
    return byTag(li, "li").filter((x) => !ancestor(x, (p) => tag(p) === "LI" && p !== li && isInside(p, li), 8));
  }

  const DATE = /\b(19|20)\d\d\b|\bpresent\b|\bcurrent\b/i;
  const LOCATION = /,|\bremote\b|\bon-site\b|\bhybrid\b/i;
  const SKILLS_LINE = /^skills:|\band \+\d+ skills?$|^[^.]{0,80}\+\d+ skills?$/i;
  const JOB_TYPE_ONLY = /^(full-time|part-time|contract|internship|freelance|self-employed|apprenticeship|seasonal|temporary)$/i;
  const JOB_TYPE = /\s·\s(full-time|part-time|contract|internship|freelance|self-employed|apprenticeship|seasonal|temporary)\b.*$/i;

  function experienceEntry(ls, companyHint) {
    const e = { title: "", company: companyHint || "", dates: "", description: "", has_description: false };
    if (!ls.length) return e;
    e.title = ls[0];
    let i = 1;
    const l1 = ls[1] || "";
    if (l1 && companyHint) {
      if (JOB_TYPE_ONLY.test(l1) || JOB_TYPE.test(l1)) i = 2;          // "Full-time" under a grouped employer
    } else if (l1 && !DATE.test(l1) && (!LOCATION.test(l1) || JOB_TYPE.test(l1))) {
      e.company = l1.replace(JOB_TYPE, "").trim(); i = 2;
    }
    const rest = [];
    for (; i < ls.length; i++) {
      const l = ls[i];
      if (!e.dates && DATE.test(l) && l.length < 80) { e.dates = l; continue; }
      if (SKILLS_LINE.test(l)) continue;
      if (e.dates && !e.description && LOCATION.test(l) && l.length < 60) continue;   // location line
      rest.push(l);
    }
    const desc = rest.filter((l) => l.length >= 30).sort((a, b) => b.length - a.length)[0] || "";
    e.description = desc;
    e.has_description = desc.length > 0;
    return e;
  }

  function experience(doc) {
    const sec = section(doc, ["experience"], /^experience$/i);
    const out = [];
    for (const li of items(sec)) {
      // A nested list is a grouped employer ONLY when its items are dated roles. A single
      // role's description and skill chips are also nested <li>s, and they carry no dates.
      const subs = subItems(li).filter((s) => lines(s).some((l) => DATE.test(l) && l.length < 80));
      if (subs.length) {
        // One employer, several roles: the parent's first line is the company.
        const head = lines(li);
        const company = (head[0] || "").replace(JOB_TYPE, "").trim();
        for (const s of subs) { const ls = lines(s); if (ls.length) out.push(experienceEntry(ls, company)); }
      } else {
        const ls = lines(li);
        if (ls.length) out.push(experienceEntry(ls, ""));
      }
    }
    return out.slice(0, 40);
  }

  function education(doc) {
    const sec = section(doc, ["education"], /^education$/i);
    return items(sec).map((li) => {
      const ls = lines(li);
      const dates = ls.find((l) => DATE.test(l) && l.length < 60) || "";
      return { school: ls[0] || "", degree: ls.slice(1).find((l) => l !== dates && !SKILLS_LINE.test(l)) || "", dates };
    }).filter((e) => e.school).slice(0, 20);
  }

  function skills(doc) {
    const sec = section(doc, ["skills"], /^skills$/i);
    const out = [];
    const tagged = all(sec || doc, (c) => /skill/i.test(attr(c, "data-field")) && tag(c) === "A").map(text).filter(Boolean);
    const source = tagged.length ? tagged : items(sec).map((li) => (lines(li)[0] || ""));
    for (const s of source) {
      const v = norm(s);
      if (v && !/endorse|^\d+ /i.test(v) && v.length <= 80 && out.indexOf(v) === -1) out.push(v);
    }
    return out.slice(0, 100);
  }

  function licenses(doc) {
    const sec = section(doc, ["licenses_and_certifications", "certifications"], /^licenses\s*(&|and)\s*certifications$|^certifications$/i);
    return items(sec).map((li) => {
      const ls = lines(li);
      return { name: ls[0] || "", issuer: (ls[1] && !DATE.test(ls[1]) && !/^issued/i.test(ls[1])) ? ls[1] : "" };
    }).filter((e) => e.name).slice(0, 30);
  }

  function countItems(doc, ids, re) { return items(section(doc, ids, re)).length; }

  function topCard(doc) {
    const h1 = byTag(doc, "h1")[0] || null;
    const card = h1 && (ancestor(h1, (p) => isSectionLike(p) || hasClass(p, "pv-top-card") || hasClass(p, "top-card"), 10));
    return { h1, card: card || (h1 && h1.parentElement && h1.parentElement.parentElement) || doc };
  }

  function headline(doc, tc) {
    const tagged = first(doc, (c) => attr(c, "data-generated-suggestion-target") !== "" && tag(c) !== "H1");
    if (tagged && text(tagged)) return text(tagged);
    const el = first(tc.card, (c) => hasClass(c, "text-body-medium") && c !== tc.h1 && text(c));
    if (el) return text(el);
    // Last resort: the first block of text after the name that is not a count line.
    let seen = false, got = "";
    walk(tc.card, (c) => {
      if (got) return true;
      if (c === tc.h1) { seen = true; return true; }
      if (seen && children(c).length === 0) { const t = text(c); if (t && !/connections|followers/i.test(t)) got = t; }
    });
    return got;
  }

  function location(doc, tc) {
    const cands = all(tc.card, (c) => hasClass(c, "text-body-small") && children(c).length <= 2).map(text);
    for (const t of cands) {
      if (!t || /contact info|connections|followers|mutual|open to/i.test(t)) continue;
      if (LOCATION.test(t) || /\barea\b/i.test(t)) return t;
    }
    for (const t of cands) if (t && !/contact info|connections|followers|mutual|open to/i.test(t)) return t;
    return "";
  }

  function about(doc) {
    const sec = section(doc, ["about"], /^about$/i);
    if (!sec) return "";
    const body = first(sec, (c) => hasClass(c, "inline-show-more-text") || hasClass(c, "display-flex") && hasClass(c, "ph5"))
      || sec;
    const ls = lines(body).filter((l) => !/^about$/i.test(l) && !/^…see more$|^see more$|^… ?more$/i.test(l));
    const best = ls.sort((a, b) => b.length - a.length)[0] || "";
    return best;
  }

  function photo(doc, tc) {
    const imgs = byTag(tc.card, "img").concat(byTag(doc, "img"));
    for (const img of imgs) {
      const c = cls(img), src = attr(img, "src"), alt = attr(img, "alt");
      const isProfile = /profile-picture|top-card__photo|pv-top-card-profile-picture|profile-photo/i.test(c)
        || /profile picture|profile photo/i.test(alt);
      if (!isProfile) continue;
      if (/ghost|default|placeholder/i.test(c + " " + src)) return false;
      return /^(https?:)?\/\//.test(src) || /^data:image\/(jpe?g|png|webp)/i.test(src);
    }
    return false;
  }

  function banner(doc) {
    const el = first(doc, (c) => /background-image|profile-background|top-card__background|cover-photo/i.test(cls(c)));
    if (!el) return false;
    const src = attr(el, "src") || attr(first(el, (c) => tag(c) === "IMG") || el, "src") || attr(el, "style");
    if (/default|ghost|placeholder/i.test(cls(el))) return false;
    if (!src) return false;
    return !/static\.licdn\.com\/(aero-v1\/)?sc\/h\//i.test(src);
  }

  function openToWork(doc, tc) {
    if (first(tc.card, (c) => /open-to-work|open_to_work|open-to-carousel/i.test(cls(c)))) return true;
    if (first(tc.card, (c) => /^open to work$|^#opentowork$/i.test(text(c)) && children(c).length <= 3)) return true;
    const frame = first(tc.card, (c) => /open to work/i.test(attr(c, "alt") + " " + attr(c, "aria-label")));
    return !!frame;
  }

  function recommendations(doc) {
    const sec = section(doc, ["recommendations"], /^recommendations$/i);
    if (!sec) return 0;
    return items(sec).filter((li) => text(li).length >= 40).length;
  }

  function activity(doc) {
    const sec = section(doc, ["content_collections", "activity"], /^activity$/i);
    const out = { activity: false, activity_recent: false, followers: 0 };
    const src = sec || doc;
    const m = /(\d[\d,]*)\s+followers/i.exec(text(src));
    if (m) out.followers = parseInt(m[1].replace(/,/g, ""), 10) || 0;
    if (!sec) return out;
    const lis = items(sec);
    out.activity = lis.length > 0;
    out.activity_recent = lis.some((li) => /\b\d+\s?(h|d|w|hour|hours|day|days|week|weeks)\b/i.test(text(li)));
    return out;
  }

  const RANDOM_SLUG = /-[0-9a-f]{5,}$|-\d{3,}$/i;
  function slugOf(loc) {
    const m = /\/in\/([^/?#]+)/.exec(String((loc && loc.pathname) || ""));
    try { return m ? decodeURIComponent(m[1]) : ""; } catch { return m ? m[1] : ""; }
  }

  // Is this the person's OWN profile? Only the owner sees "Add profile section" and the
  // "Edit intro" pencil. Anything less than one of those signals means: not ours, do nothing.
  function isOwnProfile(doc, loc) {
    if (loc && !/^\/in\//.test(String(loc.pathname || ""))) return false;
    const add = first(doc, (c) => /^(BUTTON|A)$/.test(tag(c)) && /^add profile section$/i.test(text(c)));
    if (add) return true;
    const edit = first(doc, (c) => /^edit intro$|^edit (your )?(headline|about|profile intro)/i.test(attr(c, "aria-label")));
    if (edit) return true;
    return false;
  }

  function extract(doc, loc) {
    const tc = topCard(doc);
    const exp = experience(doc);
    const slug = slugOf(loc);
    const current = exp.find((e) => /present|current/i.test(e.dates)) || exp[0] || null;
    const act = activity(doc);
    const feat = countItems(doc, ["featured"], /^featured$/i);
    const proj = countItems(doc, ["projects"], /^projects$/i);
    const recs = recommendations(doc);
    return {
      own: isOwnProfile(doc, loc),
      url: slug ? "https://www.linkedin.com/in/" + slug + "/" : "",
      slug,
      custom_url: !!slug && !RANDOM_SLUG.test(slug),
      name: tc.h1 ? text(tc.h1) : "",
      headline: headline(doc, tc),
      about: about(doc),
      location: location(doc, tc),
      current_title: current ? current.title : "",
      current_company: current ? current.company : "",
      photo: photo(doc, tc),
      banner: banner(doc),
      open_to_work: openToWork(doc, tc),
      experience: exp,
      education: education(doc),
      skills: skills(doc),
      licenses: licenses(doc),
      featured: feat > 0,
      featured_count: feat,
      projects_count: proj,
      recommendations: recs > 0,
      recommendations_count: recs,
      activity: act.activity,
      activity_recent: act.activity_recent,
      followers: act.followers,
    };
  }

  return { extract, isOwnProfile, text, lines, slugOf, section };
});
