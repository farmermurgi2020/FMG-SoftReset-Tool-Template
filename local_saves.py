"""Finding and choosing the locally hosted save the local dashboard works on.

Zomboid/Saves/Multiplayer also holds the game's caches of servers joined as a player, always
named <IP>_<port>_<32-character id> (for example 208.73.207.202_16261_3367705d…). Some of
those contain map_meta.bin too, so the name is what tells them apart. A hosted save always
has map_meta.bin (players.db is not a reliable sign: a hard reset moves it out). A save whose
name also has a Zomboid/Server/<name>.ini is one of the servers set up under Host.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from worker import WorkerError


META_FILE = "map_meta.bin"
JOINED_SERVER_CACHE = re.compile(r"\d{1,3}(\.\d{1,3}){3}_\d+_[0-9a-f]{32}(_crash)?")


def is_joined_server_cache(name: str) -> bool:
    return JOINED_SERVER_CACHE.fullmatch(name) is not None


def zomboid_dir(config: dict[str, Any]) -> Path:
    """The Zomboid folder: from the configured save (…/Zomboid/Saves/Multiplayer/<save>) or the default."""
    save_dir = Path(config["server"].get("save_dir", ""))
    if len(save_dir.parts) >= 4 and save_dir.parent.name == "Multiplayer" and save_dir.parents[1].name == "Saves":
        return save_dir.parents[2]
    return Path.home() / "Zomboid"


def multiplayer_dir(config: dict[str, Any]) -> Path:
    return zomboid_dir(config) / "Saves" / "Multiplayer"


def hosted_saves(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Saves made by hosting a server, sorted by name: [{"name", "path", "has_server_ini"}]."""
    root = multiplayer_dir(config)
    server_dir = zomboid_dir(config) / "Server"
    if not root.is_dir():
        return []
    saves = []
    for folder in sorted(root.iterdir(), key=lambda path: path.name.lower()):
        if (folder.is_dir() and not folder.is_symlink() and not is_joined_server_cache(folder.name)
                and (folder / META_FILE).is_file()):
            saves.append({
                "name": folder.name,
                "path": folder,
                "has_server_ini": (server_dir / f"{folder.name}.ini").is_file(),
            })
    return saves


def validate_save_dir(path: str | Path) -> Path:
    folder = Path(path)
    if folder.is_symlink():
        raise WorkerError(f"{folder} is a symlink; choose the real save folder")
    try:
        folder = folder.resolve(strict=True)
    except OSError as exc:
        raise WorkerError(f"save folder not found: {path}") from exc
    if not folder.is_dir():
        raise WorkerError(f"{folder} is not a folder")
    if folder == Path(folder.anchor):
        raise WorkerError("refusing to use a drive root as the save folder")
    if is_joined_server_cache(folder.name):
        raise WorkerError(
            f"{folder.name} is the game's cache of a server you joined as a player, not a server you host"
        )
    if not (folder / META_FILE).is_file():
        raise WorkerError(f"{folder} has no {META_FILE}, so it is not a hosted server save")
    return folder


def select_save(config: dict[str, Any], path: str | Path) -> Path:
    """Point config at the save folder; the server name follows the folder name, as in the game."""
    folder = validate_save_dir(path)
    config["server"]["save_dir"] = str(folder)
    config["server"]["name"] = folder.name
    return folder


def export_save_mismatch(export: dict[str, Any], save_dir: str | Path) -> str | None:
    """Why the zone export can't be used for this save, or None.

    Every hosted server writes the same Zomboid/Lua/pz-reset-zones.json. Since mod 0.4.3 it
    records the save it came from; an export from another save would protect the wrong zones.
    """
    exported = export.get("saveName")
    expected = Path(save_dir).name
    if exported and exported != expected:
        return (f"The zone export was written by save '{exported}', not '{expected}'. "
                f"Host '{expected}' once (the mod exports its zones as soon as the server starts), "
                "then try again.")
    return None
