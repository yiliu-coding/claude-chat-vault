"""Local dashboard: python vault.py serve  ->  http://127.0.0.1:<port>/#t=<token>

Binds to 127.0.0.1 only. Every /api call must carry the per-launch token in a request header (which
other web sites cannot set without the browser's permission) and a local Host header, so pages open
in the same browser can neither read the vault nor trigger actions. Actions only take keys that are
looked up in the vault's own catalog, register, config or discovery results; no path or command
comes from the browser.
"""

import collections
import http.server
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

from . import discover, retention, schedule
from .backup import BackupBusy, run_backup
from .common import (IS_MAC, IS_WINDOWS, TOOL_DIR, claude_dir_of, enc_key, find_source, is_primary_local,
                     now_iso, read_json, save_config, tilde, vault_dir, vpath, write_json)
from .mirror import mirror_root
from .registry import merge_discovery
from .restore import restore_session
from .sessions import live_view, render_entries, to_markdown

STATIC_DIR = os.path.join(TOOL_DIR, "dashboard")
STATIC = {"/": "index.html", "/index.html": "index.html", "/static/app.js": "app.js",
          "/static/style.css": "style.css"}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
EDITOR_COMMANDS = ["code", "code-insiders", "cursor", "windsurf", "codium"]


def _no_window():
    return subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0


class Dashboard:
    def __init__(self, cfg, port):
        self.cfg, self.port = cfg, port
        self.token = secrets.token_urlsafe(24)
        self.backup = {"running": False}
        self.lock = threading.Lock()
        self.render_cache = collections.OrderedDict()
        self.live_cache = {}

    # ---- data -------------------------------------------------------------------------------

    def annotations(self):
        return read_json(vpath(self.cfg, "annotations.json"), {"session": {}, "directory": {}})

    def runs(self):
        out, path = [], vpath(self.cfg, "logs", "runs.jsonl")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh.read().splitlines()[-30:]:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        return out

    def data(self):
        cfg = self.cfg
        sessions = read_json(vpath(cfg, "catalog", "sessions.json"), {"sessions": []})["sessions"]
        if len(self.live_cache) > 2000:
            self.live_cache.clear()
        live_view(cfg, sessions, self.live_cache)
        reg = read_json(vpath(cfg, "registry", "directories.json"), {"directories": {}})["directories"]
        stats = collections.defaultdict(lambda: {"n": 0, "gone": 0, "size": 0, "last": ""})
        for s in sessions:
            s["key"] = f"{s['source']}/{s['session_id']}"
            s["dir_key"] = f"{s['source']}|{enc_key(s['source_type'], s['project'])}"
            d = reg.get(s["dir_key"], {})
            s["dir_path"] = d.get("path") or s.get("cwd") or s["project"]
            st = stats[s["dir_key"]]
            st["n"] += 1
            st["gone"] += 0 if s["in_source"] else 1
            st["size"] += s["size"] + s.get("sidecar_bytes", 0)
            st["last"] = max(st["last"], s.get("last_activity") or "")
        dirs = []
        for key, d in reg.items():
            st = stats.get(key, {"n": 0, "gone": 0, "size": 0, "last": ""})
            last_seen = max((e.get("last_seen", "") for e in d["evidence"].values()), default="")
            dirs.append(dict(d, key=key, n_chats=st["n"], n_gone=st["gone"], size=st["size"],
                             last_activity=st["last"] or None, evidence_last=last_seen))
        root = vault_dir(cfg)
        os.makedirs(root, exist_ok=True)
        size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _d, fs in os.walk(root) for f in fs)
        disk = shutil.disk_usage(root)
        disc = read_json(vpath(cfg, "discovery.json"), {})
        return {"generated": now_iso(), "sessions": sessions, "directories": dirs,
                "annotations": self.annotations(), "runs": self.runs(), "backup": self.backup,
                "vault": {"path": root, "bytes": size, "disk_free": disk.free, "disk_total": disk.total},
                "sources": self.sources_view(), "found_chats": sum(d.get("chats", 0) for d in disc.get("claude_dirs", [])),
                "found_bytes": sum(d.get("bytes", 0) for d in disc.get("claude_dirs", [])),
                "found_folders": sum(1 for f in disc.get("folders", []) if f.get("strong"))}

    def sources_view(self):
        last = (self.runs() or [{}])[-1].get("sources", {})
        out = []
        for s in self.cfg["sources"]:
            where = s["host"] + ":" + s.get("claude_dir", "~/.claude") if s["type"] == "ssh" else tilde(claude_dir_of(s))
            out.append({"name": s["name"], "type": s["type"], "enabled": s.get("enabled", True), "host": s.get("host"),
                        "where": where, "primary": is_primary_local(s), "last_run": last.get(s["name"])})
        return out

    def settings(self):
        out = {"config": {"vault_dir": vault_dir(self.cfg), "roots": self.cfg.get("roots", {}),
                          "include": self.cfg.get("include", [])},
               "sources": self.sources_view(), "retention": {}, "discovery": read_json(vpath(self.cfg, "discovery.json"), None),
               "presets": retention.PRESETS, "default_days": retention.DEFAULT_DAYS,
               "limits": [retention.MIN_DAYS, retention.MAX_DAYS]}
        for s in self.cfg["sources"]:
            if s["type"] == "local" and s.get("enabled", True):
                try:
                    out["retention"][s["name"]] = retention.status(self.cfg, s["name"])
                except (OSError, ValueError) as exc:
                    out["retention"][s["name"]] = {"error": str(exc)}
        try:
            out["schedule"] = schedule.status(self.cfg)
        except Exception as exc:  # the scheduler is optional; show why it is unavailable
            out["schedule"] = {"installed": False, "error": str(exc)}
        out["schedule"]["default_time"] = (self.cfg.get("schedule") or {}).get("time", "12:30")
        return out

    def session(self, source, sid):
        for s in read_json(vpath(self.cfg, "catalog", "sessions.json"), {"sessions": []})["sessions"]:
            if s["source"] == source and s["session_id"] == sid:
                return s
        return None

    def entries(self, s):
        path = os.path.join(mirror_root(self.cfg, s["source"]), *s["rel"].split("/"))
        stamp = (path, os.path.getmtime(path))
        if stamp not in self.render_cache:
            self.render_cache[stamp] = render_entries(path)
            while len(self.render_cache) > 4:
                self.render_cache.popitem(last=False)
        self.render_cache.move_to_end(stamp)
        return self.render_cache[stamp]

    # ---- actions ------------------------------------------------------------------------------

    def start_backup(self):
        with self.lock:
            if self.backup.get("running"):
                return self.backup
            self.backup = {"running": True, "started": now_iso()}

        def work():
            try:
                run = run_backup(self.cfg, quiet=True)
                self.backup = {"running": False, "finished": now_iso(), "result": run}
            except BackupBusy as exc:
                self.backup = {"running": False, "finished": now_iso(), "error": str(exc)}
            except Exception as exc:  # reported to the page, logged by run_backup
                self.backup = {"running": False, "finished": now_iso(), "error": repr(exc)}

        threading.Thread(target=work, daemon=True).start()
        return self.backup

    def open_dir(self, key):
        reg = read_json(vpath(self.cfg, "registry", "directories.json"), {"directories": {}})["directories"]
        d = reg.get(key)
        if not d or not d.get("path"):
            raise ValueError("unknown directory, or its real path is not known")
        code = next((p for p in map(shutil.which, EDITOR_COMMANDS) if p), None)
        if not code:
            raise ValueError("no VS Code command (`code`) on PATH; in VS Code run 'Shell Command: Install code command'")
        source = find_source(self.cfg, d["source"])
        if d["source_type"] == "local" and (source is None or is_primary_local(source)):
            args = [code, d["path"]]
        elif source and source["type"] == "ssh":
            args = [code, "--remote", f"ssh-remote+{source['host']}", d["path"]]
        else:
            raise ValueError(f"opening folders of source {d['source']} is not supported")
        subprocess.Popen(args, creationflags=_no_window(), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"opened": d["path"]}

    def open_vault(self):
        path = vault_dir(self.cfg)
        if IS_WINDOWS:
            os.startfile(path)  # noqa: S606 - our own folder
        else:
            subprocess.Popen(["open" if IS_MAC else "xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"opened": path}

    def annotate(self, body):
        scope = body.get("scope")
        if scope not in ("session", "directory") or not isinstance(body.get("key"), str):
            raise ValueError("bad annotation")
        ann = self.annotations()
        ent = ann.setdefault(scope, {}).setdefault(body["key"], {})
        if "note" in body:
            ent["note"] = str(body["note"])[:20000]
        if "tags" in body:
            ent["tags"] = [str(t).strip()[:40] for t in body["tags"] if str(t).strip()][:20]
        if "star" in body:
            ent["star"] = bool(body["star"])
        ent["updated"] = now_iso()
        write_json(vpath(self.cfg, "annotations.json"), ann)
        return ent

    def set_retention(self, body):
        res = retention.set_days(self.cfg, body.get("source"), body.get("days"))
        res["status"] = retention.status(self.cfg, body.get("source"))
        return res

    def set_schedule(self, body):
        if body.get("action") == "install":
            st = schedule.install(self.cfg, body.get("time") or "12:30")
            save_config(self.cfg)
            return st
        if body.get("action") == "remove":
            return schedule.remove(self.cfg)
        raise ValueError("action must be install or remove")

    def discover(self, body):
        disc = discover.run_discovery(self.cfg, include_wsl=bool(body.get("wsl")))
        merge_discovery(self.cfg, disc)        # found folders show in the Chats tab right away
        return {"folders": sum(1 for f in disc["folders"] if f["strong"]),
                "chats": sum(d.get("chats", 0) for d in disc["claude_dirs"]),
                "suggested": len(disc["suggested_sources"])}

    def change_sources(self, body):
        if body.get("add"):
            disc = read_json(vpath(self.cfg, "discovery.json"), {})
            sug = next((x for x in disc.get("suggested_sources", []) if x["id"] == body["add"]), None)
            if sug is None:
                raise ValueError("that suggestion is no longer available - scan this PC again")
            if find_source(self.cfg, sug["source"]["name"]) is None:
                self.cfg["sources"].append(dict(sug["source"], enabled=True))
        elif body.get("name"):
            s = find_source(self.cfg, body["name"])
            if s is None:
                raise ValueError("unknown source")
            if is_primary_local(s) and not body.get("enabled"):
                raise ValueError("this PC's own Claude folder cannot be switched off here")
            s["enabled"] = bool(body.get("enabled"))
        else:
            raise ValueError("nothing to change")
        save_config(self.cfg)
        return {"sources": self.sources_view()}


ROUTES_GET = {"/api/data": lambda app, q: app.data(),
              "/api/backup": lambda app, q: app.backup,
              "/api/settings": lambda app, q: app.settings(),
              "/api/retention": lambda app, q: retention.status(app.cfg, q.get("source"))}
ROUTES_POST = {"/api/backup": lambda app, b: app.start_backup(),
               "/api/open": lambda app, b: app.open_dir(b.get("key")),
               "/api/open-vault": lambda app, b: app.open_vault(),
               "/api/annotate": lambda app, b: app.annotate(b),
               "/api/retention": lambda app, b: app.set_retention(b),
               "/api/schedule": lambda app, b: app.set_schedule(b),
               "/api/discover": lambda app, b: app.discover(b),
               "/api/sources": lambda app, b: app.change_sources(b)}


def make_handler(app):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "ccvault"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, api):
            host = self.headers.get("Host", "")
            if host not in (f"127.0.0.1:{app.port}", f"localhost:{app.port}"):
                self._send(403, {"error": "bad host"})
                return False
            if api and not secrets.compare_digest(self.headers.get("X-Vault-Token", ""), app.token):
                self._send(403, {"error": "this page's key does not match this dashboard - close old dashboard windows, then use the link that start.cmd / start.sh / dashboard.cmd opens"})
                return False
            return True

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path in STATIC:
                if self._guard(api=False):
                    name = STATIC[url.path]
                    with open(os.path.join(STATIC_DIR, name), "rb") as fh:
                        self._send(200, fh.read(), TYPES[os.path.splitext(name)[1]])
                return
            if not url.path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if not self._guard(api=True):
                return
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            try:
                if url.path in ROUTES_GET:
                    return self._send(200, ROUTES_GET[url.path](app, q))
                if url.path in ("/api/session", "/api/export"):
                    s = app.session(q.get("source"), q.get("id"))
                    if s is None:
                        return self._send(404, {"error": "chat not in the vault"})
                    entries = app.entries(s)
                    if url.path == "/api/export":
                        return self._send(200, to_markdown(s, entries).encode("utf-8"), "text/markdown; charset=utf-8",
                                          {"Content-Disposition": f'attachment; filename="{s["session_id"]}.md"'})
                    offset, limit = int(q.get("offset", 0)), min(int(q.get("limit", 150)), 1000)
                    return self._send(200, {"total": len(entries), "offset": offset,
                                            "entries": entries[offset:offset + limit]})
                self._send(404, {"error": "not found"})
            except (ValueError, OSError, RuntimeError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # never drop the connection: tell the page what went wrong
                self._send(500, {"error": f"internal error: {exc!r}"})

        def do_POST(self):
            if not self._guard(api=True):
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 1_000_000)) or b"{}")
                path = urllib.parse.urlparse(self.path).path
                if path == "/api/restore":
                    res = restore_session(app.cfg, body["id"], source_name=body.get("source"))
                    return self._send(200, {"restored": len(res["restored"]), "skipped": len(res["skipped"]),
                                            "touched": res["touched"]})
                if path in ROUTES_POST:
                    return self._send(200, ROUTES_POST[path](app, body))
                self._send(404, {"error": "not found"})
            except (ValueError, KeyError, OSError, RuntimeError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # never drop the connection: tell the page what went wrong
                self._send(500, {"error": f"internal error: {exc!r}"})

    return Handler


class ExclusiveServer(http.server.ThreadingHTTPServer):
    """Refuses a port another program already listens on, so a second dashboard moves to the next
    port. (On Windows, SO_REUSEADDR would let two servers share one port and answer at random.)"""
    allow_reuse_address = not IS_WINDOWS
    daemon_threads = True

    def server_bind(self):
        if IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(cfg, port=8765, open_browser=True):
    httpd = None
    for p in range(port, port + 20):
        try:
            app = Dashboard(cfg, p)
            httpd = ExclusiveServer(("127.0.0.1", p), make_handler(app))
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit(f"no free port between {port} and {port + 19}")
    url = f"http://127.0.0.1:{app.port}/#t={app.token}"
    print(f"Claude chat vault dashboard: {url}\n(keep this window open; Ctrl+C or closing it stops the dashboard)",
          flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        sys.stdout.flush()
