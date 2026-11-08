import curses
import os
import textwrap
import time

from .registry import BY_KEY
from .util import ago, hn

LWIN = {"five_hour": "5-hour", "seven_day": "weekly"}


class Cache:
    def __init__(self):
        self.data = {}

    def get(self, key, fn, every=1.0):
        hit = self.data.get(key)
        now = time.time()
        if hit and now - hit[0] < every:
            return hit[1]
        try:
            val = fn()
        except Exception:
            val = hit[1] if hit else None
        self.data[key] = (now, val)
        return val


def menu(ui, title, options, hint="↑↓ choose · enter pick · esc cancel"):
    H, W = ui.scr.getmaxyx()
    width = min(W - 4, max(len(title) + 6, max((len(o["label"]) + len(o.get("note", "")) + 8 for o in options), default=30), 50))
    height = min(H - 4, len(options) + 5)
    y0, x0 = max(1, (H - height) // 2), max(1, (W - width) // 2)
    sel = next((i for i, o in enumerate(options) if o.get("ok", True)), 0)
    ui.scr.timeout(-1)
    try:
        while True:
            for r in range(height):
                ui.put(y0 + r, x0, " " * width, ui.A("bar") if r == 0 else 0)
            ui.put(y0, x0 + 1, f" {title} ", ui.A("bar", bold=True))
            for i, o in enumerate(options[:height - 4]):
                ok = o.get("ok", True)
                mark = "›" if i == sel else " "
                attr = (curses.A_REVERSE if i == sel else 0) | (ui.A("dim") if not ok else 0)
                x = ui.put(y0 + 2 + i, x0 + 1, f"{mark} {i + 1}. {o['label']}"[:width - 2], attr)
                if o.get("note"):
                    ui.put(y0 + 2 + i, x + 2, o["note"][:max(0, x0 + width - x - 2)], ui.A("dim"))
            ui.put(y0 + height - 1, x0 + 1, hint[:width - 2], ui.A("dim"))
            ui.scr.refresh()
            k = ui.scr.getch()
            if k in (27, ord("q")):
                return None
            if k == curses.KEY_UP:
                sel = (sel - 1) % len(options)
            elif k == curses.KEY_DOWN:
                sel = (sel + 1) % len(options)
            elif ord("1") <= k <= ord("9") and k - ord("1") < len(options):
                sel = k - ord("1")
                if options[sel].get("ok", True):
                    return sel
            elif k in (10, 13, curses.KEY_ENTER) and options[sel].get("ok", True):
                return sel
    finally:
        ui.scr.timeout(250)


def limit_lines(ui, s, key, y, W):
    wins = (s.get("limits") or {}).get(key)
    if not wins:
        return y
    meta = (s.get("limits_meta") or {}).get(key) or {}
    from .limits import amount, dur
    src = {"anthropic": "live from Anthropic", "codex": "from Codex logs", "openrouter": "live from OpenRouter",
           "budget": "counted locally", "tracked": "tracked locally"}
    head = "LIMITS " + (meta.get("plan") or "") + "  " + (src.get(meta.get("source")) or ("estimated" if key == "claude" else ""))
    ui.put(y, 0, head, ui.A("dim", True))
    y += 1
    for name, w in wins.items():
        u = w.get("utilization")
        un = w.get("unit")
        if u is None and un:
            x = ui.put(y, 2, f"{(w.get('label') or name):<14}", ui.A("dim"))
            txt = f"{amount(w.get('used'), un)} used · {amount(w.get('burn_per_hour'), un)}/h now"
            if w.get("projected") and w.get("resets"):
                txt += f" · ~{amount(w.get('projected'), un)} by {time.strftime('%H:%M', time.localtime(w['resets']))} at this rate"
            ui.put(y, x, txt, 0)
            y += 1
            continue
        if u is None:
            continue
        bar_w = 20
        n = int(round(min(1, u) * bar_w))
        col = "bad" if u >= 0.9 or w.get("severity") == "critical" else "warn" if u >= 0.7 or (w.get("pace") or 0) > 10 else "good"
        x = ui.put(y, 2, f"{(w.get('label') or LWIN.get(name, name)):<14}", ui.A("dim"))
        x = ui.put(y, x, "█" * n, ui.A(col))
        x = ui.put(y, x, "░" * (bar_w - n), ui.A("dim"))
        bits = [f" {u * 100:3.0f}%"]
        if w.get("cap"):
            bits.append(f"{amount(w.get('left'), un)} left")
        if w.get("resets"):
            left = max(0, w["resets"] - time.time())
            bits.append(f"resets in {int(left // 3600)}h {int(left % 3600 // 60)}m")
        if w.get("pace") is not None:
            bits.append(f"{'↑' if w['pace'] > 0 else '↓'}{abs(w['pace'])} {'over' if w['pace'] > 0 else 'under'} pace")
        if w.get("eta"):
            bits.append(f"~{dur(w['eta'])} left at this pace")
        ui.put(y, x, "  ".join(bits), ui.A("warn") if w.get("eta") else 0)
        y += 1
    return y


def runs_rows(ui):
    from . import heal, runs
    rs = ui.cache.get("runs", lambda: runs.listing(30), 1.0) or []
    hs = ui.cache.get("heal", lambda: heal.listing(15), 1.0) or []
    rows = [("heal", j["created"], j) for j in hs] + [("run", r["created"], r) for r in rs]
    rows.sort(key=lambda r: -r[1])
    return rows[:40]


def t_runs(ui, s):
    from . import runs
    H, W = ui.scr.getmaxyx()
    rows = runs_rows(ui)
    if not rows:
        ui.put(3, 2, "nothing yet · press p to prompt an agent, f to let one fix a failing check", ui.A("dim"))
        return
    top = min(len(rows) + 1, max(5, (H - 4) // 3))
    sel, off = ui._scroll(len(rows), top - 1)
    cols = [("KIND", 6), ("STATUS", 10), ("AGENT", 13), ("AGO", 5), ("WHAT", max(10, W - 40))]
    data, attrs = [], []
    for kind, _, r in rows:
        st = r["status"]
        live = st in ("running", "checking", "fixing", "queued")
        label = f"{ui.spin()} {st}" if live else ("✓ " + st if st in ("done", "passed") else "✗ " + st if st in ("failed", "stopped") else st)
        what = (r.get("cmd") if kind == "heal" else (r.get("label") or r.get("prompt"))) or ""
        a = BY_KEY.get(r["agent"])
        data.append([kind if kind == "heal" else "prompt", (label, ui.A("good" if st in ("done", "passed") else "bad" if st in ("failed", "stopped") else "warn")),
                     (a.name if a else r["agent"], ui.A("a:" + r["agent"], True)), ago(r["created"]), what.replace("\n", " ")])
        attrs.append(0)
    ui.table(2, 2 + top, cols, data, sel, off, attrs)
    kind, _, r = rows[sel]
    y = 3 + top
    ui.put(y, 0, "─" * W, ui.A("dim"))
    y += 1
    lines = []
    if kind == "heal":
        lines.append((f"{r['cmd']}  ·  {r['cwd'].replace(os.path.expanduser('~'), '~')}  ·  {r['tries']} tries", ui.A("accent", True)))
        for at in r["attempts"]:
            c = at["check"]
            lines.append((f"check {at['n']}: " + ("✓ passes" if c["ok"] else f"✗ exit {c['code']}") + f"  ({c['dur']}s)", ui.A("good" if c["ok"] else "bad", True)))
            if not c["ok"]:
                for l in c["tail"].splitlines()[-5:]:
                    lines.append(("    " + l, ui.A("dim")))
            if at.get("run"):
                lines.append((f"  ⚙ fix ({at.get('fix') or 'running'}): {(at.get('said') or 'working on it…')}", ui.A("cyan")))
        if r.get("error"):
            lines.append((r["error"], ui.A("bad")))
    else:
        v = ui.cache.get("run:" + r["id"], lambda: runs.view(r["id"]), 0.8) or {}
        lines.append((f"{v.get('mode', '')} · {(v.get('cwd') or '').replace(os.path.expanduser('~'), '~')}" + (" · on API key" if v.get("api") else "") + (" · sandbox" if v.get("sandbox") else ""), ui.A("dim")))
        for t in (v.get("turns") or [])[:-1]:
            lines.append(("you ▸ " + t["prompt"], ui.A("accent")))
            if t.get("answer"):
                lines.append((t["answer"][:400], ui.A("dim")))
        lines.append(("you ▸ " + (v.get("prompt") or ""), ui.A("accent", True)))
        for e in v.get("events") or []:
            if e["k"] == "text":
                for para in e["text"].split("\n"):
                    lines.append((para, 0))
            elif e["k"] == "tool":
                lines.append((f"  ⚙ {e['name']} {e.get('detail', '')}", ui.A("cyan")))
            elif e["k"] == "result" and not e.get("ok"):
                lines.append(("    ✗ " + (e.get("out") or "")[:200], ui.A("bad")))
        if v.get("status") == "failed":
            lines.append(("✗ " + (v.get("error") or "failed"), ui.A("bad")))
        elif v.get("status") == "done":
            bits = [f"{v.get('elapsed', 0):.0f}s"] + ([f"{hn(v['tokens'])} tok"] if v.get("tokens") else []) + ([f"${v['cost']:.4f}"] if v.get("cost") else [])
            lines.append(("✓ done · " + " · ".join(bits), ui.A("good")))
        if v.get("retried_as"):
            lines.append((f"hit the plan limit · retried on your API key (run {v['retried_as']})", ui.A("warn")))
    wrapped = []
    for text, attr in lines:
        for chunk in textwrap.wrap(text, max(20, W - 2)) or [""]:
            wrapped.append((chunk, attr))
    room = H - y - 1
    for text, attr in wrapped[-room:] if len(wrapped) > room else wrapped:
        ui.put(y, 1, text, attr)
        y += 1


def latest_alert(ui):
    from . import notify
    ev = ui.cache.get("alert", lambda: notify.recent(1, time.time() - 900), 2.0) or []
    if not ev:
        return None
    e = ev[-1]
    return e


def runs_key(ui, k, s):
    from . import heal, runs
    rows = runs_rows(ui)
    if k == ord("p"):
        return prompt_menu(ui, s)
    if k == ord("f"):
        return heal_menu(ui, s, None)
    if not rows:
        return
    kind, _, r = rows[max(0, min(ui.sel[6], len(rows) - 1))]
    if k == ord("s"):
        (heal.stop if kind == "heal" else runs.stop)(r["id"])
        ui.flash("stopped")
    elif k in (10, 13, curses.KEY_ENTER) and kind == "run" and r["status"] != "running":
        text = ui.ask(f"follow up with {BY_KEY[r['agent']].name if r['agent'] in BY_KEY else r['agent']}: ")
        if text:
            m = runs.start(r["agent"], text, r["cwd"], r["mode"], r.get("model"), None, r["id"])
            ui.flash(f"run {m['id']} started")
            ui.sel[6] = 0


def prompt_menu(ui, s, agent=None, cwd=None):
    from . import runs
    if not agent:
        heads = [a for a in s["agents"] if a["headless"] and (a["installed"] or a["kind"] == "api")]
        if not heads:
            ui.flash("no agent can be prompted from here")
            return
        i = menu(ui, "Prompt which agent?", [{"label": a["name"], "note": "live" if a["running"] else ""} for a in heads])
        if i is None:
            return
        agent = heads[i]["key"]
        work = [w for w in heads[i].get("work") or [] if w.get("cwd") and not w.get("collab")]
        cwd = work[0]["cwd"] if work else os.getcwd()
    text = ui.ask(f"prompt in {cwd.replace(os.path.expanduser('~'), '~')}: ")
    if not text:
        return
    j = menu(ui, "Mode", [{"label": "Ask", "note": "read-only"}, {"label": "Edit", "note": "may change files"},
                          {"label": "Auto", "note": "runs commands without asking"}])
    if j is None:
        return
    m = runs.start(agent, text, cwd, ["ask", "edit", "auto"][j])
    ui.tab, ui.sel[6] = 6, 0
    ui.flash(f"run {m['id']} started" if m["status"] == "running" else f"failed: {m.get('error')}")


def heal_menu(ui, s, a):
    from . import heal
    cwd = None
    if a:
        work = [w for w in a.get("work") or [] if w.get("cwd") and not w.get("collab")]
        cwd = work[0]["cwd"] if work else None
    cwd = ui.ask(f"folder [{(cwd or os.getcwd()).replace(os.path.expanduser('~'), '~')}]: ") or cwd or os.getcwd()
    cwd = os.path.expanduser(cwd)
    guess = heal.project_cmd(cwd) or heal.detect(cwd) or ""
    cmd = ui.ask(f"check command [{guess}]: ") or guess
    if not cmd:
        ui.flash("need a check command, e.g. npm test")
        return
    fixers = [x for x in s["agents"] if x["headless"] and x["installed"] and x["kind"] != "local"]
    if not fixers:
        ui.flash("no agent installed to fix things")
        return
    i = menu(ui, "Who fixes failures?", [{"label": x["name"]} for x in fixers])
    if i is None:
        return
    j = menu(ui, "How?", [{"label": "Fix until it passes", "note": "now, with this agent"},
                          {"label": "Live", "note": "when Claude Code finishes here, check and send failures back"}])
    if j is None:
        return
    if j == 1:
        from . import feedback
        heal.set_project(cwd, cmd)
        if not feedback.installed():
            feedback.install()
        ui.flash(f"live self-heal on for {os.path.basename(cwd)}: `{cmd}` runs whenever Claude Code finishes there")
        return
    job = heal.start(cwd, cmd, fixers[i]["key"])
    ui.tab, ui.sel[6] = 6, 0
    ui.flash(f"self-heal {job['id']} started: {cmd}")


def handoff_menu(ui, s, a):
    from . import fallback, handoff
    work = [w for w in a.get("work") or [] if w.get("cwd") and not w.get("collab")]
    if a["key"] not in handoff.READERS or not work:
        ui.flash("hand off works from a running Claude Code or Codex session")
        return
    w = next((x for x in work if (x.get("activity") or {}).get("state") == "limited"), work[0])
    if len(work) > 1:
        i = menu(ui, "Which session?", [{"label": x["cwd"].replace(os.path.expanduser("~"), "~"),
                                         "note": ((x.get("activity") or {}).get("title") or "")} for x in work])
        if i is None:
            return
        w = work[i]
    o = handoff.options(a["key"], w["cwd"])
    sm = o["summary"]
    shown = [t for t in o["targets"] if t["ok"] or t.get("fix") == "keys"]
    missing = [t["name"] for t in o["targets"] if t.get("fix") == "install"]
    opts = [{"label": t["name"], "ok": t["ok"], "note": t["why"] or "opens in a new terminal"} for t in shown]
    opts.append({"label": "Only write HANDOFF.md", "note": "no one takes over yet"})
    title = f"Hand off {os.path.basename(w['cwd'])} · {sm['prompts']} prompts, {sm['files']} files"
    i = menu(ui, title, opts, "↑↓ choose · enter hand off · esc cancel" + (f" · not installed: {', '.join(missing[:4])}" if missing else ""))
    if i is None:
        return
    try:
        if i == len(shown):
            h = handoff.prepare(a["key"], w["cwd"], (next((t["key"] for t in shown if t["ok"]), None)) or "codex")
            ui.flash(f"wrote {h['path'].replace(os.path.expanduser('~'), '~')}")
            return
        to = shown[i]["key"]
        h = handoff.prepare(a["key"], w["cwd"], to)
        r = handoff.launch(h)
        ui.flash(f"handed off to {fallback.label(to)} in {r['terminal']}")
    except Exception as e:
        ui.flash(f"hand off failed: {e}")
