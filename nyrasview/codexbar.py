import json
import os
import subprocess
import threading
import time

from . import plat
from .util import STATE, config, parse_ts

CACHE = STATE / "codexbar.json"
AGENT = {"qwencloud": "qwen", "opencodego": "opencode", "openai": "codex"}
NAMES = {"azureopenai": "Azure OpenAI", "alibaba": "Alibaba", "alibabatokenplan": "Alibaba Token Plan", "kilo": "Kilo",
         "zai": "z.ai", "kiro": "Kiro", "grok": "Grok", "vertexai": "Vertex AI", "jetbrains": "JetBrains AI"}
OWN = ("anthropic", "codex", "openrouter", "deepseek")
_busy = threading.Lock()


def settings():
    c = config().get("codexbar", {})
    return {"enabled": c.get("enabled", True) is not False, "path": os.path.expanduser(c.get("path") or "codexbar"),
            "refresh_seconds": max(30, int(c.get("refresh_seconds", 120)))}


def binary():
    st = settings()
    if not st["enabled"]:
        return None
    return plat.which(st["path"]) or (st["path"] if os.path.isfile(st["path"]) else None)


def cached():
    try:
        d = json.loads(CACHE.read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _save(d):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    plat.replace(tmp, CACHE)
    return d


def _providers(o):
    if isinstance(o, list):
        return [p for p in o if isinstance(p, dict)]
    if isinstance(o, dict):
        if isinstance(o.get("providers"), list):
            return [p for p in o["providers"] if isinstance(p, dict)]
        if o.get("provider") or o.get("id") or o.get("usage"):
            return [o]
    return []


def _error(e):
    if isinstance(e, dict):
        return str(e.get("message") or e.get("description") or e.get("kind") or "error")[:200]
    return str(e)[:200] if e else None


def fetch(force=False, now=None):
    now = now or time.time()
    exe = binary()
    if not exe:
        return None
    st = settings()
    old = cached()
    if old and not force and now - old.get("at", 0) < st["refresh_seconds"]:
        return old
    if not _busy.acquire(blocking=False):
        return old
    try:
        try:
            r = subprocess.run(plat.resolve([exe, "usage", "--format", "json", "--json-only"]), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=30, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return _save({"at": now, "error": "codexbar took longer than 30s", "providers": (old or {}).get("providers", []),
                          "stale": True})
        except OSError as e:
            return _save({"at": now, "error": str(e)[:160], "providers": []})
        try:
            o = json.loads(r.stdout)
        except ValueError:
            o = None
        provs = _providers(o)
        err = None
        if not provs:
            err = _error(o.get("error")) if isinstance(o, dict) and o.get("error") else None
            err = err or (r.stderr or r.stdout or "").strip()[:160] or "codexbar returned no provider data"
            return _save({"at": now, "error": err, "providers": (old or {}).get("providers", []), "stale": True})
        return _save({"at": now, "error": None, "providers": provs})
    finally:
        _busy.release()


def refresh_async():
    if not binary():
        return
    old = cached()
    if old and time.time() - old.get("at", 0) < settings()["refresh_seconds"]:
        return
    threading.Thread(target=fetch, daemon=True).start()


def _label(mins, title):
    if title:
        return str(title)
    if not mins:
        return "Usage"
    if mins == 300:
        return "Session (5h)"
    if mins == 10080:
        return "Weekly"
    if mins == 1440:
        return "Daily"
    if mins % 1440 == 0:
        return f"{mins // 1440}-day"
    return f"{mins // 60}-hour" if mins % 60 == 0 else f"{mins} min"


def _name(mins, slot):
    return {300: "five_hour", 10080: "seven_day", 1440: "day"}.get(mins) or (f"{mins}m" if mins else slot)


def _wins(p, now):
    from .limits import _pace
    u = p.get("usage") if isinstance(p.get("usage"), dict) else {}
    seen = parse_ts(u.get("updatedAt")) or now
    found = []
    for slot in ("primary", "secondary", "tertiary"):
        if isinstance(u.get(slot), dict):
            found.append((slot, u[slot], None))
    for key in ("extraRateWindows", "windows"):
        for i, w in enumerate(u.get(key) or []):
            if isinstance(w, dict):
                inner = w.get("window") if isinstance(w.get("window"), dict) else w
                found.append((f"{key}{i}", inner, w.get("title") or w.get("name")))
    out = {}
    for slot, w, title in found:
        if w.get("usedPercent") is None:
            continue
        try:
            used = float(w["usedPercent"]) / 100
        except (TypeError, ValueError):
            continue
        mins = w.get("windowMinutes")
        mins = int(mins) if isinstance(mins, (int, float)) and mins > 0 else None
        resets = parse_ts(w.get("resetsAt")) or None
        if resets and resets < now:
            continue
        span = mins * 60 if mins else None
        pace, eta = _pace(used, resets, span, seen)
        if eta is not None:
            eta -= now - seen
            eta = eta if eta > 0 else None
        name = _name(mins, slot)
        while name in out:
            name += "_"
        out[name] = {"label": _label(mins, title or w.get("title") or w.get("name") or w.get("label")), "utilization": used,
                     "measured": used, "measured_at": seen, "resets": resets, "pace": pace, "eta": eta,
                     "calibrated": True, "source": "codexbar"}
    cr = u.get("credits") if isinstance(u.get("credits"), dict) else p.get("credits")
    if isinstance(cr, dict) and isinstance(cr.get("remaining"), (int, float)):
        out["credits"] = {"label": "Credits", "unit": "credits", "balance": float(cr["remaining"]), "burn_per_hour": None,
                          "eta": None, "utilization": None, "resets": None, "measured_at": seen,
                          "low": False, "empty": cr["remaining"] <= 0, "calibrated": True, "source": "codexbar"}
    return out


def windows(have_meta, now=None):
    now = now or time.time()
    if not binary():
        return {}, {}
    d = cached()
    if not d:
        return {}, {}
    out, meta = {}, {}
    age = now - d.get("at", 0)
    for p in d.get("providers") or []:
        pid = str(p.get("provider") or p.get("id") or "").lower()
        if not pid:
            continue
        key = AGENT.get(pid, pid)
        if (have_meta.get(key) or {}).get("source") in OWN:
            continue
        w = _wins(p, now)
        err = _error(p.get("error"))
        if not w and not err:
            continue
        u = p.get("usage") if isinstance(p.get("usage"), dict) else {}
        ident = u.get("identity") if isinstance(u.get("identity"), dict) else {}
        plan = u.get("loginMethod") or ident.get("loginMethod")
        out[key] = w
        meta[key] = {"plan": str(plan) if plan else None, "source": "codexbar", "name": NAMES.get(pid),
                     "error": err or (d.get("error") if d.get("stale") else None),
                     "stale": bool(p.get("stale") or d.get("stale") or age > 3 * settings()["refresh_seconds"])}
    return out, meta
