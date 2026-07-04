"""CLI 入口：并发执行账号领取任务并输出结果。"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from core.config import load_accounts, reload_settings, CONFIG_DIR
from core.notify import send_claim_notification
from core.service import run_concurrent_claim, format_duration

# CLI 退出码（与 docs/06-cli-and-schedule.md 退出码表一致）
EXIT_OK = 0              # 全部成功
EXIT_PARTIAL = 1         # 部分成功
EXIT_ERROR = 2           # 全部失败
EXIT_NEED_LOGIN = 3      # 有账号需要登录
EXIT_NO_ACCOUNTS = 4     # 无启用账号
EXIT_NETWORK_ERROR = 5   # 网络/服务端错误（有账号因网络或服务端错误未能领取）


def _setup_logging(log_file: str | None = None):
    """配置根日志记录器。

    Args:
        log_file: 可选日志文件路径。若提供，则同时输出到控制台和按大小轮转的文件。
    """
    handlers = [logging.StreamHandler(sys.stderr)]
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
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


def main(auto_close: bool = False):
    """CLI 主流程：加载配置、并发领取、汇总结果并退出。

    Args:
        auto_close: 是否在计划任务模式下发送通知并自动退出。

    无返回值——通过 sys.exit 退出，退出码遵循模块级常量定义。
    """
    log_file = os.path.join(CONFIG_DIR, "cli.log")
    _setup_logging(log_file)
    logger = logging.getLogger(__name__)
    logger.info("日志文件: %s", log_file)

    settings = reload_settings()
    accounts = load_accounts()
    enabled = [a for a in accounts if a.get("enabled", True)]

    if not enabled:
        logger.error("没有启用的账号，请在 config/accounts.json 中添加")
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
        if isinstance(vip_b, int) and isinstance(vip_a, int):
            diff = vip_a - vip_b
            logger.info("%s: %s  VIP: %s -> %s (+%s)",
                        r["label"], status_str,
                        format_duration(vip_b), format_duration(vip_a), format_duration(diff))
        else:
            logger.info("%s: %s", r["label"], status_str)

    logger.info("总VIP时长: %s -> %s (+%s)",
                format_duration(total_vip_before), format_duration(total_vip_after),
                format_duration(total_vip_after - total_vip_before))

    if need_login:
        logger.warning("以下账号需要重新登录（请在GUI中操作）:")
        for r in need_login:
            logger.warning("  - %s", r["label"])

    if network_errors:
        logger.warning("以下账号因网络/服务端错误未能领取（下次运行可能自动恢复）:")
        for r in network_errors:
            logger.warning("  - %s", r["label"])

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


if __name__ == "__main__":
    main()
