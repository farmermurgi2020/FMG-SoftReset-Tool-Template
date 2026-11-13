"""Keeps one cron-job.org job that starts the dedicated GitHub workflow at the configured local time.

GitHub's own schedule trigger can be delayed, dropped, or not fire at all for some accounts.
cron-job.org instead calls GitHub's "Run workflow" API (workflow_dispatch) with action=scheduled.
The workflow then applies the same checks as a GitHub scheduled run: reset date, reset time,
allowed lateness and the Pine Hosting window, so a call at the wrong time does nothing.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, time
from typing import Any, Callable

from config import ConfigError


CRONJOB_API = "https://api.cron-job.org"
WORKFLOW_FILE = "pz-dedicated-reset.yml"
REQUEST_POST = 1  # cron-job.org requestMethod enum: 0 GET, 1 POST
WEEKDAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def dispatch_url(owner: str, repo: str) -> str:
    return (
        f"https://api.github.com/repos/{urllib.parse.quote(owner, safe='')}/"
        f"{urllib.parse.quote(repo, safe='')}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    )


def cronjob_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    """cron-job.org schedule for the dashboard schedule; cron-job.org handles the timezone itself."""
    run_time = time.fromisoformat(schedule["time"])
    anchor = date.fromisoformat(schedule["anchor_date"])
    frequency = schedule["frequency"]
    wdays: list[int] = [-1]
    mdays: list[int] = [-1]
    if frequency == "weekly":
        wdays = [(anchor.weekday() + 1) % 7]  # cron-job.org: Sunday = 0
    elif frequency == "monthly":
        # Days 29-31 do not exist in every month; the worker clamps them to the month's last day,
        # so fire on 28-31 and let the worker skip the days that are not the reset date.
        mdays = [anchor.day] if anchor.day <= 28 else [28, 29, 30, 31]
    elif frequency != "daily":
        raise ConfigError(f"unsupported schedule frequency: {frequency}")
    return {
        "timezone": schedule["timezone"],
        "expiresAt": 0,
        "hours": [run_time.hour],
        "minutes": [run_time.minute],
        "mdays": mdays,
        "months": [-1],
        "wdays": wdays,
    }


def describe_schedule(schedule: dict[str, Any]) -> str:
    frequency = schedule["frequency"]
    when = f"{schedule['time']} {schedule['timezone']}"
    if frequency == "daily":
        return f"every day at {when}"
    anchor = date.fromisoformat(schedule["anchor_date"])
    if frequency == "weekly":
        return f"every {WEEKDAY_NAMES[(anchor.weekday() + 1) % 7]} at {when}"
    return f"on day {anchor.day} of every month at {when}"


def _request(api_key: str, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        CRONJOB_API + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
            "User-Agent": "FMG-SoftReset-Tool-Dedicated-Setup",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # Never return response bodies: errors must not echo credential material.
        hint = {
            401: " (check the cron-job.org API key, and its IP allow-list if you set one)",
            403: " (the cron-job.org API key is not allowed to do this; check its IP allow-list)",
            429: " (cron-job.org rate limit reached; try again later)",
        }.get(exc.code, "")
        raise RuntimeError(f"cron-job.org returned HTTP {exc.code} for {method} {path}{hint}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach cron-job.org: {exc.reason}") from None
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def sync_cronjob(
    api_key: str,
    github_token: str,
    owner: str,
    repo: str,
    branch: str,
    schedule: dict[str, Any],
    enabled: bool,
    request: Callable[..., dict[str, Any]] = _request,
) -> str:
    """Create or update the job for owner/repo. Returns a one-line summary for the dashboard."""
    url = dispatch_url(owner, repo)
    jobs = request(api_key, "GET", "/jobs").get("jobs", [])
    existing = next((job for job in jobs if job.get("url") == url), None)

    if not enabled and existing is None:
        return "cron-job.org: no job exists and scheduled runs are off, so nothing was created."

    job: dict[str, Any] = {
        "url": url,
        "title": f"FMG Soft Reset - {owner}/{repo}",
        "enabled": enabled,
        "saveResponses": True,
        "requestMethod": REQUEST_POST,
        "schedule": cronjob_schedule(schedule),
        "notification": {"onFailure": True, "onSuccess": False, "onDisable": True},
    }
    if github_token.strip():
        job["extendedData"] = {
            "headers": {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {github_token.strip()}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            "body": json.dumps({"ref": branch, "inputs": {"action": "scheduled", "confirm_server_name": ""}}),
        }
    elif existing is None:
        raise ConfigError("enter the GitHub fine-grained token for cron-job.org so the new job can start the workflow")

    if existing is None:
        created = request(api_key, "PUT", "/jobs", {"job": job})
        verb = f"job created (id {created.get('jobId', '?')})"
    else:
        request(api_key, "PATCH", f"/jobs/{int(existing['jobId'])}", {"job": job})
        verb = f"job {int(existing['jobId'])} updated"
        if "extendedData" not in job:
            verb += " (GitHub token kept)"
    state = describe_schedule(schedule) if enabled else "paused"
    return f"cron-job.org: {verb}, {state}."


def pause_cronjob(
    api_key: str, owner: str, repo: str, request: Callable[..., dict[str, Any]] = _request
) -> str:
    """Pause the job when GitHub's own schedule is chosen, so the reset cannot run twice."""
    url = dispatch_url(owner, repo)
    jobs = request(api_key, "GET", "/jobs").get("jobs", [])
    existing = next((job for job in jobs if job.get("url") == url), None)
    if existing is None:
        return "cron-job.org: no job to pause."
    request(api_key, "PATCH", f"/jobs/{int(existing['jobId'])}", {"job": {"enabled": False}})
    return f"cron-job.org: job {int(existing['jobId'])} paused (GitHub schedule is selected)."
