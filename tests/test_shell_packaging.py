"""The installer must carry every file the desktop shell loads.

shell-electron/package.json lists what electron-builder packs into app.asar. That list was once
written file by file, and panegeom.js and agent.js were added to the shell without being added to
it: the dev app (run from the folder) worked, while every installed app since crashed at launch
with "Cannot find module './panegeom'". This walks the shell's local require() graph from main.js
and checks each file is matched by the packed list.
"""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

SHELL = Path(__file__).resolve().parent.parent / "shell-electron"
LOCAL_REQUIRE = re.compile(r"""require\(\s*["']\./([^"']+)["']\s*\)""")


def _packed_patterns() -> list[str]:
    build = json.loads((SHELL / "package.json").read_text(encoding="utf-8"))["build"]
    return list(build["files"])


def _is_packed(name: str, patterns: list[str]) -> bool:
    included = False
    for pat in patterns:
        if pat.startswith("!"):
            if fnmatch.fnmatch(name, pat[1:]):
                included = False
        elif fnmatch.fnmatch(name, pat):
            included = True
    return included


def _local_graph(entry: str) -> set[str]:
    seen, todo = set(), [entry]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        src = (SHELL / name).read_text(encoding="utf-8")
        for dep in LOCAL_REQUIRE.findall(src):
            dep = dep if dep.endswith((".js", ".json")) else dep + ".js"
            assert (SHELL / dep).exists(), f"{name} requires ./{dep}, which does not exist"
            todo.append(dep)
    return seen


def test_every_file_the_shell_loads_is_packed():
    patterns = _packed_patterns()
    entry = json.loads((SHELL / "package.json").read_text(encoding="utf-8")).get("main", "main.js")
    missing = sorted(f for f in _local_graph(entry) | {"package.json"} if not _is_packed(f, patterns))
    assert not missing, f"not packed into the installer: {missing} (shell-electron/package.json build.files)"


def test_the_graph_reaches_the_apply_agent():
    # Guard the walker itself: main.js loads agent.js and panegeom.js today.
    graph = _local_graph("main.js")
    assert {"agent.js", "panegeom.js"} <= graph
