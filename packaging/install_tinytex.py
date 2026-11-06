"""Install the LaTeX that ships inside the SponsorJobs installers: a pinned TinyTeX.

    python packaging/install_tinytex.py dist/SponsorJobs/tinytex     # what CI does
    python packaging/install_tinytex.py .scratch/tinytex               # a local copy to test with

TinyTeX is a minimal, relocatable TeX Live (https://yihui.org/tinytex/), so its pdflatex IS
TeX Live's pdflatex: the same engine, the same format, the same packages as a MacTeX or a
Homebrew TeX Live install, only fewer of them. packaging/README.md says why it is that and
not Tectonic.

What this does, in order:
1. Download the pinned TinyTeX-1 release for this OS and check its SHA-256 (PINS below).
2. Unpack it into the target directory (the target ends up holding bin/, texmf-dist/, ...).
3. ``tlmgr install`` every package in packaging/texlive-packages.txt (already-present ones
   are skipped). These come from the TeX Live 2026 package repository on CTAN.
4. Strip what the app never runs: docs, sources, other engines (XeTeX, LuaTeX), their
   formats, font sources and metrics only those use, and tlmgr itself. What stays is
   pdflatex, its format, kpathsea and the fonts and macros the templates load.
5. Write tinytex-manifest.txt into the target: the release, its SHA-256, and every
   installed package with its revision and licence, so each installer says exactly what
   LaTeX it carries.

The bundle is then read-only: nothing at compile time writes into it, which matters because
on macOS it sits inside a signed .app.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGES = HERE / "texlive-packages.txt"

# The pinned TinyTeX release. TeX Live 2026, pdfTeX 1.40.29: the same TeX Live year and the
# same pdfTeX as the reference renderer the output was compared against
# (packaging/latex_check.py). Move both together, then re-run that comparison.
TINYTEX_VERSION = "v2026.11"
_BASE = f"https://github.com/rstudio/tinytex-releases/releases/download/{TINYTEX_VERSION}/"
PINS = {
    "darwin": (f"TinyTeX-1-darwin-{TINYTEX_VERSION}.tar.xz",
               "9416a257c4caea2f82351fa2f89252b8bd75dceeb0ce6e442d25530b1922a02d"),
    "windows": (f"TinyTeX-1-windows-{TINYTEX_VERSION}.exe",
                "375e8adec0177cd9ec6a1f8c76efe0c22b1a0c260e76a00386bcea52d88e5354"),
    "linux-x86_64": (f"TinyTeX-1-linux-x86_64-{TINYTEX_VERSION}.tar.xz",
                     "5b75befa848deae702ae6fa12ff0eef3f110d7e4e2a979ae99f101eba3c9c70d"),
    "linux-arm64": (f"TinyTeX-1-linux-arm64-{TINYTEX_VERSION}.tar.xz",
                    "b351c365e4085a179c8d76fddadff612d4e4fd16a946953fb8b9d331f4f7344a"),
}
# TeX Live 2026's package repository, tried in order. The first is TinyTeX's own default (a
# CDN copy of TeX Live's tlnet); the others are CTAN mirrors. Not the mirror.ctan.org
# redirector: it hands out a random mirror, and a slow one stalled tlmgr for many minutes.
# TEXLIVE_REPOSITORY overrides the list with one repository (e.g. a nearer or frozen one).
REPOSITORIES = ([os.environ["TEXLIVE_REPOSITORY"]] if os.environ.get("TEXLIVE_REPOSITORY") else [
    "https://tlnet.yihui.org",
    "https://mirrors.mit.edu/CTAN/systems/texlive/tlnet",
    "https://ctan.math.illinois.edu/systems/texlive/tlnet",
])
TLMGR_TIMEOUT = 900   # seconds per tlmgr run before trying the next repository

# The only programs the app runs (tailoring/compiler.py): pdflatex, and kpsewhich, which
# pdflatex's own font and file lookups can call. Windows also needs the DLLs next to them.
KEEP_BIN = {"pdftex", "pdflatex", "kpsewhich"}
# Trees the app never reads. Paths are relative to the TinyTeX root.
STRIP = [
    "texmf-dist/doc", "texmf-dist/source",
    "texmf-dist/fonts/afm", "texmf-dist/fonts/source", "texmf-dist/fonts/opentype",
    "texmf-dist/fonts/truetype", "texmf-dist/scripts",
    "texmf-dist/tex/luatex", "texmf-dist/tex/lualatex",
    "texmf-dist/tex/xetex", "texmf-dist/tex/xelatex",
    "texmf-var/web2c/luatex", "texmf-var/web2c/luahbtex", "texmf-var/web2c/xetex",
    "texmf-var/web2c/metafont", "texmf-var/web2c/tex",
    "texmf-var/web2c/tlmgr.log", "texmf-var/web2c/tlmgr-commands.log",
    "texmf-var/web2c/updmap.log",
    "tlpkg",
]
# Windows DLLs that belong to the other engines (LuaTeX, XeTeX, Knuth's TeX, dvipdfmx) and
# ICU, which only XeTeX and LuaHBTeX load. pdftex.dll, kpathsea and the C runtime stay.
DROP_DLL_PREFIXES = ("luatex", "luahbtex", "xetex", "lua5", "dvipdfmx", "icu", "tex.dll")
# Formats in texmf-var/web2c/pdftex: only pdflatex.fmt is used.
KEEP_FMT = {"pdflatex.fmt"}


def platform_key() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "windows"
    return "linux-arm64" if platform.machine().lower() in ("aarch64", "arm64") else "linux-x86_64"


def bin_dir(root: Path) -> Path:
    """The one directory under ``root``/bin (universal-darwin, windows, x86_64-linux, ...)."""
    dirs = [d for d in (root / "bin").iterdir() if d.is_dir()]
    if len(dirs) != 1:
        raise SystemExit(f"expected one platform dir in {root / 'bin'}, found {dirs}")
    return dirs[0]


def read_packages(path: Path = PACKAGES) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(key: str, cache: Path) -> Path:
    name, want = PINS[key]
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / name
    if not dest.exists() or _sha256(dest) != want:
        print(f"downloading {name}", flush=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(_BASE + name, timeout=300) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.replace(dest)
    got = _sha256(dest)
    if got != want:
        raise SystemExit(f"SHA-256 mismatch for {name}: got {got}, pinned {want}")
    print(f"verified {name} sha256={got}", flush=True)
    return dest


def unpack(archive: Path, target: Path) -> None:
    """Unpack so ``target`` itself is the TinyTeX root (bin/, texmf-dist/, ...)."""
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        tmp = Path(tmp)
        if archive.suffix == ".exe":
            # A 7-Zip self-extractor. 7z itself is on every GitHub Windows runner; the
            # self-extractor is the fallback.
            seven = shutil.which("7z") or r"C:\Program Files\7-Zip\7z.exe"
            if Path(seven).exists() or shutil.which("7z"):
                subprocess.run([seven, "x", "-y", f"-o{tmp}", str(archive)], check=True,
                               stdout=subprocess.DEVNULL)
            else:
                subprocess.run([str(archive), "-y", f"-o{tmp}"], check=True)
        else:
            with tarfile.open(archive) as tf:
                tf.extractall(tmp, filter="tar") if hasattr(tarfile, "data_filter") \
                    else tf.extractall(tmp)
        roots = [p for p in tmp.iterdir() if p.is_dir()]
        if len(roots) != 1:
            raise SystemExit(f"unexpected archive layout: {roots}")
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(roots[0]), str(target))


def _tool(bindir: Path, name: str) -> list[str]:
    for cand in (bindir / name, bindir / f"{name}.exe", bindir / f"{name}.bat"):
        if cand.exists():
            return [str(cand)]
    raise SystemExit(f"{name} not found in {bindir}")


def tlmgr(bindir: Path, repo: str, *args: str) -> None:
    cmd = _tool(bindir, "tlmgr") + ["--repository", repo, *args]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, timeout=TLMGR_TIMEOUT, stdin=subprocess.DEVNULL)


def install_packages(root: Path, packages: list[str]) -> str:
    """Install ``packages``; returns the repository they came from."""
    bindir = bin_dir(root)
    last = None
    for repo in REPOSITORIES:
        try:
            # A release's tlmgr is older than the live repository's, which then refuses to
            # install anything until tlmgr updates itself. Only tlmgr changes, no package.
            tlmgr(bindir, repo, "update", "--self")
            tlmgr(bindir, repo, "install", *packages)
            return repo
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            print(f"tlmgr failed with {repo}: {exc}; trying the next repository", flush=True)
            last = exc
    raise SystemExit(f"could not install the TeX Live packages: {last}")


def write_manifest(root: Path, key: str, repo: str) -> None:
    """tinytex-manifest.txt: release, checksum, and each package's revision and licence."""
    name, sha = PINS[key]
    lines = [f"TinyTeX {TINYTEX_VERSION} ({name}, sha256 {sha})",
             f"Extra packages from {repo}. Docs, sources, tlmgr and other engines' files were",
             "then removed (packaging/install_tinytex.py). Licences: LICENSE.TL, LICENSE.CTAN.",
             "Installed TeX Live packages (name, revision, licence):", ""]
    db = root / "tlpkg" / "texlive.tlpdb"
    pkg = rev = lic = None
    for raw in db.read_text(encoding="utf-8", errors="replace").splitlines() + [""]:
        if raw.startswith("name "):
            pkg, rev, lic = raw[5:], "", ""
        elif raw.startswith("revision "):
            rev = raw[9:]
        elif raw.startswith("catalogue-license "):
            lic = raw[18:]
        elif not raw.strip() and pkg:
            lines.append(f"{pkg} r{rev} {lic or 'see LICENSE.TL'}")
            pkg = None
    (root / "tinytex-manifest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def strip(root: Path) -> None:
    bindir = bin_dir(root)
    for rel in STRIP:
        p = root / rel
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.exists() or p.is_symlink():
            p.unlink()
    fmtdir = root / "texmf-var" / "web2c" / "pdftex"
    for f in fmtdir.iterdir():
        if f.name not in KEEP_FMT:
            f.unlink()
    # Refresh kpathsea's file databases (ls-R) for the trimmed tree, while mktexlsr still
    # exists. kpathsea's own lookup is the program that does it on every OS.
    _refresh_lsr(root)
    for f in bindir.iterdir():
        stem = f.name[:-4] if f.name.lower().endswith((".exe", ".dll")) else f.name
        if f.name.lower().endswith(".dll"):
            # Windows: pdftex.dll is the engine itself; keep it, kpathsea and the C runtime.
            if f.name.lower().startswith(DROP_DLL_PREFIXES):
                f.unlink()
            continue
        if stem not in KEEP_BIN:
            if f.is_dir() and not f.is_symlink():
                shutil.rmtree(f)
            else:
                f.unlink()
    # pdflatex is a symlink to pdftex on macOS and Linux (pdftex reads its own name to pick
    # the LaTeX format). Make it a real copy: installer builders and app signing handle plain
    # files the same everywhere, and the copy keeps pdftex's own embedded code signature.
    for f in bindir.iterdir():
        if f.is_symlink():
            real = f.resolve()
            f.unlink()
            shutil.copy2(real, f)


def _refresh_lsr(root: Path) -> None:
    """Rewrite every ls-R under ``root`` to match what is on disk.

    Done in Python instead of running mktexlsr, which is a shell script on macOS and Linux
    and a Perl one on Windows. The format is kpathsea's: a header line, then for each
    directory "<dir>:", its entries and a blank line.
    """
    for lsr in root.glob("*/ls-R"):
        tree = lsr.parent
        out = ["% ls-R -- filename database for kpathsea; do not change this line."]

        def walk(d: Path, rel: str) -> None:
            entries = sorted(os.listdir(d))
            out.append("./:" if rel == "." else f"{rel}:")
            out.extend(entries)
            out.append("")
            for e in entries:
                p = d / e
                if p.is_dir() and not p.is_symlink():
                    walk(p, f"{rel}/{e}")

        walk(tree, ".")
        lsr.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Install the pinned TinyTeX into a directory.")
    ap.add_argument("target", help="directory to create (becomes the TinyTeX root)")
    ap.add_argument("--cache", help="where to keep the downloaded release "
                                    "(default: <target>/../.tinytex-download)")
    ap.add_argument("--no-strip", action="store_true", help="keep docs, tlmgr and other engines")
    args = ap.parse_args(argv)

    key = platform_key()
    target = Path(args.target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache).resolve() if args.cache else target.parent / ".tinytex-download"
    unpack(download(key, cache), target)
    repo = install_packages(target, read_packages())
    write_manifest(target, key, repo)
    if not args.no_strip:
        strip(target)
    exe = bin_dir(target) / ("pdflatex.exe" if key == "windows" else "pdflatex")
    out = subprocess.run([str(exe), "--version"], capture_output=True, text=True)
    print(out.stdout.splitlines()[0] if out.stdout else out.stderr)
    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file() and not f.is_symlink())
    print(f"TinyTeX ready at {target} ({size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
