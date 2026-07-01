import json
import os
import re
import sys
import logging
import tempfile
from threading import Lock

logger = logging.getLogger(__name__)

SCHEDULE_TIME_PATTERN = re.compile(r'^\d{2}:\d{2}$')

accounts_lock = Lock()


def get_project_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PROJECT_DIR = get_project_dir()
CONFIG_DIR = os.path.join(PROJECT_DIR, "config")
ACCOUNTS_FILE = os.path.join(CONFIG_DIR, "accounts.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "max_concurrent": 10,
    "request_interval": 1.0,
    "max_rounds": 21,
    "mobile_max_rounds": 7,
    "schedule_time": "08:00",
    "schan_enabled": False,
    "schan_key": "",
}

SETTINGS_SCHEMA = {
    "max_concurrent": {"type": int, "min": 1, "max": 50},
    "request_interval": {"type": float, "min": 0.1, "max": 30.0},
    "max_rounds": {"type": int, "min": 1, "max": 200},
    "mobile_max_rounds": {"type": int, "min": 1, "max": 200},
    "schedule_time": {"type": str},
    "schan_enabled": {"type": bool},
    "schan_key": {"type": str},
}


def validate_settings(settings: dict) -> tuple[bool, str | None, str | None]:
    settings = dict(settings)
    unknown_keys = set(settings.keys()) - set(SETTINGS_SCHEMA.keys())
    if unknown_keys:
        logger.warning("Unknown settings keys ignored: %s", ", ".join(sorted(unknown_keys)))
    for key, schema in SETTINGS_SCHEMA.items():
        if key not in settings:
            if key in DEFAULT_SETTINGS:
                settings[key] = DEFAULT_SETTINGS[key]
            else:
                return False, f"缺少必填设置 {key}", "validation"
            continue
        value = settings[key]
        if not isinstance(value, schema["type"]):
            expected_type = schema["type"].__name__
            actual_type = type(value).__name__
            return False, f"设置 {key} 类型错误，期望 {expected_type}，实际 {actual_type}", "validation"
        if "min" in schema and value < schema["min"]:
            return False, f"设置 {key} 不能小于 {schema['min']}", "validation"
        if "max" in schema and value > schema["max"]:
            return False, f"设置 {key} 不能大于 {schema['max']}", "validation"
    if "schedule_time" in settings and not SCHEDULE_TIME_PATTERN.match(settings["schedule_time"]):
        return False, "时间格式错误，应为 HH:MM", "validation"
    return True, None, None


def load_accounts() -> list[dict]:
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load accounts: %s", e)
            return []
    return []


def _atomic_write(filepath: str, data: str) -> None:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(filepath), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, filepath)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_accounts(accounts: list[dict]) -> None:
    try:
        _atomic_write(ACCOUNTS_FILE, json.dumps(accounts, ensure_ascii=False, indent=4))
    except OSError as e:
        logger.error("Failed to save accounts: %s", e)


def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load settings: %s", e)
            return dict(DEFAULT_SETTINGS)
    return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    try:
        _atomic_write(SETTINGS_FILE, json.dumps(settings, ensure_ascii=False, indent=4))
    except OSError as e:
        logger.error("Failed to save settings: %s", e)
