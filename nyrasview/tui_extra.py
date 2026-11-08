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
                              "↑↓ turn  enter diff  u undo  R rewind to before it  esc back")
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


HELP = [
    ("anywhere", [("1-7 / tab", "switch tabs"), ("P", "phone: QR code to open nyrasview on your phone"),
                  ("g", "open the web GUI"), ("?", "this help"), ("q", "quit")]),
    ("Agents", [("↑↓", "pick an agent"), ("enter / →", "its processes (k / K to stop one)"), ("p", "prompt it"),
                ("t", "turns: see and undo what it changed"), ("h", "hand off to another agent"),
                ("f", "self-heal: run a check, let it fix failures"), ("a", "show agents that aren't installed")]),
    ("Skills", [("/", "filter"), ("esc", "clear the filter")]),
    ("Collab", [("n", "new doc"), ("a", "assign a section"), ("w", "whole-doc task"), ("r", "run"), ("x", "reset"),
                ("u", "undo the last run")]),
    ("Runs", [("enter", "follow up"), ("p", "new prompt"), ("f", "self-heal"), ("s", "stop")]),
    ("from the shell: nyrasview …", [("watch", "follow an agent's edits live"), ("settings", "see and change settings"),
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
