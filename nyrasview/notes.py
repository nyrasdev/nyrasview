import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from . import plat
from .util import CONFIG, HOME, STATE, config

START = "<!-- nyrasview:shared-notes start (edit with: nyrasview notes edit; changes here sync to every agent) -->"
END = "<!-- nyrasview:shared-notes end -->"
BLOCK = re.compile(r"<!-- nyrasview:shared-notes start[^>]*-->\n?(.*?)\n?<!-- nyrasview:shared-notes end -->", re.S)
PROJECT = {"claude": "CLAUDE.md", "codex": "AGENTS.md", "gemini": "GEMINI.md", "qwen": "QWEN.md"}
GLOBAL = {"claude": ".claude/CLAUDE.md", "codex": ".codex/AGENTS.md", "gemini": ".gemini/GEMINI.md", "qwen": ".qwen/QWEN.md"}
STATEF = STATE / "notes_sync.json"
TEMPLATE = """# Shared notes

Everything here goes to every coding agent (Claude Code, Codex, Gemini, Qwen, and others that read AGENTS.md).

-
"""


def settings():
    c = config().get("notes") or {}
    return {"auto": c.get("auto", True) is not False, "agents": c.get("agents") or list(PROJECT)}


def _h(text):
    return hashlib.sha1((text or "").strip().encode()).hexdigest()


def _load_state():
    try:
        return json.loads(STATEF.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(d):
    STATEF.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATEF.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    plat.replace(tmp, STATEF)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".nyra-tmp")
    tmp.write_text(text, encoding="utf-8")
    plat.replace(tmp, path)


def _root(cwd):
    from .checkpoints import root_of
    r = root_of(cwd)
    return r or os.path.realpath(cwd)


def scope(cwd=None, glob=False):
    if glob:
        return {"key": "global", "source": CONFIG.parent / "notes.md", "base": HOME, "files": GLOBAL, "label": "every project"}
    root = _root(os.path.expanduser(cwd or os.getcwd()))
    if os.path.realpath(root) == os.path.realpath(HOME):
        raise ValueError("that's your home folder; pick a project folder, or use the global notes (-g) for every project")
    return {"key": root, "source": Path(root) / ".nyrasview" / "notes.md", "base": Path(root), "files": PROJECT, "label": root}


def _block_of(text):
    m = BLOCK.search(text or "")
    return m.group(1).strip("\n") if m else None


def _with_block(text, body):
    block = f"{START}\n{body.strip()}\n{END}"
    if text and BLOCK.search(text):
        return BLOCK.sub(lambda _: block, text, count=1)
    if not (text or "").strip():
        return block + "\n"
    return block + "\n\n" + text.lstrip("\n")


def _targets(sc):
    from .registry import BY_KEY
    want = settings()["agents"]
    out, seen = [], set()
    for agent, rel in sc["files"].items():
        p = sc["base"] / rel
        if p in seen:
            continue
        a = BY_KEY.get(agent)
        try:
            installed = bool(a and a.installed())
        except Exception:
            installed = False
        use = p.exists() or (agent in want and installed and (sc["key"] != "global" or p.parent.is_dir()))
        if use:
            seen.add(p)
            out.append((agent, p))
    return out


def status(cwd=None, glob=False):
    sc = scope(cwd, glob)
    src = sc["source"]
    body = src.read_text(encoding="utf-8") if src.exists() else None
    st = _load_state().get(sc["key"], {})
    files = []
    for agent, p in _targets(sc):
        text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        blk = _block_of(text)
        state = "missing" if not p.exists() else "no block" if blk is None else \
            "in sync" if body is not None and _h(blk) == _h(body) else "changed here" if st.get("synced") and _h(blk) != st.get("synced") else "out of date"
        files.append({"agent": agent, "path": str(p), "state": state})
    return {"scope": sc["key"], "label": sc["label"], "source": str(src), "exists": body is not None, "text": body or "",
            "files": files, "synced_at": st.get("at")}


def sync(cwd=None, glob=False):
    sc = scope(cwd, glob)
    src = sc["source"]
    if not src.exists():
        return {"changed": [], "pulled": None, "conflicts": [], "skipped": "no shared notes here yet"}
    body = src.read_text(encoding="utf-8").strip("\n")
    allst = _load_state()
    st = allst.get(sc["key"], {})
    last = st.get("synced")
    pulled, conflicts = None, []
    targets = _targets(sc)
    if last and _h(body) == last:
        for agent, p in targets:
            blk = _block_of(p.read_text(encoding="utf-8", errors="replace")) if p.exists() else None
            if blk is not None and _h(blk) != last:
                body = blk.strip("\n")
                _write(src, body + "\n")
                pulled = {"agent": agent, "path": str(p)}
                break
    changed = []
    for agent, p in targets:
        text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        blk = _block_of(text)
        if blk is not None and _h(blk) == _h(body):
            continue
        if blk is not None and last and _h(blk) not in (last, _h(body)) and not (pulled and pulled["path"] == str(p)):
            bdir = STATE / "notes_backups"
            bdir.mkdir(parents=True, exist_ok=True)
            bf = bdir / f"{agent}-{time.strftime('%Y%m%d-%H%M%S')}-{hashlib.sha1(str(p).encode()).hexdigest()[:6]}.md"
            bf.write_text(blk, encoding="utf-8")
            conflicts.append({"agent": agent, "path": str(p), "backup": str(bf)})
        _write(p, _with_block(text, body))
        changed.append({"agent": agent, "path": str(p)})
    st = {"synced": _h(body), "at": time.time()}
    allst[sc["key"]] = st
    _save_state(allst)
    return {"changed": changed, "pulled": pulled, "conflicts": conflicts}


def save(text, cwd=None, glob=False):
    sc = scope(cwd, glob)
    _write(sc["source"], text.strip("\n") + "\n")
    return sync(cwd, glob)


def add(line, cwd=None, glob=False):
    sc = scope(cwd, glob)
    cur = sc["source"].read_text(encoding="utf-8") if sc["source"].exists() else TEMPLATE.replace("- \n", "")
    cur = cur.rstrip("\n") + "\n" + ("" if line.lstrip().startswith(("-", "*", "#")) else "- ") + line.strip() + "\n"
    return save(cur, cwd, glob)


def init(cwd=None, glob=False, from_file=None):
    sc = scope(cwd, glob)
    if sc["source"].exists():
        raise ValueError(f"shared notes already exist: {sc['source']}")
    text = TEMPLATE
    if from_file:
        f = Path(from_file).expanduser()
        if not f.is_absolute():
            f = sc["base"] / f
        if not f.exists():
            raise ValueError(f"{f} doesn't exist")
        raw = f.read_text(encoding="utf-8")
        own = BLOCK.sub("", raw).strip()
        if not own:
            raise ValueError(f"{f} has nothing to import")
        bdir = STATE / "notes_backups"
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, bdir / f"import-{f.name}-{int(time.time())}")
        text = own + "\n"
        _write(f, f"{START}\n{own}\n{END}\n")
    return save(text, cwd, glob)


def auto(cwds):
    if not settings()["auto"]:
        return
    for key in ["global", *sorted(set(cwds or []))]:
        try:
            if key == "global":
                if scope(glob=True)["source"].exists():
                    sync(glob=True)
            elif os.path.isdir(key) and os.path.realpath(key) != os.path.realpath(HOME):
                if scope(key)["source"].exists():
                    sync(key)
        except Exception:
            continue
