"""
配置管理模块。

统一管理项目路径与全局设置（settings.json）的读写，提供设置校验、原子写入、
内存缓存等基础设施。账号运行时存储已迁移至 core/db.py（SQLite），本模块仅保留
_load_accounts_json（迁移工具读取旧 accounts.json 的纯函数版本，供 core/db.py 的
migrate_from_json 跨模块调用）。
"""

import copy
import json
import os
import re
import sys
import time
import logging
import tempfile
from threading import Lock

logger = logging.getLogger(__name__)

# 应用版本号（api.py 通过 /api/version 接口对外暴露）
VERSION = "1.0.1"

# 定时执行时间格式校验正则（HH:MM）
SCHEDULE_TIME_PATTERN = re.compile(r'^\d{2}:\d{2}$')

# Chromium/WebView2 不安全端口黑名单（来源：Chromium 源码 net/base/port_util.cc 的 kRestrictedPorts）
# pywebview 在 Windows 上使用 WebView2（Chromium 内核）渲染，访问这些端口的 URL 会被拦截（ERR_UNSAFE_PORT）。
# gui_port 命中此黑名单时，Flask 服务器能正常监听，但 WebView2 无法加载页面 → 白屏。
# 列表涵盖常见服务端口（FTP/SSH/SMTP/DNS/Telnet/IRC 等），防止浏览器被用作代理攻击本机服务。
UNSAFE_PORTS = frozenset({
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77,
    79, 87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123,
    135, 137, 139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515, 526,
    530, 531, 532, 540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993,
    995, 1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000, 6566, 6665,
    6666, 6667, 6668, 6669, 6697, 10080,
})


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
    "gui_port": None,        # GUI 监听端口，None 表示由 OS 动态分配
    "batch_delete_reconfirm": True,            # 非搜索态批量删除全部账号时是否强制弹窗确认（仅 count <= threshold 时生效）
    "batch_delete_reconfirm_threshold": 50,    # 批量删除触发弹窗确认的账号数阈值（超阈值永远弹，不受开关影响）
    "default_claim_target": "all",             # 新增账号默认领取目标（all/pc/mobile）
    "default_account_enabled": True,           # 新增账号默认启用状态
    "default_login_method": "sms",             # 登录弹窗默认登录方式（sms/password），仅影响手动登录弹窗
}

SETTINGS_SCHEMA = {
    "max_concurrent":    {"type": int,   "min": 1,    "max": 999,  "advanced": False, "label": "最大账号并发数", "description": "同时领取权益的账号数上限"},
    "request_interval":  {"type": float, "min": 0.01, "max": 30.0, "advanced": False, "label": "单账号请求间隔", "description": "同一账号两次请求之间的间隔"},
    "max_rounds":        {"type": int,   "min": 1,    "max": 200,  "advanced": True,  "label": "电脑权益领取最大轮数", "description": ""},
    "mobile_max_rounds": {"type": int,   "min": 1,    "max": 200,  "advanced": True,  "label": "手机权益领取最大轮数", "description": ""},
    "schedule_time":     {"type": str,                            "advanced": False, "label": "定时自动领取", "description": "定时自动领取的触发时间"},
    "schan_enabled":     {"type": bool,                            "advanced": False, "label": "领取情况通知", "description": "是否开启 Server 酱通知"},
    "schan_key":         {"type": str,                            "advanced": False, "label": "Server 酱 SendKey", "description": "Server 酱 SendKey"},
    "max_retries":       {"type": int,   "min": 0,    "max": 10,   "advanced": True,  "label": "本地原因导致请求失败后的重试次数", "description": ""},
    "retry_delay":       {"type": float, "min": 0.01, "max": 30.0, "advanced": True,  "label": "本地原因导致请求失败后的每轮重试间隔", "description": ""},
    "gui_port":          {"type": int,   "min": 1,    "max": 65535, "advanced": True,  "nullable": True, "actual_key": "actual_gui_port", "forbidden": UNSAFE_PORTS, "label": "GUI 监听端口", "description": "留空时由系统自动分配；填入端口则优先尝试，失败回退到自动分配"},
    "batch_delete_reconfirm":           {"type": bool,                              "advanced": True,  "label": "批量删除全部账号时强制弹窗确认", "description": "仅在删除数量未达到下方阈值时生效。关闭后，小批量删除全部账号不再弹窗；超过阈值时仍会弹窗"},
    "batch_delete_reconfirm_threshold": {"type": int,   "min": 1,    "max": 1000, "advanced": True,  "label": "批量删除触发弹窗确认的账号数阈值", "description": "批量删除账号数超过此值时强制弹窗，不受上方开关影响"},
    "default_claim_target":  {"type": str, "options": {"all": "全部领取", "pc": "电脑端加速时长", "mobile": "手机端加速时长"}, "advanced": True, "label": "新增账号默认领取目标", "description": ""},
    "default_account_enabled": {"type": bool,                            "advanced": True, "label": "新账号默认启用", "description": ""},
    "default_login_method":  {"type": str, "options": {"sms": "短信验证码", "password": "账号密码"}, "advanced": True, "label": "账号默认登录方式", "description": "只影响手动登录弹窗，与密码自动重登无关联"},
}


# settings.json 内存缓存：业务触发读取（reload_settings/save_settings），不在轮询路径上读磁盘
_settings_cache: dict | None = None
_settings_lock = Lock()


def validate_settings(settings: dict) -> tuple[bool, str | None, str | None]:
    """校验设置值的类型与范围，缺失字段自动填充默认值。

    内部创建入参的浅拷贝，不修改原始字典。未知字段会被记录警告并忽略。

    Args:
        settings: 待校验的设置字典，可包含 SETTINGS_SCHEMA 中定义的任意字段子集

    Returns:
        (是否通过, 错误消息, 错误类别) 三元组。
        通过时返回 (True, None, None)；
        失败时错误类别为 "validation"，错误消息描述具体失败原因。
    """
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
        # nullable 字段允许 None（如 gui_port=null 表示自动分配），跳过类型/范围校验
        if value is None and schema.get("nullable", False):
            continue
        if not isinstance(value, schema["type"]):
            expected_type = schema["type"].__name__
            actual_type = type(value).__name__
            return False, f"设置 {key} 类型错误，期望 {expected_type}，实际 {actual_type}", "validation"
        if "min" in schema and value < schema["min"]:
            return False, f"设置 {key} 不能小于 {schema['min']}", "validation"
        if "max" in schema and value > schema["max"]:
            return False, f"设置 {key} 不能大于 {schema['max']}", "validation"
        # forbidden 黑名单校验（如 gui_port 的 Chromium 不安全端口）
        if "forbidden" in schema and value in schema["forbidden"]:
            return False, f"设置 {key} 的值 {value} 不被允许（WebView2 不安全端口）", "validation"
        # enum 校验：value 必须在 options 的 key 集合中
        if "options" in schema and value not in schema["options"]:
            return False, f"设置 {key} 的值 {value!r} 不在允许的选项中", "validation"
    if "schedule_time" in settings and not SCHEDULE_TIME_PATTERN.match(settings["schedule_time"]):
        return False, "时间格式错误，应为 HH:MM", "validation"
    return True, None, None


def _load_accounts_json(json_path: str = ACCOUNTS_FILE) -> list[dict]:
    """从指定 JSON 文件读取账号列表（纯函数版本，不走缓存/锁）。

    供 core/db.py 的 migrate_from_json 跨模块调用，读取旧 accounts.json
    或 accounts.json.bak 作为迁移数据源。文件不存在或解析失败时返回空列表。

    Args:
        json_path: JSON 文件路径，默认 ACCOUNTS_FILE

    Returns:
        账号字典列表
    """
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load accounts from %s: %s", json_path, e)
            return []
    return []


def _atomic_write(filepath: str, data: str) -> None:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(filepath), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        # Windows 上 os.replace 偶发 PermissionError(13, '拒绝访问')：目标文件被其他线程/进程
        # 短暂占用（如杀软扫描、并发写入）。短重试可消除瞬时冲突，提升真实场景健壮性。
        last_err: PermissionError | None = None
        for attempt in range(3):
            try:
                os.replace(tmp, filepath)
                last_err = None
                break
            except PermissionError as e:
                last_err = e
                if attempt < 2:
                    time.sleep(0.02)
        if last_err is not None:
            raise last_err
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_settings_from_disk() -> dict:
    """从磁盘读取 settings.json（load_settings 的底层实现）。

    读取后用 DEFAULT_SETTINGS 补全缺失字段（不覆盖已有值），确保旧版配置文件
    升级到新版后内存中的 settings 含全部字段，避免前端高级区初始化读到 undefined。
    """
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 合并默认值：缺失字段补全，已有值保留（data 覆盖 DEFAULT_SETTINGS）
            merged = dict(DEFAULT_SETTINGS)
            merged.update(data)
            return merged
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load settings: %s", e)
            return dict(DEFAULT_SETTINGS)
    return dict(DEFAULT_SETTINGS)


def load_settings() -> dict:
    """读取 settings，优先走内存缓存。

    缓存未初始化时从磁盘读取并缓存；已初始化时直接返回缓存副本。
    返回 copy.deepcopy 深拷贝副本，防止调用方修改嵌套对象污染缓存。
    需要刷新缓存的场景调用 reload_settings()。
    """
    global _settings_cache
    if _settings_cache is not None:
        return copy.deepcopy(_settings_cache)
    with _settings_lock:
        if _settings_cache is not None:
            return copy.deepcopy(_settings_cache)
        _settings_cache = _read_settings_from_disk()
        return copy.deepcopy(_settings_cache)


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
        return copy.deepcopy(_settings_cache)


def save_settings(settings: dict) -> None:
    """写入 settings 并更新缓存。

    写盘失败时记日志后向上抛出 OSError，且不更新缓存（避免缓存与磁盘不一致：缓存反映
    新值但磁盘仍是旧值，后续 load_settings 走缓存读到未持久化的"幽灵配置"，重启后丢失）。
    调用方需捕获 OSError 以感知失败。

    并发说明：_atomic_write 在 _settings_lock 外执行以避免阻塞读操作，两个并发
    save_settings 可能导致缓存与磁盘不一致（A 写盘 → B 写盘覆盖 → A 更新缓存 →
    B 更新缓存，中间状态缓存为 A 值但磁盘已是 B 值）。由于写盘与加锁相互独立，
    最终磁盘值（最后完成的写盘）与缓存值（最后获取锁的 save）可能来自不同线程，
    但后续 reload_settings 可纠正。save_settings 是低频操作（用户改设置），当前权衡可接受。
    """
    global _settings_cache
    try:
        _atomic_write(SETTINGS_FILE, json.dumps(settings, ensure_ascii=False, indent=4))
    except OSError as e:
        logger.error("Failed to save settings: %s", e)
        raise  # 写盘失败，保留旧缓存（与磁盘一致），向上传播让调用方感知
    with _settings_lock:
        _settings_cache = copy.deepcopy(settings)  # 缓存写入的深拷贝副本


def migrate_settings() -> None:
    """启动时检测 settings.json 缺失字段并写回磁盘（一次性迁移）。

    读取磁盘配置，用 DEFAULT_SETTINGS 补全缺失字段，如果有补全则写回磁盘。
    幂等：已含全部字段的配置文件不会被重写。

    读取失败时跳过迁移，不崩溃（安全第一）。复用 save_settings 的原子写入逻辑。
    """
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("migrate_settings: 读取失败，跳过迁移: %s", e)
        return
    changed = False
    for key, default in DEFAULT_SETTINGS.items():
        if key not in data:
            data[key] = default
            changed = True
    if changed:
        save_settings(data)
        logger.info("settings.json 已迁移：补全缺失字段")
