import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .util import HOME, STATE

DIR = STATE / "feedback"
SETTINGS = HOME / ".claude/settings.json"
EVENTS = ("PostToolUse", "Stop", "UserPromptSubmit", "Notification")


def _file(cwd):
    return DIR / (re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd)).strip("-") + ".jsonl")


def _read(cwd):
    try:
        return [json.loads(l) for l in _file(cwd).read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


def _write(cwd, items):
    DIR.mkdir(parents=True, exist_ok=True)
    f = _file(cwd)
    tmp = f.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(i) + "\n" for i in items[-200:]))
    os.replace(tmp, f)


def message(text, target=None):
    where = []
    t = target or {}
    if t.get("file"):
        loc = t["file"]
        if t.get("lines"):
            a, b = t["lines"]
            loc += f" line {a + 1}" if a == b else f" lines {a + 1}-{b + 1}"
        where.append(loc)
    if t.get("selector"):
        where.append(f"page element `{t['selector']}`")
    if t.get("text"):
        where.append(f"the part that says \"{t['text'][:80]}\"")
    if t.get("url"):
        where.append(f"on {t['url']}")
    ctx = (" About " + ", ".join(where) + ":") if where else ""
    return f"[Live feedback from the user, sent from nyrasview while watching you work.]{ctx} {text.strip()}"


def add(agent, cwd, text, target=None):
    items = _read(cwd)
    item = {"id": f"{int(time.time() * 1000):x}", "agent": agent, "text": text.strip(), "target": target or {},
            "message": message(text, target), "at": time.time(), "status": "queued"}
    items.append(item)
    _write(cwd, items)
    if agent != "claude" or not installed():
        if shutil.which("wl-copy"):
            subprocess.run(["wl-copy", item["message"]], timeout=3)
            item["status"] = "copied"
        elif shutil.which("xclip"):
            subprocess.run(["xclip", "-selection", "clipboard"], input=item["message"].encode(), timeout=3)
            item["status"] = "copied"
        items[-1] = item
        _write(cwd, items)
    return item


def listing(cwd):
    return _read(cwd)[-30:]


def take(cwd):
    items = _read(cwd)
    pending = [i for i in items if i["status"] == "queued"]
    if not pending:
        return []
    for i in items:
        if i["status"] == "queued":
            i["status"] = "delivered"
            i["delivered"] = time.time()
    _write(cwd, items)
    return pending


def hook():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    cwd = data.get("cwd") or os.getcwd()
    event = data.get("hook_event_name") or ""
    if event in ("Notification", "Stop"):
        try:
            from .notify import from_hook
            from_hook(data)
        except Exception:
            pass
    if event == "Notification":
        return
    if event == "Stop" and data.get("stop_hook_active"):
        return
    items = take(cwd)
    if not items:
        return
    text = "\n".join(i["message"] for i in items)
    if event == "Stop":
        print(json.dumps({"decision": "block", "reason": text}))
    elif event == "UserPromptSubmit":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event or "PostToolUse", "additionalContext": text}}))


def _cmd():
    exe = shutil.which("nyrasview")
    return f"{exe} hook" if exe else f"{sys.executable} -m nyrasview hook"


def _load():
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def installed():
    hooks = _load().get("hooks") or {}
    return any("nyrasview" in h.get("command", "") and h.get("command", "").endswith(" hook")
               for ev in EVENTS for m in hooks.get(ev, []) for h in m.get("hooks", []))


def install():
    s = _load()
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, SETTINGS.with_name(f"settings.json.bak-nyrasview-{int(time.time())}"))
    hooks = s.setdefault("hooks", {})
    cmd = _cmd()
    for ev in EVENTS:
        lst = hooks.setdefault(ev, [])
        lst[:] = [m for m in lst if not any("nyrasview" in h.get("command", "") and h.get("command", "").endswith(" hook") for h in m.get("hooks", []))]
        entry = {"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}
        if ev == "PostToolUse":
            entry["matcher"] = "*"
        lst.append(entry)
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, indent=2) + "\n")
    return cmd


def uninstall():
    s = _load()
    hooks = s.get("hooks") or {}
    for ev in list(hooks):
        hooks[ev] = [m for m in hooks[ev] if not any("nyrasview" in h.get("command", "") and h.get("command", "").endswith(" hook") for h in m.get("hooks", []))]
        if not hooks[ev]:
            del hooks[ev]
    if not hooks:
        s.pop("hooks", None)
    SETTINGS.write_text(json.dumps(s, indent=2) + "\n")
