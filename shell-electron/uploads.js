// Which file goes into a form's upload box, when the box's area already lists a file of the same
// name (docs/apply-agent.md section 21).
//
// Every tailored resume leaves the app under the same employer-facing name ("<Name> Resume.pdf",
// tests/test_outgoing_filenames.py), so a Workday draft that kept an EARLIER application's resume
// lists a file named exactly like the one tailored for this job. Matching by name alone skipped
// the right resume (2026-11-16, Uline). The page shows nothing but the name and, sometimes, a
// size, so the rule is:
//
// * a listed copy with the same name AND a size that matches ours (within the rounding the page
//   shows) is this file: nothing is uploaded, and extra verified copies are removed;
// * a listed copy with no size, which this run itself uploaded (the same file, same size), is
//   ours: nothing is uploaded;
// * a listed copy with no size that this run uploaded from ANOTHER file of that name is removed
//   (ours, so safe to remove), and this file is uploaded;
// * any other copy (no size, or a different size) may be the person's earlier file: this file is
//   uploaded, theirs is left alone, and the Review answers list flags it ("earlier_file").
//
// Pure (no Electron), so tests/js/test_upload_identity.js reaches it under node.
"use strict";

// What this run has uploaded so far: { name, path, size } per file. A fresh one per application.
function createUploadLog() { return { files: [] }; }

function noteUpload(log, file) {
  if (!log || !file || !file.name) return;
  const at = log.files.findIndex((x) => x.name === file.name && x.path === file.path && x.size === file.size);
  if (at < 0) log.files.push({ name: file.name, path: file.path || "", size: Number(file.size) || 0 });
  while (log.files.length > 50) log.files.shift();
}

// ``listing``: { copies, same, unsized, other } (autofill_core.js fileListing).
// ``file``: { name, path, size } we would upload. ``log``: this run's uploads.
// Returns { upload, how: "attached" | "listed", drop: "" | "extra_same" | "unsized_own", earlier }.
function uploadDecision(listing, file, log) {
  const L = listing || {};
  const same = Number(L.same) || 0, unsized = Number(L.unsized) || 0, other = Number(L.other) || 0;
  const f = file || {};
  if (same > 0) return { upload: false, how: "listed", drop: same > 1 ? "extra_same" : "", earlier: false };
  const mine = ((log && log.files) || []).filter((x) => x.name === f.name);
  const ours = mine.some((x) => x.path === f.path && x.size === (Number(f.size) || 0));
  if (unsized > 0 && ours) {
    // The page shows no size, but this run put this very file here: it is ours. Another copy
    // beside it (unsized or another size) is still somebody's earlier file.
    return { upload: false, how: "attached", drop: "", earlier: unsized > 1 || other > 0, own: true };
  }
  if (unsized > 0 && mine.length) {
    // This run uploaded another file under this name (ours, so safe to remove), then ours.
    return { upload: true, how: "attached", drop: "unsized_own", earlier: unsized > 1 || other > 0 };
  }
  return { upload: true, how: "attached", drop: "", earlier: unsized + other > 0 };
}

module.exports = { createUploadLog, noteUpload, uploadDecision };
