import os
import re

from .util import HOME, P, load_json, load_toml

LAUNCHERS = {"npx", "uvx", "bunx", "pnpx", "pnpm", "yarn", "node", "bun", "deno", "python", "python3",
             "uv", "docker", "podman", "sh", "bash", "cmd", "pipx"}
MCP_RE = re.compile(r"(^|[/@_\-.\s])mcp([/_\-.\s]|$)", re.I)


def _spec(agent, name, spec, scope, source):
    if not isinstance(spec, dict):
        return None
    url = spec.get("url") or spec.get("httpUrl") or spec.get("serverUrl")
    cmd = spec.get("command")
    args = list(spec.get("args") or [])
    if isinstance(cmd, list):
        args = [str(x) for x in cmd[1:]] + args
        cmd = cmd[0] if cmd else None
    t = (spec.get("type") or spec.get("transport") or "").lower()
    if url:
        transport = "sse" if t == "sse" or str(url).rstrip("/").endswith("/sse") else "http"
    else:
        transport = "stdio"
    enabled = not (spec.get("disabled") is True or spec.get("enabled") is False)
    return {"agent": agent, "name": name, "transport": transport, "command": cmd,
            "args": [str(a) for a in args], "url": url, "scope": scope, "source": str(source),
            "enabled": enabled}


def _servers(agent, block, scope, source, out):
    if isinstance(block, dict):
        for name, spec in block.items():
            s = _spec(agent, name, spec, scope, source)
            if s:
                out.append(s)


def _json_key(agent, path, key, scope, out):
    d = load_json(path)
    if isinstance(d, dict):
        block = d
        for k in key.split("."):
            block = block.get(k) if isinstance(block, dict) else None
        if block is None and "." in key:
            block = d.get(key)
        _servers(agent, block, scope, path, out)


def configured(cwds=()):
    out = []
    cwds = sorted({str(c) for c in cwds if c} | {str(HOME)})

    cj = HOME / ".claude.json"
    d = load_json(cj)
    if isinstance(d, dict):
        _servers("claude", d.get("mcpServers"), "user", cj, out)
        for proj, pv in (d.get("projects") or {}).items():
            if isinstance(pv, dict) and pv.get("mcpServers"):
                _servers("claude", pv["mcpServers"], f"local:{proj}", cj, out)
    for c in cwds:
        _json_key("claude", P(c) / ".mcp.json", "mcpServers", f"project:{c}", out)

    t = load_toml(HOME / ".codex/config.toml")
    if isinstance(t, dict):
        _servers("codex", t.get("mcp_servers"), "user", HOME / ".codex/config.toml", out)

    _json_key("gemini", HOME / ".gemini/settings.json", "mcpServers", "user", out)
    _json_key("qwen", HOME / ".qwen/settings.json", "mcpServers", "user", out)
    for c in cwds:
        if c != str(HOME):
            _json_key("gemini", P(c) / ".gemini/settings.json", "mcpServers", f"project:{c}", out)

    for base in (HOME / ".config/opencode",):
        for fn in ("opencode.json", "opencode.jsonc", "config.json"):
            _json_key("opencode", base / fn, "mcp", "user", out)
    for c in cwds:
        if c != str(HOME):
            for fn in ("opencode.json", "opencode.jsonc"):
                _json_key("opencode", P(c) / fn, "mcp", f"project:{c}", out)

    _json_key("cursor", HOME / ".cursor/mcp.json", "mcpServers", "user", out)
    for c in cwds:
        if c != str(HOME):
            _json_key("cursor", P(c) / ".cursor/mcp.json", "mcpServers", f"project:{c}", out)

    _json_key("kimi", HOME / ".kimi/mcp.json", "mcpServers", "user", out)
    _json_key("antigravity", HOME / ".gemini/antigravity/mcp_config.json", "mcpServers", "user", out)
    _json_key("windsurf", HOME / ".codeium/windsurf/mcp_config.json", "mcpServers", "user", out)
    _json_key("copilot", HOME / ".copilot/mcp-config.json", "mcpServers", "user", out)
    _json_key("droid", HOME / ".factory/mcp.json", "mcpServers", "user", out)
    _json_key("amp", HOME / ".config/amp/settings.json", "amp.mcpServers", "user", out)
    _json_key("crush", HOME / ".config/crush/crush.json", "mcp", "user", out)
    for c in cwds:
        if c != str(HOME):
            for fn in ("crush.json", ".crush.json"):
                _json_key("crush", P(c) / fn, "mcp", f"project:{c}", out)

    seen, uniq = set(), []
    for s in out:
        k = (s["agent"], s["name"], s["scope"])
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    return uniq


def matches(argv, server):
    if not argv or server["transport"] != "stdio" or not server.get("command"):
        return False
    joined = " ".join(argv)
    base = os.path.basename(server["command"]).lower()
    names = {os.path.basename(a).lower() for a in argv[:4]}
    keys = [a for a in server["args"] if len(a) > 2 and not a.startswith("-")]
    if base in LAUNCHERS or base.startswith("python"):
        return any(k in joined for k in keys[:3])
    if base in names or server["command"] in joined:
        return not keys or any(k in joined for k in keys[:3]) or base not in LAUNCHERS
    return False


def guess(argv):
    if not argv:
        return None
    head = argv[:5]
    for a in head:
        b = os.path.basename(a)
        if MCP_RE.search(b) and not b.startswith("-"):
            return re.sub(r"(@[\d.^~]+|\.js|\.py)$", "", b)
    return None
