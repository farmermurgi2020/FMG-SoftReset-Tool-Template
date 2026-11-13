from __future__ import annotations

import base64
import copy
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from config import ROOT, ConfigError, validate_config
from cronjob_sync import pause_cronjob, sync_cronjob
from schedule import github_cron_lines


HTML_PATH = ROOT / "web" / "dedicated_dashboard.html"
CONFIG_PATH = ROOT / "dedicated-config.json"
LOCAL_PREFS_PATH = ROOT / "dedicated-dashboard.local.json"
WORKFLOW_PATH = ".github/workflows/pz-dedicated-reset.yml"
ENVIRONMENT_NAME = "pinhosting"
MAX_REQUEST_BYTES = 2 * 1024 * 1024
API_ROOT = "https://api.github.com"


def _default_config() -> dict[str, Any]:
    return json.loads((ROOT / "dedicated-config.example.json").read_text(encoding="utf-8"))


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return _default_config()
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _save_local_settings(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    config = _load_config()
    github = payload.get("github") or {}
    server_input = payload.get("server") or {}
    schedule = payload.get("schedule") or {}
    modes_input = payload.get("target_modes") or {}

    config["server"].update({
        "name": str(server_input.get("name", "")).strip(),
        "connection_mode": "sftp",
        "sftp_host": str(server_input.get("sftp_host", "")).strip(),
        "sftp_port": int(server_input.get("sftp_port", 22)),
        "sftp_username": str(server_input.get("sftp_username", "")).strip(),
        "sftp_key_file": "",
        "save_dir": str(server_input.get("save_dir", "")).strip(),
        "zone_export": str(server_input.get("zone_export", "")).strip(),
    })
    config["world"]["build"] = str((payload.get("world") or {}).get("build", "")).strip()
    target_modes = {
        key: str(modes_input.get(key, "skip"))
        for key in ("map_chunks", "zombie_population", "animal_population")
    }
    for key, mode in target_modes.items():
        if mode not in {"protect", "reset_all", "skip"}:
            raise ConfigError(f"unknown reset mode for {key}: {mode}")
    if all(mode == "skip" for mode in target_modes.values()):
        raise ConfigError("every reset target is set to Don't touch; nothing would be reset")
    target_settings = {key: mode != "skip" for key, mode in target_modes.items()}
    schedule_settings = {
        "enabled": schedule.get("enabled") is True,
        "frequency": str(schedule.get("frequency", "weekly")),
        "anchor_date": str(schedule.get("anchor_date", "")),
        "time": str(schedule.get("time", "")),
        "timezone": str(schedule.get("timezone", "UTC")),
        "pine_stop_time": str(schedule.get("pine_stop_time", "")),
        "pine_start_time": str(schedule.get("pine_start_time", "")),
        "minimum_start_buffer_minutes": int(schedule.get("minimum_start_buffer_minutes", 30)),
        "allowed_lateness_minutes": int(schedule.get("allowed_lateness_minutes", 30)),
    }
    scheduled_action = str(payload.get("scheduled_action", "dry-run"))
    if scheduled_action not in {"dry-run", "apply"}:
        raise ConfigError("scheduled action must be dry-run or apply")
    scheduled_trigger = str(payload.get("scheduled_trigger", "github"))
    if scheduled_trigger not in {"cronjob", "github"}:
        raise ConfigError("scheduled runs must be started by cron-job.org or the GitHub schedule")
    config["dedicated_settings"] = {
        "enabled": True,
        "reset_targets": target_settings,
        "target_modes": target_modes,
        "schedule": schedule_settings,
        "scheduled_action": scheduled_action,
        "scheduled_trigger": scheduled_trigger,
    }
    config["reset_targets"] = copy.deepcopy(target_settings)
    config["schedule"] = copy.deepcopy(schedule_settings)
    config["safety"]["apply_enabled"] = False
    config["safety"]["staging_and_rollback_tested"] = False
    config["safety"].pop("max_delete_fraction", None)
    validate_config(config)

    preferences = {
        "owner": str(github.get("owner", "")).strip(),
        "repo": str(github.get("repo", "")).strip(),
        "branch": str(github.get("branch", "main")).strip() or "main",
    }
    for path, value in ((CONFIG_PATH, config), (LOCAL_PREFS_PATH, preferences)):
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    return config, preferences


def _load_preferences() -> dict[str, str]:
    try:
        value = json.loads(LOCAL_PREFS_PATH.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return {key: str(value.get(key, "")) for key in ("owner", "repo", "branch")}
    except (OSError, json.JSONDecodeError):
        pass
    return {"owner": "", "repo": "", "branch": "main"}


def _api_request(token: str, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        API_ROOT + path,
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token.strip()}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "FMG-SoftReset-Tool-Dedicated-Setup",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # Do not return API response bodies: errors must never echo credential material.
        raise RuntimeError(f"GitHub API returned HTTP {exc.code} for {method} {path.split('?')[0]}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach GitHub API: {exc.reason}") from None
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _repo_path(owner: str, repo: str, endpoint: str) -> str:
    return f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo, safe='')}{endpoint}"


def _put_repo_file(
    token: str,
    owner: str,
    repo: str,
    branch: str,
    path: str,
    content: bytes,
    message: str,
) -> None:
    encoded_path = urllib.parse.quote(path, safe="/")
    query = urllib.parse.urlencode({"ref": branch})
    get_path = _repo_path(owner, repo, f"/contents/{encoded_path}?{query}")
    try:
        current = _api_request(token, "GET", get_path)
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        current = {}
    body: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": branch,
    }
    if isinstance(current.get("sha"), str):
        try:
            existing = base64.b64decode(current.get("content", ""))
        except (ValueError, TypeError):
            existing = b""
        if existing == content:
            return  # unchanged; avoid an empty commit
        body["sha"] = current["sha"]
    _api_request(token, "PUT", _repo_path(owner, repo, f"/contents/{encoded_path}"), body)


def _workflow_text(schedule: dict[str, Any], github_schedule: bool = True) -> str:
    schedule_block = ""
    # Only GitHub's UTC cron needs the daylight-saving line check; cron-job.org uses local time.
    schedule_env = "          PZ_SCHEDULE_CRON: ${{ github.event.schedule }}\n" if github_schedule else ""
    if not github_schedule:
        # cron-job.org starts scheduled runs; a GitHub schedule as well could reset twice.
        schedule_block = "  # Scheduled runs are started by cron-job.org (Run workflow, action: scheduled).\n"
    elif schedule["enabled"]:
        # GitHub Actions runs cron in UTC only, so the local time is converted here.
        lines = "".join(f"    - cron: '{cron}'\n" for cron in github_cron_lines(schedule))
        schedule_block = (
            f"  # {schedule['time']} {schedule['timezone']}, written in UTC"
            " (one line per daylight-saving offset)\n"
            "  schedule:\n"
            f"{lines}"
        )
    return f'''name: FMG Dedicated Soft Reset
run-name: "FMG Soft Reset: ${{{{ github.event_name == 'schedule' && 'scheduled run (GitHub schedule)' || inputs.action == 'scheduled' && 'scheduled run (cron-job.org)' || inputs.action }}}}"

on:
{schedule_block}  workflow_dispatch:
    inputs:
      action:
        description: What to run. Stop the server in the Pine Hosting panel before apply or rollback. "scheduled" is used by cron-job.org and only runs inside the reset window.
        required: true
        type: choice
        options:
          - dry-run
          - apply
          - rollback
          - benchmark
          - scheduled
        default: dry-run
      confirm_server_name:
        description: For apply or rollback, type the server name exactly as configured
        required: false
        type: string
        default: ''

permissions:
  contents: read

concurrency:
  group: fmg-dedicated-reset
  cancel-in-progress: false

jobs:
  reset:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{{{ github.ref }}}}
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install worker dependencies
        run: python -m pip install -r requirements.txt
      - name: Prepare optional SSH authentication and host-key files
        shell: bash
        env:
          PZ_SFTP_PRIVATE_KEY: ${{{{ secrets.PZRESET_SFTP_PRIVATE_KEY }}}}
          PZ_SFTP_KNOWN_HOSTS: ${{{{ secrets.PZRESET_SFTP_KNOWN_HOSTS }}}}
        run: |
          umask 077
          if [ -n "$PZ_SFTP_PRIVATE_KEY" ]; then
            printf '%s\\n' "$PZ_SFTP_PRIVATE_KEY" > "$RUNNER_TEMP/pz-sftp-key"
            echo "PZRESET_SFTP_KEY_FILE=$RUNNER_TEMP/pz-sftp-key" >> "$GITHUB_ENV"
          fi
          if [ -n "$PZ_SFTP_KNOWN_HOSTS" ]; then
            printf '%s\\n' "$PZ_SFTP_KNOWN_HOSTS" > "$RUNNER_TEMP/pz-known-hosts"
            echo "PZRESET_SFTP_KNOWN_HOSTS=$RUNNER_TEMP/pz-known-hosts" >> "$GITHUB_ENV"
          fi
      - name: Run soft reset worker
        shell: bash
        env:
          PZRESET_CONFIG: dedicated-config.json
          PZRESET_SFTP_PASSWORD: ${{{{ secrets.PZRESET_SFTP_PASSWORD }}}}
          PZRESET_SFTP_KEY_PASSPHRASE: ${{{{ secrets.PZRESET_SFTP_KEY_PASSPHRASE }}}}
          PZ_MODE: ${{{{ github.event_name == 'schedule' && 'scheduled' || inputs.action }}}}
          PZ_CONFIRM: ${{{{ inputs.confirm_server_name }}}}
{schedule_env}        run: python dedicated_workflow.py --mode "$PZ_MODE" --confirm-server-name "$PZ_CONFIRM"
      - name: Keep the run's plan or result
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: reset-plan-${{{{ github.run_id }}}}
          path: reset-plan.json
          if-no-files-found: ignore
          retention-days: 30
'''


def _sync_secret(token: str, owner: str, repo: str, name: str, value: str) -> None:
    if not value:
        return
    try:
        from nacl.public import PublicKey, SealedBox
    except ImportError as exc:
        raise RuntimeError("PyNaCl is required to encrypt GitHub Actions secrets; install requirements.txt") from exc
    key_info = _api_request(token, "GET", _repo_path(owner, repo, "/actions/secrets/public-key"))
    public_key = PublicKey(base64.b64decode(key_info["key"]))
    encrypted_value = SealedBox(public_key).encrypt(value.encode("utf-8"))
    _api_request(
        token,
        "PUT",
        _repo_path(owner, repo, f"/actions/secrets/{urllib.parse.quote(name, safe='')}"),
        {"encrypted_value": base64.b64encode(encrypted_value).decode("ascii"), "key_id": key_info["key_id"]},
    )


def _sync_cronjob_side(
    config: dict[str, Any], preferences: dict[str, str], cronjob: dict[str, Any]
) -> str:
    owner, repo, branch = preferences["owner"], preferences["repo"], preferences["branch"]
    settings = config["dedicated_settings"]
    api_key = str(cronjob.get("api_key", "")).strip()
    github_token = str(cronjob.get("github_token", "")).strip()
    if settings.get("scheduled_trigger") != "cronjob":
        return pause_cronjob(api_key, owner, repo) if api_key else ""
    if not api_key:
        return ("cron-job.org was not updated (no cron-job.org key entered). "
                "Enter the key to create the job or change its time.")
    if github_token:
        # Check the token can see this repository's workflow before handing it to cron-job.org.
        try:
            _api_request(github_token, "GET", _repo_path(owner, repo, "/actions/workflows/pz-dedicated-reset.yml"))
        except RuntimeError as exc:
            raise ConfigError(
                "the GitHub token for cron-job.org cannot read this repository's workflow "
                f"({exc}); give it access to {owner}/{repo} with Actions: Read and write"
            ) from None
    schedule = settings["schedule"]
    return sync_cronjob(api_key, github_token, owner, repo, branch, schedule, schedule["enabled"])


def _sync_to_github(
    config: dict[str, Any],
    preferences: dict[str, str],
    token: str,
    secrets: dict[str, str],
    cronjob: dict[str, Any] | None = None,
) -> str:
    owner, repo, branch = preferences["owner"], preferences["repo"], preferences["branch"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise ConfigError("enter a valid GitHub owner and repository name")
    if not token.strip():
        raise ConfigError("enter a fine-grained GitHub token for this sync")
    repository = _api_request(token, "GET", _repo_path(owner, repo, ""))
    if repository.get("private") is not True:
        raise ConfigError("the target GitHub repository must be private; settings include server paths")
    default_branch = repository.get("default_branch")
    if config["dedicated_settings"]["schedule"]["enabled"] and branch != default_branch:
        raise ConfigError(
            f"scheduled workflows must be synced to the repository default branch ({default_branch})"
        )
    config_json = (json.dumps(config, indent=2) + "\n").encode("utf-8")
    github_schedule = config["dedicated_settings"].get("scheduled_trigger", "github") == "github"
    workflow = _workflow_text(config["dedicated_settings"]["schedule"], github_schedule).encode("utf-8")
    _put_repo_file(token, owner, repo, branch, "dedicated-config.json", config_json, "Update dedicated reset settings")
    _put_repo_file(token, owner, repo, branch, WORKFLOW_PATH, workflow, "Update dedicated reset workflow")

    secret_names = {
        "password": "PZRESET_SFTP_PASSWORD",
        "private_key": "PZRESET_SFTP_PRIVATE_KEY",
        "key_passphrase": "PZRESET_SFTP_KEY_PASSPHRASE",
        "known_hosts": "PZRESET_SFTP_KNOWN_HOSTS",
    }
    uploaded = []
    for field, secret_name in secret_names.items():
        secret_value = secrets.get(field, "")
        if secret_value:
            _sync_secret(token, owner, repo, secret_name, secret_value)
            uploaded.append(secret_name)
    if uploaded:
        summary = "Encrypted GitHub Actions secrets updated: " + ", ".join(uploaded) + "."
    else:
        summary = "No secret fields were changed; existing GitHub Actions secrets were left untouched."
    message = (f"Settings and workflow synced to {owner}/{repo} ({branch}). {summary} "
               f"Scheduled runs: {config['dedicated_settings'].get('scheduled_action', 'dry-run')}.")
    try:
        cronjob_summary = _sync_cronjob_side(config, preferences, cronjob or {})
    except (ConfigError, RuntimeError, ValueError, KeyError) as exc:
        raise RuntimeError(f"{message}\nBut the cron-job.org update failed: {exc}") from None
    return f"{message}\n{cronjob_summary}" if cronjob_summary else message


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "FMG-Dedicated-Setup/1.0"

    def log_message(self, _format: str, *args: Any) -> None:
        # Never log request bodies; they can contain GitHub or SFTP credentials.
        return

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict[str, Any]) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(value).encode("utf-8"))

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ConfigError("request body is empty or too large")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ConfigError("request must be a JSON object")
        return value

    def _check_browser_origin(self) -> None:
        expected = f"http://127.0.0.1:{self.server.server_port}"
        if self.headers.get("Origin") != expected:
            raise ConfigError("request origin rejected; open the dashboard using its local browser address")

    def do_GET(self) -> None:
        if self.path == "/":
            try:
                self._send(200, "text/html; charset=utf-8", HTML_PATH.read_bytes())
            except OSError:
                self._json(500, {"error": "dashboard page is unavailable"})
            return
        if self.path == "/api/config":
            try:
                self._json(200, {"config": _load_config(), "github": _load_preferences()})
            except (OSError, json.JSONDecodeError, ConfigError):
                self._json(500, {"error": "could not load dedicated settings"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            self._check_browser_origin()
            payload = self._read_json()
            config, preferences = _save_local_settings(payload)
            if self.path == "/api/save":
                self._json(200, {"message": "Dedicated settings saved locally. SFTP secrets were not written to disk."})
                return
            if self.path == "/api/sync":
                message = _sync_to_github(
                    config,
                    preferences,
                    str(payload.get("token", "")),
                    payload.get("credentials") or {},
                    payload.get("cronjob") or {},
                )
                self._json(200, {"message": message})
                return
            if self.path == "/api/dispatch":
                action = str(payload.get("action", "dry-run"))
                if action not in {"dry-run", "apply", "rollback"}:
                    raise ConfigError("unknown workflow action")
                confirm = str(payload.get("confirm_server_name", "")).strip()
                if action != "dry-run" and confirm != config["server"]["name"]:
                    raise ConfigError("type the server name exactly to confirm apply or rollback")
                _sync_to_github(
                    config,
                    preferences,
                    str(payload.get("token", "")),
                    payload.get("credentials") or {},
                )
                owner, repo, branch = preferences["owner"], preferences["repo"], preferences["branch"]
                path = _repo_path(
                    owner,
                    repo,
                    "/actions/workflows/pz-dedicated-reset.yml/dispatches",
                )
                _api_request(
                    str(payload.get("token", "")),
                    "POST",
                    path,
                    {"ref": branch, "inputs": {"action": action, "confirm_server_name": confirm}},
                )
                label = {"dry-run": "Read-only dry run", "apply": "Reset apply", "rollback": "Rollback"}[action]
                self._json(200, {"message": f"{label} started for {owner}/{repo}. Track it in the repository Actions tab."})
                return
            self._json(404, {"error": "not found"})
        except (ConfigError, ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            self._json(500, {"error": "request failed; credentials were not saved locally"})


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 8765), DashboardHandler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"FMG Dedicated dashboard: {url}")
    print("Close this window to stop the local dashboard.")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
