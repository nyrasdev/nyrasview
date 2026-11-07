import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from . import plat
from .util import STATE, config

INDEX = STATE / "collab.json"
HIST = STATE / "history"
HEAD_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_mem = threading.Lock()


def _stem(doc):
    return re.sub(r"[^\w.-]", "_", str(Path(doc).resolve()).strip("/"))


def plan_path(doc):
    return STATE / "plans" / (_stem(doc) + ".json")


@contextmanager
def _locked(path):
    path = STATE / "locks" / (_stem(path) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _mem, plat.file_lock(path):
        yield


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def register(doc):
    doc = str(Path(doc).resolve())
    with _locked(INDEX):
        idx = _read_json(INDEX, [])
        if doc in idx:
            idx.remove(doc)
        idx.insert(0, doc)
        _write_json(INDEX, idx[:30])


def docs():
    return [d for d in _read_json(INDEX, []) if Path(d).exists()]


def load(doc):
    doc = str(Path(doc).resolve())
    p = _read_json(plan_path(doc), None)
    if not isinstance(p, dict):
        p = {"doc": doc, "tasks": [], "created": time.time()}
    p["doc"] = doc
    return p


def save(plan):
    _write_json(plan_path(plan["doc"]), plan)


def _update(doc, fn):
    with _locked(plan_path(doc)):
        p = load(doc)
        r = fn(p)
        save(p)
        return r


def sections(text):
    lines = text.split("\n")
    heads, fence = [], False
    for n, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        m = HEAD_RE.match(line)
        if m:
            heads.append((n, len(m.group(1)), m.group(2)))
    out = []
    for k, (n, lvl, title) in enumerate(heads):
        end = len(lines)
        for n2, lvl2, _ in heads[k + 1:]:
            if lvl2 <= lvl:
                end = n2
                break
        nxt = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        out.append({"title": title, "level": lvl, "line": n, "body_end": nxt, "end": end})
    return out


def new_doc(doc, title, names):
    path = Path(doc)
    if path.exists() and path.read_text().strip():
        raise FileExistsError(f"{doc} already has content")
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [f"# {title}", ""]
    for s in names:
        body += [f"## {s.strip()}", "", ""]
    path.write_text("\n".join(body))
    register(path)
    _update(path, lambda p: None)
    return str(path.resolve())


def assign(doc, section, agent, instruction, model=None):
    register(doc)
    task = {"id": secrets.token_hex(3), "section": section or "", "agent": agent,
            "scope": "doc" if not section else "section", "model": model or None,
            "instruction": instruction, "status": "pending", "created": time.time()}

    def f(p):
        p["tasks"].append(task)
        return task
    return _update(doc, f)


def remove(doc, tid):
    def f(p):
        p["tasks"] = [t for t in p["tasks"] if t["id"] != tid]
    _update(doc, f)


def reset(doc, tid=None):
    def f(p):
        for t in p["tasks"]:
            if tid is None or t["id"] == tid:
                if t.get("status") != "running" or not _alive(t.get("pid")):
                    t.update(status="pending", error=None, pid=None)
    _update(doc, f)


def _alive(pid):
    return plat.alive(pid)


def _set(doc, tid, **kw):
    def f(p):
        for t in p["tasks"]:
            if t["id"] == tid:
                t.update(kw)
    _update(doc, f)


def view(doc):
    p = load(doc)
    try:
        text = Path(p["doc"]).read_text()
    except OSError:
        text = ""
    from . import obsidian
    v = obsidian.vault_of(p["doc"])
    return {"doc": p["doc"], "name": Path(p["doc"]).name, "text": text,
            "vault": v["name"] if v else None, "obsidian": obsidian.uri(p["doc"]) if v else None,
            "sections": [{"title": s["title"], "level": s["level"]} for s in sections(text)],
            "tasks": p["tasks"], "history": len(_backups(p["doc"]))}


def overview():
    out = []
    for d in docs()[:10]:
        p = load(d)
        out.append({"doc": d, "name": Path(d).name, "tasks": p["tasks"]})
    return out


def _backups(doc):
    return sorted(HIST.glob(f"{_stem(doc)}.*.md"))


def _backup(doc, text):
    HIST.mkdir(parents=True, exist_ok=True)
    (HIST / f"{_stem(doc)}.{time.time_ns()}.md").write_text(text)
    for old in _backups(doc)[:-40]:
        old.unlink(missing_ok=True)


def undo(doc):
    b = _backups(doc)
    if not b:
        return False
    with _locked(Path(doc).resolve()):
        Path(doc).write_text(b[-1].read_text())
        b[-1].unlink()
    return True


def _clean(out, title):
    s = out.strip()
    m = re.match(r"^```[\w-]*\n(.*)\n```$", s, re.S)
    if m:
        s = m.group(1).strip()
    if title:
        lines = s.split("\n")
        hm = HEAD_RE.match(lines[0]) if lines else None
        if hm and hm.group(2).strip().lower() == title.strip().lower():
            s = "\n".join(lines[1:]).strip()
    return s


def apply(doc, task, output):
    path = Path(doc).resolve()
    with _locked(path):
        text = path.read_text()
        _backup(path, text)
        if task["scope"] == "doc":
            new = output.rstrip() + "\n"
            if text.startswith("---\n") and not new.startswith("---\n"):
                end = text.find("\n---", 4)
                if end > 0:
                    new = text[:end + 4].rstrip("\n") + "\n\n" + new
        else:
            lines = text.split("\n")
            sec = next((s for s in sections(text) if s["title"].strip().lower() == task["section"].strip().lower()), None)
            if sec is None:
                new = text.rstrip() + f"\n\n## {task['section']}\n\n{output}\n"
            else:
                body = ["", *output.split("\n"), ""]
                new = "\n".join(lines[:sec["line"] + 1] + body + lines[sec["body_end"]:])
        path.write_text(new)


def prompt_for(task, plan, text, agent_name, others):
    name = Path(plan["doc"]).name
    team = ", ".join(others) if others else "no one else yet"
    common = (f"You are {agent_name}, one of several AI agents co-writing the document \"{name}\". "
              f"Other contributors: {team}.\n"
              "Do not edit files, run commands or use tools; reply with text only.\n\n")
    if task["scope"] == "doc":
        ask = ("Your assignment covers the WHOLE document.\n"
               f"Instruction: {task['instruction']}\n\n"
               "Reply with the complete revised document in markdown and nothing else.")
    else:
        ask = (f"Your assignment is ONLY the section titled \"{task['section']}\".\n"
               f"Instruction: {task['instruction']}\n\n"
               "Reply with the markdown body of that section only: no heading line for it, no preface, "
               "no closing remarks, no code fence around the whole answer. Stay consistent with the rest "
               "of the document and do not repeat other sections.")
    return f"{common}{ask}\n\n<document>\n{text}\n</document>\n"


def _http_json(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def call(agent_key, prompt, model=None, cwd=None, on_pid=None, timeout=None):
    from . import net
    from .registry import BY_KEY
    cfg = config()
    timeout = timeout or cfg.get("collab", {}).get("timeout", 900)
    a = BY_KEY.get(agent_key)
    if a is None or not a.headless:
        raise RuntimeError(f"{agent_key} has no headless mode")
    h = a.headless
    if h.get("http") == "openrouter":
        key = net.openrouter_key()
        if not key:
            raise RuntimeError("set OPENROUTER_API_KEY or [openrouter] api_key in config")
        m = model or cfg.get("openrouter", {}).get("model", "openrouter/auto")
        d = _http_json("https://openrouter.ai/api/v1/chat/completions",
                       {"model": m, "messages": [{"role": "user", "content": prompt}]},
                       {"Authorization": f"Bearer {key}", "X-Title": "nyrasview"}, timeout)
        if "error" in d:
            raise RuntimeError(str(d["error"])[:300])
        return d["choices"][0]["message"]["content"] or ""
    if h.get("http") == "local":
        from . import local
        m = model or cfg.get(h["server"], {}).get("model")
        return local.chat(h["server"], prompt, m, timeout)[0]
    argv = list(h["cmd"])
    if not shutil.which(argv[0]):
        raise RuntimeError(f"{argv[0]} is not installed")
    if model and h.get("model"):
        at = 2 if len(argv) > 1 and not argv[1].startswith("-") else 1
        argv[at:at] = [h["model"], model]
    fd, out = tempfile.mkstemp(prefix="nyra-", suffix=".txt")
    os.close(fd)
    argv = [x.replace("{prompt}", prompt).replace("{out}", out) for x in argv]
    stdin = prompt if h.get("stdin") else None
    try:
        proc = subprocess.Popen(plat.resolve(argv), cwd=cwd, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", **plat.detached())
        if on_pid:
            on_pid(proc.pid)
        try:
            so, se = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise RuntimeError(f"timed out after {timeout}s")
        if proc.returncode != 0:
            raise RuntimeError(f"exit {proc.returncode}: {(se or so).strip()[-400:]}")
        result = Path(out).read_text() if "{out}" in " ".join(h["cmd"]) else ""
        return result.strip() or so
    finally:
        Path(out).unlink(missing_ok=True)


def run(doc, only=None, parallel=4, on_event=None, rerun=False):
    from .registry import BY_KEY
    doc = str(Path(doc).resolve())
    register(doc)
    plan = load(doc)
    todo = [t for t in plan["tasks"] if (only is None or t["id"] in only)
            and (rerun or t["status"] in ("pending", "failed"))]
    if not todo:
        return []
    names = sorted({BY_KEY[t["agent"]].name if t["agent"] in BY_KEY else t["agent"] for t in plan["tasks"]})
    emit = on_event or (lambda *a: None)
    results = []

    def one(task):
        aname = BY_KEY[task["agent"]].name if task["agent"] in BY_KEY else task["agent"]
        text = Path(doc).read_text()
        prompt = prompt_for(task, plan, text, aname, [n for n in names if n != aname])
        _set(doc, task["id"], status="running", started=time.time(), error=None, pid=None)
        emit("start", task)
        try:
            out = call(task["agent"], prompt, task.get("model"), cwd=str(Path(doc).parent),
                       on_pid=lambda pid: _set(doc, task["id"], pid=pid))
            out = _clean(out, task["section"])
            if not out:
                raise RuntimeError("empty reply")
            apply(doc, task, out)
            _set(doc, task["id"], status="done", finished=time.time(), chars=len(out), pid=None)
            _authors(doc)
            task.update(status="done", chars=len(out), finished=time.time())
            emit("done", task)
        except Exception as e:
            _set(doc, task["id"], status="failed", finished=time.time(), error=str(e)[:500], pid=None)
            task.update(status="failed", error=str(e)[:500], finished=time.time())
            emit("fail", task)
        results.append(task)

    sect = [t for t in todo if t["scope"] == "section"]
    whole = [t for t in todo if t["scope"] == "doc"]
    if sect:
        with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
            list(ex.map(one, sect))
    for t in whole:
        one(t)
    return results


def _authors(doc):
    from . import obsidian
    if obsidian.vault_of(doc) is None:
        return
    done = [t["agent"] for t in load(doc)["tasks"] if t["status"] == "done"]
    with _locked(Path(doc).resolve()):
        obsidian.set_authors(doc, done)


def run_async(doc, **kw):
    th = threading.Thread(target=run, args=(doc,), kwargs=kw, daemon=True)
    th.start()
    return th
