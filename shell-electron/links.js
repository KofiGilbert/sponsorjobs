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

module.exports = { SUPPORT_EMAIL, isSupportMailto };
