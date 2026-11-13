# FMG Soft Reset Tool

Backend for the **FMG Soft Reset Tool** Project Zomboid Build 42 mod. It resets
your multiplayer world (map chunks, zombie population, animal population) while
keeping safehouses and the areas you protect in game.

The Steam Workshop mod gives admins the in-game tools: protected zones and reset
settings. This repository does the actual reset while the server is stopped.

## Features

- **Protected zones.** Multiplayer safehouses are protected automatically. Admins
  draw, name, rename and remove extra protected areas on the in-game world map.
- **Three choices per data type.** Map chunks, zombie population (`zpop`) and
  animal population (`apop`) each get one of:
  - **Reset, keep protected zones**: reset everything that doesn't overlap a
    protected zone or safehouse.
  - **Reset everything**: ignore the zones for that data type.
  - **Don't touch**: leave it as it is.
- **Dry run first.** Every plan shows how many chunks and cells would be kept and
  reset before anything changes.
- **Reset now or on a schedule** (daily, weekly or monthly, in your timezone).
- **One-step rollback.** The original folders are kept in a
  `softreset.rollback-<time>` folder inside the save. Rolling back swaps them
  back, and a rollback can itself be undone. Only the newest rollback folder is
  kept.
- **Safe swaps.** Protected files are copied and checked before the live folders
  are swapped. If a swap fails, the originals are put back.
- **Hard Reset (Player Data only).** Removes every character so players start
  fresh, restarts the world at day 1, and removes all safehouses and factions
  (and Knox Claim land and vehicle claims). The map, zombies, animals, vehicles
  and admin accounts are kept, and it can be undone. See
  [Hard Reset](#hard-reset-player-data-only).

## Supported setups

| Server | How the reset runs | Status |
|---|---|---|
| Windows, locally hosted (Host from the main menu) | Desktop dashboard on the same PC | Tested |
| Linux dedicated server with SFTP access | GitHub Actions in your own private copy of this repository, set up from a dashboard on your Windows PC | Tested on Pine Hosting |

Other hosts may work if they give SFTP access to the save folder, but they are
not tested. Windows dedicated servers are not supported.

## What you need

- The **FMG Soft Reset Tool** mod from the Steam Workshop (mod ID
  `FMGResetTool`), enabled on the server.
- A Windows PC with [Python 3.12 or newer](https://www.python.org/downloads/)
  (tick "Add python.exe to PATH" while installing it).
- For dedicated servers: a free GitHub account and your host's SFTP details.

## Step 1: Make your own copy of this repository

1. Sign in to GitHub and click **Use this template** → **Create a new
   repository** at the top of this page.
2. Give it a name and choose **Private**. Keep it private: the dashboard stores
   your server's SFTP host, username and save paths in it, and the run logs
   show your file layout.
3. On your new repository, click **Code** → **Download ZIP** and unzip it on
   your Windows PC (or clone it with Git).
4. Open a Command Prompt in that folder and install the requirements once:

   ```
   py -3 -m pip install -r requirements.txt
   ```

Locally hosted servers only need the downloaded copy; GitHub isn't used after
this step.

## Step 2: Set up the mod in game

1. Add the mod to your server (Workshop item **FMG Soft Reset Tool**, mod ID
   `FMGResetTool`) and start it.
2. Join as an admin, open the world map, right-click it and choose
   **FMG-SoftReset-Tool**.
3. **Protected Zones** tab: type a name, click **Add zone on map...** and drag
   across the map. Safehouses are added automatically.
4. **Soft Reset Settings** tab: choose **Local hosted** or **Dedicated hosted**,
   pick a reset mode for map chunks, zombie population and animal population,
   and click **Save settings**.

The server writes its zones and settings to `Zomboid/Lua/pz-reset-zones.json`.
The backend reads that file.

## Step 3a: Locally hosted Windows server

1. Copy `config.example.json` to `config.json` and edit it:
   - `server.save_dir`: your save folder, for example
     `C:\Users\<you>\Zomboid\Saves\Multiplayer\servertest`
   - `server.zone_export`: `C:\Users\<you>\Zomboid\Lua\pz-reset-zones.json`
   - `world.build`: copy the exact `build` value from `pz-reset-zones.json`

   Use double backslashes (`\\`) in JSON paths. `config.json` stays on your PC.
2. Double-click `run-local-dashboard.bat`.
3. **Zones and reset plan** → **Refresh export and dry-run plan** shows what
   would be kept and reset.
4. To reset: **fully exit the game**, refresh the plan, then click **Force Chunk
   Refresh** and confirm. A backup archive (in `backups/`) and a rollback folder
   are made first.
5. **Roll Back Latest Reset** restores the latest rollback folder.

The reset modes come from the in-game panel. You can also change them in the
dashboard's **Local settings** tab; whichever was saved last is used.
**Start Scheduler** runs scheduled dry-run plans; resets on a local server are
always started by hand.

## Step 3b: Linux dedicated server (SFTP)

The reset runs on GitHub's servers, in your private copy of this repository, and
connects to your game server over SFTP. Your PC only needs to be on while you
change settings.

### Create a GitHub token

On GitHub go to **Settings** → **Developer settings** → **Fine-grained tokens**
→ **Generate new token**:

- **Repository access**: only your private copy.
- **Repository permissions**, Read and write: **Contents**, **Workflows**,
  **Actions**, **Secrets**.

The dashboard only uses the token while the page is open; it is never saved.

### Configure and sync

1. Double-click `run-dedicated-dashboard.bat`. It opens at
   `http://127.0.0.1:8765`.
2. **GitHub destination**: **Repository owner** is your GitHub user name.
   **Private repository** is the name of your copy. **Branch** is `main`. Paste
   the **Fine-grained token**.
3. **SFTP and save paths**:
   - **SFTP host**, **SFTP port** and **SFTP username** come from your host's
     control panel.
   - **Server name**: any name you choose. You type it again to confirm each
     apply or rollback.
   - **Remote save directory**: for example
     `/.cache/Saves/Multiplayer/<ServerName>` on Pine Hosting.
   - **Remote zone export path**: for example `/.cache/Lua/pz-reset-zones.json`.
   - **Build identifier**: the exact `build` value from that export file.
4. **SFTP credentials**: enter the SFTP password, or a private key if your host
   uses SSH keys. The dashboard sends them straight to your repository's
   encrypted **Actions secrets**. They are never saved on your PC or committed
   to the repository.
5. **Reset and schedule**: choose a reset mode per data type, the schedule and
   your host's stop and start times.
6. **Cron-job keys** (for scheduled runs, see below): leave **Scheduled runs are
   started by** on **cron-job.org (recommended)** and enter both keys. Then
   click **Sync settings and credentials to GitHub**.

### Run it

1. **Dry run:** click **Run dry-run now** and open your repository's **Actions**
   tab to see the plan and summary. Nothing is changed.
2. **Apply:** stop the server in your host's panel and wait until it is offline.
   Then type the server name in **Reset now / roll back** and click **Apply reset
   now**. On a ~250,000-chunk map this took about 12 minutes.
3. **Roll back:** with the server stopped, click **Roll back latest reset**.
4. **Scheduled apply (optional):** set **Scheduled runs do** to **Apply the
   reset** and sync again. Use your host's power schedule to stop the server
   before the reset time. Scheduled runs only act inside your stop/start window.
   Keep scheduled runs on dry run until you have tested a manual apply and
   rollback.

You can also start runs from the **Actions** tab: choose **FMG Dedicated Soft
Reset** → **Run workflow**, pick the action and type the server name.

### Scheduled runs with cron-job.org (recommended)

GitHub's built-in schedule can start late, skip runs, or never fire at all for
some accounts. [cron-job.org](https://cron-job.org) is a free service that
instead presses your workflow's **Run workflow** button at the reset time. You
don't set anything up on their site: when you sync, the dashboard creates the
job (or updates the same job) with the day, time and timezone from **Reset and
schedule**. It uses your local time, so daylight saving time is handled for you.

1. Create a free cron-job.org account. Under **Settings** → **API**, create an
   API key. You can leave its IP allow-list empty.
2. Create a second GitHub fine-grained token for cron-job.org with the least
   access: **Repository access** only your private copy, **Repository
   permissions** only **Actions: Read and write**. This token is stored inside
   the cron-job.org job, which is why it is kept separate from your main token.
3. In the dashboard's **Cron-job keys** section, choose **cron-job.org
   (recommended)**, paste the **cron-job.org API key** and the **Corresponding
   GitHub fine-grained token**, then sync. The status line shows, for example,
   `cron-job.org: job created (id …), every Friday at 01:45 America/Toronto.`

Good to know:

- Each sync updates the same job; it never creates duplicates. On later syncs
  you can leave both keys blank: a blank cron-job.org key leaves the job as it
  is, and a blank GitHub token keeps the token already in the job. Enter the
  cron-job.org key again whenever you change the schedule, so the job's time
  is updated too.
- A run started by cron-job.org shows as **scheduled run (cron-job.org)** in the
  Actions tab. It does the same checks as any scheduled run (reset date, time,
  allowed lateness and your host's stop/start window) and does nothing outside
  them.
- With cron-job.org selected, the workflow has no GitHub schedule, so a reset
  can never run twice. Turning **Enable scheduled dedicated runs** off and
  syncing with the key pauses the job; choosing GitHub's built-in schedule
  instead pauses it too.
- The keys are only kept in the page's memory and are cleared after a
  successful sync. If a key leaks, delete it on cron-job.org or GitHub and
  create a new one.

The tool does **not** check whether the server is running, so always stop it
first. No backup is kept on GitHub, so keep your host's own backups turned on.

## Hard Reset (Player Data only)

For a fresh start for players without wiping the map: everyone makes a new
character, and the world starts again at day 1.

**Removed:** every character (inventory, skills, health), dead players'
zombies, each player's revealed map and map markings, the world clock (day
count, time, weather), the vegetation growth clock and the radio/TV schedule.
On the next server start the mod also removes **all safehouses and factions**.
If the [Knox Claim](https://steamcommunity.com/sharedfiles/filedetails/?id=3776531408)
mod is installed, its land claims, vehicle claims and pending requests are
cleared too. Otherwise it would rebuild the removed safehouses.

**Kept:** map chunks (buildings, loot and player-built bases), zombie and animal
population, vehicles (they stay where they are, unclaimed), accounts, passwords
and admin roles. Run a soft reset **first** if you want the map refreshed too:
once safehouses are gone they no longer protect anything.

**How to run it** (stop the server first, as for any reset):
- **Locally hosted:** in the local dashboard, open the **Hard reset (player
  data)** tab, check the list, click **Hard Reset (Player Data only)** and type
  the server name.
- **Linux dedicated:** in the dedicated dashboard's **Hard Reset (Player Data
  only)** card, type the server name and click the button (or use **Run
  workflow** with action `hard-reset`). Sync once after updating, so the
  workflow knows the new actions.
- Then start the server. The console shows `[FMGResetTool] Hard reset finished:
  removed N safehouses and M factions.`, and 10 in-game minutes later a check
  confirms none came back. Your server must run mod version 0.4.2 or newer.

**Undo:** the removed files are kept in a `hardreset.rollback-<time>` folder in
the save, together with copies of `map_meta.bin` and `global_mod_data.bin`.
**Undo latest hard reset** (or action `hard-reset-undo`) puts everything back:
characters, day count, safehouses, factions and claims. Building changes and
every mod's saved data changed after the hard reset are lost on undo. Only the
newest hard reset rollback is kept, so running a second hard reset removes the
undo copy of the first.

## Good to know

- Population files cover 256×256-tile cells. A cell touched by any protected
  zone is kept whole. `zpop_ids.bin` and `zpop_virtual.bin` are always kept.
  Animals that have already spawned may persist.
- "Reset everything" on map chunks leaves an empty `map` folder, and the game
  regenerates the world as players explore.
- Non-PvP zones are not treated as protected.
- Build 42 only.
- Private repositories on a free GitHub plan include 2,000 Actions minutes a
  month, which is plenty for weekly resets. After each apply, the previous
  rollback folder is deleted. Over SFTP that takes time: about 45 minutes for
  ~250,000 files on Pine Hosting. It runs after the new map is in place and
  only touches the old rollback folder, so the server can start meanwhile.

## Updating

When this template is updated, copy the changed files into your own repository
(or create a fresh copy and sync your settings from the dashboard again).
