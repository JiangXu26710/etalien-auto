import logging
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from core.client import EtAlienClient
from core.config import load_accounts, save_accounts, load_settings, save_settings, validate_settings, accounts_lock, PROJECT_DIR

logger = logging.getLogger(__name__)

PHONE_PATTERN = re.compile(r'^1[3-9]\d{9}$')

SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

TASK_NAME = "EtAlienAuto_DailyClaim"

# PC加速权益常量
PC_AD_ID = "103334281"      # 穿山甲广告位ID（生产环境）
PC_BUSINESS = 1             # business类型: 1=PC加速, 2=手机加速, 3=翻译加速

# 手机加速权益常量
MOBILE_AD_ID = "102815305"  # 手机加速穿山甲广告位ID
MOBILE_BUSINESS = 2         # business=2 表示手机加速


def validate_phone(phone: str) -> bool:
    return bool(PHONE_PATTERN.match(phone))


def account_label(account: dict) -> str:
    parts = []
    if account.get("name"):
        parts.append(account["name"])
    parts.append(account["phone"])
    if account.get("remark"):
        parts.append(f"({account['remark']})")
    return " ".join(parts)


def _get_or_create_account(phone: str, accounts: list[dict]) -> dict:
    for a in accounts:
        if a["phone"] == phone:
            return a
    new_account = {
        "name": "",
        "phone": phone,
        "remark": "",
        "enabled": True,
    }
    accounts.append(new_account)
    return new_account


def ensure_device_id(phone: str) -> str:
    with accounts_lock:
        accounts = load_accounts()
        account = _get_or_create_account(phone, accounts)
        if account.get("device_id"):
            return account["device_id"]
        device_id = EtAlienClient.generate_device_id()
        account["device_id"] = device_id
        save_accounts(accounts)
        return device_id


def send_login_code(phone: str) -> dict:
    device_id = ensure_device_id(phone)
    client = EtAlienClient(phone=phone, device_id=device_id)
    return client.get_login_code()


def _save_login_result(phone: str, login_result: dict, device_id: str, password: str | None = None) -> None:
    with accounts_lock:
        accounts = load_accounts()
        for a in accounts:
            if a["phone"] == phone:
                a["auth_token"] = login_result.get("authorization")
                a["user_id"] = login_result.get("user_id")
                a["device_id"] = device_id
                a["saved_at"] = time.time()
                if password is not None:
                    a["password"] = password
                break
        save_accounts(accounts)


def verify_login(phone: str, code: str | None = None, password: str | None = None) -> dict:
    device_id = ensure_device_id(phone)
    client = EtAlienClient(phone=phone, device_id=device_id)
    if password:
        result = client.login_by_password(password)
    else:
        result = client.login_by_code(code)
    if not result.get("_error"):
        _save_login_result(phone, result, device_id, password=password if password else None)
    return result


def get_client_for_account(account: dict) -> EtAlienClient | None:
    phone = account["phone"]
    if not account.get("auth_token") or not account.get("device_id"):
        return None
    client = EtAlienClient(
        phone=phone,
        auth_token=account["auth_token"],
        device_id=account["device_id"]
    )
    client.user_id = account.get("user_id")
    return client


def get_account_status(account: dict) -> dict:
    phone = account["phone"]
    info = {
        "phone": phone,
        "name": account.get("name", ""),
        "remark": account.get("remark", ""),
        "enabled": account.get("enabled", True),
        "logged_in": False,
        "token_valid": False,
        "vip_duration": 0,
        "free_duration": 0,
        "progress": "0/0",
        # 手机端字段（合并查询，供总进度累加用）
        "mobile_duration": 0,
        "mobile_progress": "0/0",
        "mobile_rewarded_count": 0,
        "mobile_claimed_count": 0,
        "mobile_not_get_ad_duration": 0,
        "mobile_error": False,  # 手机端接口查询失败标记，前端据此显示"查询失败"而非误导性的 0/0
        # 暴露给前端，使 idle 总进度口径与领取中对齐（只统计 claim_target 配置的阶段）
        "claim_target": account.get("claim_target", "all"),
    }
    client = get_client_for_account(account)
    if client:
        info["logged_in"] = True
        try:
            # 4 个接口并发：PC duration + PC ad_config + mobile activity + mobile profile
            with ThreadPoolExecutor(max_workers=4) as ex:
                dur_future = ex.submit(client.fetch_pc_duration)
                tasks_future = ex.submit(client.fetch_pc_ad_config)
                mob_act_future = ex.submit(client.fetch_mobile_ad_activity)
                mob_prof_future = ex.submit(client.fetch_mobile_profile)
                dur = dur_future.result()
                tasks_result = tasks_future.result()
                mob_activity = mob_act_future.result()
                mob_profile = mob_prof_future.result()
        except Exception:
            logger.exception("get_account_status API call failed for %s", phone)
            return info
        token_expired = EtAlienClient._is_auth_error(dur)
        if token_expired:
            info["token_expired"] = True
        else:
            info["token_valid"] = True
            if not dur.get("_error"):
                info["vip_duration"] = dur.get("vip_duration_second", 0)
                info["free_duration"] = dur.get("free_duration_second", 0)
            if not tasks_result.get("_error"):
                tasks = tasks_result.get("list", [])
                total_w = sum(l["watch_cnt"] for l in tasks)
                total_t = sum(len(l["items"]) for l in tasks)
                info["progress"] = f"{total_w}/{total_t}"
                info["tasks"] = tasks
            # 手机端进度（与 PC 端共用同一 token，token 有效时直接查手机端接口）
            if not mob_activity.get("_error"):
                video_cnt = mob_activity.get("video_cnt", 0)
                user_watch_cnt = mob_activity.get("user_watch_cnt", 0)
                info["mobile_rewarded_count"] = mob_activity.get("rewarded_count", 0)
                info["mobile_claimed_count"] = mob_activity.get("claimed_count", 0)
                # 进度按总任务数统计（含无奖励任务），因领取需按顺序进行
                # clamp: user_watch_cnt 是累计 is_verify=true 的调用次数，可超过 video_cnt（实测即使无可发奖励或超额，is_verify 仍返回 true，计数持续递增）
                shown_cnt = min(user_watch_cnt, video_cnt) if video_cnt > 0 else user_watch_cnt
                info["mobile_progress"] = f"{shown_cnt}/{video_cnt}"
                info["mobile_tasks"] = mob_activity.get("video_bar", [])
            else:
                info["mobile_error"] = True
            if not mob_profile.get("_error"):
                info["mobile_duration"] = mob_profile.get("remaining_seconds", 0)
                info["mobile_not_get_ad_duration"] = mob_profile.get("mobile_not_get_ad_duration", 0)
            else:
                info["mobile_error"] = True
    return info


def get_account_mobile_status(account: dict) -> dict:
    """查询单个账号的手机端状态（时长余额 + 任务进度）。

    预留给前端卡片翻转 UI 按需调用。结构与 get_account_status 对称，
    仅查询手机端接口（fetch_mobile_ad_activity + fetch_mobile_profile）。
    """
    phone = account["phone"]
    info = {
        "phone": phone,
        "name": account.get("name", ""),
        "remark": account.get("remark", ""),
        "enabled": account.get("enabled", True),
        "logged_in": False,
        "token_valid": False,
        "mobile_duration": 0,
        "mobile_progress": "0/0",
        "mobile_rewarded_count": 0,
        "mobile_claimed_count": 0,
        "mobile_not_get_ad_duration": 0,
        "mobile_error": False,  # 手机端接口查询失败标记，前端据此显示"查询失败"而非误导性的 0/0
    }
    client = get_client_for_account(account)
    if not client:
        return info
    info["logged_in"] = True
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            act_future = ex.submit(client.fetch_mobile_ad_activity)
            prof_future = ex.submit(client.fetch_mobile_profile)
            activity = act_future.result()
            profile = prof_future.result()
    except Exception:
        logger.exception("get_account_mobile_status API call failed for %s", phone)
        return info

    if EtAlienClient._is_auth_error(activity) or EtAlienClient._is_auth_error(profile):
        info["token_expired"] = True
        return info
    info["token_valid"] = True

    if not activity.get("_error"):
        video_cnt = activity.get("video_cnt", 0)
        user_watch_cnt = activity.get("user_watch_cnt", 0)
        rewarded = activity.get("rewarded_count", 0)
        claimed = activity.get("claimed_count", 0)
        info["mobile_rewarded_count"] = rewarded
        info["mobile_claimed_count"] = claimed
        # 进度按总任务数统计（含无奖励任务），因领取需按顺序进行
        # clamp: user_watch_cnt 是累计 is_verify=true 的调用次数，可超过 video_cnt（实测即使无可发奖励或超额，is_verify 仍返回 true，计数持续递增）
        shown_cnt = min(user_watch_cnt, video_cnt) if video_cnt > 0 else user_watch_cnt
        info["mobile_progress"] = f"{shown_cnt}/{video_cnt}"
        info["mobile_tasks"] = activity.get("video_bar", [])
    else:
        info["mobile_error"] = True

    if not profile.get("_error"):
        info["mobile_duration"] = profile.get("remaining_seconds", 0)
        info["mobile_not_get_ad_duration"] = profile.get("mobile_not_get_ad_duration", 0)
    else:
        info["mobile_error"] = True

    return info


def get_all_status(max_workers: int = 10) -> list[dict]:
    accounts = load_accounts()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(get_account_status, a): a for a in accounts}
        results = []
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception:
                logger.exception("get_account_status failed for %s", futures[future]["phone"])
        results.sort(key=lambda r: r.get("phone", ""))
        return results


def _get_cli_exe_path() -> str:
    if getattr(sys, "frozen", False):
        exe_path = sys.executable
        return f'"{exe_path}" --cli --auto-close'
    return f'"{os.path.join(PROJECT_DIR, "main.py")}"'


def query_schedule() -> dict[str, Any]:
    # 优先从 schtasks /xml 解析真实 StartBoundary（任务时间）和 Settings/Enabled（任务启用状态）；
    # 任务不存在或解析异常时回退到配置文件 schedule_time。
    # 用 XML 格式而非 LIST /v：字段名语言无关，避开中文 Windows 本地化字段名问题。
    settings = load_settings()
    fallback_time = settings.get("schedule_time", "08:00")
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME, "/xml"],
            capture_output=True, timeout=10,
            creationflags=SUBPROCESS_FLAGS,
        )
        if result.returncode != 0:
            return {"exists": False, "enabled": False, "time": fallback_time}

        # schtasks /xml 声明为 UTF-16，实际输出 UTF-8（实测 Win11），用 utf-8 解码
        xml_text = result.stdout.decode("utf-8", errors="replace")
        root = ET.fromstring(xml_text)
        ns = {"ms": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

        schedule_time = fallback_time
        sb = root.find(".//ms:Triggers//ms:StartBoundary", ns)
        if sb is not None and "T" in sb.text:
            # 格式 2026-07-02T21:00:00，截取时间部分 HH:MM
            time_part = sb.text.split("T", 1)[1]
            if len(time_part) >= 5 and time_part[2] == ":":
                schedule_time = time_part[:5]

        # 启用状态：Settings/Enabled 缺省视为 True（任务默认启用），
        # 显式 "false" 才视为禁用；同时检查 Triggers/Enabled 任一为 false 即视为禁用
        enabled = True
        settings_enabled = root.find("./ms:Settings/ms:Enabled", ns)
        if settings_enabled is not None and settings_enabled.text.lower() == "false":
            enabled = False
        for trig_en in root.findall(".//ms:Triggers//ms:Enabled", ns):
            if trig_en.text.lower() == "false":
                enabled = False
                break

        return {"exists": True, "enabled": enabled, "time": schedule_time}
    except Exception as e:
        logger.exception("Failed to query schedule")
        return {"exists": False, "enabled": False, "time": fallback_time, "error": str(e)}


def create_schedule(schedule_time: str) -> dict[str, Any]:
    _, err, err_cat = validate_settings({"schedule_time": schedule_time})
    if err:
        return {"error": err, "error_category": err_cat or "system"}

    cli_cmd = _get_cli_exe_path()
    if getattr(sys, "frozen", False):
        task_cmd = cli_cmd
    else:
        task_cmd = f'"{sys.executable}" {cli_cmd}'

    try:
        result = subprocess.run(
            ["schtasks", "/create", "/tn", TASK_NAME, "/tr", task_cmd,
             "/sc", "daily", "/st", schedule_time, "/f"],
            capture_output=True, text=True, timeout=15,
            creationflags=SUBPROCESS_FLAGS,
        )
        if result.returncode == 0:
            settings = load_settings()
            settings["schedule_time"] = schedule_time
            save_settings(settings)
            return {"ok": True, "msg": f"计划任务已创建，每天 {schedule_time} 执行"}
        else:
            logger.warning("schtasks create failed: %s", result.stderr.strip())
            return {"error": f"创建失败: {result.stderr.strip()}"}
    except Exception as e:
        logger.exception("Failed to create schedule")
        return {"error": str(e)}


def delete_schedule() -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            capture_output=True, text=True, timeout=10,
            creationflags=SUBPROCESS_FLAGS,
        )
        if result.returncode == 0:
            return {"ok": True, "msg": "计划任务已删除"}
        else:
            return {"ok": True, "msg": "计划任务不存在或已删除"}
    except Exception as e:
        logger.exception("Failed to delete schedule")
        return {"error": str(e)}


def format_duration(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def get_unwatched_count(tasks: list) -> int:
    return sum(
        len([item for item in level["items"] if not item["is_watch"]])
        for level in tasks
    )


def _is_password_incorrect(result: dict) -> bool:
    """判断服务端返回是否表示密码错误（需清除保存的密码）

    注意：实测"密码格式错误"是输入格式不合规（用户输错），不是保存的密码本身错误，
    不应触发清除密码，故不放入关键词列表。
    """
    if not result.get("_error"):
        return False
    msg = result.get("msg", "")
    # 服务端返回的密码相关错误关键词（"密码格式错误"已排除，见 docstring）
    return any(kw in msg for kw in ["密码错误", "密码不正确", "password incorrect"])


def _clear_account_password(phone: str) -> None:
    """清除账号保存的密码（密码错误时调用，避免反复尝试触发风控）"""
    with accounts_lock:
        accounts = load_accounts()
        for a in accounts:
            if a["phone"] == phone:
                a.pop("password", None)
                break
        save_accounts(accounts)


def init_client(account: dict) -> tuple[EtAlienClient | None, str]:
    """初始化客户端，校验 token 有效性。

    Returns:
        (client, status): status 为 "ok" / "need_login" / "network_error"
        - "ok": client 已就绪
        - "need_login": token 过期且无法密码重登（或无密码、或未登录）
        - "network_error": token 状态未知（网络/服务端错误，非认证失败）
    """
    phone = account["phone"]
    client = get_client_for_account(account)

    if client:
        token_valid = client.check_token_valid()
        if token_valid is True:
            logger.info("  [%s] Token有效，跳过登录", phone)
            return client, "ok"
        elif token_valid is False:
            logger.info("  [%s] Token已过期", phone)
            password = account.get("password", "")
            if password:
                logger.info("  [%s] 检测到已配置密码,尝试密码登录", phone)
                login_result = client.login_by_password(password)
                if not login_result.get("_error"):
                    _save_login_result(phone, login_result, account.get("device_id", ""))
                    logger.info("  [%s] 密码登录成功,已更新token", phone)
                    return client, "ok"
                else:
                    logger.warning("  [%s] 密码登录失败: %s", phone, login_result.get("msg"))
                    if _is_password_incorrect(login_result):
                        _clear_account_password(phone)
                        logger.info("  [%s] 密码错误,已清除保存的密码", phone)
            return None, "need_login"
        else:
            logger.info("  [%s] Token状态未知（网络/服务端错误），跳过", phone)
            return None, "network_error"
    else:
        logger.info("  [%s] 未找到登录状态", phone)
        return None, "need_login"


def _make_claim_result(phone: str, label: str, status: str,
                       vip_before: int = 0, vip_after: int = 0,
                       claimed: int = 0, failed: int = 0,
                       progress_entry: dict | None = None,
                       **progress_extra) -> dict:
    result = {
        "phone": phone,
        "label": label,
        "status": status,
        "vip_before": vip_before,
        "vip_after": vip_after,
        "claimed": claimed,
        "failed": failed,
    }
    if progress_entry is not None:
        progress_entry.update({"status": status, "vip_before": vip_before, "vip_after": vip_after, **progress_extra})
    return result


def _try_relogin(client: EtAlienClient, account: dict) -> bool:
    """领取过程中检测到认证错误时，尝试用保存的密码重新登录。

    成功返回 True（client 的 auth_token 已更新），失败或无密码返回 False。
    密码错误时清除保存的密码，避免反复尝试触发风控。
    网络异常时返回 False，让 consecutive_fail 正常计数。
    """
    phone = account["phone"]
    password = account.get("password", "")
    if not password:
        logger.warning("  [%s] 领取中token过期,但未配置密码,无法重登", phone)
        return False
    logger.info("  [%s] 领取中token过期,尝试密码重登", phone)
    try:
        login_result = client.login_by_password(password)
    except requests.RequestException as e:
        logger.warning("  [%s] 密码重登网络异常: %s", phone, e)
        return False
    if login_result.get("_error"):
        logger.warning("  [%s] 密码重登失败: %s", phone, login_result.get("msg"))
        if _is_password_incorrect(login_result):
            _clear_account_password(phone)
            logger.info("  [%s] 密码错误,已清除保存的密码", phone)
        return False
    _save_login_result(phone, login_result, account.get("device_id", ""))
    logger.info("  [%s] 密码重登成功,已更新token", phone)
    return True


def _claim_pc_phase(client: EtAlienClient, phone: str,
                    interval: float, max_rounds: int,
                    unwatched_before: int,
                    progress_entry: dict | None = None,
                    account: dict | None = None) -> tuple[int, int]:
    """执行PC加速权益的单阶段领取循环。

    优化：每轮只发补发请求，开始/结束各查1次config校准，请求量降约2/3。
    退出条件：local_success>=unwatched_before（领够）/ consecutive_fail>=3（失效）/ round>=max_rounds。
    真实领取数由结束查询的 unwatched 差值确定，比 is_verify 计数更可信。
    领取中 token 过期时，若 account 提供了 password，会尝试一次密码重登后继续。
    """
    local_success = 0
    total_failed = 0
    consecutive_fail = 0
    round_num = 0
    relogin_attempted = False  # 领取中仅尝试一次密码重登，避免循环重登

    if progress_entry is not None:
        logger.info("  [%s] PC阶段开始: unwatched=%d, entry.current=%d, entry.total=%d",
                    phone, unwatched_before,
                    progress_entry.get("current", 0), progress_entry.get("total", 0))

    while round_num < max_rounds:
        if local_success >= unwatched_before:
            break
        round_num += 1
        backup_result = client.pc_ad_callback_backup(ad_id=PC_AD_ID, business=PC_BUSINESS)
        if backup_result.get("_error"):
            logger.info("  [%s] PC第%d轮 error: %s", phone, round_num, backup_result.get("msg", ""))
            # 检测认证错误（401/403/16），尝试密码重登（仅一次）
            if (account is not None and not relogin_attempted
                    and EtAlienClient._is_auth_error(backup_result)):
                relogin_attempted = True
                if _try_relogin(client, account):
                    consecutive_fail = 0
                    continue  # 重登成功，重试下一轮（本轮已消耗）
            total_failed += 1
            consecutive_fail += 1
        elif backup_result.get("is_verify"):
            local_success += 1
            consecutive_fail = 0
            if progress_entry is not None:
                cur = progress_entry.get("current", 0) + 1
                tot = progress_entry.get("total", 0)
                progress_entry["current"] = min(cur, tot) if tot > 0 else cur
                logger.info("  [%s] PC第%d轮 成功: local=%d, entry.current=%d, total=%d",
                            phone, round_num, local_success, progress_entry["current"], tot)
        else:
            logger.info("  [%s] PC第%d轮 is_verify=False", phone, round_num)
            total_failed += 1
            consecutive_fail += 1

        if consecutive_fail >= 3:
            logger.info("  [%s] 连续%d次补发失败，停止", phone, consecutive_fail)
            break

        time.sleep(interval)

    # 结束查1次config校准真实领取数
    final_result = client.fetch_pc_ad_config()
    if final_result.get("_error"):
        logger.warning("  [%s] 结束校准查询失败，使用本地计数 %d", phone, local_success)
        return local_success, total_failed
    final_unwatched = get_unwatched_count(final_result.get("list", []))
    real_claimed = unwatched_before - final_unwatched
    if real_claimed < 0:
        real_claimed = 0
    if final_unwatched > 0 and local_success >= unwatched_before:
        logger.warning("  [%s] 本地计数%d已领够但服务端仍有%d未完成，可能存在异常",
                       phone, local_success, final_unwatched)
    return real_claimed, total_failed


def _calculate_final_status(total_claimed: int, total_failed: int) -> str:
    if total_failed == 0:
        return "done"
    if total_claimed > 0:
        return "partial"
    return "error"


def _combine_phase_status(phase_results: list[str]) -> str:
    """综合多阶段领取结果为整体状态。

    输入为各阶段的 status 列表（"done"/"partial"/"error"/"already_done"），
    返回整体 status。规则：
    - 空 -> error（claim_target 异常时兜底，正常不会走到）
    - 全部 already_done -> already_done（无需领取）
    - 任一 partial，或 error 与成功类(done/partial/already_done)共存 -> partial（部分完成）
    - 仅 error 无成功类 -> error（全部失败）
    - 否则（全 done，或 done + already_done）-> done

    注意：[done, error] / [already_done, error] 等混合情况应判为 partial，
    不能因为存在成功状态就忽略 error（历史 bug：曾误判为 done）。
    """
    has_error = "error" in phase_results
    has_success = any(s in ("done", "partial", "already_done") for s in phase_results)
    if not phase_results:
        return "error"
    if all(s == "already_done" for s in phase_results):
        return "already_done"
    if "partial" in phase_results or (has_error and has_success):
        return "partial"
    if has_error:
        return "error"
    return "done"


def _claim_mobile_phase(client: EtAlienClient, phone: str,
                        interval: float, max_rounds: int,
                        pending_count: int, watched_before: int,
                        progress_entry: dict | None = None,
                        account: dict | None = None) -> tuple[int, int]:
    """执行手机加速权益的领取循环。

    优化：每轮只发补发请求，开始/结束各查1次activity校准，请求量降约2/3。
    退出条件：local_success>=pending_count（领够）/ consecutive_fail>=3（失效）/ round>=max_rounds。
    真实领取数由结束查询的 user_watch_cnt 差值确定。

    注意：手机端任务需按顺序领取，无奖励任务也必须领取才能解锁后续有奖励任务，
    因此 pending_count 基于总任务数 video_cnt 计算，而非 rewarded_count。
    领取中 token 过期时，若 account 提供了 password，会尝试一次密码重登后继续。
    """
    local_success = 0
    total_failed = 0
    consecutive_fail = 0
    round_num = 0
    relogin_attempted = False  # 领取中仅尝试一次密码重登，避免循环重登

    if progress_entry is not None:
        logger.info("  [%s] 手机阶段开始: pending=%d, entry.current=%d, entry.total=%d",
                    phone, pending_count,
                    progress_entry.get("current", 0), progress_entry.get("total", 0))

    while round_num < max_rounds:
        if local_success >= pending_count:
            break
        round_num += 1
        backup_result = client.pc_ad_callback_backup(ad_id=MOBILE_AD_ID, business=MOBILE_BUSINESS)
        if backup_result.get("_error"):
            logger.info("  [%s] 手机第%d轮 error: %s", phone, round_num, backup_result.get("msg", ""))
            # 检测认证错误（401/403/16），尝试密码重登（仅一次）
            if (account is not None and not relogin_attempted
                    and EtAlienClient._is_auth_error(backup_result)):
                relogin_attempted = True
                if _try_relogin(client, account):
                    consecutive_fail = 0
                    continue  # 重登成功，重试下一轮（本轮已消耗）
            total_failed += 1
            consecutive_fail += 1
        elif backup_result.get("is_verify"):
            local_success += 1
            consecutive_fail = 0
            if progress_entry is not None:
                cur = progress_entry.get("current", 0) + 1
                tot = progress_entry.get("total", 0)
                progress_entry["current"] = min(cur, tot) if tot > 0 else cur
                logger.info("  [%s] 手机第%d轮 成功: local=%d, entry.current=%d, total=%d",
                            phone, round_num, local_success, progress_entry["current"], tot)
        else:
            logger.info("  [%s] 手机第%d轮 is_verify=False", phone, round_num)
            total_failed += 1
            consecutive_fail += 1

        if consecutive_fail >= 3:
            logger.info("  [%s] 手机端连续%d次补发失败，停止", phone, consecutive_fail)
            break

        time.sleep(interval)

    # 结束查1次activity校准真实领取数（用 user_watch_cnt 差值，因含无奖励任务）
    final_activity = client.fetch_mobile_ad_activity()
    if final_activity.get("_error"):
        logger.warning("  [%s] 手机端结束校准查询失败，使用本地计数 %d", phone, local_success)
        return local_success, total_failed
    final_watch = final_activity.get("user_watch_cnt", watched_before)
    real_claimed = final_watch - watched_before
    if real_claimed < 0:
        real_claimed = 0
    target = watched_before + pending_count
    if final_watch < target and local_success >= pending_count:
        logger.warning("  [%s] 手机端本地计数%d已领够但服务端仅%d/%d，可能存在异常",
                       phone, local_success, final_watch, target)
    return real_claimed, total_failed


def claim_for_account(account: dict, settings: dict, progress_entry: dict | None = None) -> dict:
    phone = account["phone"]
    label = account_label(account)
    interval = settings.get("request_interval", 2.0)
    max_rounds = settings.get("max_rounds", 21)
    mob_max_rounds = settings.get("mobile_max_rounds", 7)
    # claim_target: "all" / "pc" / "mobile"，默认 "all"（旧账号兼容）
    claim_target = account.get("claim_target", "all")

    client, init_status = init_client(account)

    if not client or not client.is_logged_in():
        # init_status 为 "need_login" 或 "network_error"，区分 token 过期与网络错误
        return _make_claim_result(phone, label, init_status, progress_entry=progress_entry)

    # 累计两阶段的统计
    total_claimed = 0
    total_failed = 0
    vip_before = 0
    vip_after = 0
    mobile_before = 0
    mobile_after = 0
    phase_results: list[str] = []  # 每个阶段的 status

    # ============ PC 阶段 ============
    if claim_target in ("pc", "all"):
        if progress_entry is not None:
            progress_entry["phase"] = "pc"
            progress_entry["current"] = 0
            progress_entry["total"] = 0

        ad_result = client.fetch_pc_ad_config()
        if ad_result.get("_error"):
            logger.warning("  [%s] 获取PC任务失败: %s", phone, ad_result.get('msg'))
            phase_results.append("error")
        else:
            tasks = ad_result.get("list", [])
            total_unwatched = get_unwatched_count(tasks)
            total_items = sum(len(level["items"]) for level in tasks)

            # 记录 PC 端初始进度到 progress_entry（后端统计总进度用）
            if progress_entry is not None:
                progress_entry["pc_initial"] = total_items - total_unwatched
                progress_entry["pc_total"] = total_items

            dur_before = client.fetch_pc_duration()
            vip_before = dur_before.get("vip_duration_second", 0) if not dur_before.get("_error") else 0

            if total_unwatched == 0:
                logger.info("  [%s] PC所有任务已完成, VIP: %s", phone, format_duration(vip_before))
                vip_after = vip_before
                if progress_entry is not None:
                    progress_entry["current"] = 0
                    progress_entry["total"] = total_items
                phase_results.append("already_done")
            else:
                logger.info("  [%s] PC开始领取, 待完成: %s, VIP: %s",
                            phone, total_unwatched, format_duration(vip_before))
                if progress_entry is not None:
                    progress_entry["vip_before"] = vip_before
                    if progress_entry.get("total", 0) == 0 and total_items > 0:
                        progress_entry["total"] = total_items

                pc_claimed, pc_failed = _claim_pc_phase(client, phone, interval, max_rounds, total_unwatched, progress_entry, account)
                total_claimed += pc_claimed
                total_failed += pc_failed

                dur_after = client.fetch_pc_duration()
                vip_after = dur_after.get("vip_duration_second", 0) if not dur_after.get("_error") else vip_before
                logger.info("  [%s] PC领取完成, VIP: %s -> %s (+%s)",
                            phone, format_duration(vip_before), format_duration(vip_after),
                            format_duration(vip_after - vip_before))
                phase_results.append(_calculate_final_status(pc_claimed, pc_failed))

    # ============ Mobile 阶段 ============
    if claim_target in ("mobile", "all"):
        if progress_entry is not None:
            # 保存 PC 阶段的领取数（进入手机阶段后 current 会被重置）
            progress_entry["pc_claimed"] = progress_entry.get("current", 0)
            progress_entry["phase"] = "mobile"
            progress_entry["current"] = 0
            progress_entry["total"] = 0

        activity = client.fetch_mobile_ad_activity()
        if activity.get("_error"):
            logger.warning("  [%s] 获取手机端任务失败: %s", phone, activity.get('msg'))
            phase_results.append("error")
        else:
            # 手机端任务需按顺序领取，无奖励任务也必须领取才能解锁后续有奖励任务
            # 因此 pending 基于总任务数 video_cnt 和已观看数 user_watch_cnt 计算
            video_cnt = activity.get("video_cnt", 0)
            watched_before = activity.get("user_watch_cnt", 0)

            # 记录手机端初始进度到 progress_entry（后端统计总进度用）
            # clamp: user_watch_cnt 是累计 is_verify=true 的调用次数，可超过 video_cnt（实测即使无可发奖励或超额，is_verify 仍返回 true，计数持续递增）
            # 与 get_account_status/get_account_mobile_status 的 shown_cnt 口径一致，避免总进度超过 100%
            if progress_entry is not None:
                progress_entry["mobile_initial"] = min(watched_before, video_cnt) if video_cnt > 0 else watched_before
                progress_entry["mobile_total"] = video_cnt

            profile_before = client.fetch_mobile_profile()
            mobile_before = profile_before.get("remaining_seconds", 0) if not profile_before.get("_error") else 0

            if video_cnt == 0 or watched_before >= video_cnt:
                logger.info("  [%s] 手机端所有任务已完成, 加速时长: %s",
                            phone, format_duration(mobile_before))
                mobile_after = mobile_before
                if progress_entry is not None:
                    progress_entry["current"] = 0
                    progress_entry["total"] = video_cnt
                phase_results.append("already_done")
            else:
                logger.info("  [%s] 手机端开始领取, 已观看: %s/%s, 加速时长: %s",
                            phone, watched_before, video_cnt, format_duration(mobile_before))
                if progress_entry is not None:
                    progress_entry["vip_before"] = mobile_before
                    if progress_entry.get("total", 0) == 0 and video_cnt > 0:
                        progress_entry["total"] = video_cnt

                pending = video_cnt - watched_before
                mob_claimed, mob_failed = _claim_mobile_phase(client, phone, interval, mob_max_rounds, pending, watched_before, progress_entry, account)
                total_claimed += mob_claimed
                total_failed += mob_failed

                profile_after = client.fetch_mobile_profile()
                mobile_after = profile_after.get("remaining_seconds", mobile_before) if not profile_after.get("_error") else mobile_before
                logger.info("  [%s] 手机端领取完成, 加速时长: %s -> %s (+%s)",
                            phone, format_duration(mobile_before), format_duration(mobile_after),
                            format_duration(mobile_after - mobile_before))
                phase_results.append(_calculate_final_status(mob_claimed, mob_failed))

    # ============ 汇总 ============
    # 整体状态综合判定（规则见 _combine_phase_status）
    final_status = _combine_phase_status(phase_results)

    # 若 claim_target=all，vip_after 取 PC 端的，mobile_after 取 Mobile 端的
    # 若只领一端，另一端为 0（不影响显示）
    if claim_target == "pc":
        mobile_before = mobile_after = 0
    elif claim_target == "mobile":
        vip_before = vip_after = 0

    result = {
        "phone": phone,
        "label": label,
        "status": final_status,
        "claim_target": claim_target,
        "vip_before": vip_before,
        "vip_after": vip_after,
        "mobile_before": mobile_before,
        "mobile_after": mobile_after,
        "claimed": total_claimed,
        "failed": total_failed,
        "phases": phase_results,
    }
    if progress_entry is not None:
        progress_entry.update({
            "status": final_status,
            "vip_before": vip_before,
            "vip_after": vip_after,
            "mobile_before": mobile_before,
            "mobile_after": mobile_after,
        })
    return result


def run_concurrent_claim(accounts: list[dict], settings: dict, progress_list: list[dict] | None = None, claim_mgr: Any = None) -> list[dict]:
    max_concurrent = settings.get("max_concurrent", 10)
    results = []

    with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
        futures = {}
        for account in accounts:
            entry = None
            if claim_mgr is not None:
                entry = {
                    "phone": account["phone"],
                    "status": "running",
                    "current": 0,
                    "total": 0,
                    "vip_before": 0,
                    "vip_after": 0,
                }
                claim_mgr.add_progress_entry(entry)
            elif progress_list is not None:
                entry = {
                    "phone": account["phone"],
                    "status": "running",
                    "current": 0,
                    "total": 0,
                    "vip_before": 0,
                    "vip_after": 0,
                }
                progress_list.append(entry)

            futures[executor.submit(claim_for_account, account, settings, entry)] = account

        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                account = futures[future]
                logger.exception("Claim failed for %s", account['phone'])
                logger.error("  [%s] 异常: %s", account['phone'], e)
                error_result = {
                    "phone": account["phone"],
                    "label": account_label(account),
                    "status": "error",
                    "vip_before": 0,
                    "vip_after": 0,
                    "claimed": 0,
                    "failed": 0,
                }
                results.append(error_result)
                if claim_mgr is not None:
                    claim_mgr.update_progress_entry(account["phone"], {"status": "error", "error": str(e)})
                elif progress_list is not None:
                    for entry in progress_list:
                        if entry["phone"] == account["phone"]:
                            entry.update({"status": "error", "error": str(e)})
                            break

    # 按 phone 排序，保证 CLI 输出与通知顺序稳定（与 get_all_status 一致）
    results.sort(key=lambda r: r.get("phone", ""))
    return results
