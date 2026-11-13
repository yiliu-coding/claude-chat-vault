"""Human-readable files written into the vault after every backup, so the vault explains itself
even without this tool: INVENTORY.md (every directory and chat) and README.md (layout, recovery)."""

import collections
import os

from .common import human_bytes, now_iso, vault_dir, vpath

DATA_README = """# Claude chat vault (data folder)

This folder is a backup of Claude Code chats, written by `vault.py backup` from the tool folder
one level up. It is safe to copy this folder elsewhere; everything below is plain files.

| folder | contents |
|---|---|
| `mirror/<source>/` | a copy of that machine's `~/.claude` (`projects/` = chats and memory, `file-history/`, settings). Files are never deleted here, even when Claude Code deletes them. |
| `versions/<source>/` | earlier states of files that were rewritten or truncated (`<name>.<UTC time>`) |
| `registry/directories.json` | every directory where Claude Code was used, with the evidence, kept forever |
| `catalog/sessions.json` | one entry per chat: title, directory, dates, prompts, whether it is still in `~/.claude` |
| `INVENTORY.md` | the same, readable: directory -> chats -> file in this vault |
| `annotations.json` | your notes, tags and stars from the dashboard |
| `logs/` | `backup.log` (text) and `runs.jsonl` (one line per backup run) |
| `state/` | manifests used to copy only changed files (safe to delete: the next run re-checks everything) |

## Recover a chat by hand

A chat is `mirror/<source>/projects/<folder>/<session id>.jsonl`, plus the folder
`mirror/<source>/projects/<folder>/<session id>/` when it exists (subagents, large tool results).
`<folder>` is the chat's working directory with every character that is not a letter or digit
replaced by `-`, e.g. `C:\\Users\\me\\Projects\\my_app` -> `C--Users-me-Projects-my-app`, or
`/home/me/my_app` -> `-home-me-my-app`.

1. Copy the `.jsonl` file (and the folder, if any) into `~/.claude/projects/<folder>/` on the machine
   it came from. Do not overwrite a file that is already there.
2. Open that working directory in VS Code: the chat appears in the Claude Code chat list, or run
   `claude --resume <session id>` in a terminal inside the directory.
3. Copy `mirror/<source>/projects/<folder>/memory/` back too if that project's memory notes are gone.

If a restored chat disappears again on the next start, its file is older than `cleanupPeriodDays`
in `~/.claude/settings.json`: raise that value, or give the file the current date.
"""


def _cell(text):
    return (text or "").replace("|", "\\|").replace("\n", " ")


def write_reports(cfg, catalog, registry):
    root = vault_dir(cfg)
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(DATA_README)

    sessions = catalog["sessions"]
    by_dir = collections.defaultdict(list)
    for s in sessions:
        by_dir[(s["source"], s["project"])].append(s)
    dirs = registry["directories"]
    path_of = {(d["source"], d["encoded"].lower()): d for d in dirs.values()}
    missing = sum(1 for s in sessions if not s["in_source"])

    lines = ["# Claude chat vault - inventory", "",
             f"Generated {now_iso()} by `vault.py backup`. Vault folder: `{root}`.", "",
             f"{len(sessions)} chats in {len(by_dir)} directories; {missing} of them exist only in this "
             "vault (gone from `~/.claude`). How to put a chat back: see `README.md` in this folder, or "
             "run `python vault.py restore --session <id>` in the tool folder.", ""]

    def dir_title(key):
        d = path_of.get((key[0], key[1].lower()))
        return (d or {}).get("path") or key[1]

    order = sorted(by_dir, key=lambda k: max(s.get("last_activity") or "" for s in by_dir[k]), reverse=True)
    for key in order:
        rows = by_dir[key]
        size = sum(s["size"] + s.get("sidecar_bytes", 0) for s in rows)
        lines += [f"## {dir_title(key)}  ({key[0]})", "",
                  f"Claude folder `projects/{key[1]}` - {len(rows)} chats, {human_bytes(size)}", "",
                  "| last activity | title | session | prompts | size | in ~/.claude |",
                  "|---|---|---|---|---|---|"]
        for s in rows:
            lines.append(f"| {(s.get('last_activity') or '')[:10]} | {_cell(s.get('title'))} | "
                         f"`{s['session_id']}` | {s['n_prompts']} | {human_bytes(s['size'])} | "
                         f"{'yes' if s['in_source'] else 'NO - vault only'} |")
        lines.append("")

    with_chats = {(s, p.lower()) for s, p in by_dir}
    rest = [d for d in dirs.values() if d.get("strong") and (d["source"], d["encoded"].lower()) not in with_chats]
    if rest:
        lines += ["## Directories where Claude Code was used, but no chat is in the vault", "",
                  "Usually chats deleted before the vault existed. Evidence = where the directory was seen.", "",
                  "| directory | source | evidence | first seen |", "|---|---|---|---|"]
        for d in sorted(rest, key=lambda d: (d["source"], d.get("path") or d["encoded"])):
            lines.append(f"| {_cell(d.get('path') or d['encoded'])} | {d['source']} | "
                         f"{', '.join(sorted(d['evidence']))} | {d['first_seen'][:10]} |")
        lines.append("")

    with open(vpath(cfg, "INVENTORY.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
