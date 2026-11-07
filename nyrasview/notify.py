import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request

from . import plat
from .registry import BY_KEY
from .util import HOME, STATE, config

EVENTS = STATE / "events.jsonl"
SEEN = STATE / "notified.json"
KINDS = ("approval", "finished", "limit", "limit_soon", "failed", "done")
_mem = threading.Lock()


def settings():
    c = config().get("notify", {})
    return {"desktop": c.get("desktop", True), "ntfy": c.get("ntfy") or os.environ.get("NYRA_NTFY"),
            "events": c.get("events", list(KINDS)), "finished_after": c.get("finished_after", 30),
            "soon_pct": c.get("soon_pct", 90), "soon_minutes": c.get("soon_minutes", 15)}


def _claim(key):
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    with _mem, plat.file_lock(str(SEEN) + ".lock"):
        try:
            d = json.loads(SEEN.read_text())
        except (OSError, ValueError):
            d = {}
        if key in d:
            return False
        now = time.time()
        d = {k: v for k, v in d.items() if now - v < 3 * 86400}
        d[key] = now
        tmp = SEEN.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        plat.replace(tmp, SEEN)
        return True


def emit(kind, agent, title, body, key, cwd=None, urgent=False, extra=None):
    st = settings()
    if kind not in st["events"] or not _claim(f"{kind}|{key}"):
        return None
    ev = {"id": f"{int(time.time() * 1000):x}", "kind": kind, "agent": agent, "title": title, "body": body,
          "cwd": cwd, "at": time.time(), "urgent": urgent, **(extra or {})}
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with _mem, open(EVENTS, "a") as f:
        f.write(json.dumps(ev) + "\n")
    if st["desktop"]:
        plat.notify(title, body, urgent)
    if st["ntfy"]:
        threading.Thread(target=_ntfy, args=(st["ntfy"], title, body, urgent), daemon=True).start()
    return ev


def _ntfy(target, title, body, urgent):
    url = target if target.startswith("http") else f"https://ntfy.sh/{target}"
    req = urllib.request.Request(url, data=body.encode()[:3000],
                                 headers={"Title": title.encode("ascii", "replace").decode(),
                                          "Priority": "high" if urgent else "default", "Tags": "robot"})
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass


def recent(limit=40, since=0):
    try:
        lines = EVENTS.read_text().splitlines()[-400:]
    except OSError:
        return []
    out = []
    for l in lines:
        try:
            e = json.loads(l)
        except ValueError:
            continue
        if e["at"] > since:
            out.append(e)
    return out[-limit:]


def _name(k):
    return BY_KEY[k].name if k in BY_KEY else k


def _short(p):
    return (p or "").replace(str(HOME), "~")


def from_hook(data):
    ev = data.get("hook_event_name")
    cwd = data.get("cwd") or ""
    sid = data.get("session_id") or cwd
    proj = os.path.basename(cwd.rstrip("/")) or "~"
    if ev == "Notification":
        msg = data.get("message") or "Claude Code needs your attention"
        kind = "approval" if "permission" in msg.lower() else "finished"
        title = f"Claude Code needs you · {proj}" if kind == "approval" else f"Claude Code is waiting · {proj}"
        emit(kind, "claude", title, msg, f"{sid}|{int(time.time() // 20)}", cwd, urgent=kind == "approval")
    elif ev == "Stop" and not data.get("stop_hook_active"):
        emit("finished", "claude", f"Claude Code finished · {proj}", "Done. Your turn.",
             f"{sid}|stop|{int(time.time() // 20)}", cwd)


class Watcher:
    def __init__(self, relay=False):
        self.relay = relay
        self.sess = {}
        self.runs = None
        self.tasks = None

    def tick(self, snap):
        if not snap:
            return
        st = settings()
        from .feedback import installed as hooks_installed
        hooked = hooks_installed()
        now = time.time()
        alive = set()
        for a in snap.get("agents", []):
            for w in a.get("work") or []:
                if w.get("collab") or not w.get("activity"):
                    continue
                act = w["activity"]
                k = (a["key"], w["pid"])
                alive.add(k)
                s = self.sess.setdefault(k, {"state": act["state"], "since": now, "worked": 0})
                proj = os.path.basename((w.get("cwd") or "").rstrip("/")) or "~"
                if act["state"] == "limited":
                    lim = act.get("limit") or {}
                    emit("limit", a["key"], f"{a['name']} hit its limit · {proj}",
                         lim.get("message", "usage limit reached") + (f" · resets {lim['reset']}" if lim.get("reset") else ""),
                         f"{a['key']}|{w['pid']}|{lim.get('message')}", w.get("cwd"), urgent=True)
                if not (hooked and a["key"] == "claude"):
                    pend = act.get("pending")
                    age = now - (act.get("mtime") or now)
                    if act["state"] != "working" and pend and 6 < age < 900 and w.get("cpu", 0) < 3:
                        emit("approval", a["key"], f"{a['name']} needs you · {proj}", f"Waiting to run: {pend}",
                             f"{a['key']}|{w['pid']}|{pend}|{int(act.get('mtime') or 0)}", w.get("cwd"), urgent=True)
                    if s["state"] == "working" and act["state"] in ("waiting", "idle") and not pend:
                        if now - s["since"] >= st["finished_after"]:
                            emit("finished", a["key"], f"{a['name']} finished · {proj}",
                                 (act.get("reply") or act.get("action") or "Done.")[:200],
                                 f"{a['key']}|{w['pid']}|{int(act.get('mtime') or now)}", w.get("cwd"))
                if act["state"] != s["state"]:
                    s["state"], s["since"] = act["state"], now
        for k in list(self.sess):
            if k not in alive:
                del self.sess[k]
        self._limits(snap, st)
        self._runs(st)
        self._tasks(snap)

    def _limits(self, snap, st):
        for agent, wins in (snap.get("limits") or {}).items():
            for name, w in wins.items():
                u = w.get("utilization")
                if u is None:
                    continue
                label = w.get("label") or {"five_hour": "5-hour", "seven_day": "weekly"}.get(name, name)
                eta = w.get("eta")
                soon = u * 100 >= st["soon_pct"] or (eta is not None and eta < st["soon_minutes"] * 60) or \
                    (w.get("severity") or "normal") not in ("normal", "low")
                if soon and u < 1:
                    body = f"{u * 100:.0f}% of the {label} limit used"
                    if eta:
                        body += f" · about {eta / 60:.0f} min left at this pace"
                    if w.get("resets"):
                        body += f" · resets {time.strftime('%H:%M', time.localtime(w['resets']))}"
                    ready = []
                    if self.relay and agent in ("claude", "codex"):
                        from . import handoff
                        for a in snap.get("agents", []):
                            if a["key"] != agent:
                                continue
                            for wk in a.get("work") or []:
                                if wk.get("cwd") and not wk.get("collab") and wk["cwd"] != str(HOME):
                                    try:
                                        handoff.prepare(agent, wk["cwd"])
                                        ready.append(os.path.basename(wk["cwd"]))
                                    except Exception:
                                        pass
                    if ready:
                        body += " · handoff ready for " + ", ".join(ready)
                    emit("limit_soon", agent, f"{_name(agent)} is close to its {label} limit", body,
                         f"{agent}|{name}|{int(w.get('resets') or 0)}", urgent=False, extra={"window": name})

    def _runs(self, st):
        from . import runs
        cur = {r["id"]: r for r in runs.listing(60)}
        if self.runs is not None:
            for rid, r in cur.items():
                old = self.runs.get(rid)
                if old and old["status"] == "running" and r["status"] in ("done", "failed"):
                    kind = "done" if r["status"] == "done" else "failed"
                    emit(kind, r["agent"], f"{_name(r['agent'])} {'answered' if kind == 'done' else 'failed'}",
                         (r.get("error") or r["prompt"] or "")[:200], f"run|{rid}", r.get("cwd"), extra={"run": rid})
        self.runs = cur

    def _tasks(self, snap):
        cur = {}
        for d in snap.get("collab") or []:
            for t in d["tasks"]:
                cur[(d["doc"], t["id"])] = (t, d)
        if self.tasks is not None:
            for k, (t, d) in cur.items():
                old = self.tasks.get(k)
                if old and old[0]["status"] == "running" and t["status"] in ("done", "failed"):
                    kind = "done" if t["status"] == "done" else "failed"
                    emit(kind, t["agent"], f"{_name(t['agent'])} {'wrote' if kind == 'done' else 'failed on'} {t['section'] or 'the document'}",
                         (t.get("error") or t["instruction"])[:200], f"task|{t['id']}|{t.get('finished')}",
                         extra={"doc": d["doc"]})
        self.tasks = cur
