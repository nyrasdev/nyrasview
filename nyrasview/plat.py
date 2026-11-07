import os
import shlex
import shutil
import signal
import subprocess
import sys
import webbrowser
from contextlib import contextmanager
from pathlib import Path

import psutil

WIN = os.name == "nt"
MAC = sys.platform == "darwin"
HOME = Path.home()


def state_dir():
    if WIN:
        return Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData/Local") / "nyrasview"
    if MAC:
        return HOME / "Library/Application Support/nyrasview"
    return Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state") / "nyrasview"


def config_file():
    if WIN:
        return Path(os.environ.get("APPDATA") or HOME / "AppData/Roaming") / "nyrasview" / "config.toml"
    if MAC and not os.environ.get("XDG_CONFIG_HOME"):
        return HOME / "Library/Application Support/nyrasview/config.toml"
    return Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "nyrasview" / "config.toml"


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        if WIN:
            import msvcrt
            import time
            f.seek(0)
            for _ in range(600):
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def alive(pid):
    if not pid:
        return False
    try:
        p = psutil.Process(int(pid))
        return p.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return psutil.pid_exists(int(pid)) if pid else False


def kill(pid, force=False):
    try:
        p = psutil.Process(int(pid))
    except psutil.NoSuchProcess:
        return False
    (p.kill if force else p.terminate)()
    return True


def kill_tree(pid, force=False):
    try:
        root = psutil.Process(int(pid))
    except psutil.NoSuchProcess:
        return False
    procs = root.children(recursive=True) + [root]
    for p in procs:
        try:
            (p.kill if force else p.terminate)()
        except psutil.Error:
            pass
    return True


def detached():
    if WIN:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def which(name):
    return shutil.which(name)


def resolve(argv):
    if not argv:
        return argv
    exe = shutil.which(argv[0]) or argv[0]
    if WIN and exe.lower().endswith(".cmd"):
        try:
            import re
            txt = Path(exe).read_text(errors="replace")
            m = re.search(r'"%~?dp0%?\\([^"]+?\.(?:js|cjs|mjs))"', txt)
            if m:
                script = Path(exe).parent / m.group(1)
                node = Path(exe).parent / "node.exe"
                if script.exists():
                    return [str(node) if node.exists() else (shutil.which("node") or "node"), str(script), *argv[1:]]
        except OSError:
            pass
    if WIN and exe.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", exe, *argv[1:]]
    if WIN and exe.lower().endswith(".ps1"):
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", exe, *argv[1:]]
    return [exe, *argv[1:]]


def split(text):
    return shlex.split(text, posix=not WIN)


def quote_cmd(argv):
    return subprocess.list2cmdline(argv) if WIN else shlex.join(argv)


def _ps(script):
    subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detached())


def _ps_str(s):
    return "'" + str(s).replace("'", "''") + "'"


def notify(title, body, urgent=False):
    body = (body or "")[:300]
    try:
        if WIN:
            xml = ("<toast" + (' scenario="reminder"' if urgent else "") + "><visual><binding template='ToastGeneric'>"
                   "<text>{t}</text><text>{b}</text></binding></visual></toast>")
            esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            _ps("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
                "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
                f"$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml({_ps_str(xml.format(t=esc(title), b=esc(body)))});"
                "$n = [Windows.UI.Notifications.ToastNotification]::new($x);"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('nyrasview').Show($n)")
            return True
        if MAC:
            subprocess.Popen(["osascript", "-e", f"display notification {_as(body)} with title {_as(title)}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "nyrasview", "-u", "critical" if urgent else "normal", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except OSError:
        pass
    return False


def _as(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def copy(text):
    try:
        if WIN:
            subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $input"],
                           input=text.encode("utf-8"), timeout=8, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if MAC:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if shutil.which("wl-copy") and os.environ.get("WAYLAND_DISPLAY"):
            subprocess.run(["wl-copy"], input=text.encode("utf-8"), timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if shutil.which("xclip"):
            subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"), timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if shutil.which("xsel"):
            subprocess.run(["xsel", "-ib"], input=text.encode("utf-8"), timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if shutil.which("wl-copy"):
            subprocess.run(["wl-copy"], input=text.encode("utf-8"), timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


def open_url(url):
    try:
        if WIN:
            os.startfile(url)
            return
        opener = "open" if MAC else shutil.which("xdg-open")
        if opener:
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detached())
            return
    except OSError:
        pass
    webbrowser.open(url)


LINUX_TERMS = [
    ("kitty", lambda cwd, cmd: ["kitty", "--directory", cwd, *cmd]),
    ("ghostty", lambda cwd, cmd: ["ghostty", f"--working-directory={cwd}", "-e", *cmd]),
    ("wezterm", lambda cwd, cmd: ["wezterm", "start", "--cwd", cwd, "--", *cmd]),
    ("foot", lambda cwd, cmd: ["foot", "-D", cwd, *cmd]),
    ("alacritty", lambda cwd, cmd: ["alacritty", "--working-directory", cwd, "-e", *cmd]),
    ("konsole", lambda cwd, cmd: ["konsole", "--workdir", cwd, "-e", *cmd]),
    ("gnome-terminal", lambda cwd, cmd: ["gnome-terminal", f"--working-directory={cwd}", "--", *cmd]),
    ("xterm", lambda cwd, cmd: ["xterm", "-e", "sh", "-c", f"cd {shlex.quote(cwd)} && exec {shlex.join(cmd)}"]),
]


def terminal(cwd, cmd, prefer=None):
    if WIN:
        cmd = resolve(cmd)
        if shutil.which("wt"):
            subprocess.Popen(["wt", "-d", cwd, *cmd], cwd=cwd, **detached())
            return "Windows Terminal"
        subprocess.Popen(cmd, cwd=cwd, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return "console"
    if MAC:
        script = f"cd {shlex.quote(cwd)} && {shlex.join(cmd)}"
        if prefer and "iterm" in prefer.lower():
            subprocess.Popen(["osascript", "-e", f'tell application "iTerm" to create window with default profile command {_as(script)}'])
            return "iTerm"
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script {_as(script)}', "-e",
                          'tell application "Terminal" to activate'])
        return "Terminal"
    order = sorted(LINUX_TERMS, key=lambda t: t[0] != (os.path.basename(prefer) if prefer else ""))
    for name, fn in order:
        if shutil.which(name):
            subprocess.Popen(fn(cwd, cmd), cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detached())
            return name
    raise RuntimeError("no terminal emulator found; set handoff.terminal in config")


def exe_cmd():
    exe = shutil.which("nyrasview")
    return [exe] if exe else [sys.executable, "-m", "nyrasview"]


def autostart(enable, args):
    cmd = exe_cmd() + args
    if WIN:
        if not enable:
            r = subprocess.run(["schtasks", "/Delete", "/TN", "nyrasview", "/F"], capture_output=True, text=True)
            return "removed the nyrasview logon task" if r.returncode == 0 else (r.stderr or r.stdout).strip()
        pyw = cmd
        if cmd[0].lower().endswith("python.exe"):
            pyw = [cmd[0][:-10] + "pythonw.exe", *cmd[1:]]
        r = subprocess.run(["schtasks", "/Create", "/TN", "nyrasview", "/SC", "ONLOGON", "/RL", "LIMITED", "/F",
                            "/TR", subprocess.list2cmdline(pyw)], capture_output=True, text=True)
        return "nyrasview will start at logon (Task Scheduler: nyrasview)" if r.returncode == 0 else (r.stderr or r.stdout).strip()
    if MAC:
        plist = HOME / "Library/LaunchAgents/dev.nyras.nyrasview.plist"
        if not enable:
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
            plist.unlink(missing_ok=True)
            return "removed the launch agent"
        plist.parent.mkdir(parents=True, exist_ok=True)
        items = "".join(f"<string>{a}</string>" for a in cmd)
        plist.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                         '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
                         '<key>Label</key><string>dev.nyras.nyrasview</string>'
                         f'<key>ProgramArguments</key><array>{items}</array>'
                         '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>\n', encoding="utf-8")
        subprocess.run(["launchctl", "load", "-w", str(plist)], capture_output=True)
        return f"loaded {plist}"
    unit = HOME / ".config/systemd/user/nyrasview.service"
    if not enable:
        subprocess.run(["systemctl", "--user", "disable", "--now", "nyrasview.service"], capture_output=True)
        unit.unlink(missing_ok=True)
        return "removed nyrasview.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Unit]\nDescription=nyrasview alerts and relay\nAfter=graphical-session.target\n\n[Service]\n"
                    f"ExecStart={shlex.join(cmd)}\nRestart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n",
                    encoding="utf-8")
    r = subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    r = subprocess.run(["systemctl", "--user", "enable", "--now", "nyrasview.service"], capture_output=True, text=True)
    return "enabled nyrasview.service (systemd user)" if r.returncode == 0 else f"wrote {unit}, but enabling failed: {r.stderr.strip()}"


def net_connections(pids=None):
    try:
        return psutil.net_connections("inet")
    except (psutil.AccessDenied, OSError):
        out = []
        for pid in pids or []:
            try:
                for c in psutil.Process(pid).net_connections("inet"):
                    out.append(_C(c, pid))
            except (psutil.Error, OSError):
                continue
        return out


class _C:
    __slots__ = ("fd", "family", "type", "laddr", "raddr", "status", "pid")

    def __init__(self, c, pid):
        for k in ("fd", "family", "type", "laddr", "raddr", "status"):
            setattr(self, k, getattr(c, k, None))
        self.pid = pid
