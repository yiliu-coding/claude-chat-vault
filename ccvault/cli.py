"""Command line: python vault.py <command> [options]. Run `python vault.py -h` for the list."""

import argparse
import json
import os
import sys

from . import discover, retention, schedule
from .backup import BackupBusy, run_backup
from .common import (DEFAULT_CONFIG, config_exists, find_source, human_bytes, is_primary_local, load_config,
                     read_json, save_config, tilde, vault_dir, vpath, write_json)
from .mirror import mirror_root
from .registry import merge_discovery
from .restore import find_session, restore_session
from .sessions import live_view, render_entries, to_markdown


def _catalog(cfg):
    return read_json(vpath(cfg, "catalog", "sessions.json"), {"sessions": []})["sessions"]


def _interactive():
    return sys.stdin is not None and sys.stdin.isatty()


def _ask(question, default=True):
    if not _interactive():
        return default
    hint = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {hint} ").strip().lower()
    return default if not answer else answer in ("y", "yes")


def _primary(cfg):
    return next((s for s in cfg["sources"] if is_primary_local(s)), cfg["sources"][0])


# ---------------------------------------------------------------------------------------------
# setup / start / info

def print_discovery(disc):
    s = disc["system"]
    print(f"\nThis PC: {s['os']}, {s['hostname']} (user {s['user']}), {s['cpu_count']} CPUs, "
          f"{s['memory_gb']} GB RAM, {human_bytes(s['disk']['free'])} free")
    print(f"Python {s['python']['version']}; Claude Code CLI: "
          f"{(s['claude_cli'] or {}).get('version') or 'not on PATH (the VS Code extension brings its own)'}")
    for e in s["editors"]:
        print(f"  {e['name']}: Claude extension {', '.join(e['claude_extension'] or ['not installed'])}, "
              f"{e['workspaces']} workspaces")
    print("\nClaude Code data folders:")
    for d in disc["claude_dirs"] or [{"path": "none found", "chats": 0, "bytes": 0, "cleanup_period_days": None}]:
        days = d["cleanup_period_days"]
        print(f"  {d['path']}: {d['chats']} chats, {human_bytes(d['bytes'])}, chats kept "
              f"{retention.describe(days) if isinstance(days, int) else '30 days (default)'}")
    strong = [f for f in disc["folders"] if f["strong"]]
    print(f"\nFolders where Claude Code was used: {len(strong)} (+{len(disc['folders']) - len(strong)} with weak evidence)")
    for f in strong[:15]:
        print(f"  {f['source']:8s} {f['chats']:3d} chats  {f['path'] or f['encoded']}")
    if len(strong) > 15:
        print(f"  ... and {len(strong) - 15} more (see the dashboard)")
    if disc["suggested_sources"]:
        print("\nOther places found (added to the config switched OFF; turn them on in the dashboard):")
        for x in disc["suggested_sources"]:
            print(f"  {x['source']['name']}: {x['why']}")


def cmd_setup(_cfg, args):
    existing = load_config() if config_exists() else None
    base = existing or json.loads(json.dumps(DEFAULT_CONFIG))
    if args.vault_dir:
        base["vault_dir"] = args.vault_dir
    print("Scanning this PC for Claude Code data and usage (nothing leaves this PC)...")
    disc = discover.run_discovery(base, include_wsl=args.wsl, save=False)
    print_discovery(disc)
    cfg = discover.config_from_discovery(disc, base)
    print(f"\nThe vault will be kept in {vault_dir(cfg)}")
    if not args.yes and not _ask("Save this configuration?"):
        print("Nothing saved.")
        return 1
    save_config(cfg)
    write_json(vpath(cfg, "discovery.json"), disc)
    merge_discovery(cfg, disc)
    print("Saved config.json.")

    st = retention.status(cfg, _primary(cfg)["name"])
    print(f"\nClaude Code deletes chats not used for {st['effective_label']} "
          f"({'your setting' if st['value'] else 'default'}; file {st['file']}).")
    days = args.retention
    if days is None and not args.yes and _interactive() and not st["locked"]:
        reply = input("Keep chats for how many days instead? (Enter = no change, e.g. 365) ").strip()
        days = int(reply) if reply.isdigit() else None
    if days:
        _apply_retention(cfg, _primary(cfg)["name"], days, assume_yes=args.yes)

    if args.schedule or (not args.yes and _ask(f"Back up automatically every day at {cfg['schedule']['time']}?")):
        try:
            schedule.install(cfg, cfg["schedule"]["time"])
            save_config(cfg)
            print("Daily backup scheduled.")
        except Exception as exc:
            print(f"Could not schedule the backup: {exc}")
    if args.backup or (not args.yes and _ask("Run the first backup now?")):
        run_backup(cfg)
    print("\nNext: open the dashboard with `python vault.py serve` (or the start script).")
    return 0


def cmd_start(cfg, args):
    if not config_exists() or args.rescan:
        cmd_setup(cfg, argparse.Namespace(yes=True, wsl=False, vault_dir=None, retention=None,
                                          schedule=False, backup=False))
    cfg = load_config()
    from .server import serve
    serve(cfg, port=args.port or cfg.get("dashboard_port", 8765), open_browser=not args.no_browser)
    return 0


def cmd_info(cfg, args):
    disc = discover.run_discovery(cfg, include_wsl=args.wsl, save=True)
    merge_discovery(cfg, disc)
    if args.json:
        print(json.dumps(disc, indent=1, ensure_ascii=False, default=str))
    else:
        print_discovery(disc)
    return 0


# ---------------------------------------------------------------------------------------------
# retention / schedule

def _apply_retention(cfg, source, days, assume_yes=False):
    days = retention.validate_days(days)
    st = retention.status(cfg, source)
    risk = retention.would_delete(st, days)
    print(f"New value: {days} days ({retention.describe(days)}).")
    if risk["would_delete"]:
        print(f"WARNING: {risk['would_delete']} chats are older than that and would be deleted the next time "
              f"Claude Code starts ({risk['already_in_vault']} of them are already in the vault).")
    if not assume_yes and not _ask(f"Write cleanupPeriodDays = {days} to {st['file']}?", default=not risk["would_delete"]):
        print("Not changed.")
        return None
    res = retention.set_days(cfg, source, days)
    print(f"Changed {res['file']}: {res['old']} -> {res['new']}" +
          (f" (previous file kept as {res['backup']})" if res.get("backup") else ""))
    return res


def cmd_retention(cfg, args):
    source = args.source or _primary(cfg)["name"]
    st = retention.status(cfg, source)
    print(f"Source {source}: settings file {st['file']}{'' if st['exists'] else ' (does not exist yet)'}")
    print(f"cleanupPeriodDays: {st['value'] if st['value'] is not None else 'not set'} -> chats are kept "
          f"{st['effective_label']} after their last use")
    if st["locked"]:
        print(f"Locked by your organisation in {st['managed_file']}.")
    print(f"{st['n_chats']} chats there now; the oldest was last used {st['oldest_days']} days ago.")
    if args.set is not None:
        _apply_retention(cfg, source, args.set, assume_yes=args.yes)
    return 0


def cmd_schedule(cfg, args):
    if args.action == "install":
        st = schedule.install(cfg, args.time or cfg["schedule"]["time"])
        save_config(cfg)
    elif args.action == "remove":
        st = schedule.remove(cfg)
    else:
        st = schedule.status(cfg)
    if st.get("installed"):
        print(f"Daily backup: on ({st['system']}, {st.get('time')}), next run {st.get('next', 'see the scheduler')}")
    else:
        print(f"Daily backup: off ({st['system']})")
    return 0


# ---------------------------------------------------------------------------------------------
# backup / status / list / restore / export / serve

def cmd_backup(cfg, args):
    try:
        run = run_backup(cfg, only=args.source, quiet=args.quiet)
    except BackupBusy as exc:
        print(exc)
        return 2
    return 0 if all(s.get("ok") for s in run["sources"].values()) else 1


def cmd_status(cfg, args):
    runs_file = vpath(cfg, "logs", "runs.jsonl")
    last = None
    if os.path.exists(runs_file):
        with open(runs_file, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
            last = json.loads(lines[-1]) if lines else None
    sessions = _catalog(cfg)
    reg = read_json(vpath(cfg, "registry", "directories.json"), {"directories": {}})["directories"]
    total = sum(os.path.getsize(os.path.join(dp, f)) for dp, _d, fs in os.walk(vault_dir(cfg)) for f in fs)
    print(f"vault: {vault_dir(cfg)}  ({human_bytes(total)})")
    print(f"chats: {len(sessions)}  only in the vault: {sum(1 for s in sessions if not s['in_source'])}")
    print(f"directories with chat evidence: {sum(1 for d in reg.values() if d.get('strong'))}")
    if last:
        srcs = ", ".join(f"{k} {'ok' if v.get('ok') else 'unreachable'}" for k, v in last["sources"].items())
        print(f"last backup: {last.get('finished')} ({srcs})")
    else:
        print("last backup: never - run `python vault.py backup`")
    return 0


def cmd_list(cfg, args):
    chats = sorted(live_view(cfg, _catalog(cfg)), key=lambda s: s.get("last_activity") or "", reverse=True)
    for s in chats:
        where = s.get("cwd") or s["project"]
        if args.dir and args.dir.lower() not in where.lower():
            continue
        if args.missing and s["in_source"]:
            continue
        flag = "!" if not s["in_source"] else "+" if s["pending_bytes"] else " "
        print(f"{flag} {(s.get('started') or '')[:10]} {(s.get('last_activity') or '')[:10]}  {s['session_id']}  "
              f"{s['source']:8s} {(s.get('title') or '')[:60]:60s}  {where}")
    print("\ncolumns: started, last activity (from the live chat when it is newer than the backup)")
    print("! = only in the vault (no longer in ~/.claude)   + = changed since the last backup")
    return 0


def cmd_restore(cfg, args):
    ids = [args.session] if args.session else []
    if args.all_missing:
        ids = [s["session_id"] for s in _catalog(cfg) if not s["in_source"]
               and (not args.dir or args.dir.lower() in (s.get("cwd") or s["project"]).lower())
               and (not args.source or s["source"] == args.source)]
    if not ids:
        print("nothing to restore (give --session ID, or --all-missing [--dir PART])")
        return 1
    for sid in ids:
        res = restore_session(cfg, sid, source_name=args.source, dry_run=args.dry_run)
        s = res["session"]
        verb = "would restore" if args.dry_run else "restored"
        print(f"{sid} ({s.get('title') or ''}): {verb} {len(res['restored'])} files, "
              f"skipped {len(res['skipped'])} already present")
    if not args.dry_run:
        print("Reopen the directory in VS Code (or run `claude --resume <id>` inside it) to see the chats.")
    return 0


def cmd_export(cfg, args):
    s = find_session(cfg, args.source, args.session)
    if s is None:
        print(f"session {args.session} is not in the vault")
        return 1
    path = os.path.join(mirror_root(cfg, s["source"]), *s["rel"].split("/"))
    text = to_markdown(s, render_entries(path, tool_limit=args.tool_chars))
    out = args.out or vpath(cfg, "exports", f"{s['session_id']}.md")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(out)
    return 0


def cmd_serve(cfg, args):
    from .server import serve
    serve(cfg, port=args.port or cfg.get("dashboard_port", 8765), open_browser=not args.no_browser)
    return 0


def main(argv=None):
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="vault.py", description="Back up, register and browse Claude Code chats.")
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("start", help="first run: scan this PC and write the config; then open the dashboard")
    st.add_argument("--rescan", action="store_true", help="scan again even if a config exists")
    st.add_argument("--port", type=int)
    st.add_argument("--no-browser", action="store_true")

    su = sub.add_parser("setup", help="guided setup: scan this PC, write config.json, retention, schedule")
    su.add_argument("--yes", action="store_true", help="no questions; only save the config")
    su.add_argument("--wsl", action="store_true", help="also look inside WSL distributions (starts them)")
    su.add_argument("--vault-dir", help="where to keep the vault (default: data/ in this folder)")
    su.add_argument("--retention", type=int, help="set cleanupPeriodDays to this many days")
    su.add_argument("--schedule", action="store_true", help="install the daily backup")
    su.add_argument("--backup", action="store_true", help="run the first backup")

    inf = sub.add_parser("info", help="details of this PC, Claude Code folders and usage found")
    inf.add_argument("--json", action="store_true")
    inf.add_argument("--wsl", action="store_true")

    r = sub.add_parser("retention", help="show or change how long Claude Code keeps chats (cleanupPeriodDays)")
    r.add_argument("--set", type=int, metavar="DAYS")
    r.add_argument("--source", help="source name (default: this PC)")
    r.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    sc = sub.add_parser("schedule", help="daily automatic backup: status, install or remove")
    sc.add_argument("action", nargs="?", default="status", choices=["status", "install", "remove"])
    sc.add_argument("--time", help="HH:MM, 24-hour clock (default 12:30)")

    b = sub.add_parser("backup", help="copy new/changed chats into the vault, update register and reports")
    b.add_argument("--source", help="only this source")
    b.add_argument("--quiet", action="store_true")

    sub.add_parser("status", help="vault size, chat counts, last run")

    ls = sub.add_parser("list", help="list chats in the vault")
    ls.add_argument("--dir", help="only directories containing this text")
    ls.add_argument("--missing", action="store_true", help="only chats gone from ~/.claude")

    rs = sub.add_parser("restore", help="copy chats back into ~/.claude (never overwrites)")
    rs.add_argument("--session", help="session id")
    rs.add_argument("--all-missing", action="store_true", help="every chat that is only in the vault")
    rs.add_argument("--dir", help="with --all-missing: only directories containing this text")
    rs.add_argument("--source", help="source name, if the id exists on several machines")
    rs.add_argument("--dry-run", action="store_true")

    e = sub.add_parser("export", help="write a chat as readable Markdown")
    e.add_argument("--session", required=True)
    e.add_argument("--source")
    e.add_argument("--out", help="output .md file (default data/exports/<id>.md)")
    e.add_argument("--tool-chars", type=int, default=2000, help="characters kept per tool result")

    s = sub.add_parser("serve", help="open the dashboard")
    s.add_argument("--port", type=int)
    s.add_argument("--no-browser", action="store_true")

    args = p.parse_args(argv)
    if args.cmd in ("start", "setup"):
        return {"start": cmd_start, "setup": cmd_setup}[args.cmd](None, args)
    if not config_exists():
        print("No config.json yet - run `python vault.py setup` (or the start script) first.")
        return 1
    cfg = load_config()
    handler = {"backup": cmd_backup, "status": cmd_status, "list": cmd_list, "restore": cmd_restore,
               "export": cmd_export, "serve": cmd_serve, "info": cmd_info, "retention": cmd_retention,
               "schedule": cmd_schedule}[args.cmd]
    try:
        return handler(cfg, args)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
