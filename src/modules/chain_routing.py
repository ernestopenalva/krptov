"""Externally configurable routing for inference, Monitor and social actions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable

import yaml


# Missing configuration preserves the branch's previous behavior (both WLs and
# social admission). The rollout file explicitly narrows EVM to inference only.
DEFAULT_CIRCUITS = {"inference": True, "monitor": True, "social_alert_to_monitor": True}
DEFAULT_SOCIAL_ACTION = {"telegram": True, "monitor": False}


def load_routing_sections(path: Path) -> Dict[str, Any]:
    if not Path(path).exists():
        return {"chain_routing": {}, "social_signal_routing": {}}
    with Path(path).open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    return {
        "chain_routing": loaded.get("chain_routing") or {},
        "social_signal_routing": loaded.get("social_signal_routing") or {},
    }


def chain_route(config: Dict[str, Any], chain: object) -> Dict[str, bool]:
    configured = (config.get("chain_routing") or {}).get(str(chain or "").lower()) or {}
    return {key: bool(configured.get(key, default)) for key, default in DEFAULT_CIRCUITS.items()}


def circuit_enabled(config: Dict[str, Any], chain: object, circuit: str) -> bool:
    return bool(chain_route(config, chain).get(circuit, False))


def social_actions(
    config: Dict[str, Any], chain: object, categories: Iterable[str]
) -> Dict[str, bool]:
    chain_id = str(chain or "").lower()
    matrix = (config.get("social_signal_routing") or {}).get(chain_id) or {}
    telegram = False
    monitor = False
    for category in set(categories):
        action = matrix.get(category) or DEFAULT_SOCIAL_ACTION
        telegram = telegram or bool(action.get("telegram", DEFAULT_SOCIAL_ACTION["telegram"]))
        monitor = monitor or bool(action.get("monitor", DEFAULT_SOCIAL_ACTION["monitor"]))
    monitor = monitor and circuit_enabled(config, chain_id, "social_alert_to_monitor")
    return {"telegram": telegram, "monitor": monitor}
