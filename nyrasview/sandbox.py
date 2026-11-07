import os
import shutil
import subprocess

from .util import HOME, config

AGENT_DIRS = {
    "claude": [".claude", ".claude.json"],
    "codex": [".codex"],
    "gemini": [".gemini"],
    "opencode": [".config/opencode", ".local/share/opencode"],
    "qwen": [".qwen"],
    "kimi": [".kimi"],
    "cursor": [".cursor"],
    "copilot": [".copilot"],
}


def settings():
    s = config().get("sandbox", {})
    return {"image": s.get("image", ""), "runtime": s.get("runtime") or ("podman" if shutil.which("podman") and not shutil.which("docker") else "docker"),
            "home": s.get("home", "/root"), "mount_logins": s.get("mount_logins", True), "network": s.get("network", ""),
            "extra_args": list(s.get("extra_args") or []), "collab": s.get("collab", False), "default": s.get("default", False)}


def ready():
    s = settings()
    return bool(s["image"]) and bool(shutil.which(s["runtime"]))


def wrap(argv, cwd, agent=None, env=None):
    s = settings()
    if not s["image"]:
        raise RuntimeError("no sandbox image set: add one in Settings → Sandbox")
    if not shutil.which(s["runtime"]):
        raise RuntimeError(f"{s['runtime']} is not installed")
    cmd = [s["runtime"], "run", "--rm", "-i", "-v", f"{cwd}:/work", "-w", "/work", "-e", f"HOME={s['home']}",
           "-e", "NO_COLOR=1"]
    if s["mount_logins"] and agent in AGENT_DIRS:
        for rel in AGENT_DIRS[agent]:
            src = HOME / rel
            if src.exists():
                cmd += ["-v", f"{src}:{s['home'].rstrip('/')}/{rel}"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    if s["network"]:
        cmd += ["--network", s["network"]]
    cmd += s["extra_args"] + [s["image"]] + list(argv)
    return cmd


def test():
    s = settings()
    if not s["image"]:
        return {"ok": False, "output": "Set an image first."}
    if not shutil.which(s["runtime"]):
        return {"ok": False, "output": f"{s['runtime']} is not installed on this machine."}
    probe = "for a in claude codex gemini opencode qwen kimi cursor-agent copilot crush amp goose aider; do command -v $a >/dev/null 2>&1 && echo $a; done; true"
    try:
        r = subprocess.run([s["runtime"], "run", "--rm", s["image"], "sh", "-c", probe], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "Timed out starting the container (still pulling the image?)."}
    except OSError as e:
        return {"ok": False, "output": str(e)}
    if r.returncode != 0:
        return {"ok": False, "output": (r.stderr or r.stdout).strip()[-600:]}
    found = [x for x in r.stdout.split() if x]
    return {"ok": True, "agents": found,
            "output": ("Agents available in the image: " + ", ".join(found)) if found else "The container runs, but no agent CLIs were found in it."}
