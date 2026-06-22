import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.modules import system_wake_window


BRT = ZoneInfo("America/Sao_Paulo")


class SystemWakeWindowTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "enabled": True,
            "timezone": "America/Sao_Paulo",
            "start": "10:00",
            "end": "02:00",
        }

    def test_cross_midnight_window_boundaries(self):
        self.assertTrue(
            system_wake_window.is_active(datetime(2026, 6, 21, 10, 0, tzinfo=BRT), self.config)
        )
        self.assertTrue(
            system_wake_window.is_active(datetime(2026, 6, 22, 1, 59, tzinfo=BRT), self.config)
        )
        self.assertFalse(
            system_wake_window.is_active(datetime(2026, 6, 22, 2, 0, tzinfo=BRT), self.config)
        )
        self.assertFalse(
            system_wake_window.is_active(datetime(2026, 6, 22, 9, 59, tzinfo=BRT), self.config)
        )

    def test_seconds_until_transitions(self):
        before_open = datetime(2026, 6, 22, 9, 0, tzinfo=BRT)
        before_close = datetime(2026, 6, 22, 1, 0, tzinfo=BRT)

        self.assertEqual(system_wake_window.seconds_until_open(before_open, self.config), 3600)
        self.assertEqual(system_wake_window.seconds_until_close(before_close, self.config), 3600)

    def test_loads_global_yaml(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "config.yaml"
            config_file.write_text(
                "\n".join(
                    [
                        "system_wake_window:",
                        "  enabled: true",
                        '  timezone: "America/Sao_Paulo"',
                        '  start: "11:00"',
                        '  end: "01:00"',
                        "",
                        "social_inference:",
                        "  enabled: true",
                    ]
                ),
                encoding="utf-8",
            )

            global_config = system_wake_window.load_config(config_file)

        self.assertEqual(global_config["start"], "11:00")
        self.assertEqual(global_config["end"], "01:00")


if __name__ == "__main__":
    unittest.main()
