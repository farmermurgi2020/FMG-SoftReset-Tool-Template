from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import (
    ROOT,
    ConfigError,
    load_config,
    merge_exported_worker_settings,
    protected_area_notes,
    target_modes,
)
from geometry import GeometryError, Rect, protected_chunks
from history import append_run_record
from local_saves import export_save_mismatch
from worker import (
    _eligible_zones,
    Chunk,
    LAYOUT_APPROVAL,
    WorkerError,
    _map_manifest,
    _plan_data,
    _save_plan,
    create_chunk_plan,
)


LOG = logging.getLogger("fmg-soft-reset-local")
PLAN_PATH = Path(os.environ.get("PZRESET_PLAN", ROOT / "reset-plan.json"))
MAX_ZONE_FILE_BYTES = 16 * 1024 * 1024
# Build 42 cells are 256x256 tiles (IsoCell.CELL_SIZE_IN_SQUARES).
POPULATION_CELL_SIZE = 256
MODE_LABELS = {"protect": "keep protected zones", "reset_all": "reset everything", "skip": "not touched"}
POPULATION_TARGETS = {
    "zombie_population": ("zpop", "zpop"),
    "animal_population": ("apop", "apop"),
}
GAME_PROCESS_NAMES = (
    "ProjectZomboid64.exe",
    "ProjectZomboid32.exe",
    "ProjectZomboidServer64.exe",
    "ProjectZomboidServer.exe",
)


def _read_zone_export(path: Path, config: dict[str, Any]) -> tuple[dict[str, Any], list[Rect]]:
    try:
        if path.stat().st_size > MAX_ZONE_FILE_BYTES:
            raise WorkerError(f"zone export exceeds {MAX_ZONE_FILE_BYTES} bytes")
        export = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkerError(f"zone export not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerError(f"cannot read zone export {path}: {exc}") from exc
    if not isinstance(export, dict):
        raise WorkerError("zone export must be a JSON object")
    schema_version = export.get("schemaVersion")
    if schema_version not in {1, 2}:
        raise WorkerError("unsupported or missing zone export schemaVersion")
    if export.get("build") != config["world"]["build"]:
        raise WorkerError(
            f"zone export build {export.get('build')!r} does not match configured build "
            f"{config['world']['build']!r}"
        )
    if export.get("schemaVersion") == 2 and export.get("executionMode", "local") != "local":
        raise WorkerError("zone export is not configured for Local hosted mode")
    # Every hosted server writes the same export file; never use one save's zones for another.
    mismatch = export_save_mismatch(export, config["server"]["save_dir"])
    if mismatch:
        raise WorkerError(mismatch)
    if not export.get("saveName"):
        LOG.warning("The zone export does not say which save wrote it (mod older than 0.4.3). "
                    "Make sure '%s' was the last server you hosted.", Path(config["server"]["save_dir"]).name)
    try:
        merge_exported_worker_settings(config, export)
    except ConfigError as exc:
        raise WorkerError(f"invalid worker settings in zone export: {exc}") from exc
    generated = export.get("generatedAt")
    if not isinstance(generated, str):
        raise WorkerError("zone export is missing generatedAt")
    try:
        generated_at = datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkerError("zone export generatedAt is not ISO-8601") from exc
    if generated_at.tzinfo is None:
        raise WorkerError("zone export generatedAt must include a timezone")
    if (generated_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds() > 300:
        raise WorkerError("zone export generatedAt is more than 5 minutes in the future")
    # Zones only matter for targets set to "keep protected zones".
    require_zones = "protect" in target_modes(config).values()
    zones = export.get("zones")
    if not isinstance(zones, list) or (require_zones and not zones):
        raise WorkerError("zone export has no zones; refusing to reset")
    eligible_zones = _eligible_zones(export, require_zones)
    try:
        rects = [Rect.from_mapping(zone, source="live") for zone in eligible_zones]
    except (GeometryError, TypeError) as exc:
        raise WorkerError(f"invalid zone bounds: {exc}") from exc
    if require_zones and not rects:
        raise WorkerError("zone export contains no protected areas; refusing to reset")
    return export, rects


def load_local_worker_settings(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    export, _ = _read_zone_export(Path(config["server"]["zone_export"]), config)
    return config, export


def scan_local_chunks(map_dir: Path, *, allow_empty: bool = False) -> tuple[Chunk, ...]:
    if map_dir.is_symlink() or not map_dir.is_dir():
        raise WorkerError(f"map directory is missing or is a symlink: {map_dir}")
    chunks: list[Chunk] = []
    for x_dir in map_dir.iterdir():
        if x_dir.is_symlink() or not x_dir.is_dir() or not re.fullmatch(r"-?\d+", x_dir.name):
            raise WorkerError(f"unexpected entry in map directory: {x_dir.name}")
        chunk_x = int(x_dir.name)
        if str(chunk_x) != x_dir.name:
            raise WorkerError(f"non-canonical chunk directory coordinate: {x_dir.name}")
        entries = list(x_dir.iterdir())
        if not entries:
            raise WorkerError(f"empty chunk directory: {x_dir}")
        seen_y: set[int] = set()
        for path in entries:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".bin"
                or not re.fullmatch(r"-?\d+", path.stem)
            ):
                raise WorkerError(f"unexpected entry in chunk directory: {path}")
            y = int(path.stem)
            if str(y) + ".bin" != path.name or y in seen_y:
                raise WorkerError(f"duplicate or non-canonical chunk coordinate: {path}")
            seen_y.add(y)
            info = path.stat()
            if info.st_size <= 0:
                raise WorkerError(f"empty chunk file: {path}")
            chunks.append(Chunk(chunk_x, y, str(path), info.st_size, info.st_mtime_ns))
    if not chunks and not allow_empty:
        raise WorkerError("map directory contains no chunk files; refusing reset")
    return tuple(sorted(chunks, key=lambda item: (item.x, item.y)))


def scan_population_cells(directory: Path, prefix: str) -> tuple[tuple[Chunk, ...], tuple[Path, ...]]:
    if directory.is_symlink() or not directory.is_dir():
        raise WorkerError(f"{prefix} population directory is missing or is a symlink: {directory}")
    pattern = re.compile(rf"{re.escape(prefix)}_(-?\d+)_(-?\d+)\.bin\Z")
    files: list[Chunk] = []
    shared_files: list[Path] = []
    seen: set[tuple[int, int]] = set()
    for path in directory.iterdir():
        match = pattern.fullmatch(path.name)
        if path.is_symlink() or not path.is_file():
            raise WorkerError(f"unexpected entry in {prefix} population directory: {path.name}")
        if not match:
            # Population directories also contain world-level files (for example,
            # zpop_virtual.bin and zpop_ids.bin) that cannot be spatially filtered.
            shared_files.append(path)
            continue
        cell_x, cell_y = int(match.group(1)), int(match.group(2))
        if path.name != f"{prefix}_{cell_x}_{cell_y}.bin" or (cell_x, cell_y) in seen:
            raise WorkerError(f"duplicate or non-canonical population cell file: {path}")
        seen.add((cell_x, cell_y))
        info = path.stat()
        if info.st_size <= 0:
            raise WorkerError(f"empty population cell file: {path}")
        files.append(Chunk(cell_x, cell_y, str(path), info.st_size, info.st_mtime_ns))
    return tuple(sorted(files, key=lambda item: (item.x, item.y))), tuple(sorted(shared_files))


def _build_plan(
    config: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    save_dir = Path(config["server"]["save_dir"]).resolve(strict=True)
    if not save_dir.is_dir():
        raise WorkerError(f"save path is not a directory: {save_dir}")
    map_dir = save_dir / "map"
    export, live_rects = _read_zone_export(Path(config["server"]["zone_export"]), config)
    modes = target_modes(config)
    if all(mode == "skip" for mode in modes.values()):
        raise WorkerError("every reset target is set to Don't touch; nothing would be reset")
    reset_targets = {key: mode != "skip" for key, mode in modes.items()}
    targets: dict[str, dict[str, Any]] = {}
    target_counts: dict[str, dict[str, int]] = {}
    chunks: tuple[Chunk, ...] = ()
    keep: list[Chunk] = []
    remove: list[Chunk] = []
    if reset_targets["map_chunks"]:
        chunks = scan_local_chunks(map_dir)
        keep, remove = create_chunk_plan(
            chunks, live_rects, config["world"]["chunk_size"], protect=modes["map_chunks"] == "protect"
        )
        targets["map"] = {
            "path": map_dir,
            "prefix": None,
            "files": chunks,
            "keep": keep,
            "remove": remove,
            "cell_size": config["world"]["chunk_size"],
        }
        target_counts["map_chunks"] = {
            "total": len(chunks), "preserved": len(keep), "reset": len(remove)
        }
    else:
        target_counts["map_chunks"] = {"total": 0, "preserved": 0, "reset": 0}
    for setting, (folder, prefix) in POPULATION_TARGETS.items():
        if not reset_targets[setting]:
            target_counts[setting] = {"total": 0, "preserved": 0, "reset": 0}
            continue
        directory = save_dir / folder
        population_files, shared_files = scan_population_cells(directory, prefix)
        if not population_files:
            raise WorkerError(f"{setting} is selected but {directory} contains no population files")
        if modes[setting] == "protect":
            protected_cells = protected_chunks(
                ((item.x, item.y) for item in population_files), live_rects, POPULATION_CELL_SIZE
            )
        else:
            protected_cells = set()  # Reset everything: only the shared files are kept.
        keep_cells = [item for item in population_files if item.key() in protected_cells]
        remove_cells = [item for item in population_files if item.key() not in protected_cells]
        targets[folder] = {
            "path": directory,
            "prefix": prefix,
            "files": population_files,
            "shared_files": shared_files,
            "keep": keep_cells,
            "remove": remove_cells,
            "cell_size": POPULATION_CELL_SIZE,
        }
        target_counts[setting] = {
            "total": len(population_files),
            "preserved": len(keep_cells),
            "reset": len(remove_cells),
            "shared_files_preserved": len(shared_files),
            "shared_file_names": [path.name for path in shared_files],
        }
    if reset_targets["map_chunks"]:
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
            "remove_count": 0,
            "delete_fraction": 0.0,
            "keep": [],
            "remove": [],
        }
    plan["connection_mode"] = "local"
    plan["local_save_dir"] = str(save_dir)
    plan["reset_targets"] = dict(reset_targets)
    plan["target_modes"] = dict(modes)
    plan["target_counts"] = target_counts
    plan["population_cell_size"] = POPULATION_CELL_SIZE
    plan["selected_directories"] = list(targets)
    return plan, targets


def run_local_dry_run(config: dict[str, Any], *, scheduled: bool = False) -> dict[str, Any]:
    plan, _ = _build_plan(config)
    _save_plan(plan)
    if scheduled:
        try:
            append_run_record(plan, "scheduled", "Dry run")
        except OSError:
            LOG.exception("Could not append scheduled dry-run history record")
    LOG.info("Local dry run for %s / build %s", config["server"]["name"], config["world"]["build"])
    for target, counts in plan["target_counts"].items():
        mode = plan["target_modes"].get(target, "skip")
        if mode == "skip":
            LOG.info("%s: not touched", target)
            continue
        LOG.info("%s (%s): %d total, %d preserved, %d planned for reset",
                 target, MODE_LABELS[mode], counts["total"], counts["preserved"], counts["reset"])
        if counts.get("shared_files_preserved"):
            LOG.warning("%s: preserving shared population files that cannot be spatially filtered: %s", target, ", ".join(counts["shared_file_names"]))
    info, warnings = protected_area_notes(plan)
    for line in info:
        LOG.info("%s", line)
    for line in warnings:
        LOG.warning("%s", line)
    LOG.info("Plan saved at %s; no save files were modified", PLAN_PATH)
    return plan


def _assert_game_closed() -> None:
    if os.name != "nt":
        raise WorkerError("local player-hosted apply is supported only on Windows")
    for name in GAME_PROCESS_NAMES:
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {name}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkerError(f"could not check whether Project Zomboid is running: {exc}") from exc
        if result.returncode != 0:
            raise WorkerError(f"tasklist failed while checking {name}: {result.stderr.strip()}")
        try:
            rows = csv.reader(result.stdout.splitlines())
            running = any(row and row[0].casefold() == name.casefold() for row in rows)
        except csv.Error as exc:
            raise WorkerError(f"could not parse tasklist output for {name}: {exc}") from exc
        if running:
            raise WorkerError(
                f"{name} is still running. Fully exit Project Zomboid before applying a reset."
            )


def _require_exact_test_path(config: dict[str, Any], confirmed_path: str | None) -> Path:
    configured_input = Path(config["server"]["save_dir"])
    if configured_input.is_symlink():
        raise WorkerError("save path must not be a symlink")
    is_junction = getattr(configured_input, "is_junction", None)
    if callable(is_junction) and is_junction():
        raise WorkerError("save path must not be a directory junction")
    configured = configured_input.resolve(strict=True)
    if confirmed_path is None:
        raise WorkerError("local apply requires --confirm-save-path with the exact configured save path")
    supplied = Path(confirmed_path).resolve(strict=True)
    if supplied != configured:
        raise WorkerError(
            f"confirmation path does not match configured save path: {supplied} != {configured}"
        )
    if configured == Path(configured.anchor):
        raise WorkerError("refusing to operate on a filesystem root")
    return configured


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_protected_chunks(keep: list[Chunk], stage_dir: Path) -> None:
    stage_dir.mkdir()
    try:
        total = len(keep)
        LOG.info("Staging %d protected map chunks", total)
        for index, chunk in enumerate(keep, start=1):
            source = Path(chunk.path)
            destination_dir = stage_dir / str(chunk.x)
            destination_dir.mkdir(exist_ok=True)
            destination = destination_dir / source.name
            shutil.copy2(source, destination)
            if source.stat().st_size != destination.stat().st_size or _hash_file(source) != _hash_file(destination):
                raise WorkerError(f"staged protected chunk verification failed: {source}")
            if index % 100 == 0 or index == total:
                LOG.info("Staged and verified %d/%d protected map chunks", index, total)
        staged = scan_local_chunks(stage_dir, allow_empty=not keep)
        if {(chunk.x, chunk.y, chunk.size) for chunk in staged} != {
            (chunk.x, chunk.y, chunk.size) for chunk in keep
        }:
            raise WorkerError("staged map does not match the protected chunk manifest")
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def _target_manifest(target: dict[str, Any]) -> tuple[tuple[Any, ...], ...]:
    if target["prefix"] is None:
        files = scan_local_chunks(target["path"])
        return _map_manifest(files)
    else:
        files, shared_files = scan_population_cells(target["path"], target["prefix"])
        cells = _map_manifest(files)
        shared = tuple(
            (path.name, path.stat().st_size, path.stat().st_mtime_ns, _hash_file(path))
            for path in shared_files
        )
        return cells + shared


def _stage_population_files(keep: list[Chunk], shared_files: tuple[Path, ...], stage_dir: Path, prefix: str) -> None:
    stage_dir.mkdir()
    try:
        LOG.info("Staging %d protected %s cells and %d shared files", len(keep), prefix, len(shared_files))
        for item in keep:
            source = Path(item.path)
            destination = stage_dir / source.name
            shutil.copy2(source, destination)
            if source.stat().st_size != destination.stat().st_size or _hash_file(source) != _hash_file(destination):
                raise WorkerError(f"staged protected population file verification failed: {source}")
        for source in shared_files:
            destination = stage_dir / source.name
            shutil.copy2(source, destination)
            if source.stat().st_size != destination.stat().st_size or _hash_file(source) != _hash_file(destination):
                raise WorkerError(f"staged shared population file verification failed: {source}")
        LOG.info("Staged and verified protected %s files", prefix)
        staged, staged_shared = scan_population_cells(stage_dir, prefix)
        if {(item.x, item.y, item.size) for item in staged} != {
            (item.x, item.y, item.size) for item in keep
        }:
            raise WorkerError(f"staged {prefix} files do not match protected population cells")
        if {path.name for path in staged_shared} != {path.name for path in shared_files}:
            raise WorkerError(f"staged {prefix} shared files do not match the source")
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def _stage_target(target: dict[str, Any], stage_dir: Path) -> None:
    if target["prefix"] is None:
        _copy_protected_chunks(target["keep"], stage_dir)
    else:
        _stage_population_files(target["keep"], target["shared_files"], stage_dir, target["prefix"])


def _prune_older_rollback_sets(save_dir: Path, keep: Path) -> None:
    prefixes = (
        "softreset.rollback-",
        "softreset.before-rollback-",
        "map.rollback-",
        "map.before-rollback-",
    )
    for path in save_dir.iterdir():
        if path == keep or not path.name.startswith(prefixes):
            continue
        if path.is_symlink() or not path.is_dir():
            LOG.warning("Skipping unexpected old rollback entry: %s", path)
            continue
        try:
            shutil.rmtree(path)
            LOG.info("Removed superseded rollback set: %s", path)
        except OSError:
            LOG.exception("Could not remove superseded rollback set: %s", path)


def _prune_older_backup_archives(config: dict[str, Any], keep: Path) -> None:
    backup_dir = Path(config["safety"]["backup_dir"])
    if not backup_dir.is_absolute():
        backup_dir = ROOT / backup_dir
    if not backup_dir.is_dir():
        return
    for path in backup_dir.glob("softreset-*.tar"):
        if path == keep:
            continue
        if path.is_symlink() or not path.is_file():
            LOG.warning("Skipping unexpected old backup archive: %s", path)
            continue
        try:
            path.unlink()
            LOG.info("Removed superseded backup archive: %s", path)
        except OSError:
            LOG.exception("Could not remove superseded backup archive: %s", path)


def _latest_backup_archive(config: dict[str, Any]) -> Path | None:
    backup_dir = Path(config["safety"]["backup_dir"])
    if not backup_dir.is_absolute():
        backup_dir = ROOT / backup_dir
    archives = [path for path in backup_dir.glob("softreset-*.tar") if path.is_file() and not path.is_symlink()]
    return max(archives, key=lambda path: path.name) if archives else None


def _record_rollback_history(config: dict[str, Any], counts: dict[str, int]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    plan = {
        "server": config["server"]["name"],
        "applied_at": now,
        "reset_targets": {
            "map_chunks": counts["map_chunks"] > 0,
            "zombie_population": counts["zombie_population"] > 0,
            "animal_population": counts["animal_population"] > 0,
        },
        "target_counts": {
            name: {"reset": count}
            for name, count in counts.items()
        },
    }
    try:
        append_run_record(plan, "rollback", "Rolled back")
    except OSError:
        LOG.exception("Rollback completed, but the run-history record could not be written")


def _create_local_backup(targets: dict[str, dict[str, Any]], config: dict[str, Any], timestamp: str) -> Path:
    backup_dir = Path(config["safety"]["backup_dir"])
    if not backup_dir.is_absolute():
        backup_dir = ROOT / backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    archive_path = backup_dir / f"softreset-{timestamp}.tar"
    if archive_path.exists():
        raise WorkerError(f"local backup already exists: {archive_path}")
    total_bytes = 0
    for target in targets.values():
        directory = target["path"]
        for path in directory.rglob("*"):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise WorkerError(f"symlink or special entry in reset target: {path}")
            if path.is_file():
                total_bytes += path.stat().st_size
    if shutil.disk_usage(backup_dir).free < int(total_bytes * 1.1) + 64 * 1024 * 1024:
        raise WorkerError("insufficient disk space for a full backup of selected reset targets")
    try:
        with tarfile.open(archive_path, "w") as archive:
            for name, target in targets.items():
                archive.add(target["path"], arcname=name, recursive=True)
        with tarfile.open(archive_path, "r") as archive:
            members = archive.getmembers()
        if not members or not any(member.isfile() for member in members):
            raise WorkerError("local reset backup archive is empty")
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    return archive_path


def run_local_apply(
    config: dict[str, Any],
    confirmed_server_stopped: bool,
    confirmed_test_copy: str | None,
    *,
    scheduled: bool = False,
) -> dict[str, Any]:
    if scheduled:
        config, export = load_local_worker_settings(config)
        schedule = config["schedule"]
        safety = config["safety"]
        if not schedule["enabled"]:
            raise WorkerError("local scheduled reset is disabled in the in-game settings")
        if not safety["apply_enabled"]:
            raise WorkerError("scheduled apply is disabled in worker settings")
        if safety["approved_build"] != config["world"]["build"]:
            raise WorkerError("safety.approved_build must exactly match world.build")
        if safety["approved_chunk_layout"] != LAYOUT_APPROVAL:
            raise WorkerError(f"safety.approved_chunk_layout must be {LAYOUT_APPROVAL!r}")
        if not safety["staging_and_rollback_tested"]:
            raise WorkerError("staging_and_rollback_tested must be true before scheduled apply")
        if export.get("executionMode", "local") != "local":
            raise WorkerError("scheduled apply requires Local hosted mode")
        confirmed_server_stopped = True
        confirmed_test_copy = config["server"]["save_dir"]
    if not confirmed_server_stopped:
        raise WorkerError("local apply requires --confirm-server-stopped")
    if not confirmed_test_copy:
        raise WorkerError("local apply requires --confirm-save-path with the exact configured save path")
    _assert_game_closed()
    save_dir = _require_exact_test_path(config, confirmed_test_copy)
    plan, targets = _build_plan(config)
    original_manifests = {name: _target_manifest(target) for name, target in targets.items()}
    stability = config["safety"]["file_stability_seconds"]
    LOG.info("Waiting %d seconds for selected reset targets to remain stable", stability)
    time.sleep(stability)
    for name, target in targets.items():
        if _target_manifest(target) != original_manifests[name]:
            raise WorkerError(f"{name} changed during the stability interval; refusing local apply")
    _assert_game_closed()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rollback_root = save_dir / f"softreset.rollback-{timestamp}"
    if rollback_root.exists():
        raise WorkerError("rollback path already exists; refusing to overwrite it")
    stage_paths = {
        name: save_dir / f".{name}.pz-reset-staging-{timestamp}"
        for name in targets
    }
    if any(path.exists() for path in stage_paths.values()):
        raise WorkerError("staging path already exists; refusing to overwrite it")
    LOG.info("Creating backup archive of %d selected reset targets", len(targets))
    local_backup = _create_local_backup(targets, config, timestamp)
    LOG.info("Backup archive completed: %s", local_backup)
    try:
        for name, target in targets.items():
            LOG.info("Preparing protected files for %s", name)
            _stage_target(target, stage_paths[name])
            LOG.info("Protected files staged and verified for %s", name)
        for name, target in targets.items():
            if _target_manifest(target) != original_manifests[name]:
                raise WorkerError(f"source {name} changed during backup/staging; original was not swapped")
    except Exception:
        for stage in stage_paths.values():
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
        raise
    _assert_game_closed()

    rollback_root.mkdir()
    moved_originals: list[str] = []
    installed_targets: list[str] = []
    try:
        for name, target in targets.items():
            os.replace(target["path"], rollback_root / name)
            moved_originals.append(name)
            os.replace(stage_paths[name], target["path"])
            installed_targets.append(name)
    except OSError as commit_error:
        failed_root = save_dir / f"softreset.failed-{timestamp}"
        recovery_errors = []
        for name in reversed(list(targets)):
            try:
                if name in installed_targets:
                    failed_root.mkdir(exist_ok=True)
                    os.replace(targets[name]["path"], failed_root / name)
                if name in moved_originals:
                    os.replace(rollback_root / name, targets[name]["path"])
            except OSError as recovery_error:
                recovery_errors.append(f"{name}: {recovery_error}")
        for stage in stage_paths.values():
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
        if not recovery_errors:
            rollback_root.rmdir()
            raise WorkerError(f"apply transaction failed; original targets were restored: {commit_error}") from commit_error
        raise WorkerError(
            f"CRITICAL: apply failed ({commit_error}); recovery also failed ({'; '.join(recovery_errors)}). "
            f"Inspect {rollback_root} and {failed_root}"
        ) from commit_error

    plan["applied_at"] = datetime.now(timezone.utc).isoformat()
    plan["local_rollback_dir"] = str(rollback_root)
    plan["local_backup_archive"] = str(local_backup)
    try:
        _save_plan(plan)
    except WorkerError as exc:
        raise WorkerError(
            f"local reset succeeded but the plan could not be updated: {exc}; "
            f"original targets retained at {rollback_root}"
        ) from exc
    try:
        append_run_record(plan, "scheduled" if scheduled else "forced", "Applied")
    except OSError:
        LOG.exception("Reset applied, but the run-history record could not be written")
    _prune_older_rollback_sets(save_dir, rollback_root)
    _prune_older_backup_archives(config, local_backup)
    LOG.warning("Local reset applied. Original targets retained at %s; archive at %s", rollback_root, local_backup)
    return plan


def run_local_rollback(
    config: dict[str, Any],
    rollback_path: str,
    server_stopped: bool,
    confirmed_test_copy: str | None,
) -> None:
    if not server_stopped:
        raise WorkerError("local rollback requires --confirm-server-stopped")
    _assert_game_closed()
    save_dir = _require_exact_test_path(config, confirmed_test_copy)
    rollback_dir = Path(rollback_path).resolve(strict=True)
    if rollback_dir.parent != save_dir:
        raise WorkerError("rollback directory must be directly inside the configured save")
    if rollback_dir.name.startswith("softreset.rollback-"):
        allowed = {"map", "zpop", "apop"}
        original_targets: dict[str, Path] = {}
        for path in rollback_dir.iterdir():
            if path.is_symlink() or not path.is_dir() or path.name not in allowed:
                raise WorkerError(f"unexpected entry in reset rollback directory: {path.name}")
            original_targets[path.name] = path
        if not original_targets:
            raise WorkerError("reset rollback directory contains no saved targets")
        if "map" in original_targets:
            map_files = scan_local_chunks(original_targets["map"], allow_empty=True)
        else:
            map_files = ()
        rollback_counts = {
            "map_chunks": len(map_files),
            "zombie_population": 0,
            "animal_population": 0,
        }
        for folder, prefix in (("zpop", "zpop"), ("apop", "apop")):
            if folder in original_targets:
                cells, _shared = scan_population_cells(original_targets[folder], prefix)
                rollback_counts["zombie_population" if folder == "zpop" else "animal_population"] = len(cells)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        after_root = save_dir / f"softreset.rollback-{timestamp}"
        if after_root.exists():
            raise WorkerError("post-reset retention path already exists; refusing rollback")
        after_root.mkdir()
        moved_current: list[str] = []
        restored_originals: list[str] = []
        try:
            for folder in original_targets:
                current = save_dir / folder
                if current.is_symlink() or not current.is_dir():
                    raise WorkerError(f"current reset target is missing or is a symlink: {current}")
                os.replace(current, after_root / folder)
                moved_current.append(folder)
                os.replace(original_targets[folder], current)
                restored_originals.append(folder)
        except (OSError, WorkerError) as restore_error:
            recovery_errors = []
            for folder in reversed(list(original_targets)):
                try:
                    current = save_dir / folder
                    if folder in restored_originals:
                        os.replace(current, original_targets[folder])
                    if folder in moved_current:
                        os.replace(after_root / folder, current)
                except OSError as recovery_error:
                    recovery_errors.append(f"{folder}: {recovery_error}")
            if not recovery_errors:
                after_root.rmdir()
                raise WorkerError(f"rollback failed; reset targets were restored: {restore_error}") from restore_error
            raise WorkerError(
                f"CRITICAL: rollback failed ({restore_error}); recovery also failed "
                f"({'; '.join(recovery_errors)}). Inspect {rollback_dir} and {after_root}"
            ) from restore_error
        rollback_dir.rmdir()
        _prune_older_rollback_sets(save_dir, after_root)
        latest_archive = _latest_backup_archive(config)
        if latest_archive is not None:
            _prune_older_backup_archives(config, latest_archive)
        _record_rollback_history(config, rollback_counts)
        LOG.warning("Local rollback completed. The state replaced by this rollback is retained at %s", after_root)
        return

    map_dir = save_dir / "map"
    if not rollback_dir.name.startswith("map.rollback-"):
        raise WorkerError("rollback path must be a softreset.rollback-* or legacy map.rollback-* directory")
    restored_map_chunks = len(scan_local_chunks(rollback_dir))
    _assert_game_closed()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    current_state = save_dir / f"softreset.rollback-{timestamp}"
    current_state.mkdir()
    os.replace(map_dir, current_state / "map")
    try:
        os.replace(rollback_dir, map_dir)
    except OSError as restore_error:
        try:
            os.replace(current_state / "map", map_dir)
            current_state.rmdir()
        except OSError as recovery_error:
            raise WorkerError(
                f"CRITICAL: rollback failed ({restore_error}) and current map recovery failed "
                f"({recovery_error}); map directories: {current_state}, {rollback_dir}"
            ) from recovery_error
        raise WorkerError(f"rollback failed; reset map restored from {current_state}") from restore_error
    _prune_older_rollback_sets(save_dir, current_state)
    latest_archive = _latest_backup_archive(config)
    if latest_archive is not None:
        _prune_older_backup_archives(config, latest_archive)
    _record_rollback_history(config, {
        "map_chunks": restored_map_chunks,
        "zombie_population": 0,
        "animal_population": 0,
    })
    LOG.warning("Local rollback completed. The state replaced by this rollback is retained at %s", current_state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local Windows player-hosted save worker")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="create a local plan without changing the save")
    action.add_argument("--apply-copy", action="store_true", help="apply only after the explicit copy/stop confirmations")
    action.add_argument("--rollback", metavar="ROLLBACK_DIRECTORY", help="restore a softreset.rollback-* or legacy map.rollback-* directory")
    parser.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--confirm-server-stopped", action="store_true")
    parser.add_argument(
        "--confirm-save-path",
        "--confirm-test-copy",
        dest="confirm_save_path",
        metavar="EXACT_SAVE_PATH",
        help="confirm that this is the exact configured local save path",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config()
        if config["server"].get("connection_mode", "sftp") != "local":
            raise WorkerError("local_worker requires server.connection_mode='local'")
        if args.scheduled:
            if args.rollback:
                raise WorkerError("scheduled local runs cannot perform rollback")
            config, export = load_local_worker_settings(config)
            if export.get("executionMode", "local") != "local":
                raise WorkerError("scheduled local runs require Local hosted mode")
            if not config["schedule"]["enabled"]:
                raise WorkerError("local scheduled reset is disabled in the in-game settings")
            _assert_game_closed()
            if args.apply_copy:
                run_local_apply(config, True, config["server"]["save_dir"], scheduled=True)
            else:
                run_local_dry_run(config, scheduled=True)
            return 0
        if args.rollback:
            run_local_rollback(
                config,
                args.rollback,
                args.confirm_server_stopped,
                args.confirm_save_path,
            )
        elif args.apply_copy:
            run_local_apply(config, args.confirm_server_stopped, args.confirm_save_path)
        else:
            run_local_dry_run(config)
        return 0
    except (ConfigError, GeometryError, WorkerError, OSError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
