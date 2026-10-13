"""AI-built portfolio projects: generate a SMALL working project, validate it, teach it.

The flow (approved 2026-10-13): the AI builds a small, runnable project for an idea, the person
learns it through a walkthrough of the real files, then defends it in a recorded interview.
Only a defended (Verified) project goes public or onto the resume; that gate lives in ui/app.py.

This module is pure apart from the local file store: the model is passed in, GitHub is not
touched here, and nothing about the person (name, email, profile) is ever sent to the model.

Generation runs in steps to stay inside output limits: plan the file tree (one call), then write
the files in small batches (one call each), validating every batch. A file that fails validation
gets ONE rewrite with the problem named; a second failure stops the build. The whole build is
capped at ``TOKEN_BUDGET`` (input plus output, counted from the model's own usage when it reports
it, else estimated at 4 characters a token), so a runaway build fails cleanly instead of spending.
"""

from __future__ import annotations

import ast
import io
import json
import re
import zipfile
from pathlib import Path

# ---- Limits ---------------------------------------------------------------------------------
MAX_FILES = 20             # a small project: the plan asks for at most 16, validation allows 20
MAX_FILE_BYTES = 16_000    # one file
MAX_TOTAL_BYTES = 60_000   # the whole project
MAX_DEPTH = 5              # path segments
BATCH_SIZE = 4             # files written per model call
TOKEN_BUDGET = 150_000     # one build, input plus output, all calls (plan, batches, fixes, Learn it)

AI_LINE = "> Built with an AI pair-programmer."
README_SECTIONS = ("Problem", "Approach", "Tech stack", "How to run", "Screenshots", "What I learned")

_DEP_FILES = ("requirements.txt", "pyproject.toml", "package.json", "go.mod", "cargo.toml", "pom.xml",
              "build.gradle", "build.gradle.kts", "gemfile", "composer.json", "environment.yml", "pubspec.yaml")
_BINARY_EXT = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tar", ".exe", ".dll", ".so",
               ".dylib", ".pyc", ".class", ".jar", ".whl", ".sqlite", ".db", ".bin", ".woff", ".woff2", ".mp4")
_FORBIDDEN_NAMES = (".env", "id_rsa", "id_ed25519", ".npmrc", ".pypirc", "credentials", "credentials.json",
                    ".netrc", "secrets.json", "service-account.json")
_FORBIDDEN_EXT = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")

# Known credential shapes. Any hit fails the file: a generated project never ships a secret.
_SECRET_PATTERNS = (
    ("an AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("an Anthropic or OpenAI key", re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{20,}")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a Stripe key", re.compile(r"\b(sk|rk)_live_[0-9A-Za-z]{16,}")),
    ("a private key", re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY")),
    ("a password in a URL", re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@'\"]+:[^/\s@'\"]{6,}@", re.I)),
)
# A hard-coded value assigned to a secret-sounding name, unless it is plainly a placeholder.
_SECRET_ASSIGN = re.compile(
    r"""(?ix)\b[\w-]*(api[_-]?key|secret|password|passwd|token|access[_-]?key)[\w-]*['"]?\s*[:=]\s*
        ['"]([^'"\s]{12,})['"]""")
_PLACEHOLDER = re.compile(r"(?i)(your|example|placeholder|changeme|change-me|dummy|test|fake|xxx|\.\.\.|<|\$\{|os\.environ|getenv)")
# Absolute paths that leak a machine: home folders, Windows drives, the macOS temp tree.
_ABS_PATH = re.compile(r"""(?x)(?<![\w.])(
    /Users/[^/\s'"]+ | /home/[^/\s'"]+ | /root/ | /private/var/ | /var/folders/ |
    [A-Za-z]:\\\\?(Users|Documents\ and\ Settings)\\ )""")
# Network calls that must not run at import time (module top level).
_NET_MODULES = {"requests", "httpx", "urllib", "urllib2", "urllib3", "socket", "aiohttp", "http", "ftplib",
                "smtplib", "websocket", "websockets"}
_NET_FUNCS = {"get", "post", "put", "patch", "delete", "head", "request", "urlopen", "urlretrieve",
              "connect", "create_connection", "Session", "Client", "ClientSession", "SMTP", "FTP"}


class BuildError(RuntimeError):
    """A build that cannot finish. ``stage`` says where: plan, write, validate, budget."""

    def __init__(self, message: str, stage: str = ""):
        super().__init__(message)
        self.stage = stage


# ---- Validation -----------------------------------------------------------------------------
def check_path(path: str) -> str:
    """'' when a generated path is safe to write into a repo, else the reason it is not."""
    p = str(path or "")
    if not p.strip() or p != p.strip():
        return "an empty or padded path"
    if p.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", p) or "\\" in p:
        return "an absolute path"
    parts = p.split("/")
    if any(s in ("", ".", "..") for s in parts):
        return "a path that leaves the project"
    if len(parts) > MAX_DEPTH:
        return "a path nested too deep"
    if any(not re.fullmatch(r"[A-Za-z0-9_.@+-]+", s) for s in parts) or ".git" in parts:
        return "a path with unusual characters"
    name = parts[-1].lower()
    if name in _FORBIDDEN_NAMES or name.endswith(_FORBIDDEN_EXT) or name.startswith(".env"):
        return "a secrets file"
    if name.endswith(_BINARY_EXT):
        return "a binary file"
    return ""


def _module_level_net_calls(tree: ast.Module) -> list[str]:
    """Network calls in statements that run on import: the module body, not inside functions,
    classes, or the ``if __name__ == "__main__"`` block."""
    hits = []

    def is_main_guard(node) -> bool:
        return isinstance(node, ast.If) and "__main__" in ast.dump(node.test)

    def scan(nodes):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)) \
                    or is_main_guard(node):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    break
                if isinstance(sub, ast.Call):
                    f = sub.func
                    name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
                    root = f
                    while isinstance(root, ast.Attribute):
                        root = root.value
                    base = root.id if isinstance(root, ast.Name) else ""
                    if name in _NET_FUNCS and (base in _NET_MODULES or name in ("urlopen", "urlretrieve")):
                        hits.append(f"{base or name}.{name}" if base else name)
    scan(tree.body)
    return hits


def check_file(path: str, content: str, personal: list[str] | None = None) -> list[str]:
    """Every problem with one generated file. [] means it may be written."""
    errs = []
    why = check_path(path)
    if why:
        return [f"{path!r} is {why}"]
    if not isinstance(content, str):
        return [f"{path} is not text"]
    size = len(content.encode("utf-8"))
    if size > MAX_FILE_BYTES:
        errs.append(f"{path} is {size // 1000} KB, over the {MAX_FILE_BYTES // 1000} KB file limit")
    if "\x00" in content:
        errs.append(f"{path} holds binary data")
    for label, rx in _SECRET_PATTERNS:
        if rx.search(content):
            errs.append(f"{path} contains {label}")
    for m in _SECRET_ASSIGN.finditer(content):
        if not _PLACEHOLDER.search(m.group(2)) and len(set(m.group(2))) > 6:
            errs.append(f"{path} hard-codes a secret value for {m.group(1)}")
            break
    if _ABS_PATH.search(content):
        errs.append(f"{path} contains an absolute path from someone's machine")
    low = content.lower()
    for marker in personal or []:
        if marker and marker.lower() in low:
            errs.append(f"{path} contains the person's personal details")
            break
    name = path.rsplit("/", 1)[-1].lower()
    if name.endswith(".py"):
        try:
            tree = ast.parse(content, filename=path)
        except SyntaxError as exc:
            errs.append(f"{path} has a Python syntax error on line {exc.lineno}: {exc.msg}")
        else:
            nets = _module_level_net_calls(tree)
            if nets:
                errs.append(f"{path} makes a network call at import time ({nets[0]})")
    elif name.endswith(".json"):
        try:
            json.loads(content)
        except ValueError as exc:
            errs.append(f"{path} is not valid JSON ({exc.msg})")
    return errs


def check_project(files: list[dict], personal: list[str] | None = None) -> list[str]:
    """Problems with the project as a whole: counts, total size, duplicates, required files."""
    errs = []
    paths = [f.get("path", "") for f in files]
    if len(files) > MAX_FILES:
        errs.append(f"{len(files)} files, over the {MAX_FILES} file limit")
    total = sum(len(str(f.get("content", "")).encode("utf-8")) for f in files)
    if total > MAX_TOTAL_BYTES:
        errs.append(f"{total // 1000} KB in all, over the {MAX_TOTAL_BYTES // 1000} KB limit")
    if len(set(p.lower() for p in paths)) != len(paths):
        errs.append("two files share a path")
    names = [p.rsplit("/", 1)[-1].lower() for p in paths]
    if "readme.md" not in [p.lower() for p in paths]:
        errs.append("no README.md")
    if ".gitignore" not in paths:
        errs.append("no .gitignore")
    if not any(n in _DEP_FILES for n in names):
        errs.append("no dependency file (e.g. requirements.txt or package.json)")
    if not any(_is_test(p) for p in paths):
        errs.append("no unit tests")
    for f in files:
        errs += check_file(f.get("path", ""), f.get("content", ""), personal)
    return errs


def _is_test(path: str) -> bool:
    p = path.lower()
    n = p.rsplit("/", 1)[-1]
    return (p.startswith(("tests/", "test/", "__tests__/", "spec/")) or "/tests/" in p or "/test/" in p
            or n.startswith("test_") or re.search(r"(_test\.(py|go)|\.(test|spec)\.[jt]sx?|Test\.java)$", path) is not None)


# ---- README ---------------------------------------------------------------------------------
def with_ai_line(readme: str, on: bool = True) -> str:
    """The README with the AI credit line just under the title (on), or without it (off)."""
    lines = [ln for ln in (readme or "").splitlines() if ln.strip() != AI_LINE]
    text = "\n".join(lines)
    if not on:
        return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    if lines and lines[0].startswith("# "):
        rest = "\n".join(lines[1:]).lstrip("\n")
        return f"{lines[0]}\n\n{AI_LINE}\n\n{rest}".rstrip() + "\n"
    return f"{AI_LINE}\n\n{text}".rstrip() + "\n"


def ensure_readme(readme: str, project: dict, plan: dict) -> str:
    """Make sure the README has a title and all six sections, adding any the model skipped."""
    text = (readme or "").strip()
    if not text.startswith("# "):
        text = f"# {project.get('title') or 'Project'}\n\n{text}"
    fill = {"Problem": project.get("summary") or "_What problem this solves, and for whom._",
            "Approach": "_How it works, and one decision made along the way._",
            "Tech stack": project.get("tech") or "_Languages and libraries used._",
            "How to run": "\n".join(x for x in ("```", plan.get("run") or "", plan.get("test") or "", "```") if x)
            if (plan.get("run") or plan.get("test")) else "_The exact commands to run it._",
            "Screenshots": "_Add a screenshot or GIF of it working._",
            "What I learned": "_One thing that was hard, and how you worked through it._"}
    for head in README_SECTIONS:
        if not re.search(rf"(?im)^#+\s*{re.escape(head)}\s*$", text):
            text += f"\n\n## {head}\n\n{fill[head]}"
    return text.strip() + "\n"


# ---- Budget ---------------------------------------------------------------------------------
class Meter:
    """Counts a build's tokens: the model's own usage when it reports one, else an estimate."""

    def __init__(self, budget: int = TOKEN_BUDGET):
        self.budget, self.input, self.output, self.calls, self.estimated = budget, 0, 0, 0, False

    def call(self, llm, fn, *args, prompt_chars: int = 0, **kw):
        if self.used >= self.budget:
            raise BuildError("This build reached its size limit before it finished.", "budget")
        try:
            llm.last_usage = None
        except Exception:
            pass
        out = fn(*args, **kw)
        usage = getattr(llm, "last_usage", None)
        if isinstance(usage, dict) and (usage.get("input") or usage.get("output")):
            self.input += int(usage.get("input") or 0)
            self.output += int(usage.get("output") or 0)
        else:
            self.estimated = True
            self.input += prompt_chars // 4
            self.output += len(json.dumps(out, ensure_ascii=False)) // 4
        self.calls += 1
        return out

    @property
    def used(self) -> int:
        return self.input + self.output

    def as_dict(self) -> dict:
        return {"input": self.input, "output": self.output, "total": self.used, "calls": self.calls,
                "budget": self.budget, "estimated": self.estimated}


# ---- Generation -----------------------------------------------------------------------------
def _idea(project: dict) -> dict:
    """Only the idea goes to the model, never anything about the person."""
    return {"title": project.get("title", ""), "summary": project.get("summary", ""),
            "tech": project.get("tech", ""), "skills": list(project.get("skills") or [])}


def _clean_plan(plan: dict) -> list[dict]:
    seen, out = set(), []
    for f in plan.get("files") or []:
        p = str((f or {}).get("path") or "").strip() if isinstance(f, dict) else ""
        if p.startswith("./"):
            p = p[2:]
        if not p or p.lower() in seen or check_path(p):
            continue
        seen.add(p.lower())
        out.append({"path": p, "purpose": str(f.get("purpose") or "")[:200]})
    paths = [f["path"] for f in out]
    if "README.md" not in paths:
        out.append({"path": "README.md", "purpose": "What it is and how to run it"})
    if ".gitignore" not in paths:
        out.append({"path": ".gitignore", "purpose": "Keep build output out of git"})
    # README last: by then the model has seen the code it describes.
    out.sort(key=lambda f: f["path"] == "README.md")
    return out[:MAX_FILES]


def generate(llm, project: dict, progress=lambda **kw: None, personal: list[str] | None = None,
             meter: Meter | None = None) -> dict:
    """Plan, write and validate the project. Returns {"files": [{path, content}], "plan": {...},
    "tokens": {...}}. Raises BuildError on a plan the model could not make, a file that is still
    invalid after one rewrite, or the token budget. ``progress`` receives state/done/total."""
    meter = meter or Meter()
    idea = _idea(project)
    progress(state="planning", done=0, total=0)
    raw_plan = meter.call(llm, llm.plan_project_files, idea, 16, MAX_TOTAL_BYTES, prompt_chars=3000)
    plan_files = _clean_plan(raw_plan or {})
    if len(plan_files) < 3:
        raise BuildError("The AI could not plan this project.", "plan")
    plan = {"files": plan_files, "run": (raw_plan or {}).get("run", ""), "test": (raw_plan or {}).get("test", "")}
    total = len(plan_files)
    written: list[dict] = []
    progress(state="writing", done=0, total=total)
    paths = [f["path"] for f in plan_files]
    for i in range(0, len(paths), BATCH_SIZE):
        batch = paths[i:i + BATCH_SIZE]
        got = _write_batch(llm, meter, idea, plan, batch, written, personal)
        written += got
        progress(state="writing", done=len(written), total=total)
    # README: the AI credit line on by default, and every section present.
    for f in written:
        if f["path"] == "README.md":
            f["content"] = with_ai_line(ensure_readme(f["content"], idea, plan), True)
    if not any(f["path"] == ".gitignore" for f in written):
        written.append({"path": ".gitignore", "content": "__pycache__/\nnode_modules/\n.env\n.venv/\ndist/\n"})
    errs = check_project(written, personal)
    if errs:
        raise BuildError("The generated project did not pass the safety checks: " + "; ".join(errs[:3]) + ".",
                         "validate")
    return {"files": written, "plan": plan, "tokens": meter.as_dict()}


def _write_batch(llm, meter: Meter, idea: dict, plan: dict, batch: list[str], written: list[dict],
                 personal) -> list[dict]:
    """One batch of files, validated. A missing or invalid file gets one rewrite with the problem
    named; still bad after that, the build stops."""
    want = list(batch)
    out: dict[str, str] = {}
    fix: dict[str, str] = {}
    for attempt in range(2):
        ctx = len(json.dumps(plan)) + sum(min(1500, len(w["content"])) for w in written[-8:]) + 2500
        res = meter.call(llm, llm.write_project_files, idea, plan, want, written, fix or None, prompt_chars=ctx)
        got = {str(f.get("path") or "").strip(): f.get("content") for f in (res or {}).get("files") or []
               if isinstance(f, dict)}
        fix = {}
        for p in want:
            body = got.get(p)
            # An empty __init__.py is normal; any other empty or missing file is a miss.
            if not isinstance(body, str) or (not body.strip() and not p.endswith("__init__.py")):
                fix[p] = "it was missing from your answer"
                continue
            errs = check_file(p, with_ai_line(body) if p == "README.md" else body, personal)
            if errs:
                fix[p] = "; ".join(errs)
            else:
                out[p] = body
        want = list(fix)
        if not want:
            break
    if fix:
        first = next(iter(fix.items()))
        raise BuildError(f"The AI could not write {first[0]} cleanly ({first[1]}).", "validate")
    return [{"path": p, "content": out[p]} for p in batch]


# ---- Local store ----------------------------------------------------------------------------
def save_files(root: Path, files: list[dict]) -> None:
    """Write the project under ``root/files`` (replacing what was there) plus a manifest."""
    import shutil
    base = Path(root) / "files"
    if base.exists():
        shutil.rmtree(base)
    for f in files:
        if check_path(f["path"]):
            continue
        dest = base / f["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f["content"], encoding="utf-8")
    (Path(root) / "manifest.json").write_text(json.dumps([f["path"] for f in files], indent=1), encoding="utf-8")


def load_files(root: Path) -> list[dict]:
    try:
        paths = json.loads((Path(root) / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for p in paths:
        if check_path(p):
            continue
        try:
            out.append({"path": p, "content": (Path(root) / "files" / p).read_text(encoding="utf-8")})
        except OSError:
            continue
    return out


def zip_bytes(files: list[dict], folder: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.writestr(f"{folder}/{f['path']}", f["content"])
    return buf.getvalue()


# ---- Learn it -------------------------------------------------------------------------------
def learn_steps(raw_steps: list, files: list[dict]) -> list[dict]:
    """Normalise the model's walkthrough against the REAL files: every excerpt is cut from the
    file on disk (never model-written code), line ranges are clamped, steps pointing at a file
    that does not exist are dropped, and each check is a well-formed question. 5 to 8 steps."""
    from tailoring.conform import strip_ai_dashes as clean
    by_path = {f["path"]: f["content"] for f in files}
    out = []
    for s in raw_steps or []:
        if not isinstance(s, dict) or s.get("path") not in by_path:
            continue
        lines = by_path[s["path"]].splitlines()
        try:
            a = max(1, int(s.get("start") or 1))
            b = int(s.get("end") or a + 30)
        except (TypeError, ValueError):
            a, b = 1, 30
        b = max(a, min(b, len(lines), a + 59))
        a = min(a, max(1, len(lines)))
        chk = _clean_check(s.get("check"))
        if not chk:
            continue
        out.append({"title": clean(str(s.get("title") or s["path"]).strip())[:80], "path": s["path"],
                    "start": a, "end": b, "code": "\n".join(lines[a - 1:b]),
                    "explain": clean(str(s.get("explain") or "").strip())[:900],
                    "why": clean(str(s.get("why") or "").strip())[:700], "check": chk})
        if len(out) >= 8:
            break
    return out


def _clean_check(c) -> dict | None:
    from tailoring.conform import strip_ai_dashes as clean
    if not isinstance(c, dict) or not str(c.get("q") or "").strip():
        return None
    q, why = clean(str(c["q"]).strip())[:300], clean(str(c.get("explain") or "").strip())[:400]
    opts = [clean(str(o).strip())[:160] for o in (c.get("options") or []) if str(o).strip()]
    if (c.get("kind") or "choice") == "choice" and 2 <= len(opts) <= 5:
        try:
            ans = int(c.get("answer"))
        except (TypeError, ValueError):
            return None
        if 0 <= ans < len(opts):
            return {"kind": "choice", "q": q, "options": opts, "answer": ans, "explain": why}
        return None
    acc = [str(a).strip().lower()[:60] for a in (c.get("accept") or []) if str(a).strip()]
    if acc:
        return {"kind": "line", "q": q, "accept": acc[:3], "explain": why}
    return None


def check_answer(step: dict, answer) -> bool:
    c = step.get("check") or {}
    if c.get("kind") == "choice":
        try:
            return int(answer) == int(c.get("answer"))
        except (TypeError, ValueError):
            return False
    said = re.sub(r"\s+", " ", str(answer or "")).strip().lower()
    return bool(said) and any(a in said for a in c.get("accept") or [])


def public_step(step: dict) -> dict:
    """A step as the client sees it: the answer key stays on the server."""
    c = step.get("check") or {}
    return {"title": step.get("title", ""), "path": step.get("path", ""), "start": step.get("start", 1),
            "end": step.get("end", 1), "code": step.get("code", ""), "explain": step.get("explain", ""),
            "why": step.get("why", ""),
            "check": {"kind": c.get("kind", "choice"), "q": c.get("q", ""), "options": c.get("options") or []}}
