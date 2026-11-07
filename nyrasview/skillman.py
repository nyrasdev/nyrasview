import io
import re
import secrets
import shutil
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from .registry import BY_KEY
from .util import HOME, STATE, frontmatter

TARGETS = {
    "claude": "~/.claude/skills",
    "codex": "~/.codex/skills",
    "opencode": "~/.config/opencode/skill",
    "gemini": "~/.gemini/skills",
    "kimi": "~/.kimi/skills",
    "copilot": "~/.copilot/skills",
    "shared": "~/.agents/skills",
}
PRESETS = [
    {"name": "Anthropic skills", "source": "https://github.com/anthropics/skills", "about": "pdf, docx, xlsx, frontend design, MCP builder…"},
]
CACHE = {}


def _dir(agent):
    return Path(TARGETS[agent].replace("~", str(HOME), 1))


def targets():
    out = []
    for k in TARGETS:
        a = BY_KEY.get(k)
        out.append({"key": k, "name": a.name if a else "Shared (~/.agents)", "installed": (a.installed() if a else True)})
    return out


def _meta(skill_dir):
    m = frontmatter(skill_dir / "SKILL.md")
    return {"name": m.get("name") or skill_dir.name, "description": m.get("description", ""),
            "folder": skill_dir.name, "files": sum(1 for p in skill_dir.rglob("*") if p.is_file())}


def installed():
    rows = {}
    for k in TARGETS:
        d = _dir(k)
        if not d.is_dir():
            continue
        for sk in sorted(d.iterdir()):
            if (sk / "SKILL.md").is_file():
                m = _meta(sk)
                r = rows.setdefault(sk.name, {"folder": sk.name, "name": m["name"], "description": m["description"], "agents": []})
                r["agents"].append(k)
    return {"targets": targets(), "skills": sorted(rows.values(), key=lambda r: r["name"].lower()), "presets": PRESETS}


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "nyrasview"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _github(url):
    m = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)(?:/(?:tree|blob)/([^/]+)(?:/(.*))?)?", url)
    if not m:
        return None
    owner, repo, ref, sub = m.group(1), m.group(2).removesuffix(".git"), m.group(3) or "HEAD", m.group(4) or ""
    data = _get(f"https://codeload.github.com/{owner}/{repo}/zip/{ref}")
    tmp = Path(tempfile.mkdtemp(prefix="nyra-skill-"))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            parts = Path(info.filename).parts
            if not parts or ".." in parts or info.filename.startswith("/"):
                continue
            target = tmp.joinpath(*parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(info))
    roots = [p for p in tmp.iterdir() if p.is_dir()]
    base = roots[0] if len(roots) == 1 else tmp
    if sub:
        sub = re.sub(r"/SKILL\.md$", "", sub)
        base = base / sub
    return base


def fetch(source):
    source = (source or "").strip()
    if not source:
        raise ValueError("paste a GitHub URL, a folder path or a SKILL.md URL")
    if source.startswith("http") and "github.com" in source:
        base = _github(source)
    elif source.startswith("http"):
        data = _get(source)
        tmp = Path(tempfile.mkdtemp(prefix="nyra-skill-"))
        if source.endswith(".zip") or data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for info in z.infolist():
                    if ".." in Path(info.filename).parts or info.filename.startswith("/"):
                        continue
                    z.extract(info, tmp)
            base = tmp
        else:
            text = data.decode("utf-8", "replace")
            name = frontmatter_text(text).get("name") or Path(source).parent.name or "skill"
            d = tmp / re.sub(r"[^\w.-]", "-", name)
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(text, encoding="utf-8")
            base = tmp
    else:
        base = Path(source).expanduser()
        if base.name == "SKILL.md":
            base = base.parent
        if not base.exists():
            raise FileNotFoundError(source)
    found = []
    if (base / "SKILL.md").is_file():
        found.append(base)
    else:
        for p in sorted(base.rglob("SKILL.md")):
            if any(part in (".git", "node_modules") for part in p.parts):
                continue
            found.append(p.parent)
            if len(found) >= 200:
                break
    if not found:
        raise ValueError("no SKILL.md found there")
    fid = secrets.token_hex(4)
    CACHE[fid] = {"at": time.time(), "dirs": {d.name: d for d in found}}
    return {"id": fid, "skills": [_meta(d) for d in found]}


def frontmatter_text(text):
    tmp = Path(tempfile.mkdtemp()) / "SKILL.md"
    tmp.write_text(text, encoding="utf-8")
    try:
        return frontmatter(tmp)
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def _backup(path):
    b = STATE / "backups" / "skills" / time.strftime("%Y%m%d-%H%M%S")
    b.mkdir(parents=True, exist_ok=True)
    shutil.copytree(path, b / f"{path.parent.parent.name}-{path.name}", dirs_exist_ok=True)


def _put(src, agent, folder):
    dest = _dir(agent) / folder
    if dest.exists():
        _backup(dest)
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))
    return str(dest)


def install(fid, names, agents):
    c = CACHE.get(fid)
    if not c:
        raise ValueError("that search expired, find the skills again")
    picked = [n for n in (names or list(c["dirs"])) if n in c["dirs"]]
    done = []
    for n in picked:
        for ag in agents:
            if ag in TARGETS:
                done.append(_put(c["dirs"][n], ag, n))
    return done


def create(name, description, body, agents):
    folder = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "skill"
    tmp = Path(tempfile.mkdtemp(prefix="nyra-skill-")) / folder
    tmp.mkdir(parents=True)
    desc = " ".join((description or "").split())
    (tmp / "SKILL.md").write_text(f"---\nname: {folder}\ndescription: {desc}\n---\n\n{(body or '').strip()}\n", encoding="utf-8")
    out = [_put(tmp, ag, folder) for ag in agents if ag in TARGETS]
    shutil.rmtree(tmp.parent, ignore_errors=True)
    return out


def copy(folder, src, agents):
    s = _dir(src) / folder
    if not (s / "SKILL.md").is_file():
        raise FileNotFoundError(f"{folder} isn't installed for {src}")
    return [_put(s, ag, folder) for ag in agents if ag in TARGETS and ag != src]


def remove(folder, agents):
    out = []
    for ag in agents:
        d = _dir(ag) / folder
        if ag in TARGETS and d.is_dir() and (d / "SKILL.md").exists():
            _backup(d)
            shutil.rmtree(d)
            out.append(str(d))
    return out
