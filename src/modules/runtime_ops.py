"""Rate-limited active operational alerts for the trading runtime."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict

from src.modules import telegram_notifier


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RuntimeOpsAlerter:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.ops = config.get("ops_alerts") or {}
        self.telegram = config.get("telegram_alerts") or {}
        self.cooldown = float(self.ops.get("cooldown_seconds", 1800))
        self.last_sent: Dict[str, float] = {}
        self.lock = threading.Lock()
        self.env = telegram_notifier.load_telegram_env(PROJECT_ROOT / ".env")

    def send(self, key: str, module: str, detail: str) -> bool:
        if not self.ops.get("enabled", True) or not self.telegram.get("enabled", True):
            return False
        now = time.monotonic()
        with self.lock:
            if now - self.last_sent.get(key, -self.cooldown) < self.cooldown:
                return False
            self.last_sent[key] = now
        message = (
            "<b>KRPTO-V | Falha operacional</b>\n"
            f"<b>Modulo:</b> {telegram_notifier.escape_html(module)}\n"
            f"<b>Chave:</b> <code>{telegram_notifier.escape_html(key)}</code>\n"
            f"<b>Erro:</b> {telegram_notifier.escape_html(detail)}"
        )
        result = telegram_notifier.send_message(
            telegram_notifier.CHANNEL_SYSTEM,
            message,
            config=self.telegram,
            env=self.env,
        )
        if not result.get("success"):
            print(f"[OPS_ALERT][ERRO] {result.get('error')}", flush=True)
            return False
        return True
