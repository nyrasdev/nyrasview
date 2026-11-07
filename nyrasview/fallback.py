import os
import re

from . import plat
from .registry import BY_KEY
from .util import CONFIG, config

PROVIDERS = {
    "anthropic": {"env": "ANTHROPIC_API_KEY", "name": "Anthropic", "for": "Claude Code", "url": "https://console.anthropic.com/settings/keys"},
    "openai": {"env": "OPENAI_API_KEY", "name": "OpenAI", "for": "Codex", "url": "https://platform.openai.com/api-keys"},
    "openrouter": {"env": "OPENROUTER_API_KEY", "name": "OpenRouter", "for": "opencode, collab and prompts", "url": "https://openrouter.ai/keys"},
}
AGENT_PROVIDER = {"claude": "anthropic", "codex": "openai", "opencode": "openrouter"}
TARGETS = {
    "claude@api": {"base": "claude", "provider": "anthropic", "cmd": ["claude", "--continue", "{prompt}"], "suffix": "on API key"},
    "codex@api": {"base": "codex", "provider": "openai", "cmd": ["codex", "resume", "--last", "{prompt}"], "suffix": "on API key"},
    "opencode@openrouter": {"base": "opencode", "provider": "openrouter", "cmd": ["opencode", "-m", "openrouter/{model}", "--prompt", "{prompt}"],
                            "suffix": "via OpenRouter"},
}


def key(provider):
    p = PROVIDERS[provider]
    v = os.environ.get(p["env"])
    if v:
        return v
    cfg = config()
    v = cfg.get("fallback", {}).get(f"{provider}_api_key")
    if not v and provider == "openrouter":
        v = cfg.get("openrouter", {}).get("api_key")
    return v or None


def source(provider):
    if os.environ.get(PROVIDERS[provider]["env"]):
        return "env"
    return "config" if key(provider) else None


def status():
    return [{"provider": k, "name": p["name"], "for": p["for"], "url": p["url"], "env": p["env"],
             "connected": bool(key(k)), "source": source(k),
             "hint": (key(k)[:7] + "…" + key(k)[-4:]) if key(k) and len(key(k)) > 14 else None, "key": key(k) or ""}
            for k, p in PROVIDERS.items()]


def save(provider, value):
    if provider not in PROVIDERS:
        raise ValueError("unknown provider")
    value = (value or "").strip()
    text = CONFIG.read_text() if CONFIG.exists() else ""
    lines, out, inside, done = text.split("\n"), [], False, False
    keyname = f"{provider}_api_key"
    for line in lines:
        m = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if m:
            if inside and not done and value:
                out.append(f'{keyname} = "{value}"')
                done = True
            inside = m.group(1).strip() == "fallback"
        if inside and re.match(rf"^\s*{keyname}\s*=", line):
            if value and not done:
                out.append(f'{keyname} = "{value}"')
                done = True
            continue
        out.append(line)
    if value and not done:
        if inside:
            out.append(f'{keyname} = "{value}"')
        else:
            out += ["", "[fallback]", f'{keyname} = "{value}"']
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text("\n".join(out).strip("\n") + "\n", encoding="utf-8")
    if not plat.WIN:
        os.chmod(CONFIG, 0o600)


def env_for(target_or_agent):
    t = TARGETS.get(target_or_agent)
    prov = t["provider"] if t else AGENT_PROVIDER.get(target_or_agent)
    if not prov or not key(prov):
        return {}
    return {PROVIDERS[prov]["env"]: key(prov)}


def available(base=None):
    out = []
    for name, t in TARGETS.items():
        if base and t["base"] != base:
            continue
        a = BY_KEY.get(t["base"])
        if a and key(t["provider"]) and plat.which(t["cmd"][0]):
            out.append(name)
    return out


def label(target):
    t = TARGETS.get(target)
    if t:
        return f"{BY_KEY[t['base']].name} {t['suffix']}"
    return BY_KEY[target].name if target in BY_KEY else target


def color(target):
    base = TARGETS.get(target, {}).get("base", target)
    return BY_KEY[base].hex if base in BY_KEY else None


def command(target, prompt):
    t = TARGETS[target]
    model = config().get("fallback", {}).get("openrouter_model", "moonshotai/kimi-k2")
    return [c.replace("{prompt}", prompt).replace("{model}", model) for c in t["cmd"]]
