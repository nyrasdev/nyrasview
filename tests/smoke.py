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
import urllib.parse
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
    assert "fake" in gem["mcpServers"], gem
    listing = run("mcp")
    assert "fake" in listing, listing
    out = run("mcp", "rm", "fake", "--to", "codex,gemini,opencode")
    toml = (HOME / ".codex/config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.fake]" not in toml, out + "\n" + toml


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


BIN = ROOT / "bin"
BIN.mkdir()
fixer = ROOT / "fixer.py"
fixer.write_text(textwrap.dedent("""
    import sys
    p = sys.stdin.read()
    if "fails in" in p:
        src = open("calc.py", encoding="utf-8").read()
        open("calc.py", "w", encoding="utf-8").write(src.replace("a - b", "a + b"))
        print("Fixed add: it subtracted instead of adding.")
    else:
        print("nothing to do")
"""), encoding="utf-8")
planbot = ROOT / "planbot.py"
planbot.write_text(textwrap.dedent("""
    import sys
    sys.stdin.read()
    print('Here you go: {"title": "Smoke plan", "sections": [{"title": "Goals", "agent": "echoer", "instruction": "Write goals."}, '
          '{"title": "Risks", "agent": "nobody-like-this", "instruction": "Write risks."}], "review": {"agent": "echoer", "instruction": "Tidy up."}}')
"""), encoding="utf-8")
fake_claude = ROOT / "claude_fake.py"
fake_claude.write_text(textwrap.dedent("""
    import json, os, sys
    sys.stdin.read()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "Claude AI usage limit reached|1794100000"}))
        sys.exit(1)
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "continued on the key"}]}}))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok", "total_cost_usd": 0.01, "duration_ms": 10, "usage": {}}))
"""), encoding="utf-8")
if os.name == "nt":
    (BIN / "claude.cmd").write_text(f'@"{PY}" "{fake_claude}" %*\r\n', encoding="utf-8")
else:
    (BIN / "claude").write_text(f"#!{PY}\n" + fake_claude.read_text(encoding="utf-8"), encoding="utf-8")
    (BIN / "claude").chmod(0o755)
ENV["PATH"] = str(BIN) + os.pathsep + ENV.get("PATH", "")
with open(cfg / "config.toml", "a", encoding="utf-8") as f:
    f.write("\n[agents.fixer]\nname = \"Fixer\"\nheadless = { cmd = [" + json.dumps(PY) + ", " + json.dumps(str(fixer)) + "], stdin = true }\n")
    f.write("\n[agents.planbot]\nname = \"Planbot\"\nheadless = { cmd = [" + json.dumps(PY) + ", " + json.dumps(str(planbot)) + "], stdin = true }\n")
HEAL = ROOT / "healproj"
HEAL.mkdir()
(HEAL / "check.py").write_text("from calc import add\nimport sys\nsys.exit(0 if add(2, 3) == 5 else 1)\n", encoding="utf-8")
CHECK = f'"{PY}" check.py'


@step("self-heal: an agent fixes a failing check")
def _():
    (HEAL / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    out = run("heal", CHECK, "-d", str(HEAL), "-a", "fixer", "-n", "2", timeout=180)
    assert "passes after 1 fix" in out, out
    assert "a + b" in (HEAL / "calc.py").read_text(encoding="utf-8")


@step("self-heal: live Claude session is sent back to fix")
def _():
    (HEAL / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    run("heal", "--live", CHECK, "-d", str(HEAL))
    payload = json.dumps({"session_id": "smoke", "cwd": str(HEAL), "hook_event_name": "Stop"})
    out = run("hook", input=payload)
    d = json.loads(out)
    assert d["decision"] == "block" and "self-heal" in d["reason"], out
    (HEAL / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    assert run("hook", input=payload).strip() == "", "should let Claude stop once the check passes"
    run("heal", "--live", "--off", "-d", str(HEAL))


@step("skills: add from a folder, create, copy, remove")
def _():
    src = ROOT / "skillsrc" / "smoke-skill"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: smoke-skill\ndescription: Used by the smoke test\n---\nDo the thing.\n", encoding="utf-8")
    run("skill", "add", str(src.parent), "--to", "claude,codex")
    assert (HOME / ".claude/skills/smoke-skill/SKILL.md").exists()
    run("skill", "new", "made-here", "-d", "Created from stdin", "--to", "claude", input="Some instructions.\n")
    assert "Some instructions." in (HOME / ".claude/skills/made-here/SKILL.md").read_text(encoding="utf-8")
    run("skill", "copy", "made-here", "--from", "claude", "--to", "opencode")
    assert (HOME / ".config/opencode/skill/made-here/SKILL.md").exists()
    listing = run("skill", "list")
    assert "smoke-skill" in listing and "made-here" in listing, listing
    run("skill", "rm", "smoke-skill")
    assert not (HOME / ".claude/skills/smoke-skill").exists()


@step("API key fallback: a limit-hit run retries on the key")
def _():
    run("keys", "anthropic", "sk-ant-smoke-1234567890")
    assert "connected" in run("keys")
    out = run("ask", "claude", "keep going", "-d", str(WORK), timeout=120)
    assert "retrying on your API key" in out and "continued on the key" in out, out


@step("planner: splits a goal into agent tasks")
def _():
    doc = WORK / "smoke-plan.md"
    out = run("plan", "build something small", "--planner", "planbot", "-y", "-o", str(doc), timeout=120)
    text = doc.read_text(encoding="utf-8")
    assert "## Goals" in text and "## Risks" in text, out + text
    shown = run("collab", "show", str(doc))
    assert "Echoer" in shown and "whole document" in shown, shown


def _state_dir():
    if os.name == "nt":
        return HOME / "AppData/Local/nyrasview"
    if sys.platform == "darwin":
        return HOME / "Library/Application Support/nyrasview"
    return HOME / ".local/state/nyrasview"


@step("gui api: settings, keys, hand-off options, phone login")
def _():
    import http.client
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    p = subprocess.Popen([PY, "-m", "nyrasview", "gui", "--no-open", "-p", str(port)], env=ENV, cwd=str(WORK),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        html = None
        for _ in range(60):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
                    html = r.read().decode("utf-8")
                break
            except Exception:
                continue
        assert html, "gui did not start"
        token = re.search(r'TOKEN="([^"]+)"', html).group(1)

        def api(method, path, body=None, host=None, headers=None):
            t0 = time.time()
            try:
                c = http.client.HTTPConnection("127.0.0.1", host or port, timeout=60)
                h = {"Content-Type": "application/json", "X-Nyra-Token": token, **(headers or {})}
                c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
                r = c.getresponse()
                data = r.read()
            except Exception as e:
                raise AssertionError(f"{method} {path} on port {host or port} failed after {time.time() - t0:.1f}s: {e}")
            return r.status, dict(r.getheaders()), data

        for _ in range(120):
            st, _, data = api("GET", "/api/snapshot")
            if st == 200 and json.loads(data).get("agents"):
                break
            time.sleep(0.5)

        st, _, data = api("POST", "/api/settings", {"values": {"notify": {"soon_pct": "77"}}})
        assert st == 200, data
        st, _, data = api("GET", "/api/settings")
        v = json.loads(data)
        assert "soon_pct = 77" in v["raw"] and "agents.echoer" in v["raw"], v["raw"]
        st, _, data = api("POST", "/api/keys", {"provider": "openai", "key": "sk-openai-smoke"})
        assert any(k["provider"] == "openai" and k["connected"] for k in json.loads(data)["status"]), data
        st, _, data = api("GET", f"/api/handoff/options?agent=claude&cwd={urllib.parse.quote(str(WORK))}")
        opts = json.loads(data)
        assert any(t["key"] == "codex" and t["ok"] for t in opts["targets"]), opts
        st, _, data = api("POST", "/api/phone", {"on": True})
        info = json.loads(data)
        assert info["on"] and info["port"], info
        key = (_state_dir() / "phone_key").read_text().strip()
        st, _, _ = api("GET", "/api/snapshot", host=info["port"], headers={"X-Nyra-Token": ""})
        assert st == 401, f"phone without key should be refused, got {st}"
        st, hd, _ = api("GET", f"/?key={key}", host=info["port"])
        assert st == 302 and "nyra_key=" in hd.get("Set-Cookie", ""), (st, hd)
        st, _, data = api("GET", "/api/snapshot", host=info["port"], headers={"Cookie": f"nyra_key={key}"})
        assert st == 200 and "agents" in json.loads(data), (st, data[:200])
        api("POST", "/api/phone", {"on": False})
    finally:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


print(f"\n{'ALL PASSED' if not FAILS else 'FAILED: ' + ', '.join(FAILS)} on {sys.platform}")
sys.exit(1 if FAILS else 0)
