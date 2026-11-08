import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from .registry import BY_KEY
from .util import ago, config


def _c():
    from .cli import col, dim
    return col, dim


def cmd_phone(a):
    from . import tunnel, web
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    gui = web.find_gui()
    if gui:
        info = web.gui_call(gui, "/api/phone", {"on": True, "rotate": a.rotate})
        if a.tunnel and not (info.get("tunnel") or {}).get("link"):
            if not tunnel.binary():
                print("reach-from-anywhere needs cloudflared; downloading it once…", flush=True)
            print("starting a Cloudflare tunnel…", flush=True)
            info = web.gui_call(gui, "/api/tunnel", {"on": True, "download": not tunnel.binary()}, timeout=300)
            for _ in range(90):
                if (info.get("tunnel") or {}).get("link") or (info.get("tunnel") or {}).get("error"):
                    break
                time.sleep(1)
                info = web.gui_call(gui, "/api/phone", {})
        if not info.get("urls"):
            sys.exit("the phone server didn't start; see nyrasview doctor")
        print(web.phone_lines(info, firewall=not a.tunnel))
        err = (info.get("tunnel") or {}).get("error")
        if a.tunnel and err:
            print(f"tunnel: {err}")
        print(_c()[1](f"\nserved by the nyrasview GUI on {gui[0]}; it stays on while that GUI runs"))
        return
    if a.rotate:
        web.rotate_phone_key()
        print("new phone key: old phone links stop working")
    from .collector import Collector
    g = config().get("gui", {})
    c = Collector(interval=float(g.get("interval", 1.5)))
    web.serve(c, int(g.get("port", 7341)), False, True, a.tunnel)


def _pick_agent(cwd, want):
    from . import live
    if want:
        return want
    best, at = "claude", 0
    for k in ("claude", "codex", "gemini", "qwen"):
        try:
            es = live.edits(k, cwd, 5)
        except Exception:
            es = []
        t = max((e["at"] for e in es), default=0)
        if t > at:
            best, at = k, t
    return best


def _watch_frame(agent, cwd, W, H):
    from . import activity, checkpoints, live
    col, dim = _c()
    g = BY_KEY.get(agent)
    name = g.name if g else agent
    act = activity.get(agent, cwd) or {}
    v = live.view(agent, cwd)
    try:
        turns = checkpoints.listing(cwd, 4)["turns"]
    except Exception:
        turns = []
    st = act.get("state") or "idle"
    stc = {"working": "#22c55e", "waiting": "#f59e0b", "limited": "#ef4444"}.get(st)
    out = [col(g.hex if g else None, f"● {name}") + "  " + col(stc, st) + dim(f"  {cwd.replace(os.path.expanduser('~'), '~')}")]
    if act.get("prompt"):
        out.append(dim("you ▸ ") + act["prompt"][:W - 6])
    if act.get("action") or act.get("reply"):
        out.append(dim(("now ▸ " if st == "working" else "last ▸ ")) + (act.get("action") or act.get("reply") or "")[:W - 7])
    out.append("")
    tail = min(len(v["edits"]), 5)
    room = max(6, H - len(out) - 4 - (tail + 2 if tail else 0) - (len(turns) + 2 if turns else 0))
    f = v.get("focus")
    if f and not f.get("missing"):
        rng = f.get("range")
        out.append(col(None, f["rel"]) + dim(f"  {f['tool']} · {ago(f['at'])} ago" + (f" · line {rng[0] + 1}" + (f"–{rng[1] + 1}" if rng[1] > rng[0] else "") if rng else "")))
        c0, c1 = (rng or (-1, -1))
        mid = (c0 + c1) // 2 if rng else f["start"]
        lo = max(f["start"], mid - room // 2)
        lines = f["lines"][lo - f["start"]:lo - f["start"] + room - 1]
        for j, line in enumerate(lines):
            n = lo + j
            on = c0 <= n <= c1
            text = line.replace("\t", "    ")[:W - 8]
            out.append((col("#22c55e", "▌") if on else " ") + dim(f"{n + 1:>5} ") + (col("#e5e7eb", text) if on else dim(text)))
    elif f and f.get("missing"):
        out.append(dim("the last edited file is gone or unreadable"))
    else:
        out.append(dim(f"no edits from {name} in this folder yet; this updates as soon as it changes a file"))
    if tail:
        out += ["", dim("recent edits")]
        for e in v["edits"][-tail:][::-1]:
            out.append(f"  {dim(ago(e['at']).rjust(4))}  {e['rel'][:W - 24]}  " + col("#22c55e", f"+{e['added']}") + " " + col("#ef4444", f"-{e['removed']}"))
    if turns:
        out += ["", dim("turns (nyrasview undo <id>)")]
        for t in turns:
            out.append(f"  {dim(t['id'])}  {dim(ago(t['at']).rjust(4))}  {(t['prompt'] or '')[:W - 40]}  " +
                       col("#22c55e", f"+{t['added']}") + " " + col("#ef4444", f"-{t['removed']}") + (dim(" undone") if t["undone"] else ""))
    return out


def cmd_watch(a):
    cwd = os.path.realpath(a.dir)
    agent = _pick_agent(cwd, a.agent)
    if agent not in BY_KEY:
        sys.exit(f"unknown agent {agent}; see: nyrasview agents")
    if a.once or not sys.stdout.isatty():
        sz = shutil.get_terminal_size((120, 40))
        return print("\n".join(_watch_frame(agent, cwd, sz.columns, sz.lines)))
    prev = None
    sys.stdout.write("\033[?25l")
    try:
        while True:
            sz = shutil.get_terminal_size((120, 40))
            body = _watch_frame(agent, cwd, sz.columns, sz.lines - 1)[:sz.lines - 1]
            frame = "\n".join(body)
            if frame != prev:
                sys.stdout.write("\033[H\033[2J" + frame + "\n" + _c()[1](f"watching · ctrl+c to stop · {time.strftime('%H:%M:%S')}"))
                sys.stdout.flush()
                prev = frame
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\n")


def _parse(typ, raw):
    if typ == "bool":
        v = str(raw).strip().lower()
        if v not in ("true", "false", "on", "off", "yes", "no", "1", "0"):
            raise ValueError(f"expected true or false, got {raw}")
        return v in ("true", "on", "yes", "1")
    if typ in ("int", "float", "list", "str", "secret"):
        from .settings import _coerce
        return _coerce(typ, raw, None)
    import tomllib
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw


def _field(key):
    from .settings import SCHEMA
    sec, _, name = key.partition(".")
    for s in SCHEMA:
        if s["id"] == sec:
            for f in s["fields"]:
                if f[0] == name:
                    return f
    return None


def _show(v, typ=None):
    if typ == "secret":
        return "••••" + str(v)[-4:] if v else "(not set)"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "(empty)"
    if v == "":
        return "(empty)"
    return json.dumps(v) if isinstance(v, (dict, str)) and (not v or isinstance(v, dict)) else str(v)


def cmd_settings(a):
    from . import settings
    col, dim = _c()
    sub = a.settings_cmd or "list"
    if sub == "path":
        return print(settings.CONFIG)
    if sub == "edit":
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "nano")
        fd, tmp = tempfile.mkstemp(suffix=".toml", prefix="nyrasview-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(settings.raw())
        try:
            while True:
                subprocess.call([*editor.split(), tmp])
                text = open(tmp, encoding="utf-8").read()
                try:
                    if text != settings.raw():
                        settings.write_raw(text)
                        print("saved " + str(settings.CONFIG) + dim("  (the old file is in backups)"))
                    else:
                        print("no changes")
                    return
                except Exception as e:
                    print(col("#ef4444", f"that isn't valid TOML: {e}"))
                    if input("edit again? [Y/n] ").strip().lower() in ("n", "no"):
                        return print("not saved")
        finally:
            os.unlink(tmp)
    cfg = settings.load()
    if sub == "get":
        cur = cfg
        for part in a.key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                f = _field(a.key)
                if f:
                    return print(_show(f[2], f[1]) + dim("  (default)"))
                sys.exit(f"{a.key} is not set")
            cur = cur[part]
        f = _field(a.key)
        return print(_show(cur, f[1] if f else None) if not isinstance(cur, dict) else json.dumps(cur, indent=2))
    if sub in ("set", "unset"):
        parts = a.key.split(".")
        if len(parts) < 2:
            sys.exit("use section.name, e.g. notify.soon_pct")
        f = _field(a.key)
        node = cfg
        for part in parts[:-1]:
            if not isinstance(node.get(part, {}), dict):
                sys.exit(f"{part} is a value, not a section")
            node = node.setdefault(part, {})
        if sub == "unset":
            if node.pop(parts[-1], None) is None:
                return print(f"{a.key} wasn't set")
        else:
            val = _parse(f[1] if f else None, " ".join(a.value))
            if f and f[1] == "secret" and not val:
                node.pop(parts[-1], None)
            else:
                node[parts[-1]] = val
        cfg = {k: v for k, v in cfg.items() if v != {}}
        settings.write_raw(settings.dump(cfg))
        shown = "default" if sub == "unset" else _show(node.get(parts[-1]), f[1] if f else None)
        return print(f"{a.key} = {shown}" + ("" if f or sub == "unset" else dim("  (not a built-in setting; saved as written)")))
    v = settings.view()
    print(dim(v["path"]))
    for sec in v["sections"]:
        print("\n" + col("#e879f9", f"[{sec['id']}]") + dim(f"  {sec['title']}"))
        for fl in sec["fields"]:
            val = _show(fl["value"], fl["type"])
            line = f"  {fl['name']:<20} " + (val if fl["set"] else dim(val))
            if a.verbose:
                line += dim(f"  {fl['help']}")
            print(line)
    if v["extra"]:
        print("\n" + dim("other sections: " + ", ".join(sorted(v["extra"]))))
    print(dim("\nnyrasview settings set notify.soon_pct 80 · settings edit · -v for descriptions · dim = default"))


def cmd_sandbox(a):
    from . import sandbox
    if a.sandbox_cmd == "test":
        print("starting a container to look for agents… (the first run may pull the image)", flush=True)
        r = sandbox.test()
        print(r["output"])
        if not r["ok"]:
            sys.exit(1)
        return
    s = sandbox.settings()
    print(f"image   {s['image'] or '(not set: nyrasview settings set sandbox.image ghcr.io/you/agents:latest)'}")
    print(f"runtime {s['runtime']} ({'found' if shutil.which(s['runtime']) else 'not installed'})")
    print(f"default {'on' if s.get('default') else 'off'}")


def _walk(ap):
    import argparse
    out = {}
    for act in ap._actions:
        if isinstance(act, argparse._SubParsersAction):
            helps = {c.dest: c.help for c in act._choices_actions}
            for name, sub in act.choices.items():
                if helps.get(name) == argparse.SUPPRESS:
                    continue
                out[name] = {"help": helps.get(name) or "", "opts": [o for x in sub._actions for o in x.option_strings
                                                                      if o not in ("-h", "--help")], "sub": _walk(sub)}
    return out


def cmd_completion(a):
    from .cli import build_parser
    tree = _walk(build_parser())
    q = lambda s: (s or "").replace("'", "")
    if a.shell == "fish":
        lines = ["complete -c nyrasview -f"]
        for name, d in tree.items():
            lines.append(f"complete -c nyrasview -n __fish_use_subcommand -a {name} -d '{q(d['help'])}'")
            for o in d["opts"]:
                flag = f"-l {o[2:]}" if o.startswith("--") else f"-s {o[1:]}"
                lines.append(f"complete -c nyrasview -n '__fish_seen_subcommand_from {name}' {flag}")
            for sn, sd in d["sub"].items():
                lines.append(f"complete -c nyrasview -n '__fish_seen_subcommand_from {name}; and not __fish_seen_subcommand_from {' '.join(d['sub'])}' -a {sn} -d '{q(sd['help'])}'")
            if "-d" in d["opts"] or "--dir" in d["opts"]:
                lines.append(f"complete -c nyrasview -n '__fish_seen_subcommand_from {name}' -s d -l dir -r -a '(__fish_complete_directories)'")
        lines.append("complete -c nyrasview -n '__fish_seen_subcommand_from undo restore' -a '(nyrasview checkpoints --json 2>/dev/null | string match -r \"\\\"id\\\": \\\"[0-9a-f]+\" | string replace -r \".*\\\"\" \"\")' -d turn")
        return print("\n".join(lines))
    cases = []
    for name, d in tree.items():
        words = " ".join(list(d["sub"]) + d["opts"])
        cases.append(f"    {name}) opts='{words}' ;;")
    script = f"""_nyrasview() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}" cmd="${{COMP_WORDS[1]}}" opts
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=($(compgen -W '{' '.join(tree)}' -- "$cur")); return
  fi
  case "$cmd" in
{chr(10).join(cases)}
    *) opts='' ;;
  esac
  case "${{COMP_WORDS[COMP_CWORD-1]}}" in -d|--dir) COMPREPLY=($(compgen -d -- "$cur")); return ;; esac
  COMPREPLY=($(compgen -W "$opts" -- "$cur"))
}}
"""
    if a.shell == "zsh":
        script = "autoload -U +X bashcompinit && bashcompinit\n" + script
    print(script + "complete -o default -F _nyrasview nyrasview")


def _since(v):
    if not v:
        return None
    import re as _re
    m = _re.fullmatch(r"(\d+)\s*([hdwm])", v.strip().lower())
    if not m:
        raise ValueError("--since takes something like 24h, 7d, 2w or 3m")
    return time.time() - int(m.group(1)) * {"h": 3600, "d": 86400, "w": 7 * 86400, "m": 30 * 86400}[m.group(2)]


def _hl(snippet):
    from . import search
    col, _ = _c()
    out, on = "", False
    for part in snippet.replace(search.HIT_B, search.HIT_A).split(search.HIT_A):
        out += col("#f59e0b", part) if on else part
        on = not on
    return out


def cmd_find(a):
    from . import search
    q = " ".join(a.query)
    res = search.find(q, a.agent, a.dir, _since(a.since), a.limit, turns=True)
    if a.json:
        for r in res:
            for h in r["hits"]:
                h["snippet"] = h["snippet"].replace(search.HIT_A, "").replace(search.HIT_B, "")
        return print(json.dumps(res, indent=2))
    col, dim = _c()
    if not res:
        st = search.stats()
        return print(f"nothing found for {q!r} in {st['messages']:,} messages from " +
                     ", ".join(f"{n} {k}" for k, n in st["sessions"].items()) + " sessions")
    home = os.path.expanduser("~")
    for r in res:
        g = BY_KEY.get(r["agent"])
        print(col(g.hex if g else None, f"● {g.name if g else r['agent']}") + "  " + (r["title"] or "")[:70] +
              dim(f"  · {(r['cwd'] or '?').replace(home, '~')} · {ago(r['last'])} ago"))
        for h in r["hits"]:
            who = {"user": "you", "agent": "ai ", "tool": "ran"}.get(h["role"], h["role"])
            print(f"   {dim(who)} ▸ {_hl(h['snippet'])}")
        if r.get("turns"):
            print("   " + dim("turns: " + "  ".join(f"{t['id']} {(t['prompt'] or '')[:24]}" for t in r["turns"][:3])))
        if r.get("resume"):
            print("   " + dim("resume: " + r["resume"].replace(home, "~")))
        print()
