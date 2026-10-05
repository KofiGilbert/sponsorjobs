// electron-builder afterPack hook: ad-hoc sign the macOS app bundle.
//
// Without a paid Apple Developer ID, electron-builder skips signing. But packaging edits
// Electron's Info.plist and adds our engine, which breaks the ad-hoc signature Electron
// ships with, and a downloaded app with a BROKEN signature gets "SponsorJobs is damaged and
// can't be opened", with no way through for a normal person. An intact ad-hoc signature
// instead gets the ordinary "unidentified developer" prompt, which System Settings >
// Privacy & Security > Open Anyway clears once.
//
// Runs after packing and before the DMG is built, so the DMG carries the signed app.
// Replace with real Developer ID signing and notarization when that account exists.
const { execFileSync } = require("child_process");
const path = require("path");

exports.default = async function adhocSign(context) {
  if (context.electronPlatformName !== "darwin") return;
  const app = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  execFileSync("codesign", ["--force", "--deep", "--sign", "-", app], { stdio: "inherit" });
  execFileSync("codesign", ["--verify", "--deep", "--strict", app], { stdio: "inherit" });
  console.log(`  • ad-hoc signed  ${app}`);
};
