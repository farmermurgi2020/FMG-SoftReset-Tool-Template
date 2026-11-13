"""Read-only SFTP benchmark for the dedicated soft reset.

Measures how long the slow parts of an apply would take at different levels of
parallelism, without writing anything to the server:
- reading protected chunks (the download half of staging; uploads are assumed
  to cost about the same),
- one-request operations (stat), as a stand-in for deleting a file when an
  old rollback folder is pruned,
- listing chunk folders (the map scan).
"""
from __future__ import annotations

import json
import logging
import os
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable

from config import load_config
from worker import (
    PLAN_PATH,
    _connect,
    _listdir,
    _open_sftp,
    _read_remote_file,
    _remote_join,
    atomic_write_json,
    load_remote_worker_settings,
)

LOG = logging.getLogger("fmg-soft-reset-benchmark")
SAMPLE_FILES = int(os.environ.get("PZRESET_BENCH_FILES", "100"))
# From dry run 43304278319 (2026-11-12); used for projections instead of a full scan.
TOTAL_CHUNKS = int(os.environ.get("PZRESET_BENCH_TOTAL", "249152"))
KEEP_CHUNKS = int(os.environ.get("PZRESET_BENCH_KEEP", "19132"))
LIST_FOLDERS = 32
# Pine's SFTP server does not answer a second channel on one SSH login, so only
# separate connections are measured.
CONFIGS = [
    ("connections", 1), ("connections", 4), ("connections", 8),
    ("connections", 16), ("connections", 32), ("connections", 48),
]


def _open_workers(config: dict[str, Any], client, mode: str, count: int):
    """Open `count` SFTP handles; returns (handles, closers, error)."""
    handles, closers = [], []
    try:
        for _ in range(count):
            if mode == "channels":
                sftp = _open_sftp(client)
                handles.append(sftp)
                closers.append(sftp.close)
            else:
                extra_client, sftp = _connect(config)
                handles.append(sftp)
                closers.append(sftp.close)
                closers.append(extra_client.close)
    except Exception as exc:  # report how many opened before the server refused
        return handles, closers, f"{type(exc).__name__}: {exc}"
    return handles, closers, None


def _run(handles: list, items: list, task: Callable) -> float:
    count = len(handles)
    batches = [items[index::count] for index in range(count)]
    started = perf_counter()
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(lambda h=h, b=b: [task(h, item) for item in b]) for h, b in zip(handles, batches)]
    for future in futures:
        future.result()
    return perf_counter() - started


def _fmt_minutes(seconds: float) -> str:
    return f"{seconds / 60:.1f} min"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_remote_worker_settings(load_config())
    client, sftp = _connect(config)
    results: dict[str, Any] = {
        "action": "benchmark",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "server": config["server"]["name"],
        "build": config["world"]["build"],
        "sample_files": SAMPLE_FILES,
        "rows": [],
    }
    try:
        map_dir = _remote_join(config["server"]["save_dir"], "map")
        # A full sequential scan of a large map can take longer than the server keeps
        # the connection open, so sample folders and use the latest dry-run counts.
        LOG.info("Listing the top map folder")
        started = perf_counter()
        x_folders = [entry.filename for entry in _listdir(sftp, map_dir)]
        root_seconds = perf_counter() - started
        rng = random.Random(42)
        sample_folders = rng.sample(x_folders, min(len(x_folders), 40))
        chunks = []
        started = perf_counter()
        for name in sample_folders:
            folder = _remote_join(map_dir, name)
            for entry in _listdir(sftp, folder):
                chunks.append(_remote_join(folder, entry.filename))
            LOG.info("Listed folder %s", name)
        sample_list_seconds = perf_counter() - started
        total = TOTAL_CHUNKS
        keep_count = KEEP_CHUNKS
        per_folder = sample_list_seconds / len(sample_folders)
        results.update(
            total_chunks=total, keep=keep_count, map_folders=len(x_folders),
            root_list_seconds=root_seconds, sampled_folders=len(sample_folders),
            sampled_files=len(chunks), seconds_per_folder_listing=per_folder,
            projected_sequential_scan_seconds=per_folder * len(x_folders),
        )
        LOG.info(
            "Listed %d of %d map folders (%d files) in %.1fs: %.2fs/folder -> sequential scan ~%s",
            len(sample_folders), len(x_folders), len(chunks), sample_list_seconds, per_folder,
            _fmt_minutes(per_folder * len(x_folders)),
        )
        rng.shuffle(chunks)
        need = SAMPLE_FILES * len(CONFIGS)
        keep_pool = (chunks * (need // max(1, len(chunks)) + 1))[:need]
        stat_pool = list(reversed(keep_pool))
        list_sample = [_remote_join(map_dir, name) for name in rng.sample(x_folders, min(LIST_FOLDERS, len(x_folders)))]

        # Large world-level population files (read in full during staging).
        results["large_files"] = []
        for folder in ("zpop", "apop"):
            directory = _remote_join(config["server"]["save_dir"], folder)
            try:
                entries = _listdir(sftp, directory)
            except Exception as exc:
                LOG.info("Skipping %s: %s", directory, exc)
                continue
            for entry in entries:
                if entry.filename.startswith(f"{folder}_") and entry.filename.count("_") == 2:
                    continue  # per-cell file
                path = _remote_join(directory, entry.filename)
                LOG.info("Reading %s (%.1f MB)", path, entry.st_size / (1024 * 1024))
                started = perf_counter()
                size = len(_read_remote_file(sftp, path))
                seconds = perf_counter() - started
                results["large_files"].append({"path": path, "bytes": size, "seconds": seconds})
                LOG.info("Read %s: %.1f MB in %.1fs", entry.filename, size / (1024 * 1024), seconds)

        failed_modes: set[str] = set()
        for index, (mode, count) in enumerate(CONFIGS):
            row: dict[str, Any] = {"mode": mode, "parallel": count}
            results["rows"].append(row)
            if mode in failed_modes:
                row["error"] = "skipped (a smaller count already failed)"
                continue
            LOG.info("%s x%d: opening SFTP handles", mode, count)
            open_started = perf_counter()
            handles, closers, error = _open_workers(config, client, mode, count)
            row["open_seconds"] = perf_counter() - open_started
            try:
                if error:
                    row["error"] = f"opened {len(handles)} of {count}: {error}"
                    failed_modes.add(mode)
                    LOG.warning("%s x%d: %s", mode, count, row["error"])
                    continue
                reads = keep_pool[index * SAMPLE_FILES:(index + 1) * SAMPLE_FILES]
                stats = stat_pool[index * SAMPLE_FILES:(index + 1) * SAMPLE_FILES]
                read_bytes = [0]
                lock = threading.Lock()

                def read(handle, chunk):
                    size = len(_read_remote_file(handle, chunk))
                    with lock:
                        read_bytes[0] += size

                LOG.info("%s x%d: opened in %.1fs; reading %d chunk files", mode, count, row["open_seconds"], len(reads))
                row["read_seconds"] = _run(handles, reads, read)
                LOG.info("%s x%d: reads took %.1fs; checking %d files", mode, count, row["read_seconds"], len(stats))
                row["read_files"] = len(reads)
                row["read_bytes"] = read_bytes[0]
                row["stat_seconds"] = _run(handles, stats, lambda handle, chunk: handle.stat(chunk))
                row["stat_files"] = len(stats)
                LOG.info("%s x%d: checks took %.1fs", mode, count, row["stat_seconds"])
                if count in (1, 8, 16, 32):
                    LOG.info("%s x%d: listing %d folders", mode, count, len(list_sample))
                    row["list_seconds"] = _run(handles, list_sample, lambda handle, path: _listdir(handle, path))
                    row["list_folders"] = len(list_sample)
                    row["projected_scan_seconds"] = row["list_seconds"] / len(list_sample) * len(x_folders)
                per_read = row["read_seconds"] / max(1, len(reads))
                per_stat = row["stat_seconds"] / max(1, len(stats))
                # Staging = download + upload of each protected chunk (upload assumed ~ download).
                row["projected_stage_seconds"] = per_read * 2 * keep_count
                # Pruning an old rollback set = one delete per file in the full map.
                row["projected_delete_seconds"] = per_stat * total
                LOG.info(
                    "%s x%d: read %.1f ms/file, stat %.1f ms/file -> staging ~%s, delete old rollback ~%s",
                    mode, count, per_read * 1000, per_stat * 1000,
                    _fmt_minutes(row["projected_stage_seconds"]), _fmt_minutes(row["projected_delete_seconds"]),
                )
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
                failed_modes.add(mode)
                LOG.warning("%s x%d failed: %s", mode, count, row["error"])
            finally:
                for close in reversed(closers):
                    try:
                        close()
                    except Exception:
                        pass
    finally:
        sftp.close()
        client.close()

    atomic_write_json(PLAN_PATH, results)
    _write_summary(results)
    return 0


def _write_summary(results: dict[str, Any]) -> None:
    lines = [
        "## FMG Dedicated Soft Reset — SFTP benchmark (read-only)",
        "",
        f"- Server: `{results['server']}`",
        f"- Chunks: {results['total_chunks']} total, {results['keep']} protected",
        f"- Map folders: {results['map_folders']} (listing the top folder took {results['root_list_seconds']:.1f}s)",
        f"- Sequential listing: {results['seconds_per_folder_listing']:.2f}s per folder -> full scan ~{_fmt_minutes(results['projected_sequential_scan_seconds'])}",
        f"- Sample: {results['sample_files']} files per row",
        *[f"- Shared file `{item['path']}`: {item['bytes'] / (1024 * 1024):.1f} MB read in {item['seconds']:.1f}s"
          for item in results.get("large_files", [])],
        "",
        "| Mode | Parallel | Read ms/file | Stat ms/file | Projected scan | Projected staging | Projected old-rollback delete | Note |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in results["rows"]:
        if "read_seconds" in row:
            read_ms = row["read_seconds"] / row["read_files"] * 1000
            stat_ms = row["stat_seconds"] / row["stat_files"] * 1000
            listing = _fmt_minutes(row["projected_scan_seconds"]) if "projected_scan_seconds" in row else ""
            lines.append(
                f"| {row['mode']} | {row['parallel']} | {read_ms:.1f} | {stat_ms:.1f} | {listing} | "
                f"{_fmt_minutes(row['projected_stage_seconds'])} | {_fmt_minutes(row['projected_delete_seconds'])} | |"
            )
        else:
            lines.append(f"| {row['mode']} | {row['parallel']} | | | | | | {row.get('error', '')} |")
    lines += ["", "No files were written or deleted on the server."]
    text = "\n".join(lines) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text)


if __name__ == "__main__":
    sys.exit(main())
