import atexit
import io
import os
import platform
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.request

from . import plat
from .util import STATE

BIN = STATE / "bin"
ST = {"proc": None, "url": None, "pending": None, "registered": False, "error": None, "starting": False, "downloading": False}
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
REL = "https://github.com/cloudflare/cloudflared/releases/latest/download/"


def binary():
    w = shutil.which("cloudflared")
    if w:
        return w
    local = BIN / ("cloudflared.exe" if plat.WIN else "cloudflared")
    return str(local) if local.exists() else None


def _asset():
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "arm" if m.startswith("arm") else "amd64"
    if plat.WIN:
        return f"cloudflared-windows-{arch}.exe", False
    if plat.MAC:
        return f"cloudflared-darwin-{'arm64' if arch == 'arm64' else 'amd64'}.tgz", True
    return f"cloudflared-linux-{arch}", False


def install_hint():
    if plat.WIN:
        return "winget install --id Cloudflare.cloudflared"
    if plat.MAC:
        return "brew install cloudflared"
    if shutil.which("pacman"):
        return "sudo pacman -S cloudflared"
    if shutil.which("apt"):
        return "see https://pkg.cloudflare.com (or let nyrasview download it)"
    return "let nyrasview download it"


def download():
    name, tgz = _asset()
    ST["downloading"], ST["error"] = True, None
    try:
        req = urllib.request.Request(REL + name, headers={"User-Agent": "nyrasview"})
        with urllib.request.urlopen(req, timeout=180) as r:
            data = r.read()
        BIN.mkdir(parents=True, exist_ok=True)
        target = BIN / ("cloudflared.exe" if plat.WIN else "cloudflared")
        if tgz:
            with tarfile.open(fileobj=io.BytesIO(data)) as t:
                member = next(m for m in t.getmembers() if m.name.endswith("cloudflared"))
                target.write_bytes(t.extractfile(member).read())
        else:
            target.write_bytes(data)
        if not plat.WIN:
            target.chmod(0o755)
        return str(target)
    except Exception as e:
        ST["error"] = f"download failed: {str(e)[:160]}"
        raise
    finally:
        ST["downloading"] = False


def _probe(url):
    end = time.time() + 90
    while time.time() < end and ST["proc"]:
        try:
            req = urllib.request.Request(url + "/manifest.webmanifest", headers={"User-Agent": "nyrasview"})
            urllib.request.urlopen(req, timeout=8).read(64)
            ok = True
        except urllib.error.HTTPError as e:
            ok = e.code not in (502, 503, 504, 520, 521, 522, 523, 524, 530)
        except Exception:
            ok = False
        if ok:
            ST["url"], ST["starting"], ST["error"] = url, False, None
            return
        time.sleep(2)
    if ST["proc"] and not ST["url"]:
        ST["url"], ST["starting"] = url, False
        ST["error"] = "the link may take a few more seconds to work"


def _reader(proc):
    for line in iter(proc.stderr.readline, b""):
        text = line.decode(errors="replace")
        m = URL_RE.search(text)
        if m and not ST["pending"]:
            ST["pending"] = m.group(0)
        if "Registered tunnel connection" in text and not ST["registered"] and ST["pending"]:
            ST["registered"] = True
            threading.Thread(target=_probe, args=(ST["pending"],), daemon=True).start()
    if ST["proc"] is proc:
        ST["proc"], ST["starting"] = None, False
        if not ST["url"]:
            ST["error"] = "cloudflared stopped before giving a link (no internet?)"
        ST["url"] = None


def start(port, wait=90):
    if ST["proc"] and ST["proc"].poll() is None:
        return info()
    exe = binary()
    if not exe:
        raise RuntimeError("cloudflared isn't installed")
    ST.update(url=None, pending=None, registered=False, error=None, starting=True)
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **plat.detached())
    ST["proc"] = proc
    threading.Thread(target=_reader, args=(proc,), daemon=True).start()
    end = time.time() + wait
    while time.time() < end and not ST["url"] and proc.poll() is None:
        time.sleep(0.3)
    if not ST["url"] and proc.poll() is None:
        ST["error"] = "still waiting for Cloudflare…"
    return info()


def stop():
    p = ST["proc"]
    ST.update(proc=None, url=None, pending=None, registered=False, starting=False)
    if p and p.poll() is None:
        plat.kill_tree(p.pid)


atexit.register(stop)


def info():
    return {"installed": bool(binary()), "running": bool(ST["proc"] and ST["proc"].poll() is None),
            "url": ST["url"], "starting": ST["starting"], "error": ST["error"], "downloading": ST["downloading"],
            "hint": install_hint()}
