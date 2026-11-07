import json
import os
import secrets
import shutil
import signal
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import collab, obsidian
from .registry import BY_KEY

STATIC = Path(__file__).with_name("static")
PKG = Path(__file__).parent


def build():
    h = 0
    for f in sorted(PKG.glob("*.py")):
        try:
            h ^= hash((f.name, f.stat().st_mtime_ns))
        except OSError:
            pass
    return h


BUILD = build()


def open_url(url):
    from . import plat
    plat.open_url(url)


_port_cache = {}
PHONE = {"srv": None, "port": None}
MANIFEST = {"name": "nyrasview", "short_name": "nyrasview", "start_url": "/", "display": "standalone",
            "background_color": "#0b0b0f", "theme_color": "#0b0b0f",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]}
ICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#0b0b0f"/>'
        '<circle cx="256" cy="256" r="120" fill="none" stroke="#e879f9" stroke-width="40"/><circle cx="256" cy="256" r="44" fill="#e879f9"/></svg>')


def phone_key():
    from .util import STATE
    f = STATE / "phone_key"
    try:
        k = f.read_text().strip()
        if len(k) >= 16:
            return k
    except OSError:
        pass
    k = secrets.token_urlsafe(18)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(k)
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return k


def rotate_phone_key():
    from .util import STATE
    (STATE / "phone_key").unlink(missing_ok=True)
    return phone_key()


def lan_addresses():
    import socket
    import psutil
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        primary = s.getsockname()[0]
        s.close()
    except OSError:
        primary = None
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith("127.") and not a.address.startswith("169.254."):
                kind = "Tailscale" if a.address.startswith("100.") or "tailscale" in name.lower() else ("Wi-Fi / LAN" if a.address == primary else name)
                out.append({"ip": a.address, "iface": name, "kind": kind, "primary": a.address == primary})
    out.sort(key=lambda a: (not a["primary"], a["kind"] != "Tailscale"))
    return out


def phone_info():
    from . import qr
    key = phone_key()
    on = PHONE["srv"] is not None
    urls = [{"url": f"http://{a['ip']}:{PHONE['port']}/?key={key}", **a} for a in lan_addresses()] if on else []
    for u in urls:
        try:
            u["qr"] = qr.svg(u["url"], scale=6)
        except ValueError:
            u["qr"] = None
    return {"on": on, "port": PHONE["port"], "urls": urls}


def make_handler(collector, token, port, phone=False):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origin = f"http://127.0.0.1:{port}"

    class H(BaseHTTPRequestHandler):
        server_version = "nyrasview"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            if phone:
                return self._phone_ok()
            return self.headers.get("Host", "") in allowed_hosts

        def _phone_ok(self):
            key = phone_key()
            cookie = self.headers.get("Cookie", "")
            return f"nyra_key={key}" in cookie.replace(" ", "").split(";")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if phone and q.get("key", [""])[0] == phone_key():
                self.send_response(302)
                self.send_header("Set-Cookie", f"nyra_key={phone_key()}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Lax")
                self.send_header("Location", "/")
                self.end_headers()
                return
            if not self._host_ok():
                if phone:
                    return self._send(401, b"<h3 style='font-family:sans-serif'>Scan the QR code on nyrasview's Phone page to sign in.</h3>", "text/html")
                return self._send(403, {"error": "bad host"})
            if u.path == "/manifest.webmanifest":
                return self._send(200, json.dumps(MANIFEST).encode(), "application/manifest+json")
            if u.path == "/icon.svg":
                return self._send(200, ICON.encode(), "image/svg+xml")
            if u.path == "/api/phone":
                return self._send(200, phone_info())
            if u.path in ("/", "/index.html"):
                snap = json.dumps(collector.snapshot(), default=str).replace("</", "<\\/")
                html = (STATIC / "index.html").read_text().replace("__NYRA_TOKEN__", token).replace("__NYRA_SNAP__", snap).replace("__NYRA_STALE__", "true" if build() != BUILD else "false")
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if u.path == "/api/snapshot":
                return self._send(200, collector.snapshot() or {})
            if u.path == "/api/collab":
                doc = (q.get("doc") or [None])[0]
                if not doc:
                    return self._send(200, {"docs": collab.docs(), "vaults": obsidian.vaults()})
                if not Path(doc).exists():
                    return self._send(404, {"error": "no such file"})
                return self._send(200, collab.view(doc))
            if u.path == "/api/live":
                if self.headers.get("X-Nyra-Token") != token:
                    return self._send(403, {"error": "forbidden"})
                import time as _t
                from . import feedback, live
                agent, cwd = q.get("agent", [""])[0], q.get("cwd", [""])[0]
                pick = q.get("pick", [None])[0]
                v = live.view(agent, cwd, pick)
                hit = _port_cache.get(cwd)
                if not hit or _t.time() - hit[0] > 8:
                    snap = collector.snapshot() or {}
                    pids = [p["pid"] for a in snap.get("agents", []) if a["key"] == agent for p in a["procs"]]
                    hit = (_t.time(), live.ports(cwd, pids))
                    _port_cache[cwd] = hit
                v["ports"] = hit[1]
                v["feedback"] = feedback.listing(cwd)
                v["hooks"] = feedback.installed()
                return self._send(200, v)
            if u.path.startswith("/api/runs"):
                if self.headers.get("X-Nyra-Token") != token:
                    return self._send(403, {"error": "forbidden"})
                from . import runs
                rid = u.path[len("/api/runs"):].strip("/")
                if not rid:
                    return self._send(200, runs.listing())
                v = runs.view(rid)
                if v and v.get("group"):
                    v["siblings"] = [r["id"] for r in sorted((r for r in runs.listing(200) if r.get("group") == v["group"]),
                                                             key=lambda r: (r["agent"], r["id"]))]
                return self._send(200 if v else 404, v or {"error": "no such run"})
            if u.path == "/api/skills/matrix":
                from . import skillman
                return self._send(200, skillman.installed())
            if u.path == "/api/mcp/matrix":
                from . import mcpman
                return self._send(200, mcpman.matrix())
            if u.path == "/api/planner":
                from . import planner
                try:
                    return self._send(200, {"pick": planner.pick(), "workers": planner.workers(), "options": planner.options(),
                                            "role": planner.ROLE})
                except Exception as e:
                    return self._send(200, {"error": str(e)})
            if u.path == "/api/settings":
                from . import feedback, settings
                v = settings.view()
                v["hooks"] = feedback.installed()
                return self._send(200, v)
            if u.path == "/api/heal":
                from . import heal
                cwd = q.get("cwd", [""])[0]
                return self._send(200, {"jobs": heal.listing(), "settings": heal.settings(),
                                        "detect": heal.detect(os.path.expanduser(cwd)) if cwd else None,
                                        "live": heal.project_cmd(os.path.expanduser(cwd)) if cwd else None})
            if u.path == "/api/events":
                from . import notify
                return self._send(200, notify.recent(40, float(q.get("since", ["0"])[0] or 0)))
            if u.path == "/api/handoff/options":
                from . import handoff
                return self._send(200, handoff.options(q.get("agent", ["claude"])[0], q.get("cwd", ["~"])[0]))
            if u.path == "/api/handoff/targets":
                from . import handoff
                from . import fallback
                return self._send(200, [{"key": k, "name": fallback.label(k), "hex": fallback.color(k)}
                                        for k in handoff.targets((q.get("exclude") or [None])[0])])
            if u.path == "/api/keys":
                from . import fallback
                return self._send(200, fallback.status())
            if u.path == "/api/agents":
                snap = collector.snapshot() or {}
                vis = {x["key"]: x for x in snap.get("agents", [])}
                return self._send(200, [{"key": a.key, "name": a.name, "hex": a.hex, "kind": a.kind,
                                         "installed": a.kind == "api" or bool(vis.get(a.key, {}).get("installed"))}
                                        for a in BY_KEY.values() if a.headless])
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Nyra-Token") != token:
                if phone and self._phone_ok():
                    return self._send(403, {"error": "reload the page"})
                return self._send(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            p = urlparse(self.path).path
            try:
                return self._send(200, self.route(p, body))
            except Exception as e:
                return self._send(400, {"error": str(e)})

        def route(self, p, b):
            doc = obsidian.resolve(b.get("doc") or "")
            if p == "/api/plan":
                from . import planner
                who = None
                if b.get("planner"):
                    ag, _, model = b["planner"].partition(":")
                    who = {"agent": ag, "model": model or None, "where": "chosen"}
                if who and b.get("remember"):
                    planner.remember(who["agent"], who.get("model"))
                r = planner.make(b["goal"], obsidian.resolve(b["doc"]) if b.get("doc") else None, b.get("sections"), who)
                if b.get("run"):
                    collab.run_async(r["doc"])
                collector.refresh("collab")
                return {"doc": r["doc"], "planner": r["planner"], "title": r["plan"]["title"], "tasks": len(r["tasks"])}
            if p == "/api/collab/new":
                secs = [s for s in (b.get("sections") or []) if s.strip()]
                path = collab.new_doc(doc, b.get("title") or Path(doc).stem, secs)
                collector.refresh("collab")
                return {"doc": path}
            if p == "/api/collab/open":
                if not Path(doc).is_file():
                    raise FileNotFoundError(doc)
                collab.register(doc)
                collector.refresh("collab")
                return {"doc": str(Path(doc).resolve())}
            if p == "/api/collab/assign":
                if b.get("agent") not in BY_KEY:
                    raise ValueError("unknown agent")
                t = collab.assign(doc, b.get("section") or "", b["agent"], b.get("instruction") or "",
                                  b.get("model") or None)
                collector.refresh("collab")
                return t
            if p == "/api/collab/remove":
                collab.remove(doc, b["id"])
                return {"ok": True}
            if p == "/api/collab/reset":
                collab.reset(doc, b.get("id"))
                return {"ok": True}
            if p == "/api/collab/run":
                collab.run_async(doc, only=b.get("ids"), rerun=bool(b.get("rerun")))
                return {"ok": True}
            if p == "/api/collab/sync":
                ts = obsidian.sync(doc)
                if ts and b.get("run"):
                    collab.run_async(doc, only=[t["id"] for t in ts])
                return {"tasks": ts}
            if p == "/api/collab/undo":
                return {"ok": collab.undo(doc)}
            if p == "/api/collab/save":
                Path(doc).write_text(b.get("text") or "")
                return {"ok": True}
            if p == "/api/runs":
                from . import runs
                import secrets as _s
                ags = [a for a in (b.get("agents") or [b.get("agent")]) if a]
                if not ags or not (b.get("prompt") or "").strip():
                    raise ValueError("pick at least one agent and write a prompt")
                group = _s.token_hex(3) if len(ags) > 1 else None
                out = [runs.start(a, b["prompt"], b.get("cwd") or "~", b.get("mode") or "ask", b.get("model") or None,
                                  group, b.get("parent"), bool(b.get("api")),
                                  None if b.get("sandbox") is None else bool(b.get("sandbox"))) for a in ags]
                collector.refresh("collab")
                return {"runs": [{"id": m["id"], "agent": m["agent"], "status": m["status"], "error": m.get("error")} for m in out],
                        "group": group}
            if p == "/api/runs/stop":
                from . import runs
                return {"ok": runs.stop(b["id"])}
            if p == "/api/open":
                from . import handoff
                cwd = os.path.realpath(os.path.expanduser(b.get("cwd") or "~"))
                cmd = handoff.interactive(b["agent"])
                if not cmd:
                    raise ValueError(f"no interactive mode known for {b['agent']}")
                pr = (b.get("prompt") or "").strip()
                if "{prompt}" in " ".join(cmd):
                    cmd = [c.replace("{prompt}", pr) for c in cmd] if pr else [c for c in cmd if c != "{prompt}" and c not in ("-i", "--prompt")]
                return {"terminal": handoff.terminal(cwd, cmd)}
            if p.startswith("/api/skills/"):
                from . import skillman
                act = p[len("/api/skills/"):]
                ags = b.get("agents") or []
                if act == "fetch":
                    return skillman.fetch(b.get("source"))
                if act == "install":
                    r = skillman.install(b["id"], b.get("names"), ags)
                elif act == "create":
                    r = skillman.create(b["name"], b.get("description", ""), b.get("body", ""), ags)
                elif act == "copy":
                    r = skillman.copy(b["folder"], b["from"], ags)
                elif act == "remove":
                    r = skillman.remove(b["folder"], ags)
                else:
                    raise ValueError("unknown action")
                collector.refresh("cfg")
                return {"done": r}
            if p.startswith("/api/mcp/"):
                from . import mcpman
                act = p[len("/api/mcp/"):]
                tg = [t["key"] for t in mcpman.targets()]
                want = b.get("agents") or []
                if want == ["all"]:
                    want = [t["key"] for t in mcpman.targets() if t["installed"]]
                if act == "test":
                    spec = mcpman.full_spec(b["from"], b["name"]) if b.get("from") else mcpman.parse(b.get("text"))[1]
                    if not spec:
                        raise ValueError("server not found")
                    return mcpman.probe(spec)
                if act == "parse":
                    name, spec = mcpman.parse(b.get("text"), b.get("name") or None)
                    return {"name": name, "spec": {**spec, "env": {k: "•••" for k in spec.get("env") or {}}}}
                done, errs = [], {}
                if act == "add":
                    name, spec = mcpman.parse(b.get("text"), b.get("name") or None)
                elif act == "copy":
                    name, spec = b["name"], mcpman.full_spec(b["from"], b["name"])
                    if not spec:
                        raise ValueError("server not found")
                elif act == "remove":
                    name, spec = b["name"], None
                else:
                    raise ValueError("unknown action")
                for ag in want:
                    if ag not in tg:
                        errs[ag] = "not supported"
                        continue
                    try:
                        mcpman.set_server(ag, name, spec)
                        done.append(ag)
                    except Exception as e:
                        errs[ag] = str(e)[:200]
                collector.refresh("cfg")
                return {"name": name, "done": done, "errors": errs}
            if p == "/api/settings":
                from . import settings
                if "raw" in b:
                    settings.write_raw(b["raw"])
                    out = settings.view()
                else:
                    out = settings.save(b.get("values") or {})
                collector.refresh("all")
                return out
            if p == "/api/heal":
                from . import heal
                j = heal.start(b.get("cwd") or "~", b.get("cmd") or None, b.get("agent") or None, b.get("tries") or None,
                               b.get("mode") or None, b.get("model") or None, b.get("sandbox"))
                return j
            if p == "/api/heal/stop":
                from . import heal
                return heal.stop(b["id"]) or {}
            if p == "/api/heal/live":
                from . import feedback, heal
                heal.set_project(b["cwd"], b.get("cmd") or None)
                if b.get("cmd") and not feedback.installed():
                    feedback.install()
                return {"ok": True, "hooks": feedback.installed()}
            if p == "/api/phone":
                if phone:
                    raise PermissionError("change this from the computer")
                if b.get("rotate"):
                    rotate_phone_key()
                if b.get("on") and PHONE["srv"] is None:
                    from .util import config as _c
                    start_phone(collector, int(_c().get("gui", {}).get("phone_port", port + 1)), token)
                elif b.get("on") is False and PHONE["srv"] is not None:
                    PHONE["srv"].shutdown()
                    PHONE["srv"].server_close()
                    PHONE["srv"], PHONE["port"] = None, None
                return phone_info()
            if p == "/api/sandbox/test":
                from . import sandbox
                return sandbox.test()
            if p == "/api/hooks":
                from . import feedback
                feedback.install() if b.get("on") else feedback.uninstall()
                return {"ok": True, "hooks": feedback.installed()}
            if p == "/api/autostart":
                from . import plat
                return {"message": plat.autostart(bool(b.get("on")), ["daemon"] + (["--relay"] if b.get("relay") else []))}
            if p == "/api/keys":
                from . import fallback
                fallback.save(b["provider"], b.get("key") or "")
                collector.refresh("openrouter")
                return {"ok": True, "status": fallback.status()}
            if p == "/api/preview":
                from . import proxy
                pp = proxy.start(int(b["port"]), origin)
                return {"url": f"http://127.0.0.1:{pp}/", "origin": f"http://127.0.0.1:{pp}"}
            if p == "/api/feedback":
                from . import feedback
                return feedback.add(b["agent"], b["cwd"], b["text"], b.get("target"))
            if p == "/api/hooks/install":
                from . import feedback
                return {"command": feedback.install()}
            if p == "/api/handoff":
                from . import handoff
                h = handoff.prepare(b["agent"], b["cwd"], b.get("to") or None)
                if b.get("write_only"):
                    return {"to": h["to"], "path": h["path"], "terminal": None}
                res = handoff.launch(h)
                return {"to": h["to"], "path": h["path"], "terminal": res["terminal"]}
            if p == "/api/local/chat":
                from . import local
                text, model = local.chat(b["server"], b.get("prompt") or "", b.get("model") or None, timeout=300)
                return {"text": text, "model": model}
            if p == "/api/kill":
                pid = int(b["pid"])
                snap = collector.snapshot() or {}
                owned = {pr["pid"] for a in snap.get("agents", []) for pr in a["procs"]}
                if pid not in owned:
                    raise PermissionError("pid is not an agent process")
                from . import plat
                plat.kill(pid, bool(b.get("force")))
                return {"ok": True}
            raise ValueError("unknown endpoint")

    return H


def make_server(collector, port=0, token=None):
    token = token or secrets.token_urlsafe(24)
    srv = ThreadingHTTPServer(("127.0.0.1", port), None)
    real = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(collector, token, real)
    srv.daemon_threads = True
    srv.nyra_token = token
    return srv, f"http://127.0.0.1:{real}/"


def start_phone(collector, port, token):
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), None)
    except OSError:
        if _takeover(port):
            srv = ThreadingHTTPServer(("0.0.0.0", port), None)
        else:
            srv = ThreadingHTTPServer(("0.0.0.0", 0), None)
    real = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(collector, token, real, phone=True)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    PHONE["srv"], PHONE["port"] = srv, real
    return real


def serve_background(collector, port=0):
    srv, url = make_server(collector, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return url


def _takeover(port):
    import time
    import psutil
    for c in psutil.net_connections("inet"):
        if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid and c.pid != os.getpid():
            try:
                p = psutil.Process(c.pid)
                if "nyrasview" not in " ".join(p.cmdline()):
                    return False
                p.terminate()
                p.wait(5)
            except (psutil.Error, psutil.TimeoutExpired):
                try:
                    p.kill()
                except psutil.Error:
                    return False
            time.sleep(0.3)
            return True
    return False


def serve(collector, port=7341, open_browser=True, phone=False):
    try:
        srv, url = make_server(collector, port)
    except OSError:
        if _takeover(port):
            print(f"replaced an older nyrasview GUI that was running on port {port}")
            srv, url = make_server(collector, port)
        else:
            srv, url = make_server(collector, 0)
            print(f"port {port} is busy, using another one")
    print(f"nyrasview GUI → {url}   (ctrl+c to stop)")
    from .util import config as _c
    if phone or _c().get("gui", {}).get("phone"):
        from . import qr
        start_phone(collector, int(_c().get("gui", {}).get("phone_port", srv.server_address[1] + 1)), srv.nyra_token)
        info = phone_info()
        if info["urls"]:
            first = info["urls"][0]
            print(f"\nphone ({first['kind']}) → {first['url']}\nscan this with your phone camera (same Wi-Fi, or Tailscale):\n")
            print(qr.terminal(first["url"]))
            for u in info["urls"][1:]:
                print(f"  {u['kind']}: {u['url']}")
    if open_browser:
        open_url(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
