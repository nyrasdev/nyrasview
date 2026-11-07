import json
import os
import re
import time
import tomllib
from pathlib import Path

from .plat import config_file, state_dir

HOME = Path.home()
STATE = state_dir()
CONFIG = config_file()


def P(s):
    return Path(os.path.expanduser(s))


def hb(n):
    n = float(n or 0)
    for u in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or u == "T":
            return f"{n:.0f}{u}" if u in ("B", "K") else f"{n:.1f}{u}"
        n /= 1024


def hn(n):
    n = float(n or 0)
    for d, u in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= d:
            return f"{n / d:.1f}{u}"
    return f"{n:.0f}"


def ago(ts):
    if not ts:
        return "-"
    d = max(0, time.time() - ts)
    for s, u in ((86400, "d"), (3600, "h"), (60, "m")):
        if d >= s:
            return f"{int(d // s)}{u}"
    return f"{int(d)}s"


def strip_jsonc(s):
    out = []
    i, n, ins = 0, len(s), False
    while i < n:
        c = s[i]
        if ins:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(s[i + 1])
                i += 2
                continue
            if c == '"':
                ins = False
            i += 1
            continue
        if c == '"':
            ins = True
            out.append(c)
            i += 1
            continue
        if s.startswith("//", i):
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if s.startswith("/*", i):
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_json(path):
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return None
    try:
        return json.loads(text)
    except ValueError:
        try:
            return json.loads(strip_jsonc(text))
        except ValueError:
            return None


def load_toml(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def config():
    return load_toml(CONFIG) or {}


def frontmatter(path):
    try:
        with open(path, errors="replace") as f:
            head = f.read(6000)
    except OSError:
        return {}
    if not head.startswith("---"):
        m = re.search(r"^#\s+(.+)$", head, re.M)
        return {"name": m.group(1).strip()} if m else {}
    meta, key, buf = {}, None, []
    for line in head.splitlines()[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            if key and buf:
                meta[key] = " ".join(buf).strip()
            key, val = m.group(1), m.group(2).strip()
            if val in (">", "|", ">-", "|-", ""):
                buf = []
            else:
                meta[key] = val.strip("'\"")
                key, buf = None, []
        elif key is not None:
            buf.append(line.strip())
    if key and buf:
        meta[key] = " ".join(buf).strip()
    return meta


def day_start(ts=None):
    t = time.localtime(ts or time.time())
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def parse_ts(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 1e11 else float(v)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
