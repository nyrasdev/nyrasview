import hashlib
import json
import time

from . import checkpoints as cp
from . import plat
from .util import STATE

DIR = STATE / "reviews"
_parsed = {}


def _state_file(root):
    return DIR / (hashlib.sha1(root.encode()).hexdigest()[:16] + ".json")


def _load(root):
    try:
        return json.loads(_state_file(root).read_text())
    except (OSError, ValueError):
        return {}


def _save(root, d):
    DIR.mkdir(parents=True, exist_ok=True)
    f = _state_file(root)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    plat.replace(tmp, f)


def _split(root, m):
    key = (root, m["before"], m["after"], tuple(m.get("files") or []))
    if key in _parsed:
        return _parsed[key]
    r = cp._git(root, "diff", "--no-color", "--no-renames", "-U3", m["before"], m["after"], "--", *(m.get("files") or []))
    text = r.stdout.decode("utf-8", "replace") if r is not None else ""
    out = []
    for chunk in ("\n" + text).split("\ndiff --git ")[1:]:
        lines = ("diff --git " + chunk).split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        i = next((n for n, l in enumerate(lines) if l.startswith("@@")), len(lines))
        head, body = lines[:i], lines[i:]
        f = next((l[6:] for l in head if l.startswith("+++ b/")), None) or next((l[6:] for l in head if l.startswith("--- a/")), None)
        if not f:
            parts = head[0].split(" b/", 1)
            f = parts[1] if len(parts) > 1 else head[0]
        binary = any(l.startswith("Binary files") or l.startswith("GIT binary patch") for l in head)
        kind = "new" if any(l.startswith("new file") for l in head) else "deleted" if any(l.startswith("deleted file") for l in head) else "changed"
        hunks, cur = [], None
        for l in body:
            if l.startswith("@@"):
                cur = [l]
                hunks.append(cur)
            elif cur is not None:
                cur.append(l)
        if not hunks:
            hunks = [[]]
        for n, h in enumerate(hunks):
            txt = "\n".join(h)
            hid = hashlib.sha1(f"{f}\n{txt}".encode()).hexdigest()[:12]
            out.append({"id": hid, "file": f, "kind": kind, "binary": binary, "n": n + 1, "of": len(hunks),
                        "head": head, "lines": h, "added": sum(1 for l in h if l.startswith("+")),
                        "removed": sum(1 for l in h if l.startswith("-"))})
    if len(_parsed) > 300:
        _parsed.clear()
    _parsed[key] = out
    return out


def hunks(cwd, ident):
    root = cp._need_root(cwd)
    sha, m = cp._find(root, ident)
    st = _load(root)
    out = []
    for h in _split(root, m):
        d = dict(h)
        d["status"] = st.get(f"{sha}:{h['id']}")
        out.append(d)
    return {"root": root, "id": sha[:10], "agent": m.get("agent"), "prompt": m.get("prompt"), "hunks": out}


def pending(root, sha):
    try:
        m = cp._find(root, sha)[1]
    except ValueError:
        return 0
    st = _load(root)
    return sum(1 for h in _split(root, m) if not st.get(f"{sha}:{h['id']}"))


def _patch(root, m, h):
    if h["binary"]:
        r = cp._git(root, "diff", "--binary", "--full-index", "--no-renames", m["before"], m["after"], "--", h["file"])
        return r.stdout if r is not None else b""
    return ("\n".join(h["head"] + h["lines"]) + "\n").encode()


def decide(cwd, ident, hid, action):
    if action not in ("accept", "reject", "reset"):
        raise ValueError("action must be accept, reject or reset")
    root = cp._need_root(cwd)
    sha, m = cp._find(root, ident)
    h = next((x for x in _split(root, m) if x["id"] == hid), None)
    if h is None:
        raise ValueError("no such change in that turn")
    st = _load(root)
    key = f"{sha}:{hid}"
    if action == "reset":
        st.pop(key, None)
        _save(root, st)
        return {"ok": True, "status": None}
    if action == "accept":
        st[key] = "accepted"
        _save(root, st)
        return {"ok": True, "status": "accepted"}
    if st.get(key) == "rejected":
        return {"ok": True, "status": "rejected", "message": "already rejected"}
    patch = _patch(root, m, h)
    ok, err = cp._apply(root, patch, reverse=True, check=True)
    if not ok:
        return {"ok": False, "status": st.get(key), "message": f"{h['file']} changed again since, so this part can't be taken back cleanly",
                "detail": err[-1000:]}
    slot = f"review|{sha[:10]}|{hid}|{time.time()}"
    label = f"reject {h['file']} ({h['n']}/{h['of']}) from {m.get('agent')}: {m.get('prompt') or ''}"
    if not cp.begin(slot, "you", root, label[:300]):
        raise ValueError("couldn't save a safety checkpoint, nothing was changed")
    ok, err = cp._apply(root, patch, reverse=True)
    if not ok:
        raise ValueError("git apply failed: " + err[-400:])
    new = cp.end(slot)
    cp._slot_file(slot).unlink(missing_ok=True)
    st[key] = "rejected"
    _save(root, st)
    return {"ok": True, "status": "rejected", "message": f"took back that part of {h['file']}", "checkpoint": (new or "")[:10]}


def accept_rest(cwd, ident):
    d = hunks(cwd, ident)
    root = d["root"]
    sha = cp._find(root, ident)[0]
    st = _load(root)
    n = 0
    for h in d["hunks"]:
        if not h["status"]:
            st[f"{sha}:{h['id']}"] = "accepted"
            n += 1
    _save(root, st)
    return {"ok": True, "accepted": n}


def queue(cwd, limit=40):
    lst = cp.listing(cwd, limit)
    if not lst["root"]:
        return {"root": None, "turns": []}
    out = []
    for t in lst["turns"]:
        if t["agent"] == "you" or t["undone"] or not t["files"]:
            continue
        n = pending(lst["root"], t["sha"])
        if n:
            out.append(dict(t, pending=n))
    return {"root": lst["root"], "turns": out}
