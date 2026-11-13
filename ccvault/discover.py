"""Discovery: details of this PC, Claude Code's data folders, and the folders where Claude Code or the
VS Code Claude extension (or a fork: Cursor, Windsurf, VSCodium) was used.

Used by `vault.py setup` / `vault.py start` to write a config without hand editing, and by the
dashboard's "This PC" page. Everything is read locally and only written to data/discovery.json;
nothing is sent anywhere. Reading WSL distributions is opt-in because it starts them.
"""

import collections
import getpass
import glob
import json
import os
import pathlib
import platform
import re
import shutil
import socket
import subprocess
import sys

from .common import (EDITORS, IS_MAC, IS_WINDOWS, DEFAULT_CONFIG, app_data_dir, default_claude_dir,
                     encode_path, enc_key, expand, nodejs_cache_dir, now_iso, tilde,
                     vpath, vscode_storage_dirs, write_json)
from .registry import _norm, _uri_to_source, _vscode_workspaces, resolve_local
from . import retention

EDITOR_CLI = {"VS Code": "code", "VS Code Insiders": "code-insiders", "Cursor": "cursor",
              "Windsurf": "windsurf", "VSCodium": "codium"}


def _run(args, timeout=10):
    try:
        res = subprocess.run(args, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout


def _text(raw):
    if raw is None:
        return None
    for enc in ("utf-8", "utf-16-le"):
        try:
            return raw.decode(enc).replace("\x00", "").strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1").strip()


# ---------------------------------------------------------------------------------------------
# this PC

def _memory_gb():
    try:
        if IS_WINDOWS:
            import ctypes

            class MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MemStatus()
            st.dwLength = ctypes.sizeof(MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return round(st.ullTotalPhys / 2**30, 1)
        if IS_MAC:
            return round(int(_text(_run(["sysctl", "-n", "hw.memsize"])) or 0) / 2**30, 1)
        with open("/proc/meminfo") as fh:
            kb = int(re.search(r"MemTotal:\s+(\d+)", fh.read()).group(1))
        return round(kb / 2**20, 1)
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def _os_name():
    if IS_MAC:
        return f"macOS {platform.mac_ver()[0]}"
    if IS_WINDOWS:
        build = sys.getwindowsversion().build
        return f"Windows {'11' if build >= 22000 else platform.release()} (build {build})"
    try:
        with open("/etc/os-release") as fh:
            m = re.search(r'^PRETTY_NAME="?([^"\n]+)', fh.read(), re.M)
            if m:
                return m.group(1)
    except OSError:
        pass
    return f"{platform.system()} {platform.release()}"


def claude_cli():
    path = shutil.which("claude")
    if not path:
        return None
    lines = (_text(_run([path, "--version"])) or "").splitlines()
    return {"path": path, "version": lines[0] if lines else None}


def editors():
    found = []
    for name, app, dot in EDITORS:
        ext_dir = expand(f"~/{dot}/extensions")
        storage = os.path.join(app_data_dir(app), "User", "workspaceStorage")
        if not os.path.isdir(ext_dir) and not os.path.isdir(storage):
            continue
        try:
            claude = sorted(n for n in os.listdir(ext_dir) if n.lower().startswith("anthropic.claude-code"))
        except OSError:
            claude = []
        found.append({"name": name, "extensions_dir": ext_dir, "claude_extension": claude[-1:] or None,
                      "storage": storage if os.path.isdir(storage) else None,
                      "workspaces": len(glob.glob(os.path.join(storage, "*"))),
                      "command": shutil.which(EDITOR_CLI[name])})
    return found


def wsl_distros():
    if not IS_WINDOWS or not shutil.which("wsl.exe"):
        return []
    out = _text(_run(["wsl.exe", "-l", "-q"], timeout=15)) or ""
    return [d.strip() for d in out.splitlines() if d.strip() and not d.strip().startswith("docker-desktop")]


def ssh_hosts():
    hosts = []
    try:
        with open(expand("~/.ssh/config"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r"\s*Host\s+(.+)", line, re.I)
                if m:
                    hosts += [h for h in m.group(1).split() if not any(c in h for c in "*?!")]
    except OSError:
        pass
    return hosts


def system_info():
    home = expand("~")
    disk = shutil.disk_usage(home)
    return {
        "collected": now_iso(), "os": _os_name(), "platform": platform.platform(), "machine": platform.machine(),
        "hostname": socket.gethostname(), "user": getpass.getuser(), "home": home,
        "python": {"version": platform.python_version(), "path": sys.executable},
        "cpu_count": os.cpu_count(), "memory_gb": _memory_gb(),
        "disk": {"path": home, "total": disk.total, "free": disk.free},
        "claude_cli": claude_cli(), "claude_config_dir_env": os.environ.get("CLAUDE_CONFIG_DIR"),
        "claude_log_cache": nodejs_cache_dir() if os.path.isdir(nodejs_cache_dir()) else None,
        "editors": editors(), "wsl_distros": wsl_distros(), "ssh_hosts": ssh_hosts(),
    }


# ---------------------------------------------------------------------------------------------
# Claude Code data folders

def describe_claude_dir(path):
    chats = glob.glob(os.path.join(path, "projects", "*", "*.jsonl"))
    projects = glob.glob(os.path.join(path, "projects", "*", ""))
    size = 0
    for dp, _d, fs in os.walk(os.path.join(path, "projects")):
        size += sum(os.path.getsize(os.path.join(dp, f)) for f in fs if os.path.isfile(os.path.join(dp, f)))
    settings = os.path.join(path, "settings.json")
    value = None
    if os.path.exists(settings):
        try:
            with open(settings, encoding="utf-8") as fh:
                value = json.load(fh).get(retention.KEY)
        except (OSError, ValueError, AttributeError):
            value = "unreadable"
    return {"path": path, "projects": len(projects), "chats": len(chats), "bytes": size,
            "settings_file": settings if os.path.exists(settings) else None, "cleanup_period_days": value}


def claude_dirs(include_wsl=False):
    found, seen = [], set()
    for path, kind in ((default_claude_dir(), "default"), (expand("~/.claude"), "home"),
                       (os.path.join(os.environ.get("XDG_CONFIG_HOME") or expand("~/.config"), "claude"), "xdg")):
        key = os.path.normcase(path)
        if key in seen or not os.path.isdir(path):
            continue
        seen.add(key)
        found.append(dict(describe_claude_dir(path), kind=kind))
    if include_wsl:
        for distro in wsl_distros():
            base = f"\\\\wsl.localhost\\{distro}\\home"
            try:
                users = os.listdir(base)
            except OSError:
                continue
            for user in users:
                path = os.path.join(base, user, ".claude")
                if os.path.isdir(path):
                    found.append(dict(describe_claude_dir(path), kind="wsl", distro=distro, user=user))
    return found


# ---------------------------------------------------------------------------------------------
# folders where Claude Code was used

def _first_cwd(project_dir):
    for f in sorted(glob.glob(os.path.join(project_dir, "*.jsonl")), key=os.path.getmtime, reverse=True):
        try:
            with open(f, "rb") as fh:
                for i, raw in enumerate(fh):
                    if i > 50:
                        break
                    try:
                        cwd = json.loads(raw).get("cwd")
                    except (ValueError, AttributeError):
                        continue
                    if cwd:
                        return cwd
        except OSError:
            continue
    return None


def usage_folders(cfg):
    """Folders on this PC (and remote hosts seen in VS Code) with Claude Code activity."""
    folders = {}

    def add(source, enc, kind, path=None, chats=0):
        key = (source, enc_key("local" if source == "local" else "ssh", enc))
        ent = folders.setdefault(key, {"source": source, "encoded": enc, "path": None, "evidence": set(), "chats": 0})
        ent["evidence"].add(kind)
        ent["chats"] += chats
        if path and not ent["path"]:
            ent["path"] = _norm(path)

    cdir = default_claude_dir()
    for pdir in glob.glob(os.path.join(cdir, "projects", "*", "")):
        pdir = pdir.rstrip("\\/")
        enc = os.path.basename(pdir)
        cwd = _first_cwd(pdir)
        if cwd and enc_key("local", encode_path(cwd)) != enc_key("local", enc):
            cwd = None
        add("local", enc, "project_dir", cwd, len(glob.glob(os.path.join(pdir, "*.jsonl"))))
    try:
        for e in os.scandir(nodejs_cache_dir()):
            if e.is_dir():
                add("local", e.name, "nodejs_cache")
    except OSError:
        pass
    remote_hosts = collections.Counter()
    for uri, kind, _tabs in _vscode_workspaces(cfg):
        source, path = _uri_to_source(cfg, uri)
        if source == "local":
            add("local", encode_path(path), kind, path)
        elif source and source.startswith("ssh:"):
            remote_hosts[source[4:]] += 1
            add(source[4:], encode_path(path), kind, path)
        elif source:
            remote_hosts[next((s["host"] for s in cfg["sources"] if s["name"] == source), source)] += 1
            add(source, encode_path(path), kind, path)

    todo = [e["encoded"] for e in folders.values() if e["source"] == "local" and not e["path"]]
    resolved = resolve_local(todo) if todo else {}
    out = []
    for ent in folders.values():
        if ent["source"] == "local" and not ent["path"]:
            cands = resolved.get(ent["encoded"].lower(), [])
            ent["path"] = cands[0] if cands else None
        ent["exists"] = bool(ent["path"]) and ent["source"] == "local" and os.path.isdir(ent["path"])
        ent["strong"] = bool(ent["evidence"] - {"vscode_views"})
        ent["evidence"] = sorted(ent["evidence"])
        out.append(ent)
    out.sort(key=lambda e: (e["source"] != "local", not e["strong"], (e["path"] or e["encoded"]).lower()))
    return out, dict(remote_hosts)


def suggest_roots(paths, limit=10):
    home = expand("~")
    counts = collections.Counter()
    for p in paths:
        try:
            rel = os.path.relpath(p, home)
        except ValueError:          # another drive
            rel = None
        if rel and not rel.startswith(".."):
            parts = rel.split(os.sep)
            if len(parts) < 2 or parts[0] == ".":
                continue
            root = os.path.join(home, *parts[:2]) if len(parts) > 2 else os.path.join(home, parts[0])
        else:                       # outside the home folder: keep the first two levels
            parts = pathlib.PurePath(p).parts
            root = os.path.join(*parts[:3]) if len(parts) > 3 else None
        if root:
            counts[root] += 1
    return [tilde(r) for r, _ in counts.most_common(limit)]


# ---------------------------------------------------------------------------------------------

def run_discovery(cfg, include_wsl=False, save=True):
    info = system_info()
    dirs = claude_dirs(include_wsl)
    folders, hosts = usage_folders(cfg)
    local_paths = [f["path"] for f in folders if f["source"] == "local" and f["path"] and f["strong"]]
    disc = {"generated": now_iso(), "system": info, "claude_dirs": dirs, "folders": folders,
            "remote_hosts": hosts, "suggested_roots": suggest_roots(local_paths),
            "vscode_storage": vscode_storage_dirs(cfg)}
    disc["suggested_sources"] = suggest_sources(cfg, disc)
    if save:
        write_json(vpath(cfg, "discovery.json"), disc)
    return disc


def suggest_sources(cfg, disc):
    have_dirs = {os.path.normcase(expand(s["claude_dir"])) for s in cfg["sources"]
                 if s["type"] == "local" and s.get("claude_dir") not in ("auto", None)}
    have_dirs.add(os.path.normcase(default_claude_dir()))
    have_hosts = {s.get("host", "").lower() for s in cfg["sources"] if s["type"] == "ssh"}
    names = {s["name"] for s in cfg["sources"]}
    out = []
    for d in disc["claude_dirs"]:
        if os.path.normcase(d["path"]) in have_dirs:
            continue
        name = f"wsl-{d['distro']}".lower() if d.get("kind") == "wsl" else f"local-{d['kind']}"
        out.append({"id": f"dir:{d['path']}", "why": f"Claude Code folder with {d['chats']} chats",
                    "source": {"name": _unique(name, names), "type": "local", "claude_dir": d["path"], "enabled": False}})
    for host, n in sorted(disc["remote_hosts"].items()):
        if host.lower() in have_hosts:
            continue
        out.append({"id": f"ssh:{host}", "why": f"used with the Claude extension over VS Code Remote-SSH ({n} folders)",
                    "source": {"name": _unique(re.sub(r"[^A-Za-z0-9_.-]", "-", host), names), "type": "ssh",
                               "host": host, "claude_dir": "~/.claude", "enabled": False}})
    return out


def _unique(name, names):
    base, i = name, 2
    while name in names:
        name, i = f"{base}-{i}", i + 1
    names.add(name)
    return name


def config_from_discovery(disc, existing=None):
    """A config for a first run, or the existing one with new sources added (disabled) and roots filled."""
    cfg = json.loads(json.dumps(existing)) if existing else json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in DEFAULT_CONFIG.items():
        cfg.setdefault(key, value)
    have = {s["name"] for s in cfg["sources"]}
    for sug in disc["suggested_sources"]:
        if sug["source"]["name"] not in have:
            cfg["sources"].append(dict(sug["source"]))
    local_name = next((s["name"] for s in cfg["sources"] if s["type"] == "local"), "local")
    if not cfg["roots"].get(local_name):
        cfg["roots"][local_name] = disc["suggested_roots"]
    return cfg
