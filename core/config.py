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
    "max_retries": 3,        # 本地原因失败的最大重试次数（不含首次请求）
    "retry_delay": 1.0,      # 重试间隔（秒）
}

SETTINGS_SCHEMA = {
    "max_concurrent": {"type": int, "min": 1, "max": 50},
    "request_interval": {"type": float, "min": 0.1, "max": 30.0},
    "max_rounds": {"type": int, "min": 1, "max": 200},
    "mobile_max_rounds": {"type": int, "min": 1, "max": 200},
    "schedule_time": {"type": str},
    "schan_enabled": {"type": bool},
    "schan_key": {"type": str},
    "max_retries": {"type": int, "min": 0, "max": 10},       # 0=不重试，10=极端场景
    "retry_delay": {"type": float, "min": 0.1, "max": 30.0},  # 与 request_interval 范围一致
}


# settings.json 内存缓存：业务触发读取（reload_settings/save_settings），不在轮询路径上读磁盘
_settings_cache: dict | None = None
_settings_lock = Lock()


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


def _read_settings_from_disk() -> dict:
    """从磁盘读取 settings.json（load_settings 的底层实现）。"""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load settings: %s", e)
            return dict(DEFAULT_SETTINGS)
    return dict(DEFAULT_SETTINGS)


def load_settings() -> dict:
    """读取 settings，优先走内存缓存。

    缓存未初始化时从磁盘读取并缓存；已初始化时直接返回缓存副本。
    需要刷新缓存的场景调用 reload_settings()。
    """
    global _settings_cache
    if _settings_cache is not None:
        return dict(_settings_cache)  # 返回副本，防止调用方 in-place 修改污染缓存
    with _settings_lock:
        if _settings_cache is not None:
            return dict(_settings_cache)
        _settings_cache = _read_settings_from_disk()
        return dict(_settings_cache)


def reload_settings() -> dict:
    """显式从磁盘刷新 settings 缓存。

    在以下场景调用：
    - 程序启动时（初始化缓存）
    - 打开设置页时（GET /api/settings）
    - 领取流程开始前（POST /api/claim）
    """
    global _settings_cache
    with _settings_lock:
        _settings_cache = _read_settings_from_disk()
        return dict(_settings_cache)


def save_settings(settings: dict) -> None:
    """写入 settings 并更新缓存。

    写盘失败时仅记录日志，不更新缓存（避免缓存与磁盘不一致：缓存反映新值但磁盘仍是旧值，
    后续 load_settings 走缓存读到未持久化的"幽灵配置"，重启后丢失）。
    """
    global _settings_cache
    try:
        _atomic_write(SETTINGS_FILE, json.dumps(settings, ensure_ascii=False, indent=4))
    except OSError as e:
        logger.error("Failed to save settings: %s", e)
        return  # 写盘失败，保留旧缓存（与磁盘一致）
    with _settings_lock:
        _settings_cache = dict(settings)  # 缓存写入的副本
