import os
import shutil
from dataclasses import dataclass, field

from .util import P, config

INTERPRETERS = {"node", "nodejs", "bun", "deno", "python", "python3", "uv", "npx", "bunx", "pnpx"}


@dataclass
class Agent:
    key: str
    name: str
    color: int
    hex: str
    kind: str = "cli"
    bins: tuple = ()
    names: tuple = ()
    prefixes: tuple = ()
    cmd: tuple = ()
    homes: tuple = ()
    headless: dict | None = None
    extra: dict = field(default_factory=dict)

    def installed(self):
        if any(shutil.which(b) for b in self.bins):
            return True
        return any(P(h).exists() for h in self.homes)

    def binary(self):
        for b in self.bins:
            w = shutil.which(b)
            if w:
                return w
        return None


AGENTS = [
    Agent("claude", "Claude Code", 209, "#d97757",
          bins=("claude",), names=("claude",), cmd=("@anthropic-ai/claude-code",),
          homes=("~/.claude",),
          headless={"cmd": ["claude", "-p", "--output-format", "text"], "stdin": True, "model": "--model"}),
    Agent("codex", "Codex", 79, "#10a37f",
          bins=("codex",), names=("codex",), prefixes=("codex-x86_64", "codex-aarch64"),
          cmd=("@openai/codex",), homes=("~/.codex",),
          headless={"cmd": ["codex", "exec", "--skip-git-repo-check", "--color", "never",
                            "-o", "{out}", "-"], "stdin": True, "model": "-m"}),
    Agent("gemini", "Gemini CLI", 75, "#4f8cf7",
          bins=("gemini",), names=("gemini",), cmd=("@google/gemini-cli",), homes=("~/.gemini",),
          headless={"cmd": ["gemini", "-p", "{prompt}"], "model": "-m"}),
    Agent("opencode", "opencode", 250, "#b8b8b8",
          bins=("opencode",), names=("opencode", ".opencode"), cmd=("opencode-ai",),
          homes=("~/.config/opencode", "~/.local/share/opencode"),
          headless={"cmd": ["opencode", "run", "{prompt}"], "model": "-m"}),
    Agent("kimi", "Kimi CLI", 141, "#9b7bff",
          bins=("kimi", "kimi-cli"), names=("kimi", "kimi-cli"), cmd=("kimi_cli", "kimi-cli"),
          homes=("~/.kimi",),
          headless={"cmd": ["kimi", "--print", "-c", "{prompt}"], "model": "-m"}),
    Agent("antigravity", "Antigravity", 44, "#00c2d1", kind="ide",
          bins=("antigravity", "agy"), names=("antigravity",), cmd=("/antigravity/", "Antigravity"),
          homes=("~/.gemini/antigravity", "~/.config/Antigravity")),
    Agent("cursor", "Cursor", 255, "#e6e6e6", kind="ide",
          bins=("cursor-agent", "cursor"), names=("cursor", "cursor-agent"), cmd=("cursor-agent",),
          homes=("~/.cursor",),
          headless={"cmd": ["cursor-agent", "-p", "{prompt}", "--output-format", "text"], "model": "--model"}),
    Agent("qwen", "Qwen Code", 99, "#7b6cf6",
          bins=("qwen",), names=("qwen",), cmd=("@qwen-code/qwen-code",), homes=("~/.qwen",),
          headless={"cmd": ["qwen", "-p", "{prompt}"], "model": "-m"}),
    Agent("copilot", "Copilot CLI", 33, "#3b82f6",
          bins=("copilot",), names=("copilot",), cmd=("@github/copilot",), homes=("~/.copilot",),
          headless={"cmd": ["copilot", "-p", "{prompt}"], "model": "--model"}),
    Agent("crush", "Crush", 205, "#ff5fd2",
          bins=("crush",), names=("crush",), homes=("~/.config/crush", "~/.local/share/crush"),
          headless={"cmd": ["crush", "run", "{prompt}"]}),
    Agent("amp", "Amp", 203, "#f34e3f",
          bins=("amp",), names=("amp",), cmd=("@sourcegraph/amp",), homes=("~/.config/amp",),
          headless={"cmd": ["amp", "-x", "{prompt}"]}),
    Agent("droid", "Factory Droid", 214, "#ffaf00",
          bins=("droid",), names=("droid",), homes=("~/.factory",),
          headless={"cmd": ["droid", "exec", "{prompt}"], "model": "-m"}),
    Agent("goose", "Goose", 186, "#d7d787",
          bins=("goose",), names=("goose", "goosed"), homes=("~/.config/goose",),
          headless={"cmd": ["goose", "run", "-t", "{prompt}"]}),
    Agent("aider", "Aider", 156, "#afff87",
          bins=("aider",), names=("aider",), cmd=("aider_chat", "-m aider"), homes=("~/.aider",)),
    Agent("windsurf", "Windsurf", 37, "#00afaf", kind="ide",
          bins=("windsurf",), names=("windsurf",), homes=("~/.codeium/windsurf",)),
    Agent("ollama", "Ollama", 252, "#d0d0d0", kind="local",
          bins=("ollama",), names=("ollama",), homes=("~/.ollama",),
          headless={"http": "local", "server": "ollama"}),
    Agent("lmstudio", "LM Studio", 111, "#87afff", kind="local",
          bins=("lms",), names=("lm-studio", "lmstudio", "lms"), homes=("~/.lmstudio",),
          headless={"http": "local", "server": "lmstudio"}),
    Agent("llamacpp", "llama.cpp", 180, "#d7af87", kind="local",
          bins=("llama-server",), names=("llama-server", "llama-cli"),
          headless={"http": "local", "server": "llamacpp"}),
    Agent("vllm", "vLLM", 117, "#87d7ff", kind="local",
          bins=("vllm",), names=("vllm",), cmd=("vllm.entrypoints", "vllm serve"),
          headless={"http": "local", "server": "vllm"}),
    Agent("jan", "Jan", 153, "#afd7ff", kind="local",
          bins=("jan",), names=("jan",), homes=("~/.config/Jan",),
          headless={"http": "local", "server": "jan"}),
    Agent("koboldcpp", "KoboldCpp", 173, "#d78787", kind="local",
          bins=("koboldcpp",), names=("koboldcpp",), cmd=("koboldcpp",),
          headless={"http": "local", "server": "koboldcpp"}),
    Agent("openrouter", "OpenRouter", 167, "#6467f2", kind="api",
          headless={"http": "openrouter"}),
]

BY_KEY = {a.key: a for a in AGENTS}


def apply_config():
    for s in config().get("local", []) or []:
        if isinstance(s, dict) and s.get("url"):
            key = s.get("key") or s.get("name", "local").lower().replace(" ", "-")
            if key not in BY_KEY:
                a = Agent(key, s.get("name") or key, 150, s.get("hex", "#afd7af"), kind="local",
                          headless={"http": "local", "server": key})
                AGENTS.append(a)
                BY_KEY[key] = a
    cfg = config().get("agents", {})
    for key, over in cfg.items():
        if not isinstance(over, dict):
            continue
        a = BY_KEY.get(key)
        if a is None:
            a = Agent(key, over.get("name", key), over.get("color", 250), over.get("hex", "#bbbbbb"))
            AGENTS.append(a)
            BY_KEY[key] = a
        for attr in ("name", "color", "hex", "kind"):
            if attr in over:
                setattr(a, attr, over[attr])
        for attr in ("bins", "names", "prefixes", "cmd", "homes"):
            if attr in over:
                setattr(a, attr, tuple(over[attr]))
        if "headless" in over:
            a.headless = dict(over["headless"])


apply_config()


def tokens(info):
    toks = set()
    name = (info.get("name") or "").lower()
    if name:
        toks.add(name)
    exe = info.get("exe") or ""
    if exe:
        toks.add(os.path.basename(exe).lower())
    argv = info.get("cmdline") or []
    if argv:
        a0 = os.path.basename(argv[0]).lower()
        toks.add(a0)
        if (a0 in INTERPRETERS or a0.startswith("python")) and len(argv) > 1:
            for a in argv[1:3]:
                if not a.startswith("-"):
                    toks.add(os.path.basename(a).lower())
                    break
    return toks


def identify(info):
    toks = tokens(info)
    head = " ".join((info.get("cmdline") or [])[:3])
    exe = info.get("exe") or ""
    for a in AGENTS:
        if a.kind == "api":
            continue
        if toks & set(a.names):
            return a.key
        if a.prefixes and any(t.startswith(p) for t in toks for p in a.prefixes):
            return a.key
        if a.cmd and any(c in head or c in exe for c in a.cmd):
            return a.key
    return None
