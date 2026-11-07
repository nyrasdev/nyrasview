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


def make_handler(collector, token, port):
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
            return self.headers.get("Host", "") in allowed_hosts

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            u = urlparse(self.path)
            q = parse_qs(u.query)
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
                    v["siblings"] = [r["id"] for r in runs.listing(200) if r.get("group") == v["group"]]
                return self._send(200 if v else 404, v or {"error": "no such run"})
            if u.path == "/api/mcp/matrix":
                from . import mcpman
                return self._send(200, mcpman.matrix())
            if u.path == "/api/events":
                from . import notify
                return self._send(200, notify.recent(40, float(q.get("since", ["0"])[0] or 0)))
            if u.path == "/api/handoff/targets":
                from . import handoff
                return self._send(200, [{"key": k, "name": BY_KEY[k].name, "hex": BY_KEY[k].hex}
                                        for k in handoff.targets((q.get("exclude") or [None])[0])])
            if u.path == "/api/agents":
                snap = collector.snapshot() or {}
                vis = {x["key"]: x for x in snap.get("agents", [])}
                return self._send(200, [{"key": a.key, "name": a.name, "hex": a.hex, "kind": a.kind,
                                         "installed": a.kind == "api" or bool(vis.get(a.key, {}).get("installed"))}
                                        for a in BY_KEY.values() if a.headless])
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Nyra-Token") != token:
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
                                  group, b.get("parent")) for a in ags]
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


def make_server(collector, port=0):
    token = secrets.token_urlsafe(24)
    srv = ThreadingHTTPServer(("127.0.0.1", port), None)
    real = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(collector, token, real)
    srv.daemon_threads = True
    return srv, f"http://127.0.0.1:{real}/"


def serve_background(collector, port=0):
    srv, url = make_server(collector, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return url


def serve(collector, port=7341, open_browser=True):
    try:
        srv, url = make_server(collector, port)
    except OSError:
        srv, url = make_server(collector, 0)
    print(f"nyrasview GUI → {url}   (ctrl+c to stop)")
    if open_browser:
        open_url(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
