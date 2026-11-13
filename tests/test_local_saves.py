import json
import tempfile
import unittest
from pathlib import Path

import local_saves
from worker import WorkerError


class LocalSavesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.zomboid = Path(self.temp.name) / "Zomboid"
        self.mp = self.zomboid / "Saves" / "Multiplayer"
        (self.zomboid / "Server").mkdir(parents=True)
        for name in ("servertest", "servertest-copy", "beta"):
            (self.mp / name).mkdir(parents=True)
            (self.mp / name / "map_meta.bin").write_bytes(b"meta")
        (self.zomboid / "Server" / "servertest.ini").write_text("PVP=true\n")
        (self.zomboid / "Server" / "beta.ini").write_text("PVP=true\n")
        # Joined servers' caches; some real ones contain map_meta.bin too.
        cache = self.mp / "208.73.207.202_16261_3367705d39e2ce42f6452fafd517547c"
        cache.mkdir()
        (cache / "map_t.bin").write_bytes(b"t")
        cache_with_meta = self.mp / "99.8.42.84_16261_31a60c22b61f6cd5f3b0be8ef1dbb5b6"
        cache_with_meta.mkdir()
        (cache_with_meta / "map_meta.bin").write_bytes(b"meta")
        self.config = {"server": {"name": "servertest", "save_dir": str(self.mp / "servertest")}}

    def test_lists_hosted_saves_and_skips_joined_server_caches(self):
        saves = local_saves.hosted_saves(self.config)
        self.assertEqual([s["name"] for s in saves], ["beta", "servertest", "servertest-copy"])
        self.assertEqual({s["name"]: s["has_server_ini"] for s in saves},
                         {"beta": True, "servertest": True, "servertest-copy": False})

    def test_zomboid_dir_follows_the_configured_save(self):
        self.assertEqual(local_saves.zomboid_dir(self.config), self.zomboid)

    def test_select_save_updates_path_and_name(self):
        folder = local_saves.select_save(self.config, self.mp / "beta")
        self.assertEqual(self.config["server"]["save_dir"], str(folder))
        self.assertEqual(self.config["server"]["name"], "beta")

    def test_rejects_folders_that_are_not_hosted_saves(self):
        with self.assertRaises(WorkerError):
            local_saves.select_save(self.config, self.mp / "208.73.207.202_16261_3367705d39e2ce42f6452fafd517547c")
        with self.assertRaises(WorkerError):
            local_saves.select_save(self.config, self.mp / "99.8.42.84_16261_31a60c22b61f6cd5f3b0be8ef1dbb5b6")
        with self.assertRaises(WorkerError):
            local_saves.select_save(self.config, self.mp / "missing")
        self.assertEqual(self.config["server"]["name"], "servertest")

    def test_export_from_another_save_is_refused(self):
        self.assertIsNone(local_saves.export_save_mismatch({"saveName": "servertest"}, self.mp / "servertest"))
        self.assertIsNone(local_saves.export_save_mismatch({}, self.mp / "servertest"))  # older mod
        message = local_saves.export_save_mismatch({"saveName": "beta"}, self.mp / "servertest")
        self.assertIn("written by save 'beta'", message)


class LocalWorkerExportCheckTests(unittest.TestCase):
    def test_local_worker_refuses_an_export_from_another_save(self):
        import local_worker

        with tempfile.TemporaryDirectory() as folder:
            export_path = Path(folder) / "pz-reset-zones.json"
            export_path.write_text(json.dumps({
                "schemaVersion": 2, "build": "42.22.2", "saveName": "beta", "executionMode": "local",
                "generatedAt": "2026-11-13T00:00:00Z", "zones": [],
            }))
            config = {"server": {"save_dir": str(Path(folder) / "servertest")}, "world": {"build": "42.22.2"}}
            with self.assertRaises(WorkerError) as caught:
                local_worker._read_zone_export(export_path, config)
            self.assertIn("written by save 'beta'", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
