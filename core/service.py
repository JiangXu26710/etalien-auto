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
from threading import Lock, Thread
from typing import Any, Callable

import requests

from core.client import EtAlienClient, NeedLoginError
from core.config import load_settings, save_settings, validate_settings, PROJECT_DIR
from core.db import DbAccountRepository
from core.privacy import mask_phone

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

# 问题态集合：条目首次进入这些状态时写入 firstSeenTs，
# network_error 额外写入 lastReqTs（每次重试失败刷新为最新）。
# 4.4.2 关键修订：service.py L766/L1286 的正常状态变更路径直接调
# progress_entry.update 修改 dict 引用，不经过 claim_mgr.update_progress_entry，
# 必须在此处兜底写入才能覆盖绝大多数问题账号。
PROBLEM_STATUSES = {'partial', 'need_login', 'error', 'network_error'}

# CONC-006: 串行化 create_schedule，避免并发调用的 check-then-act 竞态
# （query_schedule 检查 → schtasks /create 创建，两步之间可能被并发请求插入）
_schedule_lock = Lock()


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
        logger.warning("ensure_device_id: account vanished after get_or_create for %s", mask_phone(phone))
        return EtAlienClient.generate_device_id()
    if account.get("device_id"):
        return account["device_id"]
    device_id = EtAlienClient.generate_device_id()
    try:
        repo.update_fields(phone, device_id=device_id)
    except Exception:
        # 辅助流程：device_id 已生成并用于本次登录，写盘失败不影响主流程，下次调用会重新尝试持久化
        # EH-011: logger.exception 保留 stack trace，便于定位 update_fields 内部失败位置
        logger.exception("ensure_device_id update_fields failed for %s", mask_phone(phone))
    return device_id


def send_login_code(phone: str) -> dict:
    """发送登录验证码。"""
    device_id = ensure_device_id(phone)
    client = EtAlienClient(phone=phone, device_id=device_id)
    try:
        return client.get_login_code()
    finally:
        client.close()


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
    except Exception:
        # 辅助流程：token 持久化失败不影响本次登录结果，下次 token 过期会触发重登重新获取
        # EH-011: logger.exception 保留 stack trace，便于定位 update_fields 内部失败位置
        logger.exception("_save_login_result update_fields failed for %s", mask_phone(phone))


def verify_login(phone: str, code: str | None = None, password: str | None = None) -> dict:
    """验证登录，支持验证码或密码两种方式，成功后持久化 token。"""
    device_id = ensure_device_id(phone)
    client = EtAlienClient(phone=phone, device_id=device_id)
    try:
        if password:
            result = client.login_by_password(password)
        else:
            result = client.login_by_code(code)
    finally:
        client.close()
    if not result.get("_error"):
        _save_login_result(phone, result, device_id, password=password if password else None)
    return result


def get_client_for_account(account: dict, enable_relogin: bool = False,
                           settings: dict | None = None) -> EtAlienClient | None:
    """构建账号对应的 EtAlienClient。

    Args:
        account: 账号字典，需包含 phone、auth_token、device_id。
        enable_relogin: 是否注入 token 过期自动重登能力（含密码与持久化回调）。
            领取流程传 True；状态查询传 False（默认），token 过期时由公共流程抛
            NeedLoginError，状态查询函数捕获后标记 token_expired。
            重试配置（max_retries/retry_delay）无论 enable_relogin 是否为 True 都从 settings 注入，
            保持全局一致。settings.json 走内存缓存，无磁盘 IO。
        settings: 可选的 settings 引用（PERF-003 热路径优化）。批量场景由调用方
            在入口处一次 load_settings() 后传入，避免每个 worker 重复 deepcopy。
            None 时内部 fallback 调用 load_settings()。

    Returns:
        构造好的 EtAlienClient，若缺少 auth_token 或 device_id 则返回 None。
    """
    phone = account["phone"]
    if not account.get("auth_token") or not account.get("device_id"):
        return None
    if settings is None:
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


def get_account_status(account: dict, enable_relogin: bool = False,
                       settings: dict | None = None) -> dict:
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
            批量懒加载查询传 True（密码重登兜底，副作用已知）。
        settings: 可选的 settings 引用（PERF-003 热路径优化）。批量场景由
            batch_get_account_status 入口处一次 load_settings() 后传入，
            避免每个 worker 重复 deepcopy。None 时内部 fallback 调用 load_settings()。

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
    # PERF-003: 批量场景由调用方传入 settings，避免内部每次 load_settings() deepcopy
    if settings is None:
        settings = load_settings()
    client = get_client_for_account(account, enable_relogin=enable_relogin, settings=settings)
    if client:
        info["logged_in"] = True
        try:
            # 2 个接口并发：PC duration + PC ad_config
            # 用 Thread 替代 ThreadPoolExecutor(max_workers=2)，避免频繁创建/销毁线程池
            # token 过期时，第一个 join 后检查结果即可感知 NeedLoginError
            dur_result: list = [None]
            dur_exc: list = [None]
            tasks_result_ref: list = [None]
            tasks_exc: list = [None]

            def _fetch_duration():
                try:
                    dur_result[0] = client.fetch_pc_duration()
                except Exception as e:
                    # EH-009: 线程内异常记 debug 日志保留原始 stack trace（外层 logger.exception 记录的是 raise 位置）
                    logger.debug("[%s] _fetch_duration thread exception: %s", mask_phone(phone), e, exc_info=True)
                    dur_exc[0] = e

            def _fetch_tasks():
                try:
                    tasks_result_ref[0] = client.fetch_pc_ad_config()
                except Exception as e:
                    # EH-009: 线程内异常记 debug 日志保留原始 stack trace
                    logger.debug("[%s] _fetch_tasks thread exception: %s", mask_phone(phone), e, exc_info=True)
                    tasks_exc[0] = e

            t_dur = Thread(target=_fetch_duration)
            t_tasks = Thread(target=_fetch_tasks)
            t_dur.start()
            t_tasks.start()
            # EH-005: join timeout 根据 max_retries / retry_delay 动态计算
            # 单接口最坏耗时 (max_retries+1)*(30+retry_delay)，+5s 冗余
            # PERF-003: settings 已由函数入参传入（或上方 fallback 调用 load_settings()），此处不再 deepcopy
            _max_retries = settings.get("max_retries", 3)
            _retry_delay = settings.get("retry_delay", 1.0)
            join_timeout = (max(_max_retries, 0) + 1) * (30 + max(_retry_delay, 0)) + 5
            t_dur.join(timeout=join_timeout)
            t_tasks.join(timeout=join_timeout)
            if t_dur.is_alive():
                logger.warning("[%s] fetch_pc_duration join 超时", mask_phone(phone))
                dur_exc[0] = requests.Timeout("fetch_pc_duration join timeout")
            if t_tasks.is_alive():
                logger.warning("[%s] fetch_pc_ad_config join 超时", mask_phone(phone))
                tasks_exc[0] = requests.Timeout("fetch_pc_ad_config join timeout")

            # 优先抛 NeedLoginError（让调用方感知 token 过期）
            if isinstance(dur_exc[0], NeedLoginError):
                raise dur_exc[0]
            if isinstance(tasks_exc[0], NeedLoginError):
                raise tasks_exc[0]
            if dur_exc[0]:
                raise dur_exc[0]
            if tasks_exc[0]:
                raise tasks_exc[0]

            dur = dur_result[0]
            tasks_result = tasks_result_ref[0]
        except NeedLoginError:
            info["token_expired"] = True
            return info
        except requests.RequestException as e:
            # EH-006: 预期异常（网络/超时），前端按"查询失败"展示，下次刷新可能恢复；不带 exc_info 避免日志膨胀
            logger.warning("get_account_status network error for %s: %s: %s",
                           mask_phone(phone), type(e).__name__, e)
            return info
        except Exception:
            # EH-006: 未预期异常（编程 bug）：记完整栈便于诊断
            logger.exception("get_account_status unexpected for %s", mask_phone(phone))
            return info
        finally:
            client.close()
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
        # 2 个接口并发：mobile activity + mobile profile
        # 用 Thread 替代 ThreadPoolExecutor(max_workers=2)，避免频繁创建/销毁线程池
        act_result: list = [None]
        act_exc: list = [None]
        prof_result: list = [None]
        prof_exc: list = [None]

        def _fetch_activity():
            try:
                act_result[0] = client.fetch_mobile_ad_activity()
            except Exception as e:
                # EH-009: 线程内异常记 debug 日志保留原始 stack trace
                logger.debug("[%s] _fetch_activity thread exception: %s", mask_phone(phone), e, exc_info=True)
                act_exc[0] = e

        def _fetch_profile():
            try:
                prof_result[0] = client.fetch_mobile_profile()
            except Exception as e:
                # EH-009: 线程内异常记 debug 日志保留原始 stack trace
                logger.debug("[%s] _fetch_profile thread exception: %s", mask_phone(phone), e, exc_info=True)
                prof_exc[0] = e

        t_act = Thread(target=_fetch_activity)
        t_prof = Thread(target=_fetch_profile)
        t_act.start()
        t_prof.start()
        # EH-005: join timeout 根据 max_retries / retry_delay 动态计算
        # 单接口最坏耗时 (max_retries+1)*(30+retry_delay)，+5s 冗余
        _settings = load_settings()
        _max_retries = _settings.get("max_retries", 3)
        _retry_delay = _settings.get("retry_delay", 1.0)
        join_timeout = (max(_max_retries, 0) + 1) * (30 + max(_retry_delay, 0)) + 5
        t_act.join(timeout=join_timeout)
        t_prof.join(timeout=join_timeout)
        if t_act.is_alive():
            logger.warning("[%s] fetch_mobile_ad_activity join 超时", mask_phone(phone))
            act_exc[0] = requests.Timeout("fetch_mobile_ad_activity join timeout")
        if t_prof.is_alive():
            logger.warning("[%s] fetch_mobile_profile join 超时", mask_phone(phone))
            prof_exc[0] = requests.Timeout("fetch_mobile_profile join timeout")

        if isinstance(act_exc[0], NeedLoginError):
            raise act_exc[0]
        if isinstance(prof_exc[0], NeedLoginError):
            raise prof_exc[0]
        if act_exc[0]:
            raise act_exc[0]
        if prof_exc[0]:
            raise prof_exc[0]

        activity = act_result[0]
        profile = prof_result[0]
    except NeedLoginError:
        info["token_expired"] = True
        return info
    except requests.RequestException as e:
        # EH-006: 预期异常（网络/超时），前端按"查询失败"展示，下次刷新可能恢复；不带 exc_info 避免日志膨胀
        logger.warning("get_account_mobile_status network error for %s: %s: %s",
                       mask_phone(phone), type(e).__name__, e)
        return info
    except Exception:
        # EH-006: 未预期异常（编程 bug）：记完整栈便于诊断
        logger.exception("get_account_mobile_status unexpected for %s", mask_phone(phone))
        return info
    finally:
        client.close()

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

    # 批量查询：单条 SQL + 单次锁获取，替代逐个 repo.get(p) 的 N+1 模式
    phone_to_account = repo.get_by_phones(phones)
    pending_phones = list(phone_to_account.keys())
    not_found_phones = [p for p in phones if p not in phone_to_account]

    # 占位项：phone 不存在
    for p in not_found_phones:
        results.append(_placeholder(p, marker="phone_not_found"))

    if not pending_phones:
        return results

    deadline = time.monotonic() + total_timeout
    # PERF-003: 入口处一次 load_settings() 后传给所有 worker，避免每个 get_account_status
    # 内部重复 deepcopy 整个 settings（1000 账号场景下省 999 次 deepcopy）
    batch_settings = load_settings()
    # auto_relogin 开关：批量状态查询是否启用密码重登兜底（与 claim_for_account 口径一致）
    batch_enable_relogin = batch_settings.get("auto_relogin", True)
    # 不用 with 语句：with 退出时 __exit__ 会调用 shutdown(wait=True) 阻塞等待已启动的
    # future 完成，违反 PERF-001 设计权衡（用户决策保持现状：超时后已启动的 future 在后台
    # 继续执行至完成，结果被丢弃，主线程立即返回）。手动管理 executor，finally 中用
    # shutdown(wait=False, cancel_futures=True) 立即返回（CONC-004 因与 PERF-001 冲突保持现状）。
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {executor.submit(get_account_status, phone_to_account[p], batch_enable_relogin, batch_settings): p for p in pending_phones}
    try:
        # as_completed 传 timeout 防止无限阻塞：剩余 future 全部慢时，无 timeout 的
        # as_completed 会一直阻塞到所有 future 完成，导致 90s 总超时失效。
        # timeout 到期后 __next__() 抛 TimeoutError，由外层 except 捕获处理剩余 future。
        remaining = max(0.1, deadline - time.monotonic())
        # processed_phones 记录正常路径已处理的 phone，避免 TimeoutError 路径重复 append
        # （as_completed 抛 TimeoutError 时 futures 字典仍含已 yield 过的 future，无法区分
        # "已处理"与"未处理"，需用显式集合去重）
        processed_phones: set[str] = set()
        try:
            for future in as_completed(futures, timeout=remaining):
                phone = futures[future]
                processed_phones.add(phone)
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
                except (requests.RequestException, TimeoutError) as e:
                    # 预期异常：网络/超时 → 占位（不含 query_timeout / phone_not_found 标记，
                    # 前端按 token_valid=false 走 account-disabled 样式，由设计文档 4.2 失败处理定义）
                    logger.warning("batch_get_account_status network/timeout for %s: %s: %s",
                                   mask_phone(phone), type(e).__name__, e)
                    results.append(_placeholder(phone))
                except Exception as e:
                    # 未预期异常（编程 bug）：兜底但记录类型与栈，便于诊断
                    logger.exception("batch_get_account_status unexpected for %s: %s",
                                     mask_phone(phone), type(e).__name__)
                    item = _placeholder(phone)
                    item["unexpected_error"] = True
                    item["error_type"] = type(e).__name__
                    results.append(item)
        except TimeoutError:
            # as_completed 总超时：遍历所有 future，done() 的提取结果（as_completed 抛
            # TimeoutError 瞬间可能有 future 已完成但未被 yield，结果不应丢弃），
            # 未 done() 的填 query_timeout 占位
            for future, phone in futures.items():
                if phone in processed_phones:
                    continue  # 正常路径已处理，跳过避免重复 append
                if future.done():
                    try:
                        info = future.result()
                        for k, v in _default_for_real.items():
                            info.setdefault(k, v)
                        status_item = {k: v for k, v in info.items()
                                       if k not in ("name", "remark", "enabled", "claim_target", "tasks")}
                        results.append(status_item)
                    except (requests.RequestException, TimeoutError) as e:
                        # 预期异常：网络/超时 → 默认占位
                        logger.warning("batch_get_account_status network/timeout (timeout branch) for %s: %s: %s",
                                       mask_phone(phone), type(e).__name__, e)
                        results.append(_placeholder(phone))
                    except Exception as e:
                        # 未预期异常：兜底但记录类型与栈，便于诊断
                        logger.exception("batch_get_account_status unexpected (timeout branch) for %s: %s",
                                         mask_phone(phone), type(e).__name__)
                        item = _placeholder(phone)
                        item["unexpected_error"] = True
                        item["error_type"] = type(e).__name__
                        results.append(item)
                else:
                    future.cancel()
                    results.append(_placeholder(phone, marker="query_timeout"))
    finally:
        # PERF-001 设计权衡（用户决策保持现状）：超时后已启动的 future 在后台继续执行，
        # 结果被丢弃；cancel_futures=True 仅取消尚未启动的 future
        executor.shutdown(wait=False, cancel_futures=True)

    return results


def batch_update_accounts(phones: list[str], action: str) -> dict:
    """批量启用/禁用/删除账号。

    enable/disable 用单条 UPDATE ... WHERE phone IN (...) + 单次 commit；
    delete 用单条 DELETE ... WHERE phone IN (...) + 单次 commit。
    替代逐条 update_fields/delete 的 N 次 commit 模式，大幅减少 fsync 次数。

    非原子语义保留：affected 返回实际受影响行数（不含不存在的 phone），
    failed_phones 返回不存在的 phone（affected + len(failed_phones) 可能 < len(phones)
    因重复 phone 被 SQL IN 自动去重）。

    性能：用 get_existing_phones（仅 SELECT phone）替代 get_by_phones（SELECT *），
    避免载入 password/auth_token 等敏感字段冗余开销。

    Args:
        phones: 手机号列表（API 层已校验上限 1000 并去重，此处不重复校验）
        action: ``"enable"`` / ``"disable"`` / ``"delete"``

    Returns:
        ``{"ok": True, "affected": int, "failed_phones": list[str]}``：
        - affected：实际成功处理的账号数
        - failed_phones：处理失败的 phone 列表
    """
    if not phones:
        return {"ok": True, "affected": 0, "failed_phones": []}

    try:
        # 仅 SELECT phone 列判断存在性，避免 SELECT * 载入敏感字段
        existing_phones = repo.get_existing_phones(phones)
        unique_phones = set(phones)
        not_found = list(unique_phones - existing_phones)

        existing_list = list(existing_phones)
        if action == "enable":
            affected = repo.batch_set_enabled(existing_list, True)
        elif action == "disable":
            affected = repo.batch_set_enabled(existing_list, False)
        else:  # action == "delete"
            affected = repo.batch_delete(existing_list)
    except Exception:
        # EH-002: 用 logger.exception 保留 stack trace；EH-004: 异常路径返回 ok=False 与 error 字段，
        # 由调用方（gui/api.py）转为 500 响应，避免前端误以为操作成功
        logger.exception("batch_update_accounts %s failed", action)
        return {"ok": False, "error": "批量操作失败", "affected": 0, "failed_phones": list(phones)}
    return {"ok": True, "affected": affected, "failed_phones": not_found}


def get_enabled_accounts_by_phones(phones: list[str]) -> list[dict]:
    """按 phones 列表取 enabled 账号（供 /api/claim 搜索态用）。

    批量 SQL 查询 phones 中 enabled=True 的账号。
    用于搜索态下"开始领取"只领搜索结果中启用账号的场景。

    Args:
        phones: 手机号列表（可能含不存在或重复的 phone）

    Returns:
        list[dict]：仅 enabled=True 的账号完整字典列表（含 auth_token / device_id
        等字段，供 run_concurrent_claim 用）。重复 phone 自动去重（SQL IN 语义）。
    """
    if not phones:
        return []
    try:
        return repo.get_enabled_by_phones(phones)
    except Exception:
        # EH-011: logger.exception 保留 stack trace，便于定位 repo 内部失败位置
        logger.exception("get_enabled_accounts_by_phones failed")
        return []


def _get_cli_exe_path() -> str:
    """生成计划任务中 CLI 模式的执行命令路径。

    - 打包环境: 返回 exe 路径（带引号） + --cli --auto-close
    - 开发环境: 通过 gui/app.py 入口启动 CLI，与打包环境行为一致（避免卡在 input()）
    """
    if getattr(sys, "frozen", False):
        exe_path = sys.executable
        return f'"{exe_path}" --cli --auto-close'
    app_py = os.path.join(PROJECT_DIR, "gui", "app.py")
    return f'"{sys.executable}" "{app_py}" --cli --auto-close'


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
        # SEC-003: 不暴露原始异常细节给前端，仅返回通用错误消息；原始异常已通过 logger.exception 记录
        return {"exists": False, "enabled": False, "time": fallback_time, "error": "查询计划任务失败，详情见日志"}


def create_schedule(schedule_time: str) -> dict[str, Any]:
    """创建每日自动执行的 Windows 计划任务。"""
    _, err, err_cat = validate_settings({"schedule_time": schedule_time})
    if err:
        return {"error": err, "error_category": err_cat or "system"}

    # CONC-006: 串行化 check-then-act（query_schedule 检查 → schtasks /create 创建），
    # 避免并发 create_schedule 调用都通过检查后都执行 /create /f 覆盖。
    with _schedule_lock:
        # 覆盖前先检查：若任务已存在、时间相同且处于启用状态，则跳过 schtasks /create，
        # 避免重复注册、避免误清用户在外部做的修改；前端据此不弹"计划任务已创建"提示。
        current = query_schedule()
        if current.get("exists") and current.get("enabled") and current.get("time") == schedule_time:
            return {"ok": True, "unchanged": True}

        cli_cmd = _get_cli_exe_path()
        task_cmd = cli_cmd

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
                except OSError:
                    # 计划任务已成功创建，仅配置文件保存失败：返回部分成功，避免用户误以为创建失败
                    # EH-011: logger.exception 保留 stack trace，便于定位 save_settings 内部失败位置
                    logger.exception("计划任务已创建但配置保存失败")
                    return {"ok": True, "msg": f"计划任务已创建（每天 {schedule_time} 执行），但配置文件保存失败，下次启动可能回退"}
                return {"ok": True, "msg": f"计划任务已创建，每天 {schedule_time} 执行"}
            else:
                logger.warning("schtasks create failed: %s", result.stderr.strip())
                return {"error": f"创建失败: {result.stderr.strip()}"}
        except Exception as e:
            logger.exception("Failed to create schedule")
            # SEC-003: 不暴露原始异常细节给前端，仅返回通用错误消息；原始异常已通过 logger.exception 记录
            return {"error": "创建计划任务失败，详情见日志"}


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
        # SEC-003: 不暴露原始异常细节给前端，仅返回通用错误消息；原始异常已通过 logger.exception 记录
        return {"error": "删除计划任务失败，详情见日志"}


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
    except Exception:
        # 辅助流程：清除密码失败不中断领取主流程，下次密码错误时仍会再次尝试清除
        # EH-011: logger.exception 保留 stack trace，便于定位 update_fields 内部失败位置
        logger.exception("_clear_account_password update_fields failed for %s", mask_phone(phone))


def init_client(account: dict, enable_relogin: bool = True) -> tuple[EtAlienClient | None, str]:
    """初始化客户端。不再预检查 token，token 有效性由 API 请求自动检测。

    Args:
        account: 账号字典
        enable_relogin: 是否注入 token 过期自动重登能力。由 claim_for_account 根据
            settings.auto_relogin 传入（关闭后 token 过期即抛 NeedLoginError，
            不再尝试密码重登）。

    Returns:
        (client, status): status 为 "ok" / "need_login"
        - "ok": client 已就绪（注入了重登能力，token 过期时由公共流程处理）
        - "need_login": 未登录（无 auth_token 或 device_id）
    """
    phone = account["phone"]
    client = get_client_for_account(account, enable_relogin=enable_relogin)
    if client and client.is_logged_in():
        logger.info("  [%s] 已登录，token 有效性由首个 API 请求检测", mask_phone(phone))
        return client, "ok"
    logger.info("  [%s] 未找到登录状态", mask_phone(phone))
    return None, "need_login"


def _make_claim_result(phone: str, label: str, status: str,
                       vip_before: int = 0, vip_after: int | None = None,
                       mobile_before: int = 0, mobile_after: int | None = None,
                       claimed: int = 0, failed: int = 0,
                       claim_target: str = "all",
                       progress_entry: dict | None = None) -> dict:
    """构建领取结果字典，并同步更新 progress_entry（如提供）。

    用于 need_login / network_error 等简单路径的结果构建，
    同时将状态信息写入 progress_entry 供前端进度查询。

    vip_after/mobile_after 允许 None：异常路径下未查询到时回退到 before，
    序列化到 result dict 时若仍为 None 则回退为 0（保留前端兼容性）。
    """
    vip_after_ser = vip_after if vip_after is not None else 0
    mobile_after_ser = mobile_after if mobile_after is not None else 0
    result = {
        "phone": phone,
        "label": label,
        "status": status,
        "claim_target": claim_target,
        "vip_before": vip_before,
        "vip_after": vip_after_ser,
        "mobile_before": mobile_before,
        "mobile_after": mobile_after_ser,
        "claimed": claimed,
        "failed": failed,
    }
    if progress_entry is not None:
        # 首次进入问题态时写入 firstSeenTs；network_error 刷新 lastReqTs
        if status in PROBLEM_STATUSES and not progress_entry.get('firstSeenTs'):
            progress_entry['firstSeenTs'] = time.time()
        if status == 'network_error':
            progress_entry['lastReqTs'] = time.time()
        progress_update: dict = {
            "status": status,
            "vip_before": vip_before, "vip_after": vip_after_ser,
            "mobile_before": mobile_before, "mobile_after": mobile_after_ser,
        }
        # BF-012: 异常路径下显式 current = total，表示已停止
        if status in ("need_login", "network_error", "error"):
            progress_update["current"] = progress_entry.get("total", 0)
        progress_entry.update(progress_update)
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
                    mask_phone(phone), unwatched_before,
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
            logger.info("  [%s] PC第%d轮 error: %s", mask_phone(phone), round_num, backup_result.get("msg", ""))
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
                            mask_phone(phone), round_num, local_success, progress_entry["current"], tot)
        else:
            logger.info("  [%s] PC第%d轮 is_verify=False", mask_phone(phone), round_num)
            total_failed += 1
            consecutive_fail += 1

        if consecutive_fail >= 3:
            logger.info("  [%s] 连续%d次补发失败，停止", mask_phone(phone), consecutive_fail)
            break

        # 已领够则不再 sleep，避免最后一轮成功后多等 interval 秒
        if local_success >= unwatched_before:
            break

        time.sleep(interval)

    # 结束查1次config校准真实领取数
    # EH-001: 包 try/except RequestException，网络异常时与 _error=True 走相同分支（用本地计数），
    # 避免异常上抛导致 claim_for_account 把已领取部分错误标记为 network_error
    try:
        final_result = client.fetch_pc_ad_config()
    except requests.RequestException:
        logger.warning("  [%s] 结束校准查询网络异常，使用本地计数 %d", mask_phone(phone), local_success)
        return local_success, total_failed
    if final_result.get("_error"):
        logger.warning("  [%s] 结束校准查询失败，使用本地计数 %d", mask_phone(phone), local_success)
        return local_success, total_failed
    final_unwatched = get_unwatched_count(final_result.get("list", []))
    real_claimed = unwatched_before - final_unwatched
    if real_claimed < 0:
        real_claimed = 0
    if final_unwatched > 0 and local_success >= unwatched_before:
        logger.warning("  [%s] 本地计数%d已领够但服务端仍有%d未完成，可能存在异常",
                       mask_phone(phone), local_success, final_unwatched)
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
                    mask_phone(phone), pending_count,
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
            logger.info("  [%s] 手机第%d轮 error: %s", mask_phone(phone), round_num, backup_result.get("msg", ""))
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
                            mask_phone(phone), round_num, local_success, progress_entry["current"], tot)
        else:
            logger.info("  [%s] 手机第%d轮 is_verify=False", mask_phone(phone), round_num)
            total_failed += 1
            consecutive_fail += 1

        if consecutive_fail >= 3:
            logger.info("  [%s] 手机端连续%d次补发失败，停止", mask_phone(phone), consecutive_fail)
            break

        # 已领够则不再 sleep，避免最后一轮成功后多等 interval 秒
        if local_success >= pending_count:
            break

        time.sleep(interval)

    # 结束查1次activity校准真实领取数（用 user_watch_cnt 差值，因含无奖励任务）
    # EH-001: 包 try/except RequestException，网络异常时与 _error=True 走相同分支（用本地计数），
    # 避免异常上抛导致 claim_for_account 把已领取部分错误标记为 network_error
    try:
        final_activity = client.fetch_mobile_ad_activity()
    except requests.RequestException:
        logger.warning("  [%s] 手机端结束校准查询网络异常，使用本地计数 %d", mask_phone(phone), local_success)
        return local_success, total_failed
    if final_activity.get("_error"):
        logger.warning("  [%s] 手机端结束校准查询失败，使用本地计数 %d", mask_phone(phone), local_success)
        return local_success, total_failed
    final_watch = final_activity.get("user_watch_cnt", watched_before)
    real_claimed = final_watch - watched_before
    if real_claimed < 0:
        real_claimed = 0
    target = watched_before + pending_count
    if final_watch < target and local_success >= pending_count:
        logger.warning("  [%s] 手机端本地计数%d已领够但服务端仅%d/%d，可能存在异常",
                       mask_phone(phone), local_success, final_watch, target)
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
    """
    phone = account["phone"]
    label = account_label(account)
    interval = settings.get("request_interval", 1.0)
    max_rounds = settings.get("max_rounds", 21)
    mob_max_rounds = settings.get("mobile_max_rounds", 7)
    # claim_target: "all" / "pc" / "mobile"，默认 "all"（旧账号兼容）
    claim_target = account.get("claim_target", "all")

    # EH-013: init_client 调用包入 try/except，捕获 EtAlienClient 构造或 load_settings 异常，
    # 避免向上抛到 run_concurrent_claim 被标记为 "error"
    try:
        # auto_relogin 开关：关闭后不注入密码重登能力，token 过期即抛 NeedLoginError
        client, init_status = init_client(account, enable_relogin=settings.get("auto_relogin", True))
    except Exception:
        logger.exception("[%s] init_client failed", mask_phone(phone))
        return _make_claim_result(phone, label, "error",
                                  claim_target=claim_target,
                                  progress_entry=progress_entry)

    # init_client 不再返回 "network_error"，仅 "ok" / "need_login"
    if not client:
        return _make_claim_result(phone, label, init_status,
                                  claim_target=claim_target,
                                  progress_entry=progress_entry)

    # 累计两阶段的统计
    total_claimed = 0
    total_failed = 0
    vip_before = 0
    vip_after: int | None = None  # BF-001: None 表示未查询到，异常路径回退到 vip_before
    mobile_before = 0
    mobile_after: int | None = None  # BF-001: None 表示未查询到，异常路径回退到 mobile_before
    phase_results: list[str] = []  # 每个阶段的 status

    try:
        # ============ PC 阶段 ============
        if claim_target in ("pc", "all"):
            if progress_entry is not None:
                progress_entry["phase"] = "pc"
                progress_entry["current"] = 0
                progress_entry["total"] = 0

            # PC 阶段初始状态：并发查询 ad_config + duration（两者无依赖）
            ad_result_ref: list = [None]
            ad_exc: list = [None]
            dur_before_ref: list = [None]
            dur_exc: list = [None]

            def _fetch_pc_ad():
                try:
                    ad_result_ref[0] = client.fetch_pc_ad_config()
                except Exception as e:
                    # EH-009: 线程内异常记 debug 日志保留原始 stack trace
                    logger.debug("  [%s] _fetch_pc_ad thread exception: %s", mask_phone(phone), e, exc_info=True)
                    ad_exc[0] = e

            def _fetch_pc_dur():
                try:
                    dur_before_ref[0] = client.fetch_pc_duration()
                except Exception as e:
                    # EH-009: 线程内异常记 debug 日志保留原始 stack trace
                    logger.debug("  [%s] _fetch_pc_dur thread exception: %s", mask_phone(phone), e, exc_info=True)
                    dur_exc[0] = e

            t_ad = Thread(target=_fetch_pc_ad)
            t_dur = Thread(target=_fetch_pc_dur)
            t_ad.start()
            t_dur.start()
            # EH-005: join timeout 根据 max_retries / retry_delay 动态计算
            # 单接口最坏耗时 (max_retries+1)*(30+retry_delay)，+5s 冗余
            pc_max_retries = settings.get("max_retries", 3)
            pc_retry_delay = settings.get("retry_delay", 1.0)
            pc_join_timeout = (max(pc_max_retries, 0) + 1) * (30 + max(pc_retry_delay, 0)) + 5
            t_ad.join(timeout=pc_join_timeout)
            t_dur.join(timeout=pc_join_timeout)
            if t_ad.is_alive():
                logger.warning("  [%s] fetch_pc_ad_config join 超时", mask_phone(phone))
                ad_exc[0] = requests.Timeout("fetch_pc_ad_config join timeout")
            if t_dur.is_alive():
                logger.warning("  [%s] fetch_pc_duration join 超时", mask_phone(phone))
                dur_exc[0] = requests.Timeout("fetch_pc_duration join timeout")

            # 检查 NeedLoginError / RequestException 优先上抛（保持原有异常语义）
            if isinstance(ad_exc[0], NeedLoginError):
                raise ad_exc[0]
            if isinstance(dur_exc[0], NeedLoginError):
                raise dur_exc[0]
            if isinstance(ad_exc[0], requests.RequestException):
                raise ad_exc[0]
            if isinstance(dur_exc[0], requests.RequestException):
                raise dur_exc[0]

            ad_result = ad_result_ref[0]
            dur_before = dur_before_ref[0]

            if ad_exc[0]:
                ad_result = {"_error": True, "msg": str(ad_exc[0])}
            if dur_exc[0]:
                dur_before = {"_error": True, "msg": str(dur_exc[0])}

            if ad_result is None:
                ad_result = {"_error": True}
            if dur_before is None:
                dur_before = {"_error": True}

            if ad_result.get("_error"):
                logger.warning("  [%s] 获取PC任务失败: %s", mask_phone(phone), ad_result.get('msg'))
                # BF-002: PC 任务查询失败但时长查询成功时保留 vip_before/vip_after
                if not dur_before.get("_error"):
                    vip_before = dur_before.get("vip_duration_second", 0)
                vip_after = vip_before  # 未领取，前后一致（dur_before._error 时为 0）
                phase_results.append("error")
            else:
                tasks = ad_result.get("list", [])
                total_unwatched = get_unwatched_count(tasks)
                total_items = sum(len(level["items"]) for level in tasks)

                vip_before = dur_before.get("vip_duration_second", 0) if not dur_before.get("_error") else 0

                if total_unwatched == 0:
                    logger.info("  [%s] PC所有任务已完成, VIP: %s", mask_phone(phone), format_duration(vip_before))
                    vip_after = vip_before
                    if progress_entry is not None:
                        progress_entry["current"] = 0
                        progress_entry["total"] = total_items
                    phase_results.append("already_done")
                else:
                    logger.info("  [%s] PC开始领取, 待完成: %s, VIP: %s",
                                mask_phone(phone), total_unwatched, format_duration(vip_before))
                    if progress_entry is not None:
                        progress_entry["vip_before"] = vip_before
                        if progress_entry.get("total", 0) == 0 and total_items > 0:
                            progress_entry["total"] = total_items

                    pc_claimed, pc_failed = _claim_pc_phase(client, phone, interval, max_rounds, total_unwatched, progress_entry)
                    total_claimed += pc_claimed
                    total_failed += pc_failed

                    # EH-001: 结束校准查询外包 try/except RequestException，网络异常时回退到 vip_before，
                    # 确保 phase_results.append 仍能执行（不被网络异常打断标记为 network_error）
                    try:
                        dur_after = client.fetch_pc_duration()
                        vip_after = dur_after.get("vip_duration_second", 0) if not dur_after.get("_error") else vip_before
                    except requests.RequestException:
                        vip_after = vip_before  # 网络异常，前后一致
                    logger.info("  [%s] PC领取完成, VIP: %s -> %s (+%s)",
                                mask_phone(phone), format_duration(vip_before), format_duration(vip_after),
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

            # Mobile 阶段初始状态：并发查询 activity + profile（两者无依赖）
            act_result_ref: list = [None]
            act_exc: list = [None]
            prof_result_ref: list = [None]
            prof_exc: list = [None]

            def _fetch_mobile_act():
                try:
                    act_result_ref[0] = client.fetch_mobile_ad_activity()
                except Exception as e:
                    # EH-009: 线程内异常记 debug 日志保留原始 stack trace
                    logger.debug("  [%s] _fetch_mobile_act thread exception: %s", mask_phone(phone), e, exc_info=True)
                    act_exc[0] = e

            def _fetch_mobile_prof():
                try:
                    prof_result_ref[0] = client.fetch_mobile_profile()
                except Exception as e:
                    # EH-009: 线程内异常记 debug 日志保留原始 stack trace
                    logger.debug("  [%s] _fetch_mobile_prof thread exception: %s", mask_phone(phone), e, exc_info=True)
                    prof_exc[0] = e

            t_act = Thread(target=_fetch_mobile_act)
            t_prof = Thread(target=_fetch_mobile_prof)
            t_act.start()
            t_prof.start()
            # EH-005: join timeout 根据 max_retries / retry_delay 动态计算
            # 单接口最坏耗时 (max_retries+1)*(30+retry_delay)，+5s 冗余
            mob_max_retries = settings.get("max_retries", 3)
            mob_retry_delay = settings.get("retry_delay", 1.0)
            mob_join_timeout = (max(mob_max_retries, 0) + 1) * (30 + max(mob_retry_delay, 0)) + 5
            t_act.join(timeout=mob_join_timeout)
            t_prof.join(timeout=mob_join_timeout)
            if t_act.is_alive():
                logger.warning("  [%s] fetch_mobile_ad_activity join 超时", mask_phone(phone))
                act_exc[0] = requests.Timeout("fetch_mobile_ad_activity join timeout")
            if t_prof.is_alive():
                logger.warning("  [%s] fetch_mobile_profile join 超时", mask_phone(phone))
                prof_exc[0] = requests.Timeout("fetch_mobile_profile join timeout")

            # 检查 NeedLoginError / RequestException 优先上抛（保持原有异常语义）
            if isinstance(act_exc[0], NeedLoginError):
                raise act_exc[0]
            if isinstance(prof_exc[0], NeedLoginError):
                raise prof_exc[0]
            if isinstance(act_exc[0], requests.RequestException):
                raise act_exc[0]
            if isinstance(prof_exc[0], requests.RequestException):
                raise prof_exc[0]

            activity = act_result_ref[0]
            profile_before = prof_result_ref[0]

            if act_exc[0]:
                activity = {"_error": True, "msg": str(act_exc[0])}
            if prof_exc[0]:
                profile_before = {"_error": True, "msg": str(prof_exc[0])}

            if activity is None:
                activity = {"_error": True}
            if profile_before is None:
                profile_before = {"_error": True}

            if activity.get("_error"):
                logger.warning("  [%s] 获取手机端任务失败: %s", mask_phone(phone), activity.get('msg'))
                # BF-002: 手机端任务查询失败但 profile 查询成功时保留 mobile_before/mobile_after
                if not profile_before.get("_error"):
                    mobile_before = profile_before.get("remaining_seconds", 0)
                mobile_after = mobile_before  # 未领取，前后一致（profile._error 时为 0）
                phase_results.append("error")
            else:
                # 手机端任务需按顺序领取，无奖励任务也必须领取才能解锁后续有奖励任务
                # 因此 pending 基于总任务数 video_cnt 和已观看数 user_watch_cnt 计算
                video_cnt = activity.get("video_cnt", 0)
                watched_before = activity.get("user_watch_cnt", 0)

                mobile_before = profile_before.get("remaining_seconds", 0) if not profile_before.get("_error") else 0

                if video_cnt == 0 or watched_before >= video_cnt:
                    logger.info("  [%s] 手机端所有任务已完成, 加速时长: %s",
                                mask_phone(phone), format_duration(mobile_before))
                    mobile_after = mobile_before
                    if progress_entry is not None:
                        progress_entry["current"] = 0
                        progress_entry["total"] = video_cnt
                    phase_results.append("already_done")
                else:
                    logger.info("  [%s] 手机端开始领取, 已观看: %s/%s, 加速时长: %s",
                                mask_phone(phone), watched_before, video_cnt, format_duration(mobile_before))
                    if progress_entry is not None:
                        progress_entry["mobile_before"] = mobile_before
                        if progress_entry.get("total", 0) == 0 and video_cnt > 0:
                            progress_entry["total"] = video_cnt

                    pending = video_cnt - watched_before
                    mob_claimed, mob_failed = _claim_mobile_phase(client, phone, interval, mob_max_rounds, pending, watched_before, progress_entry)
                    total_claimed += mob_claimed
                    total_failed += mob_failed

                    # EH-001: 结束校准查询外包 try/except RequestException，网络异常时回退到 mobile_before，
                    # 确保 phase_results.append 仍能执行（不被网络异常打断标记为 network_error）
                    try:
                        profile_after = client.fetch_mobile_profile()
                        mobile_after = profile_after.get("remaining_seconds", mobile_before) if not profile_after.get("_error") else mobile_before
                    except requests.RequestException:
                        mobile_after = mobile_before  # 网络异常，前后一致
                    logger.info("  [%s] 手机端领取完成, 加速时长: %s -> %s (+%s)",
                                mask_phone(phone), format_duration(mobile_before), format_duration(mobile_after),
                                format_duration(mobile_after - mobile_before))
                    phase_results.append(_calculate_final_status(mob_claimed, mob_failed))

    except NeedLoginError as e:
        # token 过期且无法重登（无密码/密码错误/已重登过仍失败）
        logger.info("[%s] 领取中 token 失效且无法重登: %s", mask_phone(phone), e)
        # BF-001: mobile_after/vip_after 未被赋值时（异常路径）回退到 before，避免显示 0
        # 注意：必须用 is not None 判断，=0 是合法的"0秒时长"，falsy 判断会误回退
        final_mobile_after = mobile_after if mobile_after is not None else mobile_before
        final_vip_after = vip_after if vip_after is not None else vip_before
        result = _make_claim_result(
            phone, label, "need_login",
            vip_before=vip_before,
            vip_after=final_vip_after,
            mobile_before=mobile_before, mobile_after=final_mobile_after,
            claim_target=claim_target,
            progress_entry=progress_entry,
        )
        return result
    except requests.RequestException as e:
        # 网络异常（重试 max_retries 次后仍失败）
        logger.warning("[%s] 领取中网络异常: %s", mask_phone(phone), e)
        # BF-001: mobile_after/vip_after 未被赋值时（异常路径）回退到 before，避免显示 0
        # 注意：必须用 is not None 判断，=0 是合法的"0秒时长"，falsy 判断会误回退
        final_mobile_after = mobile_after if mobile_after is not None else mobile_before
        final_vip_after = vip_after if vip_after is not None else vip_before
        result = _make_claim_result(
            phone, label, "network_error",
            vip_before=vip_before,
            vip_after=final_vip_after,
            mobile_before=mobile_before, mobile_after=final_mobile_after,
            claim_target=claim_target,
            progress_entry=progress_entry,
        )
        return result
    finally:
        # 显式释放 client 的 requests.Session 连接池（client 非 None 时才进入此块）
        if client:
            client.close()

    # ============ 汇总 ============
    # 整体状态综合判定（规则见 _combine_phase_status）
    # EH-12: 空列表通常表示 claim_target 配置异常（既不是 "pc" 也不是 "mobile"），
    # 加上下文日志便于诊断"配置异常"与"实际领取全部失败"的区别
    if not phase_results:
        logger.warning("[%s] claim_target=%s 未执行任何阶段（phase_results 为空）",
                       mask_phone(phone), claim_target)
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
    }
    if progress_entry is not None:
        # 首次进入问题态时写入 firstSeenTs；network_error 刷新 lastReqTs
        if final_status in PROBLEM_STATUSES and not progress_entry.get('firstSeenTs'):
            progress_entry['firstSeenTs'] = time.time()
        if final_status == 'network_error':
            progress_entry['lastReqTs'] = time.time()
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

    并发说明（CONC-002/003）：
    - progress_entry 在 claim_for_account 内部不持 claim_mgr._lock 直接修改字段，
      ClaimManager.get_progress() 浅拷贝期间可能读到"部分新值部分旧值"的弱一致快照
      （如 current=5/total=3 瞬间态）。此为有意设计权衡：避免 deepcopy 在万级账号下
      持锁 50-100ms 阻塞 get_progress。仅影响 UI 瞬间显示，不影响业务正确性。
    - get_account_status/claim_for_account 内部用 Thread 并发查询 2 接口，join(timeout=130)
      超时后线程仍在后台运行。client.close() 关闭 session 连接池后，后台线程的请求
      会继续完成（依赖 requests 内部实现），异常被线程内 try/except 捕获，不影响主流程。
    """
    max_concurrent = settings.get("max_concurrent", 10)
    results = []

    # CONC-003: 总超时避免单个慢账号拖延整个批次（原 as_completed 无 timeout 会无限阻塞）
    # 单账号最坏耗时 ≈ (max_rounds + mobile_max_rounds) * (request_interval + 30s 网络超时) + 60s 重试冗余
    # 总超时 = 单账号最坏耗时 * 批次轮数（向上取整）+ 60s 冗余
    max_rounds = settings.get("max_rounds", 21)
    mob_max_rounds = settings.get("mobile_max_rounds", 7)
    interval = settings.get("request_interval", 1.0)
    per_account_worst = (max_rounds + mob_max_rounds) * (interval + 30) + 60
    batch_rounds = (len(accounts) + max_concurrent - 1) // max_concurrent
    total_timeout = batch_rounds * per_account_worst + 60

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
                    "pc_claimed": 0,
                }
                claim_mgr.add_progress_entry(entry)

            futures[executor.submit(claim_for_account, account, settings, entry)] = account

        # CONC-003: as_completed 传 timeout 防止无限阻塞
        processed_phones: set[str] = set()
        try:
            for future in as_completed(futures, timeout=total_timeout):
                account = futures[future]
                processed_phones.add(account["phone"])
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    # 防御性兜底：理论上 claim_for_account 内部已 try/except 全部异常，
                    # 不会向上抛出。保留此分支以防未来 claim_for_account 改为抛异常。
                    logger.exception("Claim failed for %s", mask_phone(account['phone']))
                    # EH-002: 上一行 logger.exception 已带 stack trace，此处仅补充异常消息
                    logger.info("  [%s] 异常: %s", mask_phone(account['phone']), e)
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
                        # SEC-003: 不暴露原始异常细节给前端，仅返回通用错误消息；原始异常已通过 logger.exception 记录
                        claim_mgr.update_progress_entry(account["phone"], {"status": "error", "error": "领取失败，详情见日志"})
        except TimeoutError:
            # CONC-003: 总超时到期，未完成的 future 取消并填占位
            # done() 的 future 仍可提取结果（as_completed 抛 TimeoutError 瞬间可能有 future 已完成但未被 yield）
            for future, account in futures.items():
                if account["phone"] in processed_phones:
                    continue
                if future.done():
                    try:
                        results.append(future.result())
                        continue
                    except Exception as e:
                        logger.exception("Claim failed for %s", mask_phone(account['phone']))
                        error_msg = "领取失败，详情见日志"
                else:
                    future.cancel()
                    logger.warning("[%s] 领取超时（total_timeout=%ss）", mask_phone(account['phone']), total_timeout)
                    error_msg = "领取超时"
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
                    claim_mgr.update_progress_entry(account["phone"], {"status": "error", "error": error_msg})

    # 按 id 升序排序，保证 CLI 输出与通知顺序稳定
    phone_to_id = {a["phone"]: a.get("id", 0) for a in accounts}
    results.sort(key=lambda r: phone_to_id.get(r.get("phone", ""), 0), reverse=False)
    return results
