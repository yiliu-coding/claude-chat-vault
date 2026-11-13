"""Claude Code's chat retention: the `cleanupPeriodDays` setting.

Each time Claude Code starts it deletes chat transcripts that have not been written to for
`cleanupPeriodDays` days (30 when the setting is absent). The value lives in
<claude dir>/settings.json. An organisation can pin it in managed settings, which then wins and
cannot be changed here.

Changing the value: the old settings.json is first copied into the vault
(data/settings-backups/<source>/), every other setting is kept as it is, and a file that is not
valid JSON is never touched.
"""

import json
import os
import shutil
import time

from .common import (IS_WINDOWS, claude_dir_of, find_source, is_primary_local, managed_settings_files,
                     read_json, ts_compact, vpath)
from .mirror import load_manifest, run_ssh

DEFAULT_DAYS = 30
MIN_DAYS, MAX_DAYS = 1, 36500
PRESETS = [(30, "30 days (Claude Code default)"), (90, "3 months"), (180, "6 months"), (365, "1 year"),
           (730, "2 years"), (1825, "5 years"), (3650, "10 years"), (7300, "20 years")]
KEY = "cleanupPeriodDays"


def describe(days):
    for value, label in PRESETS:
        if value == days:
            return label.split(" (")[0]
    if days and days % 365 == 0:
        return f"{days // 365} years"
    return f"{days} days"


def validate_days(days):
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("the number of days must be a whole number")
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise ValueError(f"choose between {MIN_DAYS} and {MAX_DAYS} days "
                         "(Claude Code does not accept 0; it would stop saving chats)")
    return days


def _valid_value(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


# ---------------------------------------------------------------------------------------------
# managed (organisation) settings

def _registry_policy():
    if not IS_WINDOWS:
        return None, None
    import winreg
    for hive, label in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"), (winreg.HKEY_CURRENT_USER, "HKCU")):
        try:
            key = winreg.OpenKey(hive, r"SOFTWARE\Policies\ClaudeCode")
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name, data, _t = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if name == KEY and isinstance(data, int):
                    return data, rf"{label}\SOFTWARE\Policies\ClaudeCode\{name}"
                if isinstance(data, str) and KEY in data:
                    try:
                        value = json.loads(data).get(KEY)
                    except (ValueError, AttributeError):
                        continue
                    if _valid_value(value):
                        return value, rf"{label}\SOFTWARE\Policies\ClaudeCode\{name}"
    return None, None


def managed_value():
    for path in managed_settings_files():
        data = read_json(path, {}) if os.path.exists(path) else {}
        if isinstance(data, dict) and _valid_value(data.get(KEY)):
            return data[KEY], path
    return _registry_policy()


# ---------------------------------------------------------------------------------------------
# reading

def _transcripts(claude_dir):
    root, now, out = os.path.join(claude_dir, "projects"), time.time(), []
    try:
        projects = list(os.scandir(root))
    except OSError:
        return out
    for p in projects:
        if not p.is_dir():
            continue
        for f in os.scandir(p.path):
            if f.name.endswith(".jsonl") and f.is_file():
                out.append({"rel": f"projects/{p.name}/{f.name}",
                            "age_days": round((now - f.stat().st_mtime) / 86400, 1)})
    return out


def _mark_vaulted(cfg, name, chats):
    files = load_manifest(cfg, name)["files"]
    for c in chats:
        c["in_vault"] = c["rel"] in files
    return chats


def _summary(st):
    chats = st.pop("chats", [])
    st["n_chats"] = len(chats)
    st["oldest_days"] = max((c["age_days"] for c in chats), default=None)
    # ages are sent so the dashboard can preview any value without asking again
    st["chat_ages"] = sorted(([c["age_days"], c["in_vault"]] for c in chats), reverse=True)
    st["presets"] = PRESETS
    st["default"] = DEFAULT_DAYS
    st["effective_label"] = describe(st["effective"])
    return st


def status(cfg, source_name):
    source = find_source(cfg, source_name)
    if source is None:
        raise ValueError(f"unknown source {source_name!r}")
    if source["type"] == "ssh":
        return _remote(cfg, source, "get")
    cdir = claude_dir_of(source)
    path = os.path.join(cdir, "settings.json")
    st = {"source": source["name"], "type": "local", "claude_dir": cdir, "file": path,
          "exists": os.path.exists(path), "value": None, "valid": True, "error": None}
    if st["exists"]:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
            st["value"] = data.get(KEY)
        except (OSError, ValueError) as exc:
            st["valid"], st["error"] = False, str(exc)
    mval, mfile = managed_value() if is_primary_local(source) else (None, None)
    st.update(managed_value=mval, managed_file=mfile, locked=mval is not None)
    st["effective"] = mval or (st["value"] if _valid_value(st["value"]) else DEFAULT_DAYS)
    st["chats"] = _mark_vaulted(cfg, source["name"], _transcripts(cdir))
    return _summary(st)


def would_delete(st, days):
    """How many of the source's chats are older than `days` (deleted at Claude Code's next start)."""
    old = [a for a in st["chat_ages"] if a[0] > days]
    return {"days": days, "would_delete": len(old), "already_in_vault": sum(1 for a in old if a[1])}


# ---------------------------------------------------------------------------------------------
# changing

def set_days(cfg, source_name, days):
    days = validate_days(days)
    source = find_source(cfg, source_name)
    if source is None:
        raise ValueError(f"unknown source {source_name!r}")
    if source["type"] == "ssh":
        st = _remote(cfg, source, "set", days)
        return {"file": st["file"], "old": st.get("old"), "new": days, "backup": st.get("backup")}
    st = status(cfg, source_name)
    if st["locked"]:
        raise ValueError(f"{KEY} is set by your organisation in {st['managed_file']} and cannot be changed here")
    if not st["valid"]:
        raise ValueError(f"{st['file']} is not valid JSON ({st['error']}); fix it by hand first - not touching it")
    path, data, backup = st["file"], {}, None
    if st["exists"]:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        backup = vpath(cfg, "settings-backups", source["name"], f"settings.json.{ts_compact()}")
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        shutil.copy2(path, backup)
    old = data.get(KEY)
    data[KEY] = days
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".ccvault-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return {"file": path, "old": old, "new": days, "backup": backup}


# ---------------------------------------------------------------------------------------------
# remote (ssh) sources: the same read / change, done by a small python3 script on the server

REMOTE = r"""
import glob, json, os, shutil, time
d = os.path.expanduser(ARGS["dir"])
f = os.path.join(d, "settings.json")
out = {"file": f, "claude_dir": d, "exists": os.path.exists(f), "value": None, "valid": True, "error": None}
data = {}
if out["exists"]:
    try:
        data = json.load(open(f))
        assert isinstance(data, dict), "top level is not an object"
        out["value"] = data.get("cleanupPeriodDays")
    except Exception as exc:
        out["valid"], out["error"] = False, str(exc)
mv, mf = None, None
m = "/etc/claude-code/managed-settings.json"
if os.path.exists(m):
    try:
        mv = json.load(open(m)).get("cleanupPeriodDays")
        mf = m if mv else None
    except Exception:
        pass
out.update(managed_value=mv, managed_file=mf, locked=mv is not None)
now = time.time()
out["chats"] = [{"rel": os.path.relpath(p, d), "age_days": round((now - os.path.getmtime(p)) / 86400, 1)}
                for p in glob.glob(os.path.join(d, "projects", "*", "*.jsonl"))]
if ARGS["mode"] == "set":
    if out["locked"] or not out["valid"]:
        out["refused"] = "managed by policy" if out["locked"] else "settings.json is not valid JSON"
    else:
        if out["exists"]:
            out["backup"] = f + ".bak-" + time.strftime("%Y%m%dT%H%M%S")
            shutil.copy2(f, out["backup"])
        out["old"] = data.get("cleanupPeriodDays")
        data["cleanupPeriodDays"] = ARGS["days"]
        os.makedirs(d, exist_ok=True)
        with open(f + ".ccvault-tmp", "w") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(f + ".ccvault-tmp", f)
        out["value"] = ARGS["days"]
print(json.dumps(out))
"""


def _remote(cfg, source, mode, days=None):
    args = {"mode": mode, "days": days, "dir": source.get("claude_dir", "~/.claude")}
    out = run_ssh(source, "python3 -", stdin_text=f"ARGS = {args!r}\n" + REMOTE, timeout=120)
    try:
        st = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ValueError(f"could not read the settings on {source['host']} (is python3 installed there?)")
    if st.get("refused"):
        raise ValueError(f"not changed on {source['host']}: {st['refused']}")
    st.update(source=source["name"], type="ssh", host=source["host"])
    st["effective"] = st["managed_value"] or (st["value"] if _valid_value(st["value"]) else DEFAULT_DAYS)
    _mark_vaulted(cfg, source["name"], st["chats"])
    return _summary(st)

