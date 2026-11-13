"""Put chats from the vault back into a Claude config directory so Claude Code lists them again.

Restoring never overwrites: a file that already exists at the destination is left alone and
reported as skipped. Restored files get the current time as modification time when they would
otherwise be older than that machine's cleanupPeriodDays, so the next cleanup does not remove them.
"""

import io
import os
import shutil
import subprocess
import tarfile
import time

from .common import claude_dir_of, read_json, ssh_args, vpath
from .mirror import _remote_dir, mirror_root


def session_files(cfg, source_name, project, session_id):
    root = mirror_root(cfg, source_name)
    rels = [f"projects/{project}/{session_id}.jsonl"]
    for base in (f"projects/{project}/{session_id}", f"file-history/{session_id}"):
        top = os.path.join(root, *base.split("/"))
        for dirpath, _dirs, files in os.walk(top):
            for fn in files:
                rels.append(os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/"))
    return root, rels


def memory_files(cfg, source_name, project):
    root = mirror_root(cfg, source_name)
    top = os.path.join(root, "projects", project, "memory")
    rels = []
    for dirpath, _dirs, files in os.walk(top):
        for fn in files:
            rels.append(os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/"))
    return root, rels


def find_session(cfg, source_name, session_id):
    cat = read_json(vpath(cfg, "catalog", "sessions.json"), {"sessions": []})
    for s in cat["sessions"]:
        if s["session_id"] == session_id and (source_name is None or s["source"] == source_name):
            return s
    return None


def _cleanup_days(claude_dir):
    days = read_json(os.path.join(claude_dir, "settings.json"), {}).get("cleanupPeriodDays", 30)
    return days if isinstance(days, (int, float)) and days > 0 else 30


def _restore_local(source, root, rels, dry_run):
    claude_dir = claude_dir_of(source)
    cutoff = time.time() - (_cleanup_days(claude_dir) - 1) * 86400
    done, skipped, touched = [], [], 0
    for rel in rels:
        src, dst = os.path.join(root, *rel.split("/")), os.path.join(claude_dir, *rel.split("/"))
        if os.path.exists(dst):
            skipped.append(rel)
            continue
        if not dry_run:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            if os.path.getmtime(dst) < cutoff:
                os.utime(dst, None)
                touched += 1
        done.append(rel)
    return {"restored": done, "skipped": skipped, "touched": touched, "dry_run": dry_run}


def _restore_remote(source, root, rels, dry_run):
    if dry_run:
        return {"restored": rels, "skipped": [], "touched": 0, "dry_run": True,
                "note": "remote dry run: existing files are only detected on the real run"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel in rels:
            tar.add(os.path.join(root, *rel.split("/")), arcname=rel)
    # -k keeps existing files, -m stamps restored files with the current time
    cdir = _remote_dir(source["claude_dir"])
    res = subprocess.run(ssh_args(source) + [f"mkdir -p {cdir} && cd {cdir} && tar -xkmf - 2>&1; true"],
                         input=buf.getvalue(), capture_output=True, timeout=300)
    out = res.stdout.decode("utf-8", "replace")
    skipped = [rel for rel in rels if rel in out]
    return {"restored": [r for r in rels if r not in skipped], "skipped": skipped,
            "touched": len(rels) - len(skipped), "dry_run": False}


def restore(cfg, source_name, rels_root_rels, dry_run=False):
    source = next((s for s in cfg["sources"] if s["name"] == source_name), None)
    if source is None:
        raise ValueError(f"unknown source {source_name!r}")
    root, rels = rels_root_rels
    if source["type"] == "local":
        return _restore_local(source, root, rels, dry_run)
    return _restore_remote(source, root, rels, dry_run)


def restore_session(cfg, session_id, source_name=None, dry_run=False, with_memory=True):
    s = find_session(cfg, source_name, session_id)
    if s is None:
        raise ValueError(f"session {session_id} is not in the vault catalog")
    result = restore(cfg, s["source"], session_files(cfg, s["source"], s["project"], session_id), dry_run)
    if with_memory:
        mem = restore(cfg, s["source"], memory_files(cfg, s["source"], s["project"]), dry_run)
        result["restored"] += mem["restored"]
        result["skipped"] += mem["skipped"]
    result["session"] = s
    return result
