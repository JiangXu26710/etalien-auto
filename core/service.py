import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from core.client import EtAlienClient
from core.config import load_accounts, save_accounts, load_settings, save_settings, validate_settings, accounts_lock, PROJECT_DIR

logger = logging.getLogger(__name__)

PHONE_PATTERN = re.compile(r'^1[3-9]\d{9}$')

SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

TASK_NAME = "EtAlienAuto_DailyClaim"


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


def _save_login_result(phone: str, login_result: dict, device_id: str) -> None:
    with accounts_lock:
        accounts = load_accounts()
        for a in accounts:
            if a["phone"] == phone:
                a["auth_token"] = login_result.get("authorization")
                a["user_id"] = login_result.get("user_id")
                a["device_id"] = device_id
                a["saved_at"] = time.time()
                break
        save_accounts(accounts)


def verify_login(phone: str, code: str) -> dict:
    device_id = ensure_device_id(phone)
    client = EtAlienClient(phone=phone, device_id=device_id)
    result = client.login_by_code(code)
    if not result.get("_error"):
        _save_login_result(phone, result, device_id)
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
    }
    client = get_client_for_account(account)
    if client:
        info["logged_in"] = True
        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                dur_future = ex.submit(client.fetch_pc_duration)
                tasks_future = ex.submit(client.fetch_pc_ad_config)
                dur = dur_future.result()
                tasks_result = tasks_future.result()
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
    settings = load_settings()
    schedule_time = settings.get("schedule_time", "08:00")
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME, "/fo", "TABLE", "/nh"],
            capture_output=True, text=True, timeout=10,
            creationflags=SUBPROCESS_FLAGS,
        )
        exists = result.returncode == 0
        return {
            "exists": exists,
            "enabled": exists,
            "time": schedule_time,
        }
    except Exception as e:
        logger.exception("Failed to query schedule")
        return {"exists": False, "enabled": False, "time": schedule_time, "error": str(e)}


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


def init_client(account: dict) -> EtAlienClient | None:
    phone = account["phone"]
    client = get_client_for_account(account)

    if client:
        token_valid = client.check_token_valid()
        if token_valid is True:
            logger.info("  [%s] Token有效，跳过登录", phone)
            return client
        elif token_valid is False:
            logger.info("  [%s] Token已过期", phone)
            return None
        else:
            logger.info("  [%s] Token状态未知（网络/服务端错误），跳过", phone)
            return None
    else:
        logger.info("  [%s] 未找到登录状态", phone)
        return None


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


def _claim_business_phase(client: EtAlienClient, phone: str, business: int,
                          interval: float, max_rounds: int,
                          progress_entry: dict | None = None) -> tuple[int, int]:
    total_claimed = 0
    total_failed = 0
    consecutive_no_progress = 0
    round_num = 0

    while round_num < max_rounds:
        result = client.fetch_pc_ad_config()
        if result.get("_error"):
            logger.warning("  [%s] 获取任务状态失败", phone)
            break

        tasks = result.get("list", [])
        unwatched_before = get_unwatched_count(tasks)

        if progress_entry is not None:
            total_items = sum(len(level["items"]) for level in tasks)
            if progress_entry.get("total", 0) == 0 and total_items > 0:
                progress_entry["total"] = total_items

        if unwatched_before == 0:
            break

        round_num += 1
        backup_result = client.pc_ad_callback_backup(ad_id="103334281", business=business)
        if backup_result.get("_error"):
            total_failed += 1
        elif backup_result.get("is_verify"):
            total_claimed += 1
            if progress_entry is not None:
                cur = progress_entry.get("current", 0) + 1
                tot = progress_entry.get("total", 0)
                progress_entry["current"] = min(cur, tot) if tot > 0 else cur
        else:
            total_failed += 1
        time.sleep(interval)

        result = client.fetch_pc_ad_config()
        if result.get("_error"):
            break
        tasks = result.get("list", [])
        unwatched_after = get_unwatched_count(tasks)

        if unwatched_after == unwatched_before:
            consecutive_no_progress += 1
        else:
            consecutive_no_progress = 0

        if consecutive_no_progress >= 3:
            logger.info("  [%s] 连续%d轮无进展，停止", phone, consecutive_no_progress)
            break

    return total_claimed, total_failed


def _calculate_final_status(total_claimed: int, total_failed: int) -> str:
    if total_failed == 0:
        return "done"
    if total_claimed > 0:
        return "partial"
    return "error"


def claim_for_account(account: dict, settings: dict, progress_entry: dict | None = None) -> dict:
    phone = account["phone"]
    label = account_label(account)
    interval = settings.get("request_interval", 2.0)
    max_rounds = settings.get("max_rounds", 50)

    client = init_client(account)

    if not client or not client.is_logged_in():
        return _make_claim_result(phone, label, "need_login", progress_entry=progress_entry)

    ad_result = client.fetch_pc_ad_config()
    if ad_result.get("_error"):
        logger.warning("  [%s] 获取任务失败: %s", phone, ad_result.get('msg'))
        return _make_claim_result(phone, label, "error", progress_entry=progress_entry)

    tasks = ad_result.get("list", [])
    total_unwatched = get_unwatched_count(tasks)

    if total_unwatched == 0:
        dur = client.fetch_pc_duration()
        vip = dur.get("vip_duration_second", 0) if not dur.get("_error") else 0
        logger.info("  [%s] 所有任务已完成, VIP: %s", phone, format_duration(vip))
        total_items = sum(len(level["items"]) for level in tasks)
        return _make_claim_result(phone, label, "already_done",
                                 vip_before=vip, vip_after=vip,
                                 progress_entry=progress_entry,
                                 current=0, total=total_items)

    dur_before = client.fetch_pc_duration()
    vip_before = dur_before.get("vip_duration_second", 0) if not dur_before.get("_error") else 0

    logger.info("  [%s] 开始领取, 待完成: %s, VIP: %s", phone, total_unwatched, format_duration(vip_before))

    if progress_entry is not None:
        progress_entry["vip_before"] = vip_before

    total_claimed = 0
    total_failed = 0

    for business in [1, 2, 3]:
        claimed, failed = _claim_business_phase(client, phone, business, interval, max_rounds, progress_entry)
        total_claimed += claimed
        total_failed += failed

    dur_after = client.fetch_pc_duration()
    vip_after = dur_after.get("vip_duration_second", 0) if not dur_after.get("_error") else 0

    logger.info("  [%s] 领取完成, VIP: %s -> %s (+%s)", phone, format_duration(vip_before), format_duration(vip_after), format_duration(vip_after - vip_before))

    final_status = _calculate_final_status(total_claimed, total_failed)
    return _make_claim_result(phone, label, final_status,
                             vip_before=vip_before, vip_after=vip_after,
                             claimed=total_claimed, failed=total_failed,
                             progress_entry=progress_entry)


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

    return results
