"""Storage contract for the independent Monitor watchlist.

The market ranker owns market/ranking fields.  Scheduler and Monitor own runtime
fields.  Keeping those sets separate prevents a live ranking refresh from
clobbering an active monitor or its retry cooldown.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
MONITOR_WATCHLIST_FILE = DATA_DIR / "monitor_watchlist.json"
MONITOR_WATCHLIST_LOCK_FILE = DATA_DIR / "monitor_watchlist.lock"
CAMPAIGN_INDEX_FILE = DATA_DIR / "monitor_campaign_index.json"

RUNTIME_FIELDS = {
    "admission_source",
    "rank_bypass",
    "social_enqueued_at_utc",
    "social_ready_at_utc",
    "social_alert_snapshot",
    "technical_admission_override",
    "technical_redeemed_at_utc",
    "technical_redemption_original_eligibility",
    "technical_redemption_original_reason",
    "monitor_status",
    "monitor_attempts",
    "monitor_started_at_utc",
    "monitor_finished_at_utc",
    "monitor_cooldown_until_utc",
    "monitor_reentry_due",
    "monitor_last_reason",
    "monitor_last_tick_at_utc",
    "monitor_session_id",
    "monitor_attempt_id",
    "position_id",
    "campaign_first_price_usd",
    "campaign_first_price_at_utc",
    "campaign_peak_price_usd",
    "campaign_peak_price_at_utc",
}

# Social-inference state belongs only to the original WL.  These fields must
# never leak into the technical Monitor WL during ranker synchronization.
EXCLUDED_PREFIXES = ("social_", "telegram_", "last_alert_", "best_social_")
EXCLUDED_FIELDS = {
    "best_alert_rank",
    "discarded_reason",
    "status_reason",
    "social_status",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_unlocked(path: Optional[Path] = None) -> Dict[str, Any]:
    path = path or MONITOR_WATCHLIST_FILE
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} precisa ser um dict indexado por watchlist_key")
    return payload


def _save_unlocked(payload: Dict[str, Any], path: Optional[Path] = None) -> None:
    path = path or MONITOR_WATCHLIST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def _campaign_index_unlocked() -> Dict[str, Any]:
    return _load_unlocked(MONITOR_WATCHLIST_FILE.parent / CAMPAIGN_INDEX_FILE.name)


def _save_campaign_index_unlocked(payload: Dict[str, Any]) -> None:
    _save_unlocked(payload, MONITOR_WATCHLIST_FILE.parent / CAMPAIGN_INDEX_FILE.name)


@contextmanager
def monitor_watchlist_lock(timeout_seconds: float = 30, poll_seconds: float = 0.05):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    descriptor: Optional[int] = None
    while descriptor is None:
        try:
            descriptor = os.open(
                MONITOR_WATCHLIST_LOCK_FILE,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(descriptor, str(os.getpid()).encode("ascii"))
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError("Timeout aguardando lock da monitor_watchlist")
            time.sleep(poll_seconds)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            MONITOR_WATCHLIST_LOCK_FILE.unlink()
        except FileNotFoundError:
            pass


def load_monitor_watchlist() -> Dict[str, Any]:
    with monitor_watchlist_lock():
        return _load_unlocked()


def mutate_entry(watchlist_key: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    with monitor_watchlist_lock():
        watchlist = _load_unlocked()
        entry = watchlist.get(watchlist_key)
        if not isinstance(entry, dict):
            return None
        entry.update(updates)
        _save_unlocked(watchlist)
        return entry.copy()


def remove_entry(watchlist_key: str) -> Optional[Dict[str, Any]]:
    with monitor_watchlist_lock():
        watchlist = _load_unlocked()
        removed = watchlist.pop(watchlist_key, None)
        _save_unlocked(watchlist)
        return removed if isinstance(removed, dict) else None


def exclude_and_remove(watchlist_key: str, reason: str, details: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Create the no-new-campaign tombstone and remove the live WL entry."""
    with monitor_watchlist_lock():
        watchlist = _load_unlocked()
        removed = watchlist.pop(watchlist_key, None)
        index = _campaign_index_unlocked()
        index[watchlist_key] = {"excluded_at_utc": utc_now_iso(), "reason": reason,
                                "details": details or {}}
        _save_unlocked(watchlist)
        _save_campaign_index_unlocked(index)
        return removed if isinstance(removed, dict) else None


def _ranker_projection(entry: Dict[str, Any], rank: int) -> Dict[str, Any]:
    projected = {
        key: value
        for key, value in entry.items()
        if key not in RUNTIME_FIELDS
        and key not in EXCLUDED_FIELDS
        and not key.startswith(EXCLUDED_PREFIXES)
    }
    projected["technical_rank"] = rank
    projected["ranked_for_monitor_at_utc"] = utc_now_iso()
    return projected


def sync_ranked_watchlist(ranked_entries: Iterable[tuple[str, Dict[str, Any]]]) -> Dict[str, int]:
    """Merge the current technical ranking without touching live runtime state."""
    ranked = list(ranked_entries)
    ranked_keys = {key for key, _entry in ranked}
    with monitor_watchlist_lock():
        current = _load_unlocked()
        excluded = _campaign_index_unlocked()
        result: Dict[str, Any] = {}
        for rank, (key, source) in enumerate(ranked, start=1):
            if key in excluded:
                continue
            existing = current.get(key) if isinstance(current.get(key), dict) else {}
            runtime = {field: existing[field] for field in RUNTIME_FIELDS if field in existing}
            merged = _ranker_projection(source, rank)
            merged.update(runtime)
            merged.setdefault("admission_source", "technical_rank")
            merged.setdefault("rank_bypass", False)
            merged.setdefault("monitor_status", "eligible")
            merged.setdefault("monitor_attempts", 0)
            result[key] = merged

        # Social bypass entries remain queued even when absent from the ranking.
        # Active/cooldown technical entries remain only until Scheduler finalizes
        # them, avoiding deletion underneath a running worker.
        for key, existing in current.items():
            if key in ranked_keys or not isinstance(existing, dict):
                continue
            keep_runtime = existing.get("rank_bypass") is True or existing.get("monitor_status") in {
                "reserved", "monitoring", "cooldown", "position_open",
            }
            if keep_runtime:
                carried = existing.copy()
                carried["technical_rank"] = None
                result[key] = carried

        _save_unlocked(result)
        return {
            "ranked": sum(key in result for key in ranked_keys),
            "preserved_runtime": sum(key not in ranked_keys for key in result),
        }


def admit_social_alert(
    watchlist_key: str,
    source_entry: Dict[str, Any],
    alert_snapshot: Dict[str, Any],
    admitted_at_utc: Optional[str] = None,
) -> bool:
    admitted_at = admitted_at_utc or utc_now_iso()
    with monitor_watchlist_lock():
        watchlist = _load_unlocked()
        if watchlist_key in _campaign_index_unlocked():
            return False
        existing = watchlist.get(watchlist_key)
        if isinstance(existing, dict) and (
            existing.get("rank_bypass") is True
            or existing.get("monitor_status") in {"reserved", "monitoring", "position_open", "completed"}
        ):
            return False

        entry = existing.copy() if isinstance(existing, dict) else _ranker_projection(source_entry, 0)
        entry.update({
            "watchlist_key": watchlist_key,
            "admission_source": "social_alert",
            "rank_bypass": True,
            "social_enqueued_at_utc": admitted_at,
            "social_ready_at_utc": admitted_at,
            "social_alert_snapshot": alert_snapshot,
            "monitor_status": "eligible",
            "monitor_attempts": int(entry.get("monitor_attempts") or 0),
            "technical_admission_override": "social_alert",
            "technical_redeemed_at_utc": admitted_at,
            "technical_redemption_original_eligibility": source_entry.get("technical_eligibility"),
            "technical_redemption_original_reason": source_entry.get("technical_eligibility_reason"),
        })
        watchlist[watchlist_key] = entry
        _save_unlocked(watchlist)
        return True


def reserve_next_candidate(
    *,
    active_keys: set[str],
    active_social: int,
    max_social: int,
    max_attempts: int,
    now: Optional[datetime] = None,
) -> Optional[tuple[str, Dict[str, Any]]]:
    """Atomically reserve one candidate, social FIFO before technical rank.

    A technical retry gets exactly one live-ranking decision after cooldown. If
    it is not the best available token at that vacancy, its campaign is over.
    """
    current_time = now or datetime.now(timezone.utc)
    now_text = current_time.isoformat(timespec="seconds")
    with monitor_watchlist_lock():
        watchlist = _load_unlocked()
        social_ready = []
        technical_ready = []
        technical_retry_keys = []
        changed = False
        for key, entry in watchlist.items():
            if not isinstance(entry, dict) or key in active_keys:
                continue
            status = entry.get("monitor_status", "eligible")
            attempts = int(entry.get("monitor_attempts") or 0)
            if attempts >= max_attempts:
                continue
            if status == "cooldown":
                ready_at = _parse_iso(entry.get("monitor_cooldown_until_utc"))
                if ready_at is None or current_time < ready_at:
                    continue
                entry["monitor_status"] = "eligible"
                status = "eligible"
                changed = True
                if entry.get("rank_bypass") is True:
                    entry["social_ready_at_utc"] = entry.get("monitor_cooldown_until_utc") or now_text
                else:
                    entry["monitor_reentry_due"] = True
            if status != "eligible":
                continue
            if entry.get("rank_bypass") is True:
                social_ready.append((key, entry))
            elif entry.get("technical_rank") is not None:
                technical_ready.append((key, entry))
                if entry.get("monitor_reentry_due") is True:
                    technical_retry_keys.append(key)

        selected: Optional[tuple[str, Dict[str, Any]]] = None
        if active_social < max_social and social_ready:
            selected = min(
                social_ready,
                key=lambda item: item[1].get("social_ready_at_utc")
                or item[1].get("social_enqueued_at_utc") or "",
            )
        elif technical_ready:
            selected = min(technical_ready, key=lambda item: int(item[1].get("technical_rank") or 10**9))

        selected_key = selected[0] if selected else None
        technical_decision = selected is not None and selected[1].get("rank_bypass") is not True
        for key in technical_retry_keys:
            if technical_decision and key != selected_key:
                entry = watchlist[key]
                entry["monitor_status"] = "completed"
                entry["monitor_finished_at_utc"] = now_text
                entry["monitor_last_reason"] = "missed_live_rank_reentry"
                changed = True

        if selected:
            key, entry = selected
            entry["monitor_status"] = "reserved"
            entry["monitor_reentry_due"] = False
            entry["monitor_session_id"] = None
            entry["monitor_started_at_utc"] = now_text
            changed = True
            selected = (key, entry.copy())
        if changed:
            _save_unlocked(watchlist)
        return selected


def finish_monitor_attempt(
    watchlist_key: str,
    result: Dict[str, Any],
    *,
    max_attempts: int,
    cooldown_minutes: float,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    current_time = now or datetime.now(timezone.utc)
    now_text = current_time.isoformat(timespec="seconds")
    with monitor_watchlist_lock():
        watchlist = _load_unlocked()
        entry = watchlist.get(watchlist_key)
        if not isinstance(entry, dict): return None
        attempts = int(entry.get("monitor_attempts") or 0) + 1
        entry.update({"monitor_attempts": attempts, "monitor_finished_at_utc": now_text,
                      "monitor_last_reason": result.get("reason")})
        if result.get("outcome") == "buy":
            entry["monitor_status"] = "position_open"
            entry["position_id"] = result.get("position_id")
        elif result.get("outcome") == "stopped":
            entry["monitor_status"] = "stopped"
        elif attempts >= max_attempts:
            entry["monitor_status"] = "completed"
        else:
            entry["monitor_status"] = "cooldown"
            entry["monitor_reentry_due"] = False
            entry["monitor_cooldown_until_utc"] = (
                current_time + timedelta(minutes=cooldown_minutes)
            ).isoformat(timespec="seconds")
        _save_unlocked(watchlist)
        return entry.copy()


def reset_stale_live_states() -> int:
    """Paper mode never reconnects to workers/positions from an old process."""
    changed = 0
    with monitor_watchlist_lock():
        watchlist = _load_unlocked()
        for entry in watchlist.values():
            if isinstance(entry, dict) and entry.get("monitor_status") in {
                "reserved", "monitoring", "position_open", "stopped",
            }:
                entry["monitor_status"] = "eligible"
                entry.pop("position_id", None)
                changed += 1
        if changed: _save_unlocked(watchlist)
    return changed


def pop_completed_entries() -> list[tuple[str, Dict[str, Any]]]:
    completed: list[tuple[str, Dict[str, Any]]] = []
    with monitor_watchlist_lock():
        watchlist = _load_unlocked()
        index = _campaign_index_unlocked()
        for key in list(watchlist):
            entry = watchlist.get(key)
            if isinstance(entry, dict) and entry.get("monitor_status") == "completed":
                completed_entry = watchlist.pop(key)
                completed.append((key, completed_entry))
                index[key] = {"excluded_at_utc": utc_now_iso(),
                              "reason": completed_entry.get("monitor_last_reason") or "monitor_campaign_completed",
                              "details": {"attempts": completed_entry.get("monitor_attempts"),
                                          "admission_source": completed_entry.get("admission_source")}}
        if completed:
            _save_unlocked(watchlist)
            _save_campaign_index_unlocked(index)
    return completed


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value: return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
