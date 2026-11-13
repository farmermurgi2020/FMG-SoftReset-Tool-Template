"""Hard Reset (Player Data only) for a locally hosted Windows server.

Removes every character and restarts the world clock at day 1, leaving map chunks, zombie
and animal population, vehicles, accounts and admin roles alone.

Two parts:
1. With the game closed, the player-only files below are moved into a
   hardreset.rollback-<UTC time> folder inside the save (so the hard reset can be undone),
   and map_meta.bin and global_mod_data.bin are copied there too.
2. A "pending" note is written next to the zone export (Zomboid/Lua). On the next server
   start the FMGResetTool mod removes all safehouses and factions (and Knox Claim claims),
   which live inside those two files together with world data and cannot be removed safely
   from outside the game, and marks the note done.

The rules shared with the dedicated version (hard_reset_remote.py) live here: what moves,
what is copied, the note, the plan log and the move-or-put-everything-back transaction.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from config import ConfigError, load_config
from local_worker import _assert_game_closed, _require_exact_test_path
from worker import WorkerError, atomic_write_json


LOG = logging.getLogger("fmg-hard-reset")
ROLLBACK_PREFIX = "hardreset.rollback-"
PENDING_FILE = "fmg-hard-reset-pending.json"
META_FILE = "map_meta.bin"
# (save entry, what it holds). Moved out of the save; the game recreates them fresh.
PLAYER_ITEMS: tuple[tuple[str, str], ...] = (
    ("players.db", "Characters: inventory, skills, health, position"),
    ("players.db-journal", "Characters database journal"),
    ("reanimated.bin", "Zombies of dead players"),
    ("map_visited_server", "Each player's revealed map"),
    ("servermap_symbols.bin", "Player map markings"),
    ("map_t.bin", "World clock: day count, time and weather"),
    ("erosion.ini", "Vegetation growth clock"),
    ("radio", "Radio and TV schedule (days since start)"),
)
PLAYER_NAMES = frozenset(name for name, _ in PLAYER_ITEMS)
# Copied (not moved) so an undo can restore them: safehouses and factions live in map_meta.bin;
# claim mods such as Knox Claim keep their land and vehicle claims in global_mod_data.bin.
MOD_DATA_FILE = "global_mod_data.bin"
COPIED_FILES = (META_FILE, MOD_DATA_FILE)
ALLOWED_ROLLBACK_ENTRIES = PLAYER_NAMES | set(COPIED_FILES)


# ----- Rules shared with the dedicated version -----------------------------------------------

def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def or_unknown(value: Any) -> Any:
    return "unknown" if value is None else value


def require_local(config: dict[str, Any]) -> None:
    if config["server"].get("connection_mode") != "local":
        raise WorkerError("hard reset is only available for local hosted servers here")


def undo_swap_names(rollback_entries: Iterable[str]) -> list[str]:
    """Save entries an undo moves aside before restoring rollback_entries.

    Player entries always swap, including ones the game recreated after the reset (for
    example a new players.db), so old and new are never mixed. A copied file only swaps when
    the rollback has a copy of it: otherwise the live file (all mods' data) would be lost.
    """
    entries = set(rollback_entries)
    return sorted(name for name in ALLOWED_ROLLBACK_ENTRIES if name in PLAYER_NAMES or name in entries)


def check_rollback_entries(folder_name: str, entries: Iterable[str]) -> list[str]:
    entries = sorted(entries)
    unexpected = [name for name in entries if name not in ALLOWED_ROLLBACK_ENTRIES]
    if unexpected or META_FILE not in entries:
        raise WorkerError(f"unexpected contents in {folder_name}: {unexpected or 'missing ' + META_FILE}")
    return entries


def new_note(config: dict[str, Any], save_name: str, rollback_name: str) -> dict[str, Any]:
    """The note the mod acts on at the next start of the save named save_name."""
    return {
        "status": "pending",
        "requestedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "server": config["server"]["name"],
        "saveName": save_name,
        "rollback": rollback_name,
    }


def parse_note(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"status": "unreadable"}
    return value if isinstance(value, dict) else {"status": "unreadable"}


def note_status_after_undo(status: str | None) -> str | None:
    """New note status after an undo, or None to leave the note alone.

    An undo of a hard reset whose in-game part has not run yet cancels it. Undoing that undo
    restores the reset state again, so the in-game part has to run after all.
    """
    return {"pending": "cancelled", "cancelled": "pending"}.get(status or "")


def update_note_after_undo(note: dict[str, Any] | None) -> bool:
    """Apply note_status_after_undo to note in place; True when it changed and must be written."""
    new_status = note_status_after_undo((note or {}).get("status"))
    if not note or not new_status:
        return False
    note["status"] = new_status
    LOG.info("Safehouse/faction removal on the next server start: %s.",
             "cancelled" if new_status == "cancelled" else "scheduled again")
    return True


def count_characters(players_db: Path, *, immutable: bool = False) -> int | None:
    try:
        uri = players_db.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
        # closing() matters: sqlite3's own context manager does not close the file, and an open
        # handle would stop Windows from moving players.db afterwards.
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
            return int(connection.execute("select count(*) from networkPlayers").fetchone()[0])
    except sqlite3.Error:
        return None


def count_safehouses(export_text: str) -> int | None:
    try:
        export = json.loads(export_text)
        return sum(1 for zone in export.get("zones", []) if zone.get("source") == "safehouse")
    except (json.JSONDecodeError, AttributeError):
        return None


def log_plan(plan: dict[str, Any]) -> None:
    LOG.info("Characters to remove: %s", or_unknown(plan["characters"]))
    LOG.info("Safehouses to remove on next server start: %s (plus all factions and Knox Claim claims)",
             or_unknown(plan["safehouses"]))
    for item in plan["items"]:
        LOG.info("Move out: %s (%s; %d files, %.1f MB)", item["name"], item["label"],
                 item["files"], item["bytes"] / 1_048_576)
    LOG.info("Kept: map chunks, zpop, apop, vehicles, accounts, admin roles, mod settings.")
    note = plan.get("note")
    if note and note.get("status") == "pending":
        LOG.warning("The previous hard reset note is still pending: the server has not started with "
                    "the FMGResetTool mod since then (safehouses/factions not removed yet).")


class MoveFailed(WorkerError):
    def __init__(self, error: Exception, recovery_errors: list[str]) -> None:
        self.error = error
        self.recovery_errors = recovery_errors
        self.recovered = not recovery_errors
        super().__init__(str(error))


def apply_moves(
    pairs: list[tuple[Any, Any]],
    rename: Callable[[Any, Any], None],
    caught: tuple[type[BaseException], ...],
    then: Callable[[], None] | None = None,
) -> None:
    """Rename each (source, target) in order, then run `then`. If anything fails, rename the
    finished moves back in reverse order and raise MoveFailed, which says whether that worked."""
    done: list[tuple[Any, Any]] = []
    try:
        for source, target in pairs:
            rename(source, target)
            done.append((source, target))
        if then is not None:
            then()
    except caught as error:
        recovery_errors = []
        for source, target in reversed(done):
            try:
                rename(target, source)
            except caught as recovery_error:
                recovery_errors.append(f"{target}: {recovery_error}")
        raise MoveFailed(error, recovery_errors) from error


def critical(action: str, failure: MoveFailed, keep: str) -> WorkerError:
    return WorkerError(
        f"CRITICAL: {action} failed ({failure.error}); putting files back also failed "
        f"({'; '.join(failure.recovery_errors)}). Inspect {keep}. Do not start the server until fixed."
    )


# ----- Local (Windows) file operations ---------------------------------------------------------

def pending_path(config: dict[str, Any]) -> Path:
    """The note lives in the folder the mod writes its zone export to (Zomboid/Lua)."""
    return Path(config["server"]["zone_export"]).parent / PENDING_FILE


def read_pending(config: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return parse_note(pending_path(config).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError:
        return {"status": "unreadable"}


def _write_pending(config: dict[str, Any], value: dict[str, Any]) -> None:
    atomic_write_json(pending_path(config), value)


def _entry_size(path: Path) -> tuple[int, int]:
    """(files, bytes) for a file or folder."""
    if path.is_file():
        return 1, path.stat().st_size
    files = total = 0
    for child in path.rglob("*"):
        if child.is_symlink():
            raise WorkerError(f"symlink inside {path}; refusing hard reset")
        if child.is_file():
            files += 1
            total += child.stat().st_size
    return files, total


def plan_hard_reset(config: dict[str, Any]) -> dict[str, Any]:
    """Read-only summary of what a hard reset would do."""
    save_dir = Path(config["server"]["save_dir"]).resolve(strict=True)
    items = []
    for name, label in PLAYER_ITEMS:
        path = save_dir / name
        if path.is_symlink():
            raise WorkerError(f"{path} is a symlink; refusing hard reset")
        if path.exists():
            files, size = _entry_size(path)
            items.append({"name": name, "label": label, "files": files, "bytes": size})
    if not (save_dir / META_FILE).is_file():
        raise WorkerError(f"{META_FILE} not found in {save_dir}; is this the right save folder?")
    players_db = save_dir / "players.db"
    try:
        safehouses = count_safehouses(Path(config["server"]["zone_export"]).read_text(encoding="utf-8"))
    except OSError:
        safehouses = None
    return {
        "server": config["server"]["name"],
        "save_dir": str(save_dir),
        "items": items,
        "characters": count_characters(players_db) if players_db.is_file() else 0,
        "safehouses": safehouses,
        "note": read_pending(config),
    }


def _prune_older_hard_reset_sets(save_dir: Path, keep: Path) -> None:
    for path in save_dir.glob(ROLLBACK_PREFIX + "*"):
        if path == keep or path.is_symlink() or not path.is_dir():
            continue
        try:
            shutil.rmtree(path)
            LOG.info("Removed older hard reset rollback: %s", path.name)
        except OSError:
            LOG.exception("Could not remove older hard reset rollback: %s", path)


def latest_rollback(save_dir: Path) -> Path | None:
    candidates = [p for p in save_dir.glob(ROLLBACK_PREFIX + "*") if p.is_dir() and not p.is_symlink()]
    return max(candidates, key=lambda p: p.name) if candidates else None


def run_hard_reset(config: dict[str, Any], server_stopped: bool, confirmed_save_path: str | None) -> dict[str, Any]:
    if not server_stopped:
        raise WorkerError("hard reset requires --confirm-server-stopped")
    _assert_game_closed()
    save_dir = _require_exact_test_path(config, confirmed_save_path)
    plan = plan_hard_reset(config)
    log_plan(plan)

    rollback_root = save_dir / f"{ROLLBACK_PREFIX}{_timestamp()}"
    if rollback_root.exists():
        raise WorkerError(f"{rollback_root} already exists; refusing to overwrite it")
    rollback_root.mkdir()
    try:
        for name in COPIED_FILES:
            if (save_dir / name).is_file():
                copy = rollback_root / name
                shutil.copy2(save_dir / name, copy)
                if copy.stat().st_size != (save_dir / name).stat().st_size:
                    raise WorkerError(f"copy of {name} does not match the original")
        _assert_game_closed()
        apply_moves(
            [(save_dir / item["name"], rollback_root / item["name"]) for item in plan["items"]],
            os.replace,
            (OSError, WorkerError),
            then=lambda: _write_pending(config, new_note(config, save_dir.name, rollback_root.name)),
        )
    except MoveFailed as failure:
        if not failure.recovered:
            raise critical("hard reset", failure, str(rollback_root)) from failure
        shutil.rmtree(rollback_root, ignore_errors=True)
        raise WorkerError(f"hard reset failed; nothing was changed: {failure.error}") from failure
    except (OSError, WorkerError) as error:
        shutil.rmtree(rollback_root, ignore_errors=True)
        raise WorkerError(f"hard reset failed; nothing was changed: {error}") from error

    _prune_older_hard_reset_sets(save_dir, rollback_root)
    LOG.warning("Hard reset applied: %d characters removed, world clock back to day 1.",
                plan["characters"] or 0)
    LOG.warning("Safehouses and factions are removed by the mod on the next server start.")
    LOG.warning("Undo is possible from %s", rollback_root.name)
    plan["applied_at"] = datetime.now(timezone.utc).isoformat()
    plan["rollback_dir"] = str(rollback_root)
    return plan


def run_hard_reset_rollback(config: dict[str, Any], server_stopped: bool, confirmed_save_path: str | None) -> Path:
    """Put the latest hard reset's files back. The state it replaces is kept as a new rollback."""
    if not server_stopped:
        raise WorkerError("undoing a hard reset requires --confirm-server-stopped")
    _assert_game_closed()
    save_dir = _require_exact_test_path(config, confirmed_save_path)
    source = latest_rollback(save_dir)
    if source is None:
        raise WorkerError("no hard reset rollback folder found in the save")
    entries = check_rollback_entries(source.name, (path.name for path in source.iterdir()))

    after_root = save_dir / f"{ROLLBACK_PREFIX}{_timestamp()}"
    if after_root.exists() or after_root == source:
        raise WorkerError(f"{after_root} already exists; try again in a second")
    after_root.mkdir()
    pairs = [(save_dir / name, after_root / name) for name in undo_swap_names(entries)
             if (save_dir / name).exists()]
    pairs += [(source / name, save_dir / name) for name in entries]
    try:
        apply_moves(pairs, os.replace, (OSError,))
    except MoveFailed as failure:
        if not failure.recovered:
            raise critical("undo", failure, f"{source} and {after_root}") from failure
        after_root.rmdir()
        raise WorkerError(f"undo failed; the save is unchanged: {failure.error}") from failure

    # The swap is done; nothing below may report the undo as failed.
    try:
        note = read_pending(config)
        if update_note_after_undo(note):
            _write_pending(config, note)
    except OSError:
        LOG.exception("Undo done, but the hard reset note could not be updated")
    try:
        source.rmdir()
    except OSError:
        LOG.warning("Undo done, but the empty folder %s could not be removed", source.name)
    LOG.warning("Hard reset undone. The state it replaced is kept in %s", after_root.name)
    return after_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hard Reset (Player Data only), local hosted server")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="show what would be removed (default)")
    action.add_argument("--apply", action="store_true", help="remove player data (game must be closed)")
    action.add_argument("--undo", action="store_true", help="restore the latest hard reset rollback")
    parser.add_argument("--confirm-server-stopped", action="store_true")
    parser.add_argument("--confirm-save-path", metavar="EXACT_SAVE_PATH")
    args = parser.parse_args(argv)
    try:
        config = load_config()
        require_local(config)
        if args.apply:
            run_hard_reset(config, args.confirm_server_stopped, args.confirm_save_path)
        elif args.undo:
            run_hard_reset_rollback(config, args.confirm_server_stopped, args.confirm_save_path)
        else:
            plan = plan_hard_reset(config)
            LOG.info("Hard reset dry run for %s (nothing is changed)", plan["server"])
            log_plan(plan)
            if plan["note"]:
                LOG.info("Last hard reset note: %s", json.dumps(plan["note"]))
        return 0
    except (ConfigError, WorkerError, OSError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
