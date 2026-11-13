import unittest
from pathlib import Path


class ModPackageTests(unittest.TestCase):
    def test_build_42_package_contains_in_game_controls_and_server_exporter(self):
        mod_root = (
            Path(__file__).resolve().parents[1]
            / "mod"
            / "Contents"
            / "mods"
            / "FMGResetTool"
        )
        self.assertIn("id=FMGResetTool", (mod_root / "42" / "mod.info").read_text())
        server_lua = (
            mod_root / "42" / "media" / "lua" / "server" / "PZWeeklyResetExporter.lua"
        ).read_text(encoding="utf-8")
        client_lua = (
            mod_root / "42" / "media" / "lua" / "client" / "PZWeeklyResetExporter.lua"
        ).read_text(encoding="utf-8")
        self.assertTrue(server_lua.startswith("if not isServer() then return end"))
        self.assertIn("SafeHouse.getSafehouseList()", server_lua)
        self.assertIn('formatZone(\n            "tool"', server_lua)
        self.assertNotIn("NonPvpZone", server_lua)
        self.assertNotIn("getMetaGrid", server_lua)
        self.assertIn('context:addOption("FMG-SoftReset-Tool"', client_lua)
        self.assertIn("ISWorldMap.render =", client_lua)
        settings_lua = (
            mod_root / "42" / "media" / "lua" / "client" / "PZWeeklyResetAdminSettings.lua"
        ).read_text(encoding="utf-8")
        self.assertIn('local RESET_MODES = { "protect", "reset_all", "skip" }', settings_lua)
        self.assertIn("resetModes = resetModes", settings_lua)
        self.assertIn('"resetModes":%s', server_lua)
        self.assertIn("targetsFromModes(resetModes)", server_lua)


if __name__ == "__main__":
    unittest.main()
