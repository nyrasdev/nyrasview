import curses
import os
import signal
import time

from . import collab
from .registry import BY_KEY
from .util import ago, hb, hn

TABS = ["Agents", "MCP", "Skills", "Usage", "Network", "Collab"]
SPARK = " ▁▂▃▄▅▆▇█"
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ROLE = {"main": "●", "collab": "✎", "mcp": "◆", "mcp?": "◇", "shell": "$", "child": "·"}


def spark(vals, width):
    vals = list(vals)[-width:]
    if not vals:
        return ""
    top = max(max(vals), 1e-9)
    return "".join(SPARK[min(8, int(v / top * 8 + 0.5))] for v in vals)


class UI:
    def __init__(self, scr, collector, gui_url=None):
        self.scr = scr
        self.c = collector
        self.gui_url = gui_url
        self.tab = 0
        self.sel = [0] * len(TABS)
        self.off = [0] * len(TABS)
        self.focus = 0
        self.psel = 0
        self.poff = 0
        self.filter = ""
        self.msg = ("", 0)
        self.frame = 0
        self.eased = {}
        self.animating = False
        self.show_all = False
        self.colors = {}
        curses.curs_set(0)
        scr.timeout(250)
        scr.keypad(True)
        self._init_colors()

    def _init_colors(self):
        self.has = curses.has_colors()
        if not self.has:
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        many = curses.COLORS >= 256
        base = {"dim": 244 if many else curses.COLOR_WHITE, "accent": 213 if many else curses.COLOR_MAGENTA,
                "good": 114 if many else curses.COLOR_GREEN, "bad": 203 if many else curses.COLOR_RED,
                "warn": 221 if many else curses.COLOR_YELLOW, "cyan": 81 if many else curses.COLOR_CYAN}
        n = 1
        for k, col in base.items():
            curses.init_pair(n, col, bg)
            self.colors[k] = n
            n += 1
        curses.init_pair(n, curses.COLOR_BLACK, base["accent"])
        self.colors["bar"] = n
        n += 1
        for key, a in BY_KEY.items():
            if n >= curses.COLOR_PAIRS - 1:
                break
            curses.init_pair(n, a.color if many else curses.COLOR_WHITE, bg)
            self.colors["a:" + key] = n
            n += 1

    def A(self, name, bold=False, rev=False):
        a = curses.color_pair(self.colors.get(name, 0)) if self.has else 0
        if bold:
            a |= curses.A_BOLD
        if rev:
            a |= curses.A_REVERSE
        return a

    def put(self, y, x, s, attr=0, w=None):
        H, W = self.scr.getmaxyx()
        if y < 0 or y >= H or x >= W:
            return x
        s = str(s)
        lim = W - x - (1 if y == H - 1 else 0)
        if w is not None:
            s = s[:w].ljust(w)
            lim = min(lim, w)
        s = s[:max(0, lim)]
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass
        return x + len(s)

    def flash(self, text):
        self.msg = (text, time.time())

    def ask(self, prompt):
        H, W = self.scr.getmaxyx()
        self.scr.move(H - 1, 0)
        self.scr.clrtoeol()
        self.put(H - 1, 0, prompt, self.A("accent", True))
        curses.echo()
        curses.curs_set(1)
        self.scr.timeout(-1)
        try:
            raw = self.scr.getstr(H - 1, min(len(prompt), W - 2), 400)
            val = raw.decode("utf-8", "replace").strip()
        except (curses.error, KeyboardInterrupt):
            val = ""
        curses.noecho()
        curses.curs_set(0)
        self.scr.timeout(250)
        return val

    def agents(self, s):
        return [a for a in s["agents"] if a["visible"] or self.show_all]

    def ease(self, key, target):
        v = self.eased.get(key, target)
        v += (target - v) * 0.35
        if abs(target - v) < 0.05:
            v = target
        else:
            self.animating = True
        self.eased[key] = v
        return v

    def spin(self):
        self.animating = True
        return SPIN[self.frame % len(SPIN)]

    def livedot(self, key):
        self.animating = True
        return self.A("a:" + key, bold=(self.frame // 5) % 2 == 0)

    def run(self):
        while True:
            s = self.c.snapshot()
            self.frame += 1
            self.animating = False
            self.scr.erase()
            if s is None:
                self.put(0, 0, "nyrasview: collecting…", self.A("accent", True))
            else:
                try:
                    self.draw(s)
                except curses.error:
                    pass
            self.scr.refresh()
            self.scr.timeout(100 if self.animating else 250)
            k = self.scr.getch()
            if k == -1:
                continue
            if self.key(k, s) is False:
                return

    def header(self, s):
        H, W = self.scr.getmaxyx()
        live = [a for a in s["agents"] if a["running"]]
        tok = sum(a["usage"].get("today", {}).get("out", 0) for a in s["agents"])
        sy = s["system"]
        parts = [f" nyrasview {s['version']} ", f" {s['host']} ", f" cpu {sy['cpu']:.0f}% ",
                 f" ram {hb(sy['mem_used'])}/{hb(sy['mem_total'])} ",
                 f" {len(live)} agents live ", f" {hn(tok)} out-tok today ",
                 *[f" {k} {({'five_hour': '5h', 'seven_day': '7d'}).get(wn, wn)} {w['utilization'] * 100:.0f}% "
                   for k, ws in (s.get("limits") or {}).items() for wn, w in ws.items()
                   if w.get("utilization") is not None and wn == "five_hour"],
                 f" {time.strftime('%H:%M:%S')} "]
        self.put(0, 0, " " * W, self.A("bar"))
        x = 0
        for i, p in enumerate(parts):
            x = self.put(0, x, p, self.A("bar", bold=i == 0))
        x = 0
        for i, t in enumerate(TABS):
            lab = f" {i + 1} {t} "
            x = self.put(1, x, lab, self.A("accent", True, rev=True) if i == self.tab else self.A("dim"))
            x += 1
        msg, t0 = self.msg
        if msg and time.time() - t0 < 4:
            foot = msg
        else:
            foot = {
                0: "↑↓ agent  enter/→ processes  p prompt  h hand off  k kill  a show all  g gui  q quit",
                1: "↑↓ scroll  g gui  q quit",
                2: "↑↓ scroll  / filter  esc clear  g gui  q quit",
                3: "↑↓ agent  g gui  q quit",
                4: "↑↓ scroll  g gui  q quit",
                5: "↑↓ doc  n new  a assign  w whole-doc task  r run  x reset  u undo  g gui  q quit",
            }[self.tab]
        self.put(H - 1, 0, foot, self.A("dim"))

    def draw(self, s):
        self.header(s)
        [self.t_agents, self.t_mcp, self.t_skills, self.t_usage, self.t_net, self.t_collab][self.tab](s)

    def table(self, y0, y1, cols, rows, sel=None, off=0, attrs=None):
        x = 0
        for name, w in cols:
            x = self.put(y0, x, name.ljust(w)[:w] + " ", self.A("dim", True))
        h = y1 - y0 - 1
        for n, row in enumerate(rows[off:off + h]):
            i = n + off
            y = y0 + 1 + n
            base = attrs[i] if attrs else 0
            if sel is not None and i == sel:
                base |= curses.A_REVERSE
            x = 0
            for (name, w), cell in zip(cols, row):
                if isinstance(cell, tuple):
                    text, at = cell
                else:
                    text, at = cell, 0
                x = self.put(y, x, str(text).ljust(w)[:w] + " ", base | at)

    def _scroll(self, n, h):
        t = self.tab
        self.sel[t] = max(0, min(self.sel[t], n - 1))
        if self.sel[t] < self.off[t]:
            self.off[t] = self.sel[t]
        if self.sel[t] >= self.off[t] + h:
            self.off[t] = self.sel[t] - h + 1
        return self.sel[t], self.off[t]

    def t_agents(self, s):
        H, W = self.scr.getmaxyx()
        ag = self.agents(s)
        top = min(len(ag) + 1, max(4, (H - 4) // 3))
        sel, off = self._scroll(len(ag), top - 1)
        cols = [("  AGENT", 16), ("STATE", 8), ("SESS", 4), ("PROCS", 5), ("CPU%", 6), ("RAM", 7),
                ("NET", 5), ("MCP", 7), ("SKILLS", 6), ("TODAY", 7), ("7D", 7), ("ACTIVE", 6),
                ("CPU", max(6, W - 102))]
        rows, attrs = [], []
        for a in ag:
            u = a["usage"]
            live = len(a["mcp_live"])
            state = "live" if a["running"] else ("idle" if a["installed"] or a["kind"] == "api" else "absent")
            rows.append([
                ("● " + a["name"], self.livedot(a["key"]) if a["running"] else self.A("a:" + a["key"], True)),
                (state, self.A("good" if a["running"] else "dim")),
                a["sessions"] or "", a["nprocs"] or "", f"{self.ease('cpu:' + a['key'], a['cpu']):.1f}" if a["running"] else "",
                hb(a["rss"]) if a["rss"] else "", sum(a["providers"].values()) or "",
                f"{live}/{a['mcp_configured']}" if (live or a["mcp_configured"]) else "",
                a["skills"] or "",
                hn(u.get("today", {}).get("out", 0)) if u else "",
                hn(u.get("week", {}).get("out", 0)) if u else "",
                ago(u.get("last")) if u.get("last") else "",
                (spark([h[0] for h in a["hist"]], cols[-1][1]), self.A("a:" + a["key"])),
            ])
            attrs.append(0)
        self.table(2, 2 + top, cols, rows, sel if self.focus == 0 else None, off, attrs)
        if not ag:
            return
        a = ag[sel]
        y = 3 + top
        self.put(y, 0, "─" * W, self.A("dim"))
        self.put(y, 2, f" {a['name']} ", self.A("a:" + a["key"], True))
        x = 4 + len(a["name"]) + 2
        info = []
        if a["binary"]:
            info.append(a["binary"])
        if a["listen"]:
            info.append("listening " + ",".join(map(str, a["listen"][:6])))
        if a["providers"]:
            info.append("net " + " ".join(f"{k}×{v}" for k, v in sorted(a["providers"].items(), key=lambda kv: -kv[1])))
        self.put(y, x, " │ ".join(info), self.A("dim"))
        y += 1
        if a["mcp_live"] or a["mcp_configured"]:
            line = "MCP  "
            cfg = [m for m in s["mcp"] if m["agent"] == a["key"]]
            names = []
            for m in cfg:
                on = m["name"] in a["mcp_live"]
                names.append(("◆ " if on else "◇ ") + m["name"] + (f" ({m['transport']})" if m["transport"] != "stdio" else ""))
            for n, m in a["mcp_live"].items():
                if not any(c["name"] == n for c in cfg):
                    names.append("◆ " + n + "?")
            self.put(y, 0, line + "  ".join(names), self.A("cyan"))
            y += 1
        for w in a.get("work") or []:
            if y >= H - 6:
                break
            act = w.get("activity")
            proj = (w.get("cwd") or "").replace(os.path.expanduser("~"), "~")
            if w.get("collab"):
                self.put(y, 0, f"{self.spin()} collab job · pid {w['pid']}", self.A("warn", True))
                y += 1
                continue
            if not act:
                self.put(y, 0, f"● {proj}", self.A("dim"))
                y += 1
                continue
            state = act["state"]
            if state == "limited":
                lim = act.get("limit") or {}
                self.put(y, 0, f"⚠ {proj} · {lim.get('message', 'limit reached')}" +
                         (f" · resets {lim['reset']}" if lim.get("reset") else "") + "   press h to hand off",
                         self.A("bad", True, rev=(self.frame // 6) % 2 == 0))
                self.animating = True
                y += 1
                continue
            mark = self.spin() if state == "working" else ("◐" if state == "waiting" else "○")
            head = f"{mark} {proj}"
            x = self.put(y, 0, head, self.A("good" if state == "working" else "dim", True))
            label = act.get("title") or ""
            x = self.put(y, x + 1, ("· " + label + " ") if label else "", self.A("a:" + a["key"], True))
            self.put(y, x + 1, f"{state} · {ago(act.get('at'))} ago", self.A("dim"))
            y += 1
            if act.get("prompt"):
                self.put(y, 2, "you ▸ " + act["prompt"].replace("\n", " "), self.A("dim"))
                y += 1
            now = act.get("action") or act.get("reply")
            if now:
                self.put(y, 2, ("now ▸ " if state == "working" else "last ▸ ") + now.replace("\n", " "),
                         self.A("cyan") if state == "working" else self.A("dim"))
                y += 1
        u = a["usage"]
        if u.get("models"):
            self.put(y, 0, "MODELS " + "  ".join(f"{m['model']} {hn(m['tokens'])}" for m in u["models"][:4]), self.A("dim"))
            y += 1
        procs = a["procs"]
        self.psel = max(0, min(self.psel, len(procs) - 1))
        h = H - y - 2
        if self.psel < self.poff:
            self.poff = self.psel
        if self.psel >= self.poff + h:
            self.poff = self.psel - h + 1
        pcols = [("PID", 7), ("ROLE", 12), ("CPU%", 6), ("RAM", 7), ("THR", 4), ("NET", 4),
                 ("COMMAND", max(10, W - 50))]
        prow, pat = [], []
        for p in procs:
            role = ROLE.get(p["role"], "·") + " " + (p["mcp"] or p["role"])
            prow.append([p["pid"], role, p["cpu"], hb(p["rss"]), p["threads"], len(p["conns"]) or "",
                         "  " * min(p["depth"], 6) + p["cmd"]])
            pat.append(self.A("cyan") if p["role"].startswith("mcp") else
                       self.A("warn") if p["role"] == "collab" else
                       self.A("a:" + a["key"], True) if p["role"] == "main" else 0)
        self.table(y, H - 1, pcols, prow, self.psel if self.focus == 1 else None, self.poff, pat)
        if not procs:
            self.put(y + 1, 2, "not running" + ("" if a["installed"] else " · not installed"), self.A("dim"))

    def t_mcp(self, s):
        H, W = self.scr.getmaxyx()
        live = {}
        for a in s["agents"]:
            for n, m in a["mcp_live"].items():
                live[(a["key"], n)] = m
        rows, attrs = [], []
        listed = set()
        for m in s["mcp"]:
            l = live.get((m["agent"], m["name"]))
            listed.add((m["agent"], m["name"]))
            if l:
                st = ("● running", self.A("good"))
            elif m["transport"] != "stdio":
                st = ("◌ remote", self.A("cyan"))
            elif not m["enabled"]:
                st = ("○ disabled", self.A("dim"))
            else:
                st = ("○ idle", self.A("dim"))
            target = m["url"] or " ".join([m["command"] or ""] + m["args"])
            rows.append([(m["agent"], self.A("a:" + m["agent"], True)), m["name"], m["transport"], st,
                         ",".join(map(str, l["pids"][:3])) if l else "", f"{l['cpu']:.1f}" if l else "",
                         hb(l["rss"]) if l else "", m["scope"].replace(os.path.expanduser("~"), "~"), target])
            attrs.append(0)
        for (ak, n), l in live.items():
            if (ak, n) not in listed:
                rows.append([(ak, self.A("a:" + ak, True)), n, "stdio", ("● running?", self.A("warn")),
                             ",".join(map(str, l["pids"][:3])), f"{l['cpu']:.1f}", hb(l["rss"]), "unlisted",
                             "detected from process"])
                attrs.append(0)
        cols = [("AGENT", 11), ("SERVER", 18), ("TYPE", 5), ("STATUS", 11), ("PID", 8), ("CPU%", 5),
                ("RAM", 7), ("SCOPE", 22), ("TARGET", max(10, W - 100))]
        sel, off = self._scroll(len(rows), H - 4)
        self.table(2, H - 1, cols, rows, sel, off, attrs)
        if not rows:
            self.put(4, 2, "no MCP servers configured in any agent", self.A("dim"))

    def t_skills(self, s):
        H, W = self.scr.getmaxyx()
        f = self.filter.lower()
        items = [k for k in s["skills"] if not f or f in (k["name"] + k["description"] + k["agent"] + k["kind"]).lower()]
        items.sort(key=lambda k: (k["agent"], k["kind"], k["name"].lower()))
        rows = [[(k["agent"], self.A("a:" + k["agent"], True) if k["agent"] in BY_KEY else self.A("dim")),
                 k["kind"], k["name"], "proj" if k["scope"].startswith("project") else k["scope"],
                 k["description"]] for k in items]
        cols = [("AGENT", 11), ("KIND", 9), ("NAME", 26), ("SCOPE", 6), ("DESCRIPTION", max(10, W - 58))]
        y0 = 2
        if self.filter:
            self.put(2, 0, f"filter: {self.filter}", self.A("warn"))
            y0 = 3
        sel, off = self._scroll(len(rows), H - y0 - 2)
        self.table(y0, H - 1, cols, rows, sel, off)
        counts = {}
        for k in s["skills"]:
            counts[k["agent"]] = counts.get(k["agent"], 0) + 1
        self.put(H - 2, 0, "  ".join(f"{a}:{n}" for a, n in sorted(counts.items())), self.A("dim"))

    def t_usage(self, s):
        H, W = self.scr.getmaxyx()
        ag = [a for a in s["agents"] if a["usage"]]
        cols = [("  AGENT", 16), ("TODAY in", 9), ("out", 8), ("cache", 8), ("7D in", 9), ("out", 8),
                ("cache", 8), ("ALL out", 9), ("MSGS", 7), ("COST", 8), ("LAST", 5)]
        rows = []
        for a in ag:
            u = a["usage"]
            t, w, al = u["today"], u["week"], u["total"]
            c = al["cost"]
            cost = (f"${c:.2f}" if c >= 1 else f"${c:.4f}") if al.get("has_cost") else "n/a"
            rows.append([("● " + a["name"], self.A("a:" + a["key"], True)), hn(t["in"]), hn(t["out"]),
                         hn(t["cache"]), hn(w["in"]), hn(w["out"]), hn(w["cache"]), hn(al["out"]),
                         al["msgs"], cost, ago(u["last"])])
        sel, off = self._scroll(len(rows), 8)
        n = min(len(rows), 8)
        self.table(2, 3 + n, cols, rows, sel, off)
        y = 4 + n
        orr = s["openrouter"]
        if orr.get("configured"):
            if orr.get("error"):
                self.put(y, 0, f"OpenRouter: {orr['error']}", self.A("bad"))
            else:
                lim = f" / limit ${orr['limit']}" if orr.get("limit") else ""
                self.put(y, 0, f"OpenRouter key {orr.get('label') or ''}: ${orr.get('usage') or 0:.2f} used{lim} · "
                         f"today ${orr.get('daily') or 0:.2f} · week ${orr.get('weekly') or 0:.2f} · "
                         f"month ${orr.get('monthly') or 0:.2f}", self.A("a:openrouter", True))
            y += 1
        if not ag:
            self.put(y, 2, "no local usage logs found yet", self.A("dim"))
            return
        a = ag[sel]
        u = a["usage"]
        y += 1
        self.put(y, 0, f"{a['name']} · last 14 days (all tokens incl. cache)", self.A("a:" + a["key"], True))
        y += 1
        daily = u["daily"]
        top = max(daily) or 1
        bh = max(3, min(10, H - y - 12))
        cw = max(5, min(9, (W - 4) // 14))
        for r in range(bh):
            line = ""
            for v in daily:
                lvl = v / top * bh
                ch = "█" if lvl >= bh - r else "▄" if lvl >= bh - r - 0.5 else " "
                line += ch * (cw - 2) + "  "
            self.put(y + r, 2, line, self.A("a:" + a["key"]))
        y += bh
        days = [time.strftime("%d", time.localtime(time.time() - (13 - i) * 86400)) for i in range(14)]
        self.put(y, 2, "".join(d.ljust(cw) for d in days), self.A("dim"))
        self.put(y + 1, 2, "".join((hn(v) if v else "·").ljust(cw)[:cw - 1] + " " for v in daily), self.A("dim"))
        y += 3
        self.put(y, 0, "MODEL".ljust(34) + "TOKENS".ljust(10) + "OUT".ljust(10) + "MSGS", self.A("dim", True))
        for i, m in enumerate(u["models"][: max(0, H - y - 2)]):
            self.put(y + 1 + i, 0, m["model"][:33].ljust(34) + hn(m["tokens"]).ljust(10) + hn(m["out"]).ljust(10) + str(m["msgs"]))

    def t_net(self, s):
        H, W = self.scr.getmaxyx()
        rows = []
        for a in s["agents"]:
            for h in a["hosts"]:
                rows.append([(a["name"], self.A("a:" + a["key"], True)), (h["provider"], self.A("cyan") if h["provider"] not in ("other", None) else self.A("dim")),
                             h["count"], h["host"]])
            if a["listen"]:
                rows.append([(a["name"], self.A("a:" + a["key"], True)), ("listen", self.A("warn")), "",
                             "ports " + ", ".join(map(str, a["listen"]))])
        for srv in s.get("local", []):
            if not srv["up"]:
                continue
            at = self.A("a:" + srv["key"], True)
            rows.append([(srv["name"], at), ("local ai", self.A("good")), len(srv["models"]),
                         f"{srv['url']}  ·  " + ", ".join(m["id"] for m in srv["models"][:6])])
            for m in srv["loaded"]:
                extra = f"  {hb(m['size'])} ({hb(m['vram'])} vram)" if m.get("size") else ""
                rows.append([(srv["name"], at), ("loaded", self.A("cyan")), "", f"{m['id']}{extra}"])
        cols = [("AGENT", 16), ("PROVIDER", 12), ("CONNS", 6), ("ENDPOINT", max(10, W - 40))]
        sel, off = self._scroll(len(rows), H - 4)
        self.table(2, H - 1, cols, rows, sel, off)
        if not rows:
            self.put(4, 2, "no agent network activity right now", self.A("dim"))

    def t_collab(self, s):
        H, W = self.scr.getmaxyx()
        ds = s["collab"]
        if not ds:
            self.put(3, 2, "no shared documents yet — press n to create one, or run:", self.A("dim"))
            self.put(4, 4, "nyrasview collab new plan.md --title 'Plan' --sections 'Goals,Design,Risks'", self.A("accent"))
            return
        sel, off = self._scroll(len(ds), 6)
        for i, d in enumerate(ds[:6]):
            done = sum(t["status"] == "done" for t in d["tasks"])
            run = sum(t["status"] == "running" for t in d["tasks"])
            at = curses.A_REVERSE if i == sel else 0
            self.put(2 + i, 0, f" {d['name']:<28} {len(d['tasks'])} tasks · {done} done · {run} running  {d['doc']}", at)
        d = ds[sel]
        y = 3 + min(len(ds), 6)
        self.put(y, 0, "─" * W, self.A("dim"))
        y += 1
        for t in d["tasks"]:
            if y >= H - 3:
                break
            st = t["status"]
            col = {"done": "good", "failed": "bad", "running": "warn"}.get(st, "dim")
            x = self.put(y, 0, f" {t['id']} ", self.A("dim"))
            fresh = st == "done" and t.get("finished") and time.time() - t["finished"] < 3
            if fresh:
                self.animating = True
            label = f"{self.spin()} {st}" if st == "running" else ("✓ " + st if st == "done" else st)
            x = self.put(y, x, f"{label:<10}", self.A(col, True, rev=bool(fresh)))
            name = BY_KEY[t["agent"]].name if t["agent"] in BY_KEY else t["agent"]
            x = self.put(y, x, f" {name:<13}", self.A("a:" + t["agent"], True))
            where = t["section"] or "⟨whole document⟩"
            x = self.put(y, x, f" {where[:24]:<24} ", self.A("accent"))
            self.put(y, x, (t.get("error") or t["instruction"]).replace("\n", " "), self.A("bad") if st == "failed" else 0)
            y += 1
        try:
            text = open(d["doc"]).read().split("\n")
        except OSError:
            text = []
        y += 1
        for line in text[: max(0, H - y - 1)]:
            self.put(y, 2, line, self.A("accent", True) if line.startswith("#") else self.A("dim"))
            y += 1

    def collab_doc(self, s):
        ds = s["collab"] if s else []
        if not ds:
            return None
        return ds[max(0, min(self.sel[5], len(ds) - 1))]["doc"]

    def key(self, k, s):
        if k in (ord("q"), ord("Q")):
            return False
        if k in (ord("1"), ord("2"), ord("3"), ord("4"), ord("5"), ord("6")):
            self.tab, self.focus = k - ord("1"), 0
            return
        if k == 9 or (k == curses.KEY_RIGHT and self.tab != 0):
            self.tab, self.focus = (self.tab + 1) % len(TABS), 0
            return
        if k == curses.KEY_BTAB or (k == curses.KEY_LEFT and self.tab != 0):
            self.tab, self.focus = (self.tab - 1) % len(TABS), 0
            return
        if k == ord("g"):
            self.open_gui()
            return
        if s is None:
            return
        t = self.tab
        if t == 0 and self.focus == 1:
            if k in (curses.KEY_UP, ord("k") + 1000):
                self.psel -= 1
            elif k == curses.KEY_DOWN:
                self.psel += 1
            elif k == curses.KEY_PPAGE:
                self.psel -= 10
            elif k == curses.KEY_NPAGE:
                self.psel += 10
            elif k in (curses.KEY_LEFT, 27):
                self.focus = 0
            elif k in (ord("k"), ord("K")):
                self.kill(s, k == ord("K"))
            return
        if k == curses.KEY_UP:
            self.sel[t] -= 1
        elif k == curses.KEY_DOWN:
            self.sel[t] += 1
        elif k == curses.KEY_PPAGE:
            self.sel[t] -= 15
        elif k == curses.KEY_NPAGE:
            self.sel[t] += 15
        elif k == curses.KEY_HOME:
            self.sel[t] = 0
        elif t == 0 and k in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
            self.focus, self.psel, self.poff = 1, 0, 0
        elif t == 0 and k == ord("p"):
            self.prompt_agent(s)
        elif t == 0 and k == ord("h"):
            self.handoff(s)
        elif t == 0 and k == ord("a"):
            self.show_all = not self.show_all
        elif t == 0 and k in (ord("k"), ord("K")):
            self.focus = 1
            self.flash("select a process, then k / K")
        elif t == 2 and k == ord("/"):
            self.filter = self.ask("filter: ")
            self.sel[2] = 0
        elif t == 2 and k == 27:
            self.filter = ""
        elif t == 5:
            self.collab_key(k, s)
        self.sel[t] = max(0, self.sel[t])

    def prompt_agent(self, s):
        from . import runs
        ag = self.agents(s)
        if not ag:
            return
        a = ag[max(0, min(self.sel[0], len(ag) - 1))]
        if not a["headless"]:
            self.flash(f"{a['name']} can't be prompted headlessly")
            return
        work = [w for w in a.get("work") or [] if w.get("cwd") and not w.get("collab")]
        cwd = work[0]["cwd"] if work else os.getcwd()
        text = self.ask(f"prompt {a['name']} in {cwd.replace(os.path.expanduser('~'), '~')}: ")
        if not text:
            return
        mode = self.ask("mode: ask (read-only) / edit / auto [ask]: ") or "ask"
        if mode not in runs.MODES:
            self.flash("mode must be ask, edit or auto")
            return
        m = runs.start(a["key"], text, cwd, mode)
        self.flash(f"run {m['id']} started · follow it in the GUI (g) or: nyrasview runs" if m["status"] == "running"
                   else f"failed: {m.get('error')}")

    def handoff(self, s):
        from . import handoff
        ag = self.agents(s)
        if not ag:
            return
        a = ag[max(0, min(self.sel[0], len(ag) - 1))]
        work = [w for w in a.get("work") or [] if w.get("cwd") and not w.get("collab")]
        if a["key"] not in handoff.READERS or not work:
            self.flash("hand off works from a running Claude Code or Codex session")
            return
        w = next((x for x in work if (x.get("activity") or {}).get("state") == "limited"), work[0])
        ts = handoff.targets(exclude=a["key"])
        if not ts:
            self.flash("no other coding agent installed to hand off to")
            return
        pick = self.ask(f"hand {os.path.basename(w['cwd'])} to ({' '.join(ts)}) [{ts[0]}]: ") or ts[0]
        if pick not in ts:
            self.flash("not an installed target")
            return
        try:
            h = handoff.prepare(a["key"], w["cwd"], pick)
            r = handoff.launch(h)
            self.flash(f"handed off to {BY_KEY[pick].name} in {r['terminal']} · {h['path']}")
        except Exception as e:
            self.flash(f"handoff failed: {e}")

    def kill(self, s, force):
        ag = self.agents(s)
        if not ag:
            return
        a = ag[max(0, min(self.sel[0], len(ag) - 1))]
        if not a["procs"]:
            return
        p = a["procs"][max(0, min(self.psel, len(a["procs"]) - 1))]
        name = "force-kill" if force else "stop"
        if self.ask(f"{name} {p['pid']} {p['name']}? [y/N] ").lower() != "y":
            return
        try:
            from . import plat
            plat.kill(p["pid"], force)
            self.flash(f"sent {name} to {p['pid']}")
        except OSError as e:
            self.flash(f"kill failed: {e}")

    def collab_key(self, k, s):
        doc = self.collab_doc(s)
        if k == ord("n"):
            path = self.ask("new doc path: ")
            if not path:
                return
            title = self.ask("title: ") or os.path.splitext(os.path.basename(path))[0]
            secs = [x for x in self.ask("sections (comma separated): ").split(",") if x.strip()]
            try:
                collab.new_doc(os.path.expanduser(path), title, secs)
                self.c.refresh("collab")
                self.flash("created " + path)
            except Exception as e:
                self.flash(str(e))
            return
        if not doc:
            return
        if k in (ord("a"), ord("w")):
            v = collab.view(doc)
            section = ""
            if k == ord("a"):
                secs = v["sections"]
                listing = " ".join(f"{i + 1}:{x['title'][:14]}" for i, x in enumerate(secs[:12]))
                pick = self.ask(f"section # ({listing}) or new title: ")
                if not pick:
                    return
                section = secs[int(pick) - 1]["title"] if pick.isdigit() and 0 < int(pick) <= len(secs) else pick
            heads = [a for a in s["agents"] if a["headless"]]
            listing = " ".join(f"{a['key']}{'' if a['installed'] or a['kind'] == 'api' else '(x)'}" for a in heads)
            agent = self.ask(f"agent ({listing}): ")
            if agent not in BY_KEY:
                self.flash("unknown agent")
                return
            model = self.ask("model (enter for default): ") or None
            instr = self.ask("instruction: ")
            if not instr:
                return
            collab.assign(doc, section, agent, instr, model)
            self.c.refresh("collab")
            self.flash(f"assigned {agent} → {section or 'whole document'}")
        elif k == ord("r"):
            collab.run_async(doc)
            self.flash("running pending tasks…")
        elif k == ord("x"):
            collab.reset(doc)
            self.c.refresh("collab")
            self.flash("tasks reset to pending")
        elif k == ord("u"):
            self.flash("restored previous version" if collab.undo(doc) else "nothing to undo")
        elif k == ord("d"):
            tid = self.ask("task id to delete: ")
            if tid:
                collab.remove(doc, tid)
                self.c.refresh("collab")

    def open_gui(self):
        if self.gui_url is None:
            from . import web
            self.gui_url = web.serve_background(self.c)
        from .web import open_url
        open_url(self.gui_url)
        self.flash(f"GUI at {self.gui_url}")


def run(collector):
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: UI(scr, collector).run())
