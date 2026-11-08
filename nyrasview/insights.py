import json
import re
import time
from collections import defaultdict

from . import plat
from .util import STATE, day_start

HISTORY = STATE / "limits_history.jsonl"
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25), "claude-mythos-5-1": (10.0, 50.0, 0.25), "claude-fable-5": (10.0, 50.0, None),
    "claude-opus-5-5": (4.0, 20.0, 0.20), "claude-opus-5": (5.0, 25.0, None), "claude-opus-4-8": (5.0, 25.0, None),
    "claude-opus-4-7": (5.0, 25.0, None), "claude-opus-4-6": (5.0, 25.0, None),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20), "claude-sonnet-5": (2.0, 10.0, None), "claude-sonnet-4-6": (3.0, 15.0, None),
    "claude-haiku-5-5": (0.10, 0.50, None), "claude-haiku-4-5": (1.0, 5.0, None),
}
_last = {}


def price(model):
    m = re.sub(r"\[.*?\]$", "", (model or "").lower().strip())
    m = re.sub(r"-\d{8}$", "", m)
    m = m.split("/")[-1].replace("anthropic.", "")
    p = PRICES.get(m)
    if not p:
        return None
    inp, out, cr = p
    return {"in": inp, "out": out, "cache_read": cr if cr is not None else inp * 0.1, "cache_write": inp * 1.25}


def api_cost(rec):
    p = price(rec[1])
    if not p:
        return None
    _, _, i, o, cr, cw, _, _ = rec
    return (i * p["in"] + o * p["out"] + cr * p["cache_read"] + cw * p["cache_write"]) / 1e6


def record_limits(snap):
    now = time.time()
    if not _last:
        for e in _tail_history(2 * 86400):
            _last[(e["agent"], e["window"])] = (e["t"], e["u"])
    lines = []
    for agent, wins in (snap or {}).items():
        for name, w in wins.items():
            u = w.get("utilization")
            if u is None or not w.get("calibrated", True):
                continue
            key = (agent, name)
            t0, u0 = _last.get(key, (0, None))
            if u0 is not None and abs(u - u0) < 0.01 and now - t0 < 600:
                continue
            _last[key] = (now, u)
            lines.append(json.dumps({"t": round(now, 1), "agent": agent, "window": name, "u": round(u, 4),
                                     "label": w.get("label"), "resets": w.get("resets")}))
    if not lines:
        return
    try:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY, "a") as f:
            f.write("\n".join(lines) + "\n")
        if HISTORY.stat().st_size > 3_000_000:
            keep = HISTORY.read_text().splitlines()[-15000:]
            tmp = HISTORY.with_suffix(".tmp")
            tmp.write_text("\n".join(keep) + "\n")
            plat.replace(tmp, HISTORY)
    except OSError:
        pass


def _tail_history(span):
    cut = time.time() - span
    try:
        lines = HISTORY.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-20000:]:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("t", 0) >= cut:
            out.append(e)
    return out


def _prompts(since):
    try:
        from . import search
        search.refresh()
        con = search._db()
        try:
            rows = con.execute("SELECT s.agent, COUNT(*) FROM docs d JOIN sessions s ON s.key = d.skey "
                               "WHERE d.role = 'user' AND CAST(d.ts AS REAL) >= ? GROUP BY s.agent", (since,)).fetchall()
        finally:
            con.close()
        return dict(rows)
    except Exception:
        return {}


def report(records, days=30, limit_days=7):
    now = time.time()
    today = day_start(now)
    starts = [day_start(today - i * 86400 + 3600) for i in range(days - 1, -1, -1)]
    first = starts[0]
    agents = {}
    for agent, recs in (records or {}).items():
        rs = list(recs.values()) if isinstance(recs, dict) else list(recs or [])
        daily = defaultdict(int)
        dcost = defaultdict(float)
        tok = cache = 0
        known = est = 0.0
        has_known = has_est = False
        models = defaultdict(lambda: {"tokens": 0, "api_cost": 0.0, "priced": False})
        for r in rs:
            ts = r[0]
            if ts < first:
                continue
            work = r[2] + r[3] + r[5]
            d = day_start(ts)
            daily[d] += work
            tok += work
            cache += r[4]
            if r[6] is not None:
                known += r[6] or 0
                has_known = True
                dcost[d] += r[6] or 0
            c = api_cost(r)
            mm = models[r[1]]
            mm["tokens"] += work + r[4]
            if c is not None:
                est += c
                has_est = True
                mm["api_cost"] += c
                mm["priced"] = True
                if r[6] is None:
                    dcost[d] += c
        if not tok and not cache:
            continue
        agents[agent] = {"daily": [daily.get(d, 0) for d in starts], "daily_cost": [round(dcost.get(d, 0.0), 4) for d in starts],
                         "tokens": tok, "cache_read": cache, "cost": round(known, 4) if has_known else None,
                         "api_cost": round(est, 4) if has_est else None,
                         "models": sorted(({"model": k, **v} for k, v in models.items()), key=lambda x: -x["tokens"])[:6]}
    prompts = _prompts(first)
    for agent, a in agents.items():
        n = prompts.get(agent, 0)
        a["prompts"] = n
        a["tokens_per_prompt"] = round(a["tokens"] / n) if n else None
        spend = a["cost"] if a["cost"] is not None else a["api_cost"]
        a["cost_per_prompt"] = round(spend / n, 4) if n and spend is not None else None
    hist = defaultdict(list)
    for e in _tail_history(limit_days * 86400):
        hist[(e["agent"], e["window"])].append([e["t"], e["u"]])
    hits = []
    try:
        from .notify import recent
        hits = [{"at": e["at"], "agent": e.get("agent"), "title": e.get("title")} for e in recent(400, now - limit_days * 86400)
                if e.get("kind") == "limit"]
    except Exception:
        hits = []
    for (agent, window), pts in hist.items():
        prev = 0.0
        for t, u in pts:
            if u >= 0.995 > prev:
                hits.append({"at": t, "agent": agent, "title": f"{window.replace('_', ' ')} window full"})
            prev = u
    hits.sort(key=lambda h: h["at"])
    hours = defaultdict(int)
    for h in hits:
        hours[time.localtime(h["at"]).tm_hour] += 1
    return {"days": [s for s in starts], "agents": agents,
            "limits": [{"agent": a, "window": w, "points": p} for (a, w), p in sorted(hist.items())],
            "hits": hits[-50:], "hit_hours": dict(hours), "since": first, "generated": now}
