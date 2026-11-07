import hashlib
import json
import os
import re
import time
from pathlib import Path

from .util import HOME, parse_ts

_cache = {}


def _tail(path, size=262144):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            end = f.tell()
            f.seek(max(0, end - size))
            data = f.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if end > size:
        lines = lines[1:]
    out = []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def _short(s, n=140):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def describe_tool(name, inp):
    if not isinstance(inp, dict):
        return _short(name)
    for k in ("command", "cmd"):
        if inp.get(k):
            v = inp[k]
            return f"{name} {_short(' '.join(v) if isinstance(v, list) else v, 100)}"
    for k in ("file_path", "path", "notebook_path", "filePath"):
        if inp.get(k):
            return f"{name} {_short(str(inp[k]).replace(str(HOME), '~'), 100)}"
    for k in ("pattern", "query", "url", "description", "prompt", "skill"):
        if inp.get(k):
            return f"{name} {_short(inp[k], 100)}"
    return name


def _newest(paths, limit=6):
    items = []
    for p in paths:
        try:
            items.append((os.stat(p).st_mtime, str(p)))
        except OSError:
            pass
    return [p for _, p in sorted(items, reverse=True)[:limit]]


def _cached(path, fn):
    try:
        m = os.stat(path).st_mtime_ns
    except OSError:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == m:
        return hit[1]
    val = fn(path)
    _cache[path] = (m, val)
    return val


def _claude_file(path):
    from .handoff import _limit
    limit = None
    title = prompt = action = text = None
    ts = 0.0
    open_tools = {}
    for o in _tail(path):
        t = o.get("type")
        if t == "ai-title" and o.get("aiTitle"):
            title = o["aiTitle"]
        elif t == "last-prompt" and o.get("lastPrompt"):
            prompt = o["lastPrompt"]
        elif t == "user":
            c = (o.get("message") or {}).get("content")
            if isinstance(c, str) and not c.startswith("<"):
                prompt = c
                action, text, limit = None, None, None
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        open_tools.pop(b.get("tool_use_id"), None)
                    if b.get("type") == "text" and not str(b.get("text", "")).startswith("<"):
                        prompt = b["text"]
                        action, text, limit = None, None, None
        elif t == "assistant":
            for b in (o.get("message") or {}).get("content") or []:
                if b.get("type") == "tool_use":
                    action = describe_tool(b.get("name"), b.get("input"))
                    open_tools[b.get("id")] = action
                elif b.get("type") == "text" and b.get("text", "").strip():
                    lim = _limit(b["text"], bool(o.get("isApiErrorMessage") or o.get("error")))
                    if lim:
                        limit = lim
                    else:
                        text = b["text"]
        if o.get("timestamp"):
            ts = max(ts, parse_ts(o["timestamp"]))
    pending = list(open_tools.values())[-1] if open_tools else None
    return {"limit": limit, "title": title, "prompt": _short(prompt, 220) if prompt else None,
            "action": action, "reply": _short(text, 160) if text else None, "at": ts, "pending": pending}


def claude(cwd):
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    d = HOME / ".claude/projects" / slug
    if not d.is_dir():
        return None
    files = _newest(d.glob("*.jsonl"), 1)
    if not files:
        return None
    a = _cached(files[0], _claude_file)
    if a:
        a = dict(a, mtime=os.stat(files[0]).st_mtime, session=Path(files[0]).stem)
    return a


def _codex_file(path):
    from .handoff import _limit
    limit = None
    cwd = prompt = action = text = None
    ts = 0.0
    open_calls = {}
    try:
        with open(path) as f:
            first = json.loads(f.readline() or "{}")
        cwd = (first.get("payload") or {}).get("cwd")
    except (OSError, ValueError):
        pass
    for o in _tail(path):
        p = o.get("payload") or {}
        pt = p.get("type")
        if pt == "user_message" and p.get("message"):
            prompt, action, text, limit = p["message"], None, None, None
        elif pt in ("error", "stream_error"):
            limit = _limit(p.get("message") or "") or limit
        elif pt in ("function_call", "custom_tool_call", "local_shell_call"):
            args = p.get("arguments") or p.get("input") or p.get("action") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"command": args}
            action = describe_tool(p.get("name") or "shell", args)
            open_calls[p.get("call_id") or p.get("id")] = action
        elif pt in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
            open_calls.pop(p.get("call_id") or p.get("id"), None)
        elif pt == "agent_message" and p.get("message"):
            text = p["message"]
        elif pt == "agent_reasoning" and p.get("text"):
            text = p["text"]
        if o.get("timestamp"):
            ts = max(ts, parse_ts(o["timestamp"]))
    return {"limit": limit, "cwd": cwd, "title": None, "prompt": _short(prompt, 220) if prompt else None,
            "action": action, "reply": _short(text, 160) if text else None, "at": ts,
            "pending": list(open_calls.values())[-1] if open_calls else None}


def codex(cwd):
    root = HOME / ".codex/sessions"
    if not root.is_dir():
        return None
    for f in _newest(root.rglob("rollout-*.jsonl"), 8):
        a = _cached(f, _codex_file)
        if a and (not a.get("cwd") or os.path.realpath(a["cwd"]) == os.path.realpath(cwd)):
            return dict(a, mtime=os.stat(f).st_mtime, session=Path(f).stem)
    return None


def _gemini_file(path):
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    msgs = d.get("messages") if isinstance(d, dict) else d
    prompt = action = text = None
    ts = 0.0
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
        if m.get("type") == "user" and content:
            prompt, action, text = content, None, None
        elif m.get("type") == "gemini":
            for tc in m.get("toolCalls") or []:
                action = describe_tool(tc.get("name"), tc.get("args"))
            if content:
                text = content
        ts = max(ts, parse_ts(m.get("timestamp")))
    return {"title": None, "prompt": _short(prompt, 220) if prompt else None, "action": action,
            "reply": _short(text, 160) if text else None, "at": ts}


def gemini(cwd, home=".gemini"):
    h = hashlib.sha256(cwd.encode()).hexdigest()
    d = HOME / home / "tmp" / h / "chats"
    if not d.is_dir():
        return None
    files = _newest(d.glob("*.json"), 1)
    if not files:
        return None
    a = _cached(files[0], _gemini_file)
    return dict(a, mtime=os.stat(files[0]).st_mtime, session=Path(files[0]).stem) if a else None


READERS = {"claude": claude, "codex": codex, "gemini": gemini,
           "qwen": lambda c: gemini(c, ".qwen")}


def get(agent, cwd):
    fn = READERS.get(agent)
    if not fn or not cwd:
        return None
    try:
        a = fn(cwd)
    except Exception:
        return None
    if not a:
        return None
    age = time.time() - (a.get("mtime") or 0)
    a["state"] = "working" if age < 12 else "waiting" if age < 600 else "idle"
    if a.get("limit"):
        a["state"] = "limited"
    a["project"] = cwd.replace(str(HOME), "~")
    return a
