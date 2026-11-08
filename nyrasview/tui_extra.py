import curses
import os
import secrets
import threading
import time

from .registry import BY_KEY
from .util import ago, config


def _confirm(ui, text):
    H, W = ui.scr.getmaxyx()
    ui.put(H - 1, 0, " " * (W - 1), ui.A("bar"))
    ui.put(H - 1, 0, (text + " [y/N] ")[:W - 1], ui.A("bar", bold=True))
    ui.scr.refresh()
    ui.scr.timeout(-1)
    try:
        return ui.scr.getch() in (ord("y"), ord("Y"))
    finally:
        ui.scr.timeout(250)


def _fresh(ui):
    ui.scr.clear()


def _frame(ui, title, hint):
    H, W = ui.scr.getmaxyx()
    ui.scr.erase()
    ui.put(0, 0, " " * W, ui.A("bar"))
    ui.put(0, 1, f" {title} ", ui.A("bar", bold=True))
    ui.put(H - 1, 0, hint[:W - 1], ui.A("dim"))
    return H, W


def phone_screen(ui):
    from . import qr, tunnel, web
    gui = web.find_gui()
    st = {"info": None, "at": 0, "msg": "", "pick": 0, "busy": False}

    def load():
        if gui:
            return web.gui_call(gui, "/api/phone", {"on": True})
        if web.PHONE["srv"] is None:
            web.start_phone(ui.c, int(config().get("gui", {}).get("phone_port", 7342)), secrets.token_urlsafe(24))
        return web.phone_info()

    def start_tunnel():
        st["busy"] = True
        try:
            if gui:
                web.gui_call(gui, "/api/tunnel", {"on": True, "download": not tunnel.binary()}, timeout=300)
            else:
                if not tunnel.binary():
                    st["msg"] = "downloading cloudflared once…"
                    tunnel.download()
                st["msg"] = "starting the tunnel…"
                tunnel.start(web.PHONE["port"])
            st["msg"] = ""
        except Exception as e:
            st["msg"] = f"tunnel: {e}"
        finally:
            st["busy"] = False
            st["at"] = 0

    ui.scr.timeout(500)
    _fresh(ui)
    try:
        while True:
            if time.time() - st["at"] > 2:
                try:
                    st["info"] = load()
                except Exception as e:
                    st["msg"] = f"couldn't start the phone server: {e}"
                st["at"] = time.time()
            info = st["info"] or {}
            targets = [(u["kind"], u["url"]) for u in info.get("urls") or []]
            t = info.get("tunnel") or {}
            if t.get("link"):
                targets.insert(0, ("from anywhere", t["link"]))
            hint = "n next address  t reach from anywhere  r new key  esc back"
            H, W = _frame(ui, "Phone · scan to open nyrasview on your phone", hint)
            y = 2
            if not targets:
                ui.put(y, 2, st["msg"] or "starting…", ui.A("warn"))
            else:
                kind, url = targets[st["pick"] % len(targets)]
                lines = qr.terminal(url).split("\n")
                ui.put(y, 2, f"{kind}  ", ui.A("accent", True))
                ui.put(y + 1, 2, url[:W - 4], ui.A("dim"))
                y += 3
                if len(lines) + y + 4 > H or len(lines[0]) + 4 > W:
                    ui.put(y, 2, "make the terminal bigger to show the QR code", ui.A("warn"))
                    y += 2
                else:
                    x0 = max(2, (W - len(lines[0])) // 2)
                    for line in lines:
                        ui.put(y, x0, line)
                        y += 1
                    y += 1
                if kind == "from anywhere":
                    ui.put(y, 2, "works on mobile data too; anyone with this link can use nyrasview", ui.A("warn"))
                elif info.get("firewall"):
                    fw = info["firewall"]
                    ui.put(y, 2, fw["note"][:W - 4], ui.A("warn"))
                    if fw.get("cmd"):
                        ui.put(y + 1, 4, fw["cmd"][:W - 6], ui.A("dim"))
                if len(targets) > 1:
                    ui.put(H - 2, 2, f"{st['pick'] % len(targets) + 1}/{len(targets)} addresses · " +
                           ("served by your nyrasview GUI" if gui else "stays on while this window is open"), ui.A("dim"))
                if st["msg"]:
                    ui.put(H - 3, 2, st["msg"][:W - 4], ui.A("warn"))
                elif t.get("error"):
                    ui.put(H - 3, 2, f"tunnel: {t['error']}"[:W - 4], ui.A("bad"))
            ui.scr.refresh()
            k = ui.scr.getch()
            if k in (27, ord("q")):
                return
            if k == ord("n") and targets:
                st["pick"] += 1
            elif k == ord("t") and not st["busy"] and not t.get("link"):
                st["msg"] = "starting the tunnel…"
                threading.Thread(target=start_tunnel, daemon=True).start()
            elif k == ord("r") and _confirm(ui, "new login key? phones that are logged in have to scan again"):
                if gui:
                    web.gui_call(gui, "/api/phone", {"rotate": True, "on": True})
                else:
                    web.rotate_phone_key()
                st["at"] = 0
    finally:
        ui.scr.timeout(250)
        _fresh(ui)


def turns_screen(ui, cwd):
    from . import checkpoints
    st = {"sel": 0, "data": None, "at": 0, "diff": None, "doff": 0, "msg": ("", 0)}
    ui.scr.timeout(1000)
    _fresh(ui)
    try:
        while True:
            if time.time() - st["at"] > 2:
                try:
                    st["data"] = checkpoints.listing(cwd, 60)
                except Exception as e:
                    st["data"] = {"root": None, "turns": [], "error": str(e)}
                st["at"] = time.time()
            d = st["data"]
            turns = d["turns"]
            st["sel"] = max(0, min(st["sel"], len(turns) - 1))
            cur = turns[st["sel"]] if turns else None
            if st["diff"] is not None:
                H, W = _frame(ui, f"Turn {cur['id'] if cur else ''} · {(cur or {}).get('prompt', '')[:70]}",
                              "↑↓ pgup pgdn scroll  u undo this turn  esc back")
                body = st["diff"]
                st["doff"] = max(0, min(st["doff"], max(0, len(body) - (H - 3))))
                for i, line in enumerate(body[st["doff"]:st["doff"] + H - 3]):
                    attr = ui.A("good") if line.startswith("+") and not line.startswith("+++") else \
                        ui.A("bad") if line.startswith("-") and not line.startswith("---") else \
                        ui.A("accent") if line.startswith("@@") else ui.A("dim", True) if line.startswith("diff ") else 0
                    ui.put(2 + i, 1, line.replace("\t", "    ")[:W - 2], attr)
            else:
                H, W = _frame(ui, f"Turns · {(d.get('root') or cwd).replace(os.path.expanduser('~'), '~')}",
                              "↑↓ turn  enter diff  r review part by part  u undo  R rewind to before it  esc back")
                if not d.get("root"):
                    ui.put(2, 2, "not inside a git repository, so there are no checkpoints here", ui.A("dim"))
                elif not turns:
                    ui.put(2, 2, "no turns yet: the next time an agent works here, its changes show up as a turn you can undo", ui.A("dim"))
                top = max(0, st["sel"] - (H - 5))
                for i, t in enumerate(turns[top:top + H - 3]):
                    n = top + i
                    g = BY_KEY.get(t["agent"])
                    row = 2 + i
                    attr = curses.A_REVERSE if n == st["sel"] else 0
                    ui.put(row, 0, " " * (W - 1), attr)
                    x = ui.put(row, 1, f"{t['id']}  {ago(t['at']).rjust(4)}  ", attr | ui.A("dim"))
                    x = ui.put(row, x, f"{(g.name if g else t['agent'] or 'you')[:12]:<12} ", attr | ui.A("a:" + g.key if g else "dim"))
                    x = ui.put(row, x, f"+{t['added']}", attr | ui.A("good"))
                    x = ui.put(row, x + 1, f"-{t['removed']}  ", attr | ui.A("bad"))
                    label = (t["prompt"] or "(no prompt)") + ("  · undone" if t["undone"] else "") + "  " + ", ".join(t["files"][:3])
                    ui.put(row, x, label[:max(0, W - x - 1)], attr | (ui.A("dim") if t["undone"] else 0))
            msg, t0 = st["msg"]
            if msg and time.time() - t0 < 5:
                ui.put(H - 2, 1, msg[:W - 2], ui.A("warn", True))
            ui.scr.refresh()
            k = ui.scr.getch()
            if k in (27, ord("q")):
                if st["diff"] is not None:
                    st["diff"] = None
                    continue
                return
            if st["diff"] is not None:
                if k == curses.KEY_UP:
                    st["doff"] -= 1
                elif k == curses.KEY_DOWN:
                    st["doff"] += 1
                elif k == curses.KEY_PPAGE:
                    st["doff"] -= H - 4
                elif k in (curses.KEY_NPAGE, ord(" ")):
                    st["doff"] += H - 4
            elif k == curses.KEY_UP:
                st["sel"] -= 1
            elif k == curses.KEY_DOWN:
                st["sel"] += 1
            elif k in (10, 13, curses.KEY_ENTER, ord("d")) and cur:
                try:
                    st["diff"], st["doff"] = checkpoints.diff(cwd, cur["id"]).split("\n"), 0
                except ValueError as e:
                    st["msg"] = (str(e), time.time())
            if k == ord("r") and cur and st["diff"] is None:
                if cur["agent"] == "you" or cur["undone"]:
                    st["msg"] = ("nothing to review in that turn", time.time())
                else:
                    review_screen(ui, cwd, cur["id"])
                    st["at"] = 0
                    _fresh(ui)
                continue
            if k == ord("u") and cur:
                st["msg"] = (_undo(ui, cwd, cur), time.time())
                st["diff"], st["at"] = None, 0
            elif k == ord("R") and cur and _confirm(ui, f"rewind the whole repo to just before {cur['id']}? (you can undo the rewind)"):
                try:
                    st["msg"] = (checkpoints.restore(cwd, cur["id"], before=True)["message"], time.time())
                except ValueError as e:
                    st["msg"] = (str(e), time.time())
                st["at"] = 0
    finally:
        ui.scr.timeout(250)
        _fresh(ui)


def review_screen(ui, cwd, tid):
    from . import review
    st = {"i": 0, "off": 0, "msg": ("", 0)}
    ui.scr.timeout(-1)
    try:
        while True:
            try:
                d = review.hunks(cwd, tid)
            except ValueError as e:
                ui.flash(str(e))
                return
            hs = d["hunks"]
            if not hs:
                ui.flash("that turn changed nothing")
                return
            todo = [n for n, h in enumerate(hs) if not h["status"]]
            st["i"] = max(0, min(st["i"], len(hs) - 1))
            h = hs[st["i"]]
            ui.scr.clear()
            H, W = _frame(ui, f"Review · {d['prompt'] or ''}"[:W_of(ui) - 4],
                          "a accept  r reject (takes it back)  ←→ part  ↑↓ scroll  A accept the rest  esc back")
            g = BY_KEY.get(d["agent"])
            x = ui.put(2, 1, f"{h['file']}", ui.A("accent", True))
            x = ui.put(2, x + 2, f"part {h['n']}/{h['of']}" + (f" · {h['kind']} file" if h["kind"] != "changed" else "") +
                       f" · {st['i'] + 1} of {len(hs)} in this turn · {len(todo)} left", ui.A("dim"))
            if h["status"]:
                ui.put(2, x + 2, "✓ accepted" if h["status"] == "accepted" else "↶ taken back", ui.A("good" if h["status"] == "accepted" else "warn", True))
            body = h["lines"] or [f"({h['kind']} file, no text)"]
            st["off"] = max(0, min(st["off"], max(0, len(body) - (H - 6))))
            for j, line in enumerate(body[st["off"]:st["off"] + H - 6]):
                attr = ui.A("good") if line.startswith("+") else ui.A("bad") if line.startswith("-") else ui.A("dim") if line.startswith("@@") else 0
                ui.put(4 + j, 1, line.replace("\t", "    ")[:W - 2], attr)
            msg, t0 = st["msg"]
            if msg and time.time() - t0 < 5:
                ui.put(H - 2, 1, msg[:W - 2], ui.A("warn", True))
            ui.scr.refresh()
            k = ui.scr.getch()
            if k in (27, ord("q")):
                return
            if k in (curses.KEY_RIGHT, ord("n"), ord("s")):
                st["i"], st["off"] = st["i"] + 1, 0
            elif k in (curses.KEY_LEFT, ord("p")):
                st["i"], st["off"] = st["i"] - 1, 0
            elif k == curses.KEY_DOWN:
                st["off"] += 1
            elif k == curses.KEY_UP:
                st["off"] -= 1
            elif k in (ord("a"), ord("r")) and not h["status"]:
                try:
                    r = review.decide(cwd, tid, h["id"], "accept" if k == ord("a") else "reject")
                    st["msg"] = (r.get("message") or "", time.time())
                except ValueError as e:
                    st["msg"] = (str(e), time.time())
                nxt = [n for n in todo if n > st["i"]] or [n for n in todo if n != st["i"]]
                if not nxt and (k == ord("a") or st["msg"][0].startswith("took back")):
                    ui.flash("every part of this turn is reviewed")
                    return
                st["i"], st["off"] = (nxt[0] if nxt else st["i"]), 0
            elif k == ord("A"):
                n = review.accept_rest(cwd, tid)["accepted"]
                ui.flash(f"accepted {n} part(s)")
                return
    finally:
        ui.scr.timeout(250)
        _fresh(ui)


def W_of(ui):
    return ui.scr.getmaxyx()[1]


def _undo(ui, cwd, t):
    from . import checkpoints
    if t["undone"]:
        return "that turn is already undone"
    try:
        chk = checkpoints.undo(cwd, t["id"], dry=True)
        if not chk["ok"]:
            c = ", ".join(chk.get("conflicts") or [])
            if _confirm(ui, f"can't undo cleanly{' (' + c + ')' if c else ''}. rewind the whole repo to before it instead?"):
                return checkpoints.restore(cwd, t["id"], before=True)["message"]
            return "nothing changed"
        if not chk["files"]:
            return chk["message"]
        if not _confirm(ui, f"undo {len(chk['files'])} file(s): {', '.join(chk['files'][:4])}? later work stays"):
            return "nothing changed"
        return checkpoints.undo(cwd, t["id"])["message"]
    except ValueError as e:
        return str(e)


def search_screen(ui):
    from . import plat, search
    q = ui.ask("search history: ")
    if not q or not q.strip():
        return
    _fresh(ui)
    try:
        res = search.find(q, limit=40, turns=True)
    except ValueError as e:
        ui.flash(str(e))
        return
    st = {"sel": 0, "open": False, "msg": ("", 0)}
    home = os.path.expanduser("~")
    ui.scr.timeout(-1)
    try:
        while True:
            ui.scr.clear()
            H, W = _frame(ui, f"Search · {q} · {len(res)} session(s)",
                          "↑↓ session  enter details  c copy resume command  w watch / turns  / new search  esc back")
            if not res:
                ui.put(2, 2, "nothing found", ui.A("dim"))
            st["sel"] = max(0, min(st["sel"], len(res) - 1))
            y = 2
            start = st["sel"] if st["open"] else max(0, st["sel"] - max(1, (H - 4) // 4) + 1)
            for i, r in enumerate(res[start:], start):
                if y >= H - 2:
                    break
                g = BY_KEY.get(r["agent"])
                on = i == st["sel"]
                attr = curses.A_REVERSE if on else 0
                ui.put(y, 0, " " * (W - 1), attr)
                x = ui.put(y, 1, f"● {(g.name if g else r['agent'])} ", attr | ui.A("a:" + r["agent"], True))
                x = ui.put(y, x, (r["title"] or "")[:60] + "  ", attr)
                ui.put(y, x, f"{(r['cwd'] or '?').replace(home, '~')} · {ago(r['last'])} ago", attr | ui.A("dim"))
                y += 1
                hits = r["hits"] if (on and st["open"]) else r["hits"][:2]
                for h in hits:
                    if y >= H - 2:
                        break
                    who = {"user": "you", "agent": "ai ", "tool": "ran"}.get(h["role"], h["role"])
                    x = ui.put(y, 3, f"{who} ▸ ", ui.A("dim"))
                    for k, part in enumerate(h["snippet"].replace(search.HIT_B, search.HIT_A).split(search.HIT_A)):
                        x = ui.put(y, x, part[:max(0, W - x - 1)], ui.A("warn", True) if k % 2 else 0)
                    y += 1
                if on and st["open"]:
                    for t in r.get("turns") or []:
                        if y >= H - 2:
                            break
                        ui.put(y, 3, f"turn {t['id']}  {(t['prompt'] or '')[:50]}  +{t['added']} -{t['removed']}" + ("  undone" if t["undone"] else ""), ui.A("cyan"))
                        y += 1
                    if r.get("resume") and y < H - 2:
                        ui.put(y, 3, "resume: " + r["resume"].replace(home, "~"), ui.A("good"))
                        y += 1
                y += 1
            msg, t0 = st["msg"]
            if msg and time.time() - t0 < 5:
                ui.put(H - 2, 1, msg[:W - 2], ui.A("warn", True))
            ui.scr.refresh()
            k = ui.scr.getch()
            cur = res[st["sel"]] if res else None
            if k in (27, ord("q")):
                if st["open"]:
                    st["open"] = False
                    continue
                return
            if k == curses.KEY_UP:
                st["sel"] -= 1
            elif k == curses.KEY_DOWN:
                st["sel"] += 1
            elif k in (10, 13, curses.KEY_ENTER):
                st["open"] = not st["open"]
            elif k == ord("/"):
                return search_screen(ui)
            elif k == ord("c") and cur:
                if cur.get("resume"):
                    how = plat.copy(cur["resume"])
                    st["msg"] = ("copied the resume command" if how else "resume: " + cur["resume"], time.time())
                else:
                    st["msg"] = (f"{cur['agent']} sessions can't be resumed from a command", time.time())
            elif k == ord("w") and cur and cur.get("cwd"):
                turns_screen(ui, cur["cwd"])
                _fresh(ui)
    finally:
        ui.scr.timeout(250)
        _fresh(ui)


HELP = [
    ("anywhere", [("1-7 / tab", "switch tabs"), ("F", "search every past agent session"), ("P", "phone: QR code to open nyrasview on your phone"),
                  ("g", "open the web GUI"), ("?", "this help"), ("q", "quit")]),
    ("Agents", [("↑↓", "pick an agent"), ("enter / →", "its processes (k / K to stop one)"), ("p", "prompt it"),
                ("t", "turns: see, review and undo what it changed"), ("h", "hand off to another agent"),
                ("f", "self-heal: run a check, let it fix failures"), ("a", "show agents that aren't installed")]),
    ("Skills", [("/", "filter"), ("esc", "clear the filter")]),
    ("Collab", [("n", "new doc"), ("a", "assign a section"), ("w", "whole-doc task"), ("r", "run"), ("x", "reset"),
                ("u", "undo the last run")]),
    ("Runs", [("enter", "follow up"), ("p", "new prompt"), ("f", "self-heal"), ("s", "stop")]),
    ("from the shell: nyrasview …", [("find", "search past sessions"), ("review", "accept or take back changes"), ("watch", "follow an agent's edits live"), ("settings", "see and change settings"),
                                     ("phone", "QR code without the dashboard"), ("checkpoints", "turns in this repo"),
                                     ("completion", "tab completion (fish | source)")]),
]


def help_screen(ui):
    ui.scr.timeout(-1)
    _fresh(ui)
    try:
        H, W = _frame(ui, "Keys", "any key to close")
        colw = min(W - 4, 72)
        y, col = 2, 2
        for title, keys in HELP:
            if y + len(keys) + 2 > H - 1:
                y, col = 2, col + colw + 2
                if col + 30 > W:
                    break
            kw = max(len(k) for k, _ in keys) + 2
            ui.put(y, col, title, ui.A("accent", True))
            y += 1
            for k, what in keys:
                x = ui.put(y, col + 2, k.ljust(kw), ui.A("good"))
                ui.put(y, x, what[:max(0, min(W - x - 1, col + colw - x))])
                y += 1
            y += 1
        ui.scr.refresh()
        ui.scr.getch()
    finally:
        ui.scr.timeout(250)
        _fresh(ui)


def pick_cwd(ui, a):
    from . import tui_more
    work = [w["cwd"] for w in a.get("work") or [] if w.get("cwd") and not w.get("collab")]
    work = list(dict.fromkeys(work))
    if len(work) > 1:
        i = tui_more.menu(ui, f"{a['name']}: which folder?", [{"label": w.replace(os.path.expanduser("~"), "~")} for w in work])
        return work[i] if i is not None else None
    return work[0] if work else os.getcwd()
