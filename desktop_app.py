from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Any

import hard_reset

from config import (
    CONFIG_PATH,
    ROOT,
    ConfigError,
    load_config,
    merge_exported_worker_settings,
    desktop_override_active,
    target_modes,
    validate_config,
    validate_target_modes,
)
from history import load_run_history


TARGET_LABELS = {
    "map_chunks": "Map chunks",
    "zombie_population": "Zombie population cells",
    "animal_population": "Animal population cells",
}
# Dropdown text -> worker mode, shared with the dedicated dashboard and the in-game panel.
MODE_OPTIONS = {
    "Reset, keep protected zones": "protect",
    "Reset everything (ignore zones)": "reset_all",
    "Don't touch": "skip",
}
MODE_TEXT = {mode: text for text, mode in MODE_OPTIONS.items()}
MODE_SHORT = {"protect": "Keep protected zones", "reset_all": "Reset everything", "skip": "Not touched"}
TIMEZONE_OPTIONS = {
    "UTC": "UTC",
    "Toronto / New York (Eastern)": "America/Toronto",
    "Halifax (Atlantic)": "America/Halifax",
    "St. John's (Newfoundland)": "America/St_Johns",
    "Winnipeg / Chicago (Central)": "America/Winnipeg",
    "Edmonton / Denver (Mountain)": "America/Edmonton",
    "Vancouver / Los Angeles (Pacific)": "America/Vancouver",
    "Anchorage (Alaska)": "America/Anchorage",
    "Honolulu (Hawaii)": "Pacific/Honolulu",
    "Mexico City": "America/Mexico_City",
    "London": "Europe/London",
    "Dublin": "Europe/Dublin",
    "Lisbon": "Europe/Lisbon",
    "Paris": "Europe/Paris",
    "Berlin": "Europe/Berlin",
    "Amsterdam / Brussels": "Europe/Amsterdam",
    "Madrid": "Europe/Madrid",
    "Rome": "Europe/Rome",
    "Zurich / Vienna": "Europe/Zurich",
    "Prague / Warsaw": "Europe/Prague",
    "Stockholm / Oslo / Copenhagen": "Europe/Stockholm",
    "Helsinki": "Europe/Helsinki",
    "Athens": "Europe/Athens",
}
TIMEZONE_LABELS = {zone: label for label, zone in TIMEZONE_OPTIONS.items()}
CELL_SIZE = 256
CHUNK_SIZE = 8
PLAN_PATH = ROOT / "reset-plan-dashboard.json"
SCHEDULER_LOG_PATH = ROOT / "local-scheduler.log"


def _read_export(config: dict[str, Any]) -> dict[str, Any]:
    export_path = Path(config["server"]["zone_export"])
    try:
        export = json.loads(export_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"zone export not found: {export_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read zone export {export_path}: {exc}") from exc
    if not isinstance(export, dict) or export.get("schemaVersion") != 2:
        raise ConfigError("dashboard requires a schemaVersion 2 zone export")
    if export.get("executionMode") != "local":
        raise ConfigError("select Local hosted in the in-game panel before opening this dashboard")
    if export.get("build") != config["world"]["build"]:
        raise ConfigError("configured Build does not match the game zone export")
    if not isinstance(export.get("zones"), list):
        raise ConfigError("zone export does not contain a zones list")
    return export


def _overlap_count(x: int, y: int, width: int, height: int, size: int) -> int:
    first_x, last_x = x // size, (x + width - 1) // size
    first_y, last_y = y // size, (y + height - 1) // size
    return (last_x - first_x + 1) * (last_y - first_y + 1)


def _overlap_coordinates(x: int, y: int, width: int, height: int, size: int) -> set[tuple[int, int]]:
    first_x, last_x = x // size, (x + width - 1) // size
    first_y, last_y = y // size, (y + height - 1) // size
    return {
        (grid_x, grid_y)
        for grid_x in range(first_x, last_x + 1)
        for grid_y in range(first_y, last_y + 1)
    }


def _display_time(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds() / 60
    return f"{stamp.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')} ({age_minutes:.0f} min old)"


class LocalDashboard:
    def __init__(self, root: tk.Tk, config: dict[str, Any], export: dict[str, Any]) -> None:
        self.root = root
        self.config = config
        self.export = export
        self.runtime_config = copy.deepcopy(config)
        merge_exported_worker_settings(self.runtime_config, export)
        self.root.title("FMG Soft Reset Tool - Local host")
        self.root.geometry("1080x760")
        self.root.minsize(900, 640)
        self.scheduler_process: subprocess.Popen[Any] | None = None
        self.scheduler_pid: int | None = None
        self.scheduler_log_handle = None
        self.scheduler_poll_id: str | None = None
        self.force_running = False
        self._build_ui()
        self._load_controls()
        self.tabs.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._poll_scheduler()
        self.refresh_plan()
        self.refresh_history()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="FMG Soft Reset Tool", font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Label(header, text="Local hosted only | scheduled runs are dry-run only", foreground="#8a4d00").pack(side="right")

        self.status_var = tk.StringVar(value="Loading local zone export...")
        ttk.Label(outer, textvariable=self.status_var, wraplength=1020).pack(fill="x", pady=(6, 10))

        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        self.overview_tab = ttk.Frame(self.tabs, padding=10)
        self.settings_tab = ttk.Frame(self.tabs, padding=10)
        self.history_tab = ttk.Frame(self.tabs, padding=10)
        self.hard_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.overview_tab, text="Zones and reset plan")
        self.tabs.add(self.settings_tab, text="Local settings")
        self.tabs.add(self.history_tab, text="Run history")
        self.tabs.add(self.hard_tab, text="Hard reset (player data)")
        self._build_overview()
        self._build_settings()
        self._build_history()
        self._build_hard_reset()

        scheduler_frame = ttk.LabelFrame(outer, text="Local scheduler", padding=8)
        scheduler_frame.pack(fill="x", pady=(8, 0))
        self.scheduler_status_var = tk.StringVar(value="Not running")
        self.start_scheduler_button = ttk.Button(scheduler_frame, text="Start Scheduler", command=self.start_scheduler)
        self.start_scheduler_button.pack(side="left")
        self.stop_scheduler_button = ttk.Button(scheduler_frame, text="Stop Scheduler", command=self.stop_scheduler, state="disabled")
        self.stop_scheduler_button.pack(side="left", padx=(6, 12))
        ttk.Label(scheduler_frame, textvariable=self.scheduler_status_var).pack(side="left", fill="x", expand=True)
        ttk.Label(scheduler_frame, text="Runs continue after this dashboard closes; log: local-scheduler.log").pack(side="right")

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, text="Scheduled apply stays locked off. Force applies require explicit confirmation.", foreground="#8a1c1c").pack(side="left")
        ttk.Button(footer, text="Close", command=self.close_dashboard).pack(side="right")

    def _build_overview(self) -> None:
        self.export_var = tk.StringVar()
        ttk.Label(self.overview_tab, textvariable=self.export_var).pack(anchor="w", pady=(0, 8))

        zone_frame = ttk.LabelFrame(self.overview_tab, text="Protected zones", padding=6)
        zone_frame.pack(fill="both", expand=True)
        columns = ("zone", "source", "bounds", "chunks", "cells")
        self.zone_tree = ttk.Treeview(zone_frame, columns=columns, show="headings", height=11)
        for key, heading, width in (
            ("zone", "Zone name", 260),
            ("source", "Type", 120),
            ("bounds", "Bounds (x, y, w, h)", 220),
            ("chunks", "Map chunks", 110),
            ("cells", "B42 cells", 100),
        ):
            self.zone_tree.heading(key, text=heading)
            self.zone_tree.column(key, width=width, anchor="w")
        self.zone_tree.tag_configure("safehouse", background="#e0f2df")
        self.zone_tree.tag_configure("tool", background="#ddecfb")
        scroll = ttk.Scrollbar(zone_frame, orient="vertical", command=self.zone_tree.yview)
        self.zone_tree.configure(yscrollcommand=scroll.set)
        self.zone_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.zone_total_var = tk.StringVar()
        ttk.Label(self.overview_tab, textvariable=self.zone_total_var).pack(anchor="w", pady=(4, 10))

        plan_frame = ttk.LabelFrame(self.overview_tab, text="Current worker plan", padding=6)
        plan_frame.pack(fill="x")
        plan_columns = ("target", "mode", "total", "saved", "removed", "shared")
        self.plan_tree = ttk.Treeview(plan_frame, columns=plan_columns, show="headings", height=3)
        for key, heading, width in (
            ("target", "Reset target", 260),
            ("mode", "Mode", 150),
            ("total", "Files/cells found", 120),
            ("saved", "Saved / protected", 150),
            ("removed", "Planned for reset", 150),
            ("shared", "Shared files retained", 180),
        ):
            self.plan_tree.heading(key, text=heading)
            self.plan_tree.column(key, width=width, anchor="w")
        self.plan_tree.pack(fill="x")
        self.plan_message = tk.StringVar(value="Refresh the plan to calculate exact file counts.")
        ttk.Label(plan_frame, textvariable=self.plan_message, wraplength=990).pack(anchor="w", pady=(6, 0))
        plan_actions = ttk.Frame(self.overview_tab)
        plan_actions.pack(fill="x", pady=(8, 0))
        self.force_button = ttk.Button(plan_actions, text="Force Chunk Refresh", command=self.force_refresh)
        self.force_button.pack(side="right")
        self.rollback_button = ttk.Button(plan_actions, text="Roll Back Latest Reset", command=self.rollback_latest)
        self.rollback_button.pack(side="right", padx=(0, 8))
        ttk.Button(plan_actions, text="Refresh export and dry-run plan", command=self.refresh_plan).pack(side="right", padx=(0, 8))

    def _build_settings(self) -> None:
        note = ttk.Label(
            self.settings_tab,
            text="Values load from the in-game export. Saving here creates a Local worker override; zone geometry remains owned by the game mod. Use the 'Use in-game settings' button to remove that override.",
            wraplength=980,
        )
        note.pack(fill="x", pady=(0, 10))
        self.source_var = tk.StringVar()
        ttk.Label(self.settings_tab, textvariable=self.source_var, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 8))

        target_frame = ttk.LabelFrame(self.settings_tab, text="What a reset does", padding=8)
        target_frame.pack(fill="x", pady=(0, 10))
        self.target_vars: dict[str, tk.StringVar] = {}
        for row, (target, label) in enumerate(TARGET_LABELS.items()):
            var = tk.StringVar(value=MODE_TEXT["skip"])
            self.target_vars[target] = var
            ttk.Label(target_frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=2)
            ttk.Combobox(
                target_frame, textvariable=var, values=tuple(MODE_OPTIONS), state="readonly", width=32
            ).grid(row=row, column=1, sticky="w", pady=2)
        ttk.Label(
            target_frame,
            text="Keep protected zones: whatever overlaps a protected zone or safehouse is kept "
                 "(8x8-tile map chunks, whole 256x256-tile population cells). zpop_ids.bin and "
                 "zpop_virtual.bin are always kept.",
            wraplength=940,
            foreground="#555555",
        ).grid(row=len(TARGET_LABELS), column=0, columnspan=2, sticky="w", pady=(6, 0))

        schedule_frame = ttk.LabelFrame(self.settings_tab, text="Local schedule", padding=8)
        schedule_frame.pack(fill="x", pady=(0, 10))
        self.schedule_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(schedule_frame, text="Enable scheduled dry-run plans", variable=self.schedule_enabled).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        self.schedule_vars: dict[str, tk.StringVar] = {}
        self.timezone_combo: ttk.Combobox | None = None
        self.frequency_var = tk.StringVar(value="weekly")
        fields = (
            ("anchor_date", "Start date (YYYY-MM-DD)"),
            ("time", "Reset time (HH:MM)"),
            ("timezone", "Timezone (IANA)"),
            ("pine_stop_time", "Host stop time (HH:MM)"),
            ("pine_start_time", "Host start time (HH:MM)"),
            ("minimum_start_buffer_minutes", "Start buffer (minutes)"),
            ("allowed_lateness_minutes", "Allowed lateness (minutes)"),
        )
        ttk.Label(schedule_frame, text="Frequency").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
        ttk.Combobox(schedule_frame, textvariable=self.frequency_var, values=("daily", "weekly", "monthly"), state="readonly", width=26).grid(row=1, column=1, sticky="w", pady=3)
        for index, (key, label) in enumerate(fields):
            row, col = 2 + index // 2, (index % 2) * 2
            ttk.Label(schedule_frame, text=label).grid(row=row, column=col, sticky="w", padx=(0, 6), pady=3)
            var = tk.StringVar()
            self.schedule_vars[key] = var
            if key == "timezone":
                self.timezone_combo = ttk.Combobox(
                    schedule_frame,
                    textvariable=var,
                    values=tuple(TIMEZONE_OPTIONS),
                    state="readonly",
                    width=26,
                )
                self.timezone_combo.grid(row=row, column=col + 1, sticky="w", padx=(0, 18), pady=3)
            else:
                ttk.Entry(schedule_frame, textvariable=var, width=29).grid(row=row, column=col + 1, sticky="w", padx=(0, 18), pady=3)

        buttons = ttk.Frame(self.settings_tab)
        buttons.pack(fill="x", pady=(4, 0))
        ttk.Button(buttons, text="Save dashboard settings", command=self.save_settings).pack(side="left")
        ttk.Button(buttons, text="Use in-game settings", command=self.clear_overrides).pack(side="left", padx=8)
        ttk.Label(buttons, text="Scheduled apply is locked off. Force is a separate manual action.", foreground="#8a1c1c").pack(side="right")

    def _build_history(self) -> None:
        intro = ttk.Label(
            self.history_tab,
            text="Scheduled entries are dry-run plans. Forced entries are manual worker applies.",
            wraplength=980,
        )
        intro.pack(anchor="w", pady=(0, 8))
        columns = ("date", "time", "type", "chunks", "zpop", "apop", "outcome")
        history_table = ttk.Frame(self.history_tab)
        history_table.pack(fill="both", expand=True)
        self.history_tree = ttk.Treeview(history_table, columns=columns, show="headings", height=20)
        for key, heading, width in (
            ("date", "Date", 105),
            ("time", "Time", 115),
            ("type", "Run type", 105),
            ("chunks", "Chunks", 100),
            ("zpop", "Zpop cells", 120),
            ("apop", "Apop cells", 120),
            ("outcome", "Outcome", 130),
        ):
            self.history_tree.heading(key, text=heading)
            self.history_tree.column(key, width=width, anchor="w")
        scroll = ttk.Scrollbar(history_table, orient="vertical", command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=scroll.set)
        self.history_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        ttk.Button(self.history_tab, text="Refresh history", command=self.refresh_history).pack(anchor="e", pady=(8, 0))

    def refresh_history(self) -> None:
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for record in load_run_history():
            value = record.get("timestamp", "")
            try:
                stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                local_stamp = stamp.astimezone()
                date_text = local_stamp.strftime("%Y-%m-%d")
                time_text = local_stamp.strftime("%H:%M:%S %Z")
            except (AttributeError, ValueError):
                date_text, time_text = str(value), ""
            self.history_tree.insert("", "end", values=(
                date_text,
                time_text,
                str(record.get("run_type", "")).title(),
                record.get("map_chunks_reset", 0),
                record.get("zombie_population_reset", 0),
                record.get("animal_population_reset", 0),
                record.get("status", ""),
            ))

    def _on_tab_changed(self, _event: Any = None) -> None:
        if self.tabs.select() == str(self.history_tab):
            self.refresh_history()
        elif self.tabs.select() == str(self.hard_tab):
            self.refresh_hard_reset()

    # ----- Hard Reset (Player Data only) -----------------------------------------------------

    def _build_hard_reset(self) -> None:
        ttk.Label(
            self.hard_tab,
            wraplength=1000,
            justify="left",
            text=(
                "Removes every character so players start fresh, and restarts the world at day 1. "
                "On the next server start the mod also removes all safehouses and factions.\n"
                "Kept: map chunks (buildings, loot, player-built bases), zombie and animal population, "
                "vehicles, accounts, passwords and admin roles. Run a soft reset first if you want the "
                "map refreshed too: once safehouses are gone they no longer protect anything."
            ),
        ).pack(anchor="w", pady=(0, 8))
        frame = ttk.LabelFrame(self.hard_tab, text="What a hard reset moves out of the save", padding=6)
        frame.pack(fill="both", expand=True)
        columns = ("entry", "what", "files", "size")
        self.hard_tree = ttk.Treeview(frame, columns=columns, show="headings", height=9)
        for key, heading, width in (
            ("entry", "File or folder", 200),
            ("what", "What it holds", 440),
            ("files", "Files", 80),
            ("size", "Size", 100),
        ):
            self.hard_tree.heading(key, text=heading)
            self.hard_tree.column(key, width=width, anchor="w")
        self.hard_tree.pack(fill="both", expand=True)
        self.hard_summary_var = tk.StringVar(value="Open this tab to read the save.")
        ttk.Label(self.hard_tab, textvariable=self.hard_summary_var, wraplength=1000, justify="left").pack(
            anchor="w", pady=(8, 0))
        self.hard_note_var = tk.StringVar(value="")
        ttk.Label(self.hard_tab, textvariable=self.hard_note_var, wraplength=1000, justify="left",
                  foreground="#1d5e20").pack(anchor="w", pady=(4, 0))
        actions = ttk.Frame(self.hard_tab)
        actions.pack(fill="x", pady=(10, 0))
        self.hard_button = ttk.Button(actions, text="Hard Reset (Player Data only)", command=self.hard_reset_apply)
        self.hard_button.pack(side="right")
        self.hard_undo_button = ttk.Button(actions, text="Undo Latest Hard Reset", command=self.hard_reset_undo)
        self.hard_undo_button.pack(side="right", padx=(0, 8))
        ttk.Button(actions, text="Refresh", command=self.refresh_hard_reset).pack(side="right", padx=(0, 8))
        self.hard_progress_var = tk.StringVar(value="")
        ttk.Label(actions, textvariable=self.hard_progress_var, wraplength=600).pack(side="left", fill="x", expand=True)

    @staticmethod
    def _describe_hard_note(note: dict[str, Any] | None) -> str:
        if not note:
            return ""
        status = note.get("status")
        if status == "pending":
            return (f"Hard reset done on {_display_time(note.get('requestedAt'))}. Safehouses and factions "
                    "will be removed the next time the server starts.")
        if status == "done":
            text = (f"Last hard reset finished {_display_time(note.get('completedAt'))}: "
                    f"{note.get('safehousesRemoved', 0)} safehouses and {note.get('factionsRemoved', 0)} "
                    "factions removed by the mod.")
            if note.get("knoxClaimFound"):
                text += (f" Knox Claim: {note.get('knoxLandClaimsRemoved', 0)} land claims and "
                         f"{note.get('knoxVehicleClaimsRemoved', 0)} vehicle claims cleared.")
            return text
        if status == "failed":
            return f"Last hard reset: the mod could not remove everything: {note.get('error', 'unknown error')}"
        if status == "cancelled":
            return "Last hard reset was undone before the server started; nothing was removed in game."
        return "The hard reset note could not be read."

    def refresh_hard_reset(self) -> None:
        self.hard_tree.delete(*self.hard_tree.get_children())
        try:
            config = load_config()
            hard_reset.require_local(config)
            plan = hard_reset.plan_hard_reset(config)
            save_dir = Path(plan["save_dir"])
        except (OSError, ConfigError, hard_reset.WorkerError, KeyError, TypeError) as exc:
            self.hard_summary_var.set(f"Cannot read the save: {exc}")
            self.hard_note_var.set("")
            self.hard_button.configure(state="disabled")
            self.hard_undo_button.configure(state="disabled")
            return
        for item in plan["items"]:
            self.hard_tree.insert("", "end", values=(
                item["name"], item["label"], item["files"], f"{item['bytes'] / 1_048_576:.1f} MB"))
        self.hard_summary_var.set(
            f"Save: {save_dir}\nCharacters that will be removed: {hard_reset.or_unknown(plan['characters'])}. "
            f"Safehouses removed on next server start: {hard_reset.or_unknown(plan['safehouses'])} "
            "(plus all factions and Knox Claim claims).")
        self.hard_note_var.set(self._describe_hard_note(plan["note"]))
        busy = self.force_running
        self.hard_button.configure(state="disabled" if busy else "normal")
        has_undo = hard_reset.latest_rollback(save_dir) is not None
        self.hard_undo_button.configure(state="normal" if has_undo and not busy else "disabled")

    def _hard_reset_preflight(self) -> tuple[dict[str, Any], Path] | None:
        if self.force_running:
            return None
        try:
            config = load_config()
            hard_reset.require_local(config)
            if not self._scheduler_lock_is_available():
                raise ConfigError("Stop the local scheduler first")
            return config, Path(config["server"]["save_dir"]).resolve(strict=True)
        except (OSError, ConfigError, hard_reset.WorkerError, KeyError, TypeError) as exc:
            messagebox.showerror("Cannot start hard reset", str(exc), parent=self.root)
            return None

    def hard_reset_apply(self) -> None:
        checked = self._hard_reset_preflight()
        if checked is None:
            return
        config, save_dir = checked
        try:
            plan = hard_reset.plan_hard_reset(config)
        except (OSError, hard_reset.WorkerError) as exc:
            messagebox.showerror("Cannot start hard reset", str(exc), parent=self.root)
            return
        prompt = (
            "Hard Reset (Player Data only)\n\n"
            f"- Removes {hard_reset.or_unknown(plan['characters'])} characters: every player starts with "
            "a new character.\n"
            "- Restarts the world clock at day 1 (power and water come back on).\n"
            "- On the next server start, removes ALL safehouses, factions and Knox Claim claims.\n"
            "- Keeps map chunks, zombies, animals, vehicles, accounts and admin roles.\n\n"
            f"Save folder: {save_dir}\n\n"
            "The game must be fully closed. The removed files are kept in a hardreset.rollback "
            "folder, so this can be undone. Continue?"
        )
        if not messagebox.askyesno("Confirm Hard Reset", prompt, icon="warning", parent=self.root):
            return
        name = config["server"]["name"]
        typed = simpledialog.askstring(
            "Type the server name", f"Type the server name to confirm:\n{name}", parent=self.root)
        if typed is None:
            return
        if typed.strip() != name:
            messagebox.showerror("Hard reset cancelled", "The server name did not match.", parent=self.root)
            return
        self._run_hard_reset_command(
            ["--apply", "--confirm-server-stopped", "--confirm-save-path", str(save_dir)],
            "Hard reset",
        )

    def hard_reset_undo(self) -> None:
        checked = self._hard_reset_preflight()
        if checked is None:
            return
        _config, save_dir = checked
        latest = hard_reset.latest_rollback(save_dir)
        if latest is None:
            messagebox.showerror("Nothing to undo", "No hard reset rollback folder was found.", parent=self.root)
            return
        prompt = (
            f"Put back the characters, world clock, safehouses, factions and claims from {latest.name}?\n\n"
            "map_meta.bin and global_mod_data.bin are restored from that hard reset too, so building "
            "and room changes and every mod's saved data (claims, shops, events...) since then are lost. "
            "Characters created after the hard reset are moved into a new hardreset.rollback folder "
            "(so this undo can itself be undone).\n\n"
            "The game must be fully closed. Continue?"
        )
        if not messagebox.askyesno("Confirm undo", prompt, icon="warning", parent=self.root):
            return
        self._run_hard_reset_command(
            ["--undo", "--confirm-server-stopped", "--confirm-save-path", str(save_dir)],
            "Undo hard reset",
        )

    def _run_hard_reset_command(self, arguments: list[str], label: str) -> None:
        self.force_running = True
        for button in (self.hard_button, self.hard_undo_button, self.force_button, self.rollback_button,
                       self.start_scheduler_button):
            button.configure(state="disabled")
        self.hard_progress_var.set(f"{label} is running. Keep the game closed until it finishes...")
        command = [sys.executable, str(ROOT / "hard_reset.py"), *arguments]
        env = os.environ.copy()
        env["PZRESET_CONFIG"] = str(CONFIG_PATH)

        def run() -> None:
            # Streamed line by line (like rollback and Force Chunk Refresh) so the tab shows progress.
            try:
                process = subprocess.Popen(
                    command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                lines: list[str] = []
                for line in process.stdout or ():
                    lines.append(line.rstrip())
                    self.root.after(0, lambda message=line.rstrip(): self.hard_progress_var.set(message))
                returncode = process.wait()
                output = "\n".join(lines).strip()
                self.root.after(0, lambda: self._finish_hard_reset(label, returncode, output))
            except Exception as exc:
                self.root.after(0, lambda error=exc: self._finish_hard_reset(label, 1, str(error)))

        threading.Thread(target=run, name="fmg-hard-reset", daemon=True).start()

    def _finish_hard_reset(self, label: str, returncode: int, output: str) -> None:
        self.force_running = False
        self._refresh_scheduler_controls()
        self._refresh_rollback_button()
        self.refresh_hard_reset()
        if returncode == 0:
            self.hard_progress_var.set(f"{label} completed.")
            messagebox.showinfo(f"{label} complete", output or f"{label} completed.", parent=self.root)
        else:
            self.hard_progress_var.set(f"{label} failed. Nothing was changed unless the message says otherwise.")
            messagebox.showerror(f"{label} failed", output or "The worker returned an error.", parent=self.root)

    def _load_controls(self) -> None:
        local_override = desktop_override_active(self.config, self.export)
        source = self.config["desktop_settings"] if local_override else self.runtime_config
        if local_override and isinstance(source.get("target_modes"), dict):
            modes = source["target_modes"]
        elif local_override:
            # Older override saved on/off targets: on = keep protected zones, off = don't touch.
            targets = source.get("reset_targets", {})
            modes = {key: "protect" if targets.get(key) else "skip" for key in TARGET_LABELS}
        else:
            modes = target_modes(self.runtime_config)
        schedule = source.get("schedule", self.runtime_config["schedule"])
        for key, var in self.target_vars.items():
            var.set(MODE_TEXT.get(modes.get(key, "skip"), MODE_TEXT["skip"]))
        self.schedule_enabled.set(schedule.get("enabled") is True)
        self.frequency_var.set(schedule.get("frequency", "weekly"))
        for key, var in self.schedule_vars.items():
            value = str(schedule.get(key, ""))
            if key == "timezone":
                display = TIMEZONE_LABELS.get(value, value)
                if self.timezone_combo and display not in self.timezone_combo.cget("values"):
                    self.timezone_combo.configure(values=tuple(TIMEZONE_OPTIONS) + (display,))
                var.set(display)
            else:
                var.set(value)
        game_saved = self.export.get("settingsSavedAt")
        dashboard_saved = self.config.get("desktop_settings", {}).get("saved_at")
        if local_override:
            text = f"Settings source: this dashboard (saved {_display_time(dashboard_saved)})"
            if game_saved:
                text += f"; newer than the in-game save ({_display_time(game_saved)})"
        else:
            text = "Settings source: in-game Soft Reset Settings"
            if game_saved:
                text += f" (saved {_display_time(game_saved)})"
            if self.config.get("desktop_settings", {}).get("enabled") is True:
                text += "; newer than this dashboard's saved settings"
        self.source_var.set(text + ". Whichever was saved last is used.")

    def _populate_zones(self, export: dict[str, Any]) -> None:
        self.export_var.set(f"Zone export: {export['generatedAt']} | Build {export['build']} | Local hosted")
        for item in self.zone_tree.get_children():
            self.zone_tree.delete(item)
        protected_chunks: set[tuple[int, int]] = set()
        protected_cells: set[tuple[int, int]] = set()
        for zone in export["zones"]:
            if not isinstance(zone, dict) or zone.get("source") == "non-pvp":
                continue
            try:
                x, y = int(zone["x"]), int(zone["y"])
                width = int(zone.get("w", zone.get("width")))
                height = int(zone.get("h", zone.get("height")))
                if width <= 0 or height <= 0:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            chunks = _overlap_count(x, y, width, height, CHUNK_SIZE)
            cells = _overlap_count(x, y, width, height, CELL_SIZE)
            protected_chunks.update(_overlap_coordinates(x, y, width, height, CHUNK_SIZE))
            protected_cells.update(_overlap_coordinates(x, y, width, height, CELL_SIZE))
            source = str(zone.get("source", "tool"))
            tag = "safehouse" if source == "safehouse" else "tool"
            self.zone_tree.insert("", "end", values=(
                zone.get("name", "Unnamed zone"), source,
                f"{x}, {y}, {width}, {height}", chunks, cells,
            ), tags=(tag,))
        self.zone_total_var.set(
            f"Protected zone coverage (unique): {len(protected_chunks)} chunks and {len(protected_cells)} Build 42 256-tile cells."
        )

    def refresh_plan(self) -> None:
        try:
            config = load_config()
            if config["server"].get("connection_mode") != "local":
                raise ConfigError("set server.connection_mode to local in config.json")
            export = _read_export(config)
            self.config = config
            self.export = export
            self._populate_zones(export)
            self.plan_message.set("Running a read-only dry run...")
            self.root.update_idletasks()
            env = os.environ.copy()
            env["PZRESET_CONFIG"] = str(CONFIG_PATH)
            env["PZRESET_PLAN"] = str(PLAN_PATH)
            result = subprocess.run(
                [sys.executable, str(ROOT / "worker.py"), "--dry-run"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            if result.returncode != 0:
                raise ConfigError((result.stderr or result.stdout or "worker dry run failed").strip())
            plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
            for item in self.plan_tree.get_children():
                self.plan_tree.delete(item)
            modes = self._plan_modes(plan)
            counts = plan.get("target_counts", {})
            for key, label in TARGET_LABELS.items():
                target_count = counts.get(key, {})
                shared = ", ".join(target_count.get("shared_file_names", [])) or "-"
                self.plan_tree.insert("", "end", values=(
                    label,
                    MODE_SHORT[modes[key]],
                    target_count.get("total", 0),
                    target_count.get("preserved", 0),
                    target_count.get("reset", 0),
                    shared,
                ))
            self.plan_message.set(f"Read-only plan saved to {PLAN_PATH}. No save files were modified.")
            self.status_var.set(f"Zone export: {_display_time(export.get('generatedAt'))} | Scheduled apply: LOCKED OFF")
            # Refresh settings from the persisted dashboard override or game export.
            self.runtime_config = copy.deepcopy(config)
            merge_exported_worker_settings(self.runtime_config, export)
            self._load_controls()
        except (OSError, json.JSONDecodeError, ConfigError, subprocess.SubprocessError) as exc:
            self.plan_message.set(str(exc))
            self.status_var.set("Dashboard is Local hosted only. Dry-run could not be refreshed.")

    def force_refresh(self) -> None:
        if self.force_running:
            return
        try:
            config = load_config()
            if config["server"].get("connection_mode") != "local":
                raise ConfigError("Force Chunk Refresh is available only in Local hosted mode")
            export = _read_export(config)
            runtime_config = copy.deepcopy(config)
            merge_exported_worker_settings(runtime_config, export)
            configured_modes = target_modes(runtime_config)
            if configured_modes != self._selected_modes():
                raise ConfigError("Save the current reset settings before forcing a refresh")
            if all(mode == "skip" for mode in configured_modes.values()):
                raise ConfigError("every reset target is set to Don't touch; nothing would be reset")
            if not self._scheduler_lock_is_available():
                raise ConfigError("Stop the local scheduler before starting a forced reset")
            save_dir = Path(config["server"]["save_dir"]).resolve(strict=True)
            if not save_dir.is_dir():
                raise ConfigError(f"configured save path is not a directory: {save_dir}")
        except (OSError, ConfigError, KeyError, TypeError) as exc:
            messagebox.showerror("Cannot start forced refresh", str(exc), parent=self.root)
            return

        self.force_running = True
        self.force_button.configure(state="disabled")
        self.start_scheduler_button.configure(state="disabled")
        self.plan_message.set("Preparing a fresh plan for the manual force action...")
        env = os.environ.copy()
        env["PZRESET_CONFIG"] = str(CONFIG_PATH)
        env["PZRESET_PLAN"] = str(PLAN_PATH)

        def prepare() -> None:
            try:
                result = subprocess.run(
                    [sys.executable, str(ROOT / "worker.py"), "--dry-run"],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                if result.returncode != 0:
                    raise ConfigError((result.stderr or result.stdout or "worker dry run failed").strip())
                plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
                if Path(plan.get("local_save_dir", "")).resolve(strict=True) != save_dir:
                    raise ConfigError("fresh plan save path does not match the configured local save")
                if self._plan_modes(plan) != configured_modes:
                    raise ConfigError("fresh plan reset modes do not match the saved dashboard settings")
                counts = plan.get("target_counts", {})
                summary = {
                    "plan": plan,
                    "save_dir": save_dir,
                    "env": env,
                    "map": counts.get("map_chunks", {}).get("reset", 0),
                    "zombie": counts.get("zombie_population", {}).get("reset", 0),
                    "animal": counts.get("animal_population", {}).get("reset", 0),
                }
                if not any((summary["map"], summary["zombie"], summary["animal"])):
                    raise ConfigError("the fresh plan contains no files or cells to reset")
                self.root.after(0, lambda: self._confirm_force_apply(summary))
            except (OSError, json.JSONDecodeError, ConfigError, subprocess.SubprocessError) as exc:
                self.root.after(0, lambda error=exc: self._finish_force_error(str(error)))

        threading.Thread(target=prepare, name="fmg-force-plan", daemon=True).start()

    def _latest_rollback(self, save_dir: Path) -> Path | None:
        candidates = []
        for pattern in ("softreset.rollback-*", "map.rollback-*"):
            for path in save_dir.glob(pattern):
                if not path.is_symlink() and path.is_dir():
                    candidates.append(path)
        def timestamp(path: Path) -> str:
            return path.name.rsplit("-", 1)[-1]

        return max(candidates, key=timestamp) if candidates else None

    def _refresh_rollback_button(self) -> None:
        try:
            save_dir = Path(self.config["server"]["save_dir"]).resolve(strict=True)
            available = self._latest_rollback(save_dir) is not None
        except (OSError, KeyError, TypeError):
            available = False
        available = available and not self.force_running and self.force_button.cget("state") != "disabled"
        self.rollback_button.configure(state="normal" if available else "disabled")

    def rollback_latest(self) -> None:
        if self.force_running:
            return
        try:
            config = load_config()
            if config["server"].get("connection_mode") != "local":
                raise ConfigError("Rollback is available only in Local hosted mode")
            save_dir = Path(config["server"]["save_dir"]).resolve(strict=True)
            rollback_dir = self._latest_rollback(save_dir)
            if rollback_dir is None:
                raise ConfigError("No rollback folder is available for the configured save")
            if not self._scheduler_lock_is_available():
                raise ConfigError("Stop the local scheduler before starting a rollback")
        except (OSError, ConfigError, KeyError, TypeError) as exc:
            messagebox.showerror("Cannot start rollback", str(exc), parent=self.root)
            self._refresh_rollback_button()
            return

        prompt = (
            "This is the most recent rollback available. Are you sure you want to proceed?\n\n"
            f"Rollback folder: {rollback_dir}\n"
            f"Save folder: {save_dir}\n\n"
            "The game must be fully closed. The current reset state will be retained as the new rollback copy."
        )
        if not messagebox.askyesno("Confirm rollback", prompt, icon="warning", parent=self.root):
            return

        self.force_running = True
        self.force_button.configure(state="disabled")
        self.rollback_button.configure(state="disabled")
        self.start_scheduler_button.configure(state="disabled")
        self.plan_message.set("Rollback is running. Keep the game closed until it finishes...")
        command = [
            sys.executable, str(ROOT / "worker.py"), "--rollback", str(rollback_dir),
            "--confirm-server-stopped", "--confirm-save-path", str(save_dir),
        ]
        env = os.environ.copy()
        env["PZRESET_CONFIG"] = str(CONFIG_PATH)

        def run_rollback() -> None:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                output_lines: list[str] = []
                if process.stdout is not None:
                    for line in process.stdout:
                        output_lines.append(line.rstrip())
                        self.root.after(0, lambda message=line.rstrip(): self._set_force_progress(message))
                returncode = process.wait()
                output = "\n".join(output_lines).strip()
                self.root.after(0, lambda: self._finish_rollback(returncode, output))
            except Exception as exc:
                self.root.after(0, lambda error=exc: self._finish_rollback_error(str(error)))

        threading.Thread(target=run_rollback, name="fmg-rollback", daemon=True).start()

    def _finish_rollback(self, returncode: int, output: str) -> None:
        self.force_running = False
        self._refresh_scheduler_controls()
        self._refresh_rollback_button()
        if returncode == 0:
            self.plan_message.set("Rollback completed. The state it replaced is now the latest rollback copy.")
            messagebox.showinfo("Rollback complete", output or "Rollback completed successfully.", parent=self.root)
            self.refresh_plan()
        else:
            self.plan_message.set("Rollback failed. Review the worker output.")
            messagebox.showerror("Rollback failed", output or "Worker returned an error.", parent=self.root)

    def _finish_rollback_error(self, error: str) -> None:
        self.force_running = False
        self._refresh_scheduler_controls()
        self._refresh_rollback_button()
        self.plan_message.set(f"Rollback stopped: {error}")
        messagebox.showerror("Rollback stopped", error, parent=self.root)

    def _confirm_force_apply(self, summary: dict[str, Any]) -> None:
        plan = summary["plan"]
        modes = self._plan_modes(plan)
        counts = {"map_chunks": summary["map"], "zombie_population": summary["zombie"],
                  "animal_population": summary["animal"]}
        counts_text = "\n".join(
            f"{label}: {MODE_SHORT[modes[key]]}"
            + (f" ({counts[key]} to reset)" if modes[key] != "skip" else "")
            for key, label in TARGET_LABELS.items()
        )
        details = (
            f"This will apply a reset to save '{plan.get('server', '')}'.\n\n"
            f"{counts_text}\n\n"
            f"Save folder: {summary['save_dir']}\n\n"
            "The game must be fully closed. The worker creates a backup and rollback copy."
        )
        if not messagebox.askyesno("Confirm Force Chunk Refresh", details, icon="warning", parent=self.root):
            self._finish_force_cancelled()
            return
        self.plan_message.set("Forced reset is running. Keep the game closed until it finishes...")
        command = [
            sys.executable, str(ROOT / "worker.py"), "--apply",
            "--confirm-server-stopped", "--confirm-save-path", str(summary["save_dir"]),
        ]

        def apply() -> None:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=summary["env"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                output_lines: list[str] = []
                if process.stdout is not None:
                    for line in process.stdout:
                        output_lines.append(line.rstrip())
                        self.root.after(0, lambda message=line.rstrip(): self._set_force_progress(message))
                returncode = process.wait()
                output = "\n".join(output_lines).strip()
                self.root.after(0, lambda: self._finish_force_apply(returncode, output))
            except Exception as exc:
                self.root.after(0, lambda error=exc: self._finish_force_error(str(error)))

        threading.Thread(target=apply, name="fmg-force-apply", daemon=True).start()

    def _set_force_progress(self, message: str) -> None:
        self.plan_message.set(f"Force refresh: {message}")

    def _finish_force_cancelled(self) -> None:
        self.force_running = False
        self.force_button.configure(state="normal")
        self.plan_message.set("Forced refresh cancelled. No save files were modified.")
        self._refresh_scheduler_controls()

    def _finish_force_error(self, error: str) -> None:
        self.force_running = False
        self.force_button.configure(state="normal")
        self.plan_message.set(f"Forced refresh stopped: {error}")
        self._refresh_scheduler_controls()
        self._refresh_rollback_button()
        messagebox.showerror("Forced refresh stopped", error, parent=self.root)

    def _finish_force_apply(self, returncode: int, output: str) -> None:
        self.force_running = False
        self.force_button.configure(state="normal")
        self._refresh_scheduler_controls()
        self._refresh_rollback_button()
        self.refresh_history()
        self.plan_message.set(
            "Forced reset completed. See worker output and Run history." if returncode == 0
            else "Worker stopped with an error. Review the message and Run history."
        )
        if returncode == 0:
            self.status_var.set("Forced reset applied. Original files are retained in the rollback folder and backup archive.")
            messagebox.showinfo("Forced reset complete", output or "Reset completed successfully.", parent=self.root)
            self.refresh_plan()
        else:
            messagebox.showerror("Forced reset failed", output or "Worker returned an error.", parent=self.root)

    def _selected_modes(self) -> dict[str, str]:
        return {key: MODE_OPTIONS.get(var.get(), "skip") for key, var in self.target_vars.items()}

    @staticmethod
    def _plan_modes(plan: dict[str, Any]) -> dict[str, str]:
        modes = plan.get("target_modes")
        if isinstance(modes, dict):
            return {key: modes.get(key, "skip") for key in TARGET_LABELS}
        targets = plan.get("reset_targets", {})
        return {key: "protect" if targets.get(key) else "skip" for key in TARGET_LABELS}

    def _collect_settings(self) -> tuple[dict[str, str], dict[str, Any]]:
        modes = validate_target_modes(self._selected_modes(), "reset modes")
        schedule: dict[str, Any] = {
            "enabled": self.schedule_enabled.get(),
            "frequency": self.frequency_var.get(),
        }
        for key, var in self.schedule_vars.items():
            value = var.get().strip()
            if key == "timezone":
                schedule[key] = TIMEZONE_OPTIONS.get(value, value)
            elif key in {"minimum_start_buffer_minutes", "allowed_lateness_minutes"}:
                try:
                    schedule[key] = int(value)
                except ValueError as exc:
                    raise ConfigError(f"{key} must be a whole number") from exc
            else:
                schedule[key] = value
        candidate = copy.deepcopy(self.config)
        candidate["reset_targets"] = {key: mode != "skip" for key, mode in modes.items()}
        candidate["schedule"].update(schedule)
        validate_config(candidate)
        return modes, schedule

    def save_settings(self) -> None:
        try:
            config = load_config()
            if config["server"].get("connection_mode") != "local" or self.export.get("executionMode") != "local":
                raise ConfigError("dashboard settings can be saved only in Local hosted mode")
            modes, schedule = self._collect_settings()
            config["desktop_settings"] = {
                "enabled": True,
                "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "reset_targets": {key: mode != "skip" for key, mode in modes.items()},
                "target_modes": modes,
                "schedule": schedule,
            }
            config["safety"]["apply_enabled"] = False
            config["safety"]["staging_and_rollback_tested"] = False
            validate_config(config)
            temporary = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
            temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, CONFIG_PATH)
            self.config = config
            self.source_var.set("Settings source: Local dashboard override (worker and scheduler)")
            self.status_var.set("Dashboard settings saved. Apply remains locked off.")
            self.refresh_plan()
        except (OSError, ConfigError) as exc:
            messagebox.showerror("Could not save settings", str(exc), parent=self.root)

    def clear_overrides(self) -> None:
        try:
            config = load_config()
            config["desktop_settings"] = {"enabled": False}
            temporary = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
            temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, CONFIG_PATH)
            self.config = config
            self.runtime_config = copy.deepcopy(config)
            merge_exported_worker_settings(self.runtime_config, self.export)
            self._load_controls()
            self.status_var.set("Dashboard override removed; worker settings now follow the in-game export.")
            self.refresh_plan()
        except (OSError, ConfigError) as exc:
            messagebox.showerror("Could not clear settings", str(exc), parent=self.root)

    def _scheduler_lock_is_available(self) -> bool:
        from scheduler import acquire_lock

        try:
            lock_file = acquire_lock()
        except RuntimeError:
            return False
        lock_file.close()
        return True

    def _find_scheduler_pid(self) -> int | None:
        try:
            pid = int((ROOT / ".local-scheduler.pid").read_text(encoding="ascii").strip())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            pass
        try:
            result = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match 'scheduler\\.py.*--local' } | ForEach-Object { $_.ProcessId }",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            try:
                return int(line.strip())
            except ValueError:
                continue
        return None

    def _refresh_scheduler_controls(self) -> None:
        force_state = "disabled" if self.force_running else "normal"
        self.force_button.configure(state=force_state)
        if self.scheduler_process is not None and self.scheduler_process.poll() is None:
            self.scheduler_pid = self.scheduler_process.pid
            self.start_scheduler_button.configure(state="disabled")
            self.force_button.configure(state="disabled")
            self.stop_scheduler_button.configure(state="normal")
            self.scheduler_status_var.set(
                f"Running (PID {self.scheduler_pid}); scheduled runs are dry-run only."
            )
            self._refresh_rollback_button()
            return
        previous_pid = self.scheduler_pid
        if self.scheduler_process is not None:
            previous_pid = self.scheduler_process.pid
            self.scheduler_process = None
            if self.scheduler_log_handle:
                self.scheduler_log_handle.close()
                self.scheduler_log_handle = None
        if self._scheduler_lock_is_available():
            self._remove_scheduler_pid_file(previous_pid)
            self.scheduler_pid = None
            self.start_scheduler_button.configure(state="disabled" if self.force_running else "normal")
            self.stop_scheduler_button.configure(state="disabled")
            self.scheduler_status_var.set("Not running")
            self._refresh_rollback_button()
            return
        self.scheduler_pid = self._find_scheduler_pid()
        self.start_scheduler_button.configure(state="disabled")
        self.force_button.configure(state="disabled")
        self.stop_scheduler_button.configure(state="normal" if self.scheduler_pid else "disabled")
        if self.scheduler_pid:
            self.scheduler_status_var.set(
                f"Already running (PID {self.scheduler_pid}); scheduled runs are dry-run only."
            )
        else:
            self.scheduler_status_var.set(
                "A scheduler is running, but its PID could not be identified. See local-scheduler.log."
            )
        self._refresh_rollback_button()

    def start_scheduler(self) -> None:
        try:
            if self.force_running:
                raise ConfigError("wait for the forced reset to finish before starting the scheduler")
            if os.name != "nt":
                raise ConfigError("the dashboard scheduler control is supported on Windows only")
            config = load_config()
            if config["server"].get("connection_mode") != "local":
                raise ConfigError("set server.connection_mode to local before starting the scheduler")
            export = _read_export(config)
            runtime_config = copy.deepcopy(config)
            merge_exported_worker_settings(runtime_config, export)
            if not runtime_config["schedule"].get("enabled"):
                raise ConfigError("enable the schedule in Local settings before starting the scheduler")
            if runtime_config["safety"].get("apply_enabled"):
                raise ConfigError("scheduled apply must remain disabled; this dashboard starts dry-run scheduling only")
            if not self._scheduler_lock_is_available():
                self._refresh_scheduler_controls()
                return

            env = os.environ.copy()
            env["PZRESET_CONFIG"] = str(CONFIG_PATH)
            self.scheduler_log_handle = SCHEDULER_LOG_PATH.open("a", encoding="utf-8", buffering=1)
            self.scheduler_log_handle.write("\n--- Scheduler started from the Local dashboard ---\n")
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                self.scheduler_process = subprocess.Popen(
                    [sys.executable, str(ROOT / "scheduler.py"), "--local"],
                    cwd=ROOT,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=self.scheduler_log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=creation_flags,
                    close_fds=True,
                )
            except OSError:
                self.scheduler_log_handle.close()
                self.scheduler_log_handle = None
                raise
            self.start_scheduler_button.configure(state="disabled")
            self.stop_scheduler_button.configure(state="normal")
            self.scheduler_pid = self.scheduler_process.pid
            try:
                (ROOT / ".local-scheduler.pid").write_text(str(self.scheduler_pid), encoding="ascii")
            except OSError:
                # The scheduler writes its own PID marker after acquiring the lock.
                pass
            self.scheduler_status_var.set(
                f"Running (PID {self.scheduler_process.pid}); scheduled runs are dry-run only."
            )
            self._poll_scheduler()
        except (OSError, ConfigError) as exc:
            messagebox.showerror("Could not start scheduler", str(exc), parent=self.root)

    def stop_scheduler(self) -> None:
        if self.force_running:
            messagebox.showerror("Reset in progress", "Wait for the forced reset to finish before stopping the scheduler.", parent=self.root)
            return
        process = self.scheduler_process
        pid = process.pid if process is not None and process.poll() is None else self.scheduler_pid
        if pid is None:
            self._refresh_scheduler_controls()
            if self.scheduler_pid is None and not self._scheduler_lock_is_available():
                messagebox.showerror(
                    "Could not stop scheduler",
                    "The scheduler is running but its PID could not be identified. See local-scheduler.log or stop it from Task Manager.",
                    parent=self.root,
                )
            return
        if self._scheduler_lock_is_available():
            self.scheduler_status_var.set("Not running")
            self.start_scheduler_button.configure(state="normal")
            self.stop_scheduler_button.configure(state="disabled")
            self.scheduler_pid = None
            return
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            if result.returncode != 0 and not self._scheduler_lock_is_available():
                raise OSError(result.stderr.strip() or "taskkill could not stop the scheduler process tree")
            if self._scheduler_lock_is_available():
                self._remove_scheduler_pid_file(pid)
                self.scheduler_status_var.set("Scheduler stopped")
                self.scheduler_process = None
                self.scheduler_pid = None
                self.start_scheduler_button.configure(state="normal")
                self.stop_scheduler_button.configure(state="disabled")
            else:
                self.scheduler_status_var.set("Stopping scheduler...")
                self._poll_scheduler()
        except (OSError, subprocess.SubprocessError) as exc:
            messagebox.showerror("Could not stop scheduler", str(exc), parent=self.root)

    def _remove_scheduler_pid_file(self, expected_pid: int | None) -> None:
        pid_path = ROOT / ".local-scheduler.pid"
        try:
            if expected_pid is None or pid_path.read_text(encoding="ascii").strip() == str(expected_pid):
                pid_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _poll_scheduler(self) -> None:
        self._refresh_scheduler_controls()
        self.scheduler_poll_id = self.root.after(3000, self._poll_scheduler)

    def close_dashboard(self) -> None:
        if self.force_running:
            messagebox.showinfo(
                "Reset in progress",
                "Keep the dashboard open until the forced reset finishes.",
                parent=self.root,
            )
            return
        if self.scheduler_poll_id:
            self.root.after_cancel(self.scheduler_poll_id)
            self.scheduler_poll_id = None
        if self.scheduler_log_handle:
            self.scheduler_log_handle.close()
            self.scheduler_log_handle = None
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    root.withdraw()
    try:
        config = load_config()
        if config["server"].get("connection_mode") != "local":
            raise ConfigError("set server.connection_mode to local in config.json")
        export = _read_export(config)
    except (ConfigError, OSError) as exc:
        messagebox.showerror("Local mode required", str(exc), parent=root)
        root.destroy()
        return 1
    try:
        root.deiconify()
        LocalDashboard(root, config, export)
    except ConfigError as exc:
        messagebox.showerror("Invalid local settings", str(exc), parent=root)
        root.destroy()
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
