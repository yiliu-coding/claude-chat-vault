"""The daily automatic backup, using the operating system's own scheduler.

  Windows  Task Scheduler task (runs as you, only when you are logged on; catches up after a missed start)
  macOS    a LaunchAgent in ~/Library/LaunchAgents (runs at the next wake if the Mac was asleep)
  Linux    a line in your crontab, marked with "# claude-chat-vault"

Nothing needs administrator rights. `dry_run=True` returns what would be done without doing it.
"""

import base64
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys

from .common import IS_MAC, IS_WINDOWS, TOOL_DIR, vpath

LABEL = "com.claude-chat-vault.backup"
CRON_MARK = "# claude-chat-vault"


def _name(cfg):
    return (cfg.get("schedule") or {}).get("name") or "Claude chat vault backup"


def _check_time(value):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (value or "").strip())
    if not m:
        raise ValueError("time must look like 12:30 (24-hour clock)")
    return int(m.group(1)), int(m.group(2))


def _python(windowless=False):
    exe = sys.executable
    if windowless and IS_WINDOWS:
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            return w
    return exe


def _command():
    return [_python(windowless=True), os.path.join(TOOL_DIR, "vault.py"), "backup", "--quiet"]


def _powershell(script):
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    res = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                         capture_output=True, encoding="utf-8", errors="replace", timeout=60,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip().splitlines()[-1] if (res.stderr or res.stdout) else "PowerShell failed")
    return res.stdout


def _ps_quote(s):
    return "'" + s.replace("'", "''") + "'"


# ---------------------------------------------------------------------------------------------

def status(cfg):
    if IS_WINDOWS:
        out = _powershell(f"""
$t = Get-ScheduledTask -TaskName {_ps_quote(_name(cfg))} -ErrorAction SilentlyContinue
if ($t) {{
  $i = $t | Get-ScheduledTaskInfo
  [pscustomobject]@{{ installed = $true; state = [string]$t.State; next = [string]$i.NextRunTime;
    last = [string]$i.LastRunTime; last_result = $i.LastTaskResult;
    command = $t.Actions[0].Execute + ' ' + $t.Actions[0].Arguments;
    at = [string]$t.Triggers[0].StartBoundary }} | ConvertTo-Json -Compress
}} else {{ '{{"installed": false}}' }}""")
        st = json.loads(out.strip() or '{"installed": false}')
        if st.get("installed"):
            m = re.search(r"T(\d\d:\d\d)", st.get("at") or "")
            st["time"] = m.group(1) if m else None
            st["points_here"] = os.path.join(TOOL_DIR, "vault.py").lower() in st.get("command", "").lower()
        return dict(st, system="Windows Task Scheduler", name=_name(cfg))
    if IS_MAC:
        plist = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
        if not os.path.exists(plist):
            return {"installed": False, "system": "launchd", "name": LABEL}
        with open(plist, "rb") as fh:
            p = plistlib.load(fh)
        cal = p.get("StartCalendarInterval", {})
        return {"installed": True, "system": "launchd", "name": LABEL, "file": plist,
                "time": f"{cal.get('Hour', 0):02d}:{cal.get('Minute', 0):02d}",
                "points_here": os.path.join(TOOL_DIR, "vault.py") in p.get("ProgramArguments", [])}
    line = next((ln for ln in _crontab() if CRON_MARK in ln), None)
    if not line:
        return {"installed": False, "system": "cron", "name": CRON_MARK}
    minute, hour = line.split()[:2]
    return {"installed": True, "system": "cron", "name": CRON_MARK, "line": line,
            "time": f"{int(hour):02d}:{int(minute):02d}", "points_here": TOOL_DIR in line}


def install(cfg, time="12:30", dry_run=False):
    hour, minute = _check_time(time)
    cmd = _command()
    if IS_WINDOWS:
        script = f"""
$action = New-ScheduledTaskAction -Execute {_ps_quote(cmd[0])} -Argument {_ps_quote(subprocess.list2cmdline(cmd[1:]))} -WorkingDirectory {_ps_quote(TOOL_DIR)}
$trigger = New-ScheduledTaskTrigger -Daily -At {_ps_quote(f"{hour:02d}:{minute:02d}")}
# no UTC offset = local wall-clock time, so the run does not shift by an hour after a clock change
$trigger.StartBoundary = ([datetime]$trigger.StartBoundary).ToString('yyyy-MM-ddTHH:mm:ss')
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName {_ps_quote(_name(cfg))} -Action $action -Trigger $trigger -Settings $settings -Description {_ps_quote(f"Copies new and changed Claude Code chats into {vpath(cfg)}")} -Force | Out-Null"""
        if dry_run:
            return {"dry_run": True, "script": script}
        _powershell(script)
    elif IS_MAC:
        plist = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
        body = {"Label": LABEL, "ProgramArguments": cmd, "WorkingDirectory": TOOL_DIR,
                "StartCalendarInterval": {"Hour": hour, "Minute": minute},
                "StandardOutPath": vpath(cfg, "logs", "launchd.log"),
                "StandardErrorPath": vpath(cfg, "logs", "launchd.log")}
        if dry_run:
            return {"dry_run": True, "file": plist, "plist": plistlib.dumps(body).decode()}
        os.makedirs(os.path.dirname(plist), exist_ok=True)
        os.makedirs(vpath(cfg, "logs"), exist_ok=True)
        subprocess.run(["launchctl", "unload", plist], capture_output=True)
        with open(plist, "wb") as fh:
            plistlib.dump(body, fh)
        subprocess.run(["launchctl", "load", "-w", plist], check=True, capture_output=True)
    else:
        if not shutil.which("crontab"):
            raise RuntimeError("crontab is not installed; run `python3 vault.py backup` from your own scheduler")
        quoted = " ".join("'" + c.replace("'", "'\\''") + "'" for c in cmd)
        line = f"{minute} {hour} * * * cd '{TOOL_DIR}' && {quoted} {CRON_MARK}"
        lines = [ln for ln in _crontab() if CRON_MARK not in ln] + [line]
        if dry_run:
            return {"dry_run": True, "crontab": "\n".join(lines)}
        _write_crontab(lines)
    cfg.setdefault("schedule", {})["time"] = f"{hour:02d}:{minute:02d}"
    return status(cfg)


def remove(cfg, dry_run=False):
    if dry_run:
        return {"dry_run": True}
    if IS_WINDOWS:
        _powershell(f"Unregister-ScheduledTask -TaskName {_ps_quote(_name(cfg))} -Confirm:$false -ErrorAction SilentlyContinue")
    elif IS_MAC:
        plist = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
        if os.path.exists(plist):
            subprocess.run(["launchctl", "unload", plist], capture_output=True)
            os.remove(plist)
    else:
        _write_crontab([ln for ln in _crontab() if CRON_MARK not in ln])
    return status(cfg)


def _crontab():
    if not shutil.which("crontab"):
        return []
    res = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return res.stdout.splitlines() if res.returncode == 0 else []


def _write_crontab(lines):
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
