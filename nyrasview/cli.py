import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import VERSION, collab
from .registry import AGENTS, BY_KEY
from .util import CONFIG, ago, hb, hn

C = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def col(hexc, s):
    if not C or not hexc:
        return s
    h = hexc.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"\033[1;38;2;{r};{g};{b}m{s}\033[0m"


def dim(s):
    return f"\033[2m{s}\033[0m" if C else s


def table(headers, rows):
    import re
    strip = lambda s: re.sub(r"\033\[[\d;]*m", "", str(s))
    w = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            w[i] = min(max(w[i], len(strip(c))), 60)
    print(dim("  ".join(h.ljust(w[i]) for i, h in enumerate(headers))))
    for r in rows:
        out = []
        for i, c in enumerate(r):
            s = str(c)
            vis = strip(s)
            if len(vis) > 60:
                s = vis[:59] + "…"
                vis = s
            out.append(s + " " * (w[i] - len(vis)))
        print("  ".join(out).rstrip())


def snap():
    from .collector import oneshot
    return oneshot()


def cmd_status(a):
    s = snap()
    if a.json:
        return print(json.dumps(s, default=str, indent=2))
    rows = []
    for g in s["agents"]:
        if not (g["visible"] or a.all):
            continue
        u = g["usage"]
        rows.append([col(g["hex"], "● " + g["name"]),
                     "live" if g["running"] else ("idle" if g["installed"] else "absent"),
                     g["sessions"] or "", g["nprocs"] or "", f"{g['cpu']:.1f}" if g["running"] else "",
                     hb(g["rss"]) if g["rss"] else "", sum(g["providers"].values()) or "",
                     f"{len(g['mcp_live'])}/{g['mcp_configured']}", g["skills"] or "",
                     hn(u.get("today", {}).get("out", 0)) if u else "", ago(u.get("last")) if u else ""])
    sy = s["system"]
    print(f"{col('#e879f9', 'nyrasview')} {VERSION} · {s['host']} · cpu {sy['cpu']:.0f}% · "
          f"ram {hb(sy['mem_used'])}/{hb(sy['mem_total'])}\n")
    table(["AGENT", "STATE", "SESS", "PROCS", "CPU%", "RAM", "NET", "MCP", "SKILLS", "OUT TODAY", "ACTIVE"], rows)


def cmd_ps(a):
    s = snap()
    for g in s["agents"]:
        if not g["procs"] or (a.agent and g["key"] != a.agent):
            continue
        print(col(g["hex"], f"\n{g['name']}") + dim(f"  {g['cpu']:.1f}% cpu · {hb(g['rss'])} · " +
              " ".join(f"{k}×{v}" for k, v in g["providers"].items())))
        table(["PID", "ROLE", "CPU%", "RAM", "NET", "COMMAND"],
              [[p["pid"], (p["mcp"] + " (mcp)") if p["mcp"] else p["role"], p["cpu"], hb(p["rss"]),
                len(p["conns"]) or "", "  " * min(p["depth"], 6) + p["cmd"]] for p in g["procs"]])


def cmd_mcp(a):
    from . import mcpman
    act = getattr(a, "mcp_cmd", None)
    if act:
        tg = [t["key"] for t in mcpman.targets()]
        to = [x for x in (a.to or "").split(",") if x] if hasattr(a, "to") else []
        if to == ["all"]:
            to = [t["key"] for t in mcpman.targets() if t["installed"]]
        if act == "test":
            spec = None
            if getattr(a, "src", None):
                spec = mcpman.full_spec(a.src, a.what)
            if spec is None:
                spec = mcpman.parse(a.what)[1]
            print(dim("starting it and asking for its tools…"), flush=True)
            r = mcpman.probe(spec)
            if r["ok"]:
                print(col("#22c55e", "✓ works") + f"  {r.get('server') or ''} {r.get('version') or ''}  " +
                      (f"{len(r['tools'])} tools: " + ", ".join(r["tools"][:12]) if r.get("tools") is not None else r.get("note", "")) +
                      dim(f"  ({r['ms']} ms)"))
            else:
                print(col("#ef4444", "✗ failed") + "  " + (r.get("error") or "")[:400])
            return
        if act == "add":
            name, spec = mcpman.parse(a.what, a.name)
        elif act == "copy":
            name, spec = a.what, mcpman.full_spec(a.src, a.what)
            if not spec:
                sys.exit(f"{a.what} isn't configured for {a.src}")
        else:
            name, spec = a.what, None
            if not to:
                to = [t for t in tg]
        if not to:
            sys.exit("say where: --to claude,codex or --to all")
        for ag in to:
            try:
                mcpman.set_server(ag, name, spec)
                print(f"{'removed' if spec is None else 'added'} {name} {'from' if spec is None else 'to'} {ag}")
            except Exception as e:
                print(col("#ef4444", f"{ag}: {e}"))
        from .util import STATE
        print(dim(f"restart running agents to load the change · backups in {STATE / 'backups'}"))
        return
    s = snap()
    if a.json:
        return print(json.dumps(s["mcp"], indent=2))
    live = {(g["key"], n): m for g in s["agents"] for n, m in g["mcp_live"].items()}
    rows = []
    for m in s["mcp"]:
        l = live.get((m["agent"], m["name"]))
        st = "running" if l else ("remote" if m["transport"] != "stdio" else ("idle" if m["enabled"] else "disabled"))
        rows.append([col(BY_KEY[m["agent"]].hex if m["agent"] in BY_KEY else None, m["agent"]), m["name"],
                     m["transport"], st, ",".join(map(str, l["pids"])) if l else "",
                     hb(l["rss"]) if l else "", m["scope"].replace(os.path.expanduser("~"), "~"),
                     m["url"] or " ".join([m["command"] or ""] + m["args"])])
    if not rows:
        return print("no MCP servers configured")
    table(["AGENT", "SERVER", "TYPE", "STATUS", "PIDS", "RAM", "SCOPE", "TARGET"], rows)


def cmd_skills(a):
    from . import skills
    from .collector import Collector
    items = skills.scan([os.getcwd()])
    if a.agent:
        items = [s for s in items if s["agent"] == a.agent]
    if a.query:
        q = a.query.lower()
        items = [s for s in items if q in (s["name"] + s["description"]).lower()]
    if a.json:
        return print(json.dumps(items, indent=2))
    items.sort(key=lambda s: (s["agent"], s["kind"], s["name"].lower()))
    table(["AGENT", "KIND", "NAME", "SCOPE", "DESCRIPTION"],
          [[col(BY_KEY[s["agent"]].hex if s["agent"] in BY_KEY else None, s["agent"]), s["kind"], s["name"],
            "project" if s["scope"].startswith("project") else s["scope"], s["description"]] for s in items])


def cmd_usage(a):
    from . import net
    from .usage import UsageStore
    u = UsageStore()
    u.refresh()
    s = u.summary()
    if a.json:
        return print(json.dumps(s, indent=2))
    rows = []
    for k, v in s.items():
        g = BY_KEY.get(k)
        t, w, al = v["today"], v["week"], v["total"]
        rows.append([col(g.hex if g else None, g.name if g else k), hn(t["in"]), hn(t["out"]), hn(t["cache"]),
                     hn(w["in"]), hn(w["out"]), hn(al["out"]), al["msgs"],
                     money(al["cost"]) if al["has_cost"] else "n/a", ago(v["last"])])
    if rows:
        table(["AGENT", "TODAY IN", "OUT", "CACHE", "7D IN", "7D OUT", "ALL OUT", "MSGS", "COST", "LAST"], rows)
    else:
        print("no local usage logs found")
    o = net.openrouter()
    if o.get("configured"):
        print()
        if o.get("error"):
            print(f"OpenRouter: {o['error']}")
        else:
            print(f"OpenRouter: ${o.get('usage') or 0:.2f} used · today ${o.get('daily') or 0:.2f} · "
                  f"month ${o.get('monthly') or 0:.2f}" + (f" · limit ${o['limit']}" if o.get("limit") else ""))
    for k, v in s.items():
        if a.models and v["models"]:
            print(col(BY_KEY[k].hex if k in BY_KEY else None, f"\n{k}"))
            table(["MODEL", "TOKENS", "OUT", "MSGS"], [[m["model"], hn(m["tokens"]), hn(m["out"]), m["msgs"]] for m in v["models"]])


def cmd_agents(a):
    rows = []
    for g in AGENTS:
        h = g.headless or {}
        how = h.get("http") and f"http:{h['http']}" or " ".join(h.get("cmd", [])[:4])
        rows.append([col(g.hex, g.key), g.name, g.kind, "yes" if g.installed() else "", how or dim("monitor only")])
    table(["KEY", "NAME", "KIND", "INSTALLED", "COLLAB VIA"], rows)
    print(dim(f"\noverride or add agents in {CONFIG}"))


def cmd_gui(a):
    from . import web
    from .collector import Collector
    from .util import config as _cfg
    g = _cfg().get("gui", {})
    c = Collector(interval=a.interval if a.interval != 1.5 else float(g.get("interval", 1.5)), relay=a.relay)
    web.serve(c, a.port if a.port != 7341 else int(g.get("port", 7341)), not a.no_open, a.phone, a.tunnel)


def cmd_tui(a):
    if not sys.stdout.isatty():
        sys.exit("nyrasview needs a terminal (try: nyrasview status)")
    try:
        from . import tui
    except ImportError:
        sys.exit("the terminal UI needs curses. On Windows: pip install windows-curses  (or use: nyrasview gui)")
    from .collector import Collector
    tui.run(Collector(interval=a.interval, relay=a.relay))


def cmd_now(a):
    s = snap()
    any_ = False
    for g in s["agents"]:
        for w in g.get("work") or []:
            any_ = True
            act = w.get("activity") or {}
            proj = (w.get("cwd") or "?").replace(os.path.expanduser("~"), "~")
            state = "collab job" if w.get("collab") else act.get("state", "running")
            mark = {"working": "●", "waiting": "◐", "idle": "○"}.get(state, "●")
            print(f"{col(g['hex'], mark + ' ' + g['name'])}  {proj}  " + dim(f"{state} · pid {w['pid']}" +
                  (f" · {ago(act.get('at'))} ago" if act.get("at") else "")))
            if act.get("title"):
                print(f"   {act['title']}")
            if act.get("prompt"):
                print(dim("   you ▸ ") + act["prompt"].replace("\n", " ")[:160])
            if act.get("action") or act.get("reply"):
                print(dim("   now ▸ " if state == "working" else "   last ▸ ") + (act.get("action") or act.get("reply"))[:160])
    if not any_:
        print("no agents running")


def cmd_local(a):
    from . import local
    if a.local_cmd == "ask":
        text, model = local.chat(a.server, a.prompt, a.model)
        print(dim(f"[{a.server} · {model}]"))
        return print(text)
    rows = []
    for srv in local.scan():
        rows.append([col(BY_KEY[srv["key"]].hex if srv["key"] in BY_KEY else "#afd7af", srv["name"]),
                     "up" if srv["up"] else dim("down"), srv["url"], len(srv["models"]) or "",
                     ", ".join(m["id"] for m in srv["loaded"]) or "",
                     ", ".join(m["id"] for m in srv["models"][:5])])
    table(["SERVER", "STATE", "URL", "MODELS", "LOADED", "AVAILABLE"], rows)
    print(dim("\nadd any OpenAI-compatible server in config.toml under [[local]] (name, url, model, api_key)"))
    print(dim("try one:  nyrasview local ask ollama 'hello'   ·   use in collab:  collab assign doc.md Intro ollama '...'"))


def cmd_handoff(a):
    from . import handoff
    cwd = os.path.realpath(a.dir)
    src = a.source
    if not src:
        for k in ("claude", "codex"):
            if handoff.detect(k, cwd):
                src = k
                break
    if not src:
        sys.exit("no Claude Code or Codex session found for this folder; pass --from")
    h = handoff.prepare(src, cwd, a.to)
    ctx = h["ctx"]
    lim = ctx.get("limit")
    from . import fallback
    print(f"{col(BY_KEY[src].hex, BY_KEY[src].name)} → {col(fallback.color(h['to']), fallback.label(h['to']))}  "
          + dim(f"{len(ctx['prompts'])} prompts · {len(ctx['edits'])} files changed · {len(ctx['commands'])} commands"))
    if lim:
        print(col("#f87171", f"limit: {lim['message']}") + (dim(f"  resets {lim['reset']}") if lim.get("reset") else ""))
    print(dim(f"wrote {h['path']}"))
    if a.dry_run:
        return
    if a.window:
        r = handoff.launch(h, "terminal")
        print(dim(f"opened {fallback.label(h['to'])} in {r['terminal']}"))
    else:
        handoff.launch(h, "here")


def cmd_relay(a):
    from . import handoff
    from . import fallback
    ts = handoff.targets()
    print(f"relay on · watching Claude Code and Codex for usage limits · next in line: " +
          (", ".join(fallback.label(k) for k in ts) or col("#f87171", "nothing installed")))

    def ev(kind, d):
        if kind == "handoff":
            print(f"⇢ {BY_KEY[d['from']].name} hit its limit in {d['cwd']} → {fallback.label(d['to'])}  " + dim(d["path"]), flush=True)
        else:
            print(col("#f87171", f"✗ {d['from']} in {d['cwd']}: {d['error']}"), flush=True)
    handoff.relay(interval=a.interval, auto=not a.no_launch, on_event=ev)


def cmd_hook(a):
    from . import feedback
    feedback.hook()


def cmd_hooks(a):
    from . import feedback
    if a.hooks_cmd == "install":
        cmd = feedback.install()
        print(f"installed Claude Code hooks → {cmd}\n" + dim("backup saved next to ~/.claude/settings.json · restart running Claude sessions to pick it up"))
    elif a.hooks_cmd == "uninstall":
        feedback.uninstall()
        print("removed nyrasview hooks from ~/.claude/settings.json")
    else:
        print("installed" if feedback.installed() else "not installed (run: nyrasview hooks install)")


def cmd_say(a):
    from . import feedback
    cwd = os.path.realpath(a.dir)
    it = feedback.add(a.agent, cwd, a.text, {"file": a.file} if a.file else None)
    print({"queued": "queued, Claude Code gets it after its next step",
           "copied": "copied to clipboard, paste it into the agent"}.get(it["status"], it["status"]))


def cmd_ask(a):
    from . import runs
    ags = [x.strip() for x in a.agent.split(",") if x.strip()]
    for g in ags:
        if g not in BY_KEY:
            sys.exit(f"unknown agent {g}; see: nyrasview agents")
    group = os.urandom(3).hex() if len(ags) > 1 else None
    started = [runs.start(g, a.prompt, a.dir, a.mode, a.model, group, a.cont, sandboxed=True if a.sandbox else None) for g in ags]
    seen = {m["id"]: 0 for m in started}
    tools = {}
    try:
        while True:
            busy = False
            for m in started:
                v = runs.view(m["id"])
                g = BY_KEY[v["agent"]]
                tag = col(g.hex, g.name) + " " if len(started) > 1 else ""
                for e in v["events"][seen[m["id"]]:]:
                    if e["k"] == "text":
                        print(tag + e["text"].strip(), flush=True)
                    elif e["k"] == "tool":
                        tools[e.get("id")] = e
                        print(tag + dim(f"  ⚙ {e['name']} {e.get('detail', '')}"[:160]), flush=True)
                    elif e["k"] == "result" and not e.get("ok"):
                        print(tag + col("#f87171", "  ✗ " + (e.get("out") or "")[:160]), flush=True)
                seen[m["id"]] = len(v["events"])
                if v["status"] == "running":
                    busy = True
                elif v.get("retried_as") is None and v["status"] == "failed":
                    time.sleep(0.3)
                    v = runs.view(m["id"])
                if v.get("retried_as") and v["retried_as"] not in seen:
                    print(col("#f59e0b", f"{g.name} hit its plan limit · retrying on your API key (run {v['retried_as']})"), flush=True)
                    nm = runs.load(v["retried_as"])
                    started.append(nm)
                    seen[nm["id"]] = 0
                    busy = True
                elif not v.get("_shown"):
                    pass
            if not busy:
                break
            time.sleep(0.4)
    except KeyboardInterrupt:
        for m in started:
            runs.stop(m["id"])
        print(dim("stopped"))
        return
    for m in started:
        v = runs.view(m["id"])
        g = BY_KEY[v["agent"]]
        bits = [v["status"], f"{v['elapsed']:.0f}s"]
        if v.get("tokens"):
            bits.append(f"{hn(v['tokens'])} tok")
        if v.get("cost"):
            bits.append(money(v["cost"]))
        line = f"{g.name}: " + " · ".join(bits) + f" · run {v['id']}"
        print(col("#f87171", line + " · " + (v.get("error") or "")[:200]) if v["status"] == "failed" else dim(line))
    if len(started) == 1:
        print(dim(f"follow up:  nyrasview ask {ags[0]} \"…\" -c {started[0]['id']}"))


def cmd_runs(a):
    from . import runs
    rows = []
    for r in runs.listing(a.limit):
        g = BY_KEY.get(r["agent"])
        rows.append([dim(r["id"]), col(g.hex if g else None, g.name if g else r["agent"]), r["status"], r["mode"],
                     ago(r["created"]), r["cwd"].replace(os.path.expanduser("~"), "~"), (r["prompt"] or "").replace("\n", " ")])
    table(["RUN", "AGENT", "STATUS", "MODE", "AGO", "FOLDER", "PROMPT"], rows) if rows else print("no runs yet")


def _bar(u, w=24):
    u = max(0.0, min(1.0, u or 0))
    n = int(round(u * w))
    c = "#22c55e" if u < 0.7 else "#f59e0b" if u < 0.9 else "#ef4444"
    return col(c, "█" * n) + dim("░" * (w - n))


def cmd_limits(a):
    from . import limits
    from .usage import UsageStore
    u = UsageStore()
    u.refresh()
    from . import net
    snap = limits.snapshot(u.records, net.openrouter())
    meta = limits.META
    if a.json:
        return print(json.dumps({"limits": snap, "meta": meta}, indent=2))
    if not snap:
        return print("no limit data yet. Run any prompt from nyrasview (nyrasview ask claude \"hi\") to calibrate Claude.")
    src = {"anthropic": "live from Anthropic", "codex": "from Codex's own logs", "openrouter": "live from OpenRouter",
           "budget": "counted locally against the plan", "tracked": "no known limit, tracked locally"}
    for agent, wins in snap.items():
        g = BY_KEY.get(agent)
        m = meta.get(agent) or {}
        head = col(g.hex if g else None, g.name if g else agent)
        if m.get("plan"):
            head += dim(f"  {m['plan']}")
        s = src.get(m.get("source")) or ("estimated" if agent == "claude" else "")
        head += dim(f"  · {s}" if s else "")
        print(head)
        if m.get("error"):
            print("  " + col("#f59e0b", m["error"]))
        for name, w in wins.items():
            label = w.get("label") or {"five_hour": "5-hour", "seven_day": "weekly"}.get(name, name)
            ut = w.get("utilization")
            line = f"  {label:<13} "
            un = w.get("unit")
            if ut is None and un:
                line += f"{limits.amount(w.get('used'), un)} used" + dim(f" · {limits.amount(w.get('burn_per_hour'), un)}/h now")
                if w.get("projected") and w.get("resets"):
                    line += col("#f59e0b", f"  ~{limits.amount(w.get('projected'), un)} by {time.strftime('%a %H:%M', time.localtime(w['resets']))} at this rate")
            elif ut is None:
                line += dim(f"not calibrated · {hn(w.get('used_tokens'))} tokens in window · {hn(w.get('burn_per_hour'))}/h")
            else:
                line += _bar(ut) + f" {ut * 100:4.0f}%"
                if w.get("cap"):
                    line += f"  {limits.amount(w.get('left'), un)} left" + dim(f" of {limits.amount(w['cap'], un)}")
                if w.get("resets"):
                    line += dim(f"  resets {time.strftime('%a %H:%M', time.localtime(w['resets']))}")
                if w.get("pace") is not None:
                    line += dim(f"  {'↑' if w['pace'] > 0 else '↓'}{abs(w['pace'])} {'over' if w['pace'] > 0 else 'under'} pace")
                if w.get("eta"):
                    line += col("#f59e0b", f"  ~{limits.dur(w['eta'])} left at this pace")
                if w.get("measured_at") and w.get("measured") is not None and abs((w["measured"] or 0) - ut) > 0.005:
                    line += dim(f"  (measured {w['measured'] * 100:.0f}% {ago(w['measured_at'])} ago, estimated since)")
            print(line)


def cmd_checkpoints(a):
    from . import checkpoints
    if a.diff:
        return print(checkpoints.diff(a.dir, a.diff), end="")
    d = checkpoints.listing(a.dir, a.limit)
    if a.json:
        return print(json.dumps(d, indent=2))
    if not d["root"]:
        return print(f"{os.path.realpath(a.dir)} is not inside a git repository, so there are no checkpoints")
    if not d["turns"]:
        return print("no checkpoints yet. They're saved before and after every agent turn from now on." +
                     ("" if checkpoints.settings()["enabled"] else " (turned off in settings: [checkpoints] enabled = false)"))
    print(dim(d["root"]))
    for t in d["turns"]:
        g = BY_KEY.get(t["agent"])
        who = col(g.hex if g else None, (g.name if g else t["agent"] or "?")[:12].ljust(12))
        stat = col("#22c55e", f"+{t['added']}") + " " + col("#ef4444", f"-{t['removed']}")
        tag = dim(" undone") if t["undone"] else ""
        files = ", ".join(t["files"][:3]) + (f" +{len(t['files']) - 3} more" if len(t["files"]) > 3 else "")
        print(f"{dim(t['id'])}  {dim(ago(t['at']).rjust(4))}  {who} {stat}{tag}  {(t['prompt'] or '')[:60]}")
        if files:
            print(f"{' ' * 30}{dim(files)}")
    print(dim("\nnyrasview undo <id> reverts one turn · nyrasview checkpoints --diff <id> shows it · nyrasview restore <id> rewinds everything"))


def cmd_undo(a):
    from . import checkpoints
    r = checkpoints.undo(a.dir, a.id, dry=a.dry_run)
    if not r["ok"]:
        print(col("#ef4444", r["message"]))
        if r.get("conflicts"):
            print("  conflicts in: " + ", ".join(r["conflicts"]))
        print(dim("nothing was changed. You can still rewind the whole repo with: nyrasview restore " + a.id))
        sys.exit(1)
    print(r["message"] + ("" if not r["files"] else ": " + ", ".join(r["files"][:8]) + (" …" if len(r["files"]) > 8 else "")))
    if r.get("checkpoint"):
        print(dim(f"changed your mind? nyrasview undo {r['checkpoint']}"))


def cmd_restore(a):
    from . import checkpoints
    r = checkpoints.restore(a.dir, a.id, before=a.before)
    print(r["message"] + ("" if not r["files"] else ": " + ", ".join(r["files"][:8]) + (" …" if len(r["files"]) > 8 else "")))
    if r.get("checkpoint"):
        print(dim(f"changed your mind? nyrasview undo {r['checkpoint']}"))


def cmd_events(a):
    from . import notify
    for e in notify.recent(a.limit):
        g = BY_KEY.get(e.get("agent"))
        mark = {"approval": "⚠", "limit": "⛔", "limit_soon": "▲", "failed": "✗", "finished": "✓", "done": "✓"}.get(e["kind"], "•")
        print(f"{dim(ago(e['at']).rjust(4))} {mark} {col(g.hex if g else None, e['title'])}  {dim(e['body'][:120])}")


def cmd_notify_test(a):
    from . import notify
    e = notify.emit("finished", "claude", "nyrasview test", "If you can read this, alerts work.", f"test|{time.time()}")
    print("sent" if e else "notifications for this kind are switched off in config")


def cmd_daemon(a):
    from .collector import Collector
    c = Collector(interval=3.0, relay=a.relay)
    if a.relay:
        import threading
        from . import handoff
        threading.Thread(target=handoff.relay, kwargs={"interval": 10.0}, daemon=True).start()
    print(f"nyrasview daemon · alerts on{' · relay on' if a.relay else ''} · ctrl+c to stop", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def cmd_service(a):
    from . import plat
    print(plat.autostart(a.service_cmd == "install", ["daemon"] + (["--relay"] if a.relay else [])))


def cmd_plan(a):
    from . import planner
    who = planner.pick()
    if a.planner:
        ag, _, model = a.planner.partition(":")
        who = {"agent": ag, "model": model or None, "where": "chosen", "size": None}
    elif sys.stdin.isatty() and not a.yes and not a.dry:
        name = lambda w: (BY_KEY[w["agent"]].name if w["agent"] in BY_KEY else w["agent"]) + (f" · {w['model']}" if w.get("model") else "")
        print(dim(planner.ROLE) + "\n")
        print(f"Best planner we found: {col('#22c55e', name(who))} " + dim(f"({who['where']})"))
        ans = input("Use it? [Enter = yes · l = pick another] ").strip().lower()
        if ans.startswith("l") or ans.startswith("n"):
            opts = planner.options()
            for i, o in enumerate(opts, 1):
                print(f"  {i:>2}. {name(o)} " + dim(f"({o['where']})"))
            sel = input("number: ").strip()
            if sel.isdigit() and 1 <= int(sel) <= len(opts):
                who = opts[int(sel) - 1]
                if input("remember this choice? [y/N] ").strip().lower() == "y":
                    planner.remember(who["agent"], who.get("model"))
    print(dim("planner: ") + col(BY_KEY[who["agent"]].hex if who["agent"] in BY_KEY else None, BY_KEY[who["agent"]].name if who["agent"] in BY_KEY else who["agent"])
          + (f" · {who['model']}" if who.get("model") else "") + dim(f"  ({who['where']})"), flush=True)
    if a.dry:
        print(dim("workers: ") + ", ".join(planner.workers()))
        return
    r = planner.make(a.goal, a.doc, a.sections, who, not a.no_review)
    print(f"\n{col('#e879f9', r['plan']['title'])}  " + dim(r["doc"]))
    for t in r["tasks"]:
        g = BY_KEY.get(t["agent"])
        print(f"  {col(g.hex if g else None, (g.name if g else t['agent']).ljust(13))} → {(t['section'] or 'whole document (review)')}: " + dim(t["instruction"][:110]))
    if a.run:
        print()
        a.collab_cmd, a.doc, a.only, a.parallel, a.rerun, a.reset = "run", r["doc"], None, 4, False, False
        return cmd_collab(a)
    print(dim(f"\nedit anything you like, then:  nyrasview collab run \"{r['doc']}\"   (or open it in the GUI)"))


def cmd_keys(a):
    from . import fallback
    if a.provider:
        val = a.value if a.value is not None else __import__("getpass").getpass(f"{fallback.PROVIDERS[a.provider]['name']} API key (empty to remove): ")
        fallback.save(a.provider, val)
    for s in fallback.status():
        st = col("#22c55e", "connected") + dim(f" ({s['source']}, {s['hint']})") if s["connected"] else dim("not connected")
        print(f"{s['name']:<11} {st}  " + dim(f"→ {s['for']}"))
    print(dim("\nconnect:  nyrasview keys anthropic   (prompts for the key; stored in your config file, readable only by you)"))
    print(dim("with a key, relay continues the same Claude Code / Codex session on API billing when the plan limit hits"))


def cmd_skill(a):
    from . import skillman
    to = [x for x in (getattr(a, "to", "") or "").split(",") if x]
    if to == ["all"]:
        to = [t["key"] for t in skillman.targets() if t["installed"]]
    if a.skill_cmd == "add":
        r = skillman.fetch(a.source)
        names = [x for x in (a.only or "").split(",") if x] or None
        if len(r["skills"]) > 1 and not names and not a.all:
            for s in r["skills"]:
                print(f"  {s['folder']:<28} " + dim(s["description"][:90]))
            sys.exit(dim(f"\n{len(r['skills'])} skills found: pick some with --only a,b or install everything with --all"))
        for d in skillman.install(r["id"], names, to):
            print("installed " + d.replace(os.path.expanduser("~"), "~"))
    elif a.skill_cmd == "new":
        body = sys.stdin.read() if not sys.stdin.isatty() else (a.body or "")
        for d in skillman.create(a.name, a.description or "", body, to):
            print("created " + d.replace(os.path.expanduser("~"), "~"))
    elif a.skill_cmd == "copy":
        for d in skillman.copy(a.folder, a.src, to):
            print("copied to " + d.replace(os.path.expanduser("~"), "~"))
    elif a.skill_cmd == "rm":
        tg = to or list(skillman.TARGETS)
        for d in skillman.remove(a.folder, tg):
            print("removed " + d.replace(os.path.expanduser("~"), "~"))
    else:
        m = skillman.installed()
        cols = [t["key"] for t in m["targets"] if t["installed"] or any(t["key"] in s["agents"] for s in m["skills"])]
        table(["SKILL", *[c.upper() for c in cols], "DESCRIPTION"],
              [[s["folder"], *[col("#22c55e", "✓") if c in s["agents"] else dim("·") for c in cols], s["description"][:60]] for s in m["skills"]])


def cmd_heal(a):
    from . import feedback, heal
    cwd = os.path.realpath(a.dir)
    if a.live is not None:
        cmd = a.live or a.check or heal.detect(cwd)
        if a.off:
            heal.set_project(cwd, None)
            return print(f"live self-heal off for {cwd}")
        if not cmd:
            sys.exit("pass a check command, e.g. nyrasview heal --live \"npm test\"")
        heal.set_project(cwd, cmd)
        if not feedback.installed():
            feedback.install()
            print(dim("installed the Claude Code hook (restart running Claude sessions once)"))
        return print(f"live self-heal on: when Claude Code finishes in {cwd.replace(os.path.expanduser('~'), '~')}, nyrasview runs `{cmd}` "
                     f"and sends failures back, up to {heal.settings()['tries']} times")
    if a.list:
        for j in heal.listing(15):
            mark = {"passed": col("#22c55e", "✓"), "failed": col("#ef4444", "✗")}.get(j["status"], "…")
            print(f"{mark} {dim(j['id'])} {j['cmd']}  " + dim(f"{j['cwd'].replace(os.path.expanduser('~'), '~')} · {len(j['attempts'])} checks · {ago(j['created'])}"))
        return
    g = BY_KEY.get(a.agent or heal.settings()["agent"])

    def ev(kind, j):
        last = j["attempts"][-1] if j["attempts"] else {}
        if kind == "check":
            print(dim(f"▶ check {len(j['attempts']) + 1}: {j['cmd']}"), flush=True)
        elif kind == "passed":
            fixes = len(j["attempts"]) - 1
            print(col("#22c55e", f"✓ passes{f' after {fixes} fix' + ('es' if fixes > 1 else '') if fixes else ''}") + dim(f"  ({last['check']['dur']}s)"), flush=True)
        elif kind == "failing":
            tail = "\n".join(last["check"]["tail"].splitlines()[-6:])
            print(col("#ef4444", f"✗ exit {last['check']['code']}") + dim(f"  ({last['check']['dur']}s)\n" + "\n".join("    " + l for l in tail.splitlines())), flush=True)
        elif kind == "fixing":
            print(f"⚙ {g.name if g else j['agent']} is fixing it " + dim(f"(try {len(j['attempts'])}/{j['tries']}, run {last.get('run')})"), flush=True)
        elif kind == "failed":
            print(col("#ef4444", f"✗ {j.get('error')}"), flush=True)
    try:
        j = heal.start(a.dir, a.check, a.agent, a.tries, a.mode, a.model, True if a.sandbox else None, background=False, on_event=ev)
    except KeyboardInterrupt:
        return
    if j["status"] != "passed":
        sys.exit(1)


def cmd_doctor(a):
    import shutil
    import subprocess
    import tomllib
    from . import feedback, plat, tunnel, web
    from .registry import AGENTS
    from .util import CONFIG
    issues = 0

    def line(ok, label, detail="", fix=""):
        nonlocal issues
        mark = col("#22c55e", "✓") if ok is True else col("#f59e0b", "!") if ok is None else col("#ef4444", "✗")
        if ok is False:
            issues += 1
        print(f" {mark} {label}" + (dim(f"  {detail}") if detail else ""))
        if fix and ok is not True:
            print(f"     → {fix}")

    print(col("#e879f9", "nyrasview doctor") + dim(f"  {VERSION} · Python {sys.version.split()[0]} · {sys.platform}\n"))
    found = [g for g in AGENTS if g.kind != "api" and g.installed()]
    line(bool(found), "AI agents", ", ".join(g.name for g in found) or "none found",
         "install Claude Code, Codex, Gemini CLI or opencode")
    try:
        if CONFIG.exists():
            tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        line(True, "config file", str(CONFIG).replace(os.path.expanduser("~"), "~") if CONFIG.exists() else "none yet (defaults)")
    except tomllib.TOMLDecodeError as e:
        line(False, "config file", f"invalid TOML: {e}", f"fix it on the Settings page or in {CONFIG}")
    if any(g.key == "claude" for g in found):
        line(True if feedback.installed() else None, "Claude Code hook", "on" if feedback.installed() else "off",
             "nyrasview hooks install  (instant alerts, live notes, live self-heal)")
        from . import limits
        api = limits.claude_api(force=True)
        if api and api.get("windows"):
            line(True, "real Claude limits", f"{api.get('plan')} · live from Anthropic")
        else:
            line(None, "real Claude limits", (api or {}).get("error") or "no Claude login found",
                 "open Claude Code once so it signs in")
    import psutil
    guis = []
    for pr in psutil.process_iter(["pid", "cmdline", "create_time"]):
        c = " ".join(pr.info["cmdline"] or [])
        if "nyrasview" in c and " gui" in c:
            guis.append(pr.info)
    if guis:
        newest_code = max(f.stat().st_mtime for f in Path(__file__).parent.glob("*.py"))
        stale = [g for g in guis if g["create_time"] < newest_code]
        line(False if stale else True, "GUI", f"{len(guis)} running" + (" · older than the installed code" if stale else ""),
             "restart it: Ctrl+C where nyrasview gui runs, then start it again")
    else:
        line(None, "GUI", "not running", "nyrasview gui")
    addrs = web.lan_addresses()
    lan = [x for x in addrs if x["kind"] != "Tailscale"]
    ts = [x for x in addrs if x["kind"] == "Tailscale"]
    line(bool(lan) or None, "network for your phone", ", ".join(f"{x['ip']} ({x['iface']})" for x in addrs) or "no network",
         "connect this computer to Wi-Fi or Ethernet")
    if lan and shutil.which("iw"):
        try:
            out = subprocess.run(["iw", "dev", lan[0]["iface"], "link"], capture_output=True, text=True, timeout=3).stdout
            ssid = next((l.split(":", 1)[1].strip() for l in out.splitlines() if "SSID" in l), None)
            freq = next((l.split(":", 1)[1].strip() for l in out.splitlines() if "freq" in l), None)
            if ssid:
                band = "2.4 GHz" if freq and freq.startswith("24") else "5 GHz" if freq and freq[:1] in "56" else ""
                line(None, "Wi-Fi", f"{ssid} {band}".strip(),
                     f"your phone must be on \"{ssid}\" too; another band or guest network usually can't reach this computer")
        except (OSError, subprocess.TimeoutExpired):
            pass
    fw = web.firewall_hint(7342, [x["ip"] for x in lan])
    if fw and fw.get("cmd"):
        line(None, f"firewall ({fw['name']})", "on; phones are blocked unless the port is allowed", fw["cmd"])
    line(True if ts else None, "Tailscale", ts[0]["ip"] if ts else "not running", "optional: lets your phone reach it from anywhere")
    t = tunnel.info()
    line(True if t["installed"] else None, "cloudflared", "installed" if t["installed"] else "not installed",
         "optional: the Phone page downloads it with one click for Reach it from anywhere")
    for name in ("git", "node"):
        line(True if shutil.which(name) else None, name, "found" if shutil.which(name) else "missing",
             f"install {name} (used by watch/handoff diffs)" if name == "git" else "needed by npm-based agents and MCP servers")
    if plat.WIN or plat.MAC:
        line(None, "desktop alerts", "allow notifications for Python if your system asks")
    print("\n" + (col("#22c55e", "all good") if not issues else col("#ef4444", f"{issues} thing{'s' if issues > 1 else ''} to fix")) +
          dim("  (! = optional or worth knowing)"))
    if issues:
        sys.exit(1)


def money(c):
    return f"${c:.2f}" if c >= 1 else f"${c:.4f}"


def cmd_obsidian(a):
    from . import obsidian
    vs = obsidian.vaults()
    if not vs:
        return print("no Obsidian vaults found (open Obsidian once so it writes obsidian.json)")
    for v in vs:
        n = sum(1 for _ in obsidian.notes(v["path"]))
        print(f"{col('#a882ff', v['name']):<24} {n:>5} notes  {dim(v['path'])}" + ("  (open)" if v["open"] else ""))
    print(dim("\nuse notes as collab docs:  nyrasview collab new 'Vault:Projects/Plan.md' -s 'Goals,Design'"))
    print(dim("assign from inside a note:  > [!ai] codex  write the API section   then: nyrasview collab watch"))


def cmd_collab(a):
    from . import obsidian
    sub = a.collab_cmd
    if getattr(a, "doc", None):
        a.doc = obsidian.resolve(a.doc)
    if sub == "watch":
        roots = [obsidian.resolve(r) if ":" in r else os.path.expanduser(r) for r in (a.paths or [])]
        if not roots:
            roots = [v["path"] for v in obsidian.vaults()]
        if not roots:
            sys.exit("nothing to watch: pass folders or open a vault in Obsidian first")
        print(f"watching {len(roots)} folder(s) for > [!ai] callouts …  " + dim(", ".join(roots)))

        def wev(kind, t):
            if kind == "queued":
                print(f"✎ {os.path.basename(t['doc'])}: {len(t['tasks'])} task(s)", flush=True)
                return
            g = BY_KEY.get(t["agent"])
            mark = {"start": "▶", "done": "✓", "fail": "✗"}[kind]
            err = "  " + col("#f87171", t.get("error", "")[:160]) if kind == "fail" else ""
            print(f"  {mark} {col(g.hex if g else None, g.name if g else t['agent'])} → {t['section'] or 'whole document'}{err}", flush=True)
        obsidian.watch(roots, a.interval, wev, a.parallel)
        return
    if sub == "new":
        secs = [s for s in (a.sections or "").split(",") if s.strip()]
        path = collab.new_doc(a.doc, a.title or os.path.splitext(os.path.basename(a.doc))[0], secs)
        print(f"created {path} with {len(secs)} sections")
    elif sub == "assign":
        if a.agent not in BY_KEY:
            sys.exit(f"unknown agent {a.agent}; see: nyrasview agents")
        t = collab.assign(a.doc, a.section, a.agent, a.message, a.model)
        print(f"{t['id']}  {BY_KEY[a.agent].name} → {a.section or 'whole document'}")
    elif sub == "whole":
        if a.agent not in BY_KEY:
            sys.exit(f"unknown agent {a.agent}")
        t = collab.assign(a.doc, "", a.agent, a.message, a.model)
        print(f"{t['id']}  {BY_KEY[a.agent].name} → whole document")
    elif sub == "show":
        v = collab.view(a.doc)
        print(col("#e879f9", v["name"]), dim(v["doc"]))
        if v.get("vault"):
            print(dim(f"obsidian vault {v['vault']} · {v['obsidian']}"))
        print(dim("sections: ") + " · ".join(s["title"] for s in v["sections"]))
        for t in v["tasks"]:
            g = BY_KEY.get(t["agent"])
            print(f"  {dim(t['id'])} {t['status']:<8} {col(g.hex if g else None, (g.name if g else t['agent']).ljust(13))} "
                  f"→ {t['section'] or 'whole document'}: {t['instruction'][:70]}")
            if t.get("error"):
                print("           " + col("#f87171", t["error"][:200]))
    elif sub == "run":
        t0 = time.time()

        def ev(kind, t):
            g = BY_KEY.get(t["agent"])
            who = col(g.hex if g else None, g.name if g else t["agent"])
            where = t["section"] or "whole document"
            mark = {"start": "▶", "done": "✓", "fail": "✗"}[kind]
            extra = ""
            if kind == "done":
                extra = dim(f"  {t.get('chars', 0)} chars · {time.time() - t0:.0f}s")
            if kind == "fail":
                extra = "  " + col("#f87171", t.get("error", "")[:200])
            print(f"{mark} {who} → {where}{extra}", flush=True)
        if a.reset:
            collab.reset(a.doc)
        res = collab.run(a.doc, only=a.only, parallel=a.parallel, on_event=ev, rerun=a.rerun)
        if not res:
            print("nothing pending (use --rerun to run everything again)")
    elif sub == "undo":
        print("restored previous version" if collab.undo(a.doc) else "nothing to undo")
    elif sub == "rm":
        collab.remove(a.doc, a.id)
    elif sub == "sync":
        ts = obsidian.sync(a.doc)
        for t in ts:
            print(f"{t['id']}  {t['agent']} → {t['section'] or 'whole document'}: {t['instruction'][:70]}")
        if not ts:
            print("no > [!ai] callouts found")
        elif a.run:
            a.only, a.parallel, a.rerun, a.reset = [t["id"] for t in ts], 4, False, False
            a.collab_cmd = "run"
            return cmd_collab(a)
    elif sub == "list":
        for d in collab.overview():
            done = sum(t["status"] == "done" for t in d["tasks"])
            print(f"{d['name']:<30} {len(d['tasks'])} tasks, {done} done  {dim(d['doc'])}")


def build_parser():
    ap = argparse.ArgumentParser(prog="nyrasview", description="control center for AI-driven development: see, steer and coordinate your AI agents")
    ap.add_argument("-v", "--version", action="version", version=f"nyrasview {VERSION}")
    ap.add_argument("-i", "--interval", type=float, default=1.5)
    ap.add_argument("--relay", action="store_true", help="also run relay mode in the background")
    sp = ap.add_subparsers(dest="cmd")
    sp.add_parser("tui", help="interactive terminal dashboard (default)")
    g = sp.add_parser("gui", help="web dashboard in your browser")
    g.add_argument("-p", "--port", type=int, default=7341)
    g.add_argument("--no-open", action="store_true")
    g.add_argument("--phone", action="store_true", help="also serve it to your phone on Wi-Fi/Tailscale (shows a QR code)")
    g.add_argument("--tunnel", action="store_true", help="reach it from anywhere through a free Cloudflare tunnel")
    s = sp.add_parser("status", help="one-shot summary of every agent")
    s.add_argument("--json", action="store_true")
    s.add_argument("-a", "--all", action="store_true")
    p = sp.add_parser("ps", help="process tree per agent")
    p.add_argument("agent", nargs="?")
    m = sp.add_parser("mcp", help="MCP servers across all agents (add, copy, rm, test)")
    m.add_argument("--json", action="store_true")
    ms = m.add_subparsers(dest="mcp_cmd")
    x = ms.add_parser("add", help="add a server from a command, URL or JSON")
    x.add_argument("what")
    x.add_argument("--name")
    x.add_argument("--to", required=True, help="claude,codex,… or all")
    x = ms.add_parser("copy", help="copy a configured server to other agents")
    x.add_argument("what")
    x.add_argument("--from", dest="src", required=True)
    x.add_argument("--to", required=True)
    x = ms.add_parser("rm", help="remove a server")
    x.add_argument("what")
    x.add_argument("--to", dest="to", default="", help="agents to remove it from (default: all)")
    x = ms.add_parser("test", help="start a server and list its tools")
    x.add_argument("what", help="a configured name (with --from) or a command/URL")
    x.add_argument("--from", dest="src")
    k = sp.add_parser("skills", help="skills, commands, subagents, rules")
    k.add_argument("query", nargs="?")
    k.add_argument("-a", "--agent")
    k.add_argument("--json", action="store_true")
    u = sp.add_parser("usage", help="token usage from local logs")
    u.add_argument("--json", action="store_true")
    u.add_argument("-m", "--models", action="store_true")
    sp.add_parser("agents", help="known agents and how collab calls them")
    c = sp.add_parser("collab", help="several AIs co-writing one document")
    cs = c.add_subparsers(dest="collab_cmd", required=True)
    x = cs.add_parser("new")
    x.add_argument("doc")
    x.add_argument("-t", "--title")
    x.add_argument("-s", "--sections", help="comma separated")
    x = cs.add_parser("assign")
    x.add_argument("doc")
    x.add_argument("section")
    x.add_argument("agent")
    x.add_argument("message")
    x.add_argument("--model")
    x = cs.add_parser("whole")
    x.add_argument("doc")
    x.add_argument("agent")
    x.add_argument("message")
    x.add_argument("--model")
    x = cs.add_parser("show")
    x.add_argument("doc")
    x = cs.add_parser("run")
    x.add_argument("doc")
    x.add_argument("--only", nargs="*")
    x.add_argument("-j", "--parallel", type=int, default=4)
    x.add_argument("--rerun", action="store_true")
    x.add_argument("--reset", action="store_true")
    x = cs.add_parser("undo")
    x.add_argument("doc")
    x = cs.add_parser("rm")
    x.add_argument("doc")
    x.add_argument("id")
    cs.add_parser("list")
    x = cs.add_parser("sync", help="turn > [!ai] callouts in a note into tasks")
    x.add_argument("doc")
    x.add_argument("--run", action="store_true")
    x = cs.add_parser("watch", help="watch Obsidian vaults/folders and run > [!ai] callouts")
    x.add_argument("paths", nargs="*")
    x.add_argument("-n", "--interval", type=float, default=3.0)
    x.add_argument("-j", "--parallel", type=int, default=4)
    sp.add_parser("obsidian", help="list Obsidian vaults")
    sp.add_parser("now", help="what each running agent is working on")
    sp.add_parser("doctor", help="check your setup and say exactly what to fix")
    x = sp.add_parser("heal", help="run a check and let an agent fix it until it passes")
    x.add_argument("check", nargs="?", help="check command (default: detected, e.g. npm test, pytest -q)")
    x.add_argument("-d", "--dir", default=".")
    x.add_argument("-a", "--agent")
    x.add_argument("-n", "--tries", type=int)
    x.add_argument("--mode", choices=["edit", "auto"])
    x.add_argument("-m", "--model")
    x.add_argument("--sandbox", action="store_true")
    x.add_argument("--live", nargs="?", const="", help="also check live Claude Code sessions in this folder when they finish")
    x.add_argument("--off", action="store_true", help="with --live: turn it off")
    x.add_argument("--list", action="store_true")
    sk = sp.add_parser("skill", help="add, create, copy or remove skills for your agents")
    sks = sk.add_subparsers(dest="skill_cmd")
    x = sks.add_parser("add", help="install skills from a GitHub URL, a folder or a SKILL.md URL")
    x.add_argument("source")
    x.add_argument("--to", required=True, help="claude,codex,opencode,… or all")
    x.add_argument("--only", help="comma separated skill folders to install")
    x.add_argument("--all", action="store_true")
    x = sks.add_parser("new", help="create a skill (instructions from --body or stdin)")
    x.add_argument("name")
    x.add_argument("-d", "--description")
    x.add_argument("--body")
    x.add_argument("--to", required=True)
    x = sks.add_parser("copy")
    x.add_argument("folder")
    x.add_argument("--from", dest="src", required=True)
    x.add_argument("--to", required=True)
    x = sks.add_parser("rm")
    x.add_argument("folder")
    x.add_argument("--to", default="", help="agents to remove it from (default: all)")
    sks.add_parser("list")
    x = sp.add_parser("keys", help="connect API keys to keep working when plan limits run out")
    x.add_argument("provider", nargs="?", choices=["anthropic", "openai", "openrouter"])
    x.add_argument("value", nargs="?")
    x = sp.add_parser("plan", help="let the cheapest model split a goal into tasks for your agents")
    x.add_argument("goal")
    x.add_argument("-o", "--doc", help="where to write the plan (default: ./<title>.md)")
    x.add_argument("-n", "--sections", type=int)
    x.add_argument("--planner", help="force a planner, e.g. ollama:qwen2.5:1.5b or claude:haiku")
    x.add_argument("--run", action="store_true", help="start the agents right away")
    x.add_argument("--no-review", action="store_true")
    x.add_argument("--dry", action="store_true", help="only show which planner and workers would be used")
    x.add_argument("-y", "--yes", action="store_true", help="use the best planner without asking")
    x = sp.add_parser("limits", help="usage limit gauges and forecast")
    x.add_argument("--json", action="store_true")
    x = sp.add_parser("inbox", help="recent alerts")
    x.add_argument("-n", "--limit", type=int, default=25)
    sp.add_parser("notify-test", help="send a test alert")
    x = sp.add_parser("daemon", help="run alerts (and relay) in the background without a UI")
    x.add_argument("--relay", action="store_true")
    x = sp.add_parser("service", help="run the daemon on login with systemd")
    x.add_argument("service_cmd", choices=["install", "uninstall"])
    x.add_argument("--relay", action="store_true")
    x.add_argument("--enable", action="store_true", help=argparse.SUPPRESS)
    x = sp.add_parser("ask", help="prompt an agent (or several: claude,codex) and stream the answer")
    x.add_argument("agent")
    x.add_argument("prompt")
    x.add_argument("-d", "--dir", default=".")
    x.add_argument("--mode", choices=["ask", "edit", "auto"], default="ask")
    x.add_argument("-m", "--model")
    x.add_argument("-c", "--continue", dest="cont", metavar="RUN", help="follow up on an earlier run")
    x.add_argument("--sandbox", action="store_true", help="run inside your Docker sandbox image")
    x = sp.add_parser("runs", help="recent prompts started from nyrasview")
    x.add_argument("-n", "--limit", type=int, default=20)
    sp.add_parser("hook", help=argparse.SUPPRESS)
    x = sp.add_parser("hooks", help="Claude Code hooks for live feedback")
    x.add_argument("hooks_cmd", nargs="?", choices=["install", "uninstall", "status"], default="status")
    x = sp.add_parser("checkpoints", help="every agent turn in this repo, with what it changed")
    x.add_argument("-d", "--dir", default=".")
    x.add_argument("-n", "--limit", type=int, default=20)
    x.add_argument("--diff", metavar="ID", help="show what one turn changed")
    x.add_argument("--json", action="store_true")
    x = sp.add_parser("undo", help="revert one agent turn, keeping everything done after it")
    x.add_argument("id")
    x.add_argument("-d", "--dir", default=".")
    x.add_argument("--dry-run", action="store_true", help="only check that it can be undone cleanly")
    x = sp.add_parser("restore", help="rewind the whole repo to a checkpoint (undoable)")
    x.add_argument("id")
    x.add_argument("--before", action="store_true", help="rewind to just before that turn instead of just after it")
    x.add_argument("-d", "--dir", default=".")
    x = sp.add_parser("say", help="send live feedback to the agent working in a folder")
    x.add_argument("text")
    x.add_argument("-a", "--agent", default="claude")
    x.add_argument("-d", "--dir", default=".")
    x.add_argument("-f", "--file")
    x = sp.add_parser("handoff", help="hand the current task to another coding agent")
    x.add_argument("--from", dest="source", choices=["claude", "codex"])
    x.add_argument("--to")
    x.add_argument("-d", "--dir", default=".")
    x.add_argument("-w", "--window", action="store_true", help="open it in a new terminal window")
    x.add_argument("-n", "--dry-run", action="store_true", help="only write .nyrasview/HANDOFF.md")
    x = sp.add_parser("relay", help="auto hand off when an agent runs out of usage")
    x.add_argument("-n", "--interval", type=float, default=10.0)
    x.add_argument("--no-launch", action="store_true", help="only write the handoff and notify")
    lo = sp.add_parser("local", help="local AI servers (Ollama, LM Studio, llama.cpp, vLLM, Jan…)")
    ls = lo.add_subparsers(dest="local_cmd")
    x = ls.add_parser("ask")
    x.add_argument("server")
    x.add_argument("prompt")
    x.add_argument("-m", "--model")
    x = sp.add_parser("phone", help="use nyrasview from your phone: prints a QR code to scan")
    x.add_argument("--tunnel", action="store_true", help="also reachable from anywhere through a Cloudflare tunnel")
    x.add_argument("--rotate", action="store_true", help="new login key; old phone links stop working")
    x = sp.add_parser("watch", help="follow an agent's edits live in the terminal")
    x.add_argument("-a", "--agent", help="claude, codex, gemini… (default: whoever edited this folder last)")
    x.add_argument("-d", "--dir", default=".")
    x.add_argument("-n", "--interval", type=float, default=1.5)
    x.add_argument("--once", action="store_true", help="print one frame and exit")
    st = sp.add_parser("settings", help="see and change settings (same as the GUI Settings page)")
    st.add_argument("-v", "--verbose", action="store_true", help="show what each setting does")
    ss = st.add_subparsers(dest="settings_cmd")
    ss.add_parser("list", help="every setting and its value")
    x = ss.add_parser("get", help="print one setting")
    x.add_argument("key", help="section.name, e.g. notify.soon_pct")
    x = ss.add_parser("set", help="change one setting")
    x.add_argument("key")
    x.add_argument("value", nargs="+")
    x = ss.add_parser("unset", help="back to the default")
    x.add_argument("key")
    ss.add_parser("edit", help="open the config file in $EDITOR (checked before saving)")
    ss.add_parser("path", help="where the config file is")
    sb = sp.add_parser("sandbox", help="your Docker sandbox for agents")
    sbs = sb.add_subparsers(dest="sandbox_cmd")
    sbs.add_parser("test", help="start the image and list the agents inside")
    x = sp.add_parser("find", help="search every past agent session (prompts, replies, commands, files)")
    x.add_argument("query", nargs="+")
    x.add_argument("-a", "--agent")
    x.add_argument("-d", "--dir", help="only sessions in this folder")
    x.add_argument("--since", help="e.g. 24h, 7d, 2w")
    x.add_argument("-n", "--limit", type=int, default=10)
    x.add_argument("--json", action="store_true")
    x = sp.add_parser("review", help="go through agent changes part by part: accept or take back each one")
    x.add_argument("id", nargs="?", help="one turn (default: every turn with something to review)")
    x.add_argument("-d", "--dir", default=".")
    x.add_argument("-l", "--list", action="store_true", help="only list what's waiting")
    x = sp.add_parser("completion", help="shell tab completion: nyrasview completion fish | source")
    x.add_argument("shell", choices=["fish", "bash", "zsh"])
    return ap


def main():
    if os.name == "nt" and not sys.flags.utf8_mode and not os.environ.get("NYRA_NO_REEXEC"):
        import subprocess
        try:
            sys.exit(subprocess.call([sys.executable, "-X", "utf8", "-m", "nyrasview", *sys.argv[1:]],
                                     env=dict(os.environ, NYRA_NO_REEXEC="1")))
        except KeyboardInterrupt:
            sys.exit(130)
    from . import cli_more
    a = build_parser().parse_args()
    fn = {"phone": cli_more.cmd_phone, "watch": cli_more.cmd_watch, "settings": cli_more.cmd_settings,
          "sandbox": cli_more.cmd_sandbox, "find": cli_more.cmd_find, "review": cli_more.cmd_review, "completion": cli_more.cmd_completion,"tui": cmd_tui, "gui": cmd_gui, "status": cmd_status, "ps": cmd_ps, "mcp": cmd_mcp,
          "skills": cmd_skills, "usage": cmd_usage, "agents": cmd_agents, "collab": cmd_collab, "obsidian": cmd_obsidian, "now": cmd_now, "doctor": cmd_doctor, "heal": cmd_heal, "skill": cmd_skill, "keys": cmd_keys, "plan": cmd_plan, "limits": cmd_limits, "checkpoints": cmd_checkpoints, "undo": cmd_undo, "restore": cmd_restore, "inbox": cmd_events, "notify-test": cmd_notify_test,
          "daemon": cmd_daemon, "service": cmd_service, "ask": cmd_ask, "runs": cmd_runs, "hook": cmd_hook, "hooks": cmd_hooks, "say": cmd_say, "handoff": cmd_handoff, "relay": cmd_relay, "local": cmd_local,
          None: cmd_tui}[a.cmd]
    if a.relay and a.cmd in (None, "tui", "gui"):
        import threading
        from . import handoff
        threading.Thread(target=handoff.relay, kwargs={"interval": 10.0}, daemon=True).start()
    try:
        fn(a)
    except KeyboardInterrupt:
        pass
    except (FileExistsError, FileNotFoundError, ValueError, RuntimeError) as e:
        sys.exit(f"nyrasview: {e}")
