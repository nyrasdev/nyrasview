import json
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .util import config

DEFAULTS = [
    {"key": "ollama", "name": "Ollama", "url": "http://127.0.0.1:11434", "api": "ollama"},
    {"key": "lmstudio", "name": "LM Studio", "url": "http://127.0.0.1:1234", "api": "openai"},
    {"key": "llamacpp", "name": "llama.cpp", "url": "http://127.0.0.1:8080", "api": "openai"},
    {"key": "vllm", "name": "vLLM", "url": "http://127.0.0.1:8000", "api": "openai"},
    {"key": "jan", "name": "Jan", "url": "http://127.0.0.1:1337", "api": "openai"},
    {"key": "koboldcpp", "name": "KoboldCpp", "url": "http://127.0.0.1:5001", "api": "openai"},
]


def servers():
    out = [dict(s) for s in DEFAULTS]
    oh = os.environ.get("OLLAMA_HOST")
    if oh:
        out[0]["url"] = oh if oh.startswith("http") else f"http://{oh}"
    for s in config().get("local", []) or []:
        if not isinstance(s, dict) or not s.get("url"):
            continue
        s = {"key": s.get("key") or s.get("name", "local").lower().replace(" ", "-"),
             "name": s.get("name") or s.get("key"), "url": s["url"].rstrip("/"),
             "api": s.get("api", "openai"), "model": s.get("model"), "api_key": s.get("api_key")}
        for i, d in enumerate(out):
            if d["key"] == s["key"]:
                out[i] = {**d, **{k: v for k, v in s.items() if v}}
                break
        else:
            out.append(s)
    return out


def server(key):
    return next((s for s in servers() if s["key"] == key), None)


def _req(url, payload=None, key=None, timeout=1.0):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def probe(s):
    base = s["url"].rstrip("/")
    try:
        if s["api"] == "ollama":
            tags = _req(f"{base}/api/tags", timeout=0.8)
            try:
                ps = _req(f"{base}/api/ps", timeout=0.8)
            except Exception:
                ps = {}
            models = [{"id": m.get("name"), "size": m.get("size"),
                       "params": (m.get("details") or {}).get("parameter_size"),
                       "quant": (m.get("details") or {}).get("quantization_level")} for m in tags.get("models", [])]
            loaded = [{"id": m.get("name"), "size": m.get("size"), "vram": m.get("size_vram"),
                       "until": m.get("expires_at")} for m in ps.get("models", [])]
        else:
            d = _req(f"{base}/v1/models", key=s.get("api_key"), timeout=0.8)
            models = [{"id": m.get("id")} for m in d.get("data", [])]
            loaded = [{"id": m.get("id")} for m in d.get("data", []) if m.get("state") == "loaded"]
        return {**{k: v for k, v in s.items() if k != "api_key"}, "up": True, "models": models, "loaded": loaded}
    except Exception:
        return {**{k: v for k, v in s.items() if k != "api_key"}, "up": False, "models": [], "loaded": []}


def scan():
    ss = servers()
    with ThreadPoolExecutor(max_workers=len(ss)) as ex:
        return list(ex.map(probe, ss))


def chat(key, prompt, model=None, timeout=900):
    s = server(key)
    if not s:
        raise RuntimeError(f"unknown local server {key}")
    info = probe(s)
    if not info["up"]:
        raise RuntimeError(f"{s['name']} is not running at {s['url']}")
    model = model or s.get("model") or (info["loaded"] or info["models"] or [{}])[0].get("id")
    if not model:
        raise RuntimeError(f"{s['name']} has no models")
    base = s["url"].rstrip("/")
    if s["api"] == "ollama":
        d = _req(f"{base}/api/chat", {"model": model, "stream": False,
                                       "messages": [{"role": "user", "content": prompt}]}, timeout=timeout)
        return (d.get("message") or {}).get("content", ""), model
    d = _req(f"{base}/v1/chat/completions", {"model": model, "messages": [{"role": "user", "content": prompt}]},
             key=s.get("api_key"), timeout=timeout)
    return d["choices"][0]["message"]["content"] or "", model
