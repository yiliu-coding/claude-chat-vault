"""Append-only register of every directory where Claude Code was used.

Evidence is collected from several independent places, so a directory stays known even if
~/.claude is cleaned, edited or replaced:

  transcript      cwd recorded inside a backed-up transcript (exact path)
  project_dir     ~/.claude/projects/<encoded dir> in the vault mirror or the live source
  nodejs_cache    Claude Code's log cache (common.nodejs_cache_dir()/<encoded dir>)
  running         ~/.claude/sessions/*.json written by running Claude Code processes
  vscode_tabs     VS Code workspace state listing Claude chat tabs (with their session ids)
  vscode_state    VS Code workspace state of the Claude extension, no tabs recorded
  vscode_views    only Claude view layout in a VS Code workspace (extension present, weak evidence)

Entries are never removed. Encoded names (non-alphanumerics replaced by '-') are turned back into
real paths by walking the file system along matching prefixes.
"""

import collections
import glob
import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request

from .common import (IS_WINDOWS, claude_dir_of, encode_path, enc_key, expand, is_primary_local, log,
                     nodejs_cache_dir, norm_local_path, now_iso, read_json, vpath, vscode_storage_dirs,
                     write_json)
from .mirror import SourceUnavailable, mirror_root, run_ssh

STRONG = {"transcript", "project_dir", "nodejs_cache", "running", "vscode_tabs", "vscode_state"}
PATH_RANK = {"transcript": 5, "running": 4, "vscode_tabs": 3, "vscode_state": 3, "vscode_views": 2,
             "resolved": 1}


def registry_path(cfg):
    return vpath(cfg, "registry", "directories.json")


def load_registry(cfg):
    return read_json(registry_path(cfg), {"directories": {}})


_is_primary_local = is_primary_local


def _norm(path):
    # only Windows drive paths are normalised; WSL and server paths stay POSIX
    return norm_local_path(path) if re.match(r"^[A-Za-z]:[\\/]", path or "") else path


# ---------------------------------------------------------------------------------------------
# evidence

def _vscode_workspaces(cfg):
    """(uri, kind, session ids) for VS Code workspaces that carry Claude extension state."""
    found = []
    for root in vscode_storage_dirs(cfg):
        for ws in glob.glob(os.path.join(root, "*")):
            try:
                with open(os.path.join(ws, "workspace.json"), encoding="utf-8") as fh:
                    uri = json.load(fh).get("folder")
            except (OSError, ValueError):
                continue
            db = os.path.join(ws, "state.vscdb")
            if not uri or not os.path.exists(db):
                continue
            try:
                con = sqlite3.connect(f"file:{urllib.request.pathname2url(db)}?mode=ro", uri=True)
                rows = dict(con.execute("SELECT key, value FROM ItemTable WHERE key LIKE '%laude%'"))
                con.close()
            except sqlite3.Error:
                continue
            main = rows.get("Anthropic.claude-code")
            if main:
                try:
                    tabs = [t.get("sessionId") for t in json.loads(main).get("panelTabSessions", [])
                            if t.get("sessionId")]
                except (ValueError, AttributeError):
                    tabs = []
                found.append((uri, "vscode_tabs" if tabs else "vscode_state", tabs))
            elif any("claude" in k.lower() for k in rows):
                found.append((uri, "vscode_views", []))
    return found


def _uri_to_source(cfg, uri):
    p = urllib.parse.urlparse(uri)
    if p.scheme == "file":
        return "local", norm_local_path(urllib.request.url2pathname(urllib.parse.unquote(p.path)))
    if p.scheme == "vscode-remote":
        kind, _, target = urllib.parse.unquote(p.netloc).partition("+")
        path = urllib.parse.unquote(p.path)
        for s in cfg["sources"]:
            if kind == "ssh-remote" and s["type"] == "ssh" and s["host"].lower() == target.lower():
                return s["name"], path
            if kind == "wsl" and s["type"] == "local" and target.lower() in s.get("claude_dir", "").lower():
                return s["name"], path
        if kind == "ssh-remote":
            return f"ssh:{target}", path
    return None, None


def gather_evidence(cfg, catalog, extras):
    ev = []
    types = {s["name"]: s["type"] for s in cfg["sources"]}

    for s in catalog["sessions"]:
        path = s.get("cwd")
        if path and enc_key(s["source_type"], encode_path(path)) != enc_key(s["source_type"], s["project"]):
            path = None                       # the session moved away from its launch directory
        ev.append(dict(source=s["source"], enc=s["project"], kind="transcript", path=_norm(path),
                       session=s["session_id"]))

    for source in cfg["sources"]:
        name = source["name"]
        for enc in _listdirs(os.path.join(mirror_root(cfg, name), "projects")):
            ev.append(dict(source=name, enc=enc, kind="project_dir"))
        if not source.get("enabled", True):
            continue
        if source["type"] == "local":
            cdir = claude_dir_of(source)
            for enc in _listdirs(os.path.join(cdir, "projects")):
                ev.append(dict(source=name, enc=enc, kind="project_dir"))
            for f in glob.glob(os.path.join(cdir, "sessions", "*.json")):
                rec = read_json(f, {})
                if rec.get("cwd"):
                    ev.append(dict(source=name, enc=encode_path(rec["cwd"]), kind="running",
                                   path=_norm(rec["cwd"]), session=rec.get("sessionId")))
            if _is_primary_local(source):
                for enc in _listdirs(nodejs_cache_dir()):
                    ev.append(dict(source=name, enc=enc, kind="nodejs_cache"))
        extra = extras.get(name)
        if extra:
            for enc in extra["project_dirs"]:
                ev.append(dict(source=name, enc=enc, kind="project_dir"))
            for enc in extra["cache_dirs"]:
                ev.append(dict(source=name, enc=enc, kind="nodejs_cache"))
            for raw in extra["sessions"]:
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if rec.get("cwd"):
                    ev.append(dict(source=name, enc=encode_path(rec["cwd"]), kind="running",
                                   path=rec["cwd"], session=rec.get("sessionId")))

    for uri, kind, tabs in _vscode_workspaces(cfg):
        source, path = _uri_to_source(cfg, uri)
        if not source:
            continue
        enc = encode_path(path)
        if not tabs:
            ev.append(dict(source=source, enc=enc, kind=kind, path=path))
        for sid in tabs:
            ev.append(dict(source=source, enc=enc, kind=kind, path=path, session=sid))
        types.setdefault(source, "ssh")
    return ev, types


def _listdirs(path):
    try:
        return [e.name for e in os.scandir(path) if e.is_dir()]
    except OSError:
        return []


# ---------------------------------------------------------------------------------------------
# turning encoded names back into paths

def resolve_local(encs):
    """{lower-cased encoded name: [existing directories that encode to it]} for Windows/POSIX."""
    want = {e.lower() for e in encs}
    found = collections.defaultdict(list)
    if IS_WINDOWS:
        starts = sorted({e[0].upper() + ":\\" for e in want if re.match(r"^[a-z]--", e)})
    else:
        starts = ["/"] if any(e.startswith("-") for e in want) else []
    for start in starts:
        stack = [start]
        while stack:
            try:
                entries = list(os.scandir(stack.pop()))
            except OSError:
                continue
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                key = encode_path(entry.path).lower()
                if key in want:
                    found[key].append(norm_local_path(entry.path))
                if any(w.startswith(key + "-") for w in want):
                    stack.append(entry.path)
    return found


REMOTE_RESOLVE = r"""
import json, os, re
enc = lambda p: re.sub(r"[^A-Za-z0-9]", "-", p)
found, stack = {}, ["/"]
while stack and WANT:
    try:
        entries = list(os.scandir(stack.pop()))
    except OSError:
        continue
    for e in entries:
        try:
            if not e.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        k = enc(e.path)
        if k in WANT:
            found.setdefault(k, []).append(e.path)
        if any(w.startswith(k + "-") for w in WANT):
            stack.append(e.path)
print(json.dumps({"found": found, "exists": [p for p in CHECK if os.path.isdir(p)]}))
"""


def resolve_remote(source, encs, check_paths):
    script = f"WANT = set({json.dumps(sorted(encs))})\nCHECK = {json.dumps(sorted(check_paths))}\n" \
        + REMOTE_RESOLVE
    out = run_ssh(source, "python3 -", stdin_text=script, timeout=300)
    data = json.loads(out.strip().splitlines()[-1])
    return data["found"], set(data["exists"])


# ---------------------------------------------------------------------------------------------

def _root_of(cfg, source_name, source_type, path):
    for root in cfg.get("roots", {}).get(source_name, []):
        r = expand(root) if source_type == "local" else root.rstrip("/")
        a, b = (path.lower(), r.lower()) if source_type == "local" and IS_WINDOWS else (path, r)
        sep = "\\" if source_type == "local" and IS_WINDOWS else "/"
        if a == b or a.startswith(b.rstrip(sep) + sep):
            return root
    return None


def update_registry(cfg, catalog, extras, reachable):
    """Merge this run's evidence into the register; `reachable` = source names contacted this run."""
    reg = load_registry(cfg)
    dirs = reg["directories"]
    now = now_iso()
    evidence, types = gather_evidence(cfg, catalog, extras)
    for e in evidence:
        stype = types.get(e["source"], "ssh")
        key = f"{e['source']}|{enc_key(stype, e['enc'])}"
        ent = dirs.setdefault(key, {"source": e["source"], "source_type": stype, "encoded": e["enc"],
                                    "first_seen": now, "evidence": {}, "session_ids": [],
                                    "path": None, "path_origin": None, "path_candidates": []})
        ent["last_seen"] = now
        kind = ent["evidence"].setdefault(e["kind"], {"first_seen": now})
        kind["last_seen"] = now
        if e.get("session") and e["session"] not in ent["session_ids"]:
            ent["session_ids"].append(e["session"])
        if e.get("path") and PATH_RANK[e["kind"]] > PATH_RANK.get(ent.get("path_origin"), 0):
            ent["path"], ent["path_origin"] = e["path"], e["kind"]

    # resolve encoded names that have no real path yet
    by_source = collections.defaultdict(list)
    for key, ent in dirs.items():
        by_source[ent["source"]].append(ent)
    for name, ents in by_source.items():
        source = next((s for s in cfg["sources"] if s["name"] == name), None)
        todo = [e for e in ents if not e.get("path")]
        try:
            if source and source["type"] == "local" and _is_primary_local(source):
                found = resolve_local([e["encoded"] for e in todo]) if todo else {}
                for e in todo:
                    _apply_resolution(e, found.get(e["encoded"].lower(), []))
                for e in ents:
                    e["exists"] = bool(e.get("path")) and os.path.isdir(e["path"])
            elif source and source["type"] == "ssh" and name in reachable:
                found, exists = resolve_remote(source, [e["encoded"] for e in todo],
                                               [e["path"] for e in ents if e.get("path")])
                for e in todo:
                    _apply_resolution(e, found.get(e["encoded"], []))
                for e in ents:
                    e["exists"] = bool(e.get("path")) and (e["path"] in exists or
                                                           e.get("path_origin") == "resolved")
        except (SourceUnavailable, ValueError, IndexError) as exc:
            log(f"  could not resolve directories on {name}: {exc}")
        for e in ents:
            e["root"] = _root_of(cfg, name, e.get("source_type", "ssh"), e["path"]) if e.get("path") else None
            e["strong"] = any(k in STRONG for k in e["evidence"])

    reg["updated"] = now
    write_json(registry_path(cfg), reg)
    return reg


def merge_discovery(cfg, disc):
    """Add the folders found by a scan (discover.run_discovery) to the register, before any backup,
    so they show in the dashboard straight away. Their chats appear after the next backup."""
    reg = load_registry(cfg)
    dirs = reg["directories"]
    now = now_iso()
    by_host = {s.get("host", "").lower(): s["name"] for s in cfg["sources"] if s["type"] == "ssh"}
    types = {s["name"]: s["type"] for s in cfg["sources"]}
    for f in disc.get("folders", []):
        name = f["source"] if f["source"] in types else by_host.get(f["source"].lower(), f["source"])
        stype = types.get(name, "local" if name == "local" else "ssh")
        key = f"{name}|{enc_key(stype, f['encoded'])}"
        ent = dirs.setdefault(key, {"source": name, "source_type": stype, "encoded": f["encoded"],
                                    "first_seen": now, "evidence": {}, "session_ids": [],
                                    "path": None, "path_origin": None, "path_candidates": []})
        ent["last_seen"] = now
        ent["found_chats"] = f.get("chats", 0)     # chats seen in ~/.claude by this scan
        for kind in f["evidence"]:
            ent["evidence"].setdefault(kind, {"first_seen": now})["last_seen"] = now
        if f.get("path") and PATH_RANK["resolved"] > PATH_RANK.get(ent.get("path_origin"), 0):
            ent["path"], ent["path_origin"] = f["path"], "resolved"
        if stype == "local":
            ent["exists"] = bool(ent.get("path")) and os.path.isdir(ent["path"])
        ent["root"] = _root_of(cfg, name, stype, ent["path"]) if ent.get("path") else None
        ent["strong"] = any(k in STRONG for k in ent["evidence"])
    reg["updated"] = now
    write_json(registry_path(cfg), reg)
    return reg


def _apply_resolution(ent, candidates):
    ent["path_candidates"] = candidates
    if candidates:
        ent["path"], ent["path_origin"] = candidates[0], "resolved"
