import json
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from .registry import BY_KEY
from .util import STATE

DIR = STATE / "runs"
MODES = ("ask", "edit", "auto")

STREAM = {
    "claude": {"cmd": ["claude", "-p", "--output-format", "stream-json", "--verbose"], "stdin": True,
               "model": "--model", "resume": "--resume",
               "mode": {"ask": ["--permission-mode", "plan"], "edit": ["--permission-mode", "acceptEdits"],
                        "auto": ["--dangerously-skip-permissions"]}},
    "codex": {"cmd": ["codex", "exec", "--json", "--skip-git-repo-check"], "stdin": True, "stdin_arg": "-",
              "model": "-m",
              "mode": {"ask": ["--sandbox", "read-only"], "edit": ["--full-auto"],
                       "auto": ["--dangerously-bypass-approvals-and-sandbox"]}},
    "gemini": {"cmd": ["gemini", "--output-format", "stream-json"], "prompt_flag": "-p", "model": "-m",
               "mode": {"ask": [], "edit": ["--approval-mode", "auto_edit"], "auto": ["--yolo"]}},
    "qwen": {"cmd": ["qwen", "--output-format", "stream-json"], "prompt_flag": "-p", "model": "-m",
             "mode": {"ask": [], "edit": ["--approval-mode", "auto_edit"], "auto": ["--yolo"]}},
}


def _meta_path(rid):
    return DIR / rid / "meta.json"


def _save(meta):
    d = DIR / meta["id"]
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "meta.tmp"
    tmp.write_text(json.dumps(meta, indent=1))
    os.replace(tmp, d / "meta.json")


def load(rid):
    try:
        return json.loads(_meta_path(rid).read_text())
    except (OSError, ValueError):
        return None


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1][0] != "Z"
    except OSError:
        return True


def _argv(agent, prompt, mode, model, session, cwd):
    spec = STREAM.get(agent)
    a = BY_KEY.get(agent)
    if spec:
        argv = list(spec["cmd"]) + list(spec["mode"].get(mode, []))
        if model and spec.get("model"):
            argv += [spec["model"], model]
        if session and spec.get("resume"):
            argv += [spec["resume"], session]
        if spec.get("prompt_flag"):
            argv += [spec["prompt_flag"], prompt]
            return argv, None, "stream"
        if spec.get("stdin_arg"):
            argv.append(spec["stdin_arg"])
        return argv, prompt, "stream"
    if a and a.headless and a.headless.get("cmd"):
        h = a.headless
        argv = [x.replace("{prompt}", prompt) for x in h["cmd"] if "{out}" not in x and x != "-o"]
        if model and h.get("model"):
            at = 2 if len(argv) > 1 and not argv[1].startswith("-") else 1
            argv[at:at] = [h["model"], model]
        return argv, (prompt if h.get("stdin") else None), "text"
    return None, None, "http"


def _context(prev):
    if not prev:
        return ""
    lines = ["Earlier in this conversation:"]
    for t in prev[-3:]:
        lines.append(f"User: {t['prompt']}")
        if t.get("answer"):
            lines.append(f"You: {t['answer'][:4000]}")
    return "\n".join(lines) + "\n\nNow: "


def start(agent, prompt, cwd, mode="ask", model=None, group=None, parent=None):
    if mode not in MODES:
        raise ValueError("mode must be ask, edit or auto")
    cwd = os.path.realpath(os.path.expanduser(cwd or "~"))
    if not os.path.isdir(cwd):
        raise FileNotFoundError(cwd)
    a = BY_KEY.get(agent)
    if a is None:
        raise ValueError(f"unknown agent {agent}")
    prev = []
    session = None
    if parent:
        pm = load(parent)
        if pm:
            prev = pm.get("turns", [])
            session = pm.get("session")
    rid = secrets.token_hex(4)
    meta = {"id": rid, "agent": agent, "cwd": cwd, "mode": mode, "model": model, "group": group,
            "prompt": prompt, "created": time.time(), "status": "running", "pid": None, "session": None,
            "parent": parent, "turns": prev + [{"prompt": prompt}]}
    d = DIR / rid
    d.mkdir(parents=True, exist_ok=True)
    full = prompt
    native_resume = bool(session and STREAM.get(agent, {}).get("resume"))
    if prev and not native_resume:
        full = _context(prev) + prompt
    argv, stdin, kind = _argv(agent, full, mode, model, session if native_resume else None, cwd)
    meta["kind"] = kind
    if kind == "http":
        _save(meta)
        threading.Thread(target=_http, args=(meta, full), daemon=True).start()
        return meta
    if not shutil.which(argv[0]):
        meta.update(status="failed", error=f"{argv[0]} is not installed", finished=time.time())
        _save(meta)
        return meta
    meta["argv"] = [x if len(x) < 200 else x[:200] + "…" for x in argv]
    out = open(d / "out.log", "wb")
    err = open(d / "err.log", "wb")
    if stdin is not None:
        (d / "prompt.txt").write_text(stdin)
        inp = open(d / "prompt.txt", "rb")
    else:
        inp = subprocess.DEVNULL
    env = dict(os.environ, NO_COLOR="1", NYRA_RUN=rid)
    proc = subprocess.Popen(argv, cwd=cwd, stdin=inp, stdout=out, stderr=err, start_new_session=True, env=env)
    meta["pid"] = proc.pid
    _save(meta)
    threading.Thread(target=_reap, args=(rid, proc), daemon=True).start()
    return meta


def _reap(rid, proc):
    code = proc.wait()
    m = load(rid)
    if m and m["status"] == "running":
        m["exit"] = code
        _finish(m)


def _http(meta, prompt):
    from . import collab
    t0 = time.time()
    try:
        text = collab.call(meta["agent"], prompt, meta.get("model"), cwd=meta["cwd"])
        (DIR / meta["id"] / "out.log").write_text(text)
        meta.update(status="done", finished=time.time(), dur=time.time() - t0)
    except Exception as e:
        meta.update(status="failed", error=str(e)[:500], finished=time.time())
    _finish(meta, final=True)


def _finish(m, final=False):
    ev = events(m)
    done = next((e for e in reversed(ev) if e["k"] == "done"), None)
    if not final:
        if done:
            m["status"] = "done" if done.get("ok", True) else "failed"
            if not done.get("ok", True) and done.get("text"):
                m["error"] = done["text"][:500]
        elif m.get("exit") not in (None, 0):
            m["status"] = "failed"
            m["error"] = _err_tail(m) or f"exit {m.get('exit')}"
        elif m["status"] == "running":
            m["status"] = "done"
        m["finished"] = m.get("finished") or time.time()
    answer = "".join(e["text"] for e in ev if e["k"] == "text").strip()
    if m.get("turns"):
        m["turns"][-1]["answer"] = answer[:20000]
    for e in ev:
        if e["k"] == "meta" and e.get("session"):
            m["session"] = e["session"]
    if done:
        for k in ("cost", "tokens", "dur"):
            if done.get(k) is not None:
                m[k] = done[k]
    _save(m)


def _err_tail(m):
    try:
        t = (DIR / m["id"] / "err.log").read_text(errors="replace").strip()
    except OSError:
        return ""
    return t[-500:]


def _tool_detail(name, inp):
    from .activity import describe_tool
    return describe_tool(name, inp)[len(name):].strip() if isinstance(inp, dict) else str(inp)[:200]


def _parse_claude(o, out):
    t = o.get("type")
    if t == "system" and o.get("subtype") == "init":
        out.append({"k": "meta", "session": o.get("session_id"), "model": o.get("model")})
    elif t == "assistant":
        for b in (o.get("message") or {}).get("content") or []:
            if b.get("type") == "text" and b.get("text"):
                out.append({"k": "text", "text": b["text"]})
            elif b.get("type") == "tool_use":
                out.append({"k": "tool", "id": b.get("id"), "name": b.get("name"),
                            "detail": _tool_detail(b.get("name"), b.get("input"))})
    elif t == "user":
        for b in (o.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                out.append({"k": "result", "id": b.get("tool_use_id"), "ok": not b.get("is_error"),
                            "out": str(c or "")[:600]})
    elif t == "result":
        u = o.get("usage") or {}
        tok = sum(u.get(k) or 0 for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                             "cache_read_input_tokens"))
        out.append({"k": "done", "ok": not o.get("is_error"), "cost": o.get("total_cost_usd"),
                    "dur": (o.get("duration_ms") or 0) / 1000, "tokens": tok,
                    "text": o.get("result") if o.get("is_error") else None})


def _parse_codex(o, out):
    t = o.get("type") or (o.get("msg") or {}).get("type")
    if t == "thread.started":
        out.append({"k": "meta", "session": o.get("thread_id")})
    elif t in ("item.started", "item.completed", "item.updated"):
        it = o.get("item") or {}
        ty = it.get("type") or it.get("item_type")
        if ty in ("agent_message", "assistant_message") and t == "item.completed":
            out.append({"k": "text", "text": it.get("text") or ""})
        elif ty == "command_execution":
            if t == "item.started":
                out.append({"k": "tool", "id": it.get("id"), "name": "shell", "detail": it.get("command") or ""})
            elif t == "item.completed":
                out.append({"k": "result", "id": it.get("id"), "ok": (it.get("exit_code") or 0) == 0,
                            "out": (it.get("aggregated_output") or "")[:600]})
        elif ty == "file_change" and t == "item.completed":
            ch = it.get("changes") or []
            out.append({"k": "tool", "id": it.get("id"), "name": "edit",
                        "detail": ", ".join(f"{c.get('kind', '')} {c.get('path', '')}".strip() for c in ch)[:300]})
            out.append({"k": "result", "id": it.get("id"), "ok": it.get("status") != "failed", "out": ""})
        elif ty == "mcp_tool_call" and t == "item.completed":
            out.append({"k": "tool", "id": it.get("id"), "name": f"{it.get('server', 'mcp')}.{it.get('tool', '')}", "detail": ""})
            out.append({"k": "result", "id": it.get("id"), "ok": it.get("status") != "failed", "out": ""})
    elif t == "turn.completed":
        u = o.get("usage") or {}
        out.append({"k": "done", "ok": True, "tokens": sum(v for v in u.values() if isinstance(v, int))})
    elif t in ("turn.failed", "error"):
        msg = (o.get("error") or {}).get("message") if isinstance(o.get("error"), dict) else o.get("message")
        out.append({"k": "done", "ok": False, "text": msg or "failed"})


def _parse_gemini(o, out):
    t = o.get("type")
    if t == "init":
        out.append({"k": "meta", "session": o.get("session_id"), "model": o.get("model")})
    elif t == "message" and o.get("role") in ("assistant", "model"):
        out.append({"k": "text", "text": o.get("content") or "", "delta": bool(o.get("delta"))})
    elif t == "tool_use":
        out.append({"k": "tool", "id": o.get("tool_id"), "name": o.get("tool_name"),
                    "detail": _tool_detail(o.get("tool_name") or "", o.get("parameters") or {})})
    elif t == "tool_result":
        out.append({"k": "result", "id": o.get("tool_id"), "ok": o.get("status") != "error",
                    "out": str(o.get("output") or "")[:600]})
    elif t == "result":
        st = o.get("stats") or {}
        out.append({"k": "done", "ok": o.get("status") != "error", "tokens": st.get("total_tokens"),
                    "dur": (st.get("duration_ms") or 0) / 1000 or None})


PARSERS = {"claude": _parse_claude, "codex": _parse_codex, "gemini": _parse_gemini, "qwen": _parse_gemini}


def events(m):
    try:
        raw = (DIR / m["id"] / "out.log").read_bytes().decode("utf-8", "replace")
    except OSError:
        raw = ""
    out = []
    if m.get("kind") != "stream":
        if raw.strip():
            out.append({"k": "text", "text": raw})
        return out
    parse = PARSERS.get(m["agent"])
    plain = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except ValueError:
            plain.append(line)
            continue
        if isinstance(o, dict) and parse:
            parse(o, out)
    merged = []
    for e in out:
        if e["k"] == "text" and e.get("delta") and merged and merged[-1]["k"] == "text":
            merged[-1]["text"] += e["text"]
        else:
            merged.append(e)
    if plain and not any(e["k"] == "text" for e in merged):
        merged.append({"k": "text", "text": "\n".join(plain)})
    return merged


def view(rid):
    m = load(rid)
    if not m:
        return None
    if m["status"] == "running" and m.get("pid") and not _alive(m["pid"]):
        _finish(m)
        m = load(rid)
    ev = events(m)
    return {**{k: v for k, v in m.items() if k not in ("turns",)}, "turns": m.get("turns", []), "events": ev,
            "elapsed": (m.get("finished") or time.time()) - m["created"]}


def listing(limit=40):
    if not DIR.is_dir():
        return []
    out = []
    for d in sorted(DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:limit * 2]:
        m = load(d.name)
        if not m:
            continue
        if m["status"] == "running" and m.get("pid") and not _alive(m["pid"]):
            _finish(m)
            m = load(d.name)
        out.append({k: m.get(k) for k in ("id", "agent", "cwd", "mode", "model", "group", "prompt", "created",
                                           "status", "finished", "pid", "cost", "error", "parent")})
    children = {r["parent"] for r in out if r.get("parent")}
    return [r for r in out if r["id"] not in children][:limit]


def stop(rid):
    m = load(rid)
    if not m or not m.get("pid"):
        return False
    try:
        os.killpg(m["pid"], signal.SIGTERM)
    except OSError:
        try:
            os.kill(m["pid"], signal.SIGTERM)
        except OSError:
            pass
    m.update(status="stopped", finished=time.time())
    _save(m)
    return True


def active_pids():
    out = {}
    if not DIR.is_dir():
        return out
    cutoff = time.time() - 86400
    for d in DIR.iterdir():
        try:
            if d.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        m = load(d.name)
        if m and m["status"] == "running" and m.get("pid"):
            out[m["pid"]] = m
    return out
