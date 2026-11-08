import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time

from . import plat
from .util import HOME, STATE, config

REF = "refs/nyrasview/checkpoints"
OPEN = STATE / "checkpoints"
WHO = {"GIT_AUTHOR_NAME": "nyrasview", "GIT_AUTHOR_EMAIL": "nyrasview@localhost",
       "GIT_COMMITTER_NAME": "nyrasview", "GIT_COMMITTER_EMAIL": "nyrasview@localhost"}
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_cache = {}


def settings():
    c = config().get("checkpoints") or {}
    return {"enabled": c.get("enabled", True) is not False, "keep": max(20, int(c.get("keep", 300)))}


def _git(root, *args, env=None, data=None, timeout=20):
    try:
        return subprocess.run(["git", "-C", root, *args], capture_output=True, input=data, timeout=timeout,
                              env={**os.environ, **WHO, **(env or {})})
    except (OSError, subprocess.TimeoutExpired):
        return None


def _out(r):
    return r.stdout.decode("utf-8", "replace").strip() if r is not None and r.returncode == 0 else None


def root_of(cwd):
    if not cwd or not os.path.isdir(cwd) or os.path.realpath(cwd) == os.path.realpath(HOME):
        return None
    return _out(_git(cwd, "rev-parse", "--show-toplevel", timeout=5)) or None


def snapshot(root):
    idx = _out(_git(root, "rev-parse", "--git-path", "index", timeout=5))
    if not idx:
        return None
    idx = idx if os.path.isabs(idx) else os.path.join(root, idx)
    fd, tmp = tempfile.mkstemp(prefix="nyra-idx-")
    os.close(fd)
    try:
        if os.path.exists(idx):
            shutil.copyfile(idx, tmp)
        else:
            os.unlink(tmp)
        env = {"GIT_INDEX_FILE": tmp}
        r = _git(root, "add", "-A", "--", ".", env=env, timeout=8)
        if r is None or r.returncode != 0:
            return None
        return _out(_git(root, "write-tree", env=env, timeout=8))
    finally:
        for p in (tmp, tmp + ".lock"):
            try:
                os.unlink(p)
            except OSError:
                pass


def _head(root):
    return _out(_git(root, "rev-parse", "--verify", "-q", REF, timeout=5))


def _commit(root, before, after, meta):
    pair = f"040000 tree {before}\tbefore\n040000 tree {after}\tafter\n".encode()
    tree = _out(_git(root, "mktree", data=pair))
    if not tree:
        return None
    prev = _head(root)
    args = ["commit-tree", tree, "-m", json.dumps(meta)]
    if prev:
        args += ["-p", prev]
    sha = _out(_git(root, *args))
    if not sha:
        return None
    upd = ["update-ref", "-m", "nyrasview checkpoint", REF, sha] + ([prev] if prev else [])
    if _out(_git(root, *upd)) is None:
        return None
    _prune(root)
    return sha


def _prune(root):
    keep = settings()["keep"]
    n = _out(_git(root, "rev-list", "--count", REF, timeout=5))
    if not n or int(n) <= keep + 100:
        return
    shas = (_out(_git(root, "rev-list", f"-n{keep}", REF)) or "").split()[::-1]
    old = _head(root)
    prev = None
    for s in shas:
        tree = _out(_git(root, "rev-parse", s + "^{tree}"))
        msg = _out(_git(root, "log", "-1", "--format=%B", s))
        if not tree or msg is None:
            return
        args = ["commit-tree", tree, "-m", msg] + (["-p", prev] if prev else [])
        prev = _out(_git(root, *args))
        if not prev:
            return
    if prev:
        _git(root, "update-ref", REF, prev, old)


def _slot_file(slot):
    return OPEN / ("open-" + hashlib.sha1(slot.encode()).hexdigest()[:16] + ".json")


def _load_slot(slot):
    try:
        return json.loads(_slot_file(slot).read_text())
    except (OSError, ValueError):
        return None


def _save_slot(slot, d):
    OPEN.mkdir(parents=True, exist_ok=True)
    f = _slot_file(slot)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    plat.replace(tmp, f)


def _gc_slots():
    try:
        for p in OPEN.glob("open-*.json"):
            if time.time() - p.stat().st_mtime > 3 * 86400:
                p.unlink()
    except OSError:
        pass


def begin(slot, agent, cwd, prompt=""):
    if not settings()["enabled"]:
        return None
    root = root_of(cwd)
    if not root:
        return None
    tree = snapshot(root)
    if not tree:
        return None
    now = time.time()
    turn = f"{slot}|{now:.3f}|{os.urandom(3).hex()}"
    _save_slot(slot, {"root": root, "agent": agent, "turn": turn, "before": tree, "prompt": (prompt or "")[:300],
                      "at": now, "files": [], "bash": False, "last": tree})
    _gc_slots()
    return tree


def touch(slot, path=None, bash=False):
    d = _load_slot(slot)
    if not d:
        return
    if bash:
        d["bash"] = True
    if path:
        full = os.path.realpath(path if os.path.isabs(path) else os.path.join(d["root"], path))
        rel = os.path.relpath(full, os.path.realpath(d["root"]))
        if not rel.startswith("..") and rel not in d["files"]:
            d["files"].append(rel.replace(os.sep, "/"))
    _save_slot(slot, d)


def end(slot):
    d = _load_slot(slot)
    if not d or not os.path.isdir(d.get("root") or ""):
        return None
    tree = snapshot(d["root"])
    if not tree or tree == d.get("last"):
        return None
    files = d["files"] if d["files"] and not d["bash"] else None
    meta = {"kind": "after", "agent": d["agent"], "turn": d["turn"], "before": d["before"], "after": tree,
            "prompt": d["prompt"], "files": files, "started": d["at"], "at": time.time()}
    if d.get("undo_of"):
        meta["undo_of"] = d["undo_of"]
    sha = _commit(d["root"], d["before"], tree, meta)
    if sha:
        d["last"] = tree
        _save_slot(slot, d)
    return sha


def from_hook(data):
    ev = data.get("hook_event_name") or ""
    sid = data.get("session_id") or data.get("cwd") or ""
    slot = f"claude|{sid}"
    if ev == "UserPromptSubmit":
        begin(slot, "claude", data.get("cwd") or os.getcwd(), data.get("prompt") or "")
    elif ev == "PostToolUse":
        tool = data.get("tool_name") or ""
        inp = data.get("tool_input") or {}
        if tool in EDIT_TOOLS:
            touch(slot, inp.get("file_path") or inp.get("notebook_path"))
        elif tool == "Bash":
            touch(slot, bash=True)
    elif ev == "Stop":
        end(slot)


def _entries(root, limit):
    head = _head(root)
    if not head:
        return []
    key = (root, head, limit)
    if key in _cache:
        return _cache[key]
    r = _git(root, "log", f"-n{limit * 3}", "--format=%H%x1f%B%x1e", REF)
    out, seen = [], set()
    for rec in (_out(r) or "").split("\x1e"):
        rec = rec.strip()
        if not rec or "\x1f" not in rec:
            continue
        sha, body = rec.split("\x1f", 1)
        try:
            m = json.loads(body)
        except ValueError:
            continue
        if m.get("kind") != "after" or m.get("turn") in seen:
            continue
        seen.add(m["turn"])
        paths = m.get("files") or []
        ns = _out(_git(root, "diff", "--numstat", m["before"], m["after"], "--", *paths)) or ""
        files, add, rem = [], 0, 0
        for line in ns.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                a, b = (int(x) if x.isdigit() else 0 for x in parts[:2])
                add, rem = add + a, rem + b
                files.append(parts[2])
        out.append({"id": sha[:10], "sha": sha, "agent": m.get("agent"), "prompt": m.get("prompt") or "",
                    "at": m.get("at"), "started": m.get("started"), "files": files, "added": add, "removed": rem,
                    "scoped": bool(m.get("files")), "undo_of": m.get("undo_of")})
        if len(out) >= limit:
            break
    undoers = {}
    for e in out:
        if e.get("undo_of"):
            undoers.setdefault(e["undo_of"], []).append(e)
    for e in out:
        e["undone"] = any(not u["undone"] for u in undoers.get(e["sha"], []))
    if len(_cache) > 50:
        _cache.clear()
    _cache[key] = out
    return out


def listing(cwd, limit=30):
    root = root_of(cwd)
    if not root:
        return {"root": None, "turns": []}
    return {"root": root, "turns": _entries(root, limit)}


def _find(root, ident):
    sha = _out(_git(root, "rev-parse", "--verify", "-q", ident + "^{commit}", timeout=5))
    if not sha:
        raise ValueError(f"no checkpoint {ident}")
    try:
        m = json.loads(_out(_git(root, "log", "-1", "--format=%B", sha)) or "")
    except ValueError:
        raise ValueError(f"{ident} is not a nyrasview checkpoint")
    if not isinstance(m, dict) or not m.get("before") or not m.get("after"):
        raise ValueError(f"{ident} is not a nyrasview checkpoint")
    return sha, m


def _need_root(cwd):
    root = root_of(cwd)
    if not root:
        raise ValueError(f"{cwd} is not inside a git repository")
    return root


def diff(cwd, ident, limit=400000):
    root = _need_root(cwd)
    sha, m = _find(root, ident)
    r = _git(root, "diff", "--no-color", m["before"], m["after"], "--", *(m.get("files") or []))
    text = r.stdout.decode("utf-8", "replace") if r is not None else ""
    return text[:limit] + ("\n… (diff truncated)" if len(text) > limit else "")


def _apply(root, patch, reverse=False, check=False):
    args = ["apply", "--whitespace=nowarn"] + (["-R"] if reverse else []) + (["--check"] if check else [])
    r = _git(root, *args, data=patch, timeout=60)
    if r is None:
        return False, "git apply did not finish"
    return r.returncode == 0, r.stderr.decode("utf-8", "replace").strip()


def _conflicts(err):
    files = []
    for line in err.splitlines():
        if line.startswith("error: ") and ":" in line[7:]:
            f = line[7:].split(":")[0].replace("patch failed", "").strip()
            if f and f not in files and " " not in f:
                files.append(f)
    return files


def undo(cwd, ident, dry=False):
    root = _need_root(cwd)
    sha, m = _find(root, ident)
    paths = m.get("files") or []
    patch = _git(root, "diff", "--binary", "--full-index", m["before"], m["after"], "--", *paths)
    patch = patch.stdout if patch is not None else b""
    files = (_out(_git(root, "diff", "--name-only", m["before"], m["after"], "--", *paths)) or "").splitlines()
    if not patch.strip():
        return {"ok": True, "files": [], "message": "that turn changed nothing"}
    ok, err = _apply(root, patch, reverse=True, check=True)
    if not ok:
        return {"ok": False, "files": files, "conflicts": _conflicts(err),
                "message": "the files changed again since that turn, so it can't be undone cleanly", "detail": err[-2000:]}
    if dry:
        return {"ok": True, "files": files, "message": "can be undone cleanly", "dry": True}
    slot = f"undo|{sha[:10]}|{time.time()}"
    what = m.get("prompt") or ""
    if m.get("undo_of") and what.startswith("undo "):
        label = "redo " + what.split(": ", 1)[-1]
    elif m.get("undo_of") and what.startswith("redo "):
        label = "undo " + what[5:]
    else:
        label = f"undo {m.get('agent')}: {what}"
    if not begin(slot, "you", root, label[:300]):
        raise ValueError("couldn't save a safety checkpoint, nothing was changed")
    ok, err = _apply(root, patch, reverse=True)
    if not ok:
        raise ValueError("git apply failed: " + err[-500:])
    d = _load_slot(slot)
    d["undo_of"] = sha
    _save_slot(slot, d)
    new = end(slot)
    _slot_file(slot).unlink(missing_ok=True)
    return {"ok": True, "files": files, "message": f"undid {len(files)} file(s)", "checkpoint": (new or "")[:10]}


def restore(cwd, ident, before=False):
    root = _need_root(cwd)
    sha, m = _find(root, ident)
    target = m["before"] if before else m["after"]
    slot = f"restore|{sha[:10]}|{time.time()}"
    if not begin(slot, "you", root, f"restore to {'before' if before else 'after'} {sha[:10]}"):
        raise ValueError("couldn't save a safety checkpoint, nothing was changed")
    now_tree = _load_slot(slot)["before"]
    patch = _git(root, "diff", "--binary", "--full-index", now_tree, target)
    patch = patch.stdout if patch is not None else b""
    files = (_out(_git(root, "diff", "--name-only", now_tree, target)) or "").splitlines()
    if patch.strip():
        ok, err = _apply(root, patch)
        if not ok:
            raise ValueError("git apply failed: " + err[-500:])
    new = end(slot)
    _slot_file(slot).unlink(missing_ok=True)
    return {"ok": True, "files": files, "message": f"restored {len(files)} file(s)", "checkpoint": (new or "")[:10]}
