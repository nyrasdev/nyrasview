import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from .activity import describe_tool
from .util import HOME, STATE, parse_ts

DB = STATE / "search.db"
MAXLEN = 4000
HIT_A, HIT_B = "\x02", "\x03"


def _db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB), timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, sig TEXT, off INTEGER);
        CREATE TABLE IF NOT EXISTS sessions(key TEXT PRIMARY KEY, agent TEXT, sid TEXT, cwd TEXT, title TEXT,
                                            started REAL, last REAL, path TEXT, n INTEGER DEFAULT 0);
        CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(text, role UNINDEXED, skey UNINDEXED, ts UNINDEXED,
                                                          path UNINDEXED, tokenize='porter unicode61');
    """)
    return con


def _clean(text):
    text = re.sub(r"<system-reminder>.*?</system-reminder>", " ", text or "", flags=re.S)
    return re.sub(r"\s+", " ", text).strip()[:MAXLEN]


class _Batch:
    def __init__(self, con):
        self.con = con
        self.sess = {}

    def session(self, agent, sid, path, cwd=None, title=None, ts=None):
        key = f"{agent}:{sid}"
        s = self.sess.get(key)
        if s is None:
            row = self.con.execute("SELECT cwd, title, started, last, n FROM sessions WHERE key=?", (key,)).fetchone()
            s = {"agent": agent, "sid": sid, "path": path, "cwd": row[0] if row else None, "title": row[1] if row else None,
                 "started": row[2] if row else None, "last": row[3] if row else None, "n": row[4] if row else 0}
            self.sess[key] = s
        if cwd:
            s["cwd"] = cwd
        if title:
            s["title"] = title
        if ts:
            s["started"] = min(s["started"] or ts, ts)
            s["last"] = max(s["last"] or ts, ts)
        return key

    def add(self, key, role, text, ts, path):
        text = _clean(text)
        if not text:
            return
        s = self.sess[key]
        if role == "user" and not s["title"]:
            s["title"] = text[:90]
        if ts:
            s["started"] = min(s["started"] or ts, ts)
            s["last"] = max(s["last"] or ts, ts)
        s["n"] += 1
        self.con.execute("INSERT INTO docs(text, role, skey, ts, path) VALUES (?,?,?,?,?)", (text, role, key, ts or 0, path))

    def flush(self):
        for key, s in self.sess.items():
            self.con.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                             (key, s["agent"], s["sid"], s["cwd"], s["title"], s["started"], s["last"], s["path"], s["n"]))
        self.sess.clear()


def _blocks(c):
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in c or [] if isinstance(b, dict)]


def _claude_line(b, path, o, st):
    sid = o.get("sessionId") or Path(path).stem
    ts = parse_ts(o.get("timestamp"))
    key = b.session("claude", sid, path, o.get("cwd"), None, ts)
    t = o.get("type")
    if t in ("ai-title", "summary"):
        b.session("claude", sid, path, title=o.get("aiTitle") or o.get("summary"))
    elif t == "user" and not o.get("isMeta"):
        for blk in _blocks((o.get("message") or {}).get("content")):
            if blk.get("type") == "text" and not str(blk.get("text", "")).lstrip().startswith("<"):
                b.add(key, "user", blk["text"], ts, path)
    elif t == "assistant":
        for blk in _blocks((o.get("message") or {}).get("content")):
            if blk.get("type") == "text":
                b.add(key, "agent", blk.get("text"), ts, path)
            elif blk.get("type") == "tool_use":
                b.add(key, "tool", describe_tool(blk.get("name"), blk.get("input")), ts, path)


def _codex_line(b, path, o, st):
    p = o.get("payload") or {}
    ts = parse_ts(o.get("timestamp"))
    if o.get("type") == "session_meta" or p.get("type") == "session_meta":
        st["sid"] = p.get("id") or st.get("sid")
        st["cwd"] = p.get("cwd") or st.get("cwd")
    sid = st.get("sid") or Path(path).stem
    key = b.session("codex", sid, path, st.get("cwd"), None, ts)
    pt = p.get("type")
    if pt == "user_message" and p.get("message"):
        b.add(key, "user", p["message"], ts, path)
    elif pt == "agent_message" and p.get("message"):
        b.add(key, "agent", p["message"], ts, path)
    elif pt in ("function_call", "custom_tool_call", "local_shell_call"):
        args = p.get("arguments") or p.get("input") or p.get("action") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"command": args}
        b.add(key, "tool", describe_tool(p.get("name") or "shell", args), ts, path)


def _jsonl(con, b, path, fn):
    try:
        stt = os.stat(path)
    except OSError:
        return
    row = con.execute("SELECT sig, off FROM files WHERE path=?", (path,)).fetchone()
    ino = str(stt.st_ino)
    off = row[1] if row and row[0] == ino and stt.st_size >= row[1] else 0
    if row and off == 0:
        con.execute("DELETE FROM docs WHERE path=?", (path,))
    if stt.st_size == off:
        return
    st = {}
    if off:
        try:
            with open(path, "rb") as f:
                head = f.readline()
            o = json.loads(head)
            p = o.get("payload") or {}
            st = {"sid": p.get("id"), "cwd": p.get("cwd")} if fn is _codex_line else {}
        except (OSError, ValueError):
            st = {}
    try:
        with open(path, "rb") as f:
            f.seek(off)
            chunk = f.read()
    except OSError:
        return
    end = chunk.rfind(b"\n")
    if end < 0:
        return
    for raw in chunk[:end].split(b"\n"):
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        if isinstance(o, dict):
            try:
                fn(b, path, o, st)
            except (AttributeError, TypeError, KeyError):
                continue
    con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?)", (path, ino, off + end + 1))


def _whole(con, path, sig):
    row = con.execute("SELECT sig FROM files WHERE path=?", (path,)).fetchone()
    if row and row[0] == sig:
        return False
    con.execute("DELETE FROM docs WHERE path=?", (path,))
    con.execute("INSERT OR REPLACE INTO files VALUES (?,?,0)", (path, sig))
    return True


def _gemini(con, b, agent, path, cwds):
    try:
        sig = str(os.stat(path).st_mtime_ns)
    except OSError:
        return
    if not _whole(con, path, sig):
        return
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return
    msgs = d.get("messages") if isinstance(d, dict) else d
    sid = (d.get("sessionId") if isinstance(d, dict) else None) or Path(path).stem
    cwd = cwds.get(Path(path).parent.parent.name)
    key = b.session(agent, sid, path, cwd)
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        ts = parse_ts(m.get("timestamp"))
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
        if m.get("type") == "user":
            b.add(key, "user", content, ts, path)
        else:
            b.add(key, "agent", content, ts, path)
            for tc in m.get("toolCalls") or []:
                b.add(key, "tool", describe_tool(tc.get("name"), tc.get("args")), ts, path)


def _opencode(con, b):
    db = HOME / ".local/share/opencode/opencode.db"
    if not db.exists():
        return
    path = str(db)
    row = con.execute("SELECT off FROM files WHERE path=?", (path,)).fetchone()
    since = row[0] if row else 0
    try:
        oc = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        sess = {r[0]: (r[1], r[2]) for r in oc.execute("SELECT id, directory, title FROM session")}
        rows = oc.execute("SELECT p.session_id, p.time_created, p.time_updated, p.data, m.data FROM part p "
                          "JOIN message m ON m.id = p.message_id WHERE p.time_updated > ? ORDER BY p.time_updated",
                          (since,)).fetchall()
        oc.close()
    except sqlite3.Error:
        return
    newest = since
    for sid, created, updated, pdata, mdata in rows:
        newest = max(newest, updated or 0)
        try:
            p, m = json.loads(pdata), json.loads(mdata)
        except ValueError:
            continue
        cwd, title = sess.get(sid, (None, None))
        ts = parse_ts(created)
        key = b.session("opencode", sid, path, cwd, title, ts)
        if p.get("type") == "text":
            b.add(key, "user" if m.get("role") == "user" else "agent", p.get("text"), ts, path)
        elif p.get("type") == "tool":
            b.add(key, "tool", describe_tool(p.get("tool"), (p.get("state") or {}).get("input")), ts, path)
    con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?)", (path, "db", newest))


def refresh():
    con = _db()
    b = _Batch(con)
    try:
        for root in (HOME / ".claude/projects", HOME / ".config/claude/projects"):
            if root.is_dir():
                for p in root.rglob("*.jsonl"):
                    _jsonl(con, b, str(p), _claude_line)
        for root in (HOME / ".codex/sessions", HOME / ".codex/archived_sessions"):
            if root.is_dir():
                for p in root.rglob("*.jsonl"):
                    _jsonl(con, b, str(p), _codex_line)
        b.flush()
        cwds = {}
        for (cwd,) in con.execute("SELECT DISTINCT cwd FROM sessions WHERE cwd IS NOT NULL"):
            cwds[hashlib.sha256(cwd.encode()).hexdigest()] = cwd
        for agent, root in (("gemini", HOME / ".gemini/tmp"), ("qwen", HOME / ".qwen/tmp")):
            if root.is_dir():
                for p in root.glob("*/chats/*.json"):
                    _gemini(con, b, agent, str(p), cwds)
        _opencode(con, b)
        b.flush()
        con.commit()
    finally:
        con.close()


def _fts(q):
    parts = re.findall(r'"([^"]+)"|(\S+)', q)
    out = []
    for phrase, word in parts:
        if phrase:
            out.append('"' + phrase.replace('"', "") + '"')
        else:
            for w in re.findall(r"\w+", word):
                out.append(f'"{w}"*')
    return " ".join(out)


def resume_cmd(agent, sid, cwd):
    pre = f"cd {cwd} && " if cwd else ""
    if agent == "claude":
        return pre + f"claude --resume {sid}"
    if agent == "codex":
        return pre + f"codex resume {sid}"
    return None


def find(q, agent=None, cwd=None, since=None, limit=20, fresh=True, turns=False):
    if fresh:
        refresh()
    fq = _fts(q)
    if not fq:
        return []
    con = _db()
    try:
        sql = ("SELECT d.skey, d.role, d.ts, snippet(docs, 0, ?, ?, ' … ', 18), bm25(docs) FROM docs d "
               "JOIN sessions s ON s.key = d.skey WHERE docs MATCH ?")
        args = [HIT_A, HIT_B, fq]
        if agent:
            sql += " AND s.agent = ?"
            args.append(agent)
        if cwd:
            cwd = os.path.realpath(os.path.expanduser(cwd))
            sql += " AND (s.cwd = ? OR s.cwd LIKE ?)"
            args += [cwd, cwd.rstrip("/") + "/%"]
        if since:
            sql += " AND CAST(d.ts AS REAL) >= ?"
            args.append(since)
        sql += " ORDER BY bm25(docs) LIMIT 600"
        try:
            rows = con.execute(sql, args).fetchall()
        except sqlite3.OperationalError as e:
            raise ValueError(f"can't search for that: {e}")
        groups = {}
        for skey, role, ts, snip, rank in rows:
            g = groups.setdefault(skey, {"rank": 0.0, "hits": []})
            g["rank"] += rank
            if len(g["hits"]) < 3:
                g["hits"].append({"role": role, "at": float(ts or 0), "snippet": snip})
        if not groups:
            return []
        meta = {r[0]: r for r in con.execute(
            f"SELECT key, agent, sid, cwd, title, started, last, n FROM sessions WHERE key IN ({','.join('?' * len(groups))})",
            list(groups))}
    finally:
        con.close()
    now = time.time()
    out = []
    for skey, g in groups.items():
        m = meta.get(skey)
        if not m:
            continue
        _, ag, sid, scwd, title, started, last, n = m
        age_days = max(0.0, (now - (last or now)) / 86400)
        score = g["rank"] / (1 + age_days / 14)
        g["hits"].sort(key=lambda h: h["at"])
        out.append({"agent": ag, "session": sid, "cwd": scwd, "title": title, "started": started, "last": last,
                    "messages": n, "hits": g["hits"], "resume": resume_cmd(ag, sid, scwd), "score": score})
    out.sort(key=lambda r: r["score"])
    out = out[:limit]
    if turns:
        _attach_turns(out)
    return out


def _attach_turns(results):
    from . import checkpoints
    by_cwd = {}
    for r in results:
        r["turns"] = []
        if r["agent"] != "claude" or not r.get("cwd") or not os.path.isdir(r["cwd"]):
            continue
        if r["cwd"] not in by_cwd:
            try:
                by_cwd[r["cwd"]] = checkpoints.listing(r["cwd"], 200)["turns"]
            except Exception:
                by_cwd[r["cwd"]] = []
        pre = f"claude|{r['session']}|"
        r["turns"] = [{k: t[k] for k in ("id", "prompt", "at", "added", "removed", "undone", "files")}
                      for t in by_cwd[r["cwd"]] if (t.get("turn") or "").startswith(pre)][:6]


def stats():
    con = _db()
    try:
        by = dict(con.execute("SELECT agent, COUNT(*) FROM sessions GROUP BY agent").fetchall())
        docs = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    finally:
        con.close()
    return {"sessions": by, "messages": docs}
