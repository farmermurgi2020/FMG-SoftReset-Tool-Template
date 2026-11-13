import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import hard_reset
from worker import WorkerError


def _make_save(root: Path) -> tuple[dict, Path]:
    save = root / "Saves" / "Multiplayer" / "servertest"
    save.mkdir(parents=True)
    lua = root / "Lua"
    lua.mkdir()
    with closing(sqlite3.connect(save / "players.db")) as connection:
        connection.execute("create table networkPlayers (id integer, username text)")
        connection.executemany("insert into networkPlayers values (?, ?)", [(1, "a"), (2, "b")])
        connection.commit()
    for name in ("reanimated.bin", "servermap_symbols.bin", "map_t.bin", "erosion.ini", "map_meta.bin",
                 "vehicles.db", "global_mod_data.bin"):
        (save / name).write_bytes(b"data-" + name.encode())
    (save / "map_visited_server").mkdir()
    (save / "map_visited_server" / "Player One.zip").write_bytes(b"zip")
    (save / "radio" / "data").mkdir(parents=True)
    (save / "radio" / "data" / "RADIO_SAVE.txt").write_text("DaysSinceStart = 12\n")
    (save / "map").mkdir()
    (save / "map" / "1").mkdir()
    (save / "map" / "1" / "1.bin").write_bytes(b"chunk")
    (lua / "pz-reset-zones.json").write_text(json.dumps({"zones": [
        {"source": "safehouse"}, {"source": "tool"}, {"source": "safehouse"}]}))
    config = {"server": {"name": "servertest", "connection_mode": "local", "save_dir": str(save),
                         "zone_export": str(lua / "pz-reset-zones.json")}}
    return config, save


class HardResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config, self.save = _make_save(Path(self.temp.name))
        patcher = patch.object(hard_reset, "_assert_game_closed", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)

    def test_plan_counts_and_changes_nothing(self):
        plan = hard_reset.plan_hard_reset(self.config)
        self.assertEqual(plan["characters"], 2)
        self.assertEqual(plan["safehouses"], 2)
        names = [item["name"] for item in plan["items"]]
        self.assertEqual(names, ["players.db", "reanimated.bin", "map_visited_server", "servermap_symbols.bin",
                                 "map_t.bin", "erosion.ini", "radio"])
        self.assertTrue((self.save / "players.db").exists())

    def test_apply_moves_player_files_and_leaves_world_data(self):
        plan = hard_reset.run_hard_reset(self.config, True, str(self.save))
        rollback = Path(plan["rollback_dir"])
        for name in ("players.db", "reanimated.bin", "map_visited_server", "servermap_symbols.bin",
                     "map_t.bin", "erosion.ini", "radio"):
            self.assertFalse((self.save / name).exists(), name)
            self.assertTrue((rollback / name).exists(), name)
        # World data stays; map_meta.bin is only copied (safehouses are removed in game).
        for name in ("map_meta.bin", "vehicles.db", "global_mod_data.bin", "map/1/1.bin"):
            self.assertTrue((self.save / name).exists(), name)
        self.assertEqual((rollback / "map_meta.bin").read_bytes(), b"data-map_meta.bin")
        self.assertEqual((rollback / "global_mod_data.bin").read_bytes(), b"data-global_mod_data.bin")
        self.assertTrue((self.save / "global_mod_data.bin").exists())
        note = hard_reset.read_pending(self.config)
        self.assertEqual(note["status"], "pending")
        self.assertEqual(note["rollback"], rollback.name)

    def test_apply_requires_confirmations(self):
        with self.assertRaises(WorkerError):
            hard_reset.run_hard_reset(self.config, False, str(self.save))
        with self.assertRaises(WorkerError):
            hard_reset.run_hard_reset(self.config, True, None)
        self.assertTrue((self.save / "players.db").exists())

    def test_failed_move_puts_everything_back(self):
        real_replace = hard_reset.os.replace
        calls = []

        def flaky(source, target):
            calls.append(source)
            if len(calls) == 3:
                raise OSError("disk hiccup")
            return real_replace(source, target)

        with patch.object(hard_reset.os, "replace", flaky):
            with self.assertRaises(WorkerError):
                hard_reset.run_hard_reset(self.config, True, str(self.save))
        for name in ("players.db", "reanimated.bin", "map_visited_server", "map_t.bin"):
            self.assertTrue((self.save / name).exists(), name)
        self.assertEqual(list(self.save.glob("hardreset.rollback-*")), [])
        self.assertIsNone(hard_reset.read_pending(self.config))

    def test_undo_restores_and_cancels_pending_note(self):
        plan = hard_reset.run_hard_reset(self.config, True, str(self.save))
        # The game recreated a fresh players.db and changed map_meta.bin after the reset.
        (self.save / "players.db").write_bytes(b"new characters")
        (self.save / "map_meta.bin").write_bytes(b"meta without safehouses")
        (self.save / "global_mod_data.bin").write_bytes(b"knox claims cleared")
        with patch.object(hard_reset, "_timestamp", lambda: "20991231T000000Z"):
            after = hard_reset.run_hard_reset_rollback(self.config, True, str(self.save))
        self.assertFalse(Path(plan["rollback_dir"]).exists())
        self.assertEqual((self.save / "map_meta.bin").read_bytes(), b"data-map_meta.bin")
        self.assertEqual((self.save / "global_mod_data.bin").read_bytes(), b"data-global_mod_data.bin")
        self.assertEqual((self.save / "radio" / "data" / "RADIO_SAVE.txt").read_text(), "DaysSinceStart = 12\n")
        with closing(sqlite3.connect(self.save / "players.db")) as connection:
            self.assertEqual(connection.execute("select count(*) from networkPlayers").fetchone()[0], 2)
        # The replaced state is kept, so the undo can be undone.
        self.assertEqual((after / "players.db").read_bytes(), b"new characters")
        self.assertEqual((after / "map_meta.bin").read_bytes(), b"meta without safehouses")
        self.assertEqual(hard_reset.read_pending(self.config)["status"], "cancelled")

    def test_only_newest_rollback_is_kept(self):
        with patch.object(hard_reset, "_timestamp", lambda: "20000101T000000Z"):
            hard_reset.run_hard_reset(self.config, True, str(self.save))
        (self.save / "map_t.bin").write_bytes(b"day 3")
        with patch.object(hard_reset, "_timestamp", lambda: "20000102T000000Z"):
            hard_reset.run_hard_reset(self.config, True, str(self.save))
        names = sorted(path.name for path in self.save.glob("hardreset.rollback-*"))
        self.assertEqual(names, ["hardreset.rollback-20000102T000000Z"])

    def test_soft_reset_rollback_pruning_ignores_hard_reset_folders(self):
        import local_worker

        hard_reset.run_hard_reset(self.config, True, str(self.save))
        keep = self.save / "softreset.rollback-20990101T000000Z"
        keep.mkdir()
        local_worker._prune_older_rollback_sets(self.save, keep)
        self.assertEqual(len(list(self.save.glob("hardreset.rollback-*"))), 1)


    def test_note_names_the_save(self):
        hard_reset.run_hard_reset(self.config, True, str(self.save))
        self.assertEqual(hard_reset.read_pending(self.config)["saveName"], "servertest")

    def test_undo_keeps_live_mod_data_when_rollback_has_no_copy(self):
        with patch.object(hard_reset, "_timestamp", lambda: "20000101T000000Z"):
            plan = hard_reset.run_hard_reset(self.config, True, str(self.save))
        # A rollback made before global_mod_data.bin was copied (mod 0.4.0) has no copy of it.
        (Path(plan["rollback_dir"]) / "global_mod_data.bin").unlink()
        (self.save / "global_mod_data.bin").write_bytes(b"every mod's data")
        with patch.object(hard_reset, "_timestamp", lambda: "20000102T000000Z"):
            hard_reset.run_hard_reset_rollback(self.config, True, str(self.save))
        self.assertEqual((self.save / "global_mod_data.bin").read_bytes(), b"every mod's data")

    def test_undoing_an_undo_schedules_the_in_game_part_again(self):
        with patch.object(hard_reset, "_timestamp", lambda: "20000101T000000Z"):
            hard_reset.run_hard_reset(self.config, True, str(self.save))
        with patch.object(hard_reset, "_timestamp", lambda: "20000102T000000Z"):
            hard_reset.run_hard_reset_rollback(self.config, True, str(self.save))
        self.assertEqual(hard_reset.read_pending(self.config)["status"], "cancelled")
        with patch.object(hard_reset, "_timestamp", lambda: "20000103T000000Z"):
            hard_reset.run_hard_reset_rollback(self.config, True, str(self.save))
        self.assertEqual(hard_reset.read_pending(self.config)["status"], "pending")
        self.assertFalse((self.save / "players.db").exists())
        self.assertFalse((self.save / "map_t.bin").exists())

    def test_note_status_after_undo(self):
        self.assertEqual(hard_reset.note_status_after_undo("pending"), "cancelled")
        self.assertEqual(hard_reset.note_status_after_undo("cancelled"), "pending")
        self.assertIsNone(hard_reset.note_status_after_undo("done"))
        self.assertIsNone(hard_reset.note_status_after_undo(None))

if __name__ == "__main__":
    unittest.main()
