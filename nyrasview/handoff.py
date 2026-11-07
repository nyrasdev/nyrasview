import json
import os
import re
import shlex
import shutil
import subprocess
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from . import plat
from .registry import BY_KEY
from .util import HOME, STATE, config, parse_ts

LIMIT_RE = re.compile(
    r"(usage limit|limit reached|hit your (usage )?limit|out of (extra )?usage|weekly limit|"
    r"session limit|quota (exceeded|exhausted)|credit balance is too low|resource_exhausted|"
    r"insufficient_quota|too many requests)", re.I)
RESET_RE = re.compile(r"(?:resets?|try again)\s+(?:at\s+|in\s+)?([^\n·|.]{2,40})", re.I)
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "edit", "write", "write_file", "replace",
              "apply_patch", "str_replace_editor", "create_file"}

INTERACTIVE = {
    "claude": ["claude", "{prompt}"],
    "codex": ["codex", "{prompt}"],
    "gemini": ["gemini", "-i", "{prompt}"],
    "qwen": ["qwen", "-i", "{prompt}"],
    "opencode": ["opencode", "--prompt", "{prompt}"],
    "cursor": ["cursor-agent", "{prompt}"],
    "copilot": ["copilot", "-i", "{prompt}"],
    "droid": ["droid", "{prompt}"],
    "kimi": ["kimi"],
    "crush": ["crush"],
    "amp": ["amp"],
    "goose": ["goose", "session"],
    "aider": ["aider"],
}
CHAIN = ["codex", "claude", "gemini", "opencode", "cursor", "qwen", "copilot", "kimi", "droid", "crush", "amp",
         "goose", "aider"]


def _lines(path):
    try:
        with open(path, errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if raw:
                    try:
                        yield json.loads(raw)
                    except ValueError:
                        continue
    except OSError:
        return


def _limit(text, is_error=True):
    if not text or not LIMIT_RE.search(text) or (not is_error and len(text) > 400):
        return None
    m = RESET_RE.search(text)
    reset = m.group(1).strip() if m else None
    m2 = re.search(r"\|(\d{10})\b", text)
    if m2:
        reset = datetime.fromtimestamp(int(m2.group(1))).strftime("%H:%M %a")
    return {"message": re.sub(r"\|\d{10}", "", text).strip()[:300], "reset": reset}


def _newest(paths):
    best = None
    for p in paths:
        try:
            m = os.stat(p).st_mtime
        except OSError:
            continue
        if best is None or m > best[0]:
            best = (m, str(p))
    return best[1] if best else None


def claude_session(cwd):
    d = HOME / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)
    return _newest(d.glob("*.jsonl")) if d.is_dir() else None


def codex_session(cwd):
    root = HOME / ".codex/sessions"
    if not root.is_dir():
        return None
    files = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:12]
    for f in files:
        try:
            first = json.loads(open(f).readline() or "{}")
        except (OSError, ValueError):
            continue
        c = (first.get("payload") or {}).get("cwd")
        if not c or os.path.realpath(c) == os.path.realpath(cwd):
            return str(f)
    return None


def _ctx():
    return {"title": None, "prompts": [], "edits": OrderedDict(), "commands": [], "replies": [],
            "limit": None, "started": 0, "ended": 0}


def _edit(ctx, path, op):
    if not path:
        return
    path = str(path)
    e = ctx["edits"].setdefault(path, OrderedDict())
    e[op] = e.get(op, 0) + 1
    ctx["edits"].move_to_end(path)


def read_claude(path):
    ctx = _ctx()
    for o in _lines(path):
        t = o.get("type")
        ts = parse_ts(o.get("timestamp")) if o.get("timestamp") else 0
        if ts:
            ctx["started"] = ctx["started"] or ts
            ctx["ended"] = ts
        if t == "ai-title" and o.get("aiTitle"):
            ctx["title"] = o["aiTitle"]
        if t == "user" and not o.get("isMeta"):
            c = (o.get("message") or {}).get("content")
            texts = [c] if isinstance(c, str) else [b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text"]
            for x in texts:
                if x and not x.lstrip().startswith("<") and not x.startswith("[Request interrupted"):
                    ctx["prompts"].append(x.strip())
                    ctx["limit"] = None
        elif t == "assistant":
            m = o.get("message") or {}
            for b in m.get("content") or []:
                if b.get("type") == "tool_use":
                    name, inp = b.get("name"), b.get("input") or {}
                    if name in EDIT_TOOLS:
                        _edit(ctx, inp.get("file_path") or inp.get("notebook_path") or inp.get("path"), name)
                    elif name == "Bash" and inp.get("command"):
                        ctx["commands"].append(inp["command"])
                elif b.get("type") == "text" and b.get("text", "").strip():
                    lim = _limit(b["text"], bool(o.get("isApiErrorMessage") or o.get("error")))
                    if lim:
                        ctx["limit"] = lim
                    else:
                        ctx["replies"].append(b["text"].strip())
    return ctx


PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$", re.M)


def read_codex(path):
    ctx = _ctx()
    for o in _lines(path):
        p = o.get("payload") or {}
        pt = p.get("type")
        ts = parse_ts(o.get("timestamp")) if o.get("timestamp") else 0
        if ts:
            ctx["started"] = ctx["started"] or ts
            ctx["ended"] = ts
        if pt == "user_message" and p.get("message"):
            ctx["prompts"].append(p["message"].strip())
            ctx["limit"] = None
        elif pt in ("function_call", "custom_tool_call", "local_shell_call"):
            args = p.get("arguments") or p.get("input") or ""
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"input": args}
            blob = json.dumps(args) if not isinstance(args, str) else args
            raw = args.get("input") if isinstance(args, dict) and isinstance(args.get("input"), str) else blob
            hits = PATCH_FILE.findall(raw.replace("\\n", "\n"))
            for op, f in hits:
                _edit(ctx, f.strip(), op.lower())
            if not hits and isinstance(args, dict):
                cmd = args.get("command") or args.get("cmd")
                if cmd:
                    ctx["commands"].append(" ".join(cmd) if isinstance(cmd, list) else str(cmd))
        elif pt == "agent_message" and p.get("message"):
            ctx["replies"].append(p["message"].strip())
        elif pt in ("error", "stream_error") or (pt == "token_count" and (p.get("rate_limits") or {}).get("primary", {}).get("used_percent", 0) >= 100):
            lim = _limit(p.get("message") or "usage limit reached")
            if lim:
                ctx["limit"] = lim
    return ctx


READERS = {"claude": (claude_session, read_claude), "codex": (codex_session, read_codex)}


def detect(agent, cwd):
    r = READERS.get(agent)
    if not r or not cwd:
        return None
    path = r[0](cwd)
    if not path:
        return None
    ctx = r[1](path)
    ctx["session"] = path
    return ctx


def _git(cwd, *args, limit=None):
    try:
        out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if limit and len(out) > limit:
        out = out[:limit] + f"\n… diff truncated ({len(out) - limit} more chars, run `git diff` to see all)\n"
    return out


def is_repo(cwd):
    return _git(cwd, "rev-parse", "--is-inside-work-tree").strip() == "true"


def _cut(s, n):
    s = s.strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def build(agent, cwd, to, ctx):
    src = BY_KEY[agent].name if agent in BY_KEY else agent
    from . import fallback
    dst = fallback.label(to)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rel = lambda p: os.path.relpath(p, cwd) if os.path.isabs(p) and p.startswith(cwd) else p.replace(str(HOME), "~")
    L = [f"# Handoff: {src} → {dst}", ""]
    why = "it stopped"
    if ctx.get("limit"):
        why = f"it hit its usage limit (\"{ctx['limit']['message']}\")"
        if ctx["limit"].get("reset"):
            why += f", resets {ctx['limit']['reset']}"
    L += [f"_Written by nyrasview on {now}. {src} was working in `{cwd.replace(str(HOME), '~')}` and {why}. "
          f"You are taking over._", ""]
    if ctx.get("title"):
        L += [f"**Task:** {ctx['title']}", ""]
    prompts = ctx["prompts"][-8:]
    if prompts:
        L += ["## What the user asked for", ""]
        for i, p in enumerate(prompts, 1):
            tag = " **(latest)**" if i == len(prompts) else ""
            L += [f"{i}.{tag} " + _cut(p, 900).replace("\n", "\n   "), ""]
    if ctx["replies"]:
        L += [f"## What {src} said it did (last messages)", ""]
        for r in ctx["replies"][-3:]:
            L += ["> " + _cut(r, 1200).replace("\n", "\n> "), ""]
    if ctx["edits"]:
        L += [f"## Files {src} changed", ""]
        for f, ops in list(ctx["edits"].items())[-60:]:
            L.append(f"- `{rel(f)}` — " + ", ".join(f"{k} ×{v}" for k, v in ops.items()))
        L.append("")
    if ctx["commands"]:
        L += ["## Commands it ran (most recent last)", "", "```sh"]
        L += [_cut(c, 300).replace("\n", " ⏎ ") for c in ctx["commands"][-15:]]
        L += ["```", ""]
    if is_repo(cwd):
        status = _git(cwd, "status", "--short")
        stat = _git(cwd, "diff", "HEAD", "--stat")
        diff = _git(cwd, "diff", "HEAD", limit=int(config().get("handoff", {}).get("max_diff", 60000)))
        L += ["## Repository state", "", "```", status.strip() or "(clean)", "```", ""]
        if stat.strip():
            L += ["```", stat.strip(), "```", ""]
        if diff.strip():
            L += ["<details><summary>Full diff vs HEAD</summary>", "", "```diff", diff.rstrip(), "```", "", "</details>", ""]
    else:
        L += ["_Not a git repository, so there is no diff. Read the changed files listed above directly._", ""]
    latest = _cut(prompts[-1], 400) if prompts else "the task above"
    L += ["## Your job", "",
          f"1. Skim the changed files and the diff so you know what {src} already did. Do not redo finished work.",
          f"2. Continue the latest request: \"{latest}\"",
          "3. Check that it works (build/tests if the project has them), then report what you finished and what is left.",
          ""]
    return "\n".join(L)


def write(cwd, text):
    d = Path(cwd) / ".nyrasview"
    d.mkdir(exist_ok=True)
    path = d / "HANDOFF.md"
    path.write_text(text)
    arch = STATE / "handoffs"
    arch.mkdir(parents=True, exist_ok=True)
    (arch / f"{re.sub(r'[^A-Za-z0-9]', '-', cwd).strip('-')}-{int(time.time())}.md").write_text(text)
    if is_repo(cwd):
        gd = _git(cwd, "rev-parse", "--git-common-dir").strip()
        if gd:
            ex = Path(cwd, gd, "info", "exclude") if not os.path.isabs(gd) else Path(gd, "info", "exclude")
            try:
                cur = ex.read_text() if ex.exists() else ""
                if ".nyrasview/" not in cur:
                    ex.parent.mkdir(parents=True, exist_ok=True)
                    ex.write_text(cur + ("" if cur.endswith("\n") or not cur else "\n") + ".nyrasview/\n")
            except OSError:
                pass
    return str(path)


def targets(exclude=None):
    from . import fallback
    chain = config().get("handoff", {}).get("chain") or CHAIN
    out = fallback.available(exclude) if exclude else []
    for k in chain:
        if k == exclude or k not in BY_KEY:
            continue
        cmd = interactive(k)
        if cmd and plat.which(cmd[0]):
            out.append(k)
    out += [t for t in fallback.available() if t not in out and fallback.TARGETS[t]["base"] != exclude]
    return out


def interactive(key):
    over = config().get("agents", {}).get(key, {}).get("interactive")
    return list(over) if over else list(INTERACTIVE.get(key, []))


def kickoff(src, to=None):
    from . import fallback
    name = BY_KEY[src].name if src in BY_KEY else src
    if to in fallback.TARGETS and fallback.TARGETS[to]["base"] == src:
        return ("You hit the subscription usage limit, so this session now runs on an API key. Continue exactly where you "
                "left off. .nyrasview/HANDOFF.md has a summary of the task and the changes so far if you need it.")
    return (f"{name} stopped mid-task and you are taking over. Read .nyrasview/HANDOFF.md in this folder: "
            "it has what I asked for, what was already done, the files that changed and the current diff. "
            "Then continue the work from where it stopped.")


def command(to, src):
    from . import fallback
    if to in fallback.TARGETS:
        return fallback.command(to, kickoff(src, to)), True
    cmd = interactive(to)
    if not cmd:
        raise RuntimeError(f"no interactive command known for {to}")
    p = kickoff(src)
    if "{prompt}" in " ".join(cmd):
        return [c.replace("{prompt}", p) for c in cmd], True
    return cmd, False


def terminal(cwd, cmd, env=None):
    pref = os.environ.get("NYRA_TERMINAL") or config().get("handoff", {}).get("terminal") or os.environ.get("TERMINAL")
    return plat.terminal(cwd, cmd, pref, env)


def notify(title, body):
    plat.notify(title, body)


def prepare(agent, cwd, to=None):
    cwd = os.path.realpath(os.path.expanduser(cwd))
    ctx = detect(agent, cwd)
    if ctx is None:
        ctx = _ctx()
    to = to or next(iter(targets(exclude=agent)), None)
    if not to:
        raise RuntimeError("no other coding agent with an interactive mode is installed")
    text = build(agent, cwd, to, ctx)
    path = write(cwd, text)
    return {"from": agent, "to": to, "cwd": cwd, "path": path, "ctx": ctx}


def launch(h, where="terminal"):
    from . import fallback
    cmd, embedded = command(h["to"], h["from"])
    extra = fallback.env_for(h["to"]) if h["to"] in fallback.TARGETS else {}
    if where == "here":
        os.chdir(h["cwd"])
        os.environ.update(extra)
        if plat.WIN:
            import sys
            sys.exit(subprocess.call(plat.resolve(cmd)))
        os.execvp(cmd[0], cmd)
    used = terminal(h["cwd"], cmd, extra or None)
    return {"terminal": used, "embedded": embedded, "cmd": cmd}


def relay(agents=("claude", "codex"), interval=10.0, auto=True, on_event=None):
    import psutil
    from .registry import identify
    handled = {}
    while True:
        seen = set()
        for p in psutil.process_iter(["pid", "ppid", "name", "exe", "cmdline", "cwd"]):
            key = identify(p.info)
            cwd = p.info.get("cwd")
            if key not in agents or not cwd or (key, cwd) in seen:
                continue
            seen.add((key, cwd))
            ctx = detect(key, cwd)
            if not ctx or not ctx.get("limit"):
                continue
            mark = (key, cwd, ctx["limit"]["message"], len(ctx["prompts"]))
            if handled.get((key, cwd)) == mark:
                continue
            handled[(key, cwd)] = mark
            try:
                h = prepare(key, cwd)
                src = BY_KEY[key].name
                from . import fallback
                dst = fallback.label(h["to"])
                notify(f"{src} hit its limit", f"Handing {os.path.basename(cwd)} to {dst}")
                res = launch(h) if auto else None
                if on_event:
                    on_event("handoff", {**h, "launched": res})
            except Exception as e:
                if on_event:
                    on_event("error", {"from": key, "cwd": cwd, "error": str(e)})
        time.sleep(interval)
