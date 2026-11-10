// Which non-web links the desktop shell may hand to the operating system.
//
// shell.openExternal on a mailto: link opens the person's own mail app. The app page may ask
// for exactly one such link: an email to our support address, optionally with a subject and
// body. Every other scheme, address, or parameter is refused, so a bad payload cannot make the
// shell open arbitrary handlers (file:, smb:, a custom protocol) or address mail elsewhere.
"use strict";

const SUPPORT_EMAIL = "support@sponsorjobs.ai";
const ALLOWED_PARAMS = new Set(["subject", "body"]);

function isSupportMailto(url) {
  if (typeof url !== "string" || url.length > 2000) return false;
  const prefix = "mailto:" + SUPPORT_EMAIL;
  if (!url.toLowerCase().startsWith(prefix)) return false;
  const rest = url.slice(prefix.length);
  if (rest === "") return true;
  if (rest[0] !== "?") return false;
  let params;
  try { params = new URLSearchParams(rest.slice(1)); } catch (e) { return false; }
  const seen = new Set();
  for (const [k] of params) {
    if (!ALLOWED_PARAMS.has(k) || seen.has(k)) return false;   // no cc, bcc, to, or repeats
    seen.add(k);
  }
  return true;
}

// "Open in Gmail" from the Help and support panel: Gmail's compose screen, addressed to our
// support inbox, in the person's own browser. Many people have no mail app (or a browser set as
// the mailto handler with no mail in it), so this is the reliable path. Accepted: exactly
// https://mail.google.com/mail/? with view=cm and to=support@sponsorjobs.ai, plus optional fs,
// su (subject) and body, each at most once. Any other host, path, recipient or parameter is refused.
const GMAIL_PREFIX = "https://mail.google.com/mail/?";
const GMAIL_PARAMS = new Set(["view", "fs", "to", "su", "body"]);

function isSupportGmailCompose(url) {
  if (typeof url !== "string" || url.length > 2000 || !url.startsWith(GMAIL_PREFIX)) return false;
  let u;
  try { u = new URL(url); } catch (e) { return false; }
  if (u.protocol !== "https:" || u.hostname !== "mail.google.com" || u.port || u.username
      || u.password || u.pathname !== "/mail/" || u.hash) return false;
  const seen = new Set();
  for (const [k] of u.searchParams) {
    if (!GMAIL_PARAMS.has(k) || seen.has(k)) return false;   // no cc, bcc, or repeats
    seen.add(k);
  }
  const p = u.searchParams;
  if (p.get("view") !== "cm" || p.get("to") !== SUPPORT_EMAIL) return false;
  if (p.has("fs") && p.get("fs") !== "1") return false;
  return true;
}

module.exports = { SUPPORT_EMAIL, isSupportMailto, isSupportGmailCompose };
