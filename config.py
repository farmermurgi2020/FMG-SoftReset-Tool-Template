from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import date, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    pass


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("PZRESET_CONFIG", ROOT / "config.json"))
DEFAULT_RESET_TARGETS = {
    "map_chunks": True,
    "zombie_population": False,
    "animal_population": False,
}
# Per-target reset modes (dedicated dashboard, local dashboard and in-game panel):
#   protect   - reset, but keep whatever overlaps protected zones/safehouses
#   reset_all - reset the whole target, ignoring protected zones
#   skip      - leave the target untouched
TARGET_MODES = ("protect", "reset_all", "skip")
# In-game export keys (camelCase) -> worker keys.
EXPORT_TARGET_KEYS = {
    "mapChunks": "map_chunks",
    "zombiePopulation": "zombie_population",
    "animalPopulation": "animal_population",
}


def validate_target_modes(modes: Any, where: str) -> dict[str, str]:
    if not isinstance(modes, dict):
        raise ConfigError(f"{where} must be an object")
    result = {}
    for key in DEFAULT_RESET_TARGETS:
        mode = modes.get(key, "skip")
        if mode not in TARGET_MODES:
            raise ConfigError(f"{where}.{key} must be one of {', '.join(TARGET_MODES)}")
        result[key] = mode
    if all(mode == "skip" for mode in result.values()):
        raise ConfigError("every reset target is set to Don't touch; nothing would be reset")
    return result


def target_modes(config: dict[str, Any]) -> dict[str, str]:
    """Per-target modes; falls back to reset_targets (True = protect, False = skip)."""
    dedicated = config.get("dedicated_settings") or {}
    modes = dedicated.get("target_modes") if dedicated.get("enabled") else None
    if not isinstance(modes, dict):
        # Local hosted: set from the in-game export or the local dashboard override.
        modes = config.get("target_modes")
    if isinstance(modes, dict):
        return {key: modes.get(key, "skip") for key in DEFAULT_RESET_TARGETS}
    targets = config.get("reset_targets", DEFAULT_RESET_TARGETS)
    return {key: "protect" if targets.get(key) else "skip" for key in DEFAULT_RESET_TARGETS}


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {path}; copy config.example.json to config.json") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
    validate_config(value)
    return value


def desktop_override_active(config: dict[str, Any], export: dict[str, Any] | None) -> bool:
    """The local dashboard override applies only if it was saved after the in-game settings.

    Whichever was saved last wins. An override from before save times were recorded loses to
    any in-game save that has a time; an in-game export without a time (older mod) loses to
    any enabled override, as before.
    """
    desktop = config.get("desktop_settings") or {}
    if desktop.get("enabled") is not True:
        return False
    game_saved = (export or {}).get("settingsSavedAt") or ""
    if not isinstance(game_saved, str) or not game_saved:
        return True
    dashboard_saved = desktop.get("saved_at") or ""
    return isinstance(dashboard_saved, str) and dashboard_saved > game_saved


def merge_exported_worker_settings(config: dict[str, Any], export: dict[str, Any]) -> dict[str, Any]:
    if export.get("schemaVersion") != 2:
        return config
    schedule = export.get("schedule")
    safety = export.get("safety")
    if not isinstance(schedule, dict) or not isinstance(safety, dict):
        raise ConfigError("schemaVersion 2 export must contain schedule and safety objects")
    merged = deepcopy(config)
    merged["schedule"].update(schedule)
    merged["safety"].update(safety)
    merged.pop("target_modes", None)
    reset_modes = export.get("resetModes")
    if reset_modes is not None:
        if not isinstance(reset_modes, dict):
            raise ConfigError("resetModes must be an object")
        modes = validate_target_modes(
            {key: reset_modes.get(name) for name, key in EXPORT_TARGET_KEYS.items()}, "resetModes"
        )
        merged["target_modes"] = modes
        merged["reset_targets"] = {key: mode != "skip" for key, mode in modes.items()}
    reset_targets = export.get("resetTargets")
    if reset_targets is not None and reset_modes is None:
        if not isinstance(reset_targets, dict):
            raise ConfigError("resetTargets must be an object")
        mapped_targets = {
            "map_chunks": reset_targets.get("mapChunks"),
            "zombie_population": reset_targets.get("zombiePopulation"),
            "animal_population": reset_targets.get("animalPopulation"),
        }
        for key, enabled in mapped_targets.items():
            if not isinstance(enabled, bool):
                raise ConfigError(f"resetTargets.{key} must be a boolean")
        merged["reset_targets"] = mapped_targets
    desktop_settings = merged.get("desktop_settings", {})
    if desktop_override_active(merged, export):
        if merged["server"].get("connection_mode") != "local":
            raise ConfigError("desktop settings are supported only for Local hosted mode")
        targets = desktop_settings.get("reset_targets")
        schedule_override = desktop_settings.get("schedule")
        if not isinstance(targets, dict) or not isinstance(schedule_override, dict):
            raise ConfigError("enabled desktop settings must contain reset_targets and schedule")
        merged["reset_targets"] = deepcopy(targets)
        merged.pop("target_modes", None)
        if desktop_settings.get("target_modes") is not None:
            merged["target_modes"] = validate_target_modes(
                desktop_settings["target_modes"], "desktop_settings.target_modes"
            )
            merged["reset_targets"] = {key: mode != "skip" for key, mode in merged["target_modes"].items()}
        merged["schedule"].update(deepcopy(schedule_override))
        # This dashboard currently supports safe scheduled dry runs only.
        merged["safety"]["apply_enabled"] = False
        merged["safety"]["staging_and_rollback_tested"] = False
    dedicated_settings = merged.get("dedicated_settings", {})
    if dedicated_settings.get("enabled") is True:
        if merged["server"].get("connection_mode") != "sftp":
            raise ConfigError("dedicated settings are supported only for SFTP mode")
        targets = dedicated_settings.get("reset_targets")
        schedule_override = dedicated_settings.get("schedule")
        if not isinstance(targets, dict) or not isinstance(schedule_override, dict):
            raise ConfigError("enabled dedicated settings must contain reset_targets and schedule")
        merged["reset_targets"] = deepcopy(targets)
        # Dedicated runs follow the dedicated dashboard only, never the in-game panel's modes.
        merged.pop("target_modes", None)
        merged["schedule"].update(deepcopy(schedule_override))
        # The first GitHub-hosted dedicated workflow is plan-only.
        merged["safety"]["apply_enabled"] = False
        merged["safety"]["staging_and_rollback_tested"] = False
    validate_config(merged)
    config.update(merged)
    if "target_modes" not in merged:
        config.pop("target_modes", None)
    return config


# Zone sources the in-game mod exports. "claimed-vehicle" areas come from another mod (Knox
# Claim) and are only written when that mod is active. "non-pvp" zones are never protection.
ZONE_SOURCES = {"safehouse", "tool", "claimed-vehicle"}
ZONE_SOURCE_LABELS = {
    "safehouse": "Safehouses",
    "tool": "Admin protected areas",
    "claimed-vehicle": "Claimed vehicles (Knox Claim)",
}


def eligible_export_zones(export: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the exported zones that count as protection, refusing unknown sources."""
    zones = export.get("zones")
    if not isinstance(zones, list):
        raise ConfigError("zone export must contain a zones array")
    eligible = []
    for zone in zones:
        if not isinstance(zone, dict):
            raise ConfigError("each exported zone must be an object")
        source = zone.get("source")
        if source == "non-pvp":
            continue
        if export.get("schemaVersion") == 2 and source not in ZONE_SOURCES:
            raise ConfigError(f"unsupported protected-zone source in schemaVersion 2 export: {source!r}")
        eligible.append(zone)
    return eligible


def zone_source_counts(zones: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for zone in zones:
        source = str(zone.get("source", "manual"))
        counts[source] = counts.get(source, 0) + 1
    return counts


def mod_protection_errors(export: dict[str, Any]) -> list[str]:
    """Problems the mod reported while reading another mod's data (e.g. Knox Claim's claims).

    A protection that could not be read means the export may be missing areas that should be
    kept, so a reset that keeps protected zones must not run on it.
    """
    protections = export.get("modProtections")
    if protections is None:
        return []
    if not isinstance(protections, dict):
        return ["modProtections in the zone export is not an object"]
    errors = []
    for name, value in sorted(protections.items()):
        if not isinstance(value, dict):
            errors.append(f"{name}: invalid entry")
        elif value.get("status") == "error":
            errors.append(f"{name}: {value.get('message') or 'could not be read'}")
    return errors


def protected_area_notes(plan: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(info lines, warning lines) about the protected areas behind a plan, for logs and summaries."""
    counts = plan.get("protected_zone_counts") or {}
    info = [f"{ZONE_SOURCE_LABELS.get(source, source)} protected: {count}" for source, count in sorted(counts.items())]
    warnings = []
    if counts.get("claimed-vehicle") and plan.get("target_modes", {}).get("map_chunks") == "reset_all":
        warnings.append("Map chunks are set to Reset everything, which ignores protected areas: "
                        f"{counts['claimed-vehicle']} claimed vehicle(s) will NOT be protected.")
    return info, warnings


def validate_config(value: Any) -> None:
    if not isinstance(value, dict):
        raise ConfigError("config must be a JSON object")
    for key in ("server", "world", "schedule", "safety"):
        if not isinstance(value.get(key), dict):
            raise ConfigError(f"config.{key} must be an object")
    reset_targets = value.setdefault("reset_targets", deepcopy(DEFAULT_RESET_TARGETS))
    if not isinstance(reset_targets, dict):
        raise ConfigError("config.reset_targets must be an object")
    for key, default in DEFAULT_RESET_TARGETS.items():
        reset_targets.setdefault(key, default)
        if not isinstance(reset_targets[key], bool):
            raise ConfigError(f"reset_targets.{key} must be a boolean")
    if value.get("target_modes") is not None:
        validate_target_modes(value["target_modes"], "config.target_modes")
    desktop_settings = value.setdefault("desktop_settings", {"enabled": False})
    if not isinstance(desktop_settings, dict) or not isinstance(desktop_settings.get("enabled"), bool):
        raise ConfigError("config.desktop_settings.enabled must be a boolean")
    dedicated_settings = value.setdefault("dedicated_settings", {"enabled": False})
    if not isinstance(dedicated_settings, dict) or not isinstance(dedicated_settings.get("enabled"), bool):
        raise ConfigError("config.dedicated_settings.enabled must be a boolean")

    server, world = value["server"], value["world"]
    connection_mode = server.get("connection_mode", "sftp")
    if connection_mode not in {"local", "sftp"}:
        raise ConfigError("server.connection_mode must be local or sftp")
    if desktop_settings["enabled"] and connection_mode != "local":
        raise ConfigError("desktop settings are supported only when server.connection_mode is local")
    if dedicated_settings["enabled"] and connection_mode != "sftp":
        raise ConfigError("dedicated settings are supported only when server.connection_mode is sftp")
    schedule, safety = value["schedule"], value["safety"]
    for key in ("name", "save_dir", "zone_export"):
        if not isinstance(server.get(key), str) or not server[key].strip():
            raise ConfigError(f"server.{key} must be a non-empty string")
    if connection_mode == "sftp":
        if not server["save_dir"].startswith("/") or not server["zone_export"].startswith("/"):
            raise ConfigError("SFTP server.save_dir and server.zone_export must be absolute POSIX paths")
    elif not Path(server["save_dir"]).is_absolute() or not Path(server["zone_export"]).is_absolute():
        raise ConfigError("local server.save_dir and server.zone_export must be absolute Windows paths")
    for key in ("sftp_port",):
        port = server.get(key)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError(f"server.{key} must be an integer between 1 and 65535")
    for key in ("sftp_host", "sftp_username", "sftp_key_file"):
        if key in server and not isinstance(server[key], str):
            raise ConfigError(f"server.{key} must be a string")
    if connection_mode == "sftp":
        probe = server.get("offline_probe")
        if not isinstance(probe, dict) or probe.get("type") not in {"unconfigured", "tcp"}:
            raise ConfigError("server.offline_probe.type must be unconfigured or tcp")
        if not isinstance(probe.get("host", ""), str):
            raise ConfigError("server.offline_probe.host must be a string")
        if probe.get("type") == "tcp":
            if not isinstance(probe.get("host"), str) or not probe["host"].strip():
                raise ConfigError("a TCP offline probe requires a host")
            port = probe.get("port")
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ConfigError("a TCP offline probe requires a valid port")
            if probe.get("verified") is not True:
                raise ConfigError("a TCP offline probe must be explicitly verified")
        elif probe.get("verified") is not False:
            raise ConfigError("an unconfigured offline probe cannot be marked verified")
    for key in ("build",):
        if not isinstance(world.get(key), str) or not world[key].strip():
            raise ConfigError(f"world.{key} must be a non-empty string")
    for key in ("chunk_size",):
        if isinstance(world.get(key), bool) or not isinstance(world.get(key), int):
            raise ConfigError(f"world.{key} must be an integer")
    if world["chunk_size"] <= 0:
        raise ConfigError("world.chunk_size must be positive")

    if schedule.get("frequency") not in {"daily", "weekly", "monthly"}:
        raise ConfigError("schedule.frequency must be daily, weekly, or monthly")
    if not isinstance(schedule.get("enabled"), bool):
        raise ConfigError("schedule.enabled must be a boolean")
    try:
        date.fromisoformat(schedule["anchor_date"])
        times = [
            time.fromisoformat(schedule["time"]),
            time.fromisoformat(schedule["pine_stop_time"]),
            time.fromisoformat(schedule["pine_start_time"]),
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError("schedule date and times must use ISO date/time format") from exc
    if any(value.tzinfo is not None for value in times):
        raise ConfigError("schedule times must be local wall times without a UTC offset")
    if not isinstance(schedule.get("timezone"), str) or not schedule["timezone"].strip():
        raise ConfigError("schedule.timezone must be a non-empty IANA timezone")
    try:
        ZoneInfo(schedule["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"unknown IANA timezone: {schedule['timezone']}") from exc
    _integer(schedule, "minimum_start_buffer_minutes", 0, 1440)
    _integer(schedule, "allowed_lateness_minutes", 0, 1440)

    for key, expected in {
        "apply_enabled": bool,
        "staging_and_rollback_tested": bool,
    }.items():
        if not isinstance(safety.get(key), expected):
            raise ConfigError(f"safety.{key} must be a boolean")
    _integer(safety, "offline_probe_repetitions", 2, 10)
    _integer(safety, "offline_probe_interval_seconds", 1, 600)
    _integer(safety, "file_stability_seconds", 1, 3600)
    if not isinstance(safety.get("backup_dir"), str) or not safety["backup_dir"].strip():
        raise ConfigError("safety.backup_dir must be a non-empty path")
    if not isinstance(safety.get("approved_build"), str):
        raise ConfigError("safety.approved_build must be a string")
    if not isinstance(safety.get("approved_chunk_layout"), str):
        raise ConfigError("safety.approved_chunk_layout must be a string")

    if dedicated_settings["enabled"]:
        targets = dedicated_settings.get("reset_targets")
        if not isinstance(targets, dict):
            raise ConfigError("dedicated_settings.reset_targets must be an object")
        for key in DEFAULT_RESET_TARGETS:
            if not isinstance(targets.get(key), bool):
                raise ConfigError(f"dedicated_settings.reset_targets.{key} must be a boolean")
        schedule_override = dedicated_settings.get("schedule")
        if not isinstance(schedule_override, dict):
            raise ConfigError("dedicated_settings.schedule must be an object")
        for key in (
            "enabled", "frequency", "anchor_date", "time", "timezone",
            "pine_stop_time", "pine_start_time", "minimum_start_buffer_minutes",
            "allowed_lateness_minutes",
        ):
            if key not in schedule_override:
                raise ConfigError(f"dedicated_settings.schedule.{key} is required")
        if not isinstance(schedule_override["enabled"], bool):
            raise ConfigError("dedicated_settings.schedule.enabled must be a boolean")
        modes = dedicated_settings.get("target_modes")
        if modes is not None:
            if not isinstance(modes, dict):
                raise ConfigError("dedicated_settings.target_modes must be an object")
            for key in DEFAULT_RESET_TARGETS:
                if modes.get(key) not in TARGET_MODES:
                    raise ConfigError(
                        f"dedicated_settings.target_modes.{key} must be one of {', '.join(TARGET_MODES)}"
                    )
            if all(modes[key] == "skip" for key in DEFAULT_RESET_TARGETS):
                raise ConfigError("at least one reset target must be reset (not all set to skip)")
        if dedicated_settings.setdefault("scheduled_action", "dry-run") not in {"dry-run", "apply"}:
            raise ConfigError("dedicated_settings.scheduled_action must be dry-run or apply")


def _integer(section: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be an integer between {minimum} and {maximum}")
    return value


def _number(section: dict[str, Any], key: str, minimum: float, maximum: float) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be a number between {minimum} and {maximum}")
    return float(value)
