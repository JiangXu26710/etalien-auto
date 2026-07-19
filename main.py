"""CLI 入口：并发执行账号领取任务并输出结果。"""

import ctypes
import json
import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler

import requests

from core.config import reload_settings, migrate_settings, CONFIG_DIR, load_settings
from core.db import DbAccountRepository, DB_PATH, is_db_valid
from core.notify import send_claim_notification, send_schan
from core.service import run_concurrent_claim, format_duration

# CLI 退出码定义
EXIT_OK = 0              # 全部成功
EXIT_PARTIAL = 1         # 部分成功
EXIT_ERROR = 2           # 全部失败
EXIT_NEED_LOGIN = 3      # 有账号需要登录
EXIT_NO_ACCOUNTS = 4     # 无启用账号
EXIT_NETWORK_ERROR = 5   # 网络/服务端错误（有账号因网络或服务端错误未能领取）
EXIT_ALREADY_RUNNING = 6  # 已有实例在运行（统一 Mutex 已存在）/ 端口文件残留但 GUI 已退出
EXIT_NO_DB = 7           # 无 db，需用户介入迁移（仅 GUI 能创建 db）
EXIT_NOTIFIED_GUI = 8    # CLI 通知 GUI 触发领取后退出

# 标志位：_setup_logging 完成后置 True，_send_no_db_notification 据此判断是否可用 logger
# 在 _setup_logging 失败/未调用时，_send_no_db_notification 的 logger.error 无法写入 cli.log
# （root logger 无 handler），此时用 sys.stderr 兜底输出
_logging_ready = False


def _setup_logging(log_file: str | None = None):
    """配置根日志记录器。

    Args:
        log_file: 可选日志文件路径。若提供，则同时输出到控制台和按大小轮转的文件。
    """
    global _logging_ready
    handlers = [logging.StreamHandler(sys.stderr)]
    if log_file:
        try:
            dir_name = os.path.dirname(log_file)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            # 按大小轮转：5MB 一份，保留 3 个备份，避免计划任务长期运行后日志无限增长
            handlers.append(RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            ))
        except OSError as e:
            # 日志目录创建/文件打开失败时 fallback 到仅 stderr，避免启动失败
            print(f"[WARN] 无法初始化日志文件 {log_file}: {e}", file=sys.stderr)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=handlers,
    )
    _logging_ready = True


# 手机号脱敏正则（1 开头的 11 位数字，覆盖中国大陆号段）
_PHONE_RE = re.compile(r'1[3-9]\d{9}')


def _mask_label(label: str) -> str:
    """对 label 中的 11 位手机号脱敏，保留前 3 后 4 位（如 138****1234）。

    用于日志输出，避免 cli.log 长期轮转保留导致手机号泄露。
    """
    return _PHONE_RE.sub(lambda m: m.group(0)[:3] + '****' + m.group(0)[-4:], label)


def main(auto_close: bool = False):
    """CLI 主流程：加载配置、并发领取、汇总结果并退出。

    Args:
        auto_close: 是否在计划任务模式下发送通知并自动退出。

    无返回值——通过 sys.exit 退出，退出码遵循模块级常量定义。
    """
    logger = logging.getLogger(__name__)

    # 迁移旧版 settings.json（补全缺失字段，幂等；在初始化缓存前执行，确保缓存含完整字段）
    migrate_settings()
    settings = reload_settings()
    repo = DbAccountRepository()
    enabled = repo.list_enabled()

    if not enabled:
        logger.error("没有启用的账号，请在 config/accounts.db 中添加")
        sys.exit(EXIT_NO_ACCOUNTS)

    logger.info("加载 %d 个启用账号", len(enabled))

    max_concurrent = settings.get("max_concurrent", 10)
    logger.info("开始并发领取(最大并发: %d)", max_concurrent)

    results = run_concurrent_claim(enabled, settings)

    need_login = []
    network_errors = []
    total_vip_before = 0
    total_vip_after = 0
    has_error = False
    has_partial = False
    has_done = False

    # 遍历结果，累加各账号 VIP 时长变化并分类统计状态
    for r in results:
        vip_b = r.get("vip_before", 0)
        vip_a = r.get("vip_after", 0)
        if isinstance(vip_b, int):
            total_vip_before += vip_b
        if isinstance(vip_a, int):
            total_vip_after += vip_a

        if r["status"] == "need_login":
            need_login.append(r)
        elif r["status"] == "network_error":
            network_errors.append(r)
        elif r["status"] == "error":
            has_error = True
        elif r["status"] == "partial":
            has_partial = True
        elif r["status"] in ("done", "already_done"):
            has_done = True

    status_map = {
        "done": "完成",
        "partial": "部分完成",
        "already_done": "已完成",
        "need_login": "需要登录",
        "network_error": "网络错误",
        "error": "错误",
    }
    for r in results:
        status_str = status_map.get(r["status"], r["status"])
        vip_b = r.get("vip_before", 0)
        vip_a = r.get("vip_after", 0)
        label = _mask_label(r["label"])
        if isinstance(vip_b, int) and isinstance(vip_a, int):
            diff = vip_a - vip_b
            logger.info("%s: %s  VIP: %s -> %s (+%s)",
                        label, status_str,
                        format_duration(vip_b), format_duration(vip_a), format_duration(diff))
        else:
            logger.info("%s: %s", label, status_str)

    logger.info("总VIP时长: %s -> %s (+%s)",
                format_duration(total_vip_before), format_duration(total_vip_after),
                format_duration(total_vip_after - total_vip_before))

    if need_login:
        logger.warning("以下账号需要重新登录（请在GUI中操作）:")
        for r in need_login:
            logger.warning("  - %s", _mask_label(r["label"]))

    if network_errors:
        logger.warning("以下账号因网络/服务端错误未能领取（下次运行可能自动恢复）:")
        for r in network_errors:
            logger.warning("  - %s", _mask_label(r["label"]))

    # 仅在 --auto-close 模式（计划任务）下推送通知
    if auto_close:
        try:
            send_claim_notification(results)
        except Exception:
            logger.exception("Server酱通知发送异常（不影响退出码）")

    # 退出码优先级：NEED_LOGIN > NETWORK_ERROR > ERROR > PARTIAL > OK
    # need_login/network_errors 优先返回，确保登录过期/网络问题不被其他状态掩盖
    if need_login:
        sys.exit(EXIT_NEED_LOGIN)
    if network_errors:
        sys.exit(EXIT_NETWORK_ERROR)
    if has_error and not has_done and not has_partial:
        sys.exit(EXIT_ERROR)
    if has_partial or (has_error and has_done):
        sys.exit(EXIT_PARTIAL)
    sys.exit(EXIT_OK)


# 统一 Named Mutex 名（GUI 与 CLI 共用，任何模式只能开一个实例）
_MUTEX_NAME = "Local\\etalien-auto-mutex"
_ERROR_ALREADY_EXISTS = 183


def _create_mutex():
    """创建统一 Named Mutex，返回 (handle, already_exists)。

    handle 为 0 表示创建失败；already_exists 为 True 表示已有实例在运行。
    进程退出时 OS 自动释放 Mutex（崩溃也安全）。
    """
    # CreateMutexW 返回 HANDLE，0 表示失败
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    last_error = ctypes.windll.kernel32.GetLastError()
    already_exists = (last_error == _ERROR_ALREADY_EXISTS)
    return handle, already_exists


def _cli_exit(code: int, auto_close: bool):
    """根据退出码与 auto_close 决定是否等待回车后退出。

    auto_close=True（计划任务）模式下不等待回车，避免无人交互时任务卡死、Mutex 不释放；
    EXIT_NO_DB 在 auto_close 模式下已通过 Server酱 通知用户，无需 input 等待。
    auto_close=False（交互模式）下保留窗口等待回车。
    """
    if not auto_close:
        try:
            input("按回车键退出...")
        except Exception:
            pass
    sys.exit(code)


def _notify_gui_trigger_claim() -> int:
    """通知 GUI 触发一次领取，返回退出码。

    读 .gui_port 文件（JSON 格式：{"port": <int>, "token": <hex str>}）→
    HTTP POST /api/cli-trigger-claim，携带 Authorization: Bearer <token>。
    - 文件不存在/空/无效 → EXIT_ALREADY_RUNNING（已运行的是 CLI）
    - 收到 HTTP 响应（202/409/其他状态码）→ EXIT_NOTIFIED_GUI
    - 连接失败/超时 → EXIT_ALREADY_RUNNING（端口文件残留但 GUI 已退出）
    """
    logger = logging.getLogger(__name__)

    port_file = os.path.join(CONFIG_DIR, ".gui_port")
    if not os.path.exists(port_file):
        print("已有 CLI 实例在运行（无 GUI 端口文件）")
        return EXIT_ALREADY_RUNNING

    try:
        with open(port_file, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except OSError as e:
        print(f"读取 GUI 端口文件失败: {e}")
        return EXIT_ALREADY_RUNNING

    if not content:
        print("已有 CLI 实例在运行（GUI 端口文件为空）")
        return EXIT_ALREADY_RUNNING

    # 解析 JSON：{"port": <int>, "token": <hex str>}
    try:
        data = json.loads(content)
        port = int(data["port"])
        token = data.get("token", "")
    except (ValueError, KeyError, TypeError):
        print(f"GUI 端口文件内容无效: {content!r}")
        return EXIT_ALREADY_RUNNING

    # token 格式校验：非空时必须为 32 字符 hex（gui/app.py secrets.token_hex(16) 生成）
    # 防止 .gui_port 被篡改注入恶意 token 触发非预期 GUI 行为
    if token and not re.match(r'^[0-9a-f]{32}$', token):
        print(f"GUI 端口文件 token 格式无效: {token!r}")
        return EXIT_ALREADY_RUNNING

    url = f"http://127.0.0.1:{port}/api/cli-trigger-claim"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = requests.post(url, timeout=5, headers=headers)
    except (requests.ConnectionError, requests.Timeout) as e:
        print(f"无法连接 GUI，可能已退出（端口文件残留，下次 GUI 启动会自动覆盖）: {e}")
        logger.warning("GUI 连接失败（端口文件残留，GUI 可能已退出）: %s", e)
        return EXIT_ALREADY_RUNNING

    # 记录 HTTP 响应体摘要（截断 200 字符，避免日志膨胀）
    body_preview = (resp.text or "")[:200]

    if resp.status_code == 202:
        print("已通知 GUI 触发一次领取")
        logger.info("GUI 触发成功: HTTP 202")
        return EXIT_NOTIFIED_GUI
    elif resp.status_code == 409:
        print("GUI 正在忙碌，已跳过本次触发")
        logger.warning("GUI 忙碌: HTTP 409, body=%s", body_preview)
        return EXIT_NOTIFIED_GUI
    elif resp.status_code == 403:
        # 403 通常由 GUI 端 _check_origin 拦截（CSRF/Origin 校验失败）
        print(f"GUI 响应异常: HTTP {resp.status_code}")
        logger.warning("GUI 触发被拒绝（可能被 CSRF/Origin 拦截）: HTTP 403, body=%s", body_preview)
        return EXIT_NOTIFIED_GUI
    else:
        print(f"GUI 响应异常: HTTP {resp.status_code}")
        logger.warning("GUI 响应异常: HTTP %s, body=%s", resp.status_code, body_preview)
        return EXIT_NOTIFIED_GUI


def _send_no_db_notification():
    """三管齐下提示无 db：控制台 + cli.log + Server酱。"""
    msg = "检测到尚未初始化数据库，请先启动一次程序（GUI 模式），完成账号数据迁移操作后再使用 CLI。"
    # 控制台
    print(msg)
    # cli.log（通过 logger）；若 _setup_logging 未就绪则用 stderr 兜底
    if _logging_ready:
        logger = logging.getLogger(__name__)
        logger.error(msg)
    else:
        sys.stderr.write(f"[ERROR] {msg}\n")
    # Server酱（未配置 sendkey 则跳过，发送失败不阻塞退出）
    try:
        settings = load_settings()
        sendkey = str(settings.get("schan_key") or "").strip()
        if sendkey:
            send_schan(sendkey, "etalien-auto 迁移提醒", msg)
        else:
            if _logging_ready:
                logger = logging.getLogger(__name__)
                logger.info("未配置 Server酱 sendkey，跳过推送")
            else:
                sys.stderr.write("[INFO] 未配置 Server酱 sendkey，跳过推送\n")
    except Exception as e:
        if _logging_ready:
            logger = logging.getLogger(__name__)
            logger.error("Server酱推送失败: %s", e)
        else:
            sys.stderr.write(f"[ERROR] Server酱推送失败: {e}\n")


def cli_entry(auto_close: bool = False):
    """CLI 入口封装：统一 Mutex → 通知 GUI 或独立运行 → 无 db 发 Server酱 → 调用 main()。

    本函数不捕获 main() 抛出的 SystemExit，让其自然传播至 gui/app.py 外层处理
    （保留退出码 0-5 的原 if not auto_close: input() 逻辑）。
    仅对退出码 6/7/8 自行处理 print + input 等待回车。
    非 SystemExit 的未捕获异常记录完整栈到 cli.log 后以 EXIT_ERROR 退出。
    """
    log_file = os.path.join(CONFIG_DIR, "cli.log")
    _setup_logging(log_file)
    logger = logging.getLogger(__name__)

    # 1. 检查统一 Named Mutex
    handle, already_exists = _create_mutex()
    if handle == 0:
        # Mutex 创建失败（罕见，如系统资源耗尽）
        err = ctypes.windll.kernel32.GetLastError()
        logger.error("创建 Mutex 失败，GetLastError=%s", err)
        print("程序启动失败：无法创建进程锁")
        _cli_exit(EXIT_ALREADY_RUNNING, auto_close)

    if already_exists:
        # 已有实例在运行，进入"通知 GUI"分支
        code = _notify_gui_trigger_claim()
        _cli_exit(code, auto_close)

    # 2. 独立运行分支：检查 db 是否存在且健康
    if not os.path.exists(DB_PATH) or not is_db_valid(DB_PATH):
        _send_no_db_notification()
        _cli_exit(EXIT_NO_DB, auto_close)

    # 3. db 健康，调用 main() 执行原 CLI 领取流程
    # SystemExit 自然传播（保留 0-8 退出码语义）；其他异常记录栈后以 EXIT_ERROR 退出
    try:
        main(auto_close=auto_close)
    except SystemExit:
        raise
    except Exception:
        logger.exception("CLI 主流程未捕获异常")
        _cli_exit(EXIT_ERROR, auto_close)


if __name__ == "__main__":
    cli_entry()
