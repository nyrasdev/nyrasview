import http.client
import re
import select
import socket
import threading
from pathlib import Path

OVERLAY = Path(__file__).with_name("static") / "overlay.js"
_servers = {}
_lock = threading.Lock()
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "content-length", "content-encoding", "accept-encoding", "host"}


def _read_head(conn):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return None, b""
        data += chunk
        if len(data) > 1 << 20:
            return None, b""
    head, rest = data.split(b"\r\n\r\n", 1)
    return head.decode("latin-1"), rest


def _pipe(a, b):
    socks = [a, b]
    try:
        while True:
            r, _, _ = select.select(socks, [], [], 300)
            if not r:
                return
            for s in r:
                d = s.recv(65536)
                if not d:
                    return
                (b if s is a else a).sendall(d)
    except OSError:
        return
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


def _handle(conn, target, parent_origin):
    try:
        head, rest = _read_head(conn)
        if not head:
            conn.close()
            return
        lines = head.split("\r\n")
        method, path, _ = (lines[0].split(" ", 2) + ["", ""])[:3]
        headers = []
        for l in lines[1:]:
            if ":" in l:
                k, v = l.split(":", 1)
                headers.append((k.strip(), v.strip()))
        hmap = {k.lower(): v for k, v in headers}
        if path.startswith("/__nyra/overlay.js"):
            body = OVERLAY.read_bytes().replace(b"__NYRA_PARENT__", parent_origin.encode())
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/javascript\r\nCache-Control: no-store\r\n"
                         + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            conn.close()
            return
        if "upgrade" in hmap.get("connection", "").lower():
            up = socket.create_connection(("127.0.0.1", target), timeout=10)
            fixed = [lines[0]]
            for k, v in headers:
                if k.lower() == "host":
                    v = f"localhost:{target}"
                elif k.lower() == "origin":
                    v = f"http://localhost:{target}"
                fixed.append(f"{k}: {v}")
            up.sendall(("\r\n".join(fixed) + "\r\n\r\n").encode("latin-1") + rest)
            up.settimeout(None)
            conn.settimeout(None)
            _pipe(conn, up)
            return
        length = int(hmap.get("content-length") or 0)
        body = rest
        while len(body) < length:
            chunk = conn.recv(65536)
            if not chunk:
                break
            body += chunk
        fwd = {k: v for k, v in headers if k.lower() not in HOP}
        fwd["Host"] = f"localhost:{target}"
        if "origin" in hmap:
            fwd["Origin"] = f"http://localhost:{target}"
        if "referer" in hmap:
            fwd["Referer"] = re.sub(r"^https?://[^/]+", f"http://localhost:{target}", hmap["referer"])
        fwd["Accept-Encoding"] = "identity"
        up = http.client.HTTPConnection("127.0.0.1", target, timeout=60)
        up.request(method, path, body=body or None, headers=fwd)
        r = up.getresponse()
        data = r.read()
        ctype = r.getheader("Content-Type") or ""
        if "text/html" in ctype:
            tag = b'<script src="/__nyra/overlay.js" defer></script>'
            if re.search(rb"</head>", data, re.I):
                data = re.sub(rb"</head>", tag + b"</head>", data, count=1, flags=re.I)
            else:
                data = tag + data
        out = [f"HTTP/1.1 {r.status} {r.reason}"]
        for k, v in r.getheaders():
            kl = k.lower()
            if kl in HOP or kl in ("content-security-policy", "x-frame-options"):
                continue
            if kl == "location":
                v = re.sub(rf"^https?://(localhost|127\.0\.0\.1):{target}", "", v)
            out.append(f"{k}: {v}")
        out += [f"Content-Length: {len(data)}", "Connection: close"]
        conn.sendall(("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + data)
        up.close()
    except Exception as e:
        try:
            msg = f"nyrasview preview: cannot reach localhost:{target} ({e})".encode()
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\n" +
                         f"Content-Length: {len(msg)}\r\nConnection: close\r\n\r\n".encode() + msg)
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _serve(sock, target, parent_origin):
    while True:
        try:
            c, _ = sock.accept()
        except OSError:
            return
        c.settimeout(60)
        threading.Thread(target=_handle, args=(c, target, parent_origin), daemon=True).start()


def start(target, parent_origin):
    target = int(target)
    with _lock:
        if target in _servers:
            return _servers[target]
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(64)
        port = s.getsockname()[1]
        threading.Thread(target=_serve, args=(s, target, parent_origin), daemon=True).start()
        _servers[target] = port
        return port
