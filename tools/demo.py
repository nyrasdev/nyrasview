import hashlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

D = Path(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "/tmp/nyrademo").resolve()
HOME = D / "home"
BIN = D / "bin"
RUNBIN = D / "runbin"
WEB = HOME / "code/acme-web"
API = HOME / "code/api-server"
NOTES = HOME / "notes"
PY = sys.executable
REPO = Path(__file__).resolve().parent.parent
ENV = {"HOME": str(HOME), "XDG_STATE_HOME": str(HOME / ".local/state"), "XDG_CONFIG_HOME": str(HOME / ".config"),
       "NYRA_SCOPE": str(HOME), "NYRA_HOSTNAME": "devbox", "PATH": f"{RUNBIN}{os.pathsep}{os.environ['PATH']}",
       "PYTHONPATH": str(REPO)}
random.seed(7)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def slug(p):
    return re.sub(r"[^A-Za-z0-9]", "-", str(p))


def w(path, text, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    if mode:
        path.chmod(mode)
    return path


def jl(path, items):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in items), encoding="utf-8")


SITE_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Acme</title><link rel="stylesheet" href="style.css"></head>
<body>
<header class="nav"><b class="logo">acme</b><nav><a href="#">Product</a><a href="#">Docs</a><a href="#">Pricing</a></nav><a class="btn ghost" href="#">Sign in</a></header>
<main>
  <section class="hero"><span class="kicker">New · v2 is here</span><h1 class="hero-title">Ship faster with Acme</h1><p class="sub">Deploy previews, edge functions and analytics in one place.</p></section>
  <section class="plans">
    <div class="pricing-card"><h3>Starter</h3><p class="price">$0<span>/mo</span></p><ul><li>1 project</li><li>Community support</li></ul><button class="btn ghost">Get started</button></div>
    <div class="pricing-card featured"><span class="tag">Most popular</span><h3>Pro plan</h3><p class="price">$29<span>/mo</span></p><ul><li>Unlimited projects</li><li>Preview deployments</li><li>Email support</li></ul><button class="cta">Start free trial</button></div>
    <div class="pricing-card"><h3>Team</h3><p class="price">$99<span>/mo</span></p><ul><li>SSO and audit log</li><li>Priority support</li></ul><button class="btn ghost">Contact sales</button></div>
  </section>
</main>
</body></html>
"""

SITE_CSS = """
*{box-sizing:border-box}
body{font-family:Inter,system-ui,sans-serif;margin:0;background:#0b0b10;color:#e8e8ee}
.nav{display:flex;align-items:center;gap:28px;padding:18px 40px;border-bottom:1px solid #1d1d26}
.nav nav{display:flex;gap:22px;flex:1}.nav a{color:#a3a3b2;text-decoration:none}
.logo{font-size:20px;letter-spacing:-.5px}
main{padding:56px 40px;max-width:1100px;margin:0 auto}
.hero{text-align:center;margin-bottom:48px}.kicker{font-size:13px;color:#a78bfa;background:#a78bfa1a;padding:5px 12px;border-radius:99px}
.hero-title{font-size:52px;letter-spacing:-1.5px;margin:18px 0 10px}.sub{color:#a3a3b2;font-size:18px}
.plans{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
.pricing-card{position:relative;background:#13131a;border:1px solid #23232e;border-radius:6px;padding:28px}
.pricing-card h3{margin:0 0 6px}.price{font-size:38px;font-weight:700;margin:6px 0 16px}.price span{font-size:15px;color:#8b8b99}
.pricing-card ul{padding:0;margin:0 0 22px;list-style:none;color:#b4b4c2;line-height:2}
.featured{border-color:#a78bfa;box-shadow:0 0 0 1px #a78bfa55,0 20px 60px #a78bfa22}
.tag{position:absolute;top:-11px;left:28px;background:#a78bfa;color:#0b0b10;font-size:12px;font-weight:700;padding:3px 10px;border-radius:99px}
.btn{display:inline-block;padding:10px 16px;border-radius:6px;color:#e8e8ee;text-decoration:none;border:1px solid #2e2e3a;background:transparent;font:inherit}
.cta{width:100%;background:#a78bfa;color:#0b0b10;border:0;padding:12px 16px;border-radius:6px;font:inherit;font-weight:700}
"""

AGENT = r'''
import json, os, random, select, socket, ssl, subprocess, sys, time
from datetime import datetime, timezone
kind, cwd, log, kids, hosts, actions = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4]), json.loads(sys.argv[5]), json.loads(sys.argv[6])
busy = float(sys.argv[7]) if len(sys.argv) > 7 else 0.04
os.chdir(cwd)
procs = [subprocess.Popen(k, cwd=cwd) for k in kids]
socks = {}
ctx = ssl.create_default_context()
def keep():
    for h, p in hosts:
        s = socks.get(h)
        if s is not None:
            try:
                r, _, _ = select.select([s], [], [], 0)
                if not r:
                    continue
                s.settimeout(0)
                if s.recv(4096):
                    continue
            except ssl.SSLWantReadError:
                continue
            except Exception:
                pass
            try:
                s.close()
            except OSError:
                pass
        try:
            socks[h] = ctx.wrap_socket(socket.create_connection((h, p), timeout=4), server_hostname=h)
        except OSError:
            socks.pop(h, None)
keep()
iso = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
i = 0
while True:
    if actions:
        a = actions[i % len(actions)]
        i += 1
        with open(log, "a") as f:
            if kind == "claude":
                tid = f"toolu_{int(time.time()*1000)}"
                f.write(json.dumps({"type": "assistant", "timestamp": iso(), "message": {"id": f"msg_{tid}", "model": "claude-opus-5-5", "content": [{"type": "tool_use", "id": tid, "name": a[0], "input": a[1]}], "usage": {"input_tokens": 1200, "output_tokens": random.randint(200, 900), "cache_read_input_tokens": 40000, "cache_creation_input_tokens": 800}}}) + "\n")
                f.write(json.dumps({"type": "user", "timestamp": iso(), "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}) + "\n")
            else:
                f.write(json.dumps({"timestamp": iso(), "type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": f"c{i}", "arguments": json.dumps({"command": ["bash", "-lc", a]})}}) + "\n")
                f.write(json.dumps({"timestamp": iso(), "type": "response_item", "payload": {"type": "function_call_output", "call_id": f"c{i}", "output": "ok"}}) + "\n")
    keep()
    end = time.time() + random.uniform(4, 7)
    while time.time() < end:
        t = time.time() + random.uniform(0, busy)
        while time.time() < t:
            pass
        time.sleep(random.uniform(0.15, 0.4))
'''

MCPCHILD = r'''
import random, sys, time
while True:
    t = time.time() + random.uniform(0.005, 0.03)
    while time.time() < t:
        pass
    time.sleep(random.uniform(0.3, 1.2))
'''

FAKE_CLAUDE = r'''#!PYTHON
import json, sys, time, uuid
args = sys.argv[1:]
if "-p" not in args and "--print" not in args:
    while True:
        time.sleep(60)
prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
sid = str(uuid.uuid4())
def out(o, d=0.0):
    print(json.dumps(o), flush=True)
    time.sleep(d)
out({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-opus-5-5"}, 0.6)
out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.62, "resetsAt": int(time.time()) + 7400}, "seven_day": {"utilization": 0.31, "resetsAt": int(time.time()) + 400000}}}})
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Grep", "input": {"pattern": "border-radius"}}]}}, 1.0)
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "style.css:11: border-radius:6px"}]}}, 0.4)
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "style.css"}}]}}, 1.0)
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "21 lines"}]}}, 0.5)
txt = ["The cards use a **6px radius** everywhere, which reads a bit sharp next to the pill-shaped tags.",
       "\n\nI'd go with:\n\n```css\n.pricing-card{border-radius:16px}\n.cta,.btn{border-radius:10px}\n```\n\n",
       "That keeps the buttons slightly tighter than the cards, so the hierarchy still reads. Want me to apply it?"]
for t in txt:
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}, 0.9)
out({"type": "result", "subtype": "success", "is_error": False, "result": "".join(txt), "total_cost_usd": 0.0412, "duration_ms": 6100, "usage": {"input_tokens": 3200, "output_tokens": 410, "cache_read_input_tokens": 18000, "cache_creation_input_tokens": 900}})
'''

FAKE_CODEX = r'''#!PYTHON
import json, sys, time
args = sys.argv[1:]
if "exec" not in args:
    while True:
        time.sleep(60)
sys.stdin.read()
def out(o, d=0.0):
    print(json.dumps(o), flush=True)
    time.sleep(d)
out({"type": "thread.started", "thread_id": "th_demo"}, 0.5)
out({"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "rg -n radius style.css"}}, 1.2)
out({"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "rg -n radius style.css", "aggregated_output": "11:.pricing-card{... border-radius:6px ...}", "exit_code": 0}}, 0.8)
out({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "Bump `.pricing-card` to `border-radius: 14px` and the buttons to `8px`. Anything above ~18px starts to look bubbly at this card size."}}, 0.3)
out({"type": "turn.completed", "usage": {"input_tokens": 5400, "cached_input_tokens": 3000, "output_tokens": 220}})
'''

FAKE_GEMINI = r'''#!PYTHON
import json, sys, time
while True:
    time.sleep(60)
'''

FAKE_LLM = r'''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _j(self, d):
        b = json.dumps(d).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        self._j({"data": [{"id": "qwen3-coder-30b", "state": "loaded"}, {"id": "gpt-oss-20b"}, {"id": "llama-3.3-8b"}]})
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self._j({"choices": [{"message": {"content": "Rounder corners (12-16px) usually read friendlier for pricing cards."}}]})
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''


def build():
    if D.exists():
        shutil.rmtree(D)
    for d in (HOME, BIN, RUNBIN):
        d.mkdir(parents=True)
    for name in ("claude", "codex", "gemini", "opencode", "npx", "uvx"):
        os.symlink(PY, BIN / name)
    w(D / "agent.py", AGENT)
    w(D / "mcpchild.py", MCPCHILD)
    w(D / "fakellm.py", FAKE_LLM)
    for name, src in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX), ("gemini", FAKE_GEMINI), ("opencode", FAKE_GEMINI)):
        w(RUNBIN / name, src.replace("PYTHON", PY), 0o755)

    w(WEB / "index.html", SITE_HTML)
    w(WEB / "style.css", SITE_CSS)
    w(WEB / "package.json", '{"name":"acme-web","scripts":{"dev":"vite"}}\n')
    w(API / "app/main.py", "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get('/health')\ndef health():\n    return {'ok': True}\n")
    w(API / "tests/test_health.py", "def test_health(client):\n    assert client.get('/health').json() == {'ok': True}\n")
    for repo in (WEB, API):
        subprocess.run(["git", "init", "-q"], cwd=repo)
        subprocess.run(["git", "-c", "user.name=demo", "-c", "user.email=demo@example.com", "add", "."], cwd=repo)
        subprocess.run(["git", "-c", "user.name=demo", "-c", "user.email=demo@example.com", "commit", "-qm", "init"], cwd=repo)
    css = (WEB / "style.css").read_text().replace(".pricing-card{position:relative;background:#13131a;border:1px solid #23232e;border-radius:6px;padding:28px}",
                                                   ".pricing-card{position:relative;background:#13131a;border:1px solid #23232e;border-radius:6px;padding:28px;transition:transform .2s}")
    (WEB / "style.css").write_text(css)

    now = time.time()
    claude_dir = HOME / ".claude/projects" / slug(WEB)
    items = [{"type": "ai-title", "aiTitle": "Pricing page redesign"},
             {"type": "user", "timestamp": iso(now - 600), "message": {"content": "make the pricing cards feel more premium, the Pro plan should stand out"}}]
    t = now - 560
    for name, inp in [("Read", {"file_path": str(WEB / "index.html")}), ("Read", {"file_path": str(WEB / "style.css")}),
                      ("Edit", {"file_path": str(WEB / "style.css"), "old_string": ".featured{border-color:#333}",
                                "new_string": ".featured{border-color:#a78bfa;box-shadow:0 0 0 1px #a78bfa55,0 20px 60px #a78bfa22}"}),
                      ("Edit", {"file_path": str(WEB / "index.html"), "old_string": "<div class=\"pricing-card\"><h3>Pro plan</h3>",
                                "new_string": '    <div class="pricing-card featured"><span class="tag">Most popular</span><h3>Pro plan</h3><p class="price">$29<span>/mo</span></p><ul><li>Unlimited projects</li><li>Preview deployments</li><li>Email support</li></ul><button class="cta">Start free trial</button></div>'})]:
        t += 40
        tid = f"toolu_{int(t)}"
        items.append({"type": "assistant", "timestamp": iso(t), "message": {"id": f"m{tid}", "model": "claude-opus-5-5", "content": [
            {"type": "text", "text": "Let me look at the current cards first."} if name == "Read" else {"type": "text", "text": "Giving the Pro card an accent border and a soft glow."},
            {"type": "tool_use", "id": tid, "name": name, "input": inp}], "usage": {"input_tokens": 900, "output_tokens": 640, "cache_read_input_tokens": 52000, "cache_creation_input_tokens": 2100}}})
        items.append({"type": "user", "timestamp": iso(t + 2), "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}})
    jl(claude_dir / "s-web.jsonl", items)

    api_dir = HOME / ".claude/projects" / slug(API)
    jl(api_dir / "s-api.jsonl", [{"type": "ai-title", "aiTitle": "Rate limiting middleware"},
                                 {"type": "user", "timestamp": iso(now - 900), "message": {"content": "add per-key rate limiting to the API"}},
                                 {"type": "assistant", "timestamp": iso(now - 860), "message": {"id": "ma1", "model": "claude-sonnet-5-5", "content": [
                                     {"type": "text", "text": "Added a token-bucket middleware in app/limits.py and wired it into main.py. Tests pass."}],
                                     "usage": {"input_tokens": 700, "output_tokens": 900, "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 1000}}}])
    os.utime(api_dir / "s-api.jsonl", (now - 240, now - 240))

    models = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
    hist = []
    for day in range(14):
        n = random.randint(4, 30) if day not in (5, 6) else random.randint(0, 4)
        for k in range(n):
            ts = now - (13 - day) * 86400 - random.uniform(0, 30000)
            hist.append({"type": "assistant", "timestamp": iso(ts), "message": {"id": f"h{day}-{k}", "model": random.choices(models, [5, 3, 1])[0],
                         "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": random.randint(300, 3000), "output_tokens": random.randint(300, 4000),
                         "cache_read_input_tokens": random.randint(20000, 160000), "cache_creation_input_tokens": random.randint(500, 9000)}}})
    old = HOME / ".claude/projects" / slug(HOME / "code/old-site")
    jl(old / "hist.jsonl", hist)
    os.utime(old / "hist.jsonl", (now - 86400 * 2, now - 86400 * 2))

    cx = HOME / ".codex/sessions/2026/11/06"
    ev = [{"timestamp": iso(now - 700), "type": "session_meta", "payload": {"cwd": str(API)}},
          {"timestamp": iso(now - 700), "type": "turn_context", "payload": {"model": "gpt-5-codex"}},
          {"timestamp": iso(now - 690), "type": "event_msg", "payload": {"type": "user_message", "message": "write tests for the rate limiter and fix anything flaky"}}]
    for day in range(14):
        for k in range(random.randint(2, 14)):
            ts = now - (13 - day) * 86400 - random.uniform(0, 30000)
            ev.append({"timestamp": iso(ts), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {
                "input_tokens": random.randint(20000, 90000), "cached_input_tokens": random.randint(10000, 60000), "output_tokens": random.randint(800, 6000)}}}})
    ev.append({"timestamp": iso(now - 30), "type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": {
        "primary": {"used_percent": 41.0, "window_minutes": 300, "resets_in_seconds": 9100},
        "secondary": {"used_percent": 18.0, "window_minutes": 10080, "resets_in_seconds": 420000}}}})
    jl(cx / "rollout-2026-11-06T20-00-00-demo.jsonl", ev)

    gh = hashlib.sha256(str(NOTES).encode()).hexdigest()
    msgs = [{"type": "user", "content": "summarize this week's meeting notes into action items", "timestamp": iso(now - 3000)}]
    for day in range(14):
        for k in range(random.randint(0, 6)):
            msgs.append({"type": "gemini", "model": "gemini-2.5-pro", "timestamp": iso(now - (13 - day) * 86400 - k * 400), "content": "done",
                         "tokens": {"input": random.randint(4000, 30000), "output": random.randint(300, 2500), "cached": random.randint(0, 8000), "thoughts": 200}})
    w(HOME / f".gemini/tmp/{gh}/chats/session-1.json", json.dumps({"messages": msgs}))

    oc = HOME / ".local/share/opencode/storage/message/ses_demo"
    for day in range(14):
        for k in range(random.randint(0, 5)):
            mid = f"msg_{day}_{k}"
            w(oc / f"{mid}.json", json.dumps({"id": mid, "role": "assistant", "providerID": random.choice(["lmstudio", "openrouter"]),
                                               "modelID": random.choice(["qwen3-coder-30b", "moonshotai/kimi-k2"]), "cost": round(random.uniform(0, 0.02), 4),
                                               "time": {"created": int((now - (13 - day) * 86400 - k * 900) * 1000)},
                                               "tokens": {"input": random.randint(2000, 20000), "output": random.randint(200, 2000), "reasoning": 0, "cache": {"read": 0, "write": 0}}}))

    w(HOME / ".claude.json", json.dumps({"mcpServers": {
        "playwright": {"type": "stdio", "command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
        "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
        "github": {"type": "http", "url": "https://api.githubcopilot.com/mcp/"}}}, indent=2))
    w(HOME / ".codex/config.toml", 'model = "gpt-5-codex"\n\n[mcp_servers.playwright]\ncommand = "npx"\nargs = ["-y", "@playwright/mcp@latest"]\n\n[mcp_servers.fetch]\ncommand = "uvx"\nargs = ["mcp-server-fetch"]\n')
    w(HOME / ".gemini/settings.json", json.dumps({"mcpServers": {"context7": {"httpUrl": "https://mcp.context7.com/mcp"}}}, indent=2))
    w(HOME / ".config/opencode/opencode.json", json.dumps({"$schema": "https://opencode.ai/config.json", "mcp": {
        "memory": {"type": "local", "command": ["npx", "-y", "@modelcontextprotocol/server-memory"], "enabled": True}}}, indent=2))

    for name, desc in [("frontend-design", "Build distinctive, production-grade UI instead of generic layouts"),
                       ("commit", "Write a conventional commit message from the staged diff"),
                       ("security-review", "Review the pending changes for injection, authz and secrets"),
                       ("pdf", "Read, fill and generate PDF files")]:
        w(HOME / f".claude/skills/{name}/SKILL.md", f"---\nname: {name}\ndescription: {desc}\n---\n")
    w(HOME / ".claude/commands/ship.md", "---\ndescription: Run tests, bump version and open a PR\n---\n")
    w(HOME / ".claude/agents/test-runner.md", "---\nname: test-runner\ndescription: Runs the suite and fixes failures\n---\n")
    w(HOME / ".claude/CLAUDE.md", "Prefer small PRs. Never commit secrets.\n")
    w(HOME / ".codex/prompts/refactor.md", "# Refactor the selected module for readability\n")
    w(HOME / ".codex/AGENTS.md", "Run pytest before finishing.\n")
    w(HOME / ".config/opencode/agent/docs.md", "---\ndescription: Writes and updates docs pages\n---\n")
    w(HOME / ".gemini/extensions/notes-tools/gemini-extension.json", json.dumps({"name": "notes-tools", "version": "1.2.0", "description": "Meeting notes helpers"}))
    w(HOME / ".claude/settings.json", json.dumps({"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "nyrasview hook"}]}]}}))

    w(NOTES / "standup.md", "# Standup\n")
    (NOTES / ".obsidian").mkdir(parents=True, exist_ok=True)

    os.environ.update(ENV)
    sys.path.insert(0, str(REPO))
    from nyrasview import collab, runs
    from nyrasview.util import STATE
    doc = collab.new_doc(str(NOTES / "launch-plan.md"), "Acme v2 launch plan", ["Goals", "Architecture", "Risks", "Timeline"])
    tasks = [("Goals", "claude", "Three measurable goals for the launch"),
             ("Architecture", "codex", "Services, data flow and what changes in v2"),
             ("Risks", "gemini", "Top risks with mitigations"),
             ("Timeline", "lmstudio", "A 4-week timeline with owners")]
    body = {"Goals": "1. **Activation:** 40% of new teams deploy a preview in their first session.\n2. **Speed:** p95 deploy time under 45 s.\n3. **Revenue:** 300 Pro upgrades in the first 30 days.",
            "Architecture": "- `edge-router` terminates TLS and routes preview hosts.\n- `builder` runs in isolated sandboxes, results go to object storage.\n- `analytics` ingests events through a queue, so the dashboard never blocks deploys.",
            "Risks": "| risk | mitigation |\n|---|---|\n| Builder queue backs up at launch | autoscale on queue depth, cap per-team concurrency |\n| Pricing page confuses free vs Pro | A/B test the comparison table |",
            "Timeline": ""}
    text = Path(doc).read_text()
    for sec, b in body.items():
        if b:
            text = text.replace(f"## {sec}\n\n", f"## {sec}\n\n{b}\n", 1)
    Path(doc).write_text(text)
    for sec, ag, ins in tasks:
        t = collab.assign(doc, sec, ag, ins)
        st = "running" if sec == "Timeline" else "done"
        collab._set(doc, t["id"], status=st, started=now - random.uniform(60, 400), finished=None if st == "running" else now - 30, chars=400)

    group = "demo01"
    for ag, kind, out in (("claude", "stream", "claude"), ("codex", "stream", "codex")):
        meta = {"id": f"run{ag[:3]}01", "agent": ag, "cwd": str(WEB), "mode": "ask", "model": None, "group": group,
                "prompt": "are the corners on the pricing cards too sharp?", "created": now - 400, "status": "done",
                "pid": None, "kind": kind, "finished": now - 380, "turns": [{"prompt": "are the corners on the pricing cards too sharp?"}]}
        runs._save(meta)
        fake = RUNBIN / out
        r = subprocess.run([str(fake), "-p", "exec"], input="x", capture_output=True, text=True, env=dict(os.environ))
        (runs.DIR / meta["id"] / "out.log").write_text(r.stdout)
        runs._finish(runs.load(meta["id"]))

    lim = STATE / "limits.json"
    lim.write_text(json.dumps({"claude": {"five_hour": {"utilization": 0.58, "resets": now + 7400, "status": "allowed", "seen": now - 300},
                                          "seven_day": {"utilization": 0.29, "resets": now + 400000, "status": "allowed", "seen": now - 300}}, "_scanned": now}))
    evs = [("approval", "claude", "Claude Code needs you · api-server", "Claude needs your permission to use Bash", 1500),
           ("finished", "codex", "Codex finished · api-server", "All 14 tests pass. Rate limiter covered.", 1100),
           ("done", "claude", "Claude Code answered", "are the corners on the pricing cards too sharp?", 380),
           ("limit_soon", "claude", "Claude Code is close to its 5-hour limit", "58% used · about 70 min left at this pace", 200)]
    with open(STATE / "events.jsonl", "w") as f:
        for i, (k, ag, ti, bo, ago) in enumerate(evs):
            f.write(json.dumps({"id": f"e{i}", "kind": k, "agent": ag, "title": ti, "body": bo, "at": now - ago,
                                "cwd": str(API if "api" in ti else WEB), "urgent": k == "approval"}) + "\n")
    fb = STATE / "feedback" / (slug(os.path.realpath(WEB)).strip("-") + ".jsonl")
    fb.parent.mkdir(parents=True, exist_ok=True)
    fb.write_text(json.dumps({"id": "f1", "agent": "claude", "text": "make the Pro card glow a little less", "target": {"selector": "div.pricing-card.featured", "file": "index.html"},
                              "message": "...", "at": now - 90, "status": "delivered"}) + "\n")


def start():
    env = dict(os.environ, **ENV)
    procs = []

    def spawn(argv, cwd):
        procs.append(subprocess.Popen([str(a) for a in argv], cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    pw = [str(BIN / "npx"), str(D / "mcpchild.py"), "-y", "@playwright/mcp@latest"]
    fetch = [str(BIN / "uvx"), str(D / "mcpchild.py"), "mcp-server-fetch"]
    mem = [str(BIN / "npx"), str(D / "mcpchild.py"), "-y", "@modelcontextprotocol/server-memory"]
    web_actions = [["Edit", {"file_path": str(WEB / "style.css"), "old_string": "x", "new_string": ".pricing-card{position:relative;background:#13131a;border:1px solid #23232e;border-radius:6px;padding:28px;transition:transform .2s}"}],
                   ["Edit", {"file_path": str(WEB / "index.html"), "old_string": "x", "new_string": '    <div class="pricing-card featured"><span class="tag">Most popular</span><h3>Pro plan</h3><p class="price">$29<span>/mo</span></p><ul><li>Unlimited projects</li><li>Preview deployments</li><li>Email support</li></ul><button class="cta">Start free trial</button></div>'}],
                   ["Bash", {"command": "npx playwright screenshot http://localhost:5173 /tmp/pricing.png"}]]
    spawn([BIN / "claude", D / "agent.py", "claude", WEB, HOME / ".claude/projects" / slug(WEB) / "s-web.jsonl", json.dumps([pw]),
           json.dumps([["api.anthropic.com", 443], ["statsig.anthropic.com", 443]]), json.dumps(web_actions)], WEB)
    spawn([BIN / "claude", D / "agent.py", "claude", API, HOME / ".claude/projects" / slug(API) / "s-api.jsonl", "[]",
           json.dumps([["api.anthropic.com", 443]]), "[]", "0"], API)
    spawn([BIN / "codex", D / "agent.py", "codex", API, HOME / ".codex/sessions/2026/11/06/rollout-2026-11-06T20-00-00-demo.jsonl",
           json.dumps([pw, fetch]), json.dumps([["api.openai.com", 443], ["chatgpt.com", 443]]),
           json.dumps(["pytest -q tests/test_limits.py", "rg -n 'bucket' app/", "python -m pytest -x"])], API)
    spawn([BIN / "gemini", D / "agent.py", "gemini", NOTES, D / "none.log", "[]", json.dumps([["generativelanguage.googleapis.com", 443]]), "[]", "0"], NOTES)
    spawn([BIN / "opencode", D / "agent.py", "opencode", API, D / "none.log", json.dumps([mem]), "[]", "[]"], API)
    spawn([PY, "-m", "http.server", "5173", "--bind", "127.0.0.1"], WEB)
    spawn([PY, D / "fakellm.py", "1234"], HOME)
    return procs


if __name__ == "__main__":
    build()
    procs = start()
    print("demo ready. run the GUI with:\n")
    print("  " + " ".join(f"{k}={v}" for k, v in ENV.items() if k != "PATH") + f" PATH={RUNBIN}:$PATH nyrasview gui\n")
    print("ctrl+c to stop the fake agents", flush=True)
    try:
        if "--seconds" in sys.argv:
            time.sleep(float(sys.argv[sys.argv.index("--seconds") + 1]))
        else:
            signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
