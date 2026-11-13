import json
import unittest
from pathlib import Path

from config import ConfigError, desktop_override_active, validate_config


class DesktopOverrideTests(unittest.TestCase):
    def test_whichever_was_saved_last_wins(self):
        override = {"desktop_settings": {"enabled": True, "saved_at": "2026-11-13T03:10:00Z"}}
        self.assertTrue(desktop_override_active(override, {"settingsSavedAt": "2026-11-13T02:59:20Z"}))
        self.assertFalse(desktop_override_active(override, {"settingsSavedAt": "2026-11-13T03:20:00Z"}))
        # Override saved before save times existed loses to a timed in-game save.
        old = {"desktop_settings": {"enabled": True}}
        self.assertFalse(desktop_override_active(old, {"settingsSavedAt": "2026-11-13T02:59:20Z"}))
        # Older mod without a save time: override still applies, as before.
        self.assertTrue(desktop_override_active(old, {}))
        self.assertFalse(desktop_override_active({"desktop_settings": {"enabled": False}}, {}))


class DedicatedModeSourceTests(unittest.TestCase):
    def test_in_game_modes_never_drive_dedicated_runs(self):
        from config import merge_exported_worker_settings, target_modes

        path = Path(__file__).resolve().parents[1] / "dedicated-config.example.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        validate_config(config)
        config["dedicated_settings"].pop("target_modes", None)  # older dashboard config
        export = {
            "schemaVersion": 2, "schedule": {}, "safety": {},
            "resetModes": {"mapChunks": "reset_all", "zombiePopulation": "reset_all", "animalPopulation": "reset_all"},
        }
        merge_exported_worker_settings(config, export)
        self.assertEqual(
            target_modes(config),
            {"map_chunks": "protect", "zombie_population": "skip", "animal_population": "skip"},
        )


class ConfigTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "config.example.json"
        self.config = json.loads(path.read_text(encoding="utf-8"))

    def test_example_config_is_valid(self):
        validate_config(self.config)

    def test_unverified_tcp_offline_probe_is_rejected(self):
        self.config["server"]["connection_mode"] = "sftp"
        self.config["server"]["save_dir"] = "/home/container/Saves/Multiplayer/servertest"
        self.config["server"]["zone_export"] = "/home/container/pz-reset-zones.json"
        self.config["server"]["offline_probe"] = {
            "type": "tcp",
            "host": "example.invalid",
            "port": 1234,
            "verified": False,
        }
        with self.assertRaises(ConfigError):
            validate_config(self.config)

if __name__ == "__main__":
    unittest.main()
