import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"

DEFAULT_CONFIG = {
    "enabled": True,
    "timezone": "America/Sao_Paulo",
    "start": "10:00",
    "end": "02:00",
}


def parse_value(value):
    text = str(value or "").strip().strip('"').strip("'")
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    return text


def load_config(path=CONFIG_FILE):
    config = dict(DEFAULT_CONFIG)
    path = Path(path)
    if not path.exists():
        return config

    in_section = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if not raw_line.startswith(" ") and stripped.endswith(":"):
            in_section = stripped[:-1] == "system_wake_window"
            continue
        if not in_section or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        if key in config:
            config[key] = parse_value(value)
    return config


def parse_hhmm(value, default):
    try:
        hour_text, minute_text = str(value or default).split(":", 1)
        return int(hour_text), int(minute_text)
    except (TypeError, ValueError):
        hour_text, minute_text = default.split(":", 1)
        return int(hour_text), int(minute_text)


def local_now(config=None):
    config = config or DEFAULT_CONFIG
    timezone_name = config.get("timezone") or DEFAULT_CONFIG["timezone"]
    return datetime.now(ZoneInfo(timezone_name)).replace(microsecond=0)


def window_bounds(local_time, config=None):
    config = config or DEFAULT_CONFIG
    start_hour, start_minute = parse_hhmm(config.get("start"), DEFAULT_CONFIG["start"])
    end_hour, end_minute = parse_hhmm(config.get("end"), DEFAULT_CONFIG["end"])
    start = local_time.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end = local_time.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)

    if end <= start:
        if local_time < end:
            start -= timedelta(days=1)
        else:
            end += timedelta(days=1)
    return start, end


def is_active(local_time, config=None):
    config = config or DEFAULT_CONFIG
    if not config.get("enabled", True):
        return True
    start, end = window_bounds(local_time, config)
    return start <= local_time < end


def seconds_until_open(local_time, config=None):
    config = config or DEFAULT_CONFIG
    if is_active(local_time, config) or not config.get("enabled", True):
        return 0
    start, _ = window_bounds(local_time, config)
    if start <= local_time:
        start += timedelta(days=1)
    return max(1, int((start - local_time).total_seconds()))


def seconds_until_close(local_time, config=None):
    config = config or DEFAULT_CONFIG
    if not config.get("enabled", True):
        return 86400
    if not is_active(local_time, config):
        return 0
    _, end = window_bounds(local_time, config)
    return max(1, int((end - local_time).total_seconds()))


def status(config=None, current_time=None):
    config = config or DEFAULT_CONFIG
    current_time = current_time or local_now(config)
    start, end = window_bounds(current_time, config)
    active = is_active(current_time, config)
    return {
        "active": active,
        "enabled": bool(config.get("enabled", True)),
        "timezone": config.get("timezone"),
        "start": config.get("start"),
        "end": config.get("end"),
        "current_time": current_time.isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "seconds_until_open": seconds_until_open(current_time, config),
        "seconds_until_close": seconds_until_close(current_time, config),
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Consulta a vigilia global do KRPTO-V.")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--is-active", action="store_true")
    actions.add_argument("--seconds-until-open", action="store_true")
    actions.add_argument("--seconds-until-close", action="store_true")
    actions.add_argument("--status", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    config = load_config(args.config)
    current_time = local_now(config)
    if args.is_active:
        raise SystemExit(0 if is_active(current_time, config) else 1)
    if args.seconds_until_open:
        print(seconds_until_open(current_time, config))
        return
    if args.seconds_until_close:
        print(seconds_until_close(current_time, config))
        return
    print(json.dumps(status(config, current_time), ensure_ascii=False))


if __name__ == "__main__":
    main()
