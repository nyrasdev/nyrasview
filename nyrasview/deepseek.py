import json
import time
import urllib.error
import urllib.request

from . import plat
from .util import STATE, config

HISTORY = STATE / "deepseek_balance.json"
KEEP = 600


def settings():
    c = config().get("deepseek", {})
    return {"base_url": (c.get("base_url") or "https://api.deepseek.com").rstrip("/"),
            "model": c.get("model") or "deepseek-chat", "low_balance": float(c.get("low_balance", 2)),
            "refresh_minutes": max(1.0, float(c.get("refresh_minutes", 5)))}


def key():
    from . import fallback
    return fallback.key("deepseek")


def _history():
    try:
        h = json.loads(HISTORY.read_text())
        return h if isinstance(h, list) else []
    except (OSError, ValueError):
        return []


def _record(balance, currency, now):
    h = [x for x in _history() if x[2:3] == [currency]]
    if h and now - h[-1][0] < 60:
        h[-1] = [now, balance, currency]
    else:
        h.append([now, balance, currency])
    h = h[-KEEP:]
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY.with_suffix(".tmp")
    tmp.write_text(json.dumps(h))
    plat.replace(tmp, HISTORY)
    return h


def burn(history, now, span=86400):
    pts = [x for x in history if x[0] >= now - span]
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < 600:
        return None
    spent = sum(max(0.0, a[1] - b[1]) for a, b in zip(pts, pts[1:]))
    return spent / ((pts[-1][0] - pts[0][0]) / 3600)


def fetch(now=None):
    k = key()
    if not k:
        return {"configured": False}
    now = now or time.time()
    st = settings()
    req = urllib.request.Request(st["base_url"] + "/user/balance",
                                 headers={"Authorization": f"Bearer {k}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"configured": True, "error": "DeepSeek says the API key is not valid" if e.code == 401 else f"DeepSeek API error {e.code}"}
    except Exception as e:
        return {"configured": True, "error": str(e)[:120]}
    infos = d.get("balance_infos") if isinstance(d, dict) else None
    if not infos or not isinstance(infos[0], dict):
        return {"configured": True, "error": "DeepSeek returned no wallet for this key"}
    w = infos[0]
    try:
        bal = float(w.get("total_balance") or 0)
    except (TypeError, ValueError):
        bal = 0.0
    cur = str(w.get("currency") or "USD").upper()
    h = _record(bal, cur, now)

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    out = {"configured": True, "error": None, "balance": bal, "currency": cur, "available": d.get("is_available", bal > 0),
           "granted": num(w.get("granted_balance")), "topped_up": num(w.get("topped_up_balance")),
           "burn_per_hour": burn(h, now), "at": now, "low": bal < st["low_balance"]}
    if out["low"]:
        from . import notify
        from .limits import amount
        notify.emit("limit_soon", "deepseek", "DeepSeek balance is low",
                    f"{amount(bal, cur.lower())} left · top up at platform.deepseek.com/top_up",
                    f"deepseek|low|{time.strftime('%Y-%m-%d', time.localtime(now))}")
    return out


def windows(d):
    if d and d.get("configured") and d.get("error"):
        return {}, {"error": d["error"], "source": "deepseek"}
    if not d or not d.get("configured") or d.get("balance") is None:
        return None, None
    b = d.get("burn_per_hour")
    eta = d["balance"] / b * 3600 if b and b > 0 and d["balance"] > 0 else None
    w = {"label": "Balance", "unit": d["currency"].lower(), "balance": d["balance"], "burn_per_hour": b, "eta": eta,
         "utilization": None, "resets": None, "measured_at": d.get("at"), "low": bool(d.get("low")),
         "empty": not d.get("available", True) or d["balance"] <= 0, "calibrated": True, "source": "deepseek"}
    return {"balance": w}, {"plan": "pay as you go", "source": "deepseek"}


def chat(prompt, model=None, timeout=900):
    k = key()
    if not k:
        raise RuntimeError("connect a DeepSeek key first: nyrasview keys deepseek")
    st = settings()
    req = urllib.request.Request(st["base_url"] + "/chat/completions", method="POST",
                                 data=json.dumps({"model": model or st["model"],
                                                  "messages": [{"role": "user", "content": prompt}]}).encode(),
                                 headers={"Authorization": f"Bearer {k}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = (json.loads(e.read().decode()).get("error") or {}).get("message")
        except Exception:
            msg = None
        raise RuntimeError(msg or f"DeepSeek API error {e.code}")
    if "error" in d:
        raise RuntimeError(str(d["error"])[:300])
    return d["choices"][0]["message"]["content"] or ""
