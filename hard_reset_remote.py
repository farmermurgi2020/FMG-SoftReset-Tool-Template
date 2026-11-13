"""Hard Reset (Player Data only) for a Linux dedicated server over SFTP (run from GitHub Actions).

Same two parts as the local version (hard_reset.py), which also holds the shared rules:
1. With the server stopped, the player-only files are moved into a hardreset.rollback-<UTC time>
   folder inside the save, and map_meta.bin and global_mod_data.bin are copied there, so the
   hard reset can be undone.
2. A "pending" note is written next to the zone export (Zomboid/Lua). On the next server start
   the FMGResetTool mod removes all safehouses and factions (and Knox Claim claims) and marks
   the note done.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import posixpath
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paramiko

from config import ConfigError, load_config
from hard_reset import (
    COPIED_FILES,
    META_FILE,
    PENDING_FILE,
    PLAYER_ITEMS,
    ROLLBACK_PREFIX,
    MoveFailed,
    _timestamp,
    apply_moves,
    check_rollback_entries,
    count_characters,
    count_safehouses,
    critical,
    log_plan,
    new_note,
    parse_note,
    undo_swap_names,
    update_note_after_undo,
)
from worker import (
    WorkerError,
    _connect,
    _copy_remote_file,
    _listdir,
    _read_remote_file,
    _remote_join,
    _rename,
    _save_plan,
)


LOG = logging.getLogger("fmg-hard-reset")
CAUGHT = (OSError, WorkerError)


def note_path(config: dict[str, Any]) -> str:
    return _remote_join(posixpath.dirname(config["server"]["zone_export"]), PENDING_FILE)


def read_note(sftp: paramiko.SFTPClient, path: str) -> dict[str, Any] | None:
    try:
        data = _read_remote_file(sftp, path)
    except FileNotFoundError:
        return None
    except OSError:
        return {"status": "unreadable"}
    try:
        return parse_note(data.decode("utf-8"))
    except UnicodeDecodeError:
        return {"status": "unreadable"}


def write_note(sftp: paramiko.SFTPClient, path: str, value: dict[str, Any]) -> None:
    data = (json.dumps(value) + "\n").encode("utf-8")
    sftp.putfo(io.BytesIO(data), path, file_size=len(data), confirm=True)


def _entry_size(sftp: paramiko.SFTPClient, path: str, mode: int, size: int) -> tuple[int, int]:
    if not stat.S_ISDIR(mode):
        return 1, size
    files = total = 0
    for entry in _listdir(sftp, path):
        child_files, child_bytes = _entry_size(
            sftp, _remote_join(path, entry.filename), entry.st_mode or 0, entry.st_size or 0)
        files += child_files
        total += child_bytes
    return files, total


def _character_count(sftp: paramiko.SFTPClient, path: str) -> int | None:
    try:
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / "players.db"
            sftp.get(path, str(local))  # streamed to disk, not held in memory
            return count_characters(local, immutable=True)
    except OSError:
        return None


def _safehouse_count(sftp: paramiko.SFTPClient, zone_export: str) -> int | None:
    try:
        return count_safehouses(_read_remote_file(sftp, zone_export).decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _remove_tree(sftp: paramiko.SFTPClient, path: str) -> int:
    """Delete a small folder on the open connection (a hard reset rollback is a few dozen files,
    not worth the 16 extra SSH logins of worker._remote_rmtree)."""
    removed = 0
    for entry in _listdir(sftp, path):
        child = _remote_join(path, entry.filename)
        if stat.S_ISDIR(entry.st_mode or 0):
            removed += _remove_tree(sftp, child)
        else:
            sftp.remove(child)
            removed += 1
    sftp.rmdir(path)
    return removed


def _save_plan_after_change(plan: dict[str, Any]) -> None:
    """The server files are already changed; a plan file that can't be written is only a warning."""
    try:
        _save_plan(plan)
    except WorkerError:
        LOG.exception("The server files were changed, but the run summary could not be saved")


def _hard_reset_sets(listing: list[paramiko.SFTPAttributes]) -> list[str]:
    return sorted(entry.filename for entry in listing
                  if entry.filename.startswith(ROLLBACK_PREFIX) and stat.S_ISDIR(entry.st_mode or 0))


def plan_hard_reset(config: dict[str, Any], sftp: paramiko.SFTPClient,
                    listing: list[paramiko.SFTPAttributes]) -> dict[str, Any]:
    save_dir = config["server"]["save_dir"]
    present = {entry.filename: entry for entry in listing}
    if META_FILE not in present:
        raise WorkerError(f"{META_FILE} not found in {save_dir}; is this the right save folder?")
    items = []
    for name, label in PLAYER_ITEMS:
        entry = present.get(name)
        if entry is None:
            continue
        if stat.S_ISLNK(entry.st_mode or 0):
            raise WorkerError(f"{name} is a symlink; refusing hard reset")
        files, size = _entry_size(sftp, _remote_join(save_dir, name), entry.st_mode or 0, entry.st_size or 0)
        items.append({"name": name, "label": label, "files": files, "bytes": size})
    return {
        "action": "hard-reset",
        "server": config["server"]["name"],
        "build": config["world"].get("build", ""),
        "save_dir": save_dir,
        "items": items,
        "copied": {name: present[name].st_size for name in COPIED_FILES if name in present},
        "characters": (_character_count(sftp, _remote_join(save_dir, "players.db"))
                       if "players.db" in present else 0),
        "safehouses": _safehouse_count(sftp, config["server"]["zone_export"]),
        "note": read_note(sftp, note_path(config)),
    }


def run_hard_reset(config: dict[str, Any]) -> dict[str, Any]:
    client, sftp = _connect(config)
    try:
        save_dir = config["server"]["save_dir"]
        listing = _listdir(sftp, save_dir)
        plan = plan_hard_reset(config, sftp, listing)
        log_plan(plan)
        rollback_name = f"{ROLLBACK_PREFIX}{_timestamp()}"
        rollback_root = _remote_join(save_dir, rollback_name)
        sftp.mkdir(rollback_root)  # fails if it already exists
        try:
            for name, size in plan["copied"].items():
                copied = _copy_remote_file(sftp, _remote_join(save_dir, name), _remote_join(rollback_root, name))
                if copied != size:
                    raise WorkerError(f"copy of {name} does not match the original")
            save_name = posixpath.basename(save_dir.rstrip("/"))
            apply_moves(
                [(_remote_join(save_dir, item["name"]), _remote_join(rollback_root, item["name"]))
                 for item in plan["items"]],
                lambda source, target: _rename(sftp, source, target),
                CAUGHT,
                then=lambda: write_note(sftp, note_path(config), new_note(config, save_name, rollback_name)),
            )
        except MoveFailed as failure:
            if not failure.recovered:
                raise critical("hard reset", failure, rollback_root) from failure
            _discard(sftp, rollback_root)
            raise WorkerError(f"hard reset failed; nothing was changed: {failure.error}") from failure
        except CAUGHT as error:
            _discard(sftp, rollback_root)
            raise WorkerError(f"hard reset failed; nothing was changed: {error}") from error

        for name in _hard_reset_sets(listing):  # older rollbacks; the new one is not in this listing
            try:
                removed = _remove_tree(sftp, _remote_join(save_dir, name))
                LOG.info("Removed older hard reset rollback %s (%d files)", name, removed)
            except CAUGHT:
                LOG.exception("Could not remove older hard reset rollback %s", name)
        plan["applied_at"] = datetime.now(timezone.utc).isoformat()
        plan["remote_rollback_dir"] = rollback_root
        _save_plan_after_change(plan)
        LOG.warning("Hard reset applied: %s characters removed, world clock back to day 1.",
                    plan["characters"] if plan["characters"] is not None else "all")
        LOG.warning("Safehouses and factions are removed by the FMGResetTool mod on the next server start.")
        LOG.warning("Undo is possible from %s", rollback_root)
        return plan
    finally:
        sftp.close()
        client.close()


def _discard(sftp: paramiko.SFTPClient, path: str) -> None:
    try:
        _remove_tree(sftp, path)
    except CAUGHT:
        LOG.warning("Could not remove %s; delete it manually", path)


def run_hard_reset_undo(config: dict[str, Any]) -> dict[str, Any]:
    client, sftp = _connect(config)
    try:
        save_dir = config["server"]["save_dir"]
        listing = _listdir(sftp, save_dir)
        sets = _hard_reset_sets(listing)
        if not sets:
            raise WorkerError("no hard reset rollback folder found in the save")
        source_name = sets[-1]
        source = _remote_join(save_dir, source_name)
        entries = check_rollback_entries(source_name, (entry.filename for entry in _listdir(sftp, source)))
        after_name = f"{ROLLBACK_PREFIX}{_timestamp()}"
        if after_name == source_name:
            raise WorkerError(f"{after_name} already exists; try again in a second")
        after_root = _remote_join(save_dir, after_name)
        sftp.mkdir(after_root)  # fails if it already exists
        current = {entry.filename for entry in listing}
        pairs = [(_remote_join(save_dir, name), _remote_join(after_root, name))
                 for name in undo_swap_names(entries) if name in current]
        pairs += [(_remote_join(source, name), _remote_join(save_dir, name)) for name in entries]
        try:
            apply_moves(pairs, lambda a, b: _rename(sftp, a, b), CAUGHT)
        except MoveFailed as failure:
            if not failure.recovered:
                raise critical("undo", failure, f"{source} and {after_root}") from failure
            sftp.rmdir(after_root)
            raise WorkerError(f"undo failed; the save is unchanged: {failure.error}") from failure

        # The swap is done; nothing below may report the undo as failed.
        try:
            path = note_path(config)
            note = read_note(sftp, path)
            if update_note_after_undo(note):
                write_note(sftp, path, note)
        except CAUGHT:
            LOG.exception("Undo done, but the hard reset note could not be updated")
        try:
            sftp.rmdir(source)
        except OSError:
            LOG.warning("Undo done, but the empty folder %s could not be removed", source)
        plan = {
            "action": "hard-reset-undo",
            "server": config["server"]["name"],
            "build": config["world"].get("build", ""),
            "restored_from": source_name,
            "restored_entries": entries,
            "remote_rollback_dir": after_root,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_plan_after_change(plan)
        LOG.warning("Hard reset undone from %s. The state it replaced is kept in %s", source_name, after_root)
        return plan
    finally:
        sftp.close()
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hard Reset (Player Data only), dedicated server over SFTP")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true")
    action.add_argument("--undo", action="store_true")
    parser.add_argument("--confirm-server-name", default="")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config()
        if config["server"].get("connection_mode") != "sftp":
            raise WorkerError("the dedicated hard reset requires connection_mode='sftp'")
        if args.confirm_server_name.strip() != config["server"]["name"]:
            raise WorkerError(
                f"hard reset requires --confirm-server-name to exactly match server.name "
                f"({config['server']['name']!r})"
            )
        if args.apply:
            run_hard_reset(config)
        else:
            run_hard_reset_undo(config)
        return 0
    except (ConfigError, WorkerError, OSError, paramiko.SSHException) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
