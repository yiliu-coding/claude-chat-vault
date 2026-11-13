"""Incremental mirror of a Claude config directory (local, or remote over ssh) into the vault.

A file is copied only when its size or modification time changed since the last run. Copies are
never deleted: a file that disappears from the source is only marked missing in the manifest. When a
copy has to be replaced, the old copy is moved to versions/ unless the new file simply extends it.
Transcripts grow by appending, so ordinary growth does not pile up versions, while a truncated or
rewritten file keeps its previous state.
"""

import collections
import os
import shlex
import shutil
import subprocess
import tarfile
import threading

from .common import SECRET_NAMES, claude_dir_of, log, now_iso, read_json, ssh_args, ts_compact, vpath, write_json

PROBE = 4096


class SourceUnavailable(Exception):
    pass


def mirror_root(cfg, name):
    return vpath(cfg, "mirror", name)


def versions_root(cfg, name):
    return vpath(cfg, "versions", name)


def manifest_path(cfg, name):
    return vpath(cfg, "state", name, "manifest.json")


def load_manifest(cfg, name):
    return read_json(manifest_path(cfg, name), {"files": {}})


def _same(fa, fb, offset, length):
    fa.seek(offset)
    fb.seek(offset)
    return fa.read(length) == fb.read(length)


def is_extension(old, new):
    """True if `new` starts with the whole of `old` (compared at old's head and at old's tail)."""
    old_size, new_size = os.path.getsize(old), os.path.getsize(new)
    if new_size < old_size:
        return False
    with open(old, "rb") as fa, open(new, "rb") as fb:
        n = min(PROBE, old_size)
        return _same(fa, fb, 0, n) and _same(fa, fb, old_size - n, n)


def install(tmp, dest, rel, versions_dir, mtime_s):
    """Move a fetched file into place. Returns 'new', 'updated' (pure append) or 'versioned'."""
    status = "new"
    if os.path.exists(dest):
        if is_extension(dest, tmp):
            status = "updated"
        else:
            keep = os.path.join(versions_dir, *rel.split("/")) + "." + ts_compact()
            os.makedirs(os.path.dirname(keep), exist_ok=True)
            os.replace(dest, keep)
            status = "versioned"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.replace(tmp, dest)
    os.utime(dest, (mtime_s, mtime_s))
    return status


# ---------------------------------------------------------------------------------------------
# listing a source

def list_local(claude_dir, include):
    """{rel: (size, mtime)} for every file under the included items; mtime is st_mtime_ns as str."""
    found = {}

    def add(full):
        if os.path.basename(full) in SECRET_NAMES:
            return
        try:
            st = os.stat(full)
        except OSError:
            return
        rel = os.path.relpath(full, claude_dir).replace(os.sep, "/")
        found[rel] = (st.st_size, str(st.st_mtime_ns))

    for item in include:
        top = os.path.join(claude_dir, item)
        if os.path.isfile(top):
            add(top)
        elif os.path.isdir(top):
            for dirpath, _dirs, filenames in os.walk(top):
                for fn in filenames:
                    add(os.path.join(dirpath, fn))
    return found


def _remote_dir(path):
    # leave a leading ~ unquoted so the remote shell expands it
    if path == "~" or path.startswith("~/"):
        return "~" + shlex.quote(path[1:]) if len(path) > 1 else "~"
    return shlex.quote(path)


LIST_SCRIPT = r"""
cd {cdir} 2>/dev/null || {{ echo NODIR; exit 0; }}
for i in {items}; do
  [ -e "$i" ] && find "$i" -type f ! -name .credentials.json -printf 'F\t%s\t%T@\t%p\n'
done
for d in projects/*/; do [ -d "$d" ] && printf 'P\t%s\n' "$(basename "$d")"; done
for c in ~/.cache/claude-cli-nodejs/*/ ~/Library/Caches/claude-cli-nodejs/*/; do [ -d "$c" ] && printf 'C\t%s\n' "$(basename "$c")"; done
for s in sessions/*.json; do [ -f "$s" ] && printf 'S\t%s\n' "$(tr -d '\n\t' < "$s")"; done
exit 0
"""


def run_ssh(source, command, stdin_text=None, timeout=180):
    try:
        res = subprocess.run(ssh_args(source) + [command], input=stdin_text, capture_output=True,
                             encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceUnavailable(f"ssh to {source['host']} failed: {exc}")
    if res.returncode == 255:
        detail = [ln for ln in res.stderr.splitlines() if ln.strip() and "*" not in ln]
        raise SourceUnavailable(f"ssh to {source['host']} failed: {detail[-1] if detail else 'exit 255'}")
    return res.stdout


def list_remote(source, include):
    script = LIST_SCRIPT.format(cdir=_remote_dir(source["claude_dir"]),
                                items=" ".join(shlex.quote(i) for i in include))
    out = run_ssh(source, "bash -s", stdin_text=script)
    found, extra = {}, {"project_dirs": [], "cache_dirs": [], "sessions": [], "no_dir": False}
    for line in out.splitlines():
        parts = line.split("\t")
        if parts[0] == "NODIR":
            extra["no_dir"] = True
        elif parts[0] == "F" and len(parts) >= 4:
            rel = parts[3][2:] if parts[3].startswith("./") else parts[3]
            found[rel] = (int(parts[1]), parts[2])
        elif parts[0] == "P" and len(parts) == 2:
            extra["project_dirs"].append(parts[1])
        elif parts[0] == "C" and len(parts) == 2:
            extra["cache_dirs"].append(parts[1])
        elif parts[0] == "S" and len(parts) == 2:
            extra["sessions"].append(parts[1])
    return found, extra


# ---------------------------------------------------------------------------------------------
# fetching changed files

def fetch_remote(source, rels, deliver):
    cmd = ssh_args(source) + [f"cd {_remote_dir(source['claude_dir'])} && tar -cf - -T -"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def feed():
        proc.stdin.write(("\n".join(rels) + "\n").encode("utf-8"))
        proc.stdin.close()

    errors = []
    threading.Thread(target=feed, daemon=True).start()
    threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True).start()
    with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
        for member in tar:
            if member.isfile():
                name = member.name[2:] if member.name.startswith("./") else member.name
                deliver(name, tar.extractfile(member))
    proc.wait()


def mirror_source(cfg, source):
    """Bring the vault copy of one source up to date. Returns (stats, extra listing info)."""
    name = source["name"]
    man = load_manifest(cfg, name)
    files = man["files"]
    if source["type"] == "local":
        claude_dir = claude_dir_of(source)
        if not os.path.isdir(claude_dir):
            raise SourceUnavailable(f"{claude_dir} not found")
        listing, extra = list_local(claude_dir, cfg["include"]), None
    else:
        listing, extra = list_remote(source, cfg["include"])
        if extra["no_dir"]:
            raise SourceUnavailable(f"{source['claude_dir']} not found on {source['host']}")

    root, versions = mirror_root(cfg, name), versions_root(cfg, name)
    tmp = vpath(cfg, "state", "tmp", name + ".part")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    stats = collections.Counter()
    now = now_iso()

    changed = []
    for rel, (size, mtime) in listing.items():
        ent = files.get(rel)
        if ent and ent["size"] == size and ent["mtime"] == mtime and \
                os.path.exists(os.path.join(root, *rel.split("/"))):
            if not ent.get("present", True):
                ent["present"] = True
                ent.pop("missing_since", None)
                stats["reappeared"] += 1
            stats["unchanged"] += 1
        else:
            changed.append(rel)

    def deliver(rel, fileobj):
        if rel not in listing:
            return
        size, mtime = listing[rel]
        with open(tmp, "wb") as out:
            shutil.copyfileobj(fileobj, out)
        mtime_s = int(mtime) / 1e9 if source["type"] == "local" else float(mtime)
        status = install(tmp, os.path.join(root, *rel.split("/")), rel, versions, mtime_s)
        ent = files.setdefault(rel, {"first_seen": now})
        ent.update(size=size, mtime=mtime, present=True, changed=now)
        ent.pop("missing_since", None)
        stats[status] += 1
        stats["bytes_copied"] += size

    if changed and source["type"] == "local":
        for rel in changed:
            try:
                with open(os.path.join(claude_dir, *rel.split("/")), "rb") as fh:
                    deliver(rel, fh)
            except OSError as exc:
                stats["errors"] += 1
                log(f"  could not copy {rel}: {exc}")
    elif changed:
        fetch_remote(source, changed, deliver)

    included = set(cfg["include"])
    for rel, ent in files.items():
        if rel not in listing and ent.get("present", True) and rel.split("/")[0] in included:
            ent["present"] = False
            ent["missing_since"] = now
            stats["went_missing"] += 1
    stats["missing_total"] = sum(1 for e in files.values() if not e.get("present", True))
    stats["files_total"] = len(files)
    man["last_run"] = now
    write_json(manifest_path(cfg, name), man)
    return stats, extra
