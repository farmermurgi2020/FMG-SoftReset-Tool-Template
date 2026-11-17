from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path

from config import CONFIG_PATH, ROOT, ConfigError, load_config, protected_area_notes
from schedule import github_cron_for, occurrence_on_or_after, validate_timezone


# Also the options of the workflow's "Run workflow" dropdown (dedicated_web_ui._workflow_text).
MODES = ("dry-run", "apply", "rollback", "benchmark", "scheduled", "hard-reset", "hard-reset-undo", "cleanup")
HARD_RESET_MODES = {"hard-reset": "--apply", "hard-reset-undo": "--undo"}


def _scheduled_window_is_open(config: dict) -> tuple[bool, str]:
    settings = config.get("dedicated_settings", {})
    schedule = settings.get("schedule", config["schedule"])
    if not schedule.get("enabled"):
        return False, "Dedicated schedule is disabled; skipping this workflow occurrence."
    timezone = validate_timezone(schedule["timezone"])
    now = datetime.now(timezone)
    due_date = occurrence_on_or_after(schedule, now.date())
    if due_date != now.date():
        return False, "Today is not a configured reset date; skipping this workflow occurrence."
    due = datetime.combine(due_date, time.fromisoformat(schedule["time"]), tzinfo=timezone)
    trigger = os.environ.get("PZ_SCHEDULE_CRON", "").strip()
    expected = github_cron_for(schedule, due)
    if trigger and trigger != expected:
        return False, (
            f"Started by cron '{trigger}', but today's reset belongs to '{expected}' "
            f"(the other daylight-saving offset of {schedule['timezone']}); skipping."
        )
    elapsed = (now - due).total_seconds()
    allowed = int(schedule["allowed_lateness_minutes"]) * 60
    if elapsed < -60:
        return False, "Workflow started before the configured reset time; skipping."
    if elapsed > allowed:
        return False, "Workflow started after the allowed lateness window; skipping."
    if not _inside_provider_window(now, schedule):
        return False, "Current time is outside the configured Pine Hosting maintenance window; skipping."
    return True, f"Scheduled run started at {now.isoformat()} (due {due.isoformat()})."


def _inside_provider_window(now: datetime, schedule: dict) -> bool:
    stop = time.fromisoformat(schedule["pine_stop_time"])
    start = time.fromisoformat(schedule["pine_start_time"])
    buffer = int(schedule["minimum_start_buffer_minutes"])
    minute = now.hour * 60 + now.minute
    stop_minute = stop.hour * 60 + stop.minute
    start_minute = start.hour * 60 + start.minute
    if stop_minute == start_minute:
        return False
    if stop_minute < start_minute:
        return stop_minute + buffer <= minute <= start_minute - buffer
    return minute >= stop_minute + buffer or minute <= start_minute - buffer


def _hard_reset_summary(action: str, plan: dict, succeeded: bool) -> list[str]:
    if action == "hard-reset":
        title = "hard reset applied (player data)" if succeeded else "hard reset FAILED"
    else:
        title = "hard reset undone" if succeeded else "hard reset undo FAILED"
    lines = [f"## FMG Dedicated Soft Reset — {title}", "", f"- Server: `{plan.get('server', 'unknown')}`"]
    if not succeeded:
        return lines + ["", "See the job log for the error. Files that were moved are put back when a step fails."]
    if action == "hard-reset":
        characters = plan.get("characters")
        safehouses = plan.get("safehouses")
        lines.append(f"- Characters removed: {'unknown' if characters is None else characters}")
        lines.append("- World clock: back to day 1")
        lines.append(f"- Safehouses to remove on next server start: {'unknown' if safehouses is None else safehouses}"
                     " (plus all factions), done by the FMGResetTool mod")
        lines += ["", "| Moved out | What it holds | Files |", "|---|---|---|"]
        for item in plan.get("items", []):
            lines.append(f"| `{item['name']}` | {item['label']} | {item['files']} |")
        lines += ["", f"- Kept for undo at: `{plan.get('remote_rollback_dir')}`",
                  "- Not touched: map chunks, zpop, apop, vehicles, accounts, admin roles."]
    else:
        lines.append(f"- Restored from: `{plan.get('restored_from')}` ({', '.join(plan.get('restored_entries', []))})")
        lines.append(f"- Replaced state kept at: `{plan.get('remote_rollback_dir')}` (undo again to reverse)")
    return lines


def _cleanup_summary(plan: dict, succeeded: bool) -> list[str]:
    title = "old rollbacks cleaned up" if succeeded else "cleanup INCOMPLETE"
    lines = [f"## FMG Dedicated Soft Reset — {title}", "", f"- Server: `{plan.get('server', 'unknown')}`",
             f"- Kept (newest rollback set): `{plan.get('kept_rollback_set') or 'none found'}`"]
    removed, failed = plan.get("removed", []), plan.get("failed", [])
    lines.append(f"- Removed: {', '.join(f'`{name}`' for name in removed) if removed else 'nothing to remove'}")
    if failed:
        lines.append(f"- Still there after 3 attempts: {', '.join(f'`{name}`' for name in failed)}. Run the cleanup again later.")
    if "cleaned_at" not in plan and not failed:
        lines += ["", "See the job log for the error."]
    lines += ["", "Map, zpop, apop and player data were not touched. Safe to run while the server is up."]
    return lines


def _write_summary(path: Path, action: str, plan: dict, succeeded: bool) -> None:
    if action == "cleanup":
        lines = _cleanup_summary(plan, succeeded)
    elif action in HARD_RESET_MODES:
        lines = _hard_reset_summary(action, plan, succeeded)
    else:
        lines = _reset_summary(action, plan, succeeded)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _reset_summary(action: str, plan: dict, succeeded: bool) -> list[str]:
    title = {
        "dry-run": "read-only plan",
        "apply": "reset applied" if succeeded else "reset FAILED",
        "rollback": "rollback completed" if succeeded else "rollback FAILED",
    }[action]
    lines = [f"## FMG Dedicated Soft Reset — {title}", ""]
    lines.append(f"- Server: `{plan.get('server', 'unknown')}`")
    lines.append(f"- Build: `{plan.get('build', 'unknown')}`")
    if action == "rollback":
        if succeeded:
            lines.append(f"- Restored from: `{plan.get('rolled_back_from')}`")
            lines.append(f"- Restored folders: {', '.join(plan.get('restored_folders', []))}")
            lines.append(f"- Map chunks restored: {plan.get('restored_map_chunks', 0)}")
            lines.append(f"- Replaced state kept at: `{plan.get('remote_rollback_dir')}` (roll back again to undo)")
    else:
        lines.append(f"- Protected 256-tile cells covered by zones: {plan.get('protected_cell_count', 0)}")
        info, warnings = protected_area_notes(plan)
        lines += [f"- {line}" for line in info]
        lines += [f"- **WARNING:** {line}" for line in warnings]
        selected = plan.get("reset_targets", {"map_chunks": True})
        verb = "Reset" if action == "apply" and succeeded else "Planned for reset"
        modes = plan.get("target_modes", {})
        mode_labels = {"protect": "keep protected zones", "reset_all": "reset everything", "skip": "not touched"}
        lines += ["", f"| Target | Mode | Total | Preserved | {verb} | Shared files kept |", "|---|---|---|---|---|---|"]
        labels = {"map_chunks": "Map chunks", "zombie_population": "Zombie population (zpop)",
                  "animal_population": "Animal population (apop)"}
        for setting, label in labels.items():
            if not selected.get(setting):
                lines.append(f"| {label} | not touched | - | - | - | - |")
                continue
            counts = plan.get("target_counts", {}).get(setting, {})
            shared = ", ".join(counts.get("shared_file_names", [])) or "-"
            mode = mode_labels.get(modes.get(setting, "protect"), "")
            lines.append(f"| {label} | {mode} | {counts.get('total', 0)} | {counts.get('preserved', 0)} | "
                         f"{counts.get('reset', 0)} | {shared} |")
        if action == "apply" and succeeded:
            lines += ["", f"- Originals kept at: `{plan.get('remote_rollback_dir')}` "
                          f"({', '.join(plan.get('selected_directories', ['map']))})"]
    lines.append("")
    if action == "dry-run":
        lines.append("This run did not modify the server save.")
    elif not succeeded:
        lines.append("See the job log for the error. The worker restores the original folders when a swap fails.")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GitHub Actions dispatcher for the dedicated soft reset")
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument(
        "--confirm-server-name",
        default="",
        help="required for apply and rollback; must match server.name in the config",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(CONFIG_PATH)
        if config["server"].get("connection_mode") != "sftp":
            raise ConfigError("dedicated workflow requires connection_mode='sftp'")
        env = os.environ.copy()
        env["PZRESET_CONFIG"] = str(CONFIG_PATH)
        if args.mode == "benchmark":
            print(f"Running read-only SFTP benchmark for {config['server']['name']}.", flush=True)
            return subprocess.run([sys.executable, str(ROOT / "benchmark.py")], cwd=ROOT, env=env, check=False).returncode
        action = args.mode
        command = [sys.executable, str(ROOT / "worker.py")]
        if action in HARD_RESET_MODES:
            command = [sys.executable, str(ROOT / "hard_reset_remote.py"), HARD_RESET_MODES[action],
                       "--confirm-server-name", args.confirm_server_name]
        elif args.mode == "scheduled":
            allowed, message = _scheduled_window_is_open(config)
            print(message, flush=True)
            if not allowed:
                return 0
            action = config.get("dedicated_settings", {}).get("scheduled_action", "dry-run")
            command += ["--apply", "--scheduled"] if action == "apply" else ["--dry-run"]
        else:
            if action == "dry-run":
                command.append("--dry-run")
            elif action == "cleanup":
                command.append("--cleanup")
            else:
                command += [f"--{action}", "--confirm-server-name", args.confirm_server_name]
        print(f"Running {action} for {config['server']['name']}.", flush=True)

        plan_path = ROOT / "reset-plan.json"
        plan_path.unlink(missing_ok=True)
        result = subprocess.run(command, cwd=ROOT, env=env, check=False)
        succeeded = result.returncode == 0
        plan = {"server": config["server"]["name"], "build": config["world"]["build"]}
        if plan_path.exists():
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            # A plan is written before apply starts; only a finished apply has applied_at.
            if action == "apply" and "applied_at" not in plan:
                succeeded = False
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            _write_summary(Path(summary_path), action, plan, succeeded)
        return result.returncode
    except (ConfigError, OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Dedicated workflow could not complete: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
