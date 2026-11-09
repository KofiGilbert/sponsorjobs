"""One agent turn with tools, the shape every backend speaks (``llm.agent_turn``).

    agent_turn(system, messages, tools) -> {"text", "tool_calls": [{"id", "name", "input"}], "stop"}

``messages`` is backend neutral:
    {"role": "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "input"}]}
    {"role": "tool", "results": [{"id", "name", "content": str, "is_error": bool}]}
``tools`` are Anthropic tool definitions: {"name", "description", "input_schema"}.
``stop`` is "tool_use" when the model asked for tools, else "end_turn" (or the model's own reason).

AnthropicLLM speaks this natively (tool use content blocks), and so does BrokerLLM when the broker
passes tools through (its answer carries ``content`` blocks, read by ``from_blocks``). A client
without native tool use (OpenAI here, and BrokerLLM on an older broker) uses ``json_agent_turn``:
the tools are described in the system prompt and the model answers with one JSON object.
"""

from __future__ import annotations

import json
import re


def anthropic_messages(messages: list[dict]) -> list[dict]:
    """Neutral messages as Anthropic content blocks, merged so roles alternate and the first
    message is the person's."""
    out: list[dict] = []

    def push(role: str, blocks: list[dict]):
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages or []:
        role = m.get("role")
        if role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": r.get("id"),
                           "content": str(r.get("content") or ""),
                           **({"is_error": True} if r.get("is_error") else {})}
                          for r in m.get("results") or []])
        elif role == "assistant":
            blocks = []
            if str(m.get("content") or "").strip():
                blocks.append({"type": "text", "text": str(m["content"])})
            for c in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": c.get("id"), "name": c.get("name"),
                               "input": c.get("input") or {}})
            push("assistant", blocks)
        else:
            text = str(m.get("content") or "").strip()
            if text:
                push("user", [{"type": "text", "text": text}])
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(start)"}]})
    return out


def from_anthropic(msg) -> dict:
    text, calls = [], []
    for b in getattr(msg, "content", None) or []:
        kind = getattr(b, "type", "")
        if kind == "text":
            text.append(getattr(b, "text", "") or "")
        elif kind == "tool_use":
            calls.append({"id": getattr(b, "id", ""), "name": getattr(b, "name", ""),
                          "input": dict(getattr(b, "input", None) or {})})
    stop = getattr(msg, "stop_reason", "") or ("tool_use" if calls else "end_turn")
    return {"text": "".join(text).strip(), "tool_calls": calls, "stop": stop}


def from_blocks(content: list, stop_reason=None) -> dict:
    """The same turn from content blocks as plain JSON (the broker's /llm/complete answer)."""
    text, calls = [], []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            text.append(str(b.get("text") or ""))
        elif b.get("type") == "tool_use" and b.get("name"):
            inp = b.get("input")
            calls.append({"id": str(b.get("id") or ""), "name": str(b["name"]),
                          "input": dict(inp) if isinstance(inp, dict) else {}})
    stop = str(stop_reason or "") or ("tool_use" if calls else "end_turn")
    return {"text": "".join(text).strip(), "tool_calls": calls, "stop": stop}


# ----------------------------------------------------------------------------- JSON fallback

JSON_PROTOCOL = (
    "\n\nTOOLS. You call a tool by answering with ONLY one JSON object, nothing else:\n"
    '{{"tool_calls": [{{"name": "<tool name>", "input": {{...}}}}]}}\n'
    "When you have what you need, answer with ONLY:\n"
    '{{"reply": "<your message to the person>"}}\n'
    "Tool results come back to you in a message that starts with TOOL RESULTS. The tools:\n{tools}"
)


def _tools_text(tools: list[dict]) -> str:
    return "\n".join(f"- {t['name']}: {t.get('description', '')} Input schema: "
                     f"{json.dumps(t.get('input_schema') or {}, separators=(',', ':'))}"
                     for t in tools or [])


def json_history(messages: list[dict]) -> list[dict]:
    """Neutral messages as plain text turns ({"role": "user"|"agent", "content"}) for a
    client's ``_complete_history``."""
    out = []
    for m in messages or []:
        role = m.get("role")
        if role == "tool":
            body = "\n".join(f"{r.get('name')}: {r.get('content')}" for r in m.get("results") or [])
            out.append({"role": "user", "content": "TOOL RESULTS\n" + body})
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            content = (json.dumps({"tool_calls": [{"name": c.get("name"), "input": c.get("input") or {}}
                                                  for c in calls]})
                       if calls else json.dumps({"reply": str(m.get("content") or "")}))
            out.append({"role": "agent", "content": content})
        else:
            out.append({"role": "user", "content": str(m.get("content") or "")})
    return out


def _first_object(raw: str):
    raw = str(raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass
    start = raw.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(raw)):
            if raw[i] == "{":
                depth += 1
            elif raw[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[start:i + 1])
                    except (ValueError, TypeError):
                        break
        start = raw.find("{", start + 1)
    return None


def parse_json_reply(raw: str, tools: list[dict]) -> dict:
    """The model's JSON answer as an agent turn. Unparseable text is the reply itself; a call
    to a tool that is not registered is dropped."""
    names = {t["name"] for t in tools or []}
    obj = _first_object(raw)
    if isinstance(obj, dict):
        calls = []
        for i, c in enumerate(obj.get("tool_calls") or []):
            if isinstance(c, dict) and c.get("name") in names:
                inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                calls.append({"id": f"j{i + 1}", "name": c["name"], "input": inp})
        if calls:
            return {"text": "", "tool_calls": calls, "stop": "tool_use"}
        if isinstance(obj.get("reply"), str):
            return {"text": obj["reply"].strip(), "tool_calls": [], "stop": "end_turn"}
    return {"text": str(raw or "").strip(), "tool_calls": [], "stop": "end_turn"}


def json_agent_turn(complete_history, system: str, messages: list[dict], tools: list[dict],
                    max_tokens: int = 900) -> dict:
    """An agent turn for a client without native tool use: ``complete_history(system, history,
    max_tokens=...)`` is the client's own multi-turn text completion."""
    sys2 = system + JSON_PROTOCOL.format(tools=_tools_text(tools)) if tools else system
    raw = complete_history(sys2, json_history(messages), max_tokens=max_tokens)
    return parse_json_reply(raw, tools)
