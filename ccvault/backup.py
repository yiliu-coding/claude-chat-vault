"""One backup run: mirror every enabled source, re-index the chats, update the register, write reports."""

import json
import os
import time
import traceback

from .common import enabled_sources, log, now_iso, set_log_file, vpath
from .mirror import SourceUnavailable, mirror_source
from .registry import update_registry
from .reports import write_reports
from .sessions import build_catalog

STALE_LOCK_S = 3 * 3600


class BackupBusy(Exception):
    pass


def _acquire(lock):
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {now_iso()}".encode())
            os.close(fd)
            return
        except FileExistsError:
            if time.time() - os.path.getmtime(lock) > STALE_LOCK_S:
                os.remove(lock)
                continue
            raise BackupBusy("another backup is running (data/state/backup.lock)")


def run_backup(cfg, only=None, quiet=False):
    set_log_file(vpath(cfg, "logs", "backup.log"))
    lock = vpath(cfg, "state", "backup.lock")
    _acquire(lock)
    run = {"started": now_iso(), "sources": {}}
    try:
        log("backup started", quiet)
        extras, reachable = {}, set()
        for source in enabled_sources(cfg, only):
            name = source["name"]
            try:
                stats, extra = mirror_source(cfg, source)
                reachable.add(name)
                if extra:
                    extras[name] = extra
                run["sources"][name] = {"ok": True, **stats}
                log(f"  {name}: {stats.get('new', 0)} new, {stats.get('updated', 0)} grown, "
                    f"{stats.get('versioned', 0)} rewritten (old kept), {stats.get('unchanged', 0)} unchanged, "
                    f"{stats.get('went_missing', 0)} newly missing from source "
                    f"({stats.get('missing_total', 0)} missing in total)", quiet)
            except SourceUnavailable as exc:
                run["sources"][name] = {"ok": False, "error": str(exc)}
                log(f"  {name}: skipped - {exc}", quiet)
            except Exception as exc:  # one broken source must not stop the others
                run["sources"][name] = {"ok": False, "error": repr(exc)}
                log(f"  {name}: FAILED - {exc}\n{traceback.format_exc()}", quiet)
        catalog = build_catalog(cfg)
        registry = update_registry(cfg, catalog, extras, reachable)
        write_reports(cfg, catalog, registry)
        n_missing = sum(1 for s in catalog["sessions"] if not s["in_source"])
        run.update(finished=now_iso(), sessions=len(catalog["sessions"]), vault_only=n_missing,
                   directories=sum(1 for d in registry["directories"].values() if d.get("strong")))
        log(f"backup finished: {run['sessions']} chats ({n_missing} only in the vault), "
            f"{run['directories']} directories registered", quiet)
    finally:
        os.remove(lock)
        run.setdefault("finished", now_iso())
        os.makedirs(vpath(cfg, "logs"), exist_ok=True)
        with open(vpath(cfg, "logs", "runs.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(run, ensure_ascii=False) + "\n")
    return run
