import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta

from . import plat
from .registry import BY_KEY
from .util import STATE, config

DIR = STATE / "queue"
LOCK = DIR / ".lock"
ALIVE = DIR / ".alive"
LIMITED = DIR / ".limited"
LABEL = {"five_hour": "5-hour", "seven_day": "weekly"}
RESUME = ("You were stopped by a usage limit before you finished. Continue the task from where you left off. "
          "Check the files first: some of the work may already be done.")
TAKEOVER = ("\n\n(Another agent started on this and was stopped by its usage limit. Check the current state of the "
            "files first, then finish the job.)")


def settings():
    c = config().get("queue", {})
    return {"full_pct": float(c.get("full_pct", 98)), "retry_minutes": float(c.get("retry_minutes", 30)),
            "max_tries": int(c.get("max_tries", 4)), "grace_seconds": int(c.get("grace_seconds", 60))}


def _name(k):
    a = BY_KEY.get(k)
    return a.name if a else k


def _path(qid):
    return DIR / f"{qid}.json"


def _save(it):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / f"{it['id']}.tmp"
    tmp.write_text(json.dumps(it, indent=1))
    plat.replace(tmp, _path(it["id"]))


def load(qid):
    try:
        return json.loads(_path(qid).read_text())
    except (OSError, ValueError):
        return None


def _all():
    if not DIR.is_dir():
        return []
    out = []
    for p in DIR.glob("*.json"):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda x: (x.get("created", 0), x["id"]))


def _clock(ts):
    d = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    if d.date() == today:
        return d.strftime("%H:%M")
    if d.date() == today + timedelta(days=1):
        return "tomorrow " + d.strftime("%H:%M")
    return d.strftime("%a %H:%M")


def parse_at(s, now=None):
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    now = now or time.time()
    s = s.strip().lower()
    m = re.fullmatch(r"(?:in\s+)?\+?\s*(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)", s)
    if m:
        n, u = float(m.group(1)), m.group(2)[0]
        return now + n * {"m": 60, "h": 3600, "d": 86400}[u]
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if m and (m.group(2) or m.group(3)):
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if m.group(3) == "pm" and h < 12:
            h += 12
        if m.group(3) == "am" and h == 12:
            h = 0
        if h > 23 or mi > 59:
            raise ValueError(f"not a time: {s}")
        d = datetime.fromtimestamp(now).replace(hour=h, minute=mi, second=0, microsecond=0)
        if d.timestamp() <= now:
            d += timedelta(days=1)
        return d.timestamp()
    try:
        return datetime.fromisoformat(s.upper().replace(" ", "T")).timestamp()
    except ValueError:
        raise ValueError(f"can't read the time {s!r}: use 03:00, 7pm, +2h, 30m or 2026-11-15 03:00")


def _limited():
    try:
        return json.loads(LIMITED.read_text())
    except (OSError, ValueError):
        return {}


def _mark_limited(agent, until):
    d = {k: v for k, v in _limited().items() if v > time.time()}
    d[agent] = until
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = LIMITED.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    plat.replace(tmp, LIMITED)


def room(agent, lim, model=None, now=None):
    now = now or time.time()
    st = settings()
    full = st["full_pct"] / 100
    worst = None
    for name, w in ((lim or {}).get(agent) or {}).items():
        if not isinstance(w, dict):
            continue
        u = w.get("utilization")
        if u is None or u < full:
            continue
        label = w.get("label") or LABEL.get(name, name)
        if " · " in label and not (model and label.split(" · ")[-1].lower() in model.lower()):
            continue
        r = w.get("resets")
        if r and now >= r + st["grace_seconds"]:
            continue
        until = r + st["grace_seconds"] if r else None
        if worst is None or (until or float("inf")) > (worst[0] or float("inf")):
            worst = (until, label)
    if worst:
        return False, worst[0], f"{_name(agent)}'s {worst[1].lower()} limit is full"
    hit = _limited().get(agent)
    if hit and hit > now:
        return False, hit, f"{_name(agent)} hit its usage limit"
    return True, None, ""


def runnable(key):
    from .runs import STREAM
    a = BY_KEY.get(key)
    if not a or not a.headless or a.headless.get("http"):
        return False
    cmd = (STREAM.get(key) or {}).get("cmd") or a.headless.get("cmd") or [None]
    return bool(cmd[0] and plat.which(cmd[0]))


def _other(it, lim, now):
    from .handoff import CHAIN
    chain = config().get("handoff", {}).get("chain") or CHAIN
    for k in chain:
        if k != it["agent"] and runnable(k) and room(k, lim, None, now)[0]:
            return k
    return None


def add(agent, prompt, cwd, mode="ask", model=None, at=None, fallback=False, api=False, sandbox=None, group=None):
    from .runs import MODES
    if agent not in BY_KEY or not BY_KEY[agent].headless:
        raise ValueError(f"{agent} can't be prompted from nyrasview")
    if mode not in MODES:
        raise ValueError("mode must be ask, edit or auto")
    if not (prompt or "").strip():
        raise ValueError("write a prompt")
    cwd = os.path.realpath(os.path.expanduser(cwd or "~"))
    if not os.path.isdir(cwd):
        raise FileNotFoundError(cwd)
    it = {"id": secrets.token_hex(4), "agent": agent, "prompt": prompt.strip(), "cwd": cwd, "mode": mode,
          "model": model or None, "at": parse_at(at), "fallback": bool(fallback), "api": bool(api),
          "sandbox": sandbox, "group": group, "created": time.time(), "status": "waiting", "run": None,
          "tries": 0, "why": None, "until": None}
    with plat.file_lock(LOCK):
        _save(it)
    return it


def remove(qid):
    with plat.file_lock(LOCK):
        it = load(qid)
        if not it:
            return False
        _path(qid).unlink(missing_ok=True)
        return True


def start_now(qid):
    with plat.file_lock(LOCK):
        it = load(qid)
        if not it:
            raise ValueError("no such queued prompt")
        if it["status"] == "running":
            return it
        it.update(status="waiting", error=None)
        _start(it, it["agent"], time.time())
        _save(it)
        return it


def _start(it, agent, now):
    from . import runs
    prompt, parent = it["prompt"], None
    if it.get("resume"):
        if agent == it.get("resume_agent"):
            prompt, parent = RESUME, it["resume"]
        else:
            prompt = it["prompt"] + TAKEOVER
    try:
        m = runs.start(agent, prompt, it["cwd"], it["mode"], it.get("model") if agent == it["agent"] else None,
                       it.get("group"), parent, bool(it.get("api")) and agent == it["agent"], it.get("sandbox"),
                       origin="queue")
    except Exception as e:
        it.update(status="failed", error=str(e)[:300], finished=now)
        return
    if m["status"] == "failed":
        it.update(status="failed", error=m.get("error"), run=m["id"], finished=now)
        return
    it.update(status="running", run=m["id"], ran_on=agent, started=now, why=None, until=None)
    from . import notify
    who = _name(agent) + (f" (instead of {_name(it['agent'])})" if agent != it["agent"] else "")
    notify.emit("queue", agent, f"{who} started your queued prompt", it["prompt"][:160],
                f"{it['id']}|{it['tries']}", cwd=it["cwd"], extra={"run": m["id"]})


def _reset_ts(text, now):
    m = re.search(r"\|(\d{10})\b", text or "")
    if m and int(m.group(1)) > now:
        return float(m.group(1))
    return None


def _follow(it, now):
    from . import runs
    from .handoff import _limit
    m = runs.load(it["run"]) if it.get("run") else None
    if not m:
        it.update(status="failed", error="its run is gone", finished=now)
        return True
    if m["status"] == "running":
        if m.get("pid") and not plat.alive(m["pid"]):
            runs.view(m["id"])
            m = runs.load(m["id"]) or m
        if m["status"] == "running":
            return False
    if m.get("retried_as"):
        it["run"] = m["retried_as"]
        return True
    if m["status"] in ("done", "stopped"):
        it.update(status=m["status"], finished=now)
        return True
    if now - (m.get("finished") or now) < 8:
        return False
    ev = runs.events(m)
    text = (m.get("error") or "") + " " + " ".join(e.get("text") or "" for e in ev if e["k"] in ("text", "done"))
    if not _limit(text):
        it.update(status="failed", error=(m.get("error") or "failed")[:300], finished=now)
        return True
    st = settings()
    it["tries"] = it.get("tries", 0) + 1
    if it["tries"] >= st["max_tries"]:
        it.update(status="failed", error=f"hit the usage limit {it['tries']} times in a row", finished=now)
        return True
    until = _reset_ts(text, now) or now + st["retry_minutes"] * 60
    _mark_limited(m["agent"], until)
    if any(e["k"] == "tool" or (e["k"] == "text" and e.get("text", "").strip() and not _limit(e["text"])) for e in ev):
        it.update(resume=m["id"], resume_agent=m["agent"])
    it.update(status="waiting", run=None, why=f"{_name(m['agent'])} hit its usage limit", until=until)
    return True


def tick(lim=None, now=None, heartbeat=True):
    now = now or time.time()
    try:
        with plat.file_lock(LOCK):
            if heartbeat:
                ALIVE.write_text(json.dumps({"at": now, "pid": os.getpid()}))
            return _tick(lim, now)
    except OSError:
        return listing()


def _tick(lim, now):
    if lim is None:
        lim = _limits()
    items = _all()
    hold = {}
    for it in items:
        before = json.dumps(it, sort_keys=True)
        key = os.path.normcase(it["cwd"])
        if it["status"] in ("done", "stopped") and now - (it.get("finished") or now) > 86400:
            _path(it["id"]).unlink(missing_ok=True)
            continue
        if it["status"] == "failed" and now - (it.get("finished") or now) > 3 * 86400:
            _path(it["id"]).unlink(missing_ok=True)
            continue
        if it["status"] == "running":
            _follow(it, now)
        if it["status"] == "waiting":
            _consider(it, lim, now, hold.get(key))
        if it["status"] in ("waiting", "running"):
            hold.setdefault(key, (it.get("group"), "waits for the prompt before it in this folder"))
        elif it["status"] == "failed":
            hold.setdefault(key, (None, "on hold: the prompt before it in this folder failed"))
        if json.dumps(it, sort_keys=True) != before and _path(it["id"]).exists():
            _save(it)
    return listing()


def _consider(it, lim, now, held):
    if held and (held[0] is None or held[0] != it.get("group")):
        it.update(why=held[1], until=None)
        return
    if it.get("at") and now < it["at"]:
        it.update(why="starts at " + _clock(it["at"]), until=it["at"])
        return
    ok, until, why = room(it["agent"], lim, it.get("model"), now)
    agent = it["agent"]
    if not ok and it.get("fallback"):
        agent = _other(it, lim, now)
        ok = agent is not None
    if not ok:
        it.update(why=why + (f", resets {_clock(until)}" if until else ""), until=until)
        return
    _start(it, agent, now)


def _limits():
    from . import limits
    from .usage import UsageStore
    u = UsageStore()
    u.refresh()
    return limits.snapshot(u.records)


def alive(now=None):
    try:
        d = json.loads(ALIVE.read_text())
    except (OSError, ValueError):
        return False
    return (now or time.time()) - d.get("at", 0) < 45 and plat.alive(d.get("pid"))


def listing():
    out = []
    for it in _all():
        if it["status"] in ("done", "stopped"):
            continue
        out.append({k: it.get(k) for k in ("id", "agent", "prompt", "cwd", "mode", "model", "at", "fallback", "created",
                                           "status", "run", "ran_on", "tries", "why", "until", "error", "group")})
    return out
