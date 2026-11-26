// SHIM (2026-11-26, feat/frame-walk): the app's one sent detector lives on feat/sent-detection
// with this same interface. Whichever of the two merges second deletes this shim. Until then the
// frame walk's signals are kept here (and nothing is announced: only the detector announces).
"use strict";
const { EventEmitter } = require("events");
const bus = new EventEmitter();
function signal(sig) { bus.emit("signal", Object.assign({}, sig || {}, { at: Date.now() })); return true; }
function onSignal(cb) { bus.on("signal", cb); return () => bus.removeListener("signal", cb); }
module.exports = { signal, onSignal };
