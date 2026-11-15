from __future__ import annotations

import argparse
import errno
import io
import json
import logging
import os
import posixpath
import re
import stat
import tempfile
import threading
import time as time_module
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable

import paramiko

from config import ROOT, ConfigError, load_config, merge_exported_worker_settings, target_modes
from geometry import GeometryError, Rect, covered_grid_units, protected_chunks, rectangles_from_export
from schedule import validate_timezone


LOG = logging.getLogger("fmg-soft-reset")
PLAN_PATH = Path(os.environ.get("PZRESET_PLAN", ROOT / "reset-plan.json"))
CHUNK_NAME = re.compile(r"-?\d+\.bin\Z")
COORD_NAME = re.compile(r"-?\d+\Z")
LAYOUT_APPROVAL = "pz-b42-map-x-y-bin-v1"
MAX_ZONE_FILE_BYTES = 16 * 1024 * 1024
SFTP_REQUEST_TIMEOUT = 60  # seconds per SFTP request
SFTP_WORKERS = max(1, min(32, int(os.environ.get("PZRESET_SFTP_WORKERS", "16"))))


class WorkerError(RuntimeError):
    pass


class Chunk:
    def __init__(self, x: int, y: int, path: str, size: int, mtime: int):
        self.x = x
        self.y = y
        self.path = path
        self.size = size
        self.mtime = mtime

    def key(self) -> tuple[int, int]:
        return self.x, self.y


def atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
        ) as handle:
            temporary = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _remote_join(*parts: str) -> str:
    path = posixpath.normpath(posixpath.join(*parts))
    if not path.startswith("/"):
        raise WorkerError("remote paths must be absolute")
    return path


def _connect(config: dict[str, Any]) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
    server = config["server"]
    host = os.environ.get("PZRESET_SFTP_HOST", server.get("sftp_host", "")).strip()
    username = os.environ.get("PZRESET_SFTP_USERNAME", server.get("sftp_username", "")).strip()
    key_file = os.environ.get("PZRESET_SFTP_KEY_FILE", server.get("sftp_key_file", "")).strip()
    known_hosts_file = os.environ.get("PZRESET_SFTP_KNOWN_HOSTS", "").strip()
    password = os.environ.get("PZRESET_SFTP_PASSWORD")
    passphrase = os.environ.get("PZRESET_SFTP_KEY_PASSPHRASE")
    port = int(os.environ.get("PZRESET_SFTP_PORT", server.get("sftp_port", 22)))
    if not host or not username or (not key_file and not password):
        raise WorkerError(
            "configure SFTP host/username and provide PZRESET_SFTP_KEY_FILE "
            "or PZRESET_SFTP_PASSWORD on the VPS"
        )
    if not 1 <= port <= 65535:
        raise WorkerError("SFTP port must be between 1 and 65535")

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if known_hosts_file:
        try:
            client.load_host_keys(known_hosts_file)
        except OSError as exc:
            raise WorkerError(f"cannot load configured SFTP known_hosts file: {exc}") from exc
    if known_hosts_file:
        # A configured pin is enforced strictly; don't silently accept a mismatch.
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        # Some game hosts provide SFTP login details but no host-key pin.
        # Continue with an explicit warning when no pin was configured.
        client.set_missing_host_key_policy(paramiko.WarningPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            key_filename=key_file or None,
            passphrase=passphrase,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=20,
            banner_timeout=20,
            auth_timeout=20,
        )
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise WorkerError("SFTP SSH transport is not active")
        transport.set_keepalive(15)
        return client, _open_sftp(client)
    except Exception:
        client.close()
        raise


def _listdir(sftp: paramiko.SFTPClient, remote_dir: str) -> list[paramiko.SFTPAttributes]:
    try:
        entries = sftp.listdir_attr(remote_dir)
    except OSError as exc:
        raise WorkerError(f"cannot list remote directory {remote_dir}: {exc}") from exc
    return entries


def scan_chunks(sftp: paramiko.SFTPClient, map_dir: str, *, allow_empty: bool = False) -> tuple[Chunk, ...]:
    try:
        root_attributes = sftp.stat(map_dir)
    except OSError as exc:
        raise WorkerError(f"map directory is unavailable at {map_dir}: {exc}") from exc
    if not stat.S_ISDIR(root_attributes.st_mode):
        raise WorkerError(f"map path is not a directory: {map_dir}")

    chunks: list[Chunk] = []
    x_entries = _listdir(sftp, map_dir)
    LOG.info("Scanning %s: %d chunk folders (this can take a few minutes)", map_dir, len(x_entries))
    for index, x_entry in enumerate(x_entries, start=1):
        if index % 200 == 0:
            LOG.info("Scanned %d/%d chunk folders", index, len(x_entries))
        x_name = x_entry.filename
        if not COORD_NAME.fullmatch(x_name) or not stat.S_ISDIR(x_entry.st_mode):
            raise WorkerError(f"unexpected entry in map directory: {x_name}")
        chunk_x = int(x_name)
        if str(chunk_x) != x_name:
            raise WorkerError(f"non-canonical chunk directory coordinate: {x_name}")
        x_path = _remote_join(map_dir, x_name)
        y_entries = _listdir(sftp, x_path)
        if not y_entries:
            raise WorkerError(f"empty chunk directory: {x_path}")
        seen_y: set[int] = set()
        for entry in y_entries:
            if (
                not CHUNK_NAME.fullmatch(entry.filename)
                or not stat.S_ISREG(entry.st_mode)
            ):
                raise WorkerError(f"unexpected entry in chunk directory: {x_path}/{entry.filename}")
            y = int(entry.filename[:-4])
            if str(y) + ".bin" != entry.filename or y in seen_y:
                raise WorkerError(f"duplicate or non-canonical chunk coordinate: {x_path}/{entry.filename}")
            seen_y.add(y)
            if entry.st_size <= 0:
                raise WorkerError(f"empty chunk file: {x_path}/{entry.filename}")
            chunks.append(
                Chunk(
                    chunk_x,
                    y,
                    _remote_join(x_path, entry.filename),
                    int(entry.st_size),
                    int(entry.st_mtime),
                )
            )
    if not chunks and not allow_empty:
        raise WorkerError("map directory contains no chunk files; refusing reset")
    LOG.info("Scan of %s complete: %d chunks", map_dir, len(chunks))
    return tuple(sorted(chunks, key=lambda item: (item.x, item.y)))


def _read_remote_json(sftp: paramiko.SFTPClient, path: str) -> dict[str, Any]:
    try:
        attributes = sftp.stat(path)
        if attributes.st_size > MAX_ZONE_FILE_BYTES:
            raise WorkerError(f"zone export exceeds {MAX_ZONE_FILE_BYTES} bytes")
        with sftp.open(path, "rb") as handle:
            raw = handle.read(MAX_ZONE_FILE_BYTES + 1)
    except WorkerError:
        raise
    except OSError as exc:
        raise WorkerError(f"cannot read live zone export {path}: {exc}") from exc
    if len(raw) > MAX_ZONE_FILE_BYTES:
        raise WorkerError("zone export is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerError(f"live zone export is invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkerError("live zone export must be a JSON object")
    return data


def _validate_zone_export(
    export: dict[str, Any], config: dict[str, Any], *, require_zones: bool = True
) -> list[Rect]:
    schema_version = export.get("schemaVersion")
    if schema_version not in {1, 2}:
        raise WorkerError("unsupported or missing live zone export schemaVersion")
    build = export.get("build")
    if build != config["world"]["build"]:
        raise WorkerError(
            f"live zone export build {build!r} does not match configured build "
            f"{config['world']['build']!r}"
        )
    generated = export.get("generatedAt")
    if not isinstance(generated, str):
        raise WorkerError("live zone export has no generatedAt timestamp")
    try:
        generated_at = datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkerError("live zone export generatedAt is not ISO-8601") from exc
    if generated_at.tzinfo is None:
        raise WorkerError("live zone export timestamp must include a timezone")
    try:
        merge_exported_worker_settings(config, export)
    except ConfigError as exc:
        raise WorkerError(f"invalid worker settings in live export: {exc}") from exc
    if (generated_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds() > 300:
        raise WorkerError("live zone export timestamp is more than 5 minutes in the future")
    zones = export.get("zones")
    if not isinstance(zones, list):
        raise WorkerError("live zone export must contain a zones array")
    eligible_zones = []
    for zone in zones:
        if not isinstance(zone, dict):
            raise WorkerError("each live zone must be an object")
        source = zone.get("source")
        if source == "non-pvp":
            continue
        if schema_version == 2 and source not in {"safehouse", "tool"}:
            raise WorkerError(f"unsupported protected-zone source in schemaVersion 2 export: {source!r}")
        eligible_zones.append(zone)
    try:
        live_rects = rectangles_from_export({"zones": eligible_zones})
    except (GeometryError, TypeError) as exc:
        raise WorkerError(f"invalid zone bounds in live export: {exc}") from exc
    if not live_rects and require_zones:
        raise WorkerError("live zone export contains no zones; refusing to reset")
    return live_rects


def load_remote_worker_settings(config: dict[str, Any]) -> dict[str, Any]:
    client, sftp = _connect(config)
    try:
        export = _read_remote_json(sftp, config["server"]["zone_export"])
        modes = target_modes(config)
        _validate_zone_export(export, config, require_zones="protect" in modes.values())
        if all(mode == "skip" for mode in modes.values()):
            raise WorkerError("every reset target is set to skip; nothing to reset")
        return config
    finally:
        sftp.close()
        client.close()


def create_chunk_plan(
    chunks: Iterable[Chunk], rects: list[Rect], chunk_size: int, *, protect: bool = True
) -> tuple[list[Chunk], list[Chunk]]:
    all_chunks = list(chunks)
    if not protect:
        # Full reset: nothing is kept.
        return [], all_chunks
    protected = protected_chunks((chunk.key() for chunk in all_chunks), rects, chunk_size)
    keep = [chunk for chunk in all_chunks if chunk.key() in protected]
    remove = [chunk for chunk in all_chunks if chunk.key() not in protected]
    if not keep:
        raise WorkerError("no chunks intersect protected zones; refusing a destructive plan")
    if not remove:
        raise WorkerError("no unprotected chunks remain to reset")
    return keep, remove


def _plan_data(
    config: dict[str, Any],
    chunks: tuple[Chunk, ...],
    keep: list[Chunk],
    remove: list[Chunk],
    export: dict[str, Any],
    live_rects: list[Rect],
) -> dict[str, Any]:
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "server": config["server"]["name"],
        "build": config["world"]["build"],
        "zone_export_generated_at": export["generatedAt"],
        "chunk_size": config["world"]["chunk_size"],
        "total_chunks": len(chunks),
        "protected_count": len(keep),
        "protected_cell_count": len(covered_grid_units(live_rects, 256)),
        "remove_count": len(remove),
        "delete_fraction": len(remove) / len(chunks),
        "keep": [[chunk.x, chunk.y] for chunk in keep],
        "remove": [[chunk.x, chunk.y] for chunk in remove],
    }


def _save_plan(plan: dict[str, Any]) -> None:
    try:
        atomic_write_json(PLAN_PATH, plan)
    except OSError as exc:
        raise WorkerError(f"cannot save local plan {PLAN_PATH}: {exc}") from exc



def _map_manifest(chunks: Iterable[Chunk]) -> tuple[tuple[int, int, int, int], ...]:
    return tuple((item.x, item.y, item.size, item.mtime) for item in chunks)


ROLLBACK_PREFIX = "softreset.rollback-"
ROLLBACK_NAME = re.compile(r"softreset\.rollback-\d{8}T\d{6}Z\Z")
# Older rollback layouts are pruned too, so only one rollback set stays on the server.
PRUNABLE_PREFIXES = (
    ROLLBACK_PREFIX,
    "softreset.before-rollback-",
    "map.rollback-",
    "map.before-rollback-",
)
ROLLBACK_FOLDERS = {"map", "zpop", "apop"}


def _in_provider_window(now: datetime, schedule: dict[str, Any]) -> bool:
    stop = time.fromisoformat(schedule["pine_stop_time"])
    start = time.fromisoformat(schedule["pine_start_time"])
    buffer = schedule["minimum_start_buffer_minutes"]
    minute = now.hour * 60 + now.minute
    stop_minute = stop.hour * 60 + stop.minute
    start_minute = start.hour * 60 + start.minute
    if stop_minute < start_minute:
        return stop_minute + buffer <= minute <= start_minute - buffer
    return minute >= stop_minute + buffer or minute <= start_minute - buffer


def _ensure_before_provider_start(config: dict[str, Any]) -> None:
    timezone_info = validate_timezone(config["schedule"]["timezone"])
    now = datetime.now(timezone_info)
    if not _in_provider_window(now, config["schedule"]):
        raise WorkerError(
            "the scheduled Pine Hosting maintenance window ended before the map swap; "
            "the original map is unchanged"
        )


# Build 42 population files are indexed by 256x256-tile cells (IsoCell.CELL_SIZE_IN_SQUARES).
POPULATION_CELL_SIZE = 256
POPULATION_TARGETS = {
    "zombie_population": ("zpop", "zpop"),
    "animal_population": ("apop", "apop"),
}


def scan_population_cells(
    sftp: paramiko.SFTPClient, directory: str, prefix: str
) -> tuple[tuple[Chunk, ...], tuple[tuple[str, int], ...]]:
    """Return (per-cell files, shared world-level files as (name, size))."""
    try:
        attributes = sftp.stat(directory)
    except OSError as exc:
        raise WorkerError(f"{prefix} population directory is unavailable at {directory}: {exc}") from exc
    if not stat.S_ISDIR(attributes.st_mode):
        raise WorkerError(f"{prefix} population path is not a directory: {directory}")
    pattern = re.compile(rf"{re.escape(prefix)}_(-?\d+)_(-?\d+)\.bin\Z")
    cells: list[Chunk] = []
    shared: list[tuple[str, int]] = []
    seen: set[tuple[int, int]] = set()
    for entry in _listdir(sftp, directory):
        name = entry.filename
        if not stat.S_ISREG(entry.st_mode):
            raise WorkerError(f"unexpected entry in {prefix} population directory: {name}")
        match = pattern.fullmatch(name)
        if not match:
            # World-level files (for example zpop_virtual.bin, zpop_ids.bin) cannot be
            # filtered by location, so they are always kept.
            shared.append((name, int(entry.st_size)))
            continue
        cell_x, cell_y = int(match.group(1)), int(match.group(2))
        if name != f"{prefix}_{cell_x}_{cell_y}.bin" or (cell_x, cell_y) in seen:
            raise WorkerError(f"duplicate or non-canonical population cell file: {directory}/{name}")
        seen.add((cell_x, cell_y))
        if entry.st_size <= 0:
            raise WorkerError(f"empty population cell file: {directory}/{name}")
        cells.append(Chunk(cell_x, cell_y, _remote_join(directory, name), int(entry.st_size), int(entry.st_mtime)))
    return tuple(sorted(cells, key=lambda item: (item.x, item.y))), tuple(sorted(shared))


def _build_remote_plan(
    sftp: paramiko.SFTPClient, config: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Plan every selected target. Returns (plan, targets keyed by save folder name)."""
    save_dir = config["server"]["save_dir"]
    modes = target_modes(config)
    export = _read_remote_json(sftp, config["server"]["zone_export"])
    live_rects = _validate_zone_export(export, config, require_zones="protect" in modes.values())
    reset_targets = {key: mode != "skip" for key, mode in modes.items()}
    if not any(reset_targets.values()):
        raise WorkerError("every reset target is set to skip; nothing to reset")
    targets: dict[str, dict[str, Any]] = {}
    target_counts: dict[str, dict[str, Any]] = {}
    chunks: tuple[Chunk, ...] = ()
    keep: list[Chunk] = []
    remove: list[Chunk] = []
    if reset_targets["map_chunks"]:
        map_dir = _remote_join(save_dir, "map")
        chunks = scan_chunks(sftp, map_dir)
        keep, remove = create_chunk_plan(
            chunks, live_rects, config["world"]["chunk_size"], protect=modes["map_chunks"] == "protect"
        )
        targets["map"] = {"path": map_dir, "prefix": None, "keep": keep, "remove": remove, "shared": ()}
        target_counts["map_chunks"] = {"total": len(chunks), "preserved": len(keep), "reset": len(remove)}
    else:
        target_counts["map_chunks"] = {"total": 0, "preserved": 0, "reset": 0}
    for setting, (folder, prefix) in POPULATION_TARGETS.items():
        if not reset_targets[setting]:
            target_counts[setting] = {"total": 0, "preserved": 0, "reset": 0}
            continue
        directory = _remote_join(save_dir, folder)
        cells, shared = scan_population_cells(sftp, directory, prefix)
        if not cells:
            raise WorkerError(f"{setting} is selected but {directory} contains no population cell files")
        if modes[setting] == "protect":
            protected = protected_chunks(((item.x, item.y) for item in cells), live_rects, POPULATION_CELL_SIZE)
        else:
            protected = set()
        keep_cells = [item for item in cells if item.key() in protected]
        remove_cells = [item for item in cells if item.key() not in protected]
        targets[folder] = {
            "path": directory, "prefix": prefix, "keep": keep_cells, "remove": remove_cells, "shared": shared,
        }
        target_counts[setting] = {
            "total": len(cells),
            "preserved": len(keep_cells),
            "reset": len(remove_cells),
            "shared_files_preserved": len(shared),
            "shared_file_names": [name for name, _size in shared],
            "shared_file_sizes": {name: size for name, size in shared},
        }
    if reset_targets["map_chunks"] and chunks:
        plan = _plan_data(config, chunks, keep, remove, export, live_rects)
    else:
        plan = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "server": config["server"]["name"],
            "build": config["world"]["build"],
            "zone_export_generated_at": export["generatedAt"],
            "chunk_size": config["world"]["chunk_size"],
            "total_chunks": 0,
            "protected_count": 0,
            "protected_cell_count": len(covered_grid_units(live_rects, POPULATION_CELL_SIZE)),
            "remove_count": 0,
            "delete_fraction": 0.0,
            "keep": [],
            "remove": [],
        }
    plan["connection_mode"] = "sftp"
    plan["reset_targets"] = dict(reset_targets)
    plan["target_modes"] = dict(modes)
    plan["target_counts"] = target_counts
    plan["population_cell_size"] = POPULATION_CELL_SIZE
    plan["selected_directories"] = list(targets)
    return plan, targets


MODE_LABELS = {
    "protect": "reset, keep protected zones",
    "reset_all": "reset everything",
    "skip": "not touched",
}


def _log_target_counts(plan: dict[str, Any], verb: str) -> None:
    for setting, counts in plan["target_counts"].items():
        if not plan["reset_targets"].get(setting):
            continue
        mode = plan.get("target_modes", {}).get(setting, "protect")
        LOG.info(
            "%s %s (%s): %d total, %d preserved, %d %s",
            verb, setting, MODE_LABELS.get(mode, mode), counts["total"], counts["preserved"], counts["reset"],
            "planned for reset" if verb == "Dry run" else "to reset",
        )
        if counts.get("shared_files_preserved"):
            sizes = counts.get("shared_file_sizes", {})
            LOG.info("%s: keeping shared files that cannot be filtered by location: %s",
                     setting, ", ".join(
                         f"{name} ({sizes[name] / (1024 * 1024):.1f} MB)" if name in sizes else name
                         for name in counts["shared_file_names"]))


def run_dry_run(config: dict[str, Any]) -> dict[str, Any]:
    client, sftp = _connect(config)
    try:
        plan, _targets = _build_remote_plan(sftp, config)
        plan["action"] = "dry-run"
        _save_plan(plan)
        LOG.info(
            "Dry run for %s / build %s: %d protected 256-tile cells",
            config["server"]["name"], config["world"]["build"], plan["protected_cell_count"],
        )
        _log_target_counts(plan, "Dry run")
        LOG.info("Plan saved at %s; no remote files were modified", PLAN_PATH)
        return plan
    finally:
        sftp.close()
        client.close()


def _exists(sftp: paramiko.SFTPClient, path: str) -> bool:
    try:
        sftp.stat(path)
    except OSError as exc:
        if exc.errno == errno.ENOENT or "no such file" in str(exc).lower():
            return False
        raise WorkerError(f"cannot check remote path {path}: {exc}") from exc
    return True


def _rename(sftp: paramiko.SFTPClient, source: str, target: str) -> None:
    if _exists(sftp, target):
        raise WorkerError(f"rename target already exists: {target}")
    try:
        sftp.rename(source, target)
    except OSError as exc:
        raise WorkerError(f"cannot rename {source} to {target}: {exc}") from exc


def _open_sftp(client: paramiko.SSHClient) -> paramiko.SFTPClient:
    """Open an SFTP channel; opening and each request fail instead of hanging forever."""
    transport = client.get_transport()
    if transport is None or not transport.is_active():
        raise WorkerError("SFTP SSH transport is not active")
    channel = transport.open_session(timeout=SFTP_REQUEST_TIMEOUT)
    channel.settimeout(SFTP_REQUEST_TIMEOUT)
    channel.invoke_subsystem("sftp")
    return paramiko.SFTPClient(channel)


def _read_remote_file(sftp: paramiko.SFTPClient, path: str) -> bytes:
    # Pipelined read-ahead: a plain read() fetches 8 KB per round trip, which is far too
    # slow for large world-level files such as zpop_virtual.bin.
    buffer = io.BytesIO()
    sftp.getfo(path, buffer, prefetch=True, max_concurrent_prefetch_requests=64)
    return buffer.getvalue()


LARGE_FILE_BYTES = 8 * 1024 * 1024


def _copy_remote_file(channel: paramiko.SFTPClient, source: str, target: str) -> int:
    started = time_module.monotonic()
    data = _read_remote_file(channel, source)
    channel.putfo(io.BytesIO(data), target, file_size=len(data), confirm=True)
    if len(data) >= LARGE_FILE_BYTES:
        LOG.info(
            "Copied %s (%.1f MB) in %.0fs",
            posixpath.basename(source), len(data) / (1024 * 1024), time_module.monotonic() - started,
        )
    return len(data)


def _run_parallel(config: dict[str, Any], items: list[Any], task, label: str) -> None:
    """Run task(sftp, item) for every item over several separate SFTP logins.

    Each SFTP request waits a full network round trip; Pine Hosting saves have
    hundreds of thousands of chunk files, so one-at-a-time transfers are too slow.
    Pine's SFTP server does not answer a second channel on one SSH login, so each
    worker opens its own connection.
    """
    items = list(items)
    if not items:
        return
    workers = min(SFTP_WORKERS, len(items))
    stop = threading.Event()
    lock = threading.Lock()
    progress = [0]
    step = max(1000, len(items) // 10)

    def run(batch: list[Any]) -> None:
        worker_client, channel = _connect(config)
        try:
            for item in batch:
                if stop.is_set():
                    return
                task(channel, item)
                with lock:
                    progress[0] += 1
                    done = progress[0]
                if done % step == 0:
                    LOG.info("%s: %d/%d", label, done, len(items))
        except BaseException:
            stop.set()
            raise
        finally:
            channel.close()
            worker_client.close()

    LOG.info("%s: %d items using %d SFTP connections", label, len(items), workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run, items[index::workers]) for index in range(workers)]
    for future in futures:
        future.result()
    LOG.info("%s: %d/%d done", label, progress[0], len(items))


def _remote_rmtree(config: dict[str, Any], sftp: paramiko.SFTPClient, path: str) -> int:
    """Delete a remote directory tree; returns the number of files removed."""
    files: list[str] = []
    directories: list[str] = []

    def walk(directory: str) -> None:
        directories.append(directory)
        for entry in _listdir(sftp, directory):
            name = entry.filename
            if name in {".", ".."} or "/" in name:
                raise WorkerError(f"invalid remote path entry under {directory}")
            child = _remote_join(directory, name)
            if stat.S_ISDIR(entry.st_mode):
                walk(child)
            else:
                files.append(child)

    walk(path)
    _run_parallel(config, files, lambda channel, item: channel.remove(item), f"Deleting {path}")
    for directory in reversed(directories):
        sftp.rmdir(directory)
    return len(files)


def _discard_stage(config: dict[str, Any], sftp: paramiko.SFTPClient, stage_dir: str) -> None:
    try:
        if _exists(sftp, stage_dir):
            _remote_rmtree(config, sftp, stage_dir)
    except (OSError, WorkerError):
        LOG.exception("Could not remove staging directory %s; delete it manually", stage_dir)


def _stage_protected_chunks(
    config: dict[str, Any], sftp: paramiko.SFTPClient, keep: list[Chunk], stage_dir: str
) -> None:
    """Copy protected chunks into a fresh map folder on the server (SFTP has no server-side copy)."""
    sftp.mkdir(stage_dir)
    for chunk_x in sorted({chunk.x for chunk in keep}):
        sftp.mkdir(_remote_join(stage_dir, str(chunk_x)))
    copied: dict[tuple[int, int], int] = {}

    def copy(channel: paramiko.SFTPClient, chunk: Chunk) -> None:
        data = _read_remote_file(channel, chunk.path)
        if not data:
            raise WorkerError(f"protected chunk read back empty: {chunk.path}")
        target = _remote_join(stage_dir, str(chunk.x), f"{chunk.y}.bin")
        # confirm=True makes paramiko stat the upload and compare its size.
        channel.putfo(io.BytesIO(data), target, file_size=len(data), confirm=True)
        copied[(chunk.x, chunk.y)] = len(data)

    _run_parallel(config, keep, copy, "Staging protected map chunks")
    staged = scan_chunks(sftp, stage_dir, allow_empty=not keep)
    if {(item.x, item.y, item.size) for item in staged} != {
        (x, y, size) for (x, y), size in copied.items()
    }:
        raise WorkerError("staged map does not match the protected chunks that were copied")


def _stage_population_files(
    config: dict[str, Any], sftp: paramiko.SFTPClient, target: dict[str, Any], stage_dir: str
) -> None:
    """Copy protected population cells plus shared world-level files into a fresh folder."""
    sftp.mkdir(stage_dir)
    pairs = [(cell.path, posixpath.basename(cell.path)) for cell in target["keep"]]
    pairs += [(_remote_join(target["path"], name), name) for name, _size in target["shared"]]
    copied: dict[str, int] = {}

    def copy(channel: paramiko.SFTPClient, pair: tuple[str, str]) -> None:
        source, name = pair
        copied[name] = _copy_remote_file(channel, source, _remote_join(stage_dir, name))

    _run_parallel(config, pairs, copy, f"Staging protected {target['prefix']} files")
    staged = {entry.filename: int(entry.st_size) for entry in _listdir(sftp, stage_dir)}
    if staged != copied:
        raise WorkerError(f"staged {target['prefix']} files do not match the files that were copied")


def _stage_target(config: dict[str, Any], sftp: paramiko.SFTPClient, target: dict[str, Any], stage_dir: str) -> None:
    if target["prefix"] is None:
        _stage_protected_chunks(config, sftp, target["keep"], stage_dir)
    else:
        _stage_population_files(config, sftp, target, stage_dir)


STAGING_NAME = re.compile(r"\.(map|zpop|apop)\.pz-reset-staging-\d{8}T\d{6}Z\Z")


def _remove_stale_staging(config: dict[str, Any], sftp: paramiko.SFTPClient, save_dir: str) -> None:
    """Delete staging folders left behind by an earlier run that failed before its swap."""
    for entry in _listdir(sftp, save_dir):
        if STAGING_NAME.fullmatch(entry.filename) and stat.S_ISDIR(entry.st_mode):
            LOG.info("Removing leftover staging folder from an earlier run: %s", entry.filename)
            _discard_stage(config, sftp, _remote_join(save_dir, entry.filename))


DELETE_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 30


def _delete_with_retries(config: dict[str, Any], sftp: paramiko.SFTPClient, path: str, what: str) -> bool:
    """Delete a folder the game never reads (old rollback set, leftover staging), retrying only the delete.

    A dropped SFTP connection shows up as a timeout part-way through; later attempts use a
    fresh login and continue with whatever is left. Returns False if every attempt failed.
    """
    for attempt in range(1, DELETE_ATTEMPTS + 1):
        client = None
        try:
            if attempt == 1:
                channel = sftp
            else:
                client, channel = _connect(config)
            if not _exists(channel, path):
                LOG.info("%s %s is already gone", what, path)
                return True
            removed = _remote_rmtree(config, channel, path)
            LOG.info("Removed %s %s (%d files, attempt %d of %d)", what, path, removed, attempt, DELETE_ATTEMPTS)
            return True
        except (OSError, WorkerError, paramiko.SSHException) as exc:
            if attempt == DELETE_ATTEMPTS:
                LOG.error("Could not remove %s %s after %d attempts: %s: %s",
                          what, path, DELETE_ATTEMPTS, type(exc).__name__, exc)
                return False
            LOG.warning("Removing %s %s failed (attempt %d of %d: %s); retrying in %ds",
                        what, path, attempt, DELETE_ATTEMPTS, type(exc).__name__, RETRY_DELAY_SECONDS)
            time_module.sleep(RETRY_DELAY_SECONDS)
        finally:
            if client is not None:
                client.close()
    return False


def _older_rollback_sets(sftp: paramiko.SFTPClient, save_dir: str, keep_name: str | None) -> list[str]:
    names = []
    for entry in _listdir(sftp, save_dir):
        name = entry.filename
        if name == keep_name or not name.startswith(PRUNABLE_PREFIXES):
            continue
        if not stat.S_ISDIR(entry.st_mode):
            LOG.warning("Skipping unexpected old rollback entry: %s", name)
            continue
        names.append(name)
    return sorted(names)


def _prune_older_rollback_sets(
    config: dict[str, Any], sftp: paramiko.SFTPClient, save_dir: str, keep_name: str
) -> None:
    for name in _older_rollback_sets(sftp, save_dir, keep_name):
        path = _remote_join(save_dir, name)
        LOG.info("Removing superseded rollback set %s", path)
        _delete_with_retries(config, sftp, path, "superseded rollback set")


def run_cleanup(config: dict[str, Any]) -> dict[str, Any]:
    """Delete old rollback sets and leftover staging folders; keeps the newest rollback set.

    The game never reads these folders, so this is safe while the server is running.
    """
    save_dir = config["server"]["save_dir"]
    client, sftp = _connect(config)
    try:
        entries = _listdir(sftp, save_dir)
        current = sorted(e.filename for e in entries
                         if ROLLBACK_NAME.fullmatch(e.filename) and stat.S_ISDIR(e.st_mode))
        if current:
            keep = current[-1]
        else:  # only older layouts left: keep the newest of those so a rollback stays possible
            older = _older_rollback_sets(sftp, save_dir, None)
            keep = older[-1] if older else None
        targets = [(name, "old rollback set") for name in _older_rollback_sets(sftp, save_dir, keep)]
        targets += [(e.filename, "leftover staging folder") for e in entries
                    if STAGING_NAME.fullmatch(e.filename) and stat.S_ISDIR(e.st_mode)]
        LOG.info("Keeping newest rollback set: %s", keep or "none found")
        if not targets:
            LOG.info("Nothing to clean up in %s", save_dir)
        removed, failed = [], []
        for name, what in targets:
            path = _remote_join(save_dir, name)
            LOG.info("Removing %s %s", what, path)
            (removed if _delete_with_retries(config, sftp, path, what) else failed).append(name)
        plan = {
            "server": config["server"]["name"],
            "build": config["world"]["build"],
            "cleanup": True,
            "kept_rollback_set": keep,
            "removed": removed,
            "failed": failed,
            "cleaned_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_plan(plan)
        if failed:
            raise WorkerError(f"could not remove {len(failed)} folder(s): {', '.join(failed)}; run the cleanup again later")
        LOG.warning("Cleanup finished: %d folder(s) removed, kept %s", len(removed), keep or "no rollback set")
        return plan
    finally:
        sftp.close()
        client.close()


def run_apply(config: dict[str, Any], *, scheduled: bool = False) -> dict[str, Any]:
    """Selective reset over SFTP. The server must already be stopped (Pine Hosting panel)."""
    client, sftp = _connect(config)
    try:
        save_dir = config["server"]["save_dir"]
        plan, targets = _build_remote_plan(sftp, config)
        plan["action"] = "apply"
        _save_plan(plan)
        LOG.info("Applying reset for %s (%s)", config["server"]["name"], ", ".join(targets))
        _log_target_counts(plan, "Apply")

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        rollback_name = f"{ROLLBACK_PREFIX}{timestamp}"
        rollback_root = _remote_join(save_dir, rollback_name)
        stage_paths = {name: _remote_join(save_dir, f".{name}.pz-reset-staging-{timestamp}") for name in targets}
        _remove_stale_staging(config, sftp, save_dir)
        for path in (rollback_root, *stage_paths.values()):
            if _exists(sftp, path):
                raise WorkerError(f"{path} already exists; refusing to overwrite it")

        try:
            for name, target in targets.items():
                _stage_target(config, sftp, target, stage_paths[name])
            if scheduled:
                _ensure_before_provider_start(config)
        except Exception:
            for stage in stage_paths.values():
                _discard_stage(config, sftp, stage)
            raise

        sftp.mkdir(rollback_root)
        moved: list[str] = []
        installed: list[str] = []
        try:
            for name, target in targets.items():
                _rename(sftp, target["path"], _remote_join(rollback_root, name))
                moved.append(name)
                _rename(sftp, stage_paths[name], target["path"])
                installed.append(name)
        except (OSError, WorkerError) as commit_error:
            failed_root = _remote_join(save_dir, f"softreset.failed-{timestamp}")
            recovery_errors = []
            for name in reversed(list(targets)):
                try:
                    if name in installed:
                        if not _exists(sftp, failed_root):
                            sftp.mkdir(failed_root)
                        _rename(sftp, targets[name]["path"], _remote_join(failed_root, name))
                    if name in moved:
                        _rename(sftp, _remote_join(rollback_root, name), targets[name]["path"])
                except (OSError, WorkerError) as recovery_error:
                    recovery_errors.append(f"{name}: {recovery_error}")
            if recovery_errors:
                raise WorkerError(
                    f"CRITICAL: swap failed ({commit_error}); restoring the originals also failed "
                    f"({'; '.join(recovery_errors)}). Originals are under {rollback_root}; "
                    f"staged copies under {failed_root} or the .pz-reset-staging folders. "
                    "Do not start the server until fixed."
                ) from commit_error
            try:
                sftp.rmdir(rollback_root)
            except OSError:
                LOG.warning("Could not remove empty rollback folder %s", rollback_root)
            for stage in stage_paths.values():
                _discard_stage(config, sftp, stage)
            _discard_stage(config, sftp, failed_root)
            raise WorkerError(f"swap failed; the original folders are in place: {commit_error}") from commit_error

        plan["applied_at"] = datetime.now(timezone.utc).isoformat()
        plan["remote_rollback_dir"] = rollback_root
        try:
            _save_plan(plan)
        except WorkerError:
            LOG.exception("Reset applied, but the local plan file could not be updated")
        _prune_older_rollback_sets(config, sftp, save_dir, rollback_name)
        for setting, counts in plan["target_counts"].items():
            if plan["reset_targets"].get(setting):
                LOG.warning("Reset applied for %s: %d reset, %d preserved", setting, counts["reset"], counts["preserved"])
        LOG.warning("Originals kept at %s (%s)", rollback_root, ", ".join(targets))
        return plan
    finally:
        sftp.close()
        client.close()


def _latest_rollback_set(sftp: paramiko.SFTPClient, save_dir: str) -> str:
    names = [
        entry.filename
        for entry in _listdir(sftp, save_dir)
        if ROLLBACK_NAME.fullmatch(entry.filename) and stat.S_ISDIR(entry.st_mode)
    ]
    if not names:
        raise WorkerError(f"no softreset.rollback-* folder found in {save_dir}; nothing to roll back")
    return max(names)


def run_rollback(config: dict[str, Any], backup_name: str | None = None) -> dict[str, Any]:
    """Swap the newest rollback set back in. The replaced state becomes the new rollback set."""
    client, sftp = _connect(config)
    try:
        save_dir = config["server"]["save_dir"]
        name = backup_name or _latest_rollback_set(sftp, save_dir)
        if not ROLLBACK_NAME.fullmatch(name):
            raise WorkerError("rollback must name a softreset.rollback-YYYYMMDDTHHMMSSZ folder")
        rollback_dir = _remote_join(save_dir, name)
        originals: dict[str, str] = {}
        for entry in _listdir(sftp, rollback_dir):
            if not stat.S_ISDIR(entry.st_mode) or entry.filename not in ROLLBACK_FOLDERS:
                raise WorkerError(f"unexpected entry in rollback folder {rollback_dir}: {entry.filename}")
            originals[entry.filename] = _remote_join(rollback_dir, entry.filename)
        if not originals:
            raise WorkerError(f"rollback folder {rollback_dir} contains no saved targets")
        restored_chunks = (
            len(scan_chunks(sftp, originals["map"], allow_empty=True)) if "map" in originals else 0
        )
        LOG.info("Rolling back from %s (%s)", rollback_dir, ", ".join(sorted(originals)))

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        after_name = f"{ROLLBACK_PREFIX}{timestamp}"
        after_root = _remote_join(save_dir, after_name)
        if _exists(sftp, after_root):
            raise WorkerError(f"{after_root} already exists; refusing rollback")
        sftp.mkdir(after_root)
        moved_current: list[str] = []
        restored: list[str] = []
        try:
            for folder, original in originals.items():
                current = _remote_join(save_dir, folder)
                if not _exists(sftp, current):
                    raise WorkerError(f"current {folder} folder is missing: {current}")
                _rename(sftp, current, _remote_join(after_root, folder))
                moved_current.append(folder)
                _rename(sftp, original, current)
                restored.append(folder)
        except (OSError, WorkerError) as restore_error:
            recovery_errors = []
            for folder in reversed(list(originals)):
                current = _remote_join(save_dir, folder)
                try:
                    if folder in restored:
                        _rename(sftp, current, originals[folder])
                    if folder in moved_current:
                        _rename(sftp, _remote_join(after_root, folder), current)
                except (OSError, WorkerError) as recovery_error:
                    recovery_errors.append(f"{folder}: {recovery_error}")
            if not recovery_errors:
                try:
                    sftp.rmdir(after_root)
                except OSError:
                    LOG.warning("Could not remove empty folder %s", after_root)
                raise WorkerError(f"rollback failed; the save was left as it was: {restore_error}") from restore_error
            raise WorkerError(
                f"CRITICAL: rollback failed ({restore_error}); recovery also failed "
                f"({'; '.join(recovery_errors)}). Inspect {rollback_dir} and {after_root} "
                "before starting the server."
            ) from restore_error

        try:
            sftp.rmdir(rollback_dir)
        except OSError:
            LOG.warning("Could not remove emptied rollback folder %s", rollback_dir)
        _prune_older_rollback_sets(config, sftp, save_dir, after_name)
        result = {
            "action": "rollback",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "server": config["server"]["name"],
            "build": config["world"]["build"],
            "rolled_back_from": rollback_dir,
            "restored_folders": sorted(originals),
            "restored_map_chunks": restored_chunks,
            "remote_rollback_dir": after_root,
        }
        try:
            _save_plan(result)
        except WorkerError:
            LOG.exception("Rollback completed, but the local result file could not be written")
        LOG.warning(
            "Rollback completed: restored %s. The replaced state is kept at %s (roll back again to undo).",
            ", ".join(sorted(originals)), after_root,
        )
        return result
    finally:
        sftp.close()
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="FMG Soft Reset Tool SFTP worker")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="create a local plan only (default)")
    action.add_argument("--apply", action="store_true", help="apply the selective reset")
    action.add_argument(
        "--rollback",
        nargs="?",
        const="latest",
        metavar="ROLLBACK_SET",
        help="restore the latest (or a named) softreset.rollback-* set",
    )
    action.add_argument(
        "--cleanup",
        action="store_true",
        help="dedicated: delete old rollback sets and leftover staging folders (safe while the server runs)",
    )
    parser.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--confirm-server-name",
        metavar="SERVER_NAME",
        help="dedicated apply/rollback: the exact server.name from the config",
    )
    parser.add_argument("--confirm-server-stopped", action="store_true")
    parser.add_argument(
        "--confirm-save-path",
        "--confirm-test-copy",
        dest="confirm_save_path",
        metavar="EXACT_SAVE_PATH",
        help="confirm the exact configured local save path for a manual apply or rollback",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config()
        if config["server"].get("connection_mode", "sftp") == "local":
            if args.cleanup:
                raise WorkerError("--cleanup is for the dedicated (SFTP) setup")
            from local_worker import main as run_local

            local_args = []
            if args.scheduled:
                local_args.append("--scheduled")
            if args.rollback:
                if args.rollback == "latest":
                    raise WorkerError("local rollback requires the rollback folder path")
                local_args.extend(["--rollback", args.rollback])
                if args.confirm_server_stopped:
                    local_args.append("--confirm-server-stopped")
                if args.confirm_save_path:
                    local_args.extend(["--confirm-save-path", args.confirm_save_path])
            elif args.apply:
                local_args.append("--apply-copy")
                if args.confirm_server_stopped:
                    local_args.append("--confirm-server-stopped")
                if args.confirm_save_path:
                    local_args.extend(["--confirm-save-path", args.confirm_save_path])
            return run_local(local_args)

        if args.rollback and args.scheduled:
            raise WorkerError("scheduled runs cannot roll back")
        if (args.apply or args.rollback) and not args.scheduled:
            if (args.confirm_server_name or "").strip() != config["server"]["name"]:
                raise WorkerError(
                    "apply and rollback require --confirm-server-name to exactly match "
                    f"server.name ({config['server']['name']!r})"
                )
        if args.apply and args.scheduled:
            if config.get("dedicated_settings", {}).get("scheduled_action") != "apply":
                raise WorkerError("scheduled apply is not enabled (dedicated_settings.scheduled_action)")
        if args.cleanup:
            if args.scheduled:
                raise WorkerError("cleanup is not a scheduled action")
            run_cleanup(config)
        elif args.rollback:
            run_rollback(config, None if args.rollback == "latest" else args.rollback)
        else:
            config = load_remote_worker_settings(config)
            if args.apply:
                run_apply(config, scheduled=args.scheduled)
            else:
                run_dry_run(config)
        return 0
    except (ConfigError, GeometryError, WorkerError, OSError, paramiko.SSHException) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
