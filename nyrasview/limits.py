import json
import os
import time
from pathlib import Path

from . import plat
from .util import HOME, STATE, parse_ts

FILE = STATE / "limits.json"
WIN = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}


def _load():
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save(d):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    plat.replace(tmp, FILE)


def record_claude(info, ts=None):
    if not isinstance(info, dict):
        return
    wins = info.get("unifiedWindows") or {}
    if not wins and info.get("rateLimitType"):
        wins = {info["rateLimitType"]: {"utilization": info.get("utilization"), "resetsAt": info.get("resetsAt"),
                                        "status": info.get("status")}}
    d = _load()
    c = d.setdefault("claude", {})
    now = ts or time.time()
    for name, w in wins.items():
        if not isinstance(w, dict) or w.get("utilization") is None:
            continue
        old = c.get(name)
        if old and old.get("seen", 0) > now:
            continue
        c[name] = {"utilization": float(w["utilization"]), "resets": w.get("resetsAt"), "status": w.get("status"),
                   "seen": now}
    c["overage"] = info.get("overageStatus")
    _save(d)


def scan_runs():
    root = STATE / "runs"
    if not root.is_dir():
        return
    d = _load()
    last = d.get("_scanned", 0)
    newest = last
    for p in root.glob("*/out.log"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m <= last:
            continue
        newest = max(newest, m)
        try:
            for line in p.read_text(errors="replace").splitlines():
                if '"rate_limit_event"' in line:
                    o = json.loads(line)
                    record_claude(o.get("rate_limit_info"), m)
        except (OSError, ValueError):
            continue
    if newest > last:
        d = _load()
        d["_scanned"] = newest
        _save(d)


def _pace(u, resets, span, at):
    if u is None or not resets or not span:
        return None, None
    elapsed = max(0.0, min(1.0, 1 - (resets - at) / span))
    eta = None
    if elapsed and 0 < u < 1 and u > elapsed:
        eta = (1 - u) / (u / (elapsed * span))
        if at + eta > resets:
            eta = None
    return round((u - elapsed) * 100), eta


def _weighted(records, start, end=None):
    end = end or time.time()
    return sum(i + o + cw for ts, _, i, o, cr, cw, _, _ in records if start <= ts <= end)


def claude_forecast(records):
    now = time.time()
    c = _load().get("claude", {})
    out = {}
    recs = list(records.values()) if isinstance(records, dict) else list(records or [])
    for name, span in WIN.items():
        w = c.get(name)
        resets = w.get("resets") if w else None
        if resets and resets < now:
            w, resets = None, None
        start = (resets - span) if resets else now - span
        used = _weighted(recs, start)
        burn = _weighted(recs, now - 1800) * 2
        est = None
        cap = None
        if w and w["utilization"] > 0.02:
            at_seen = _weighted(recs, start, w["seen"])
            if at_seen > 0:
                cap = at_seen / w["utilization"]
                est = min(1.5, w["utilization"] + max(0, used - at_seen) / cap)
            else:
                est = w["utilization"]
        eta = None
        if est is not None and cap and burn > 0 and est < 1:
            eta = (1 - est) * cap / burn * 3600
            if (resets and now + eta > resets) or eta > 6 * 3600:
                eta = None
        pace = _pace(est, resets, span, now)[0]
        out[name] = {"utilization": est, "measured": w["utilization"] if w else None,
                     "measured_at": w["seen"] if w else None, "resets": resets, "used_tokens": used,
                     "burn_per_hour": burn, "eta": eta, "pace": pace, "calibrated": cap is not None}
    return out


def codex_limits():
    root = HOME / ".codex/sessions"
    if not root.is_dir():
        return None
    files = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    for f in files:
        try:
            with open(f, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 400000))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            rl = (o.get("payload") or {}).get("rate_limits") or o.get("rate_limits")
            if not isinstance(rl, dict):
                continue
            seen = parse_ts(o.get("timestamp")) or os.stat(f).st_mtime
            out = {}
            for slot, name in (("primary", "five_hour"), ("secondary", "seven_day")):
                r = rl.get(slot)
                if not isinstance(r, dict) or r.get("used_percent") is None:
                    continue
                mins = r.get("window_minutes")
                span = WIN[name]
                if mins:
                    name = "five_hour" if mins <= 360 else "seven_day" if mins >= 10000 else f"{mins}m"
                    span = mins * 60
                resets = r.get("resets_at")
                if resets is None and r.get("resets_in_seconds") is not None:
                    resets = seen + r["resets_in_seconds"]
                now = time.time()
                if resets and resets < now:
                    continue
                u = r["used_percent"] / 100
                pace, eta = _pace(u, resets, span, seen)
                if eta is not None:
                    eta -= now - seen
                    if eta <= 0:
                        eta = None
                out[name] = {"utilization": u, "measured": u, "measured_at": seen, "resets": resets,
                             "pace": pace, "eta": eta, "calibrated": True}
            if out:
                return out
    return None


PLAN = {"pro": "Claude Pro", "max": "Claude Max", "team": "Claude Team", "enterprise": "Claude Enterprise", "free": "Claude Free"}
API_FILE = STATE / "claude_api.json"
KIND = {"session": ("five_hour", "Session (5h)", 5 * 3600), "weekly_all": ("seven_day", "Weekly", 7 * 86400)}


def _credentials():
    import subprocess
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
    f = base / ".credentials.json"
    raw = None
    if f.exists():
        try:
            raw = json.loads(f.read_text())
        except (OSError, ValueError):
            raw = None
    elif plat.MAC:
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                               capture_output=True, text=True, timeout=5)
            raw = json.loads(r.stdout) if r.returncode == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raw = None
    return (raw or {}).get("claudeAiOauth")


def plan_name(o):
    tier = (o.get("rateLimitTier") or "").lower()
    name = PLAN.get((o.get("subscriptionType") or "").lower(), "Claude")
    import re
    m = re.search(r"max_(\d+x)", tier)
    if m:
        name = ("Claude Team " if "team" in tier or "team" in name.lower() else "Claude Max ") + m.group(1)
    return name


def _window(kind, group, scope):
    if kind in KIND:
        return KIND[kind]
    label = ("Weekly" if group == "weekly" else group.title() if group else kind.replace("_", " ").title())
    if scope:
        label += f" · {str(scope).replace('_', ' ').title()}"
    return kind, label, 7 * 86400 if group == "weekly" else 5 * 3600


def claude_api(force=False):
    from .util import config
    if config().get("limits", {}).get("claude_api", True) is False:
        return None
    now = time.time()
    try:
        cached = json.loads(API_FILE.read_text())
    except (OSError, ValueError):
        cached = None
    every = max(60, int(config().get("limits", {}).get("refresh_seconds", 120)))
    if cached and not force and now - cached.get("at", 0) < every:
        return cached
    o = _credentials()
    if not o or not o.get("accessToken"):
        return None
    out = {"at": now, "plan": plan_name(o), "windows": {}, "error": None}
    if o.get("expiresAt") and o["expiresAt"] / 1000 < now:
        out["error"] = "Claude Code's login token expired. Open Claude Code once and it refreshes."
        if cached and cached.get("windows"):
            out["windows"] = cached["windows"]
            out["stale"] = True
        API_FILE.parent.mkdir(parents=True, exist_ok=True)
        API_FILE.write_text(json.dumps(out))
        return out
    import urllib.request
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + o["accessToken"], "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json", "User-Agent": "nyrasview"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
    except Exception as e:
        out["error"] = f"usage API: {str(e)[:120]}"
        if cached and cached.get("windows"):
            out["windows"] = cached["windows"]
            out["stale"] = True
        return out
    lst = d.get("limits") or []
    if not lst:
        for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
            w = d.get(key)
            if isinstance(w, dict) and w.get("utilization") is not None:
                lst.append({"kind": kind, "group": "weekly" if "seven" in key else "session",
                            "percent": w["utilization"], "resets_at": w.get("resets_at"), "severity": "normal"})
    for w in lst:
        if w.get("percent") is None:
            continue
        key, label, span = _window(w.get("kind") or "", w.get("group") or "", w.get("scope"))
        resets = parse_ts(w.get("resets_at")) or None
        u = float(w["percent"]) / 100
        pace, eta = _pace(u, resets, span, now)
        out["windows"][key] = {"label": label, "utilization": u, "measured": u, "measured_at": now, "resets": resets,
                               "severity": w.get("severity") or "normal", "active": w.get("is_active"),
                               "pace": pace, "eta": eta, "calibrated": True, "source": "api"}
        if key == "five_hour" or key == "seven_day":
            record_claude({"unifiedWindows": {key: {"utilization": u, "resetsAt": resets}}}, now)
    API_FILE.parent.mkdir(parents=True, exist_ok=True)
    API_FILE.write_text(json.dumps(out))
    return out


DEFAULT_BUDGETS = {
    "gemini": {"cap": 1000, "unit": "requests", "period": "day", "tz": "America/Los_Angeles",
               "label": "Daily requests", "plan": "free tier · 1,000 requests/day"},
    "qwen": {"cap": 2000, "unit": "requests", "period": "day",
             "label": "Daily requests", "plan": "free tier · 2,000 requests/day"},
}
PERIOD_LABEL = {"day": "Today", "week": "This week", "month": "This month"}


def budgets():
    from .util import config
    out = {k: dict(v) for k, v in DEFAULT_BUDGETS.items()}
    for k, v in ((config().get("limits") or {}).get("budget") or {}).items():
        if not isinstance(v, dict):
            continue
        if "cap" in v and not v["cap"]:
            out.pop(k, None)
            continue
        b = {**out.get(k, {}), **v}
        if "cap" in v:
            b["plan"] = v.get("plan") or "your budget"
        if b.get("cap"):
            out[k] = b
    return out


def amount(v, unit):
    v = v or 0
    if unit == "usd":
        return f"${v:.2f}"
    if unit == "requests":
        return f"{v:,.0f} req"
    from .util import hn
    return f"{hn(v)} tok"


def dur(s):
    s = max(0, int(s or 0))
    return f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"{s // 60} min"


def _amount(rec, unit):
    if unit == "requests":
        return 1
    if unit == "usd":
        return rec[6] or 0
    return rec[2] + rec[3] + rec[5]


def _bounds(period, tz, now):
    from datetime import datetime, timedelta
    zone = None
    if tz:
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(tz)
        except Exception:
            zone = None
    d = datetime.fromtimestamp(now, zone) if zone else datetime.fromtimestamp(now)
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start -= timedelta(days=start.weekday())
        end = start + timedelta(days=7)
    elif period == "month":
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        start = start.replace(day=1)
    else:
        end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def _window_for(used, burn, cap, start, end, now, label, unit):
    w = {"label": label, "unit": unit, "used": used, "burn_per_hour": burn, "resets": end, "measured_at": now}
    if cap:
        u = used / cap
        eta = (cap - used) / burn * 3600 if burn > 0 and used < cap else None
        if eta is not None and now + eta > end:
            eta = None
        w.update(utilization=u, measured=u, cap=cap, left=max(0.0, cap - used),
                 pace=round((u - (now - start) / (end - start)) * 100), eta=eta, calibrated=True)
    else:
        w.update(utilization=None, projected=used + burn * (end - now) / 3600 if burn > 0 else None, eta=None,
                 calibrated=False)
    return w


def agent_windows(records, skip=("claude", "codex"), now=None):
    now = now or time.time()
    out, meta = {}, {}
    bud = budgets()
    for agent, recs in (records or {}).items():
        if agent in skip:
            continue
        rs = list(recs.values()) if isinstance(recs, dict) else list(recs or [])
        if not rs or max(r[0] for r in rs) < now - 7 * 86400:
            continue
        b = bud.get(agent) or {}
        unit = b.get("unit", "tokens")
        period = b.get("period", "day")
        start, end = _bounds(period, b.get("tz"), now)
        used = sum(_amount(r, unit) for r in rs if start <= r[0] <= now)
        burn = sum(_amount(r, unit) for r in rs if now - 3600 <= r[0] <= now)
        label = b.get("label") or PERIOD_LABEL.get(period, period)
        w = _window_for(used, burn, float(b["cap"]) if b.get("cap") else None, start, end, now, label, unit)
        w["source"] = "budget" if b else "tracked"
        out[agent] = {period: w}
        meta[agent] = {"plan": b.get("plan"), "source": w["source"]}
    return out, meta


def openrouter_windows(o, now=None):
    if not o or not o.get("configured") or o.get("error"):
        return None, None
    now = now or time.time()
    day0, day1 = _bounds("day", "UTC", now)
    hours = (now - day0) / 3600
    week_hours = (now - _bounds("week", "UTC", now)[0]) / 3600
    rate = 0.0
    if hours >= 3:
        rate = (o.get("daily") or 0) / hours
    elif o.get("weekly") and week_hours >= 3:
        rate = o["weekly"] / week_hours
    plan = o.get("label") or ("free tier" if o.get("free") else None)
    if o.get("limit"):
        cap = float(o["limit"])
        left = o.get("remaining")
        left = cap - (o.get("usage") or 0) if left is None else float(left)
        period = {"daily": "day", "weekly": "week", "monthly": "month"}.get(o.get("reset"))
        if period:
            start, end = _bounds(period, "UTC", now)
            w = _window_for(cap - left, rate, cap, start, end, now, "Credits", "usd")
        else:
            w = _window_for(cap - left, rate, cap, now - 1, now + 3650 * 86400, now, "Credits", "usd")
            w.update(resets=None, pace=None)
    else:
        w = _window_for(o.get("daily") or 0, rate, None, day0, day1, now, "Today (UTC)", "usd")
    w["source"] = "openrouter"
    return {"credits": w}, {"plan": plan, "source": "openrouter"}


META = {}


def snapshot(usage_records, openrouter=None):
    global META
    scan_runs()
    out = {}
    META = {}
    api = claude_api()
    if api and api.get("windows"):
        out["claude"] = dict(api["windows"])
        META = {"claude": {"plan": api.get("plan"), "error": api.get("error"), "stale": api.get("stale"),
                           "source": "anthropic"}}
    else:
        c = claude_forecast(usage_records.get("claude", {}))
        if any(v["utilization"] is not None or v["used_tokens"] for v in c.values()):
            out["claude"] = c
            if api and api.get("error"):
                META = {"claude": {"error": api["error"], "plan": api.get("plan"), "source": "estimate"}}
        elif "claude" in out:
            META = {"claude": {"source": "estimate"}}
    x = codex_limits()
    if x:
        out["codex"] = x
        META["codex"] = {"source": "codex"}
    aw, am = agent_windows(usage_records)
    out.update(aw)
    META.update(am)
    ow, om = openrouter_windows(openrouter)
    if ow:
        out["openrouter"] = ow
        META["openrouter"] = om
    return out
