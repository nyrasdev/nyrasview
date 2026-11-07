import os
import socket
import threading
import time
from collections import defaultdict, deque

import psutil

from . import VERSION, activity, collab, limits, local, mcp, net, notify, skills
from .registry import AGENTS, BY_KEY, identify
from .usage import UsageStore

SHELLS = {"bash", "zsh", "fish", "sh", "dash", "nu", "pwsh", "powershell", "cmd", "bash.exe", "cmd.exe",
          "powershell.exe", "pwsh.exe"}
ATTRS = ["pid", "ppid", "name", "exe", "cmdline", "cpu_percent", "memory_info", "create_time",
         "cwd", "num_threads"]


class Collector:
    def __init__(self, interval=1.5, background=True, watch=True, relay=False):
        self.interval = interval
        self.usage = UsageStore()
        self.resolver = net.Resolver()
        self.hist = defaultdict(lambda: deque(maxlen=90))
        self.slow = {"mcp": [], "skills": [], "usage": {}, "openrouter": {"configured": False},
                     "local": [], "collab": [], "limits": {}}
        self.watcher = notify.Watcher(relay=relay) if watch else None
        self.cwds = set()
        self.snap = None
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.me = os.getpid()
        if background:
            threading.Thread(target=self._slow_loop, daemon=True).start()
            threading.Thread(target=self._fast_loop, daemon=True).start()

    def _fast_loop(self):
        while True:
            try:
                s = self.tick()
                with self.lock:
                    self.snap = s
            except Exception as e:
                with self.lock:
                    self.snap = dict(self.snap or {}, error=repr(e))
            self.wake.wait(self.interval)
            self.wake.clear()

    def _slow_loop(self):
        last = defaultdict(float)
        periods = {"usage": 12, "cfg": 20, "openrouter": 90, "local": 6, "collab": 2, "limits": 10, "watch": 2}
        while True:
            now = time.time()
            for k, p in periods.items():
                if now - last[k] >= p:
                    last[k] = now
                    try:
                        self.refresh(k)
                    except Exception:
                        pass
            time.sleep(1)

    def refresh(self, what="all"):
        if what in ("usage", "all"):
            self.usage.refresh()
            self.slow["usage"] = self.usage.summary()
        if what in ("cfg", "all"):
            self.slow["mcp"] = mcp.configured(self.cwds)
            self.slow["skills"] = skills.scan(self.cwds)
        if what in ("openrouter", "all"):
            self.slow["openrouter"] = net.openrouter()
        if what in ("local", "all"):
            self.slow["local"] = local.scan()
        if what in ("limits", "all"):
            self.slow["limits"] = limits.snapshot(self.usage.records)
        if what == "watch" and self.watcher:
            self.watcher.tick(self.snapshot())
        if what in ("collab", "all"):
            self.slow["collab"] = collab.overview()

    def snapshot(self):
        with self.lock:
            return self.snap

    def tick(self):
        infos = {}
        for p in psutil.process_iter(ATTRS):
            i = p.info
            if i["pid"] == self.me:
                continue
            infos[i["pid"]] = i
        kids = defaultdict(list)
        for pid, i in infos.items():
            kids[i["ppid"]].append(pid)
        ident = {pid: identify(i) for pid, i in infos.items()}
        roots = defaultdict(list)
        for pid, key in ident.items():
            if not key:
                continue
            pp, own = infos[pid]["ppid"], False
            seen = 0
            while pp in infos and seen < 64:
                if ident.get(pp) == key:
                    own = True
                    break
                pp = infos[pp]["ppid"]
                seen += 1
            if not own:
                roots[key].append(pid)
        rootset = {p for v in roots.values() for p in v}

        conns = defaultdict(list)
        listen = defaultdict(list)
        from . import plat
        try:
            for c in plat.net_connections([pid for pid, k in ident.items() if k]):
                if c.pid is None:
                    continue
                if c.status == psutil.CONN_LISTEN and c.laddr:
                    listen[c.pid].append(c.laddr.port)
                elif c.raddr and c.status == psutil.CONN_ESTABLISHED:
                    conns[c.pid].append((c.raddr.ip, c.raddr.port))
        except (psutil.AccessDenied, OSError):
            pass

        cfg_mcp = self.slow["mcp"]
        collab_pids = {}
        for d in self.slow.get("collab", []):
            for t in d.get("tasks", []):
                if t.get("status") == "running" and t.get("pid"):
                    collab_pids[t["pid"]] = (d["doc"], t)

        from . import runs as _runs
        run_pids = _runs.active_pids()
        ncpu = psutil.cpu_count() or 1
        agents_out = []
        cwds = set()
        for a in AGENTS:
            procs = []
            for r in roots.get(a.key, []):
                stack = [(r, 0, "main", None)]
                while stack:
                    pid, depth, role, mname = stack.pop()
                    i = infos.get(pid)
                    if not i:
                        continue
                    argv = i.get("cmdline") or []
                    if pid == r:
                        role = "collab" if pid in collab_pids else "run" if pid in run_pids else "main"
                    elif mname is None:
                        hit = next((s for s in cfg_mcp if s["agent"] in (a.key,) and mcp.matches(argv, s)), None)
                        if hit is None:
                            hit = next((s for s in cfg_mcp if mcp.matches(argv, s)), None)
                        if hit:
                            role, mname = "mcp", hit["name"]
                        else:
                            g = mcp.guess(argv)
                            if g:
                                role, mname = "mcp?", g
                            elif (i.get("name") or "") in SHELLS:
                                role = "shell"
                            else:
                                role = "child"
                    mem = i.get("memory_info")
                    cl = conns.get(pid, [])
                    eps = []
                    for ip, port in cl:
                        label, prov = self.resolver.classify(ip, port)
                        eps.append({"host": label, "provider": prov or "other", "port": port})
                    if role == "main" and i.get("cwd"):
                        cwds.add(i["cwd"])
                    procs.append({
                        "pid": pid, "ppid": i["ppid"], "depth": depth, "role": role, "mcp": mname,
                        "name": i.get("name") or "?", "cmd": " ".join(argv)[:400] or i.get("name"),
                        "cpu": round(i.get("cpu_percent") or 0.0, 1),
                        "rss": mem.rss if mem else 0, "threads": i.get("num_threads") or 0,
                        "cwd": i.get("cwd"), "started": i.get("create_time") or 0,
                        "conns": eps, "listen": sorted(set(listen.get(pid, []))),
                        "collab": collab_pids[pid][1].get("id") if pid in collab_pids else None,
                    })
                    for c in sorted(kids.get(pid, []), reverse=True):
                        if c in rootset:
                            continue
                        stack.append((c, depth + 1, role if role in ("mcp", "mcp?") else None,
                                      mname if role in ("mcp", "mcp?") else None))
            cpu = sum(p["cpu"] for p in procs)
            rss = sum(p["rss"] for p in procs)
            prov = defaultdict(int)
            hosts = defaultdict(lambda: {"count": 0, "provider": None})
            for p in procs:
                for e in p["conns"]:
                    prov[e["provider"]] += 1
                    h = hosts[e["host"]]
                    h["count"] += 1
                    h["provider"] = e["provider"]
            self.hist[a.key].append((round(cpu, 1), rss))
            amcp = [s for s in cfg_mcp if s["agent"] == a.key]
            live = {}
            for p in procs:
                if p["role"] in ("mcp", "mcp?") and p["mcp"]:
                    m = live.setdefault(p["mcp"], {"pids": [], "cpu": 0.0, "rss": 0, "guessed": p["role"] == "mcp?"})
                    m["pids"].append(p["pid"])
                    m["cpu"] += p["cpu"]
                    m["rss"] += p["rss"]
            sessions = []
            for p in procs:
                if p["role"] in ("main", "collab", "run") and p["depth"] == 0:
                    act = activity.get(a.key, p["cwd"]) if p["role"] == "main" else None
                    tree_cpu = sum(q["cpu"] for q in procs if q["pid"] == p["pid"] or q["depth"] > 0)
                    if act and act["state"] not in ("working", "limited") and tree_cpu > 3 and time.time() - (act.get("mtime") or 0) < 1800:
                        act["state"] = "working"
                    sessions.append({"pid": p["pid"], "cwd": p["cwd"], "started": p["started"],
                                     "cpu": p["cpu"], "collab": p["role"] in ("collab", "run"),
                                     "run": run_pids[p["pid"]]["id"] if p["pid"] in run_pids else None, "activity": act})
            usage = self.slow["usage"].get(a.key, {})
            nskills = sum(1 for s in self.slow["skills"] if s["agent"] == a.key)
            installed = a.installed()
            visible = bool(procs or installed or amcp or usage or nskills)
            if a.key == "openrouter":
                visible = bool(self.slow["openrouter"].get("configured") or prov)
            lsrv = next((s for s in self.slow["local"] if a.headless and s["key"] == a.headless.get("server")), None)
            if lsrv and lsrv["up"]:
                visible = installed = True
            agents_out.append({
                "key": a.key, "name": a.name, "hex": a.hex, "color": a.color, "kind": a.kind,
                "installed": installed, "binary": a.binary(), "visible": visible,
                "headless": bool(a.headless), "running": bool(procs),
                "sessions": len(roots.get(a.key, [])), "nprocs": len(procs), "work": sessions,
                "server": lsrv,
                "cpu": round(cpu, 1), "cpu_share": round(cpu / ncpu, 1), "rss": rss,
                "procs": procs, "providers": dict(prov),
                "hosts": sorted(({"host": k, **v} for k, v in hosts.items()), key=lambda x: -x["count"]),
                "listen": sorted({x for p in procs for x in p["listen"]}),
                "mcp_configured": len(amcp), "mcp_live": live,
                "skills": nskills, "usage": usage,
                "hist": list(self.hist[a.key]),
            })
        self.cwds = cwds
        vm = psutil.virtual_memory()
        return {
            "ts": time.time(), "version": VERSION, "host": socket.gethostname(),
            "system": {"cpu": psutil.cpu_percent(), "ncpu": ncpu, "mem_used": vm.total - vm.available,
                       "mem_total": vm.total},
            "agents": agents_out, "mcp": cfg_mcp, "skills": self.slow["skills"],
            "openrouter": self.slow["openrouter"], "local": self.slow["local"],
            "collab": self.slow["collab"], "limits": self.slow["limits"],
        }


def oneshot():
    c = Collector(background=False)
    c.refresh("cfg")
    c.tick()
    time.sleep(0.6)
    c.refresh("all")
    return c.tick()
