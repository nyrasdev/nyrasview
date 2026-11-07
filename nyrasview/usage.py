import json
import os
import sqlite3
import time
from collections import defaultdict

from .util import HOME, day_start, parse_ts


def _claude(line, st):
    if '"usage"' not in line:
        return None
    o = json.loads(line)
    if o.get("type") != "assistant":
        return None
    m = o.get("message") or {}
    u = m.get("usage")
    if not u:
        return None
    rid = m.get("id") or o.get("requestId") or o.get("uuid")
    return (parse_ts(o.get("timestamp")), m.get("model") or "?",
            u.get("input_tokens") or 0, u.get("output_tokens") or 0,
            u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0,
            None, rid)


def _codex(line, st):
    if '"turn_context"' in line or '"session_meta"' in line:
        o = json.loads(line)
        p = o.get("payload") or {}
        if p.get("model"):
            st["model"] = p["model"]
        return None
    if '"token_count"' not in line:
        return None
    o = json.loads(line)
    p = o.get("payload") or {}
    info = p.get("info") or {}
    u = info.get("last_token_usage")
    if not u:
        return None
    inp = u.get("input_tokens") or 0
    cached = u.get("cached_input_tokens") or 0
    st["n"] = st.get("n", 0) + 1
    return (parse_ts(o.get("timestamp")), st.get("model", "codex"), max(0, inp - cached),
            u.get("output_tokens") or 0, cached, 0, None, f"{st['path']}#{st['n']}")


def _gemini_file(path):
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    msgs = d.get("messages") if isinstance(d, dict) else d
    out = []
    for i, m in enumerate(msgs or []):
        if not isinstance(m, dict):
            continue
        t = m.get("tokens")
        if not isinstance(t, dict):
            continue
        cached = t.get("cached") or 0
        out.append((parse_ts(m.get("timestamp")), m.get("model") or "gemini",
                    max(0, (t.get("input") or 0) - cached),
                    (t.get("output") or 0) + (t.get("thoughts") or 0), cached, 0, None,
                    f"{path}#{i}"))
    return out


def _opencode_obj(o, key):
    if not isinstance(o, dict) or o.get("role") != "assistant":
        return None
    t = o.get("tokens") or {}
    c = t.get("cache") or {}
    if not t:
        return None
    model = o.get("modelID") or "?"
    if o.get("providerID"):
        model = f"{o['providerID']}/{model}"
    return (parse_ts((o.get("time") or {}).get("created")), model, t.get("input") or 0,
            (t.get("output") or 0) + (t.get("reasoning") or 0), c.get("read") or 0,
            c.get("write") or 0, o.get("cost"), o.get("id") or key)


class UsageStore:
    def __init__(self):
        self.files = {}
        self.records = defaultdict(dict)

    def _jsonl(self, agent, path, fn):
        try:
            stt = os.stat(path)
        except OSError:
            return
        ent = self.files.get(path)
        if ent is None or stt.st_size < ent["off"] or stt.st_ino != ent["ino"]:
            ent = {"off": 0, "ino": stt.st_ino, "st": {"path": path}}
            self.files[path] = ent
        if stt.st_size == ent["off"]:
            return
        try:
            with open(path, "rb") as f:
                f.seek(ent["off"])
                chunk = f.read()
        except OSError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        ent["off"] += end + 1
        recs = self.records[agent]
        for raw in chunk[:end].split(b"\n"):
            if not raw:
                continue
            try:
                r = fn(raw.decode("utf-8", "replace"), ent["st"])
            except (ValueError, AttributeError, TypeError):
                continue
            if r:
                recs[r[7]] = r

    def _whole(self, agent, path, fn):
        try:
            sig = os.stat(path).st_mtime_ns
        except OSError:
            return
        ent = self.files.get(path)
        if ent and ent.get("sig") == sig:
            return
        self.files[path] = {"sig": sig}
        recs = self.records[agent]
        for r in fn(path):
            recs[r[7]] = r

    def refresh(self):
        for root in (HOME / ".claude/projects", HOME / ".config/claude/projects"):
            if root.is_dir():
                for p in root.rglob("*.jsonl"):
                    self._jsonl("claude", str(p), _claude)
        for root in (HOME / ".codex/sessions", HOME / ".codex/archived_sessions"):
            if root.is_dir():
                for p in root.rglob("*.jsonl"):
                    self._jsonl("codex", str(p), _codex)
        for agent, root in (("gemini", HOME / ".gemini/tmp"), ("qwen", HOME / ".qwen/tmp")):
            if root.is_dir():
                for p in root.glob("*/chats/*.json"):
                    self._whole(agent, str(p), _gemini_file)
        oc = HOME / ".local/share/opencode"
        msgdir = oc / "storage/message"
        if msgdir.is_dir():
            for p in msgdir.glob("*/*.json"):
                self._whole("opencode", str(p), lambda path: [r for r in [
                    _opencode_obj(_load(path), path)] if r])
        db = oc / "opencode.db"
        if db.exists():
            self._whole("opencode", str(db), _opencode_db)

    def summary(self):
        now = time.time()
        today = day_start(now)
        week = today - 6 * 86400
        days = [day_start(today - i * 86400 + 3600) for i in range(13, -1, -1)]
        out = {}
        for agent, recs in self.records.items():
            s = {w: {"in": 0, "out": 0, "cache": 0, "cost": 0.0, "msgs": 0, "has_cost": False}
                 for w in ("today", "week", "total")}
            models = defaultdict(lambda: {"tokens": 0, "msgs": 0, "out": 0})
            daily = defaultdict(int)
            last = 0
            for ts, model, i, o, cr, cw, cost, _ in recs.values():
                tot = i + o + cr + cw
                wins = ["total"]
                if ts >= week:
                    wins.append("week")
                if ts >= today:
                    wins.append("today")
                for w in wins:
                    b = s[w]
                    b["in"] += i + cw
                    b["out"] += o
                    b["cache"] += cr
                    b["msgs"] += 1
                    if cost:
                        b["cost"] += cost
                        b["has_cost"] = True
                mm = models[model]
                mm["tokens"] += tot
                mm["out"] += o
                mm["msgs"] += 1
                if ts >= days[0]:
                    daily[day_start(ts)] += tot
                last = max(last, ts)
            s["models"] = sorted(({"model": k, **v} for k, v in models.items()),
                                 key=lambda x: -x["tokens"])[:8]
            s["daily"] = [daily.get(d, 0) for d in days]
            s["last"] = last
            out[agent] = s
        return out


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _opencode_db(path):
    out = []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        rows = con.execute("SELECT id, data FROM message").fetchall()
        con.close()
    except sqlite3.Error:
        return out
    for mid, data in rows:
        try:
            r = _opencode_obj(json.loads(data), mid)
        except (ValueError, TypeError):
            continue
        if r:
            out.append(r)
    return out
