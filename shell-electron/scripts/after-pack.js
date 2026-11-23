// electron-builder afterPack hook: lock down the Electron binary, then ad-hoc sign the macOS app.
//
// Electron fuses are switches compiled into the Electron binary itself (docs: electronjs.org
// "Electron Fuses"). Flipped here, they turn off ways to make the shipped app run code that is not
// ours, which a packaged app never needs:
//   runAsNode                              off: ELECTRON_RUN_AS_NODE cannot turn the app into a
//                                          plain Node.js runtime (the shell never forks).
//   enableNodeOptionsEnvironmentVariable   off: NODE_OPTIONS / NODE_EXTRA_CA_CERTS are ignored.
//   enableNodeCliInspectArguments          off: --inspect and SIGUSR1 cannot attach a debugger to
//                                          the main process. (Chromium's --remote-debugging-port
//                                          for the pane is a different switch and is unaffected.)
//   onlyLoadAppFromAsar                    on:  the app code is loaded only from app.asar (the
//                                          build packs it; "asar" is not turned off in package.json).
//   enableEmbeddedAsarIntegrityValidation  on:  app.asar is checked against the hash electron-builder
//                                          writes into Info.plist (macOS) and the .exe resources
//                                          (Windows) when it packs, so an edited app.asar refuses
//                                          to load.
//
// Order matters: flipping a fuse edits the binary, which breaks any signature on it, so the fuses
// are flipped FIRST and the macOS ad-hoc signature (scripts/adhoc-sign.js) is made after. That is
// why this is done here rather than with electron-builder's "electronFuses" option, which runs
// after the afterPack hook (and would undo the ad-hoc signature). The flip goes through
// electron-builder's own packager.addElectronFuses with its own copy of @electron/fuses, so no
// extra dependency is needed. hardenedRuntime stays off: it needs a Developer ID signature.
"use strict";
const adhocSign = require("./adhoc-sign").default;

const FUSES = Object.freeze({
  runAsNode: false,
  enableNodeOptionsEnvironmentVariable: false,
  enableNodeCliInspectArguments: false,
  onlyLoadAppFromAsar: true,
  enableEmbeddedAsarIntegrityValidation: true,
  // We sign right after (macOS); @electron/fuses must not re-sign on its own.
  resetAdHocDarwinSignature: false,
});

/** Flip the fuses on the packed app in context.appOutDir. Throws when this electron-builder has
 * no fuse support, so a build never ships without them by accident. */
async function flipFuses(context) {
  const p = context.packager;
  if (!p || typeof p.generateFuseConfig !== "function" || typeof p.addElectronFuses !== "function") {
    throw new Error("electron-builder has no fuse support (packager.addElectronFuses); update electron-builder");
  }
  const config = await p.generateFuseConfig({ ...FUSES });
  await p.addElectronFuses(context, config);
  console.log(`  • electron fuses  ${Object.entries(FUSES).filter(([k]) => k !== "resetAdHocDarwinSignature")
    .map(([k, v]) => `${k}=${v ? "on" : "off"}`).join(" ")}`);
}

exports.FUSES = FUSES;
exports.flipFuses = flipFuses;
exports.default = async function afterPack(context) {
  await flipFuses(context);
  await adhocSign(context);   // macOS only; a no-op elsewhere
};
