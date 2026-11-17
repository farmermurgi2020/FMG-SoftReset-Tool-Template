import time
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from local_worker import (
    _copy_protected_chunks,
    _require_exact_test_path,
    _read_zone_export,
    run_local_apply,
    run_local_rollback,
    scan_local_chunks,
)
from worker import Chunk, WorkerError


_HISTORY_DIR = None
_ORIGINAL_HISTORY_DIR = None


def setUpModule():
    # Keep test runs out of the real run-history folder the dashboard shows.
    global _HISTORY_DIR, _ORIGINAL_HISTORY_DIR
    import history
    _HISTORY_DIR = tempfile.TemporaryDirectory()
    _ORIGINAL_HISTORY_DIR = history.HISTORY_DIR
    history.HISTORY_DIR = Path(_HISTORY_DIR.name) / "run-history"


def tearDownModule():
    import history
    history.HISTORY_DIR = _ORIGINAL_HISTORY_DIR
    _HISTORY_DIR.cleanup()

class LocalWorkerTests(unittest.TestCase):
    def test_build_42_export_uses_only_safehouse_and_tool_zones_and_worker_settings(self):
        project = Path(__file__).resolve().parents[1]
        config = json.loads((project / "config.example.json").read_text(encoding="utf-8"))
        config["world"]["build"] = "42.22.2"
        with tempfile.TemporaryDirectory() as temporary:
            export_path = Path(temporary) / "zones.json"
            export_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "build": "42.22.2",
                        "generatedAt": datetime.now(timezone.utc).isoformat(),
                        "zones": [
                            {"id": "safehouse-1", "x": 0, "y": 0, "w": 8, "h": 8, "source": "safehouse"},
                            {"id": "tool-1", "x": 8, "y": 0, "w": 8, "h": 8, "source": "tool"},
                            {"id": "non-pvp-1", "x": 16, "y": 0, "w": 8, "h": 8, "source": "non-pvp"},
                        ],
                        "schedule": config["schedule"],
                        "safety": config["safety"],
                    }
                ),
                encoding="utf-8",
            )
            export, rects = _read_zone_export(export_path, config)

        self.assertEqual(len(rects), 2)
        self.assertEqual(export["schemaVersion"], 2)

    def _write_export(self, path, config, zones, **extra):
        payload = {
            "schemaVersion": 2, "build": "42.22.2", "executionMode": "local",
            "generatedAt": datetime.now(timezone.utc).isoformat(), "zones": zones,
            "schedule": config["schedule"], "safety": config["safety"],
        }
        payload.update(extra)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _example_config(self):
        project = Path(__file__).resolve().parents[1]
        config = json.loads((project / "config.example.json").read_text(encoding="utf-8"))
        config["world"]["build"] = "42.22.2"
        return config

    def test_claimed_vehicle_zones_count_as_protection(self):
        config = self._example_config()
        with tempfile.TemporaryDirectory() as temporary:
            export_path = Path(temporary) / "zones.json"
            self._write_export(export_path, config, [
                {"id": "tool-1", "x": 0, "y": 0, "w": 8, "h": 8, "source": "tool"},
                {"id": "knox-1", "x": 800, "y": 800, "w": 24, "h": 24, "source": "claimed-vehicle"},
            ], modProtections={"knoxClaimVehicles": {"status": "ok", "count": 1}})
            _, rects = _read_zone_export(export_path, config)
        self.assertEqual(len(rects), 2)

    def test_refuses_when_knox_claims_could_not_be_read(self):
        config = self._example_config()
        with tempfile.TemporaryDirectory() as temporary:
            export_path = Path(temporary) / "zones.json"
            self._write_export(export_path, config, [
                {"id": "tool-1", "x": 0, "y": 0, "w": 8, "h": 8, "source": "tool"},
            ], modProtections={"knoxClaimVehicles": {"status": "error", "message": "store failed"}})
            with self.assertRaisesRegex(WorkerError, "store failed"):
                _read_zone_export(export_path, config)

    def test_unknown_zone_source_is_still_refused(self):
        config = self._example_config()
        with tempfile.TemporaryDirectory() as temporary:
            export_path = Path(temporary) / "zones.json"
            self._write_export(export_path, config, [
                {"id": "x-1", "x": 0, "y": 0, "w": 8, "h": 8, "source": "mystery"},
            ])
            with self.assertRaisesRegex(WorkerError, "unsupported protected-zone source"):
                _read_zone_export(export_path, config)

    def test_scans_negative_chunk_coordinates(self):
        with tempfile.TemporaryDirectory() as temporary:
            map_dir = Path(temporary) / "map"
            chunk_dir = map_dir / "-1"
            chunk_dir.mkdir(parents=True)
            (chunk_dir / "-2.bin").write_bytes(b"chunk")
            chunks = scan_local_chunks(map_dir)
            self.assertEqual([(chunk.x, chunk.y) for chunk in chunks], [(-1, -2)])

    def test_rejects_non_chunk_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            map_dir = Path(temporary) / "map"
            chunk_dir = map_dir / "0"
            chunk_dir.mkdir(parents=True)
            (chunk_dir / "notes.txt").write_text("unexpected", encoding="utf-8")
            with self.assertRaises(WorkerError):
                scan_local_chunks(map_dir)

    def test_staged_copy_contains_only_protected_chunks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "map" / "0" / "0.bin"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"protected")
            stage = root / ".map-stage"
            _copy_protected_chunks([Chunk(0, 0, str(source), 9, int(source.stat().st_mtime))], stage)
            self.assertEqual((stage / "0" / "0.bin").read_bytes(), b"protected")

    def test_confirmed_path_must_match_configured_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary) / "save-copy"
            save.mkdir()
            config = {"server": {"save_dir": str(save)}}
            self.assertEqual(_require_exact_test_path(config, str(save)), save.resolve())
            with self.assertRaises(WorkerError):
                _require_exact_test_path(config, temporary)

    def test_requires_path_attestation(self):
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary) / "save-copy"
            save.mkdir()
            with self.assertRaises(WorkerError):
                _require_exact_test_path({"server": {"save_dir": str(save)}}, None)

    def test_apply_and_rollback_on_disposable_local_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save = root / "save-copy"
            map_dir = save / "map"
            (map_dir / "0").mkdir(parents=True)
            (map_dir / "1").mkdir()
            (map_dir / "0" / "0.bin").write_bytes(b"protected")
            (map_dir / "1" / "0.bin").write_bytes(b"reset")
            export_path = root / "zones.json"
            export_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "build": "42.22.2",
                        "generatedAt": datetime.now(timezone.utc).isoformat(),
                        "zones": [{"x": 0, "y": 0, "w": 8, "h": 8}],
                    }
                ),
                encoding="utf-8",
            )
            config = {
                "server": {
                    "connection_mode": "local",
                    "name": "fixture",
                    "save_dir": str(save),
                    "zone_export": str(export_path),
                },
                "world": {"build": "42.22.2", "chunk_size": 8},
                "reset_targets": {
                    "map_chunks": True,
                    "zombie_population": False,
                    "animal_population": False,
                },
                "safety": {
                    "max_zone_export_age_minutes": 30,
                    "file_stability_seconds": 1,
                    "backup_dir": str(root / "backups"),
                },
            }

            with patch("local_worker._assert_game_closed"), patch("local_worker.time.sleep"), patch(
                "local_worker._save_plan"
            ):
                plan = run_local_apply(config, True, str(save))

            self.assertEqual(plan["protected_count"], 1)
            self.assertEqual(plan["remove_count"], 1)
            self.assertEqual((map_dir / "0" / "0.bin").read_bytes(), b"protected")
            self.assertFalse((map_dir / "1").exists())
            rollback_dir = Path(plan["local_rollback_dir"])
            self.assertTrue((rollback_dir / "map" / "1" / "0.bin").is_file())

            time.sleep(1.1)  # rollback folders are named by the second
            with patch("local_worker._assert_game_closed"):
                run_local_rollback(config, str(rollback_dir), True, str(save))
            self.assertEqual((map_dir / "1" / "0.bin").read_bytes(), b"reset")

    def _mode_fixture(self, root: Path, reset_modes: dict, zones: list) -> dict:
        save = root / "save-copy"
        (save / "map" / "0").mkdir(parents=True)
        (save / "map" / "1").mkdir()
        (save / "map" / "0" / "0.bin").write_bytes(b"protected")
        (save / "map" / "1" / "0.bin").write_bytes(b"reset")
        (save / "zpop").mkdir()
        (save / "zpop" / "zpop_0_0.bin").write_bytes(b"cell-a")
        (save / "zpop" / "zpop_5_5.bin").write_bytes(b"cell-b")
        (save / "zpop" / "zpop_virtual.bin").write_bytes(b"shared")
        project = Path(__file__).resolve().parents[1]
        config = json.loads((project / "config.example.json").read_text(encoding="utf-8"))
        config["server"].update({
            "connection_mode": "local", "name": "fixture", "save_dir": str(save),
            "zone_export": str(root / "zones.json"),
        })
        config["world"].update({"build": "42.22.2", "chunk_size": 8})
        config["safety"].update({"file_stability_seconds": 1, "backup_dir": str(root / "backups")})
        (root / "zones.json").write_text(json.dumps({
            "schemaVersion": 2,
            "build": "42.22.2",
            "executionMode": "local",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "zones": zones,
            "resetModes": reset_modes,
            "resetTargets": {key: mode != "skip" for key, mode in reset_modes.items()},
            "schedule": config["schedule"],
            "safety": config["safety"],
        }), encoding="utf-8")
        return config

    def test_reset_everything_needs_no_zones_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._mode_fixture(
                root, {"mapChunks": "reset_all", "zombiePopulation": "reset_all", "animalPopulation": "skip"}, []
            )
            save = Path(config["server"]["save_dir"])
            with patch("local_worker._assert_game_closed"), patch("local_worker.time.sleep"), patch(
                "local_worker._save_plan"
            ):
                plan = run_local_apply(config, True, str(save))
            self.assertEqual(plan["target_modes"]["map_chunks"], "reset_all")
            self.assertEqual(list((save / "map").iterdir()), [])
            self.assertEqual(sorted(p.name for p in (save / "zpop").iterdir()), ["zpop_virtual.bin"])
            self.assertEqual(plan["target_counts"]["zombie_population"]["reset"], 2)

            time.sleep(1.1)
            with patch("local_worker._assert_game_closed"):
                run_local_rollback(config, plan["local_rollback_dir"], True, str(save))
            self.assertEqual((save / "map" / "1" / "0.bin").read_bytes(), b"reset")
            self.assertEqual((save / "zpop" / "zpop_5_5.bin").read_bytes(), b"cell-b")

    def test_mixed_modes_keep_zones_only_where_asked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._mode_fixture(
                root,
                {"mapChunks": "protect", "zombiePopulation": "reset_all", "animalPopulation": "skip"},
                [{"id": "tool-1", "x": 0, "y": 0, "w": 8, "h": 8, "source": "tool"}],
            )
            save = Path(config["server"]["save_dir"])
            with patch("local_worker._assert_game_closed"), patch("local_worker.time.sleep"), patch(
                "local_worker._save_plan"
            ):
                plan = run_local_apply(config, True, str(save))
            self.assertTrue((save / "map" / "0" / "0.bin").is_file())
            self.assertFalse((save / "map" / "1").exists())
            # zpop is "reset everything", so the cell under the zone goes too.
            self.assertFalse((save / "zpop" / "zpop_0_0.bin").exists())
            self.assertEqual(plan["selected_directories"], ["map", "zpop"])

    def test_keep_protected_zones_without_zones_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._mode_fixture(
                root, {"mapChunks": "protect", "zombiePopulation": "skip", "animalPopulation": "skip"}, []
            )
            with self.assertRaisesRegex(WorkerError, "no zones"):
                _read_zone_export(Path(config["server"]["zone_export"]), config)

    def test_all_dont_touch_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._mode_fixture(
                root, {"mapChunks": "skip", "zombiePopulation": "skip", "animalPopulation": "skip"}, []
            )
            with self.assertRaisesRegex(WorkerError, "Don't touch"):
                _read_zone_export(Path(config["server"]["zone_export"]), config)


if __name__ == "__main__":
    unittest.main()
