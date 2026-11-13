from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import ConfigError, ROOT, load_config
from local_worker import _assert_game_closed, load_local_worker_settings
from schedule import next_run, occurrence_on_or_after, validate_timezone
from worker import load_remote_worker_settings


LOG = logging.getLogger("fmg-soft-reset-scheduler")
LOCK_PATH = Path(os.environ.get("PZRESET_LOCK_FILE", ROOT / ".scheduler.lock"))
LAST_LOCAL_RUN_PATH = ROOT / ".local-scheduler-last-run"
LOCAL_SCHEDULER_PID_PATH = ROOT / ".local-scheduler.pid"


def acquire_lock():
    lock_file = LOCK_PATH.open("a+")
    try:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            if lock_file.read(1) == "":
                lock_file.seek(0)
                lock_file.write(" ")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        raise RuntimeError("another scheduler instance is already running")
    return lock_file


def run_once() -> None:
    config = load_config()
    if config["server"].get("connection_mode", "sftp") != "sftp":
        raise ConfigError("the persistent scheduler is for SFTP mode; local player-hosted testing is manual")
    config = load_remote_worker_settings(config)
    schedule = config["schedule"]
    if not schedule["enabled"]:
        LOG.info("Schedule is disabled; checking again in 60 seconds")
        time.sleep(60)
        return
    timezone = validate_timezone(schedule["timezone"])
    now = datetime.now(timezone)
    due = next_run(schedule, now)
    wait = max(0.0, (due - now).total_seconds())
    LOG.info("Next scheduled %s reset is %s", schedule["frequency"], due.isoformat())
    time.sleep(wait)

    config = load_config()
    config = load_remote_worker_settings(config)
    if not config["schedule"]["enabled"]:
        LOG.info("Schedule was disabled before execution")
        return
    action = ["--apply"] if config["safety"]["apply_enabled"] else ["--dry-run"]
    command = [sys.executable, str(ROOT / "worker.py"), *action, "--scheduled"]
    LOG.info("Starting scheduled worker: %s", action[0])
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode:
        LOG.error("Scheduled worker exited with status %d; no automatic restart or retry", completed.returncode)
    else:
        LOG.info("Scheduled worker completed successfully")


def _load_local_config():
    config = load_config()
    if config["server"].get("connection_mode", "sftp") != "local":
        raise ConfigError("local scheduler requires server.connection_mode='local'")
    config, export = load_local_worker_settings(config)
    if export.get("executionMode", "local") != "local":
        raise ConfigError("the in-game hosting mode is not Local hosted")
    return config


def _next_local_due(schedule, now, last_run):
    due = next_run(schedule, now)
    if last_run is not None and due <= last_run:
        next_date = occurrence_on_or_after(schedule, last_run.date() + timedelta(days=1))
        run_time = datetime.strptime(schedule["time"], "%H:%M").time()
        due = datetime.combine(next_date, run_time, tzinfo=validate_timezone(schedule["timezone"]))
    return due


def _read_last_local_run():
    try:
        return datetime.fromisoformat(LAST_LOCAL_RUN_PATH.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise ConfigError(f"invalid local scheduler marker {LAST_LOCAL_RUN_PATH}: {exc}") from exc


def _mark_local_run(due):
    temporary = LAST_LOCAL_RUN_PATH.with_suffix(".tmp")
    temporary.write_text(due.isoformat(), encoding="utf-8")
    os.replace(temporary, LAST_LOCAL_RUN_PATH)


def run_once_local(last_run=None):
    config = _load_local_config()
    schedule = config["schedule"]
    if not schedule["enabled"]:
        LOG.info("Local schedule is disabled; checking again in 60 seconds")
        time.sleep(60)
        return last_run

    timezone = validate_timezone(schedule["timezone"])
    now = datetime.now(timezone)
    due = _next_local_due(schedule, now, last_run)
    LOG.info("Next local %s reset is %s", schedule["frequency"], due.isoformat())
    while now < due:
        time.sleep(min(30, max(1, (due - now).total_seconds())))
        config = _load_local_config()
        schedule = config["schedule"]
        if not schedule["enabled"]:
            LOG.info("Local schedule was disabled before execution")
            return last_run
        timezone = validate_timezone(schedule["timezone"])
        now = datetime.now(timezone)

    grace = timedelta(minutes=int(schedule.get("allowed_lateness_minutes", 10)))
    deadline = due + grace
    while True:
        config = _load_local_config()
        schedule = config["schedule"]
        if not schedule["enabled"]:
            LOG.info("Local schedule was disabled before execution")
            return last_run
        try:
            _assert_game_closed()
        except Exception as exc:
            if "is still running" not in str(exc):
                raise
            now = datetime.now(validate_timezone(schedule["timezone"]))
            if now >= deadline:
                LOG.warning("Game stayed open past the local schedule grace period; skipping this occurrence")
                _mark_local_run(due)
                return due
            LOG.info("Waiting for the hosted game to stop before local reset")
            time.sleep(min(30, max(1, (deadline - now).total_seconds())))
            continue

        action = ["--apply"] if config["safety"]["apply_enabled"] else ["--dry-run"]
        command = [sys.executable, str(ROOT / "worker.py"), *action, "--scheduled"]
        LOG.info("Starting local scheduled worker: %s", action[0])
        _mark_local_run(due)
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode:
            LOG.error("Local scheduled worker exited with status %d; no automatic retry", completed.returncode)
        else:
            LOG.info("Local scheduled worker completed successfully")
        return due


def run_local_scheduler() -> None:
    LOCAL_SCHEDULER_PID_PATH.write_text(str(os.getpid()), encoding="ascii")
    try:
        last_run = _read_last_local_run()
        while True:
            last_run = run_once_local(last_run)
    finally:
        try:
            if LOCAL_SCHEDULER_PID_PATH.read_text(encoding="ascii").strip() == str(os.getpid()):
                LOCAL_SCHEDULER_PID_PATH.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="FMG Soft Reset scheduler")
    parser.add_argument("--local", action="store_true", help="run the Windows player-hosted scheduler")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        lock_file = acquire_lock()
        with lock_file:
            if args.local:
                run_local_scheduler()
                return 0
            while True:
                run_once()
    except (ConfigError, OSError, RuntimeError) as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.info("Scheduler stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
