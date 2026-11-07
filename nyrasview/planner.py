import json
import re
import shutil
from pathlib import Path

from . import collab, local
from .registry import BY_KEY
from .util import config

STRENGTHS = {
    "claude": "careful reasoning, architecture, security, writing",
    "codex": "implementation details, tests, refactors",
    "gemini": "large context, research, summaries",
    "opencode": "general coding with any provider",
    "qwen": "code generation",
    "kimi": "long context, agentic coding",
    "cursor": "editing code in a project",
    "copilot": "everyday coding tasks",
    "openrouter": "any hosted model",
}


def _size(name, info=None):
    s = ((info or {}).get("params") or "").upper()
    m = re.search(r"([\d.]+)\s*([BM])", s) or re.search(r"(\d+(?:\.\d+)?)\s*([bm])(?![a-z])", name.lower())
    if m:
        v = float(m.group(1))
        return v / 1000 if m.group(2).lower() == "m" else v
    if any(k in name.lower() for k in ("mini", "tiny", "small", "lite", "nano")):
        return 3.0
    return 999.0


def candidates():
    out = []
    for srv in local.scan():
        if not srv["up"]:
            continue
        for m in srv["models"]:
            if any(k in m["id"].lower() for k in ("embed", "whisper", "rerank", "clip", "vision-only")):
                continue
            out.append({"agent": srv["key"], "model": m["id"], "size": _size(m["id"], m), "where": f"local · {srv['name']}",
                        "loaded": any(l["id"] == m["id"] for l in srv["loaded"])})
    out.sort(key=lambda c: (c["size"], not c["loaded"]))
    return out


ROLE = ("The orchestrator only plans. It reads your goal, splits it into sections and gives each one to the agent "
        "best suited for it. It doesn't write the work itself, so the smallest model that follows instructions is "
        "enough, which keeps planning free or nearly free.")


def options():
    out = []
    for c in candidates():
        out.append({**c, "id": f"{c['agent']}:{c['model']}", "label": f"{c['model']} · {c['where']}"})
    from .net import openrouter_key
    cfg = config().get("planner", {})
    if openrouter_key():
        m = cfg.get("openrouter_model", "google/gemini-2.5-flash-lite")
        out.append({"agent": "openrouter", "model": m, "where": "OpenRouter (cheap)", "id": f"openrouter:{m}", "label": f"{m} · OpenRouter"})
    for key, model in (("claude", "haiku"), ("gemini", "gemini-2.5-flash-lite"), ("codex", None), ("opencode", None),
                       ("claude", "sonnet")):
        a = BY_KEY.get(key)
        if a and a.headless and a.headless.get("cmd") and shutil.which(a.headless["cmd"][0]):
            out.append({"agent": key, "model": model, "where": "installed CLI", "id": f"{key}:{model or ''}",
                        "label": f"{a.name}{' · ' + model if model else ''}"})
    return out


def remember(agent, model):
    from . import settings
    cfg = settings.load()
    cfg["planner"] = {**cfg.get("planner", {}), "agent": agent, **({"model": model} if model else {})}
    if not model:
        cfg["planner"].pop("model", None)
    settings.write_raw(settings.dump(cfg))


def pick():
    cfg = config().get("planner", {})
    if cfg.get("agent"):
        return {"agent": cfg["agent"], "model": cfg.get("model"), "where": "from config", "size": None}
    loc = candidates()
    if loc:
        return loc[0]
    from .net import openrouter_key
    if openrouter_key():
        return {"agent": "openrouter", "model": cfg.get("openrouter_model", "google/gemini-2.5-flash-lite"),
                "where": "OpenRouter (cheap)", "size": None}
    for key, model in (("claude", "haiku"), ("gemini", "gemini-2.5-flash-lite"), ("codex", None), ("opencode", None)):
        a = BY_KEY.get(key)
        if a and a.headless and a.headless.get("cmd") and shutil.which(a.headless["cmd"][0]):
            return {"agent": key, "model": model, "where": "smallest model of an installed CLI", "size": None}
    raise RuntimeError("no model available to plan with: start Ollama/LM Studio, set OPENROUTER_API_KEY, or install an agent CLI")


def workers(exclude=None):
    out = []
    for key, a in BY_KEY.items():
        if key == exclude or not a.headless or a.kind == "local":
            continue
        if a.headless.get("http") == "openrouter":
            from .net import openrouter_key
            if not openrouter_key():
                continue
        elif not (a.headless.get("cmd") and shutil.which(a.headless["cmd"][0])):
            continue
        out.append(key)
    for srv in local.scan():
        if srv["up"] and srv["models"] and srv["key"] in BY_KEY:
            out.append(srv["key"])
    return out


def prompt(goal, avail, sections=None):
    lines = [f"- {k}: {BY_KEY[k].name}" + (f" ({STRENGTHS[k]})" if k in STRENGTHS else " (local model, free)")
             for k in avail]
    n = f"exactly {sections}" if sections else "3 to 6"
    return (
        "You are a planner that splits a goal into work for a team of AI agents. Keep it short and concrete.\n\n"
        f"Goal: {goal}\n\nAvailable agents (use only these keys):\n" + "\n".join(lines) + "\n\n"
        f"Split the goal into {n} sections of one shared markdown document. Give each section to the agent that fits it best, "
        "and spread the work across agents when that makes sense. Each instruction should be 1-3 sentences and say exactly "
        "what to write in that section.\n\n"
        "Reply with ONLY this JSON, no prose, no code fence:\n"
        '{"title": "short document title", "sections": [{"title": "Section name", "agent": "agent key", '
        '"instruction": "what to write"}], "review": {"agent": "agent key", "instruction": "what to check across the whole document"}}'
    )


def parse(text, avail):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("the planner didn't return JSON")
    raw = m.group(0)
    try:
        d = json.loads(raw)
    except ValueError:
        d = json.loads(re.sub(r",(\s*[}\]])", r"\1", raw))
    secs = []
    for s in d.get("sections") or []:
        if not isinstance(s, dict) or not s.get("title"):
            continue
        ag = str(s.get("agent") or "").strip().lower()
        if ag not in avail:
            ag = next((k for k in avail if k in ag or ag in k), avail[0] if avail else ag)
        secs.append({"title": str(s["title"]).strip()[:80], "agent": ag,
                     "instruction": str(s.get("instruction") or f"Write the {s['title']} section.").strip()})
    if not secs:
        raise ValueError("the plan had no sections")
    rv = d.get("review") if isinstance(d.get("review"), dict) else None
    if rv:
        ag = str(rv.get("agent") or "").lower()
        rv = {"agent": ag if ag in avail else secs[0]["agent"], "instruction": str(rv.get("instruction") or "Make the whole document consistent.")}
    return {"title": str(d.get("title") or "Plan").strip()[:100], "sections": secs, "review": rv}


def make(goal, doc=None, sections=None, planner=None, review=True):
    who = planner or pick()
    avail = workers()
    if not avail:
        raise RuntimeError("no worker agents available: install Claude Code, Codex, Gemini CLI or opencode, or start a local model")
    text = collab.call(who["agent"], prompt(goal, avail, sections), who.get("model"), timeout=300)
    plan = parse(text, avail)
    if not doc:
        slug = re.sub(r"[^a-z0-9]+", "-", plan["title"].lower()).strip("-")[:50] or "plan"
        doc = str(Path.cwd() / f"{slug}.md")
    path = collab.new_doc(doc, plan["title"], [s["title"] for s in plan["sections"]])
    head = Path(path).read_text().split("\n", 1)
    Path(path).write_text(head[0] + f"\n\n> Goal: {goal}\n" + (head[1] if len(head) > 1 else ""))
    tasks = [collab.assign(path, s["title"], s["agent"], s["instruction"]) for s in plan["sections"]]
    if review and plan.get("review"):
        tasks.append(collab.assign(path, "", plan["review"]["agent"], plan["review"]["instruction"]))
    return {"doc": path, "planner": who, "plan": plan, "tasks": tasks}
