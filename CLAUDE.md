# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

FMG Soft Reset Tool: a Project Zomboid Build 42 mod (mod ID `FMGResetTool`) plus a Python worker that selectively
resets map chunks, zombie population (`zpop`) and animal population (`apop`) while keeping protected zones, and a
"Hard Reset (Player Data only)" that wipes characters and restarts the world clock.
The README is the user-facing source of truth for setup, safety rules and what has been validated.

## Commands

Python is launched with `py -3` on Windows (the CI runner uses Python 3.12). No build step, linter or formatter is configured.

```bash
py -3 -m pip install -r requirements.txt          # paramiko, PyNaCl, tzdata
py -3 -m unittest discover -s tests               # full suite (run from repo root)
py -3 -m unittest tests.test_hard_reset           # one module
py -3 -m unittest tests.test_local_saves.LocalSavesTests.test_select_save_updates_path_and_name   # one test
py -3 desktop_app.py                              # local (Windows) dashboard, Tkinter
py -3 dedicated_web_ui.py                         # dedicated dashboard at http://127.0.0.1:8765
py -3 scheduler.py --local                        # local scheduler without the dashboard
```

There is no Lua interpreter in the toolchain; check Lua with the editor's diagnostics (undefined-global warnings for
game globals are expected).

## Architecture

**Handoff through the game's zone export.** The mod (`mod/Contents/mods/FMGResetTool/42/media/lua/`) owns protected
zones and reset settings and writes `Zomboid/Lua/pz-reset-zones.json` (schema-versioned; includes `build`, `saveName`,
`resetModes`, schedule and safety settings). The Python side never edits game data the mod owns; it reads that export
(`config.merge_exported_worker_settings`) and owns save-file access, staging, rollback and execution. All hosted servers
on one PC share the same export file, so the local worker refuses an export whose `saveName` differs from the selected
save (`local_saves.export_save_mismatch`).

**Two execution paths that mirror each other:**
- Local Windows: `desktop_app.py` → `local_worker.py` (filesystem) and `hard_reset.py`; save selection in `local_saves.py`;
  `scheduler.py` runs scheduled dry runs.
- Linux dedicated (SFTP only; tested on Pine Hosting): GitHub Actions runs `.github/workflows/pz-dedicated-reset.yml`
  → `dedicated_workflow.py --mode <dry-run|apply|rollback|benchmark|scheduled|hard-reset|hard-reset-undo>` →
  `worker.py` (SFTP soft reset, parallel SFTP connections) or `hard_reset_remote.py`. `dedicated_web_ui.py` +
  `web/dedicated_dashboard.html` is a local-only UI that writes `dedicated-config.json` and regenerates the workflow
  file (`_workflow_text`) and pushes both through the GitHub API — so never hand-edit the workflow without updating
  `_workflow_text`. Python code changes must be committed/pushed separately.
- `hard_reset.py` holds the shared hard-reset rules (file lists, note format, `apply_moves` with reverse-on-failure);
  `hard_reset_remote.py` imports them and does the same over SFTP. Keep the two in step.

**Reset model.** Each target has a mode: reset keeping protected zones / reset everything / don't touch
(`config.validate_target_modes`). Chunks are snapped to 8-tile chunks, population files to 256x256-tile cells; a
population cell touching any protected area is kept whole (`geometry.py`). Apply stages kept files, swaps, and keeps
only the newest `softreset.rollback-*` set; rollback restores it.

**Hard reset.** The worker moves player files (players.db, map_t.bin, etc.) into `hardreset.rollback-<UTC>` and writes
`fmg-hard-reset-pending.json` next to the zone export. The mod's `server/FMGHardReset.lua` acts on it at
`OnServerStarted` (only if `saveName` matches `getServerName()`): removes safehouses and factions, empties the Knox
Claim mod's ModData in place (otherwise Knox Claim rebuilds safehouses every 10 minutes), writes `status: done`, and
re-checks after 10 in-game minutes. Safehouses/factions live only in `map_meta.bin`.

**Scheduling.** Dedicated scheduled runs can use GitHub's own `schedule` trigger or cron-job.org, which calls
`workflow_dispatch` with action `scheduled` (GitHub's schedule may not fire reliably); `cronjob_sync.py` creates or
updates a single cron-job.org job. `schedule.py` / `dedicated_workflow.py` still enforce the date, lateness and hosting
provider power-window checks in the run itself. The workflow in this repo ships with the schedule off.

## Conventions and constraints

- Neither worker checks whether the game server is running; stop it (hosting panel or locally) before
  apply/rollback/hard reset. Destructive modes require an exact `--confirm-server-name`.
- Dry run is the default everywhere; scheduled local runs are dry-run only.
- `config.json`, `dedicated-config.json` (dashboard-managed), `dedicated-dashboard.local.json`, plans, backups and
  run history are local/private state and are git-ignored; start from the `*.example.json` files. Secrets come only
  from GitHub Actions secrets or are typed into the dashboard and kept in memory.
- When changing the mod, bump `version=` in `mod/Contents/mods/FMGResetTool/42/mod.info`. `tests/test_mod_package.py`
  checks the package contents.
