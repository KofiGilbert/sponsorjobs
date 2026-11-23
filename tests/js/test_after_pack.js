// The electron-builder afterPack hook (shell-electron/scripts/after-pack.js), under node with a
// mocked packager: the fuses it flips, that they are flipped BEFORE the macOS ad-hoc signature,
// and that a build without fuse support fails instead of shipping unfused.
// (tests/test_shell_fuses.py runs it.)
"use strict";
const path = require("path");
const Module = require("module");
const assert = (c, m) => { if (!c) throw new Error(m); };

const calls = [];
// Stub child_process before the hook (and adhoc-sign.js) load: no real codesign runs.
const realLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "child_process") return { execFileSync: (cmd, args) => calls.push(["exec", cmd, args.join(" ")]) };
  return realLoad.call(this, request, parent, isMain);
};
const hook = require(path.join(__dirname, "..", "..", "shell-electron", "scripts", "after-pack.js"));
Module._load = realLoad;

// The fuse ids as @electron/fuses numbers them (FuseV1Options).
const IDS = { runAsNode: 0, enableCookieEncryption: 1, enableNodeOptionsEnvironmentVariable: 2,
  enableNodeCliInspectArguments: 3, enableEmbeddedAsarIntegrityValidation: 4, onlyLoadAppFromAsar: 5 };

function packager() {
  return {
    appInfo: { productFilename: "SponsorJobs" },
    // electron-builder's own translation (platformPackager.generateFuseConfig), reproduced.
    async generateFuseConfig(f) {
      calls.push(["generate", JSON.stringify(f)]);
      const c = { version: "1", resetAdHocDarwinSignature: f.resetAdHocDarwinSignature };
      for (const [k, id] of Object.entries(IDS)) if (f[k] != null) c[id] = f[k];
      return c;
    },
    async addElectronFuses(context, config) { calls.push(["flip", context.electronPlatformName, config]); },
  };
}

(async () => {
  // macOS: fuses, then the ad-hoc signature over the flipped binary.
  await hook.default({ electronPlatformName: "darwin", appOutDir: "/out/mac-arm64", packager: packager() });
  const flip = calls.find((c) => c[0] === "flip");
  assert(flip, "the fuses were flipped");
  const cfg = flip[2];
  assert(cfg[IDS.runAsNode] === false, "RunAsNode off");
  assert(cfg[IDS.enableNodeOptionsEnvironmentVariable] === false, "NODE_OPTIONS off");
  assert(cfg[IDS.enableNodeCliInspectArguments] === false, "--inspect off");
  assert(cfg[IDS.onlyLoadAppFromAsar] === true, "only app.asar");
  assert(cfg[IDS.enableEmbeddedAsarIntegrityValidation] === true, "asar integrity on");
  assert(!(IDS.enableCookieEncryption in cfg), "cookie encryption is left as Electron ships it (one-way switch)");
  assert(cfg.resetAdHocDarwinSignature === false, "the hook signs itself, after the flip");
  const iFlip = calls.findIndex((c) => c[0] === "flip");
  const iSign = calls.findIndex((c) => c[0] === "exec" && c[1] === "codesign" && c[2].startsWith("--force --deep --sign -"));
  const iVerify = calls.findIndex((c) => c[0] === "exec" && c[1] === "codesign" && c[2].startsWith("--verify"));
  assert(iSign > iFlip && iVerify > iSign, "flip, then ad-hoc sign, then verify: " + JSON.stringify(calls));
  assert(calls[iSign][2].endsWith("/out/mac-arm64/SponsorJobs.app"), "signs the packed app");

  // Windows: the same fuses, and no codesign.
  calls.length = 0;
  await hook.default({ electronPlatformName: "win32", appOutDir: "C:/out/win-unpacked", packager: packager() });
  assert(calls.some((c) => c[0] === "flip" && c[1] === "win32"), "fuses on Windows too");
  assert(!calls.some((c) => c[0] === "exec"), "no codesign on Windows");

  // An electron-builder without fuse support stops the build rather than shipping unfused.
  let threw = false;
  try { await hook.default({ electronPlatformName: "darwin", appOutDir: "/x", packager: { appInfo: { productFilename: "S" } } }); }
  catch (e) { threw = /fuse support/.test(e.message); }
  assert(threw, "missing fuse support throws");
  assert(Object.isFrozen(hook.FUSES), "the fuse table is fixed");
  console.log("after_pack ok");
})().catch((e) => { console.error(e); process.exit(1); });
