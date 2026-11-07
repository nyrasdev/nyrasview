import json
import os
import re
import urllib.request

import psutil

from .activity import _tail
from .handoff import claude_session, codex_session
from .util import HOME, parse_ts

MAX_FILE = 2_000_000
WEB_EXT = {".html", ".htm", ".css", ".scss", ".sass", ".less", ".jsx", ".tsx", ".vue", ".svelte", ".astro",
           ".js", ".ts", ".mdx", ".php", ".erb", ".hbs", ".njk", ".liquid"}


def _edits_claude(path):
    out = []
    for o in _tail(path, 600000):
        if o.get("type") != "assistant":
            continue
        ts = parse_ts(o.get("timestamp"))
        for b in (o.get("message") or {}).get("content") or []:
            if b.get("type") != "tool_use":
                continue
            n, i = b.get("name"), b.get("input") or {}
            f = i.get("file_path") or i.get("notebook_path")
            if n == "Edit" and f:
                out.append({"file": f, "tool": n, "old": i.get("old_string", ""), "new": i.get("new_string", ""), "at": ts})
            elif n == "MultiEdit" and f:
                for e in i.get("edits") or []:
                    out.append({"file": f, "tool": n, "old": e.get("old_string", ""), "new": e.get("new_string", ""), "at": ts})
            elif n == "Write" and f:
                out.append({"file": f, "tool": n, "old": "", "new": i.get("content", ""), "at": ts})
            elif n == "NotebookEdit" and f:
                out.append({"file": f, "tool": n, "old": "", "new": i.get("new_source", ""), "at": ts})
    return out


PATCH = re.compile(r"^\*\*\* (Add|Update) File: (.+)$")


def _edits_codex(path, cwd):
    out = []
    for o in _tail(path, 600000):
        p = o.get("payload") or {}
        if p.get("type") not in ("function_call", "custom_tool_call"):
            continue
        raw = p.get("input") or p.get("arguments") or ""
        if not isinstance(raw, str):
            raw = json.dumps(raw)
        if "*** Begin Patch" not in raw:
            try:
                d = json.loads(raw)
                raw = d.get("input") or " ".join(d.get("command") or []) if isinstance(d, dict) else raw
            except ValueError:
                pass
        if "*** Begin Patch" not in str(raw):
            continue
        ts = parse_ts(o.get("timestamp"))
        cur, old, new = None, [], []
        for line in str(raw).replace("\\n", "\n").split("\n"):
            m = PATCH.match(line)
            if m or line.startswith("*** End Patch") or line.startswith("@@"):
                if cur and (new or old):
                    out.append({"file": cur, "tool": "apply_patch", "old": "\n".join(old), "new": "\n".join(new), "at": ts})
                old, new = [], []
                if m:
                    f = m.group(2).strip()
                    cur = f if os.path.isabs(f) else os.path.join(cwd, f)
                continue
            if line.startswith("+"):
                new.append(line[1:])
            elif line.startswith("-"):
                old.append(line[1:])
            elif line.startswith(" "):
                if new or old:
                    new.append(line[1:])
                    old.append(line[1:])
    return out


def edits(agent, cwd, limit=25):
    if agent == "claude":
        p = claude_session(cwd)
        e = _edits_claude(p) if p else []
    elif agent == "codex":
        p = codex_session(cwd)
        e = _edits_codex(p, cwd) if p else []
    else:
        e = []
    return e[-limit:]


def locate(path, snippet):
    try:
        if os.path.getsize(path) > MAX_FILE:
            return None, None
        with open(path, errors="replace") as f:
            text = f.read()
    except OSError:
        return None, None
    lines = text.split("\n")
    snip = (snippet or "").strip("\n")
    if not snip.strip():
        return lines, None
    idx = text.find(snip)
    if idx < 0:
        first = next((l.strip() for l in snip.split("\n") if len(l.strip()) > 6), None)
        idx = text.find(first) if first else -1
        if idx < 0:
            return lines, None
        start = text.count("\n", 0, idx)
        return lines, (start, start)
    start = text.count("\n", 0, idx)
    return lines, (start, start + snip.count("\n"))


def hints(edit, lines, rng):
    new = edit.get("new") or ""
    ext = os.path.splitext(edit["file"])[1].lower()
    sel, texts = [], []
    for m in re.finditer(r"""\bid\s*=\s*["'{`]+([\w-]+)""", new):
        sel.append("#" + m.group(1))
    for m in re.finditer(r"""\bclass(?:Name)?\s*=\s*\{?\s*["'`]([^"'`]+)["'`]""", new):
        toks = [t for t in m.group(1).split() if re.match(r"^[\w-]+$", t)]
        if toks:
            sel.append("." + ".".join(toks[:3]))
            sel += ["." + t for t in toks[:4]]
    if ext in (".css", ".scss", ".sass", ".less"):
        for m in re.finditer(r"(^|\n)\s*([^{}\n@][^{}\n]*?)\s*\{", new):
            sel += [x.strip() for x in m.group(2).split(",") if x.strip()]
        if not sel and lines and rng:
            for i in range(rng[0], -1, -1):
                m = re.match(r"\s*([^{}@][^{}]*?)\s*\{", lines[i])
                if m:
                    sel += [x.strip() for x in m.group(1).split(",")]
                    break
    for m in re.finditer(r">\s*([^<>{}\n]{3,80}?)\s*<", new):
        t = m.group(1).strip()
        if re.search(r"[A-Za-z]{3}", t):
            texts.append(t)
    for m in re.finditer(r"""["'`]([A-Z][^"'`\n{}<>]{4,60})["'`]""", new):
        if " " in m.group(1):
            texts.append(m.group(1))
    tags = re.findall(r"<([a-z][\w-]*)", new)
    seen, sel2 = set(), []
    for s in sel:
        if s not in seen and len(s) < 120:
            seen.add(s)
            sel2.append(s)
    return {"selectors": sel2[:12], "texts": list(dict.fromkeys(texts))[:8], "tags": list(dict.fromkeys(tags))[:5]}


def ports(cwd, extra_pids=()):
    real = os.path.realpath(cwd)
    want = set(extra_pids)
    for p in psutil.process_iter(["pid", "cwd"]):
        c = p.info.get("cwd")
        if c and (c == real or c.startswith(real + os.sep)):
            want.add(p.info["pid"])
    found = set()
    try:
        for c in psutil.net_connections("inet"):
            if c.status == psutil.CONN_LISTEN and c.pid in want and c.laddr:
                found.add(c.laddr.port)
    except (psutil.AccessDenied, OSError):
        pass
    web = []
    for port in sorted(found):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=0.8) as r:
                if "html" in (r.headers.get("Content-Type") or ""):
                    web.append(port)
        except Exception:
            try:
                req = urllib.request.Request(f"http://localhost:{port}/", headers={"Accept": "text/html"})
                with urllib.request.urlopen(req, timeout=0.8) as r:
                    if "html" in (r.headers.get("Content-Type") or ""):
                        web.append(port)
            except Exception:
                pass
    return web


def view(agent, cwd, pick=None):
    es = edits(agent, cwd)
    out = {"agent": agent, "cwd": cwd, "edits": [], "focus": None}
    for i, e in enumerate(es):
        out["edits"].append({"i": i, "file": e["file"], "rel": os.path.relpath(e["file"], cwd) if e["file"].startswith(cwd) else e["file"].replace(str(HOME), "~"),
                             "tool": e["tool"], "at": e["at"], "added": e["new"].count("\n") + (1 if e["new"] else 0),
                             "removed": e["old"].count("\n") + (1 if e["old"] else 0)})
    if not es:
        return out
    idx = len(es) - 1 if pick is None else max(0, min(int(pick), len(es) - 1))
    e = es[idx]
    lines, rng = locate(e["file"], e["new"])
    if lines is None:
        out["focus"] = {"i": idx, "file": e["file"], "missing": True}
        return out
    if e["tool"] == "Write" and rng is None:
        rng = (0, min(len(lines) - 1, 40))
    lo = max(0, (rng[0] if rng else 0) - 30)
    hi = min(len(lines), (rng[1] if rng else 60) + 31)
    ext = os.path.splitext(e["file"])[1].lower()
    out["focus"] = {"i": idx, "file": e["file"], "rel": out["edits"][idx]["rel"], "tool": e["tool"], "at": e["at"],
                    "start": lo, "lines": lines[lo:hi], "range": list(rng) if rng else None, "total": len(lines),
                    "web": ext in WEB_EXT, "hints": hints(e, lines, rng), "old": e["old"][:4000]}
    return out
