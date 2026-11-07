import json
import os
import re
import tomllib

from . import plat
from .util import CONFIG

SCHEMA = [
    {"id": "gui", "title": "App", "fields": [
        ("port", "int", 7341, "Port for nyrasview gui"),
        ("interval", "float", 1.5, "Seconds between refreshes"),
    ]},
    {"id": "notify", "title": "Alerts", "fields": [
        ("desktop", "bool", True, "Desktop notifications"),
        ("ntfy", "str", "", "ntfy topic or URL for phone alerts"),
        ("soon_pct", "int", 90, "Warn when a limit window passes this %"),
        ("soon_minutes", "int", 15, "…or when this many minutes are left at the current pace"),
        ("finished_after", "int", 30, "Only alert 'finished' if the agent worked at least this many seconds"),
        ("events", "list", ["approval", "finished", "limit", "limit_soon", "failed", "done"], "Which alerts to send"),
    ]},
    {"id": "limits", "title": "Limits", "fields": [
        ("claude_api", "bool", True, "Read real Claude limits from Anthropic's usage API"),
        ("refresh_seconds", "int", 120, "How often to ask Anthropic (seconds)"),
    ]},
    {"id": "fallback", "title": "API keys and fallback", "fields": [
        ("anthropic_api_key", "secret", "", "Anthropic key: Claude Code continues on API billing"),
        ("openai_api_key", "secret", "", "OpenAI key: Codex continues on API billing"),
        ("openrouter_api_key", "secret", "", "OpenRouter key: opencode, prompts and collab"),
        ("auto_retry", "bool", True, "Retry prompt runs that hit a plan limit on the API key"),
        ("openrouter_model", "str", "moonshotai/kimi-k2", "Model opencode uses through OpenRouter"),
    ]},
    {"id": "handoff", "title": "Relay and handoff", "fields": [
        ("chain", "list", ["codex", "claude", "gemini", "opencode", "cursor", "qwen", "copilot", "kimi"], "Who takes over, in order"),
        ("terminal", "str", "", "Terminal to open (kitty, wezterm, alacritty, wt…); empty = auto"),
        ("max_diff", "int", 60000, "Max characters of git diff in HANDOFF.md"),
    ]},
    {"id": "planner", "title": "Planner", "fields": [
        ("agent", "str", "", "Force a planner agent (e.g. ollama, lmstudio, claude); empty = cheapest available"),
        ("model", "str", "", "Model for the forced planner"),
        ("openrouter_model", "str", "google/gemini-2.5-flash-lite", "Cheap OpenRouter model used when no local model runs"),
    ]},
    {"id": "sandbox", "title": "Sandbox (your own Docker image)", "fields": [
        ("image", "str", "", "Image to run agents in, e.g. ghcr.io/you/agents:latest (agents must be installed in it)"),
        ("runtime", "str", "docker", "docker or podman"),
        ("default", "bool", False, "Run prompts in the sandbox by default"),
        ("collab", "bool", False, "Run collab tasks in the sandbox"),
        ("mount_logins", "bool", True, "Mount each agent's login folder (~/.claude, ~/.codex…) so it's signed in"),
        ("home", "str", "/root", "Home folder inside the container"),
        ("network", "str", "", "Docker network (empty = default, 'none' = offline)"),
        ("extra_args", "list", [], "Extra docker run arguments, comma separated"),
    ]},
    {"id": "collab", "title": "Collab", "fields": [
        ("timeout", "int", 900, "Seconds before a collab task gives up"),
    ]},
    {"id": "openrouter", "title": "OpenRouter", "fields": [
        ("model", "str", "openrouter/auto", "Default model when you prompt OpenRouter"),
    ]},
    {"id": "ollama", "title": "Ollama", "fields": [
        ("model", "str", "", "Default Ollama model for collab and prompts"),
    ]},
]


def raw():
    try:
        return CONFIG.read_text(encoding="utf-8")
    except OSError:
        return ""


def load():
    try:
        return tomllib.loads(raw())
    except tomllib.TOMLDecodeError:
        return {}


def view():
    cfg = load()
    out = []
    for sec in SCHEMA:
        cur = cfg.get(sec["id"], {})
        fields = []
        for name, typ, default, help in sec["fields"]:
            fields.append({"name": name, "type": typ, "default": default, "help": help,
                           "value": cur.get(name, default), "set": name in cur})
        out.append({"id": sec["id"], "title": sec["title"], "fields": fields})
    known = {s["id"] for s in SCHEMA}
    extra = {k: v for k, v in cfg.items() if k not in known}
    return {"path": str(CONFIG), "sections": out, "extra": extra, "raw": raw()}


def _key(k):
    return k if re.match(r"^[A-Za-z0-9_-]+$", k) else json.dumps(k)


def _val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_val(x)}" for k, x in v.items()) + " }"
    return json.dumps(str(v), ensure_ascii=False)


def dump(cfg):
    lines = []
    for k, v in cfg.items():
        if not isinstance(v, dict) and not (isinstance(v, list) and v and all(isinstance(x, dict) for x in v)):
            lines.append(f"{_key(k)} = {_val(v)}")

    for k, v in cfg.items():
        if isinstance(v, dict):
            simple = {a: b for a, b in v.items() if not isinstance(b, dict)}
            nested = {a: b for a, b in v.items() if isinstance(b, dict)}
            if simple or not nested:
                lines += ["", f"[{_key(k)}]"] + [f"{_key(a)} = {_val(b)}" for a, b in simple.items()]
            for a, b in nested.items():
                inner = {x: y for x, y in b.items()}
                lines += ["", f"[{_key(k)}.{_key(a)}]"] + [f"{_key(x)} = {_val(y)}" for x, y in inner.items()]
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            for item in v:
                lines += ["", f"[[{_key(k)}]]"] + [f"{_key(a)} = {_val(b)}" for a, b in item.items()]
    return "\n".join(lines).strip() + "\n"


def write_raw(text):
    tomllib.loads(text)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG.exists():
        from .util import STATE
        import shutil
        import time
        b = STATE / "backups"
        b.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CONFIG, b / f"config-{time.strftime('%Y%m%d-%H%M%S')}.toml")
    CONFIG.write_text(text, encoding="utf-8")
    if not plat.WIN:
        os.chmod(CONFIG, 0o600)


def _coerce(typ, v, default):
    if typ == "bool":
        return bool(v)
    if typ == "int":
        return int(v) if str(v).strip() not in ("", "None") else default
    if typ == "float":
        return float(v) if str(v).strip() not in ("", "None") else default
    if typ == "list":
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return [x.strip() for x in str(v).split(",") if x.strip()]
    return str(v or "")


def save(values):
    cfg = load()
    for sec in SCHEMA:
        incoming = values.get(sec["id"])
        if not isinstance(incoming, dict):
            continue
        cur = dict(cfg.get(sec["id"], {}))
        for name, typ, default, _ in sec["fields"]:
            if name not in incoming:
                continue
            v = _coerce(typ, incoming[name], default)
            if v == default or (typ in ("str", "secret") and v == ""):
                cur.pop(name, None)
            else:
                cur[name] = v
        if cur:
            cfg[sec["id"]] = cur
        else:
            cfg.pop(sec["id"], None)
    write_raw(dump(cfg))
    return view()
