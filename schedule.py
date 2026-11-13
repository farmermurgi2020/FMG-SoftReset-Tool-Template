from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any

from config import ConfigError


def validate_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"unknown IANA timezone: {name}") from exc


def fits_provider_window(schedule: dict[str, Any]) -> bool:
    try:
        run = time.fromisoformat(schedule["time"])
        stop = time.fromisoformat(schedule["pine_stop_time"])
        start = time.fromisoformat(schedule["pine_start_time"])
        buffer = int(schedule["minimum_start_buffer_minutes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError("schedule has invalid provider-window values") from exc
    if not 0 <= buffer <= 1440:
        raise ConfigError("minimum_start_buffer_minutes must be between 0 and 1440")

    run_minute = run.hour * 60 + run.minute
    stop_minute = stop.hour * 60 + stop.minute
    start_minute = start.hour * 60 + start.minute
    if stop_minute == start_minute:
        return False
    if stop_minute < start_minute:
        return stop_minute + buffer <= run_minute <= start_minute - buffer
    return run_minute >= stop_minute + buffer or run_minute <= start_minute - buffer


def _monthly_date(anchor: date, month_offset: int) -> date:
    year = anchor.year + (anchor.month - 1 + month_offset) // 12
    month = (anchor.month - 1 + month_offset) % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def occurrence_on_or_after(schedule: dict[str, Any], from_date: date) -> date:
    anchor = date.fromisoformat(schedule["anchor_date"])
    frequency = schedule["frequency"]
    if frequency == "daily":
        if from_date <= anchor:
            return anchor
        return anchor + timedelta(days=(from_date - anchor).days)
    if frequency == "weekly":
        if from_date <= anchor:
            return anchor
        weeks = (from_date - anchor).days // 7
        candidate = anchor + timedelta(weeks=weeks)
        return candidate if candidate >= from_date else candidate + timedelta(weeks=1)
    if frequency == "monthly":
        month_offset = max(0, (from_date.year - anchor.year) * 12 + from_date.month - anchor.month)
        candidate = _monthly_date(anchor, month_offset)
        return candidate if candidate >= from_date else _monthly_date(anchor, month_offset + 1)
    raise ConfigError(f"unsupported schedule frequency: {frequency}")


def next_run(schedule: dict[str, Any], now: datetime | None = None) -> datetime:
    timezone = validate_timezone(schedule["timezone"])
    current = (now or datetime.now(timezone)).astimezone(timezone)
    run_time = time.fromisoformat(schedule["time"])
    candidate_date = occurrence_on_or_after(schedule, current.date())
    candidate = datetime.combine(candidate_date, run_time, tzinfo=timezone)
    if candidate <= current:
        grace = int(schedule.get("allowed_lateness_minutes", 10))
        if current - candidate <= timedelta(minutes=grace):
            return candidate
        candidate_date = occurrence_on_or_after(schedule, current.date() + timedelta(days=1))
        candidate = datetime.combine(candidate_date, run_time, tzinfo=timezone)
    return candidate
