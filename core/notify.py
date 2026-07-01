"""Server酱 通知模块（业务层）

仅在 CLI 自动领取模式（--auto-close）下触发，将领取结果推送到管理员微信。
依赖 core/config.py 的 settings.schan_enabled / schan_key 配置。

设计要点：
  - 每次自动运行只发 1 条消息（Server酱 Turbo 免费版每日限额 5 条）
  - 发送失败只记日志，不影响主流程退出码
  - 不修改 core/service.py，错误原因由 status 映射得到
"""
import logging
import time

import requests

from core.config import load_settings

logger = logging.getLogger(__name__)

# Server酱 Turbo API
SCHAN_API_BASE = "https://sctapi.ftqq.com"
TIMEOUT = 10  # 请求超时（秒）


def send_schan(sendkey: str, title: str, desp: str) -> bool:
    """发送一条 Server酱 消息。

    Args:
        sendkey: Server酱 SendKey
        title: 消息标题（最长 32 字符）
        desp:  消息正文（支持 Markdown）

    Returns:
        True 发送成功，False 发送失败
    """
    url = f"{SCHAN_API_BASE}/{sendkey}.send"
    payload = {"title": title, "desp": desp}
    try:
        resp = requests.post(url, data=payload, timeout=TIMEOUT)
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.error("Server酱请求异常: %s", e)
        return False

    code = data.get("code")
    if code == 0:
        logger.info("Server酱推送成功 pushid=%s",
                    (data.get("data") or {}).get("pushid", ""))
        return True

    logger.error("Server酱推送失败 code=%s message=%s",
                 code, data.get("message", ""))
    return False


def _build_problem_line(r: dict) -> str:
    """构造问题账号列表的一行描述（不含前导 -）。"""
    status = r["status"]
    label = r["label"]
    if status == "need_login":
        return f"{label}: 登录状态过期"
    if status == "network_error":
        return f"{label}: 网络/服务端错误，未能领取"
    if status == "error":
        return f"{label}: 程序运行时出现报错，详见日志"
    if status == "partial":
        claimed = r.get("claimed", 0)
        failed = r.get("failed", 0)
        total = claimed + failed
        return f"{label}: 部分完成 {claimed}/{total} 个"
    return f"{label}: {status}"


def send_claim_notification(results: list[dict]) -> bool:
    """根据领取结果构造并发送 Server酱 通知。

    Args:
        results: run_concurrent_claim 返回的结果列表

    Returns:
        True 发送成功（或 schan 未启用时返回 False 表示未发送）
    """
    settings = load_settings()
    if not settings.get("schan_enabled"):
        logger.info("Server酱未启用，跳过通知")
        return False
    sendkey = settings.get("schan_key", "").strip()
    if not sendkey:
        logger.warning("Server酱已启用但 schan_key 为空，跳过通知")
        return False

    # 统计
    total = len(results)
    success = sum(1 for r in results if r["status"] in ("done", "already_done"))
    need_login = sum(1 for r in results if r["status"] == "need_login")
    network_error = sum(1 for r in results if r["status"] == "network_error")
    error = sum(1 for r in results if r["status"] == "error")
    partial = sum(1 for r in results if r["status"] == "partial")
    problem_count = need_login + network_error + error + partial
    all_success = problem_count == 0

    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    if all_success:
        title = "etalien-auto全部领取成功"
        desp = (
            f"## 全部领取成功√\n\n"
            f"- 时间: {now_str}\n"
            f"- 账号数: {total}\n"
        )
    else:
        title = "etalien-auto运行结果通知"
        # 括号内只列问题类型，不重复列成功数（避免标题冗长）
        parts = []
        if partial:
            parts.append(f"部分完成 {partial}")
        if need_login:
            parts.append(f"登录过期 {need_login}")
        if network_error:
            parts.append(f"网络错误 {network_error}")
        if error:
            parts.append(f"错误 {error}")
        summary = "，".join(parts)

        lines = [
            f"## 运行结果通知\n\n",
            f"- 时间: {now_str}\n",
            f"- 账号数: {total}（成功 {success}，{summary}）\n\n",
            f"### 问题账号\n",
        ]
        for r in results:
            if r["status"] in ("need_login", "network_error", "error", "partial"):
                lines.append(f"- {_build_problem_line(r)}\n")
        desp = "".join(lines)

    # title 最长 32 字符，截断保护
    if len(title) > 32:
        title = title[:32]

    return send_schan(sendkey, title, desp)
