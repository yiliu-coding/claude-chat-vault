"""Self-test on a throwaway fake ~/.claude: python -m unittest discover -s tests -v"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ccvault import backup, common, discover, registry, restore, retention, schedule, sessions  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


def record(kind, **kw):
    return json.dumps(dict(type=kind, sessionId=SID, **kw)) + "\n"


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class VaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccvault-test-")
        self.claude = os.path.join(self.tmp, "claude")
        self.workdir = os.path.join(self.tmp, "My Repo_1")
        os.makedirs(self.workdir)
        self.project = common.encode_path(self.workdir)
        self.pdir = os.path.join(self.claude, "projects", self.project)
        os.makedirs(os.path.join(self.pdir, "memory"))
        self.transcript = os.path.join(self.pdir, SID + ".jsonl")
        with open(self.transcript, "w", encoding="utf-8") as fh:
            fh.write(record("user", cwd=self.workdir, timestamp="2026-09-01T10:00:00Z",
                            message={"role": "user", "content": "Train the model please"}))
            fh.write(record("assistant", cwd=self.workdir, timestamp="2026-09-01T10:00:05Z",
                            message={"id": "m1", "model": "claude-opus-5-5",
                                     "content": [{"type": "text", "text": "Done."}]}))
            fh.write(record("ai-title", aiTitle="Model training"))
        with open(os.path.join(self.pdir, "memory", "MEMORY.md"), "w") as fh:
            fh.write("- note\n")
        with open(os.path.join(self.claude, ".credentials.json"), "w") as fh:
            fh.write("secret")
        with open(os.path.join(self.claude, "settings.json"), "w") as fh:
            json.dump({"cleanupPeriodDays": 30}, fh)
        self.cfg = dict(common.DEFAULT_CONFIG, vault_dir=os.path.join(self.tmp, "vault"),
                        sources=[{"name": "t", "type": "local", "claude_dir": self.claude}],
                        include=["projects", "settings.json", ".credentials.json"],
                        vscode_storage=[], roots={"t": [self.tmp]})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_backup(self):
        return backup.run_backup(self.cfg, quiet=True)["sources"]["t"]

    def mirrored(self, *parts):
        return os.path.join(self.tmp, "vault", "mirror", "t", *parts)

    def test_incremental_versions_missing_restore(self):
        first = self.run_backup()
        self.assertEqual(first["new"], 3)                         # transcript, memory, settings
        self.assertFalse(os.path.exists(self.mirrored(".credentials.json")))

        self.assertEqual(self.run_backup()["unchanged"], 3)       # nothing copied again

        time.sleep(0.01)
        with open(self.transcript, "a", encoding="utf-8") as fh:  # normal growth
            fh.write(record("user", timestamp="2026-09-02T09:00:00Z",
                            message={"role": "user", "content": "And evaluate it"}))
        grown = self.run_backup()
        self.assertEqual((grown["updated"], grown.get("versioned", 0)), (1, 0))

        with open(self.transcript, "w", encoding="utf-8") as fh:  # rewritten / truncated
            fh.write(record("user", message={"role": "user", "content": "x"}))
        self.assertEqual(self.run_backup()["versioned"], 1)
        kept = os.listdir(os.path.join(self.tmp, "vault", "versions", "t", "projects", self.project))
        self.assertEqual(len(kept), 1)

        os.remove(self.transcript)                                # deleted by a cleanup
        gone = self.run_backup()
        self.assertEqual(gone["went_missing"], 1)
        self.assertTrue(os.path.exists(self.mirrored("projects", self.project, SID + ".jsonl")))
        cat = load(os.path.join(self.tmp, "vault", "catalog", "sessions.json"))
        s = cat["sessions"][0]
        self.assertFalse(s["in_source"])
        self.assertEqual(s["versions"], 1)

        res = restore.restore_session(self.cfg, SID)
        self.assertIn(f"projects/{self.project}/{SID}.jsonl", res["restored"])
        self.assertTrue(os.path.exists(self.transcript))
        again = restore.restore_session(self.cfg, SID)
        self.assertEqual(again["restored"], [])                   # never overwrites

        reg = load(os.path.join(self.tmp, "vault", "registry", "directories.json"))
        ent = next(iter(reg["directories"].values()))
        self.assertTrue(ent["strong"])
        self.assertIn(SID, ent["session_ids"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "vault", "INVENTORY.md")))

    def test_parse_meta(self):
        m = sessions.parse_meta(self.transcript)
        self.assertEqual((m["title"], m["n_prompts"], m["n_replies"]), ("Model training", 1, 1))
        self.assertEqual(m["first_prompt"], "Train the model please")
        self.assertEqual(m["models"], ["claude-opus-5-5"])
        md = sessions.to_markdown(dict(m, session_id=SID, source="t", project=self.project),
                                  sessions.render_entries(self.transcript))
        self.assertIn("## You", md)
        self.assertIn("Done.", md)

    def test_queued_prompts_are_shown(self):
        # a prompt typed while Claude was working arrives as an attachment, not as a "user" record
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(record("attachment", timestamp="2026-09-01T10:00:07Z", attachment={
                "type": "queued_command", "commandMode": "prompt", "origin": {"kind": "human"},
                "prompt": "Also plot the loss curve"}))
            fh.write(record("attachment", timestamp="2026-09-01T10:00:09Z", attachment={
                "type": "queued_command", "commandMode": "task-notification",
                "prompt": "<task-notification><task-id>b1</task-id></task-notification>"}))
            fh.write(record("attachment", timestamp="2026-09-01T10:00:10Z", attachment={
                "type": "queued_command", "isMeta": True, "origin": {"kind": "peer"},
                "prompt": "<agent-message from=\"a1\">hand-back</agent-message>"}))
        m = sessions.parse_meta(self.transcript)
        self.assertEqual(m["n_prompts"], 2)
        self.assertEqual(m["last_prompt"], "Also plot the loss curve")
        users = [e for e in sessions.render_entries(self.transcript) if e["role"] == "user"]
        self.assertEqual([e["text"] for e in users], ["Train the model please", "Also plot the loss curve"])
        self.assertTrue(users[1]["queued"])
        md = sessions.to_markdown(dict(m, session_id=SID, source="t", project=self.project),
                                  sessions.render_entries(self.transcript))
        self.assertIn("sent while Claude was working", md)
        self.assertNotIn("task-notification", md)

    def test_activity_times_and_live_view(self):
        # bookkeeping written into every open chat (e.g. when VS Code closes) is not activity
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(record("queue-operation", operation="dequeue", timestamp="2026-09-20T22:10:29Z"))
        m = sessions.parse_meta(self.transcript)
        self.assertEqual((m["started"], m["last_activity"]), ("2026-09-01T10:00:00Z", "2026-09-01T10:00:05Z"))
        self.assertEqual(m["last_record"], "2026-09-20T22:10:29Z")

        self.run_backup()
        cat = load(os.path.join(self.tmp, "vault", "catalog", "sessions.json"))["sessions"]
        self.assertTrue(cat[0]["backed_up"])
        self.assertEqual(sessions.live_view(self.cfg, [dict(cat[0])])[0]["pending_bytes"], 0)

        # the chat goes on after the backup: the live chat is newer than the vault copy
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(record("user", timestamp="2026-09-03T08:00:00Z",
                            message={"role": "user", "content": "Continue training"}))
        live = sessions.live_view(self.cfg, [dict(cat[0])])[0]
        self.assertEqual(live["last_activity"], "2026-09-03T08:00:00Z")
        self.assertEqual(live["vault_last_activity"], "2026-09-01T10:00:05Z")
        self.assertEqual((live["pending_prompts"], live["n_prompts"]), (1, 2))
        self.assertGreater(live["pending_bytes"], 0)

    def test_retention_change_keeps_other_settings(self):
        settings = os.path.join(self.claude, "settings.json")
        with open(settings, "w", encoding="utf-8") as fh:
            json.dump({"model": "opus", "cleanupPeriodDays": 30, "permissions": {"allow": ["Read"]}}, fh)
        st = retention.status(self.cfg, "t")
        self.assertEqual((st["value"], st["effective"], st["n_chats"]), (30, 30, 1))
        self.assertEqual(retention.would_delete(st, 1)["would_delete"], 0)
        res = retention.set_days(self.cfg, "t", 90)
        self.assertEqual((res["old"], res["new"]), (30, 90))
        self.assertEqual(load(settings), {"model": "opus", "cleanupPeriodDays": 90, "permissions": {"allow": ["Read"]}})
        self.assertEqual(load(res["backup"])["cleanupPeriodDays"], 30)
        with self.assertRaises(ValueError):
            retention.set_days(self.cfg, "t", 0)                  # 0 would stop Claude Code saving chats
        with open(settings, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        with self.assertRaises(ValueError):
            retention.set_days(self.cfg, "t", 365)                # a broken file is never rewritten
        with open(settings, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{ not json")

    def test_schedule_dry_run_and_roots(self):
        plan = schedule.install(self.cfg, "07:45", dry_run=True)
        self.assertTrue(plan["dry_run"])
        self.assertIn("vault.py", json.dumps(plan))
        with self.assertRaises(ValueError):
            schedule.install(self.cfg, "25:00", dry_run=True)
        home = os.path.expanduser("~")
        roots = discover.suggest_roots([os.path.join(home, "Work", "Repos", "a"), os.path.join(home, "Work", "Repos", "b"),
                                        os.path.join(home, "Notes", "x")])
        self.assertEqual(roots[0], "~/Work/Repos")

    def test_resolve_encoded_dir(self):
        found = registry.resolve_local([self.project])
        self.assertIn(os.path.normcase(os.path.normpath(self.workdir)),
                      [os.path.normcase(p) for p in found[self.project.lower()]])


if __name__ == "__main__":
    unittest.main()
