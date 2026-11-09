"""Tool use through the broker (the resume chat agent, agent/resume_chat.py).

The app may send Anthropic tool definitions, a ``tool_choice`` and a system prompt as text blocks
with ``cache_control``. Everything is checked here before any metering or model call, so a
malformed or oversized tool list is a plain 400 and never reaches the company key. Mirrors
worker/src/tools.ts; keep the limits identical.

Only the app's own (custom) tools are allowed: a definition with a ``type`` other than "custom" is
an Anthropic server tool (web search, code execution, computer use) that would run, and bill, on
the company key, so it is refused.
"""

from __future__ import annotations

import json
import re

MAX_TOOLS = 16
MAX_TOOLS_CHARS = 32_000          # the tools' JSON, after the checks below
MAX_SYSTEM_BLOCKS = 4
_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_CHOICE_TYPES = ("auto", "any", "tool", "none")


class BadRequest(ValueError):
    pass


def _cache_control(v):
    if v is None:
        return None
    if not isinstance(v, dict) or v.get("type") != "ephemeral":
        raise BadRequest('cache_control must be {type: "ephemeral"}')
    if "ttl" not in v:
        return {"type": "ephemeral"}
    if v["ttl"] in ("5m", "1h"):
        return {"type": "ephemeral", "ttl": v["ttl"]}
    raise BadRequest("cache_control ttl must be 5m or 1h")


def _system(v):
    if v is None:
        return "", 0
    if not isinstance(v, list):
        s = str(v)
        return s, len(s)
    if len(v) > MAX_SYSTEM_BLOCKS:
        raise BadRequest(f"at most {MAX_SYSTEM_BLOCKS} system blocks")
    out, chars = [], 0
    for b in v:
        if not isinstance(b, dict) or b.get("type") != "text" or not isinstance(b.get("text"), str):
            raise BadRequest('system blocks must be {type: "text", text}')
        block = {"type": "text", "text": b["text"]}
        cc = _cache_control(b.get("cache_control"))
        if cc:
            block["cache_control"] = cc
        out.append(block)
        chars += len(b["text"])
    return out, chars


def _tools(v):
    if v is None:
        return None
    if not isinstance(v, list):
        raise BadRequest("tools must be a list")
    if not v:
        return None
    if len(v) > MAX_TOOLS:
        raise BadRequest(f"at most {MAX_TOOLS} tools")
    seen, out = set(), []
    for t in v:
        if not isinstance(t, dict):
            raise BadRequest("each tool must be an object")
        if t.get("type", "custom") != "custom":
            raise BadRequest("only the app's own tools are allowed")
        name = t.get("name")
        if not isinstance(name, str) or not _TOOL_NAME.match(name):
            raise BadRequest("a tool name is missing or invalid")
        if name in seen:
            raise BadRequest("tool names must be unique")
        seen.add(name)
        if not isinstance(t.get("input_schema"), dict):
            raise BadRequest("each tool needs an input_schema object")
        if "description" in t and not isinstance(t["description"], str):
            raise BadRequest("a tool description must be text")
        d = {"name": name, "input_schema": t["input_schema"]}
        if isinstance(t.get("description"), str):
            d["description"] = t["description"]
        cc = _cache_control(t.get("cache_control"))
        if cc:
            d["cache_control"] = cc
        out.append(d)
    return out


def _tool_choice(v, defs):
    if v is None:
        return None
    if not defs:
        raise BadRequest("tool_choice needs tools")
    if not isinstance(v, dict) or v.get("type") not in _CHOICE_TYPES:
        raise BadRequest("tool_choice type must be auto, any, tool or none")
    out = {"type": v["type"]}
    if v["type"] == "tool":
        if not any(d["name"] == v.get("name") for d in defs):
            raise BadRequest("tool_choice names a tool that was not sent")
        out["name"] = v["name"]
    if isinstance(v.get("disable_parallel_tool_use"), bool) and v["type"] != "none":
        out["disable_parallel_tool_use"] = v["disable_parallel_tool_use"]
    return out


def tool_args(body: dict) -> dict:
    """{system, tools, tool_choice, chars} from a /llm/complete body; raises BadRequest. ``chars``
    is what the system blocks and the tool definitions add to the up-front estimate."""
    system, sys_chars = _system(body.get("system"))
    defs = _tools(body.get("tools"))
    choice = _tool_choice(body.get("tool_choice"), defs)
    tool_chars = len(json.dumps(defs, separators=(",", ":"))) if defs else 0
    if tool_chars > MAX_TOOLS_CHARS:
        raise BadRequest("the tool definitions are too large")
    return {"system": system, "tools": defs, "tool_choice": choice, "chars": sys_chars + tool_chars}
