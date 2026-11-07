import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
ROOT = Path(tempfile.mkdtemp(prefix="nyra-smoke-"))
HOME = ROOT / "home"
WORK = ROOT / "work"
for d in (HOME, WORK):
    d.mkdir(parents=True)
PY = sys.executable
ENV = dict(os.environ, HOME=str(HOME), USERPROFILE=str(HOME), APPDATA=str(HOME / "AppData/Roaming"),
           LOCALAPPDATA=str(HOME / "AppData/Local"), XDG_STATE_HOME=str(HOME / ".local/state"),
           XDG_CONFIG_HOME=str(HOME / ".config"), NO_COLOR="1", PYTHONIOENCODING="utf-8")
for k in ("OPENROUTER_API_KEY", "WAYLAND_DISPLAY", "DISPLAY"):
    ENV.pop(k, None)
FAILS = []


def run(*args, input=None, timeout=120, check=True):
    r = subprocess.run([PY, "-m", "nyrasview", *args], input=input, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=ENV, cwd=str(WORK), timeout=timeout)
    if check and r.returncode != 0:
        raise AssertionError(f"nyrasview {' '.join(args)} exited {r.returncode}\n{r.stdout}\n{r.stderr}")
    return r.stdout + r.stderr


def step(name):
    def wrap(fn):
        t0 = time.time()
        try:
            fn()
            print(f"PASS {name} ({time.time() - t0:.1f}s)", flush=True)
        except Exception as e:
            FAILS.append(name)
            print(f"FAIL {name}: {e}", flush=True)
        return fn
    return wrap


def config_dir():
    if os.name == "nt":
        return HOME / "AppData/Roaming/nyrasview"
    return HOME / ".config/nyrasview"


fake_agent = ROOT / "fake_agent.py"
fake_agent.write_text(textwrap.dedent('''
    import re, sys
    p = sys.stdin.read()
    m = re.search(r'section titled "([^"]+)"', p)
    print(("wrote " + m.group(1)) if m else "héllo from the fake agent ✓")
'''), encoding="utf-8")
fake_mcp = ROOT / "fake_mcp.py"
fake_mcp.write_text(textwrap.dedent('''
    import json, sys
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        if m["method"] == "initialize":
            r = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
        elif m["method"] == "tools/list":
            r = {"tools": [{"name": "ping", "inputSchema": {"type": "object"}}]}
        else:
            r = {}
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
'''), encoding="utf-8")
cfg = config_dir()
cfg.mkdir(parents=True, exist_ok=True)
py = PY.replace("\\", "\\\\")
(cfg / "config.toml").write_text(textwrap.dedent(f'''
    [notify]
    desktop = false

    [agents.echoer]
    name = "Echoer"
    headless = {{ cmd = ["{py}", "{str(fake_agent).replace(chr(92), chr(92) * 2)}"], stdin = true }}

    [agents.codex]
    interactive = ["{py}", "-c", "pass"]
'''), encoding="utf-8")


@step("version and agents")
def _():
    assert "nyrasview" in run("--version")
    assert "Echoer" in run("agents")


@step("status --json")
def _():
    d = json.loads(run("status", "--json"))
    assert isinstance(d["agents"], list) and d["system"]["mem_total"] > 0


@step("limits and usage")
def _():
    json.loads(run("limits", "--json") or "{}") if run("limits", "--json").strip().startswith("{") else None
    run("usage")


@step("collab with a fake agent (process launch, file locks, utf-8)")
def _():
    doc = WORK / "plan.md"
    run("collab", "new", str(doc), "-t", "Plan", "-s", "Goals,Risks")
    run("collab", "assign", str(doc), "Goals", "echoer", "write goals")
    run("collab", "assign", str(doc), "Risks", "echoer", "write risks")
    out = run("collab", "run", str(doc))
    text = doc.read_text(encoding="utf-8")
    assert "wrote Goals" in text and "wrote Risks" in text, out + text
    assert "restored" in run("collab", "undo", str(doc))


@step("prompt run (ask)")
def _():
    out = run("ask", "echoer", "say hi", "-d", str(WORK))
    assert "fake agent ✓" in out, out
    assert "Echoer" in run("runs")


@step("mcp test / add / rm")
def _():
    line = f'"{PY}" "{fake_mcp}"' if os.name == "nt" else f"{PY} {fake_mcp}"
    out = run("mcp", "test", line, timeout=90)
    assert "works" in out and "ping" in out, out
    run("mcp", "add", line, "--name", "fake", "--to", "codex,gemini,opencode")
    toml = (HOME / ".codex/config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.fake]" in toml, toml
    gem = json.loads((HOME / ".gemini/settings.json").read_text(encoding="utf-8"))
    assert "fake" in gem["mcpServers"]
    assert "fake" in run("mcp")
    run("mcp", "rm", "fake", "--to", "codex,gemini,opencode")
    assert "[mcp_servers.fake]" not in (HOME / ".codex/config.toml").read_text(encoding="utf-8")


@step("alerts inbox")
def _():
    run("notify-test")
    assert "nyrasview test" in run("inbox")


@step("live feedback hook")
def _():
    run("say", "make it pop", "-a", "claude", "-d", str(WORK))
    payload = json.dumps({"cwd": str(WORK), "hook_event_name": "PostToolUse", "session_id": "s"})
    out = run("hook", input=payload)
    if out.strip():
        assert "make it pop" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


@step("handoff dry run from a Claude session log")
def _():
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(WORK))
    d = HOME / ".claude/projects" / slug
    d.mkdir(parents=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lines = [{"type": "user", "timestamp": now, "message": {"content": "add a login page"}},
             {"type": "assistant", "timestamp": now, "message": {"content": [
                 {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": str(WORK / "app.py")}}]}},
             {"type": "assistant", "timestamp": now, "isApiErrorMessage": True,
              "message": {"content": [{"type": "text", "text": "5-hour limit reached ∙ resets 3pm"}]}}]
    (d / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    out = run("handoff", "--from", "claude", "--to", "codex", "-n", "-d", str(WORK))
    h = (WORK / ".nyrasview/HANDOFF.md").read_text(encoding="utf-8")
    assert "add a login page" in h and "limit" in h.lower(), out


@step("gui server answers")
def _():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    p = subprocess.Popen([PY, "-m", "nyrasview", "gui", "--no-open", "-p", str(port)], env=ENV, cwd=str(WORK),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        snap = None
        for _ in range(60):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/snapshot", timeout=2) as r:
                    snap = json.loads(r.read())
                if snap.get("agents"):
                    break
            except Exception:
                continue
        assert snap and snap.get("agents"), "no snapshot"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            html = r.read().decode("utf-8")
        assert "nyrasview" in html and "__NYRA_TOKEN__" not in html
    finally:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


@step("terminal UI imports")
def _():
    r = subprocess.run([PY, "-c", "import nyrasview.tui"], capture_output=True, text=True, env=ENV)
    assert r.returncode == 0, r.stderr


print(f"\n{'ALL PASSED' if not FAILS else 'FAILED: ' + ', '.join(FAILS)} on {sys.platform}")
sys.exit(1 if FAILS else 0)
