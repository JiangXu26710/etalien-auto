"""
业务逻辑模块。

统一业务逻辑层，GUI 和 CLI 共用同一套领取/登录/状态查询/计划任务管理逻辑。
PC 加速和手机加速权益的领取流程在此实现，token 过期等异常由 EtAlienClient 处理。
"""

import logging
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import requests

from core.client import EtAlienClient, NeedLoginError
from core.config import load_settings, save_settings, validate_settings, PROJECT_DIR
from core.db import DbAccountRepository

logger = logging.getLogger(__name__)

# 模块级 Repository 单例（连接延迟到首次方法调用时建立，随进程退出由 OS 回收）
repo = DbAccountRepository()

# 中国大陆 11 位手机号正则
PHONE_PATTERN = re.compile(r'^1[3-9]\d{9}$')

# 子进程标志：Windows 下隐藏控制台窗口
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Windows 计划任务名称
TASK_NAME = "EtAlienAuto_DailyClaim"

# PC加速权益常量
PC_AD_ID = "103334281"      # 穿山甲广告位ID（生产环境）
PC_BUSINESS = 1             # business类型: 1=PC加速, 2=手机加速, 3=翻译功能（项目不涉及）

# 手机加速权益常量
MOBILE_AD_ID = "102815305"  # 手机加速穿山甲广告位ID
MOBILE_BUSINESS = 2         # business=2 表示手机加速


def validate_phone(phone: str) -> bool:
    """校验手机号是否符合中国大陆 11 位号段规则。"""
    return bool(PHONE_PATTERN.match(phone))


def account_label(account: dict) -> str:
    """生成账号展示标签，格式为「姓名 手机号 (备注)」，缺失字段自动省略。"""
    parts = []
    if account.get("name"):
        parts.append(account["name"])
    parts.append(account["phone"])
    if account.get("remark"):
        parts.append(f"({account['remark']})")
    return " ".join(parts)


def ensure_device_id(phone: str) -> str:
    """获取或生成 device_id，并持久化到账号数据中。"""
    account = repo.get_or_create(phone)
    if account is None:
        # 极端竞态：get_or_create 在 IntegrityError 后重查时账号已被跨进程删除
        # 仅返回临时 device_id 保证本次登录流程可继续，不持久化（无对应账号记录）
        logger.warning("ensure_device_id: account vanished after get_or_create for %s", phone)
        return EtAlienClient.generate_device_id()
    if account.get("device_id"):
        return account["device_id"]
    device_id = EtAlienClient.generate_device_id()
    try:
        repo.update_fields(phone, device_id=device_id)
    except Exception as e:
        # 辅助流程：device_id 已生成并用于本次登录，写盘失败不影响主流程，下次调用会重新尝试持久化
        logger.warning("ensure_device_id update_fields failed for %s: %s", phone, e)
    return device_id


def send_login_code(phone: str) -> dict:
    """发送登录验证码。"""
    device_id = ensure_device_id(phone)
    client = EtAlienClient(phone=phone, device_id=device_id)
    return client.get_login_code()


def _save_login_result(phone: str, login_result: dict, device_id: str, password: str | None = None) -> None:
    """持久化登录结果到账号表（Repository 内部自带 _db_lock）。

    更新 auth_token、user_id、device_id、saved_at，可选保存 password。
    写盘失败时记录警告日志但不中断调用方（辅助流程，token 已在内存中可用）。
    """
    updates = {
        "auth_token": login_result.get("authorization"),
        "user_id": login_result.get("user_id"),
        "device_id": device_id,
        "saved_at": time.time(),
    }
    if password is not None:
        updates["password"] = password
    try:
        repo.update_fields(phone, **updates)
    except Exception as e:
        # 辅助流程：token 持久化失败不影响本次登录结果，下次 token 过期会触发重登重新获取
        logger.warning("_save_login_result update_fields failed for %s: %s", phone, e)


def verify_login(phone: str, code: str | None = None, password: str | None = None) -> dict:
    """验证登录，支持验证码或密码两种方式，成功后持久化 token。"""
    device_id = ensure_device_id(phone)
    client = EtAlienClient(phone=phone, device_id=device_id)
    if password:
        result = client.login_by_password(password)
    else:
        result = client.login_by_code(code)
    if not result.get("_error"):
        _save_login_result(phone, result, device_id, password=password if password else None)
    return result


def get_client_for_account(account: dict, enable_relogin: bool = False) -> EtAlienClient | None:
    """构建账号对应的 EtAlienClient。

    Args:
        account: 账号字典，需包含 phone、auth_token、device_id。
        enable_relogin: 是否注入 token 过期自动重登能力（含密码与持久化回调）。
            领取流程传 True；状态查询传 False（默认），token 过期时由公共流程抛
            NeedLoginError，状态查询函数捕获后标记 token_expired。
            重试配置（max_retries/retry_delay）无论 enable_relogin 是否为 True 都从 settings 注入，
            保持全局一致。settings.json 走内存缓存，无磁盘 IO。

    Returns:
        构造好的 EtAlienClient，若缺少 auth_token 或 device_id 则返回 None。
    """
    phone = account["phone"]
    if not account.get("auth_token") or not account.get("device_id"):
        return None
    settings = load_settings()
    client = EtAlienClient(
        phone=phone,
        auth_token=account["auth_token"],
        device_id=account["device_id"],
        password=account.get("password") if enable_relogin else None,
        on_relogin=_make_relogin_callback(account) if enable_relogin else None,
        max_retries=settings.get("max_retries", 3),
        retry_delay=settings.get("retry_delay", 1.0),
    )
    client.user_id = account.get("user_id")
    return client


def _make_relogin_callback(account: dict) -> Callable[[str | None, int | None, bool], None]:
    """创建重登回调闭包，捕获 account 引用以便持久化新 token / 清除密码。

    回调签名：callback(new_token, user_id, clear_password=False)
    - 密码错误清除密码：clear_password=True
    - 重登成功持久化新 token：new_token 非空
    """
    phone = account["phone"]
    device_id = account.get("device_id", "")

    def callback(new_token: str | None, user_id: int | None, clear_password: bool = False):
        if clear_password:
            _clear_account_password(phone)
            return
        if new_token:
            # 构造 login_result dict 供 _save_login_result 使用
            login_result = {"authorization": new_token, "user_id": user_id}
            _save_login_result(phone, login_result, device_id)

    return callback


def _clamp_watch_cnt(user_watch_cnt: int, video_cnt: int) -> int:
    """对手机端 user_watch_cnt 做 clamp，返回前端展示用的观看数。

    user_watch_cnt 是累计 is_verify=true 的调用次数，可超过 video_cnt
    （实测即使无可发奖励或超额，is_verify 仍返回 true，计数持续递增），
    故按总任务数统计进度时需 clamp 到 video_cnt 上界。
    """
    return min(user_watch_cnt, video_cnt) if video_cnt > 0 else user_watch_cnt


def get_account_status(account: dict, enable_relogin: bool = False) -> dict:
    """查询单个账号的 PC 端状态（时长、任务进度、登录态）。

    仅查询 PC 端接口（fetch_pc_duration + fetch_pc_ad_config），手机端数据由
    get_account_mobile_status 按需查询（翻面时前端调
    /api/accounts/<phone>/mobile_status 触发），避免全量刷新拉取冗余数据。
    内部使用 ThreadPoolExecutor(max_workers=2) 并发查询 2 个接口。
    token 过期时捕获 NeedLoginError 标记 token_expired，不抛异常。

    Args:
        account: 账号字典，需包含 phone、auth_token、device_id。
        enable_relogin: 是否注入 token 过期自动重登能力（含密码与持久化回调）。
            领取流程传 True；状态查询传 False（默认），token 过期时由公共流程抛
            NeedLoginError，状态查询函数捕获后标记 token_expired。
            批量懒加载查询传 True（密码重登兜底，副作用已知见方案文档 3.2）。

    Returns:
        dict 包含以下字段:
        - phone, name, remark, enabled, logged_in, token_valid
        - vip_duration, free_duration, progress（PC 端）
        - mobile_*（保留默认值 0/0/0/false，由 get_account_mobile_status 按需填充）
        - claim_target
        - token_expired（仅 token 失效时出现）
    """
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
        # 手机端字段保留默认值，由 get_account_mobile_status 按需查询填充
        "mobile_duration": 0,
        "mobile_progress": "0/0",
        "mobile_rewarded_count": 0,
        "mobile_claimed_count": 0,
        "mobile_not_get_ad_duration": 0,
        "mobile_error": False,  # 手机端接口查询失败标记，前端据此显示"查询失败"而非误导性的 0/0
        "pc_error": False,  # PC 端接口查询失败标记，与 mobile_error 对称，前端据此区分"无任务"和"查询失败"
        # 暴露给前端，使 idle 总进度口径与领取中对齐（只统计 claim_target 配置的阶段）
        "claim_target": account.get("claim_target", "all"),
    }
    client = get_client_for_account(account, enable_relogin=enable_relogin)
    if client:
        info["logged_in"] = True
        try:
            # 2 个接口并发：PC duration + PC ad_config
            # token 过期时，第一个 result() 即抛 NeedLoginError，跳到 except
            # 其余 futures 会快速失败：_handle_auth_error_and_retry 立即抛 NeedLoginError（无重试），
            # 无需共享取消标志；ThreadPoolExecutor.__exit__ 的 shutdown(wait=True) 等待全部完成
            # 手机端接口（mobile activity / profile）由 get_account_mobile_status 按需查询，
            # 翻面时前端调 /api/accounts/<phone>/mobile_status 触发，避免全量刷新拉取冗余数据
            with ThreadPoolExecutor(max_workers=2) as ex:
                dur_future = ex.submit(client.fetch_pc_duration)
                tasks_future = ex.submit(client.fetch_pc_ad_config)
                # token 过期时 result() 抛 NeedLoginError，其余 futures 仍在飞行中；
                # 在 except 块显式 cancel（仅能取消未开始的提交，已发出的 HTTP 请求
                # 无法中断——requests 库限制，属可接受权衡，避免新增请求被调度）
                futures = [dur_future, tasks_future]
                dur = dur_future.result()
                tasks_result = tasks_future.result()
        except NeedLoginError:
            for f in futures:
                f.cancel()
            info["token_expired"] = True
            client.close()
            return info
        except Exception:
            logger.exception("get_account_status API call failed for %s", phone)
            client.close()
            return info
        # 正常路径（未抛异常）下显式标记 token 有效，否则前端无法区分"未登录"和"token 有效"
        info["token_valid"] = True
        if not dur.get("_error"):
            info["vip_duration"] = dur.get("vip_duration_second", 0)
            info["free_duration"] = dur.get("free_duration_second", 0)
        else:
            info["pc_error"] = True
        if not tasks_result.get("_error"):
            tasks = tasks_result.get("list", [])
            total_w = sum(l["watch_cnt"] for l in tasks)
            total_t = sum(len(l["items"]) for l in tasks)
            info["progress"] = f"{total_w}/{total_t}"
            info["tasks"] = tasks
        else:
            info["pc_error"] = True
        client.close()
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
    except NeedLoginError:
        info["token_expired"] = True
        client.close()
        return info
    except Exception:
        logger.exception("get_account_mobile_status API call failed for %s", phone)
        client.close()
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
        shown_cnt = _clamp_watch_cnt(user_watch_cnt, video_cnt)
        info["mobile_progress"] = f"{shown_cnt}/{video_cnt}"
        info["mobile_tasks"] = activity.get("video_bar", [])
    else:
        info["mobile_error"] = True

    if not profile.get("_error"):
        info["mobile_duration"] = profile.get("remaining_seconds", 0)
        info["mobile_not_get_ad_duration"] = profile.get("mobile_not_get_ad_duration", 0)
    else:
        info["mobile_error"] = True

    client.close()
    return info


def batch_get_account_status(phones: list[str], max_workers: int = 10, total_timeout: float = 90.0) -> list[dict]:
    """批量查询多个账号状态（供懒加载接口使用，启用密码重登兜底）。

    phones 之间用 ThreadPoolExecutor 并发（max_workers），单账号内部仍走
    get_account_status 的 4 接口并发（enable_relogin=True 启用密码重登兜底）。

    Args:
        phones: 待查手机号列表。**调用方需在 clamp 后传入**（本函数不做 clamp）。
        max_workers: phones 之间的并发度，与 run_concurrent_claim 口径一致
                     （settings.max_concurrent）。
        total_timeout: 总超时秒数。超时未完成的 future 取消等待并填占位
                       （query_timeout=true）；已启动的 future 无法中断，结果被丢弃。

    Returns:
        list[dict]：与 phones 顺序无关，每个 status 项含 phone 字段用于前端匹配。
        真实 status 项会补全 mobile_tasks/token_expired 等条件性字段的默认值，
        与占位项字段集对齐；占位项含 phone_not_found 或 query_timeout 标记。
    """
    # 占位项默认字段集（与 phone_not_found 占位一致）
    def _placeholder(phone: str, marker: str | None = None) -> dict:
        item = {
            "phone": phone,
            "logged_in": False,
            "token_valid": False,
            "token_expired": False,
            "vip_duration": 0,
            "free_duration": 0,
            "progress": "0/0",
            "mobile_duration": 0,
            "mobile_progress": "0/0",
            "mobile_rewarded_count": 0,
            "mobile_claimed_count": 0,
            "mobile_not_get_ad_duration": 0,
            "mobile_error": False,
            "mobile_tasks": [],
            "pc_error": False,
        }
        if marker:
            item[marker] = True
        return item

    # 真实 status 项字段补全（与占位项字段集对齐）
    _default_for_real = {
        "token_expired": False,
        "mobile_tasks": [],
    }

    results: list[dict] = []
    if not phones:
        return results

    phone_to_account: dict[str, dict | None] = {p: repo.get(p) for p in phones}
    pending_phones = [p for p, a in phone_to_account.items() if a is not None]
    not_found_phones = [p for p, a in phone_to_account.items() if a is None]

    # 占位项：phone 不存在
    for p in not_found_phones:
        results.append(_placeholder(p, marker="phone_not_found"))

    if not pending_phones:
        return results

    deadline = time.monotonic() + total_timeout
    # 不用 with 语句：退出时 shutdown(wait=True) 会阻塞等待已启动的 future 完成，
    # 违反"超时未完成的 future 放弃等待"的设计（方案 3.2）。手动管理 executor，
    # 超时后用 shutdown(wait=False, cancel_futures=True) 立即返回（已启动的 future
    # 在后台继续执行至完成，结果被丢弃；未启动的 future 被取消）。
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {executor.submit(get_account_status, phone_to_account[p], True): p for p in pending_phones}
    try:
        # as_completed 传 timeout 防止无限阻塞：剩余 future 全部慢时，无 timeout 的
        # as_completed 会一直阻塞到所有 future 完成，导致 90s 总超时失效。
        # timeout 到期后 __next__() 抛 TimeoutError，由外层 except 捕获处理剩余 future。
        remaining = max(0.1, deadline - time.monotonic())
        try:
            for future in as_completed(futures, timeout=remaining):
                phone = futures[future]
                # 防御性兜底：as_completed yield 的 future 可能已完成但 deadline 已过
                if time.monotonic() >= deadline:
                    future.cancel()
                    results.append(_placeholder(phone, marker="query_timeout"))
                    continue
                try:
                    info = future.result()  # future 已完成，不会阻塞
                    # 补全条件性字段默认值，与占位项字段集对齐
                    for k, v in _default_for_real.items():
                        info.setdefault(k, v)
                    # 过滤掉基础字段（name/remark/enabled/claim_target）和 tasks
                    # phone 作为标识符保留
                    status_item = {k: v for k, v in info.items()
                                   if k not in ("name", "remark", "enabled", "claim_target", "tasks")}
                    results.append(status_item)
                except Exception:
                    logger.exception("batch_get_account_status failed for %s", phone)
                    # 单账号内部异常 → 占位（不含 query_timeout / phone_not_found 标记，
                    # 前端按 token_valid=false 走 account-disabled 样式，由设计文档 4.2 失败处理定义）
                    results.append(_placeholder(phone))
        except TimeoutError:
            # as_completed 总超时：剩余未完成的 future 填 query_timeout 占位
            for future, phone in futures.items():
                if not future.done():
                    future.cancel()
                    results.append(_placeholder(phone, marker="query_timeout"))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return results


def _get_cli_exe_path() -> str:
    """生成计划任务中 CLI 模式的执行命令路径。

    - 打包环境: 返回 exe 路径（带引号） + --cli --auto-close
    - 开发环境: 返回 main.py 路径（带引号）
    """
    if getattr(sys, "frozen", False):
        exe_path = sys.executable
        return f'"{exe_path}" --cli --auto-close'
    return f'"{os.path.join(PROJECT_DIR, "main.py")}"'


def query_schedule() -> dict[str, Any]:
    """查询 Windows 计划任务状态，返回是否存在、启用状态和执行时间。"""
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
        if sb is not None and sb.text and "T" in sb.text:
            # 格式 2026-07-02T21:00:00，截取时间部分 HH:MM
            time_part = sb.text.split("T", 1)[1]
            if len(time_part) >= 5 and time_part[2] == ":":
                schedule_time = time_part[:5]

        # 启用状态：Settings/Enabled 缺省视为 True（任务默认启用），
        # 显式 "false" 才视为禁用；同时检查 Triggers/Enabled 任一为 false 即视为禁用
        enabled = True
        settings_enabled = root.find("./ms:Settings/ms:Enabled", ns)
        if settings_enabled is not None and settings_enabled.text and settings_enabled.text.lower() == "false":
            enabled = False
        for trig_en in root.findall(".//ms:Triggers//ms:Enabled", ns):
            if trig_en.text and trig_en.text.lower() == "false":
                enabled = False
                break

        return {"exists": True, "enabled": enabled, "time": schedule_time}
    except Exception as e:
        logger.exception("Failed to query schedule")
        return {"exists": False, "enabled": False, "time": fallback_time, "error": str(e)}


def create_schedule(schedule_time: str) -> dict[str, Any]:
    """创建每日自动执行的 Windows 计划任务。"""
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
            try:
                save_settings(settings)
            except OSError as e:
                # 计划任务已成功创建，仅配置文件保存失败：返回部分成功，避免用户误以为创建失败
                logger.warning("计划任务已创建但配置保存失败: %s", e)
                return {"ok": True, "msg": f"计划任务已创建（每天 {schedule_time} 执行），但配置文件保存失败，下次启动可能回退"}
            return {"ok": True, "msg": f"计划任务已创建，每天 {schedule_time} 执行"}
        else:
            logger.warning("schtasks create failed: %s", result.stderr.strip())
            return {"error": f"创建失败: {result.stderr.strip()}"}
    except Exception as e:
        logger.exception("Failed to create schedule")
        return {"error": str(e)}


def delete_schedule() -> dict[str, Any]:
    """删除 Windows 计划任务。"""
    try:
        result = subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            capture_output=True, text=True, timeout=10,
            creationflags=SUBPROCESS_FLAGS,
        )
        if result.returncode == 0:
            return {"ok": True, "msg": "计划任务已删除"}
        # schtasks 对不存在的任务返回特定错误信息，视为"已删除"幂等结果；其他错误如实返回
        stderr_lower = (result.stderr or "").lower()
        if "cannot find" in stderr_lower or "找不到" in stderr_lower:
            return {"ok": True, "msg": "计划任务不存在或已删除"}
        return {"error": f"删除失败: {(result.stderr or '').strip()}"}
    except Exception as e:
        logger.exception("Failed to delete schedule")
        return {"error": str(e)}


def format_duration(seconds: int) -> str:
    """将秒数格式化为 HH:MM:SS。"""
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def get_unwatched_count(tasks: list) -> int:
    """统计所有任务层级中未观看的条目总数。"""
    return sum(
        len([item for item in level["items"] if not item["is_watch"]])
        for level in tasks
    )


def _clear_account_password(phone: str) -> None:
    """清除账号保存的密码（密码错误时调用，避免反复尝试触发风控）。

    通过 update_fields 将 password 设为 None（Repository 内部不会显式置 NULL，
    但传入 None 会被 SQLite 存为 NULL，等价于清除）。
    """
    try:
        repo.update_fields(phone, password=None)
    except Exception as e:
        # 辅助流程：清除密码失败不中断领取主流程，下次密码错误时仍会再次尝试清除
        logger.warning("_clear_account_password update_fields failed for %s: %s", phone, e)


def init_client(account: dict) -> tuple[EtAlienClient | None, str]:
    """初始化客户端。不再预检查 token，token 有效性由 API 请求自动检测。

    Returns:
        (client, status): status 为 "ok" / "need_login"
        - "ok": client 已就绪（注入了重登能力，token 过期时由公共流程处理）
        - "need_login": 未登录（无 auth_token 或 device_id）
    """
    phone = account["phone"]
    client = get_client_for_account(account, enable_relogin=True)
    if client and client.is_logged_in():
        logger.info("  [%s] 已登录，token 有效性由首个 API 请求检测", phone)
        return client, "ok"
    logger.info("  [%s] 未找到登录状态", phone)
    return None, "need_login"


def _make_claim_result(phone: str, label: str, status: str,
                       vip_before: int = 0, vip_after: int = 0,
                       mobile_before: int = 0, mobile_after: int = 0,
                       claimed: int = 0, failed: int = 0,
                       claim_target: str = "all",
                       progress_entry: dict | None = None,
                       **progress_extra) -> dict:
    """构建领取结果字典，并同步更新 progress_entry（如提供）。

    用于 need_login / network_error 等简单路径的结果构建，
    同时将状态信息写入 progress_entry 供前端进度查询。
    """
    result = {
        "phone": phone,
        "label": label,
        "status": status,
        "claim_target": claim_target,
        "vip_before": vip_before,
        "vip_after": vip_after,
        "mobile_before": mobile_before,
        "mobile_after": mobile_after,
        "claimed": claimed,
        "failed": failed,
    }
    if progress_entry is not None:
        progress_entry.update({
            "status": status,
            "vip_before": vip_before, "vip_after": vip_after,
            "mobile_before": mobile_before, "mobile_after": mobile_after,
            **progress_extra,
        })
    return result


def _claim_pc_phase(client: EtAlienClient, phone: str,
                    interval: float, max_rounds: int,
                    unwatched_before: int,
                    progress_entry: dict | None = None) -> tuple[int, int]:
    """执行PC加速权益的单阶段领取循环。

    优化：每轮只发补发请求，开始/结束各查1次config校准，请求量降约2/3。
    退出条件：local_success>=unwatched_before（领够）/ consecutive_fail>=3（失效）/ round>=max_rounds。
    真实领取数由结束查询的 unwatched 差值确定，比 is_verify 计数更可信。
    token 过期由 _post/_get 公共流程自动处理（重登或抛 NeedLoginError），
    此处只需处理非 auth 错误的失败计数。
    """
    local_success = 0
    total_failed = 0
    consecutive_fail = 0
    round_num = 0

    if progress_entry is not None:
        logger.info("  [%s] PC阶段开始: unwatched=%d, entry.current=%d, entry.total=%d",
                    phone, unwatched_before,
                    progress_entry.get("current", 0), progress_entry.get("total", 0))

    # max_rounds=0 时不进入循环，跳过结束校准：避免服务端状态因其他原因变化
    # （如其他设备领取）被差值误归功于本次未执行的领取
    if max_rounds == 0:
        return 0, 0

    while round_num < max_rounds:
        if local_success >= unwatched_before:
            break
        round_num += 1
        backup_result = client.pc_ad_callback_backup(ad_id=PC_AD_ID, business=PC_BUSINESS)
        if backup_result.get("_error"):
            logger.info("  [%s] PC第%d轮 error: %s", phone, round_num, backup_result.get("msg", ""))
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
    """单阶段领取结果状态判定。

    规则:
    - total_failed == 0 → "done"（全部成功）
    - total_claimed > 0 → "partial"（部分完成）
    - 否则 → "error"（全部失败）
    """
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
                        progress_entry: dict | None = None) -> tuple[int, int]:
    """执行手机加速权益的领取循环。

    优化：每轮只发补发请求，开始/结束各查1次activity校准，请求量降约2/3。
    退出条件：local_success>=pending_count（领够）/ consecutive_fail>=3（失效）/ round>=max_rounds。
    真实领取数由结束查询的 user_watch_cnt 差值确定。

    注意：手机端任务需按顺序领取，无奖励任务也必须领取才能解锁后续有奖励任务，
    因此 pending_count 基于总任务数 video_cnt 计算，而非 rewarded_count。
    token 过期由 _post/_get 公共流程自动处理（重登或抛 NeedLoginError），
    此处只需处理非 auth 错误的失败计数。
    """
    local_success = 0
    total_failed = 0
    consecutive_fail = 0
    round_num = 0

    if progress_entry is not None:
        logger.info("  [%s] 手机阶段开始: pending=%d, entry.current=%d, entry.total=%d",
                    phone, pending_count,
                    progress_entry.get("current", 0), progress_entry.get("total", 0))

    # max_rounds=0 时不进入循环，跳过结束校准：避免服务端状态因其他原因变化
    # （如其他设备领取）被差值误归功于本次未执行的领取
    if max_rounds == 0:
        return 0, 0

    while round_num < max_rounds:
        if local_success >= pending_count:
            break
        round_num += 1
        backup_result = client.pc_ad_callback_backup(ad_id=MOBILE_AD_ID, business=MOBILE_BUSINESS)
        if backup_result.get("_error"):
            logger.info("  [%s] 手机第%d轮 error: %s", phone, round_num, backup_result.get("msg", ""))
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
    """单账号领取主流程，按 claim_target 分流执行 PC 和/或手机加速权益领取。

    流程:
    1. init_client 初始化客户端（注入 token 过期自动重登能力）
    2. 根据 claim_target 执行 PC 阶段和/或 Mobile 阶段
       - 每阶段先查初始状态（config/activity + duration/profile）
       - 全部已完成 → "already_done"，跳过领取循环
       - 有待完成 → 进入领取循环（_claim_pc_phase / _claim_mobile_phase）
       - 循环中 token 过期由 client 公共流程自动处理（重登或抛 NeedLoginError）
    3. 综合两阶段结果调用 _combine_phase_status 判定最终状态
    4. PC 阶段已成功但因手机阶段 token 过期中断时，附加 pc_partial 字段保留已领取数据

    Args:
        account: 账号字典，需包含 phone、auth_token、device_id，
                可选 claim_target（"pc"/"mobile"/"all"，默认 "all"）、password
        settings: 全局设置，需包含 request_interval、max_rounds、mobile_max_rounds
        progress_entry: 可选的进度追踪字典，用于前端实时查询领取进度

    Returns:
        领取结果 dict，包含:
        - phone, label, status, claim_target
        - vip_before, vip_after（PC 端 VIP 时长）
        - mobile_before, mobile_after（手机端加速时长）
        - claimed, failed（总领取计数）
        - phases（各阶段 status 列表）
        - pc_partial（仅 PC 阶段成功但后续中断时出现）
    """
    phone = account["phone"]
    label = account_label(account)
    interval = settings.get("request_interval", 1.0)
    max_rounds = settings.get("max_rounds", 21)
    mob_max_rounds = settings.get("mobile_max_rounds", 7)
    # claim_target: "all" / "pc" / "mobile"，默认 "all"（旧账号兼容）
    claim_target = account.get("claim_target", "all")

    client, init_status = init_client(account)

    # init_client 不再返回 "network_error"，仅 "ok" / "need_login"
    if not client:
        return _make_claim_result(phone, label, init_status,
                                  claim_target=claim_target,
                                  progress_entry=progress_entry)

    # 累计两阶段的统计
    total_claimed = 0
    total_failed = 0
    vip_before = 0
    vip_after = 0
    mobile_before = 0
    mobile_after = 0
    phase_results: list[str] = []  # 每个阶段的 status
    # 维护 PC 阶段结果，供 except 块附加（避免手机阶段抛 NeedLoginError 时丢失 PC 数据）
    pc_partial: dict | None = None

    try:
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

                    pc_claimed, pc_failed = _claim_pc_phase(client, phone, interval, max_rounds, total_unwatched, progress_entry)
                    total_claimed += pc_claimed
                    total_failed += pc_failed

                    dur_after = client.fetch_pc_duration()
                    vip_after = dur_after.get("vip_duration_second", 0) if not dur_after.get("_error") else vip_before
                    logger.info("  [%s] PC领取完成, VIP: %s -> %s (+%s)",
                                phone, format_duration(vip_before), format_duration(vip_after),
                                format_duration(vip_after - vip_before))
                    phase_results.append(_calculate_final_status(pc_claimed, pc_failed))

            # PC 阶段完成后记录已领取数据，供 except 块附加
            pc_partial = {
                "pc_claimed": total_claimed,
                "pc_failed": total_failed,
                "vip_before": vip_before,
                "vip_after": vip_after,
                "phase_status": phase_results[-1] if phase_results else None,
            }

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
                    progress_entry["mobile_initial"] = _clamp_watch_cnt(watched_before, video_cnt)
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
                        progress_entry["mobile_before"] = mobile_before
                        if progress_entry.get("total", 0) == 0 and video_cnt > 0:
                            progress_entry["total"] = video_cnt

                    pending = video_cnt - watched_before
                    mob_claimed, mob_failed = _claim_mobile_phase(client, phone, interval, mob_max_rounds, pending, watched_before, progress_entry)
                    total_claimed += mob_claimed
                    total_failed += mob_failed

                    profile_after = client.fetch_mobile_profile()
                    mobile_after = profile_after.get("remaining_seconds", mobile_before) if not profile_after.get("_error") else mobile_before
                    logger.info("  [%s] 手机端领取完成, 加速时长: %s -> %s (+%s)",
                                phone, format_duration(mobile_before), format_duration(mobile_after),
                                format_duration(mobile_after - mobile_before))
                    phase_results.append(_calculate_final_status(mob_claimed, mob_failed))

    except NeedLoginError as e:
        # token 过期且无法重登（无密码/密码错误/已重登过仍失败）
        logger.info("[%s] 领取中 token 失效且无法重登: %s", phone, e)
        result = _make_claim_result(
            phone, label, "need_login",
            vip_before=pc_partial["vip_before"] if pc_partial else 0,
            vip_after=pc_partial["vip_after"] if pc_partial else 0,
            mobile_before=mobile_before, mobile_after=mobile_after,
            claim_target=claim_target,
            progress_entry=progress_entry,
        )
        # 若 PC 阶段已成功，附加其结果避免丢失已领取数据（前端不解析，仅用于日志/调试）
        if pc_partial is not None:
            result["pc_partial"] = pc_partial
        return result
    except requests.RequestException as e:
        # 网络异常（重试 max_retries 次后仍失败）
        logger.warning("[%s] 领取中网络异常: %s", phone, e)
        result = _make_claim_result(
            phone, label, "network_error",
            vip_before=pc_partial["vip_before"] if pc_partial else 0,
            vip_after=pc_partial["vip_after"] if pc_partial else 0,
            mobile_before=mobile_before, mobile_after=mobile_after,
            claim_target=claim_target,
            progress_entry=progress_entry,
        )
        if pc_partial is not None:
            result["pc_partial"] = pc_partial
        return result
    finally:
        # 显式释放 client 的 requests.Session 连接池（client 非 None 时才进入此块）
        if client:
            client.close()

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


def run_concurrent_claim(accounts: list[dict], settings: dict, claim_mgr: Any = None) -> list[dict]:
    """并发执行多个账号的领取任务。

    使用 ThreadPoolExecutor 按 max_concurrent 并发度提交，通过 claim_mgr 追踪进度。
    返回按 id 升序排序的结果列表（新加用户靠后），便于 CLI 输出与通知顺序稳定。

    Args:
        accounts: 待领取的账号列表。
        settings: 全局设置，需包含 max_concurrent、request_interval 等字段。
        claim_mgr: 可选进度管理器，用于前端实时查询领取进度。

    Returns:
        按 id 升序排序的领取结果列表。
    """
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
                    "phase": "pc",
                    "current": 0,
                    "total": 0,
                    "vip_before": 0,
                    "vip_after": 0,
                    "mobile_before": 0,
                    "mobile_after": 0,
                    "pc_initial": 0,
                    "pc_total": 0,
                    "pc_claimed": 0,
                    "mobile_initial": 0,
                    "mobile_total": 0,
                }
                claim_mgr.add_progress_entry(entry)

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
                    "claim_target": account.get("claim_target", "all"),
                    "vip_before": 0,
                    "vip_after": 0,
                    "mobile_before": 0,
                    "mobile_after": 0,
                    "claimed": 0,
                    "failed": 0,
                    "phases": ["error"],
                }
                results.append(error_result)
                if claim_mgr is not None:
                    claim_mgr.update_progress_entry(account["phone"], {"status": "error", "error": str(e)})

    # 按 id 升序排序，保证 CLI 输出与通知顺序稳定
    phone_to_id = {a["phone"]: a.get("id", 0) for a in accounts}
    results.sort(key=lambda r: phone_to_id.get(r.get("phone", ""), 0), reverse=False)
    return results
