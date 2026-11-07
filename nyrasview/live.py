import json
import os
import re
import subprocess
import time
import urllib.request

import psutil

from .activity import _tail
from .handoff import claude_session, codex_session
from .util import HOME, parse_ts

MAX_FILE = 2_000_000
WEB_EXT = {".html", ".htm", ".css", ".scss", ".sass", ".less", ".jsx", ".tsx", ".vue", ".svelte", ".astro",
           ".js", ".ts", ".mdx", ".php", ".erb", ".hbs", ".njk", ".liquid"}


_scan = {}
EDIT_MARK = (b'"name":"Edit"', b'"name":"Write"', b'"name":"MultiEdit"', b'"name":"NotebookEdit"')


def _claude_objs(path):
    st = os.stat(path)
    ent = _scan.get(path)
    if ent is None or st.st_size < ent["off"] or st.st_ino != ent["ino"]:
        ent = {"off": 0, "ino": st.st_ino, "objs": []}
        _scan[path] = ent
    if st.st_size > ent["off"]:
        with open(path, "rb") as f:
            f.seek(ent["off"])
            chunk = f.read()
        end = chunk.rfind(b"\n")
        if end >= 0:
            ent["off"] += end + 1
            for raw in chunk[:end].split(b"\n"):
                if b'"tool_use"' in raw and any(m in raw for m in EDIT_MARK):
                    try:
                        ent["objs"].append(json.loads(raw))
                    except ValueError:
                        pass
            ent["objs"] = ent["objs"][-400:]
    return ent["objs"]


def _edits_claude(path):
    out = []
    for o in _claude_objs(path):
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


def _git(root, *args):
    try:
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _toplevel(d):
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    return _git(d, "rev-parse", "--show-toplevel").strip() if d else ""


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def disk_edits(roots, since):
    out = []
    for root in roots:
        cur = None
        for line in _git(root, "diff", "-U0", "--no-color", "HEAD").split("\n"):
            if line.startswith("+++ "):
                f = line[4:].strip()
                cur = None if f == "/dev/null" else os.path.join(root, f[2:] if f.startswith("b/") else f)
                continue
            m = HUNK.match(line)
            if m and cur:
                start = int(m.group(1)) - 1
                n = int(m.group(2) if m.group(2) is not None else 1)
                if n == 0:
                    continue
                try:
                    at = os.stat(cur).st_mtime
                except OSError:
                    continue
                if at >= since:
                    out.append({"file": cur, "tool": "changed on disk", "old": "", "new": "", "at": at,
                                "range": (start, start + n - 1)})
        for f in _git(root, "ls-files", "--others", "--exclude-standard").split("\n")[:30]:
            if not f:
                continue
            full = os.path.join(root, f)
            try:
                at = os.stat(full).st_mtime
            except OSError:
                continue
            if at >= since:
                out.append({"file": full, "tool": "new file", "old": "", "new": "", "at": at, "range": None})
    return out


def edits(agent, cwd, limit=40):
    p = None
    if agent == "claude":
        p = claude_session(cwd)
        e = _edits_claude(p) if p else []
    elif agent == "codex":
        p = codex_session(cwd)
        e = _edits_codex(p, cwd) if p else []
    else:
        e = []
    since = time.time() - 86400
    roots = []
    if os.path.realpath(cwd) != str(HOME):
        t = _toplevel(cwd)
        if t:
            roots.append(t)
    for x in reversed(e[-30:]):
        t = _toplevel(os.path.dirname(x["file"]))
        if t and t not in roots and t != str(HOME):
            roots.append(t)
        if len(roots) >= 3:
            break
    last_log = {}
    for x in e:
        last_log[x["file"]] = max(last_log.get(x["file"], 0), x["at"])
    for d in disk_edits(roots, since):
        if d["at"] > last_log.get(d["file"], 0) + 2:
            e.append(d)
    e.sort(key=lambda x: x["at"])
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
    skip = {os.getpid()}
    browsers = ("firefox", "chrome", "chromium", "brave", "msedge", "opera", "vivaldi", "electron", "zen")
    for p in psutil.process_iter(["pid", "cwd", "cmdline", "name"]):
        nm = (p.info.get("name") or "").lower()
        if "nyrasview" in " ".join(p.info.get("cmdline") or []) or any(b in nm for b in browsers):
            skip.add(p.info["pid"])
            continue
        c = p.info.get("cwd")
        if real != str(HOME) and c and (c == real or c.startswith(real + os.sep)):
            want.add(p.info["pid"])
    want -= skip
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
                             "tool": e["tool"], "at": e["at"], "added": (e["range"][1] - e["range"][0] + 1) if e.get("range") else e["new"].count("\n") + (1 if e["new"] else 0),
                             "removed": e["old"].count("\n") + (1 if e["old"] else 0)})
    if not es:
        return out
    idx = len(es) - 1 if pick is None else max(0, min(int(pick), len(es) - 1))
    e = es[idx]
    if "range" in e:
        lines, _ = locate(e["file"], "")
        rng = e["range"]
        if lines is not None and rng is None:
            rng = (0, min(len(lines) - 1, 30))
        if lines is not None and rng:
            e = dict(e, new="\n".join(lines[rng[0]:rng[1] + 1]))
    else:
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
