import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import paramiko

import worker


class _RemoteFile:
    def __init__(self, path: Path, mode: str):
        self._handle = open(path, mode)

    def prefetch(self):
        pass

    def read(self, size=-1):
        return self._handle.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._handle.close()


class FakeSFTP:
    """Maps absolute POSIX paths onto a temporary directory."""

    def __init__(self, root: Path, fail_rename_to=None):
        self.root = root
        self.fail_rename_to = fail_rename_to

    def _local(self, path: str) -> Path:
        assert path.startswith("/")
        return self.root / path.lstrip("/")

    def stat(self, path):
        return paramiko.SFTPAttributes.from_stat(os.stat(self._local(path)))

    def listdir_attr(self, path):
        directory = self._local(path)
        return [
            paramiko.SFTPAttributes.from_stat(os.stat(directory / name), filename=name)
            for name in sorted(os.listdir(directory))
        ]

    def open(self, path, mode="r"):
        return _RemoteFile(self._local(path), mode)

    def getfo(self, path, handle, prefetch=True, max_concurrent_prefetch_requests=None):
        data = self._local(path).read_bytes()
        handle.write(data)
        return len(data)

    def putfo(self, handle, path, file_size=0, confirm=True):
        data = handle.read()
        self._local(path).write_bytes(data)
        if confirm:
            assert self._local(path).stat().st_size == file_size

    def mkdir(self, path):
        self._local(path).mkdir()

    def rmdir(self, path):
        self._local(path).rmdir()

    def remove(self, path):
        self._local(path).unlink()

    def rename(self, source, target):
        if self.fail_rename_to and target == self.fail_rename_to:
            self.fail_rename_to = None  # fail once; the recovery rename must succeed
            raise OSError("simulated rename failure")
        os.rename(self._local(source), self._local(target))

    def get_channel(self):
        return self

    def settimeout(self, seconds):
        pass

    def close(self):
        pass


class _Client:
    def __init__(self, root: Path):
        self.root = root

    def open_sftp(self):
        return FakeSFTP(self.root)

    def close(self):
        pass


class RemoteWorkerTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.save = self.root / "save"
        (self.save / "map" / "0").mkdir(parents=True)
        (self.save / "map" / "1").mkdir()
        (self.save / "map" / "0" / "0.bin").write_bytes(b"protected")
        (self.save / "map" / "1" / "0.bin").write_bytes(b"reset-me")
        old = self.save / "softreset.rollback-20200101T000000Z" / "map" / "5"
        old.mkdir(parents=True)
        (old / "5.bin").write_bytes(b"old")
        (self.root / "zones.json").write_text(json.dumps({
            "schemaVersion": 1,
            "build": "42.22.2",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "zones": [{"x": 0, "y": 0, "w": 8, "h": 8}],
        }), encoding="utf-8")
        self.config = {
            "server": {"connection_mode": "sftp", "name": "fixture", "save_dir": "/save", "zone_export": "/zones.json"},
            "world": {"build": "42.22.2", "chunk_size": 8},
            "reset_targets": {"map_chunks": True, "zombie_population": False, "animal_population": False},
            "schedule": {},
            "safety": {},
        }

    def tearDown(self):
        self._temporary.cleanup()

    def _rollback_sets(self):
        return sorted(p.name for p in self.save.iterdir() if p.name.startswith("softreset.rollback-"))

    def _run(self, function, *args, sftp=None, **kwargs):
        sftp = sftp or FakeSFTP(self.root)
        with patch("worker._connect", return_value=(_Client(self.root), sftp)), patch("worker._save_plan"):
            return function(*args, **kwargs)

    def test_apply_then_rollback_keeps_a_single_rollback_set(self):
        plan = self._run(worker.run_apply, self.config)

        self.assertEqual(plan["protected_count"], 1)
        self.assertEqual(plan["remove_count"], 1)
        self.assertEqual((self.save / "map" / "0" / "0.bin").read_bytes(), b"protected")
        self.assertFalse((self.save / "map" / "1").exists())
        sets = self._rollback_sets()
        self.assertEqual(len(sets), 1, "older rollback sets must be pruned")
        self.assertEqual((self.save / sets[0] / "map" / "1" / "0.bin").read_bytes(), b"reset-me")
        self.assertFalse(any(p.name.startswith(".map.pz-reset-staging-") for p in self.save.iterdir()))

        time.sleep(1.1)  # rollback folders are named by the second
        result = self._run(worker.run_rollback, self.config)

        self.assertEqual((self.save / "map" / "1" / "0.bin").read_bytes(), b"reset-me")
        self.assertEqual(result["restored_folders"], ["map"])
        sets = self._rollback_sets()
        self.assertEqual(len(sets), 1)
        # The post-reset state is now the rollback set, so the rollback can be undone.
        self.assertFalse((self.save / sets[0] / "map" / "1").exists())
        self.assertTrue((self.save / sets[0] / "map" / "0" / "0.bin").is_file())

    def test_population_apply_and_rollback(self):
        for folder in ("zpop", "apop"):
            (self.save / folder).mkdir()
            (self.save / folder / f"{folder}_0_0.bin").write_bytes(b"protected-cell")
            (self.save / folder / f"{folder}_1_0.bin").write_bytes(b"reset-cell")
        (self.save / "zpop" / "zpop_ids.bin").write_bytes(b"shared")
        self.config["reset_targets"] = {"map_chunks": True, "zombie_population": True, "animal_population": True}

        plan = self._run(worker.run_apply, self.config)

        self.assertEqual(plan["target_counts"]["zombie_population"]["reset"], 1)
        self.assertEqual(plan["target_counts"]["animal_population"]["preserved"], 1)
        self.assertEqual(sorted(p.name for p in (self.save / "zpop").iterdir()), ["zpop_0_0.bin", "zpop_ids.bin"])
        self.assertEqual(sorted(p.name for p in (self.save / "apop").iterdir()), ["apop_0_0.bin"])
        rollback = self.save / self._rollback_sets()[0]
        self.assertEqual(sorted(p.name for p in rollback.iterdir()), ["apop", "map", "zpop"])
        self.assertTrue((rollback / "zpop" / "zpop_1_0.bin").is_file())

        time.sleep(1.1)  # rollback folders are named by the second
        result = self._run(worker.run_rollback, self.config)

        self.assertEqual(result["restored_folders"], ["apop", "map", "zpop"])
        self.assertEqual((self.save / "zpop" / "zpop_1_0.bin").read_bytes(), b"reset-cell")
        self.assertEqual((self.save / "apop" / "apop_1_0.bin").read_bytes(), b"reset-cell")
        self.assertEqual((self.save / "map" / "1" / "0.bin").read_bytes(), b"reset-me")

    def test_population_only_leaves_map_untouched(self):
        (self.save / "zpop").mkdir()
        (self.save / "zpop" / "zpop_0_0.bin").write_bytes(b"keep")
        (self.save / "zpop" / "zpop_3_3.bin").write_bytes(b"reset")
        self.config["reset_targets"] = {"map_chunks": False, "zombie_population": True, "animal_population": False}
        self._run(worker.run_apply, self.config)
        self.assertEqual(sorted(p.name for p in (self.save / "zpop").iterdir()), ["zpop_0_0.bin"])
        self.assertTrue((self.save / "map" / "1" / "0.bin").is_file())
        rollback = self.save / self._rollback_sets()[0]
        self.assertEqual([p.name for p in rollback.iterdir()], ["zpop"])

    def _set_modes(self, **modes):
        self.config["dedicated_settings"] = {"enabled": True, "target_modes": modes}

    def test_reset_all_map_ignores_zones_and_can_roll_back_twice(self):
        (self.save / "zpop").mkdir()
        (self.save / "zpop" / "zpop_0_0.bin").write_bytes(b"protected-cell")
        (self.save / "zpop" / "zpop_1_0.bin").write_bytes(b"reset-cell")
        self._set_modes(map_chunks="reset_all", zombie_population="protect", animal_population="skip")

        plan = self._run(worker.run_apply, self.config)

        self.assertEqual(plan["target_counts"]["map_chunks"]["reset"], 2)
        self.assertEqual(list((self.save / "map").iterdir()), [], "a full map reset leaves an empty map folder")
        self.assertEqual(sorted(p.name for p in (self.save / "zpop").iterdir()), ["zpop_0_0.bin"])
        self.assertFalse((self.save / "apop").exists())

        time.sleep(1.1)
        self._run(worker.run_rollback, self.config)
        self.assertEqual((self.save / "map" / "0" / "0.bin").read_bytes(), b"protected")
        self.assertEqual((self.save / "map" / "1" / "0.bin").read_bytes(), b"reset-me")

        time.sleep(1.1)  # rolling back again restores the empty (reset) map
        self._run(worker.run_rollback, self.config)
        self.assertEqual(list((self.save / "map").iterdir()), [])

    def test_everything_reset_all_needs_no_protected_zones(self):
        (self.root / "zones.json").write_text(json.dumps({
            "schemaVersion": 1, "build": "42.22.2",
            "generatedAt": datetime.now(timezone.utc).isoformat(), "zones": [],
        }), encoding="utf-8")
        self._set_modes(map_chunks="reset_all", zombie_population="skip", animal_population="skip")
        plan = self._run(worker.run_apply, self.config)
        self.assertEqual(plan["target_counts"]["map_chunks"]["preserved"], 0)
        self.assertEqual(list((self.save / "map").iterdir()), [])

    def test_protect_mode_still_refuses_without_zones(self):
        (self.root / "zones.json").write_text(json.dumps({
            "schemaVersion": 1, "build": "42.22.2",
            "generatedAt": datetime.now(timezone.utc).isoformat(), "zones": [],
        }), encoding="utf-8")
        self._set_modes(map_chunks="protect", zombie_population="skip", animal_population="skip")
        with self.assertRaises(worker.WorkerError):
            self._run(worker.run_apply, self.config)
        self.assertTrue((self.save / "map" / "1" / "0.bin").is_file())

    def test_leftover_staging_folder_is_removed(self):
        stale = self.save / ".zpop.pz-reset-staging-20200101T000000Z"
        stale.mkdir()
        (stale / "zpop_ids.bin").write_bytes(b"partial")
        self._run(worker.run_apply, self.config)
        self.assertFalse(stale.exists())

    def test_all_skip_is_refused(self):
        self._set_modes(map_chunks="skip", zombie_population="skip", animal_population="skip")
        with self.assertRaises(worker.WorkerError):
            self._run(worker.run_apply, self.config)

    def test_failed_swap_restores_original_map(self):
        sftp = FakeSFTP(self.root, fail_rename_to="/save/map")
        with self.assertRaises(worker.WorkerError):
            self._run(worker.run_apply, self.config, sftp=sftp)
        self.assertEqual((self.save / "map" / "1" / "0.bin").read_bytes(), b"reset-me")
        self.assertEqual((self.save / "map" / "0" / "0.bin").read_bytes(), b"protected")
        self.assertEqual(self._rollback_sets(), ["softreset.rollback-20200101T000000Z"])
        self.assertFalse(any(p.name.startswith(".map.pz-reset-staging-") for p in self.save.iterdir()))

    def test_parallel_delete_removes_whole_tree(self):
        tree = self.root / "big"
        for x in range(5):
            (tree / str(x)).mkdir(parents=True)
            for y in range(40):
                (tree / str(x) / f"{y}.bin").write_bytes(b"x")
        with patch("worker._connect", side_effect=lambda config: (_Client(self.root), FakeSFTP(self.root))):
            removed = worker._remote_rmtree(self.config, FakeSFTP(self.root), "/big")
        self.assertEqual(removed, 200)
        self.assertFalse(tree.exists())

    def test_rollback_without_a_rollback_set_is_refused(self):
        for path in sorted(self.save.glob("softreset.rollback-*/**/*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        (self.save / "softreset.rollback-20200101T000000Z").rmdir()
        with self.assertRaises(worker.WorkerError):
            self._run(worker.run_rollback, self.config)


if __name__ == "__main__":
    unittest.main()
