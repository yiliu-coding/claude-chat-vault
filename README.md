# Claude chat vault

Claude Code (in the terminal or in VS Code) keeps your chats in `~/.claude` and **deletes every chat
that has not been used for 30 days**, unless you change the `cleanupPeriodDays` setting. In VS Code
the deleted chats then show up as empty tabs. This tool:

1. **backs up your chats outside `~/.claude`**: a plain-file copy that is never pruned, updated
   incrementally (only new or changed files are copied), and documented so you can recover without
   the tool;
2. **shows and changes how long Claude Code keeps chats**: it finds the setting, warns you which
   chats a shorter period would delete, and keeps a copy of your settings before changing them;
3. **finds where you used Claude Code**: it scans this PC (Claude Code's folders, its log cache, and
   the workspace state of VS Code, Cursor, Windsurf and VSCodium) and keeps a permanent register of
   those folders;
4. **gives you a dashboard**: browse and search chats, read them, restore or export them, add notes,
   and manage all of the above from a browser page that runs only on your PC.

It needs only **Python 3.8 or newer**; nothing else to install.

**This is the Windows version** (branch `windows`): developed and tested on Windows 11 with Python
3.12. The code also contains macOS and Linux paths (folders, scheduler), but they are not tested in
this version. A macOS version may follow on its own branch.

## Get started

1. Install Python 3 if you do not have it (<https://www.python.org/downloads/>; on Windows tick
   "Add python.exe to PATH").
2. Put this folder somewhere permanent, e.g. `Documents/claude-chat-vault`. The backups are kept
   inside it, in `data/`.
3. Start it:
   - **Windows**: double-click `start.cmd`
   - **macOS / Linux**: open a terminal in this folder and run `bash start.sh`

The first start scans your PC, writes `config.json`, and opens the dashboard in your browser. The
dashboard tells you what it found. Press **Run the first backup**, then open **Settings & this PC** to:

- choose how long Claude Code keeps chats (for example 1 year instead of 30 days),
- turn on the **daily automatic backup**,
- switch on other places the scan found (a WSL distribution, or a server you use over VS Code
  Remote-SSH). These start switched off.

Keep the console window that `start` opens; closing it stops the dashboard (not the backups).
Next time, run `start.cmd` / `start.sh` again, or `dashboard.cmd`.

Prefer a terminal? `python vault.py setup` asks the same questions there.

## Everyday commands

Run them in this folder (`python3` instead of `python` on macOS/Linux).

| command | what it does |
|---|---|
| `python vault.py start` | first run: scan and configure; then open the dashboard |
| `python vault.py setup` | guided setup in the terminal (scan, config, retention, schedule, first backup) |
| `python vault.py backup` | copy new and changed chats into the vault now |
| `python vault.py status` | vault size, number of chats, how many exist only in the vault, last backup |
| `python vault.py retention` | show how long Claude Code keeps chats, and where that is set |
| `python vault.py retention --set 365` | keep chats for 365 days (asks first, shows what would be deleted) |
| `python vault.py schedule install --time 12:30` | daily automatic backup (`status`, `remove` also work) |
| `python vault.py info` | this PC's details, Claude Code folders and folders with Claude usage |
| `python vault.py list --missing` | chats that Claude Code deleted but the vault still has |
| `python vault.py restore --session <id>` | put one chat back into `~/.claude` (never overwrites) |
| `python vault.py restore --all-missing` | put back everything Claude Code deleted |
| `python vault.py export --session <id>` | save a chat as a readable Markdown file |
| `python vault.py serve` | open the dashboard |

## The dashboard

- **Chats**: summary tiles, chats per week (still in `~/.claude` vs. only in the vault), space per
  folder, the folder register and the chat list. Filters (search, source, status, period) apply to
  everything below them. Click a chat to read it, star it, tag it, write notes, copy its
  `claude --resume` command, export it, open its folder in VS Code, or restore it.
- **Settings & this PC**: chat retention with a live "what would be deleted" check, the daily
  backup, the sources being backed up (and the ones the scan found), this PC's details, and the
  vault folder.

## How long Claude Code keeps chats (`cleanupPeriodDays`)

The setting is in `~/.claude/settings.json` (or in `$CLAUDE_CONFIG_DIR/settings.json` if you set
that variable). When it is absent, chats are deleted 30 days after their last use. When you change
it here:

- the current settings file is first copied to `data/settings-backups/`;
- all your other settings are kept exactly; a file that is not valid JSON is never touched;
- values from 1 to 36500 days are accepted (0 is refused: Claude Code would stop saving chats);
- if your organisation fixes the value (managed settings), the tool shows that and does not change it;
- a shorter period shows how many existing chats Claude Code would delete at its next start, and how
  many of those are already in the vault.

## How the backup works

- Copies `projects/` (chats, subagents, tool output, project memory), `file-history/`, settings,
  `CLAUDE.md`, agents, commands, plans and todos from each source. **Login credentials
  (`.credentials.json`) are never copied.**
- Only files whose size or modification time changed are copied, so a normal day takes seconds.
- **Nothing is ever deleted from the vault.** When Claude Code removes a chat, the vault keeps it and
  marks it "only in the vault".
- If a file is rewritten or shrinks instead of growing, its previous state is kept in `data/versions/`.
- Every run is logged in `data/logs/`. `data/INVENTORY.md` lists every folder, chat and backup file,
  and `data/README.md` explains how to recover by hand.

Sources are listed in `config.json`:

| type | what | example |
|---|---|---|
| `local` | a folder on this PC; `"claude_dir": "auto"` = your own `~/.claude` | a WSL distribution: `\\wsl.localhost\Ubuntu\home\me\.claude` |
| `ssh` | `~/.claude` on a server, read over ssh with your existing key (no password prompts) | `{"name": "lab", "type": "ssh", "host": "lab-server"}` |

## The folder register

`data/registry/directories.json` remembers every folder where Claude Code was used, with when and
how it was seen:

| evidence | where it comes from |
|---|---|
| transcripts | the working folder recorded inside a backed-up chat |
| ~/.claude folder | Claude Code's project folder for that directory, now or in the vault |
| Claude log cache | Claude Code's per-project log cache |
| running session | a Claude Code process running in that folder |
| VS Code chat tabs / Claude state | the Claude extension's saved state in VS Code (or Cursor, Windsurf, VSCodium) |
| VS Code views only | weak: the folder was opened while the extension was installed |

Entries are never removed, so a folder stays on record even after its chats are gone.

## Privacy and safety

- **The vault contains your full chats**: code, file contents, and anything you pasted, possibly
  including passwords or keys. Keep the folder private. Do not put it in a shared or public location,
  and do not commit `data/` or `config.json` (the included `.gitignore` excludes both).
- Nothing is uploaded anywhere. The scan, the backups and the dashboard run on your PC; ssh sources
  are contacted only if you switch them on.
- The dashboard listens only on `127.0.0.1`. Each start creates a new key, carried in the link that
  is opened for you; requests without it are refused, so other web pages cannot use it.
- Restoring never overwrites an existing file. Changing retention or the schedule always asks first.
- The scheduler runs as you, without administrator rights: Windows Task Scheduler, a macOS
  LaunchAgent, or your Linux crontab (a line marked `# claude-chat-vault`).
- The vault is on the same disk as `~/.claude`, so it protects against Claude Code's cleanup and
  edits, not against losing the disk. Copy `data/` to another disk now and then.

## Recovering chats

1. In the dashboard, open the chat and press **Restore to ~/.claude**, or
2. run `python vault.py restore --session <id>` (or `--all-missing`), or
3. without this tool, follow `data/README.md`: copy the chat's `.jsonl` file back into
   `~/.claude/projects/<folder>/`.

Then reopen the folder in VS Code (the chat appears in the Claude Code chat list) or run
`claude --resume <id>` inside it.

## Troubleshooting

| problem | fix |
|---|---|
| `python` is not found | install Python 3 (see above); on macOS/Linux use `python3` |
| the browser does not open | copy the link printed in the console window into your browser |
| "key does not match" in the dashboard | close any older dashboard windows, then start again; each start makes a new link |
| "Open in VS Code" fails | in VS Code run the command *Shell Command: Install 'code' command in PATH* |
| an ssh source is "unreachable" | it is skipped and retried next time; check you can `ssh <host>` without a password |
| the daily backup does not run | `python vault.py schedule status`; on macOS check *Login Items & Extensions* allows Python |

## Files

```
start.cmd, start.sh     first run and dashboard          vault.py     command line
dashboard.cmd           open the dashboard (Windows)     backup.cmd   one backup (Windows)
ccvault/                the code                         dashboard/   the dashboard page
tests/                  self-test: python -m unittest discover -s tests
config.json             your settings (created by start/setup, private)
data/                   the vault (created by the first backup, private)
```
