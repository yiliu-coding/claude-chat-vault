"""Shared helpers: configuration, platform paths, atomic JSON files, time stamps, path encoding."""

import datetime
import json
import os
import re
import shutil
import sys
import time

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(TOOL_DIR, "config.json")
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

DEFAULT_CONFIG = {
    "vault_dir": "data",
    # "auto" = $CLAUDE_CONFIG_DIR, else ~/.claude; `vault.py setup` adds other sources it finds
    "sources": [{"name": "local", "type": "local", "claude_dir": "auto", "enabled": True}],
    "include": ["projects", "file-history", "settings.json", "settings.local.json", "CLAUDE.md",
                "agents", "commands", "plans", "todos"],
    "roots": {},
    "vscode_storage": "auto",
    "dashboard_port": 8765,
    "schedule": {"name": "Claude chat vault backup", "time": "12:30"},
}

# never copied, whatever the include list says
SECRET_NAMES = {".credentials.json"}

# VS Code and its forks: (display name, user-data folder name, extensions folder in home)
EDITORS = [("VS Code", "Code", ".vscode"), ("VS Code Insiders", "Code - Insiders", ".vscode-insiders"),
           ("Cursor", "Cursor", ".cursor"), ("Windsurf", "Windsurf", ".windsurf"),
           ("VSCodium", "VSCodium", ".vscode-oss")]


def expand(path):
    return os.path.normpath(os.path.expandvars(os.path.expanduser(path)))


# ---------------------------------------------------------------------------------------------
# platform locations

def default_claude_dir():
    return expand(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def claude_dir_of(source):
    path = source.get("claude_dir", "auto")
    return default_claude_dir() if path in ("auto", "", None) else expand(path)


def is_primary_local(source):
    return source["type"] == "local" and claude_dir_of(source) == default_claude_dir()


def app_data_dir(app):
    if IS_WINDOWS:
        return os.path.join(os.environ.get("APPDATA", expand("~/AppData/Roaming")), app)
    if IS_MAC:
        return expand(f"~/Library/Application Support/{app}")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or expand("~/.config"), app)


def nodejs_cache_dir():
    """Claude Code's per-project log cache (folder names are encoded working directories)."""
    if IS_WINDOWS:
        return os.path.join(os.environ.get("LOCALAPPDATA", expand("~/AppData/Local")), "claude-cli-nodejs", "Cache")
    if IS_MAC:
        return expand("~/Library/Caches/claude-cli-nodejs")
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or expand("~/.cache"), "claude-cli-nodejs")


def vscode_storage_dirs(cfg):
    value = cfg.get("vscode_storage", "auto")
    if value in ("auto", None):
        dirs = [os.path.join(app_data_dir(app), "User", "workspaceStorage") for _n, app, _e in EDITORS]
    else:
        dirs = [expand(p) for p in value]
    return [d for d in dirs if os.path.isdir(d)]


def managed_settings_files():
    """Organisation-managed Claude Code settings; values there override the user's settings."""
    if IS_WINDOWS:
        return [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ClaudeCode", "managed-settings.json")]
    if IS_MAC:
        return ["/Library/Application Support/ClaudeCode/managed-settings.json"]
    return ["/etc/claude-code/managed-settings.json"]


# ---------------------------------------------------------------------------------------------
# files and config

def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except ValueError:
        # a damaged state file must not stop a backup; keep it aside and start fresh
        bad = path + ".corrupt-" + ts_compact()
        shutil.copy2(path, bad)
        log(f"WARNING: {path} was not valid JSON, kept as {bad}")
        return default


def write_json(path, obj, indent=1, sort_keys=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=indent, ensure_ascii=False, sort_keys=sort_keys)
        fh.write("\n")
    os.replace(tmp, path)


def config_exists():
    return os.path.exists(CONFIG_PATH)


def load_config():
    if not config_exists():
        write_json(CONFIG_PATH, DEFAULT_CONFIG)
    cfg = read_json(CONFIG_PATH, {})
    for key, value in DEFAULT_CONFIG.items():
        cfg.setdefault(key, value)
    return cfg


def save_config(cfg):
    write_json(CONFIG_PATH, cfg)


def vault_dir(cfg):
    path = expand(cfg["vault_dir"])
    return path if os.path.isabs(path) else os.path.join(TOOL_DIR, path)


def vpath(cfg, *parts):
    return os.path.join(vault_dir(cfg), *parts)


def enabled_sources(cfg, only=None):
    return [s for s in cfg["sources"] if s.get("enabled", True) and (only is None or s["name"] == only)]


def find_source(cfg, name):
    return next((s for s in cfg["sources"] if s["name"] == name), None)


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ts_compact():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def encode_path(path):
    """Claude Code's project folder name for a working directory: every non-alphanumeric -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def enc_key(source_type, encoded):
    # Windows paths are case-insensitive and VS Code lower-cases the drive letter
    return encoded.lower() if source_type == "local" and IS_WINDOWS else encoded


def norm_local_path(path):
    path = os.path.normpath(path)
    if IS_WINDOWS and len(path) >= 2 and path[1] == ":":
        path = path[0].upper() + path[1:]
    return path


def tilde(path):
    """Show a path under the home folder as ~/..., for display and for portable config."""
    if not path:
        return path
    home = expand("~")
    a, b = (path.lower(), home.lower()) if IS_WINDOWS else (path, home)
    if a == b or a.startswith(b + os.sep):
        return "~" + path[len(home):].replace("\\", "/")
    return path


_log_file = None


def set_log_file(path):
    global _log_file
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _log_file = path


def log(msg, quiet=False):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    if not quiet and sys.stdout is not None:
        try:
            print(line, flush=True)
        except (OSError, ValueError):
            pass
    if _log_file:
        with open(_log_file, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def ssh_exe():
    """Prefer Windows OpenSSH: Git's ssh resolves ~ differently and misses ~/.ssh/known_hosts."""
    win = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", "ssh.exe")
    if IS_WINDOWS and os.path.exists(win):
        return win
    return shutil.which("ssh") or "ssh"


def ssh_args(source):
    return [ssh_exe(), "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
            "-o", "ServerAliveInterval=30", source["host"]]
