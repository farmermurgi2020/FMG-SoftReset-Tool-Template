import errno
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import paramiko

import hard_reset_remote as remote
from worker import WorkerError


class FakeSftp:
    """SFTP client backed by a local folder; remote '/x' maps to <root>/x."""

    def __init__(self, root: Path):
        self.root = root

    def _p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def listdir_attr(self, path):
        entries = []
        for child in sorted(self._p(path).iterdir()):
            attributes = paramiko.SFTPAttributes.from_stat(child.stat())
            attributes.filename = child.name
            entries.append(attributes)
        return entries

    def stat(self, path):
        target = self._p(path)
        if not target.exists():
            raise FileNotFoundError(errno.ENOENT, "No such file", path)
        return paramiko.SFTPAttributes.from_stat(target.stat())

    def rename(self, source, target):
        os.rename(self._p(source), self._p(target))

    def mkdir(self, path):
        self._p(path).mkdir()

    def rmdir(self, path):
        self._p(path).rmdir()

    def getfo(self, path, handle, **_kwargs):
        handle.write(self._p(path).read_bytes())

    def putfo(self, handle, path, file_size=0, confirm=True):
        self._p(path).write_bytes(handle.read())

    def get(self, path, local_path):
        Path(local_path).write_bytes(self._p(path).read_bytes())

    def remove(self, path):
        self._p(path).unlink()

    def close(self):
        pass


class FakeClient:
    def close(self):
        pass


SAVE = "/.cache/Saves/Multiplayer/Thunder"


class RemoteHardResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.save = root / SAVE.lstrip("/")
        self.save.mkdir(parents=True)
        (root / ".cache" / "Lua").mkdir()
        with closing(sqlite3.connect(self.save / "players.db")) as connection:
            connection.execute("create table networkPlayers (id integer)")
            connection.executemany("insert into networkPlayers values (?)", [(1,), (2,), (3,)])
            connection.commit()
        for name in ("reanimated.bin", "map_t.bin", "erosion.ini", "map_meta.bin", "vehicles.db", "global_mod_data.bin"):
            (self.save / name).write_bytes(b"data-" + name.encode())
        (self.save / "map_visited_server").mkdir()
        (self.save / "map_visited_server" / "Player.zip").write_bytes(b"zip")
        (self.save / "radio" / "data").mkdir(parents=True)
        (self.save / "radio" / "data" / "RADIO_SAVE.txt").write_text("DaysSinceStart = 40\n")
        (root / ".cache" / "Lua" / "pz-reset-zones.json").write_text(json.dumps({"zones": [{"source": "safehouse"}]}))
        self.note = root / ".cache" / "Lua" / "fmg-hard-reset-pending.json"
        self.config = {
            "server": {"name": "Thunder", "connection_mode": "sftp", "save_dir": SAVE,
                       "zone_export": "/.cache/Lua/pz-reset-zones.json"},
            "world": {"build": "42.22.2"},
        }
        self.sftp = FakeSftp(root)
        patches = [
            patch.object(remote, "_connect", lambda _config: (FakeClient(), self.sftp)),
            patch.object(remote, "_save_plan", lambda plan: setattr(self, "saved_plan", plan)),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_apply_moves_player_files_over_sftp(self):
        plan = remote.run_hard_reset(self.config)
        self.assertEqual(plan["characters"], 3)
        self.assertEqual(plan["safehouses"], 1)
        rollback = self.sftp._p(plan["remote_rollback_dir"])
        for name in ("players.db", "reanimated.bin", "map_visited_server", "map_t.bin", "erosion.ini", "radio"):
            self.assertFalse((self.save / name).exists(), name)
            self.assertTrue((rollback / name).exists(), name)
        self.assertTrue((self.save / "map_meta.bin").exists())
        self.assertTrue((self.save / "vehicles.db").exists())
        self.assertEqual((rollback / "map_meta.bin").read_bytes(), b"data-map_meta.bin")
        self.assertEqual((rollback / "global_mod_data.bin").read_bytes(), b"data-global_mod_data.bin")
        self.assertTrue((self.save / "global_mod_data.bin").exists())
        note = json.loads(self.note.read_text())
        self.assertEqual(note["status"], "pending")
        self.assertIn("applied_at", self.saved_plan)

    def test_failed_move_puts_files_back(self):
        real_rename = FakeSftp.rename
        calls = []

        def flaky(sftp, source, target):
            calls.append(source)
            if len(calls) == 3:
                raise OSError("connection reset")
            return real_rename(sftp, source, target)

        with patch.object(FakeSftp, "rename", flaky):
            with self.assertRaises(WorkerError):
                remote.run_hard_reset(self.config)
        for name in ("players.db", "reanimated.bin", "map_visited_server", "map_t.bin"):
            self.assertTrue((self.save / name).exists(), name)
        self.assertEqual(list(self.save.glob("hardreset.rollback-*")), [])
        self.assertFalse(self.note.exists())

    def test_undo_restores_and_cancels_note(self):
        with patch.object(remote, "_timestamp", lambda: "20000101T000000Z"):
            remote.run_hard_reset(self.config)
        (self.save / "players.db").write_bytes(b"new characters")
        (self.save / "map_meta.bin").write_bytes(b"meta without safehouses")
        (self.save / "global_mod_data.bin").write_bytes(b"knox claims cleared")
        with patch.object(remote, "_timestamp", lambda: "20000102T000000Z"):
            plan = remote.run_hard_reset_undo(self.config)
        self.assertEqual(plan["restored_from"], "hardreset.rollback-20000101T000000Z")
        self.assertEqual((self.save / "map_meta.bin").read_bytes(), b"data-map_meta.bin")
        self.assertEqual((self.save / "global_mod_data.bin").read_bytes(), b"data-global_mod_data.bin")
        self.assertEqual((self.save / "radio" / "data" / "RADIO_SAVE.txt").read_text(), "DaysSinceStart = 40\n")
        after = self.save / "hardreset.rollback-20000102T000000Z"
        self.assertEqual((after / "global_mod_data.bin").read_bytes(), b"knox claims cleared")
        self.assertEqual((after / "players.db").read_bytes(), b"new characters")
        self.assertFalse((self.save / "hardreset.rollback-20000101T000000Z").exists())
        self.assertEqual(json.loads(self.note.read_text())["status"], "cancelled")

    def test_older_hard_reset_rollbacks_are_pruned(self):
        with patch.object(remote, "_timestamp", lambda: "20000101T000000Z"):
            remote.run_hard_reset(self.config)
        (self.save / "map_t.bin").write_bytes(b"day 2")
        with patch.object(remote, "_timestamp", lambda: "20000102T000000Z"):
            remote.run_hard_reset(self.config)
        self.assertEqual([p.name for p in self.save.glob("hardreset.rollback-*")],
                         ["hardreset.rollback-20000102T000000Z"])

    def test_main_requires_exact_server_name(self):
        with patch.object(remote, "load_config", lambda: self.config):
            self.assertEqual(remote.main(["--apply", "--confirm-server-name", "wrong"]), 1)
        self.assertTrue((self.save / "players.db").exists())

    def test_note_names_the_save_folder(self):
        remote.run_hard_reset(self.config)
        self.assertEqual(json.loads(self.note.read_text())["saveName"], "Thunder")

    def test_undo_keeps_live_mod_data_when_rollback_has_no_copy(self):
        with patch.object(remote, "_timestamp", lambda: "20000101T000000Z"):
            plan = remote.run_hard_reset(self.config)
        (self.sftp._p(plan["remote_rollback_dir"]) / "global_mod_data.bin").unlink()
        (self.save / "global_mod_data.bin").write_bytes(b"every mod's data")
        with patch.object(remote, "_timestamp", lambda: "20000102T000000Z"):
            remote.run_hard_reset_undo(self.config)
        self.assertEqual((self.save / "global_mod_data.bin").read_bytes(), b"every mod's data")

    def test_summary_save_failure_does_not_fail_a_finished_undo(self):
        with patch.object(remote, "_timestamp", lambda: "20000101T000000Z"):
            remote.run_hard_reset(self.config)

        def broken(_plan):
            raise WorkerError("disk full")

        with patch.object(remote, "_save_plan", broken),                 patch.object(remote, "_timestamp", lambda: "20000102T000000Z"):
            plan = remote.run_hard_reset_undo(self.config)
        self.assertTrue((self.save / "players.db").exists())
        self.assertEqual(json.loads(self.note.read_text())["status"], "cancelled")
        self.assertIn("restored_from", plan)


class DedicatedWiringTests(unittest.TestCase):
    def test_workflow_offers_hard_reset_actions(self):
        from dedicated_web_ui import DISPATCH_LABELS, _workflow_text

        text = _workflow_text({"enabled": False})
        self.assertIn("          - hard-reset\n          - hard-reset-undo\n", text)
        self.assertEqual(DISPATCH_LABELS["hard-reset"], "Hard reset (player data)")

    def test_summary_lists_moved_files(self):
        import dedicated_workflow

        plan = {"server": "Thunder", "characters": 3, "safehouses": 1,
                "items": [{"name": "players.db", "label": "Characters", "files": 1}],
                "remote_rollback_dir": "/save/hardreset.rollback-x"}
        lines = "\n".join(dedicated_workflow._hard_reset_summary("hard-reset", plan, True))
        self.assertIn("hard reset applied", lines)
        self.assertIn("Characters removed: 3", lines)
        self.assertIn("`players.db`", lines)
        failed = "\n".join(dedicated_workflow._hard_reset_summary("hard-reset-undo", {}, False))
        self.assertIn("undo FAILED", failed)


if __name__ == "__main__":
    unittest.main()
