import json
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path

from . import plat
from .util import STATE, config

DIR = STATE / "heal"
TAIL = 7000


def settings():
    h = config().get("heal", {})
    return {"tries": int(h.get("tries", 3)), "timeout": int(h.get("timeout", 600)), "agent": h.get("agent", "claude"),
            "mode": h.get("mode", "edit"), "projects": dict(h.get("projects") or {})}


def detect(cwd):
    p = Path(cwd)
    pj = p / "package.json"
    if pj.exists():
        try:
            scripts = json.loads(pj.read_text()).get("scripts", {})
        except ValueError:
            scripts = {}
        runner = "pnpm" if (p / "pnpm-lock.yaml").exists() else "yarn" if (p / "yarn.lock").exists() else "bun" if (p / "bun.lockb").exists() or (p / "bun.lock").exists() else "npm"
        parts = [f"{runner} run {s}" for s in ("typecheck", "lint", "test", "build") if s in scripts and "no test specified" not in str(scripts.get(s))]
        if parts:
            return " && ".join(parts[:3])
    if (p / "Cargo.toml").exists():
        return "cargo test"
    if (p / "go.mod").exists():
        return "go vet ./... && go test ./..."
    if (p / "pyproject.toml").exists() or (p / "pytest.ini").exists() or (p / "tests").is_dir():
        return "pytest -q" if (p / "tests").is_dir() or (p / "pytest.ini").exists() else "python -m compileall -q ."
    mk = p / "Makefile"
    if mk.exists() and "test:" in mk.read_text(errors="replace"):
        return "make test"
    return None


def project_cmd(cwd):
    projects = settings()["projects"]
    real = os.path.realpath(cwd)
    best = None
    for path, cmd in projects.items():
        rp = os.path.realpath(os.path.expanduser(path))
        if (real == rp or real.startswith(rp + os.sep)) and (best is None or len(rp) > len(best[0])):
            best = (rp, cmd)
    return best[1] if best else None


def set_project(cwd, cmd):
    from . import settings as st
    cfg = st.load()
    h = cfg.setdefault("heal", {})
    projects = h.setdefault("projects", {})
    key = os.path.realpath(os.path.expanduser(cwd))
    if cmd:
        projects[key] = cmd
    else:
        projects.pop(key, None)
    if not projects:
        h.pop("projects", None)
    st.write_raw(st.dump(cfg))


def check(cmd, cwd, timeout=None):
    timeout = timeout or settings()["timeout"]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=dict(os.environ, CI="1", FORCE_COLOR="0", NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1"))
        out = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
        code = r.returncode
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")) + f"\n[timed out after {timeout}s]"
        code = 124
    out = out.strip()
    return {"ok": code == 0, "code": code, "tail": out[-TAIL:], "lines": out.count("\n") + 1 if out else 0,
            "dur": round(time.time() - t0, 1)}


def fix_prompt(cmd, cwd, res, attempt, tries):
    again = "" if attempt == 1 else f" This is try {attempt} of {tries}; your previous fix didn't make it pass yet, so look at the new output carefully."
    return (f"The check `{cmd}` fails in {cwd} (exit code {res['code']}).{again}\n\n"
            "Fix the code so the check passes. Rules: don't delete, skip or weaken tests, don't change the check itself, "
            "don't silence errors. Make the smallest correct change. When you're done, say in one or two sentences what you changed.\n\n"
            f"Output (last part):\n```\n{res['tail']}\n```\n")


def _path(jid):
    return DIR / f"{jid}.json"


def save(job):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / f"{job['id']}.tmp"
    tmp.write_text(json.dumps(job, indent=1))
    plat.replace(tmp, _path(job["id"]))


def load(jid):
    try:
        return json.loads(_path(jid).read_text())
    except (OSError, ValueError):
        return None


def listing(limit=30):
    if not DIR.is_dir():
        return []
    out = []
    for p in sorted(DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return out


def run_job(jid, on_event=None):
    from . import notify, runs
    job = load(jid)
    emit = on_event or (lambda *a: None)
    parent = None
    for attempt in range(1, job["tries"] + 2):
        job["status"] = "checking"
        save(job)
        emit("check", job)
        res = check(job["cmd"], job["cwd"])
        step = {"n": attempt, "check": res, "run": None, "at": time.time()}
        job["attempts"].append(step)
        if res["ok"]:
            job.update(status="passed", finished=time.time())
            save(job)
            emit("passed", job)
            if attempt > 1:
                notify.emit("done", job["agent"], f"Fixed: {job['cmd']}", f"Passes after {attempt - 1} fix{'es' if attempt > 2 else ''} in {os.path.basename(job['cwd'])}",
                            f"heal|{jid}", job["cwd"])
            return job
        if attempt > job["tries"]:
            break
        job["status"] = "fixing"
        save(job)
        emit("failing", job)
        m = runs.start(job["agent"], fix_prompt(job["cmd"], job["cwd"], res, attempt, job["tries"]), job["cwd"],
                       job["mode"], job.get("model"), None, parent, sandboxed=job.get("sandbox"),
                       label=f"self-heal fix {attempt}/{job['tries']} · {job['cmd']}")
        step["run"] = m["id"]
        save(job)
        emit("fixing", job)
        while True:
            v = runs.view(m["id"])
            if not v or v["status"] != "running":
                break
            if load(jid).get("status") == "stopped":
                runs.stop(m["id"])
                return load(jid)
            time.sleep(1)
        v = runs.view(m["id"]) or {}
        if v.get("retried_as"):
            m = runs.load(v["retried_as"])
            step["run"] = m["id"]
            while (runs.view(m["id"]) or {}).get("status") == "running":
                time.sleep(1)
            v = runs.view(m["id"]) or {}
        step["fix"] = v.get("status")
        step["said"] = next((e["text"] for e in reversed(v.get("events") or []) if e["k"] == "text"), "")[:600]
        save(job)
        if v.get("status") == "failed":
            job.update(status="failed", finished=time.time(), error=f"the agent run failed: {v.get('error') or ''}"[:400])
            save(job)
            emit("failed", job)
            notify.emit("failed", job["agent"], f"Self-heal stopped: {job['cmd']}", job["error"], f"heal|{jid}", job["cwd"], urgent=True)
            return job
        parent = m["id"]
    job.update(status="failed", finished=time.time(), error=f"still failing after {job['tries']} tries")
    save(job)
    emit("failed", job)
    notify.emit("failed", job["agent"], f"Still failing: {job['cmd']}", f"{job['tries']} fixes didn't make it pass in {os.path.basename(job['cwd'])}. Your turn.",
                f"heal|{jid}", job["cwd"], urgent=True)
    return job


def start(cwd, cmd=None, agent=None, tries=None, mode=None, model=None, sandbox=None, background=True, on_event=None):
    s = settings()
    cwd = os.path.realpath(os.path.expanduser(cwd or "."))
    cmd = cmd or project_cmd(cwd) or detect(cwd)
    if not cmd:
        raise ValueError("no check command: pass one, e.g. \"npm test\" or \"pytest -q\"")
    job = {"id": secrets.token_hex(4), "cwd": cwd, "cmd": cmd, "agent": agent or s["agent"], "tries": int(tries or s["tries"]),
           "mode": mode or s["mode"], "model": model, "sandbox": sandbox, "status": "queued", "attempts": [], "created": time.time()}
    save(job)
    if background:
        threading.Thread(target=run_job, args=(job["id"], on_event), daemon=True).start()
        return job
    return run_job(job["id"], on_event)


def stop(jid):
    job = load(jid)
    if job and job["status"] not in ("passed", "failed"):
        job["status"] = "stopped"
        job["finished"] = time.time()
        save(job)
    return job


def stop_hook(data):
    cwd = data.get("cwd") or ""
    cmd = project_cmd(cwd)
    if not cmd:
        return None
    s = settings()
    sid = data.get("session_id") or cwd
    DIR.mkdir(parents=True, exist_ok=True)
    f = DIR / f"live-{''.join(c if c.isalnum() else '-' for c in sid)[:80]}.json"
    try:
        st = json.loads(f.read_text())
    except (OSError, ValueError):
        st = {"n": 0}
    if time.time() - st.get("at", 0) > 3600:
        st = {"n": 0}
    res = check(cmd, cwd)
    from . import notify
    proj = os.path.basename(cwd.rstrip("/")) or cwd
    if res["ok"]:
        if st.get("n"):
            notify.emit("done", "claude", f"Checks pass · {proj}", f"`{cmd}` passes after {st['n']} automatic fix{'es' if st['n'] > 1 else ''}",
                        f"heal-live|{sid}|{int(time.time() // 30)}", cwd)
        f.unlink(missing_ok=True)
        return None
    if st["n"] >= s["tries"]:
        notify.emit("failed", "claude", f"Still failing · {proj}", f"`{cmd}` fails after {st['n']} automatic tries. Your turn.",
                    f"heal-live|{sid}|{int(time.time() // 30)}", cwd, urgent=True)
        f.unlink(missing_ok=True)
        return None
    st["n"] += 1
    st["at"] = time.time()
    f.write_text(json.dumps(st))
    return {"decision": "block",
            "reason": f"[nyrasview self-heal, try {st['n']} of {s['tries']}] " + fix_prompt(cmd, cwd, res, st["n"], s["tries"])}
