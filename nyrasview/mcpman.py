import json
import os
import re
import select
import shlex
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from . import plat
from .util import HOME, STATE, load_json, load_toml, strip_jsonc

PRESETS = [
    {"name": "playwright", "line": "npx -y @playwright/mcp@latest", "about": "drive a real browser"},
    {"name": "context7", "line": "npx -y @upstash/context7-mcp", "about": "up-to-date library docs"},
    {"name": "fetch", "line": "uvx mcp-server-fetch", "about": "fetch web pages as markdown"},
    {"name": "memory", "line": "npx -y @modelcontextprotocol/server-memory", "about": "persistent knowledge graph"},
    {"name": "sequential-thinking", "line": "npx -y @modelcontextprotocol/server-sequential-thinking", "about": "step-by-step reasoning"},
    {"name": "filesystem", "line": "npx -y @modelcontextprotocol/server-filesystem ~/", "about": "read/write files in a folder"},
    {"name": "git", "line": "uvx mcp-server-git", "about": "git history and diffs"},
    {"name": "time", "line": "uvx mcp-server-time", "about": "time and timezones"},
]


def _std(spec, url_key="url"):
    if spec["transport"] == "stdio":
        d = {"command": spec["command"], "args": spec.get("args") or []}
        if spec.get("env"):
            d["env"] = spec["env"]
        return d
    d = {url_key: spec["url"]}
    if spec.get("headers"):
        d["headers"] = spec["headers"]
    return d


def _gemini(spec):
    if spec["transport"] == "stdio":
        return _std(spec)
    return _std(spec, "httpUrl" if spec["transport"] == "http" else "url")


def _copilot(spec):
    d = _std(spec)
    d["type"] = "local" if spec["transport"] == "stdio" else spec["transport"]
    d["tools"] = ["*"]
    return d


def _opencode(spec):
    if spec["transport"] == "stdio":
        d = {"type": "local", "command": [spec["command"], *(spec.get("args") or [])], "enabled": True}
        if spec.get("env"):
            d["environment"] = spec["env"]
        return d
    d = {"type": "remote", "url": spec["url"], "enabled": True}
    if spec.get("headers"):
        d["headers"] = spec["headers"]
    return d


def _crush(spec):
    d = _std(spec)
    d["type"] = "stdio" if spec["transport"] == "stdio" else spec["transport"]
    return d


def _kimi(spec):
    d = _std(spec)
    if spec["transport"] != "stdio":
        d["transport"] = spec["transport"]
    return d


JSON_TARGETS = {
    "gemini": ("~/.gemini/settings.json", "mcpServers", _gemini),
    "qwen": ("~/.qwen/settings.json", "mcpServers", _gemini),
    "cursor": ("~/.cursor/mcp.json", "mcpServers", _std),
    "kimi": ("~/.kimi/mcp.json", "mcpServers", _kimi),
    "antigravity": ("~/.gemini/antigravity/mcp_config.json", "mcpServers", lambda s: _std(s, "serverUrl")),
    "windsurf": ("~/.codeium/windsurf/mcp_config.json", "mcpServers", lambda s: _std(s, "serverUrl")),
    "copilot": ("~/.copilot/mcp-config.json", "mcpServers", _copilot),
    "droid": ("~/.factory/mcp.json", "mcpServers", lambda s: dict(_std(s), type="stdio" if s["transport"] == "stdio" else "http")),
    "crush": ("~/.config/crush/crush.json", "mcp", _crush),
    "amp": ("~/.config/amp/settings.json", "amp.mcpServers", _std),
}


def _opencode_path():
    base = HOME / ".config/opencode"
    for fn in ("opencode.json", "opencode.jsonc"):
        if (base / fn).exists():
            return base / fn
    return base / "opencode.json"


def targets():
    from .registry import BY_KEY
    out = []
    for k in ["claude", "codex", "opencode", *JSON_TARGETS]:
        a = BY_KEY.get(k)
        if not a:
            continue
        out.append({"key": k, "name": a.name, "installed": a.installed()})
    return out


def _backup(path):
    path = Path(path)
    if not path.exists():
        return
    d = STATE / "backups" / time.strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, d / (str(path).replace(str(HOME), "").strip("/").replace("/", "__")))


def _read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    text = path.read_text()
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return json.loads(strip_jsonc(text))


def _write_json(path, data):
    path = Path(path)
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".nyra.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def _toml_key(k):
    return k if re.match(r"^[A-Za-z0-9_-]+$", k) else json.dumps(k)


def _toml_val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_val(x)}" for k, x in v.items()) + " }"
    return json.dumps(str(v))


def _codex_remove_block(text, name):
    head = f"mcp_servers.{_toml_key(name)}"
    out, skip = [], False
    for line in text.split("\n"):
        m = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if m:
            key = m.group(1).strip()
            skip = key == head or key.startswith(head + ".")
        if not skip:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"


def _codex_set(name, spec):
    path = HOME / ".codex/config.toml"
    text = path.read_text() if path.exists() else ""
    text = _codex_remove_block(text, name)
    if spec is not None:
        lines = ["", f"[mcp_servers.{_toml_key(name)}]"]
        if spec["transport"] == "stdio":
            lines += [f"command = {_toml_val(spec['command'])}", f"args = {_toml_val(spec.get('args') or [])}"]
            if spec.get("env"):
                lines.append(f"env = {_toml_val(spec['env'])}")
        else:
            lines.append(f"url = {_toml_val(spec['url'])}")
            if spec.get("headers"):
                lines.append(f"http_headers = {_toml_val(spec['headers'])}")
        text = text.rstrip() + "\n" + "\n".join(lines) + "\n"
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.lstrip("\n"))


def _claude_set(name, spec):
    if not shutil.which("claude"):
        raise RuntimeError("claude CLI not found")
    _backup(HOME / ".claude.json")
    subprocess.run(plat.resolve(["claude", "mcp", "remove", name, "-s", "user"]), capture_output=True, text=True, timeout=30)
    if spec is None:
        return
    d = _std(spec)
    d["type"] = spec["transport"]
    r = subprocess.run(plat.resolve(["claude", "mcp", "add-json", name, json.dumps(d), "-s", "user"]),
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])


def _json_set(agent, name, spec):
    if agent == "opencode":
        path, key, shape = _opencode_path(), "mcp", _opencode
    else:
        p, key, shape = JSON_TARGETS[agent]
        path = Path(os.path.expanduser(p))
    data = _read_json(path)
    block = data.get(key)
    if not isinstance(block, dict):
        block = {}
    if spec is None:
        block.pop(name, None)
    else:
        block[name] = shape(spec)
    data[key] = block
    if agent == "opencode" and "$schema" not in data:
        data = {"$schema": "https://opencode.ai/config.json", **data}
    _write_json(path, data)


def set_server(agent, name, spec):
    if agent == "claude":
        _claude_set(name, spec)
    elif agent == "codex":
        _codex_set(name, spec)
    elif agent == "opencode" or agent in JSON_TARGETS:
        _json_set(agent, name, spec)
    else:
        raise ValueError(f"nyrasview can't write MCP config for {agent}")


def full_spec(agent, name):
    from .mcp import configured
    for s in configured():
        if s["agent"] == agent and s["name"] == name:
            raw = _raw(agent, name, s) or {}
            env = raw.get("env") or raw.get("environment") or {}
            headers = raw.get("headers") or raw.get("http_headers") or {}
            return {"transport": s["transport"], "command": s["command"], "args": s["args"], "url": s["url"],
                    "env": env if isinstance(env, dict) else {}, "headers": headers if isinstance(headers, dict) else {}}
    return None


def _raw(agent, name, s):
    src = s.get("source") or ""
    if src.endswith(".toml"):
        t = load_toml(src) or {}
        return (t.get("mcp_servers") or {}).get(name)
    d = load_json(src) or {}
    if agent == "claude":
        scope = s.get("scope", "")
        if scope.startswith("local:"):
            return ((d.get("projects") or {}).get(scope[6:]) or {}).get("mcpServers", {}).get(name)
        return (d.get("mcpServers") or {}).get(name)
    for key in ("mcpServers", "mcp", "amp.mcpServers"):
        b = d.get(key)
        if isinstance(b, dict) and name in b:
            return b[name]
    return None


def parse(text, name=None):
    text = (text or "").strip()
    if not text:
        raise ValueError("paste a command, a URL or a JSON snippet")
    if text.startswith("{"):
        d = json.loads(text)
        if "mcpServers" in d or "mcp" in d:
            d = d.get("mcpServers") or d.get("mcp")
        if "command" not in d and "url" not in d and "httpUrl" not in d and len(d) >= 1 and all(isinstance(v, dict) for v in d.values()):
            name, d = next(iter(d.items())) if name is None else (name, d.get(name) or next(iter(d.values())))
        cmd = d.get("command")
        args = list(d.get("args") or [])
        if isinstance(cmd, list):
            cmd, args = cmd[0], cmd[1:] + args
        url = d.get("url") or d.get("httpUrl") or d.get("serverUrl")
        tr = (d.get("type") or d.get("transport") or "").lower()
        spec = {"transport": "stdio" if cmd else ("sse" if tr == "sse" or str(url).endswith("/sse") else "http"),
                "command": cmd, "args": [str(a) for a in args], "url": url,
                "env": d.get("env") or d.get("environment") or {}, "headers": d.get("headers") or {}}
    elif re.match(r"^https?://", text):
        spec = {"transport": "sse" if text.rstrip("/").endswith("/sse") else "http", "command": None, "args": [],
                "url": text.split()[0], "env": {}, "headers": {}}
    else:
        parts = plat.split(text)
        env = {}
        while parts and re.match(r"^[A-Z_][A-Z0-9_]*=", parts[0]):
            k, v = parts.pop(0).split("=", 1)
            env[k] = v
        if not parts:
            raise ValueError("no command found")
        parts = [os.path.expanduser(p) if p.startswith("~") else p for p in parts]
        spec = {"transport": "stdio", "command": parts[0], "args": parts[1:], "url": None, "env": env, "headers": {}}
    if not name:
        src = spec["url"] or " ".join(spec["args"]) or spec["command"] or "server"
        m = re.findall(r"(?:@[\w.-]+/)?([\w.-]+?)(?:-mcp|-mcp-server|mcp-server-|server-)?(?:@[\w.^~-]+)?(?:\s|$|/)", src)
        name = next((re.sub(r"^(mcp-server-|server-)|(-mcp|-mcp-server)$", "", x) for x in reversed(m)
                     if x not in ("-y", "npx", "uvx", "https:", "http:", "mcp", "latest", "sse") and len(x) > 1), "server")
    return re.sub(r"[^\w.-]", "-", name).strip("-") or "server", spec


def probe(spec, timeout=45):
    t0 = time.time()
    if spec["transport"] != "stdio":
        return _probe_http(spec, t0)
    env = dict(os.environ, **{k: str(v) for k, v in (spec.get("env") or {}).items()})
    try:
        p = subprocess.Popen(plat.resolve([spec["command"], *spec.get("args", [])]), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, **plat.detached())
    except OSError as e:
        return {"ok": False, "error": str(e)}
    import queue
    import threading
    q = queue.Queue()

    def pump():
        for raw in iter(p.stdout.readline, b""):
            q.put(raw)
        q.put(None)
    threading.Thread(target=pump, daemon=True).start()

    def send(o):
        p.stdin.write((json.dumps(o) + "\n").encode())
        p.stdin.flush()

    def wait(mid):
        while time.time() - t0 < timeout:
            try:
                line = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                return None
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if isinstance(o, dict) and o.get("id") == mid:
                return o
        return None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "nyrasview", "version": "2"}}})
        init = wait(1)
        if not init:
            err = b""
            try:
                plat.kill_tree(p.pid, True)
                err = p.stderr.read()[-600:]
            except Exception:
                pass
            return {"ok": False, "error": (err.decode(errors="replace").strip() or "no response (timed out)"),
                    "ms": int((time.time() - t0) * 1000)}
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tl = wait(2) or {}
        tools = [t.get("name") for t in ((tl.get("result") or {}).get("tools") or [])]
        info = (init.get("result") or {}).get("serverInfo") or {}
        return {"ok": True, "server": info.get("name"), "version": info.get("version"), "tools": tools,
                "ms": int((time.time() - t0) * 1000)}
    finally:
        plat.kill_tree(p.pid, True)


def _probe_http(spec, t0):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "nyrasview", "version": "2"}}}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               **{k: str(v) for k, v in (spec.get("headers") or {}).items()}}
    try:
        req = urllib.request.Request(spec["url"], data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read(200000).decode(errors="replace")
        m = re.search(r'"serverInfo"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"', raw)
        return {"ok": True, "server": m.group(1) if m else None, "tools": None, "ms": int((time.time() - t0) * 1000),
                "note": "reachable"}
    except urllib.error.HTTPError as e:
        note = "needs sign-in" if e.code in (401, 403) else f"HTTP {e.code}"
        return {"ok": e.code in (401, 403), "note": note, "error": None if e.code in (401, 403) else note,
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "ms": int((time.time() - t0) * 1000)}


def matrix():
    from .mcp import configured
    rows = {}
    for s in configured():
        if s["scope"] != "user":
            continue
        r = rows.setdefault(s["name"], {"name": s["name"], "transport": s["transport"],
                                        "target": s["url"] or " ".join([s["command"] or ""] + s["args"]), "agents": {}})
        r["agents"][s["agent"]] = {"enabled": s["enabled"]}
    return {"targets": targets(), "servers": sorted(rows.values(), key=lambda r: r["name"].lower()), "presets": PRESETS}
