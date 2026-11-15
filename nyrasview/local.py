import json
import os
import threading
import time
import urllib.error
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


PULLS = {}


def _req(url, payload=None, key=None, timeout=1.0, method=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw.strip() else {}


def _ollama(key):
    s = server(key)
    if not s:
        raise RuntimeError(f"unknown local server {key}")
    if s["api"] != "ollama":
        raise RuntimeError(f"{s['name']} can't do this: only Ollama servers manage models through nyrasview")
    return s, s["url"].rstrip("/")


def _fail(e, s):
    if isinstance(e, urllib.error.HTTPError):
        try:
            return json.loads(e.read().decode()).get("error") or f"{s['name']} error {e.code}"
        except Exception:
            return f"{s['name']} error {e.code}"
    return f"{s['name']} is not running at {s['url']}"


def pull_iter(key, model):
    s, base = _ollama(key)
    r = urllib.request.Request(f"{base}/api/pull", data=json.dumps({"model": model, "stream": True}).encode(),
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            for line in resp:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("error"):
                    raise RuntimeError(o["error"])
                yield {"status": o.get("status") or "", "completed": o.get("completed"), "total": o.get("total")}
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(_fail(e, s))


def _pull_thread(key, model):
    st = PULLS[(key, model)]
    try:
        for p in pull_iter(key, model):
            st.update(status=p["status"])
            if p.get("total"):
                st.update(completed=p.get("completed") or 0, total=p["total"])
        st.update(done=True, status="done", finished=time.time())
    except Exception as e:
        st.update(done=True, error=str(e)[:300], finished=time.time())
    try:
        from . import notify
        from .registry import BY_KEY
        name = BY_KEY[key].name if key in BY_KEY else key
        if st.get("error"):
            notify.emit("failed", key, f"{name} couldn't download {model}", st["error"], f"pull|{model}|{st['started']}")
        else:
            notify.emit("done", key, f"{name} downloaded {model}", "Ready to use.", f"pull|{model}|{st['started']}")
    except Exception:
        pass


def pull(key, model):
    model = (model or "").strip()
    if not model:
        raise ValueError("name a model, e.g. qwen3:8b")
    _ollama(key)
    cur = PULLS.get((key, model))
    if cur and not cur.get("done"):
        return cur
    PULLS[(key, model)] = {"model": model, "status": "starting", "completed": 0, "total": 0, "done": False,
                           "error": None, "started": time.time()}
    threading.Thread(target=_pull_thread, args=(key, model), daemon=True).start()
    return PULLS[(key, model)]


def pulls(key):
    now = time.time()
    return [v for (k, _), v in PULLS.items() if k == key and (not v.get("done") or now - v.get("finished", now) < 30)]


def delete(key, model):
    s, base = _ollama(key)
    try:
        _req(f"{base}/api/delete", {"model": model}, timeout=30, method="DELETE")
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(_fail(e, s))
    return True


def load(key, model, unload=False, keep=None):
    s, base = _ollama(key)
    body = {"model": model, "keep_alive": 0} if unload else {"model": model}
    if keep and not unload:
        body["keep_alive"] = keep
    try:
        _req(f"{base}/api/generate", body, timeout=300)
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(_fail(e, s))
    return True


def run_command(key, model):
    _ollama(key)
    return ["ollama", "run", model]


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
                       "until": m.get("expires_at"), "context": m.get("context_length"),
                       "params": (m.get("details") or {}).get("parameter_size"),
                       "quant": (m.get("details") or {}).get("quantization_level")} for m in ps.get("models", [])]
        else:
            d = _req(f"{base}/v1/models", key=s.get("api_key"), timeout=0.8)
            models = [{"id": m.get("id")} for m in d.get("data", [])]
            loaded = [{"id": m.get("id")} for m in d.get("data", []) if m.get("state") == "loaded"]
        return {**{k: v for k, v in s.items() if k != "api_key"}, "up": True, "models": models, "loaded": loaded,
                "pulls": pulls(s["key"])}
    except Exception:
        return {**{k: v for k, v in s.items() if k != "api_key"}, "up": False, "models": [], "loaded": [],
                "pulls": pulls(s["key"])}


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
