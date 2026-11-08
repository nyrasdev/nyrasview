import ipaddress
import json
import os
import queue
import socket
import threading
import time
import urllib.request

from .util import config

HOSTS = {
    "anthropic": ["api.anthropic.com", "claude.ai", "statsig.anthropic.com", "console.anthropic.com",
                  "mcp-proxy.anthropic.com"],
    "openai": ["api.openai.com", "chatgpt.com", "auth.openai.com", "ab.chatgpt.com"],
    "google": ["generativelanguage.googleapis.com", "cloudcode-pa.googleapis.com",
               "oauth2.googleapis.com", "aiplatform.googleapis.com", "play.googleapis.com"],
    "openrouter": ["openrouter.ai"],
    "moonshot": ["api.moonshot.ai", "api.moonshot.cn", "api.kimi.com", "kimi.com"],
    "cursor": ["api2.cursor.sh", "api.cursor.sh", "repo42.cursor.sh", "api3.cursor.sh"],
    "github": ["api.github.com", "github.com", "api.githubcopilot.com",
               "api.individual.githubcopilot.com"],
    "opencode": ["opencode.ai", "models.dev", "api.opencode.ai"],
    "deepseek": ["api.deepseek.com"],
    "xai": ["api.x.ai"],
    "mistral": ["api.mistral.ai"],
    "groq": ["api.groq.com"],
    "qwen": ["dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "chat.qwen.ai"],
    "sourcegraph": ["ampcode.com"],
    "factory": ["app.factory.ai", "api.factory.ai"],
}

SUFFIX = [
    ("anthropic.com", "anthropic"), ("claude.ai", "anthropic"), ("claude.com", "anthropic"),
    ("openai.com", "openai"), ("chatgpt.com", "openai"), ("oaiusercontent.com", "openai"),
    ("googleapis.com", "google"), ("google.com", "google"), ("1e100.net", "google"),
    ("googleusercontent.com", "gcp"), ("openrouter.ai", "openrouter"),
    ("moonshot.ai", "moonshot"), ("moonshot.cn", "moonshot"), ("kimi.com", "moonshot"),
    ("cursor.sh", "cursor"), ("cursor.com", "cursor"), ("github.com", "github"),
    ("githubcopilot.com", "github"), ("githubusercontent.com", "github"),
    ("opencode.ai", "opencode"), ("models.dev", "opencode"), ("deepseek.com", "deepseek"),
    ("x.ai", "xai"), ("mistral.ai", "mistral"), ("groq.com", "groq"),
    ("huggingface.co", "huggingface"), ("npmjs.org", "npm"), ("pypi.org", "pypi"),
    ("sentry.io", "telemetry"), ("datadoghq.com", "telemetry"), ("statsig.com", "telemetry"),
    ("segment.io", "telemetry"), ("amazonaws.com", "aws"), ("cloudfront.net", "aws"),
    ("cloudflare.com", "cloudflare"), ("ampcode.com", "sourcegraph"), ("factory.ai", "factory"),
]

CIDRS = [(ipaddress.ip_network("160.79.104.0/23"), "anthropic", "api.anthropic.com")]

LOCAL_PORTS = {11434: "ollama", 1234: "lmstudio", 4096: "opencode", 8080: "local"}


def _suffix(host):
    h = host.lower().rstrip(".")
    for suf, prov in SUFFIX:
        if h == suf or h.endswith("." + suf):
            return prov
    return None


class Resolver:
    def __init__(self):
        self.ip = {}
        self.q = queue.Queue()
        self.pending = set()
        self.lock = threading.Lock()
        threading.Thread(target=self._forward, daemon=True).start()
        for _ in range(2):
            threading.Thread(target=self._reverse, daemon=True).start()

    def _forward(self):
        while True:
            for prov, hosts in HOSTS.items():
                for h in hosts:
                    try:
                        for fam, _, _, _, sa in socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP):
                            with self.lock:
                                self.ip[sa[0]] = (h, prov)
                    except OSError:
                        pass
            time.sleep(600)

    def _reverse(self):
        while True:
            ip = self.q.get()
            try:
                host = socket.gethostbyaddr(ip)[0]
            except (OSError, UnicodeError):
                host = None
            with self.lock:
                if ip not in self.ip:
                    self.ip[ip] = (host or ip, _suffix(host) if host else None)
                self.pending.discard(ip)

    def classify(self, ip, port):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return ip, None
        if a.is_loopback:
            return f"localhost:{port}", LOCAL_PORTS.get(port, "local")
        if a.is_private or a.is_link_local:
            return f"{ip}:{port}", "lan"
        for net, prov, host in CIDRS:
            if a in net:
                return host, prov
        with self.lock:
            hit = self.ip.get(ip)
            if hit is None and ip not in self.pending:
                self.pending.add(ip)
                self.q.put(ip)
        if hit:
            return hit[0], hit[1]
        return ip, None


def _get(url, headers=None, timeout=4):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def openrouter_key():
    return os.environ.get("OPENROUTER_API_KEY") or config().get("openrouter", {}).get("api_key")


def openrouter():
    key = openrouter_key()
    if not key:
        return {"configured": False}
    try:
        d = _get("https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {key}"}).get("data", {})
    except Exception as e:
        return {"configured": True, "error": str(e)[:120]}
    return {"configured": True, "label": d.get("label"), "usage": d.get("usage"),
            "limit": d.get("limit"), "remaining": d.get("limit_remaining"), "reset": d.get("limit_reset"),
            "daily": d.get("usage_daily"), "weekly": d.get("usage_weekly"),
            "monthly": d.get("usage_monthly"), "free": d.get("is_free_tier")}


def ollama_host():
    h = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
    return h if h.startswith("http") else f"http://{h}"


def ollama():
    try:
        ps = _get(f"{ollama_host()}/api/ps", timeout=1.5)
    except Exception:
        return {"up": False}
    try:
        tags = _get(f"{ollama_host()}/api/tags", timeout=1.5)
    except Exception:
        tags = {}
    return {"up": True,
            "loaded": [{"name": m.get("name"), "size": m.get("size"), "vram": m.get("size_vram"),
                        "until": m.get("expires_at")} for m in ps.get("models", [])],
            "models": [m.get("name") for m in tags.get("models", [])]}
