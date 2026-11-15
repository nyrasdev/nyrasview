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


@step("doctor")
def _():
    out = run("doctor", check=False)
    assert "nyrasview doctor" in out and "config file" in out, out


@step("status --json")
def _():
    d = json.loads(run("status", "--json"))
    assert isinstance(d["agents"], list) and d["system"]["mem_total"] > 0


@step("limits and usage")
def _():
    json.loads(run("limits", "--json") or "{}") if run("limits", "--json").strip().startswith("{") else None
    run("usage")


@step("limits for other agents: budget, usage left, at this rate")
def _():
    chats = HOME / ".gemini/tmp/proj/chats"
    chats.mkdir(parents=True, exist_ok=True)
    now = time.time()
    msgs = [{"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - i * 60)), "model": "gemini-2.5-pro",
             "tokens": {"input": 100, "output": 20}} for i in range(12)]
    (chats / "c.json").write_text(json.dumps({"messages": msgs}), encoding="utf-8")
    with open(cfg / "config.toml", "a", encoding="utf-8") as f:
        f.write('\n[limits]\nclaude_api = false\n\n[limits.budget.gemini]\ncap = 1000\nperiod = "week"\n')
    d = json.loads(run("limits", "--json"))
    w = d["limits"]["gemini"]["week"]
    assert w["used"] == 12 and w["cap"] == 1000 and w["left"] == 988 and w["unit"] == "requests", w
    assert w["burn_per_hour"] == 12 and w["pace"] is not None, w
    assert d["meta"]["gemini"]["plan"] == "your budget", d["meta"]
    out = run("limits")
    assert "988 req left" in out and "Gemini" in out, out


@step("checkpoints: undo one agent turn, keep the rest")
def _():
    repo = ROOT / "cprepo"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                  capture_output=True, check=True)
    (repo / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    g("init", "-q")
    g("add", "-A")
    g("commit", "-qm", "init")
    hook = lambda ev, **kw: run("hook", input=json.dumps({"hook_event_name": ev, "session_id": "cp1", "cwd": str(repo), **kw}))
    hook("UserPromptSubmit", prompt="make b loud")
    (repo / "f.txt").write_text("a\nB\nc\n", encoding="utf-8")
    (repo / "new.txt").write_text("hi\n", encoding="utf-8")
    hook("PostToolUse", tool_name="Edit", tool_input={"file_path": str(repo / "f.txt")})
    hook("PostToolUse", tool_name="Write", tool_input={"file_path": str(repo / "new.txt")})
    hook("Stop")
    hook("UserPromptSubmit", prompt="add d")
    (repo / "f.txt").write_text("a\nB\nc\nd\n", encoding="utf-8")
    hook("PostToolUse", tool_name="Edit", tool_input={"file_path": str(repo / "f.txt")})
    hook("Stop")
    d = json.loads(run("checkpoints", "-d", str(repo), "--json"))
    assert [t["prompt"] for t in d["turns"]] == ["add d", "make b loud"], d
    first = d["turns"][1]
    assert sorted(first["files"]) == ["f.txt", "new.txt"], first
    assert "+B" in run("checkpoints", "-d", str(repo), "--diff", first["id"])
    out = run("undo", first["id"], "-d", str(repo))
    assert (repo / "f.txt").read_text(encoding="utf-8").replace("\r\n", "\n") == "a\nb\nc\nd\n", out
    assert not (repo / "new.txt").exists(), out
    assert g("log", "--oneline").stdout.decode().count("\n") == 1
    d = json.loads(run("checkpoints", "-d", str(repo), "--json"))
    assert d["turns"][1]["undone"] is False and d["turns"][2]["undone"] is True, d
    (repo / "f.txt").write_text("zzz\n", encoding="utf-8")
    r = subprocess.run([PY, "-m", "nyrasview", "undo", d["turns"][1]["id"], "-d", str(repo)], capture_output=True,
                       text=True, env=ENV, cwd=str(WORK))
    assert r.returncode == 1 and "can't be undone cleanly" in r.stdout, r.stdout + r.stderr
    assert (repo / "f.txt").read_text(encoding="utf-8") == "zzz\n"
    run("restore", first["id"], "--before", "-d", str(repo))
    assert (repo / "f.txt").read_text(encoding="utf-8").replace("\r\n", "\n") == "a\nb\nc\n"


@step("terminal extras: settings, watch, completion, sandbox")
def _():
    assert "notify.soon_pct = 80" in run("settings", "set", "notify.soon_pct", "80")
    assert run("settings", "get", "notify.soon_pct").strip() == "80"
    assert "••••" in run("settings", "set", "fallback.openai_api_key", "sk-smoke-abcdef")
    r = subprocess.run([PY, "-m", "nyrasview", "settings", "set", "notify.desktop", "maybe"], capture_output=True,
                       text=True, env=ENV, cwd=str(WORK))
    assert r.returncode != 0 and "true or false" in r.stderr, r.stderr
    run("settings", "unset", "notify.soon_pct")
    assert "(default)" in run("settings", "get", "notify.soon_pct")
    assert "[notify]" in run("settings") and "desktop" in run("settings", "-v")
    run("settings", "unset", "fallback.openai_api_key")
    out = run("watch", "--once", "-a", "claude", "-d", str(WORK))
    assert "Claude Code" in out, out
    fish = run("completion", "fish")
    assert "-a watch" in fish and "-a hook " not in fish, fish[:500]
    assert "_nyrasview" in run("completion", "bash") and "bashcompinit" in run("completion", "zsh")
    r = subprocess.run([PY, "-m", "nyrasview", "sandbox", "test"], capture_output=True, text=True, env=ENV, cwd=str(WORK))
    assert r.returncode == 1 and "Set an image first" in r.stdout, r.stdout + r.stderr


@step("review queue: accept one part, take back another")
def _():
    repo = ROOT / "rvrepo"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                  capture_output=True, check=True)
    big = repo / "big.txt"
    big.write_text("".join(f"line {i}\n" for i in range(1, 41)), encoding="utf-8")
    g("init", "-q")
    g("add", "-A")
    g("commit", "-qm", "init")
    hook = lambda ev, **kw: run("hook", input=json.dumps({"hook_event_name": ev, "session_id": "rv1", "cwd": str(repo), **kw}))
    hook("UserPromptSubmit", prompt="shout two lines and add notes")
    big.write_text(big.read_text(encoding="utf-8").replace("line 3\n", "LINE THREE\n").replace("line 35\n", "LINE 35\n"), encoding="utf-8")
    (repo / "notes.txt").write_text("notes\n", encoding="utf-8")
    hook("PostToolUse", tool_name="Edit", tool_input={"file_path": str(big)})
    hook("PostToolUse", tool_name="Write", tool_input={"file_path": str(repo / "notes.txt")})
    hook("Stop")
    assert "3 part(s) to review" in run("review", "-l", "-d", str(repo))
    out = run("review", "-d", str(repo), input="a\nr\nr\n")
    text = big.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "LINE THREE\n" in text and "line 35\n" in text and "LINE 35" not in text, out + text
    assert not (repo / "notes.txt").exists(), out
    assert "nothing to review" in run("review", "-l", "-d", str(repo))
    turns = json.loads(run("checkpoints", "-d", str(repo), "--json"))["turns"]
    assert sum(1 for t in turns if t["agent"] == "you" and t["prompt"].startswith("reject")) == 2, turns


@step("search history: claude + codex logs, incremental, filters")
def _():
    proj = HOME / ".claude/projects/-work-shop"
    proj.mkdir(parents=True, exist_ok=True)
    now = time.time()
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    lines = [
        {"type": "user", "sessionId": "sess-1", "cwd": str(WORK), "timestamp": iso(now - 100),
         "message": {"role": "user", "content": "fix the flaky checkout payment test"}},
        {"type": "assistant", "sessionId": "sess-1", "cwd": str(WORK), "timestamp": iso(now - 90),
         "message": {"content": [{"type": "text", "text": "The payment test races the webhook."},
                                 {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest tests/test_payment.py"}}]}},
    ]
    f = proj / "sess-1.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    cx = HOME / ".codex/sessions/2026/11/07"
    cx.mkdir(parents=True, exist_ok=True)
    (cx / "rollout-2026-11-07T10-00-00-abc.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"timestamp": iso(now - 5000), "type": "session_meta", "payload": {"id": "codex-uuid-1", "cwd": str(WORK)}},
        {"timestamp": iso(now - 4990), "type": "event_msg", "payload": {"type": "user_message", "message": "add dark mode to the settings page"}},
        {"timestamp": iso(now - 4980), "type": "event_msg", "payload": {"type": "agent_message", "message": "Dark mode toggle added."}},
    ]) + "\n", encoding="utf-8")
    d = json.loads(run("find", "payment", "--json"))
    assert len(d) == 1 and d[0]["agent"] == "claude" and d[0]["session"] == "sess-1", d
    assert d[0]["resume"].endswith("claude --resume sess-1"), d
    assert any(h["role"] == "tool" and "pytest" in h["snippet"] for h in d[0]["hits"]) or "payment" in json.dumps(d)
    d = json.loads(run("find", "dark", "mode", "--json"))
    assert d and d[0]["agent"] == "codex" and d[0]["resume"].endswith("codex resume codex-uuid-1"), d
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "sessionId": "sess-1", "cwd": str(WORK), "timestamp": iso(now),
                             "message": {"role": "user", "content": "now migrate the invoices table"}}) + "\n")
    d = json.loads(run("find", "invoices", "--json"))
    assert d and d[0]["session"] == "sess-1", d
    assert json.loads(run("find", "payment", "-a", "codex", "--json")) == []
    assert json.loads(run("find", "dark", "--since", "1h", "--json")) == []
    out = run("find", "zzzznothing")
    assert "nothing found" in out, out


@step("insights: tokens per prompt and cost at API prices")
def _():
    proj = HOME / ".claude/projects/-work-insights"
    proj.mkdir(parents=True, exist_ok=True)
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
    (proj / "ins-1.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "sessionId": "ins-1", "cwd": str(WORK), "timestamp": iso, "message": {"role": "user", "content": "price check"}},
        {"type": "assistant", "sessionId": "ins-1", "cwd": str(WORK), "timestamp": iso, "requestId": "req-ins-1",
         "message": {"id": "msg-ins-1", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "ok"}],
                     "usage": {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 10000,
                               "cache_creation_input_tokens": 2000}}},
    ]) + "\n", encoding="utf-8")
    d = json.loads(run("insights", "--json"))
    c = d["agents"]["claude"]
    assert abs(c["api_cost"] - 0.026) < 1e-6, c
    assert c["tokens"] == 3500 and c["cache_read"] == 10000 and c["prompts"] >= 1, c
    assert len(d["days"]) == 30 and len(c["daily"]) == 30 and c["daily"][-1] == 3500, c["daily"][-3:]
    assert "at API prices" in run("insights")


@step("shared notes: one source, every agent file, two-way sync")
def _():
    repo = ROOT / "ntrepo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "CLAUDE.md").write_text("# Rules\n\n- use pnpm\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# Codex only\n\nrun make lint\n", encoding="utf-8")
    run("notes", "-d", str(repo), "init", "--from", "CLAUDE.md")
    a = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "- use pnpm" in a and "run make lint" in a and "nyrasview:shared-notes" in a, a
    (repo / "AGENTS.md").write_text(a.replace("- use pnpm", "- use pnpm\n- never edit generated/"), encoding="utf-8")
    out = run("notes", "-d", str(repo), "sync")
    assert "never edit generated/" in (repo / "CLAUDE.md").read_text(encoding="utf-8"), out
    assert "never edit generated/" in (repo / ".nyrasview/notes.md").read_text(encoding="utf-8")
    run("notes", "-d", str(repo), "add", "commit messages in English")
    assert "commit messages in English" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "in sync" in run("notes", "-d", str(repo))
    r = subprocess.run([PY, "-m", "nyrasview", "notes", "-d", str(HOME)], capture_output=True, text=True, env=ENV, cwd=str(WORK))
    assert r.returncode != 0 and "home folder" in r.stderr, r.stderr
    (HOME / ".claude").mkdir(exist_ok=True)
    (HOME / ".claude/CLAUDE.md").write_text("my own global rules\n", encoding="utf-8")
    run("notes", "-g", "add", "answer in English")
    g = (HOME / ".claude/CLAUDE.md").read_text(encoding="utf-8")
    assert "answer in English" in g and "my own global rules" in g, g


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
    p = subprocess.Popen([PY, "-m", "nyrasview", "gui", "--no-open", "-p", str(port)], env=dict(ENV, PYTHONUTF8="1"), cwd=str(WORK),
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


@step("queue: waits for the limit reset, start time, start now")
def _():
    st = _state_dir()
    st.mkdir(parents=True, exist_ok=True)
    (st / "queue" / ".alive").unlink(missing_ok=True)
    now = time.time()
    (st / "limits.json").write_text(json.dumps({"claude": {"five_hour": {"utilization": 1.0, "resets": now + 3600, "seen": now - 5}}}))
    out = run("queue", "add", "claude", "do it later", "-d", str(WORK))
    assert "queued as" in out and "5-hour limit is full" in out and "Nothing is checking the queue" in out, out
    d = json.loads(run("queue", "--json"))
    first = d["items"][0]
    assert first["status"] == "waiting" and first["until"] > now + 3000 and not d["alive"], d
    out = run("ask", "echoer", "second in line", "-d", str(WORK), "--later")
    assert "waits for the prompt before it" in out, out
    run("queue", "rm", first["id"])
    run("queue", "run", "--every", "1", timeout=90)
    done = [r for r in json.loads(run("status", "--json")).get("queue", [])]
    assert not done, done
    assert "second in line" in run("runs"), run("runs")
    out = run("queue", "add", "echoer", "not yet", "-d", str(WORK), "--at", "+2h")
    assert "starts at" in out, out
    qid = json.loads(run("queue", "--json"))["items"][0]["id"]
    assert "started as run" in run("queue", "now", qid)
    run("queue", "run", "--every", "1", timeout=90)
    assert not json.loads(run("queue", "--json"))["items"]
    (st / "limits.json").unlink()
    code = ("import json, time; from nyrasview import limits, runs; now = time.time();"
            "w = lambda u: {'claude': {'five_hour': {'utilization': u, 'resets': now + 7200, 'label': 'Session (5h)', 'calibrated': True}}};"
            "a = limits.detect_resets(w(0.97)); b = limits.detect_resets(w(0.02)); c = limits.detect_resets(w(0.03));"
            "print(json.dumps([len(a), len(b), len(c), runs.model_choices('claude')[:3], runs.model_choices('goose')]))")
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True, env=ENV, cwd=str(Path(__file__).resolve().parent.parent))
    assert json.loads(r.stdout) == [0, 1, 0, ["opus", "sonnet", "haiku"], []], r.stdout + r.stderr
    assert "limit was reset early" in run("inbox")


@step("deepseek: balance, burn rate, low-balance alert, prompt")
def _():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    wallet = {"v": "12.50"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer sk-deepseek-smoke-123456":
                return self._reply(401, {"error": {"message": "bad key"}})
            self._reply(200, {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": wallet["v"],
                                                                      "granted_balance": "0.00", "topped_up_balance": wallet["v"]}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            self._reply(200, {"choices": [{"message": {"content": "deepseek says hi to " + body["model"]}}]})

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with open(cfg / "config.toml", "a", encoding="utf-8") as f:
            f.write(f'\n[deepseek]\nbase_url = "http://127.0.0.1:{srv.server_address[1]}"\nlow_balance = 2\n')
        assert "deepseek" not in json.loads(run("limits", "--json"))["limits"]
        run("keys", "deepseek", "sk-wrong-key-000000000")
        assert "API key is not valid" in run("limits")
        run("keys", "deepseek", "sk-deepseek-smoke-123456")
        w = json.loads(run("limits", "--json"))["limits"]["deepseek"]["balance"]
        assert w["balance"] == 12.5 and w["unit"] == "usd" and not w["low"], w
        out = run("limits")
        assert "DeepSeek" in out and "$12.50 left" in out, out
        hist = _state_dir() / "deepseek_balance.json"
        hist.write_text(json.dumps([[time.time() - 7200, 14.5, "USD"]]))
        wallet["v"] = "1.50"
        w = json.loads(run("limits", "--json"))["limits"]["deepseek"]["balance"]
        assert w["low"] and 6.4 < w["burn_per_hour"] < 6.6 and 700 < w["eta"] < 900, w
        assert "DeepSeek balance is low" in run("inbox")
        out = run("ask", "deepseek", "hello", "-d", str(WORK), timeout=60)
        assert "deepseek says hi to deepseek-chat" in out, out
    finally:
        srv.shutdown()
        run("keys", "deepseek", "")


@step("codexbar: extra limits from the codexbar CLI, without overriding live ones")
def _():
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 7200))
    payload = {"providers": [
        {"provider": "copilot", "usage": {"primary": {"usedPercent": 40, "windowMinutes": 43200, "resetsAt": soon, "title": "Premium requests"},
                                          "loginMethod": "pro"}},
        {"provider": "kilo", "usage": {"primary": {"usedPercent": 99, "windowMinutes": 300, "resetsAt": soon}}, "credits": {"remaining": 12.5}},
        {"provider": "cursor", "error": {"message": "not signed in"}},
        {"provider": "gemini", "usage": {"primary": {"usedPercent": 10, "windowMinutes": 1440, "resetsAt": soon}}}]}
    fake = ROOT / "codexbar_fake.py"
    fake.write_text("import sys\nassert sys.argv[1:] == ['usage', '--format', 'json', '--json-only'], sys.argv\nprint(" + repr(json.dumps(payload)) + ")\n", encoding="utf-8")
    if os.name == "nt":
        exe = ROOT / "codexbar_fake.cmd"
        exe.write_text(f'@"{PY}" "{fake}" %*\r\n', encoding="utf-8")
    else:
        exe = ROOT / "codexbar_fake"
        exe.write_text(f"#!{PY}\n" + fake.read_text(encoding="utf-8"), encoding="utf-8")
        exe.chmod(0o755)
    assert "copilot" not in json.loads(run("limits", "--json"))["limits"]
    run("settings", "set", "codexbar.path", str(exe))
    try:
        d = json.loads(run("limits", "--json"))
        lim, meta = d["limits"], d["meta"]
        w = lim["copilot"]["43200m"]
        assert w["utilization"] == 0.4 and w["label"] == "Premium requests" and w["resets"] > time.time(), w
        assert meta["copilot"]["source"] == "codexbar" and meta["copilot"]["plan"] == "pro", meta
        assert lim["kilo"]["five_hour"]["utilization"] == 0.99 and lim["kilo"]["credits"]["balance"] == 12.5, lim["kilo"]
        assert meta["cursor"]["error"] == "not signed in", meta
        assert lim["gemini"]["day"]["source"] == "codexbar", lim["gemini"]
        out = run("limits")
        assert "Premium requests" in out and "Kilo" in out and "12.5 credits left" in out and "not signed in" in out, out
        out = run("queue", "add", "copilot", "wait for kilo", "-d", str(WORK), check=False)
        run("settings", "set", "codexbar.enabled", "false")
        assert "copilot" not in json.loads(run("limits", "--json"))["limits"]
    finally:
        run("settings", "unset", "codexbar.path")
        run("settings", "unset", "codexbar.enabled")
        for q in json.loads(run("queue", "--json"))["items"]:
            run("queue", "rm", q["id"])
        (_state_dir() / "codexbar.json").unlink(missing_ok=True)


@step("more agents: Devin, Hermes, OpenHands, Agent Zero")
def _():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    out = run("agents")
    for name in ("Devin", "Hermes Agent", "OpenHands", "Agent Zero"):
        assert name in out, out
    lines = [{"type": "system", "subtype": "init", "model": "hermes-4", "session_id": "s1"},
             {"type": "tool_use", "id": "t1", "name": "terminal", "input": {"command": "ls"}},
             {"type": "tool_result", "id": "t1", "output": "README.md"},
             {"type": "text", "text": "hermes did it"},
             {"type": "result", "session_id": "s1", "exit_code": 0, "text": "hermes did it", "tokens": {"input": 10, "output": 5, "total": 15}, "duration_ms": 1200}]
    fake = ROOT / "hermes_fake.py"
    fake.write_text("import sys\nassert sys.argv[1:5] == ['chat', '--format', 'stream-json', '--yolo'] and sys.argv[5] == '-q', sys.argv\n"
                    + "".join(f"print({json.dumps(l)!r})\n" for l in lines), encoding="utf-8")
    hb = ROOT / "hermesbin"
    hb.mkdir(exist_ok=True)
    if os.name == "nt":
        (hb / "hermes.cmd").write_text(f'@"{PY}" "{fake}" %*\r\n', encoding="utf-8")
    else:
        (hb / "hermes").write_text(f"#!{PY}\n" + fake.read_text(encoding="utf-8"), encoding="utf-8")
        (hb / "hermes").chmod(0o755)
    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            seen.update(path=self.path, key=self.headers.get("X-API-KEY"), body=b)
            data = json.dumps({"context_id": "ctx1", "response": "agent zero heard: " + b["message"]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    old = ENV["PATH"]
    ENV["PATH"] = str(hb) + os.pathsep + old
    try:
        out = run("ask", "hermes", "do the thing", "-d", str(WORK), "--mode", "auto", timeout=60)
        assert "hermes did it" in out and "terminal" in out and "15 tok" in out, out
        run("settings", "set", "agentzero.url", f"http://127.0.0.1:{srv.server_address[1]}")
        run("settings", "set", "agentzero.api_key", "a0-smoke-key")
        out = run("ask", "agentzero", "ping", "-d", str(WORK), timeout=60)
        assert "agent zero heard: ping" in out and seen["path"] == "/api_message" and seen["key"] == "a0-smoke-key", (out, seen)
    finally:
        ENV["PATH"] = old
        srv.shutdown()
        run("settings", "unset", "agentzero.url")
        run("settings", "unset", "agentzero.api_key")


@step("ollama: list, download with progress, load, unload, delete")
def _():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    have, loaded = {"tiny:1b"}, set()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code, body):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

        def do_GET(self):
            if self.path == "/api/tags":
                return self._reply(200, {"models": [{"name": m, "size": 1000, "details": {"parameter_size": "1B"}} for m in sorted(have)]})
            if self.path == "/api/ps":
                return self._reply(200, {"models": [{"name": m, "size": 1000, "size_vram": 800, "context_length": 4096} for m in sorted(loaded)]})
            self._reply(404, {})

        def do_POST(self):
            b = self._body()
            if self.path == "/api/pull":
                if b["model"] == "nope":
                    return self._reply(200, json.dumps({"error": "pull model manifest: file does not exist"}).encode() + b"\n")
                have.add(b["model"])
                lines = [{"status": "pulling manifest"}, {"status": "pulling abc", "total": 2000, "completed": 1000}, {"status": "success"}]
                return self._reply(200, "".join(json.dumps(x) + "\n" for x in lines).encode())
            if self.path == "/api/generate":
                if b["model"] not in have:
                    return self._reply(404, {"error": f"model '{b['model']}' not found"})
                loaded.discard(b["model"]) if b.get("keep_alive") == 0 else loaded.add(b["model"])
                return self._reply(200, {"done": True})
            self._reply(404, {})

        def do_DELETE(self):
            b = self._body()
            if b["model"] not in have:
                return self._reply(404, {"error": f"model '{b['model']}' not found"})
            have.discard(b["model"])
            self._reply(200, b"")

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ENV["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    try:
        out = run("local", "pull", "new:2b")
        assert "downloaded new:2b" in out and "pulling manifest" in out, out
        assert "new:2b" in run("local")
        bad = run("local", "pull", "nope", check=False)
        assert "file does not exist" in bad, bad
        assert "is loaded" in run("local", "load", "new:2b")
        assert "vram" in run("local"), run("local")
        assert "unloaded" in run("local", "unload", "new:2b") and not loaded
        assert "deleted new:2b" in run("local", "rm", "new:2b") and "new:2b" not in have
        assert "not found" in run("local", "rm", "new:2b", check=False)
        assert "only Ollama" in run("local", "rm", "x", "-s", "lmstudio", check=False)
    finally:
        srv.shutdown()
        ENV.pop("OLLAMA_HOST", None)


@step("gui api: settings, keys, hand-off options, phone login")
def _():
    import http.client
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    p = subprocess.Popen([PY, "-m", "nyrasview", "gui", "--no-open", "-p", str(port)], env=dict(ENV, PYTHONUTF8="1"), cwd=str(WORK),
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
        st, _, data = api("POST", "/api/queue/add", {"agents": ["echoer"], "prompt": "queued from the gui", "cwd": str(WORK), "at": "+3h"})
        made = json.loads(data)["items"]
        assert st == 200 and made[0]["status"] == "waiting" and "starts at" in made[0]["why"], data
        st, _, data = api("GET", "/api/queue")
        assert any(q["id"] == made[0]["id"] for q in json.loads(data)["items"]), data
        st, _, data = api("POST", "/api/queue/remove", {"id": made[0]["id"]})
        assert st == 200 and not json.loads(api("GET", "/api/queue")[2])["items"], data
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
