import os
import re
import time
from pathlib import Path
from urllib.parse import quote

from . import collab
from .registry import BY_KEY
from .util import HOME, load_json

CONFIGS = [
    HOME / ".config/obsidian/obsidian.json",
    HOME / ".var/app/md.obsidian.Obsidian/config/obsidian/obsidian.json",
    HOME / "snap/obsidian/current/.config/obsidian/obsidian.json",
    HOME / "Library/Application Support/obsidian/obsidian.json",
    Path(os.environ.get("APPDATA") or HOME / "AppData/Roaming") / "obsidian/obsidian.json",
]
CALLOUT = re.compile(r"^>\s*\[!ai\][-+]?\s*(\S+)(?:\s+(\S+))?\s*(.*)$", re.I)
SKIP = {".obsidian", ".trash", ".git", "node_modules"}


def vaults():
    out, seen = [], set()
    for c in CONFIGS:
        d = load_json(c) or {}
        for vid, v in (d.get("vaults") or {}).items():
            p = v.get("path")
            if p and p not in seen and Path(p).is_dir():
                seen.add(p)
                out.append({"id": vid, "name": Path(p).name, "path": p, "open": bool(v.get("open")),
                            "ts": v.get("ts", 0)})
    return sorted(out, key=lambda v: -v["ts"])


def vault_of(path):
    rp = os.path.realpath(path)
    for v in vaults():
        root = os.path.realpath(v["path"])
        if rp == root or rp.startswith(root + os.sep):
            return v
    return None


def resolve(spec):
    if ":" in spec and not spec.startswith(("/", "~", ".")) and not re.match(r"^[A-Za-z]:\\", spec):
        name, rel = spec.split(":", 1)
        for v in vaults():
            if v["name"] == name or v["id"] == name:
                p = Path(v["path"]) / rel
                return str(p if p.suffix else p.with_suffix(".md"))
        raise FileNotFoundError(f"no Obsidian vault named {name}")
    return os.path.expanduser(spec)


def uri(path):
    return "obsidian://open?path=" + quote(os.path.realpath(path))


def callouts(text):
    lines = text.split("\n")
    found, section, i, fence = [], "", 0, False
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not fence:
            h = collab.HEAD_RE.match(line)
            if h:
                section = h.group(2)
            m = CALLOUT.match(line)
            if m:
                agent = m.group(1).lower()
                model = m.group(2) if m.group(2) and ("/" in m.group(2) or ":" in m.group(2)) else None
                first = m.group(3) if model else " ".join(x for x in (m.group(2), m.group(3)) if x)
                body = [first.strip()] if first.strip() else []
                j = i + 1
                while j < len(lines) and lines[j].startswith(">"):
                    body.append(lines[j][1:].strip())
                    j += 1
                found.append({"agent": agent, "model": model, "instruction": "\n".join(body).strip(),
                              "section": section, "start": i, "end": j})
                i = j
                continue
        i += 1
    return found


def sync(doc):
    path = Path(doc)
    text = path.read_text()
    items = [c for c in callouts(text) if c["agent"] in BY_KEY and c["instruction"]]
    if not items:
        return []
    lines = text.split("\n")
    for c in reversed(items):
        del lines[c["start"]:c["end"]]
    path.write_text("\n".join(lines))
    return [collab.assign(doc, c["section"], c["agent"], c["instruction"], c["model"]) for c in items]


def set_authors(doc, agents):
    path = Path(doc)
    text = path.read_text()
    names = sorted(set(agents))
    if not names:
        return
    val = "ai-authors: [" + ", ".join(names) + "]"
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end > 0:
            fm = text[4:end]
            rest = text[end:]
            if re.search(r"^ai-authors:.*$", fm, re.M):
                fm = re.sub(r"^ai-authors:.*$", val, fm, flags=re.M)
            else:
                fm = fm.rstrip("\n") + "\n" + val
            path.write_text("---\n" + fm + rest)
            return
    path.write_text(f"---\n{val}\n---\n\n" + text)


def notes(root):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP]
        for f in fns:
            if f.endswith(".md"):
                yield os.path.join(dp, f)


def watch(roots, interval=3.0, on_event=None, parallel=4):
    seen = {}
    while True:
        for root in roots:
            for n in notes(root):
                try:
                    m = os.stat(n).st_mtime_ns
                except OSError:
                    continue
                if seen.get(n) == m:
                    continue
                seen[n] = m
                try:
                    with open(n, errors="replace") as f:
                        if "[!ai]" not in f.read().lower():
                            continue
                except OSError:
                    continue
                tasks = sync(n)
                if tasks:
                    if on_event:
                        on_event("queued", {"doc": n, "tasks": tasks})
                    collab.run(n, only=[t["id"] for t in tasks], parallel=parallel, on_event=on_event)
                    try:
                        seen[n] = os.stat(n).st_mtime_ns
                    except OSError:
                        pass
        time.sleep(interval)
