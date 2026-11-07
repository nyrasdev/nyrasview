import os
from pathlib import Path

from .util import HOME, P, frontmatter, load_json

SKIP = {"node_modules", ".git", "__pycache__", ".venv", "venv"}

GLOBAL = [
    ("claude", "skill", "~/.claude/skills", "**/SKILL.md", "user"),
    ("claude", "skill", "~/.claude/plugins/cache", "**/skills/*/SKILL.md", "plugin"),
    ("claude", "command", "~/.claude/commands", "**/*.md", "user"),
    ("claude", "subagent", "~/.claude/agents", "**/*.md", "user"),
    ("claude", "memory", "~/.claude", "CLAUDE.md", "user"),
    ("codex", "skill", "~/.codex/skills", "**/SKILL.md", "user"),
    ("codex", "command", "~/.codex/prompts", "*.md", "user"),
    ("codex", "memory", "~/.codex", "AGENTS.md", "user"),
    ("gemini", "skill", "~/.gemini/skills", "**/SKILL.md", "user"),
    ("gemini", "command", "~/.gemini/commands", "**/*.toml", "user"),
    ("gemini", "memory", "~/.gemini", "GEMINI.md", "user"),
    ("opencode", "skill", "~/.config/opencode/skill", "**/SKILL.md", "user"),
    ("opencode", "skill", "~/.config/opencode/skills", "**/SKILL.md", "user"),
    ("opencode", "subagent", "~/.config/opencode/agent", "*.md", "user"),
    ("opencode", "command", "~/.config/opencode/command", "*.md", "user"),
    ("kimi", "skill", "~/.kimi/skills", "**/SKILL.md", "user"),
    ("antigravity", "skill", "~/.gemini/antigravity/skills", "**/SKILL.md", "user"),
    ("antigravity", "workflow", "~/.gemini/antigravity/global_workflows", "*.md", "user"),
    ("cursor", "rule", "~/.cursor/rules", "*.mdc", "user"),
    ("cursor", "command", "~/.cursor/commands", "*.md", "user"),
    ("qwen", "command", "~/.qwen/commands", "**/*.toml", "user"),
    ("copilot", "skill", "~/.copilot/skills", "**/SKILL.md", "user"),
    ("droid", "subagent", "~/.factory/droids", "*.md", "user"),
    ("shared", "skill", "~/.agents/skills", "**/SKILL.md", "user"),
    ("shared", "skill", "~/.config/agents/skills", "**/SKILL.md", "user"),
]

PROJECT = [
    ("claude", "skill", ".claude/skills", "**/SKILL.md"),
    ("claude", "command", ".claude/commands", "**/*.md"),
    ("claude", "subagent", ".claude/agents", "*.md"),
    ("claude", "memory", ".", "CLAUDE.md"),
    ("codex", "memory", ".", "AGENTS.md"),
    ("gemini", "memory", ".", "GEMINI.md"),
    ("gemini", "command", ".gemini/commands", "**/*.toml"),
    ("opencode", "subagent", ".opencode/agent", "*.md"),
    ("opencode", "command", ".opencode/command", "*.md"),
    ("cursor", "rule", ".cursor/rules", "*.mdc"),
    ("antigravity", "workflow", ".agent/workflows", "*.md"),
    ("shared", "skill", ".agents/skills", "**/SKILL.md"),
]


def _walk(base, pattern):
    if not base.is_dir():
        return []
    try:
        return [p for p in base.glob(pattern) if not (SKIP & set(p.parts)) and p.is_file()]
    except OSError:
        return []


def _entry(agent, kind, path, scope):
    meta = frontmatter(path) if path.suffix in (".md", ".mdc") else {}
    name = meta.get("name")
    if not name:
        name = path.parent.name if path.name == "SKILL.md" else path.stem
    desc = meta.get("description", "")
    if path.suffix == ".toml":
        try:
            for line in path.read_text(errors="replace").splitlines():
                if line.strip().startswith("description"):
                    desc = line.split("=", 1)[1].strip().strip("'\"")
                    break
        except OSError:
            pass
    if kind == "memory":
        desc = f"{path.stat().st_size} bytes of standing instructions"
    return {"agent": agent, "kind": kind, "name": name, "description": desc,
            "path": str(path), "scope": scope}


def scan(cwds=()):
    out, seen = [], set()

    def add(agent, kind, path, scope):
        rp = os.path.realpath(path)
        if (agent, rp) in seen:
            return
        seen.add((agent, rp))
        try:
            out.append(_entry(agent, kind, Path(path), scope))
        except OSError:
            pass

    for agent, kind, base, pat, scope in GLOBAL:
        for p in _walk(P(base), pat):
            add(agent, kind, p, scope)
    for ext in _walk(HOME / ".gemini/extensions", "*/gemini-extension.json"):
        d = load_json(ext) or {}
        seen.add(("gemini", str(ext)))
        out.append({"agent": "gemini", "kind": "extension", "name": d.get("name", ext.parent.name),
                    "description": d.get("description", f"v{d.get('version', '?')}"),
                    "path": str(ext), "scope": "user"})
    for c in sorted({str(c) for c in cwds if c and str(c) != str(HOME)}):
        for agent, kind, base, pat in PROJECT:
            for p in _walk(P(c) / base, pat):
                add(agent, kind, p, f"project:{c}")
    return out
