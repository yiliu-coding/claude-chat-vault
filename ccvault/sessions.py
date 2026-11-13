"""Reading Claude Code transcripts: per-chat metadata for the catalog, and readable renderings.

A transcript is ~/.claude/projects/<encoded working dir>/<session id>.jsonl, one JSON record per line.
Its sibling folder <session id>/ holds subagent transcripts and large tool results.
"""

import collections
import glob
import json
import os

from .common import claude_dir_of, now_iso, read_json, vpath, write_json
from .mirror import load_manifest, mirror_root

SNIPPET = 300
# bump when parse_meta changes, so cached metadata of unchanged transcripts is re-read
PARSER_VERSION = 3


def _texts(content):
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return []


def user_prompt(rec):
    """The text a person typed, or None for tool results, meta records and command echoes."""
    if rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isSidechain") \
            or rec.get("isVisibleInTranscriptOnly"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                         for b in content):
        return None
    parts = [t.strip() for t in _texts(content) if t.strip() and not t.lstrip().startswith("<")]
    return " ".join(" ".join(parts).split()) or None


def queued_prompt(rec):
    """A prompt the person typed while Claude was still working, or None.

    Claude Code does not record such a prompt as a "user" record: it is delivered later as an
    "attachment" record of type queued_command with origin.kind "human". Task notifications and
    messages from subagents or other sessions use the same record type with another origin.
    """
    if rec.get("type") != "attachment" or rec.get("isSidechain"):
        return None
    att = rec.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "queued_command" or att.get("isMeta"):
        return None
    origin = att.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != "human":
        return None
    prompt = att.get("prompt")
    text = prompt if isinstance(prompt, str) else "\n".join(_texts(prompt))
    return text.strip() or None


def is_activity(rec):
    """True for a real message: a prompt, a reply, a tool call or result, or a queued prompt.

    Claude Code also writes bookkeeping records (queue operations, titles, environment
    attachments...). Some of them are written into every open chat at once, e.g. when VS Code
    closes, so their timestamps say nothing about when the chat was last used.
    """
    kind = rec.get("type")
    if kind in ("user", "assistant"):
        return not rec.get("isMeta")
    return kind == "attachment" and queued_prompt(rec) is not None


def parse_meta(path):
    m = {"cwd": None, "cwds": [], "title": None, "ai_title": None, "custom_title": None,
         "summary": None, "first_prompt": None, "last_prompt": None, "started": None,
         "last_activity": None, "n_prompts": 0, "n_replies": 0, "models": [], "version": None,
         "git_branch": None, "entrypoint": None, "cost_usd": None, "bridge_session": None,
         "first_record": None, "last_record": None}
    reply_ids, models, cwds = set(), collections.Counter(), []
    last_typed = None
    with open(path, "rb") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            ts = rec.get("timestamp")
            if isinstance(ts, str):
                if m["first_record"] is None or ts < m["first_record"]:
                    m["first_record"] = ts
                if m["last_record"] is None or ts > m["last_record"]:
                    m["last_record"] = ts
                if is_activity(rec):
                    if m["started"] is None or ts < m["started"]:
                        m["started"] = ts
                    if m["last_activity"] is None or ts > m["last_activity"]:
                        m["last_activity"] = ts
            cwd = rec.get("cwd")
            if cwd and (not cwds or cwds[-1] != cwd) and cwd not in cwds:
                cwds.append(cwd)
            m["version"] = rec.get("version") or m["version"]
            m["git_branch"] = rec.get("gitBranch") or m["git_branch"]
            m["entrypoint"] = m["entrypoint"] or rec.get("entrypoint")
            if kind in ("user", "attachment"):
                text = user_prompt(rec) if kind == "user" else queued_prompt(rec)
                if text:
                    text = " ".join(text.split())
                    m["n_prompts"] += 1
                    m["first_prompt"] = m["first_prompt"] or text[:SNIPPET]
                    last_typed = text
            elif kind == "assistant":
                msg = rec.get("message") or {}
                reply_ids.add(msg.get("id") or rec.get("uuid"))
                if msg.get("model") and msg["model"] != "<synthetic>":
                    models[msg["model"]] += 1
            elif kind == "ai-title":
                m["ai_title"] = rec.get("aiTitle") or m["ai_title"]
            elif kind == "custom-title":
                m["custom_title"] = rec.get("customTitle") or m["custom_title"]
            elif kind == "summary":
                m["summary"] = rec.get("summary") or m["summary"]
            elif kind == "last-prompt":
                m["last_prompt"] = rec.get("lastPrompt") or m["last_prompt"]
            elif kind == "cost-state":
                m["cost_usd"] = rec.get("totalCostUSD", m["cost_usd"])
            elif kind == "bridge-session":
                m["bridge_session"] = rec.get("bridgeSessionId") or m["bridge_session"]
    m["n_replies"] = len(reply_ids)
    m["started"] = m["started"] or m["first_record"]          # a chat without any message
    m["last_activity"] = m["last_activity"] or m["last_record"]
    m["models"] = [k for k, _ in models.most_common()]
    m["cwds"] = cwds[:8]
    m["cwd"] = cwds[0] if cwds else None
    if not m["last_prompt"] and last_typed:
        m["last_prompt"] = last_typed[:SNIPPET]
    elif m["last_prompt"]:
        m["last_prompt"] = " ".join(m["last_prompt"].split())[:SNIPPET]
    m["title"] = m["custom_title"] or m["ai_title"] or m["summary"] or \
        (m["first_prompt"][:80] if m["first_prompt"] else None)
    return m


def _version_counts(cfg, source_name):
    counts = collections.Counter()
    root = vpath(cfg, "versions", source_name, "projects")
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            base = fn.rsplit(".", 1)[0]          # <id>.jsonl.<stamp> -> <id>.jsonl
            rel = os.path.relpath(os.path.join(dirpath, base), root).replace(os.sep, "/")
            counts[rel] += 1
    return counts


def build_catalog(cfg):
    """Index every transcript in the vault (including ones gone from ~/.claude)."""
    cache_path = vpath(cfg, "state", "parse_cache.json")
    cache = read_json(cache_path, {})
    sessions, seen = [], set()
    for source in cfg["sources"]:
        name = source["name"]
        root = os.path.join(mirror_root(cfg, name), "projects")
        if not os.path.isdir(root):
            continue
        files = load_manifest(cfg, name)["files"]
        versions = _version_counts(cfg, name)
        for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
            project = os.path.basename(os.path.dirname(path))
            sid = os.path.basename(path)[:-len(".jsonl")]
            rel = f"projects/{project}/{sid}.jsonl"
            st = os.stat(path)
            key, stamp = f"{name}/{rel}", f"p{PARSER_VERSION}:{st.st_size}:{st.st_mtime_ns}"
            hit = cache.get(key)
            if hit and hit.get("stamp") == stamp:
                meta = hit["meta"]
            else:
                meta = parse_meta(path)
                cache[key] = {"stamp": stamp, "meta": meta}
            seen.add(key)
            ent = files.get(rel, {})
            sub = os.path.join(root, project, sid)
            sessions.append(dict(
                meta, source=name, source_type=source["type"], session_id=sid, project=project,
                rel=rel, size=st.st_size, in_source=ent.get("present", True),
                missing_since=ent.get("missing_since"), first_backup=ent.get("first_seen"),
                backed_up=ent.get("changed") or ent.get("first_seen"),
                versions=versions.get(f"{project}/{sid}.jsonl", 0),
                subagents=len(glob.glob(os.path.join(sub, "subagents", "*.jsonl"))),
                sidecar_bytes=sum(os.path.getsize(p) for p in glob.glob(os.path.join(sub, "**", "*"),
                                                                       recursive=True)
                                  if os.path.isfile(p))))
    for key in [k for k in cache if k not in seen]:
        del cache[key]
    write_json(cache_path, cache)
    sessions.sort(key=lambda s: s.get("last_activity") or "", reverse=True)
    catalog = {"generated": now_iso(), "sessions": sessions}
    write_json(vpath(cfg, "catalog", "sessions.json"), catalog)
    return catalog


# ---------------------------------------------------------------------------------------------
# the live chat, for chats that grew after the last backup

def _tail_activity(path, offset):
    """(latest message time, prompts) in the part of `path` after byte `offset`."""
    last, prompts = None, 0
    with open(path, "rb") as fh:
        if offset:
            fh.seek(offset - 1)
            if fh.read(1) != b"\n":
                fh.readline()          # rest of a line that was half-written at the backup
        for raw in fh:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            ts = rec.get("timestamp")
            if isinstance(ts, str) and is_activity(rec) and (last is None or ts > last):
                last = ts
            if (rec.get("type") == "user" and user_prompt(rec)) or queued_prompt(rec):
                prompts += 1
    return last, prompts


def live_view(cfg, sessions, cache=None):
    """Bring catalog entries up to date with the chats as they are now in ~/.claude.

    The catalog describes the vault copies, made at the last backup. For a chat that is still in
    a local ~/.claude and has grown since, only the new bytes are read, to find the real last
    activity and the prompts not backed up yet. Sets, per chat: vault_last_activity (time in the
    vault copy), pending_bytes / pending_prompts (not in the vault yet), and updates
    last_activity and n_prompts. Remote (ssh) sources are left as backed up.
    """
    cache = {} if cache is None else cache
    dirs = {s["name"]: claude_dir_of(s) for s in cfg["sources"] if s["type"] == "local"}
    for s in sessions:
        s.setdefault("vault_last_activity", s.get("last_activity"))
        s.setdefault("vault_prompts", s.get("n_prompts", 0))
        s["pending_bytes"], s["pending_prompts"] = 0, 0
        if s["source"] not in dirs or not s.get("in_source"):
            continue
        live = os.path.join(dirs[s["source"]], *s["rel"].split("/"))
        try:
            st = os.stat(live)
        except OSError:
            continue
        if st.st_size <= s["size"]:
            continue
        key = (live, st.st_size, st.st_mtime_ns, s["size"])
        if key not in cache:
            try:
                cache[key] = _tail_activity(live, s["size"])
            except OSError:
                continue
        last, prompts = cache[key]
        s["pending_bytes"], s["pending_prompts"] = st.st_size - s["size"], prompts
        s["n_prompts"] = s["vault_prompts"] + prompts
        if last and last > (s["vault_last_activity"] or ""):
            s["last_activity"] = last
    return sessions


# ---------------------------------------------------------------------------------------------
# readable rendering (dashboard viewer and Markdown export)

def _short(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"\n... [{len(text) - limit:,} more characters]"


def _tool_label(block):
    inp = block.get("input") or {}
    hint = inp.get("description") or inp.get("command") or inp.get("file_path") or \
        inp.get("pattern") or inp.get("url") or inp.get("query") or inp.get("prompt") or ""
    if not isinstance(hint, str):
        hint = json.dumps(hint, ensure_ascii=False)
    return f"{block.get('name', 'tool')}: {' '.join(hint.split())[:200]}"


def render_entries(path, tool_limit=1500):
    """Conversation as a list of {role, text, ts}; tool traffic is summarised and truncated."""
    out = []
    with open(path, "rb") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("type") not in ("user", "assistant", "system", "attachment"):
                continue
            ts = rec.get("timestamp")
            if rec["type"] == "attachment":
                text = queued_prompt(rec)
                if text:
                    out.append({"role": "user", "text": text, "ts": ts, "queued": True})
                continue
            if rec["type"] == "system":
                if rec.get("subtype") == "compact_boundary" or rec.get("compactMetadata"):
                    out.append({"role": "system", "text": "[conversation compacted]", "ts": ts})
                continue
            content = (rec.get("message") or {}).get("content")
            if rec["type"] == "user":
                if rec.get("isCompactSummary"):
                    out.append({"role": "system", "text": "Summary carried over after compaction:\n"
                                + _short("\n".join(_texts(content)), tool_limit), "ts": ts})
                    continue
                text = user_prompt(rec)
                if text:
                    full = "\n".join(t for t in _texts(content) if not t.lstrip().startswith("<"))
                    out.append({"role": "user", "text": full.strip(), "ts": ts})
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            res = b.get("content")
                            res = "\n".join(_texts(res)) if isinstance(res, list) else res
                            out.append({"role": "tool_result", "text": _short(res or "", tool_limit),
                                        "ts": ts, "error": bool(b.get("is_error"))})
                continue
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    out.append({"role": "assistant", "text": b["text"].strip(), "ts": ts})
                elif b.get("type") == "tool_use":
                    out.append({"role": "tool_use", "text": _tool_label(b), "ts": ts})
    return out


def to_markdown(session, entries):
    lines = [f"# {session.get('title') or session['session_id']}", "",
             f"- Session: `{session['session_id']}` ({session['source']})",
             f"- Directory: `{session.get('cwd') or session['project']}`",
             f"- Started: {session.get('started')}  Last activity: {session.get('last_activity')}",
             f"- Exported from the Claude chat vault on {now_iso()}", ""]
    heads = {"user": "## You", "assistant": "## Claude"}
    prev = None
    for e in entries:
        role = e["role"]
        if role in heads and role != prev:
            lines += ["", heads[role], ""]
        if role == "user" and e.get("queued"):
            lines += ["*(sent while Claude was working)*", "", e["text"], ""]
        elif role in ("user", "assistant"):
            lines += [e["text"], ""]
        elif role == "tool_use":
            lines.append(f"> tool call - {e['text']}")
        elif role == "tool_result":
            body = e["text"].replace("```", "'''")
            lines += ["", "<details><summary>tool result</summary>", "", "```", body, "```", "",
                      "</details>", ""]
        else:
            lines += ["", f"*{e['text']}*", ""]
        prev = role if role in heads else prev
    return "\n".join(lines) + "\n"
