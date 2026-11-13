from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import ROOT


HISTORY_DIR = ROOT / "run-history"


def append_run_record(plan: dict[str, Any], run_type: str, status: str) -> Path:
    if run_type not in {"scheduled", "forced", "rollback"}:
        raise ValueError(f"unsupported reset run type: {run_type}")
    counts = plan.get("target_counts", {})
    record = {
        "timestamp": plan.get("applied_at") or plan.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "run_type": run_type,
        "status": status,
        "server": plan.get("server", ""),
        "map_chunks_reset": int(counts.get("map_chunks", {}).get("reset", 0)),
        "zombie_population_reset": int(counts.get("zombie_population", {}).get("reset", 0)),
        "animal_population_reset": int(counts.get("animal_population", {}).get("reset", 0)),
        "reset_targets": plan.get("reset_targets", {}),
    }
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}.json"
    path = HISTORY_DIR / filename
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def load_run_history() -> list[dict[str, Any]]:
    if not HISTORY_DIR.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in HISTORY_DIR.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("timestamp"), str):
            value["_path"] = str(path)
            records.append(value)
    return sorted(records, key=lambda item: item["timestamp"], reverse=True)
