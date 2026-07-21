"""EtAlien 自动领取工具的 Flask API 服务层。

负责处理前端 HTTP 请求，包括账号管理、登录验证码/密码校验、
领取任务启停与进度查询、设置读写、Windows 计划任务管理。
"""

import logging
import os
import re
import secrets
import sys
import threading
import time

from typing import Any
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request, send_from_directory

from core.client import NeedLoginError
from core.config import load_settings, save_settings, reload_settings, validate_settings, VERSION, SETTINGS_SCHEMA
from core.db import DbAccountRepository
from core.privacy import mask_phone
from core.service import validate_phone, run_concurrent_claim, send_login_code, verify_login, batch_get_account_status, get_account_mobile_status, query_schedule, create_schedule, delete_schedule, batch_update_accounts, get_enabled_accounts_by_phones, PROBLEM_STATUSES

logger = logging.getLogger(__name__)

# 模块级 Repository 单例（构造时仅记录路径，连接延迟到首次查询时建立）
repo = DbAccountRepository()

# 敏感字段过滤列表：禁止下发给前端，防止 auth_token / user_id / saved_at 泄露。
# 注意：password 不在过滤列表中，因为编辑账号弹窗需要回填密码供用户查看/修改。
# 列表接口 /api/accounts 使用 BASE_FIELDS 白名单，本身不含 password，不会泄露。
SENSITIVE_FIELDS = ("auth_token", "user_id", "saved_at")

# 基础字段白名单（只返回这些字段给前端，过滤敏感字段与状态字段）
BASE_FIELDS = ("phone", "name", "remark", "enabled", "claim_target")

# 密码格式校验正则：6-20 位字母/数字/下划线/点号（与前端 PWD_RE 一致）
PWD_RE = re.compile(r'^[A-Za-z0-9_.]{6,20}$')

# SEC-009: name / remark 字段长度上限校验（与前端 maxlength 一致）
# 防止超长字符串导致 SQLite 存储膨胀 / 前端渲染错乱 / 日志膨胀
NAME_MAX_LEN = 32
REMARK_MAX_LEN = 64


def error_response(msg: str, code: int = 400):
    """统一的错误响应 helper：返回 {"ok": False, "error": msg} + HTTP code。"""
    return jsonify({"ok": False, "error": msg}), code


def _translate_login_error(result: dict, scene: str) -> str:
    """转译登录错误为用户友好提示。

    服务端对字段校验失败返回 code=1 + 英文技术栈 msg（如
    "InvalidArgument: BINDING: Key: 'XXXRequest.xxx' Error:Field validation
    for 'xxx' failed on the 'len' tag"），直接透传给用户无法理解，需按场景转译。
    其他错误（code=1000/1001/500 等）服务端已返回友好中文，直接透传。

    SEC-006: 真正未知 code（非 1/60/500/1000/1001 等已知业务错误）的 msg 可能含
    技术栈细节（如 gRPC 字段名、错误码体系），不透传给用户。仅记录到日志便于
    排查，响应给用户时返回通用提示「操作失败」。

    Args:
        result: core.service 的 send_login_code/verify_login 返回的 result dict
        scene: "send_code" / "verify_code" / "verify_password"

    Returns:
        友好的错误提示文案
    """
    if not result.get("_error"):
        return ""
    code = result.get("code")
    msg = result.get("msg", "")
    if code == 1:
        if scene == "send_code":
            return "手机号格式不正确，请输入11位手机号"
        elif scene == "verify_code":
            return "验证码必须是6位数字"
        elif scene == "verify_password":
            return "密码格式不正确，必须是6-20位字母、数字、下划线或点号"
        # code=1 + 未知 scene：透传 msg（已是已知业务错误，scene 未覆盖时由调用方提示）
        return msg or "操作失败"
    # 已知友好中文 msg 直接透传（服务端 code=60/500/1000/1001 等业务错误）
    if code in (60, 500, 1000, 1001):
        return msg or "操作失败"
    # 真正未知 code：msg 可能含技术栈细节，仅记录日志，不透传给用户
    if msg:
        logger.warning("Unknown login error code=%s msg=%s", code, msg)
    return "操作失败"

if getattr(sys, 'frozen', False):
    STATIC_DIR = os.path.join(sys._MEIPASS, 'gui', 'static')
else:
    STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = Flask(__name__, static_folder=STATIC_DIR)
# 关闭 jsonify 的键字母排序，保留 Python dict 插入顺序。
# 前端高级区按 schema 字段定义顺序渲染（如先 PC 轮数后手机轮数），
# sort_keys=True 会破坏 SETTINGS_SCHEMA 的业务分组顺序。
app.json.sort_keys = False
# SEC-003: 限制请求体大小为 256KB，覆盖最大合法请求（批量操作 1000 个手机号约 64KB）。
# 超出时 Flask 自动返回 413 Payload Too Large，防止攻击者发送超大 JSON 占用内存。
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024


class ClaimManager:
    """线程安全的领取进度管理器。

    用于追踪并发领取任务的运行状态、各账号进度条目，以及 PC/手机端总进度汇总。
    """

    def __init__(self):
        """初始化锁、运行标记和进度字典。"""
        self._lock = threading.Lock()
        self._running = False
        self._progress: dict[str, dict[str, Any]] = {}  # P9: dict[str, dict]，key 为 phone
        self._last_progress: list[dict[str, Any]] = []  # R43: finish 浅拷贝结果，list[dict]

    @property
    def running(self) -> bool:
        """线程安全地返回当前是否正在领取。"""
        with self._lock:
            return self._running

    def start(self) -> bool:
        """尝试启动领取。

        Returns:
            若当前未运行则启动并返回 True；若已在运行中则返回 False。
        """
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._progress = {}  # P9/R43: 重置为空 dict
            # 清空上次结果快照：开始新一次领取后，「上次结果」语义上不再存在，
            # 避免新领取期间 /api/claim/last-result 返回上次结果误导用户。
            self._last_progress = []
            return True

    def finish(self) -> None:
        """标记当前领取任务结束，并将 _progress 浅拷贝保存为上次结果快照。

        R43 与 P9 dict 改造一致：_progress 是 dict[str, dict]，浅拷贝其 values()。
        避免 deepcopy 在 1 万规模下持锁 50-100ms 阻塞 get_progress。
        """
        with self._lock:
            self._last_progress = [dict(e) for e in self._progress.values()]
            self._running = False
            self._progress = {}  # R43/P9: 重置为空 dict

    def get_progress(self) -> dict:
        """返回当前进度快照。

        Returns:
            dict: 包含 running（布尔）、progress（各账号条目列表的深拷贝）。
        """
        with self._lock:
            # R19/P10: 先快照 keys 避免 iterate 期间并发 add 新增 key 触发 RuntimeError
            keys = list(self._progress.keys())
            return {
                "running": self._running,
                # 浅拷贝每个 dict：progress 条目字段全是基本类型（str/int/float），
                # 无嵌套 list/dict，浅拷贝比 deepcopy 快 5-10 倍且语义等价。
                # 持锁期间快照，释放锁后后端 update 修改的是原 dict（已不在快照中）。
                "progress": [dict(self._progress[k]) for k in keys],
            }

    def get_last_progress(self) -> dict:
        """持锁返回上次结果快照的浅拷贝。

        与 get_progress 实现一致：避免返回原 dict 引用被外部修改。
        从未领取过时 _last_progress 为 []，progress 返回空列表。
        """
        with self._lock:
            return {
                "running": self._running,
                "progress": [dict(e) for e in self._last_progress],
            }

    def add_progress_entry(self, entry: dict[str, Any]) -> None:
        """添加新账号的进度条目。

        R30/P10: 预填 firstSeenTs/lastReqTs/error 占位 None，
        避免 service.py L778/L1308 update 新增 key 触发
        get_progress / finish() 浅拷贝 iterate 时的 RuntimeError
        （dict size 变化是 RuntimeError 触发条件，预填后只改 value 不增 key）。
        兜底：直接以问题态 add 时（如 network_error 首次 add）写入 firstSeenTs/lastReqTs。
        """
        with self._lock:
            entry.setdefault('firstSeenTs', None)
            entry.setdefault('lastReqTs', None)
            entry.setdefault('error', None)
            if entry.get('status') in PROBLEM_STATUSES and not entry.get('firstSeenTs'):
                entry['firstSeenTs'] = time.time()
            if entry.get('status') == 'network_error':
                entry['lastReqTs'] = time.time()
            self._progress[entry["phone"]] = entry  # P9: dict[key]=value 替代 list.append

    def update_progress_entry(self, phone: str, updates: dict[str, Any]) -> None:
        """按手机号更新已有进度条目。

        P9: O(1) 查找替代 for 循环 O(N) 线性扫描。
        异常兜底路径同样写入 firstSeenTs/lastReqTs（与 add 一致）。
        """
        with self._lock:
            entry = self._progress.get(phone)
            if entry is not None:
                entry.update(updates)
                status = entry.get('status')
                if status in PROBLEM_STATUSES and not entry.get('firstSeenTs'):
                    entry['firstSeenTs'] = time.time()
                if status == 'network_error':
                    entry['lastReqTs'] = time.time()

    def reset(self) -> None:
        """重置内部状态（running/progress/last_progress），仅供测试使用。

        TEST-007: 替代测试中白盒赋值 claim_mgr._running = False / _progress = {} /
        _last_progress = [] 的脆弱模式，避免后续 ClaimManager 内部字段重命名时
        需同步修改多处测试代码。持锁以与 start/finish 保持一致的并发语义。
        """
        with self._lock:
            self._running = False
            self._progress = {}
            self._last_progress = []


claim_mgr = ClaimManager()

# CLI 触发领取的认证 token（由 gui/app.py 在写入 .gui_port 时生成并设置）。
# CLI 请求 /api/cli-trigger-claim 时需携带 Authorization: Bearer <token>，
# 防止本机其他进程未授权触发领取。
_cli_trigger_token: str | None = None

# GUI 实际监听端口（由 gui/app.py 在 make_server 成功后赋值）。
# GET /api/settings 返回此值供前端显示「当前:xxx」提示，与 settings.gui_port 配置值对比。
# 进程生命周期内不变；settings.gui_port 修改后需重启 GUI 才生效。
_actual_gui_port: int | None = None


@app.before_request
def _check_origin():
    """CSRF 防护：校验写请求来源（POST/PUT/DELETE/PATCH）。

    严格模式：写请求必须携带本机 Origin 头（127.0.0.1/localhost/::1），
    缺失或非本机来源一律拒绝（403），防止本机其他进程绕过 CSRF 防护。
    GET/HEAD 等安全请求不校验。

    豁免：/api/cli-trigger-claim 路径已有 Bearer Token 强校验（secrets.compare_digest），
    且 CLI 通过 Python requests 库调用（默认不发 Origin 头），故豁免 Origin 校验。

    SEC-004: 兜底校验收紧——主校验失败（netloc 不匹配）但 hostname 为本机时，
    额外校验 Origin 端口必须等于 GUI 实际监听端口 _actual_gui_port，避免本机任意
    端口运行的页面绕过 CSRF 校验。_actual_gui_port 在 gui/app.py make_server
    成功后赋值；若未设置（启动早期 / 异常路径），不放宽兜底校验，直接拒绝。
    """
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return None
    # CLI 触发领取：Bearer Token 已强校验，requests 库默认不发 Origin 头
    if request.path == "/api/cli-trigger-claim":
        return None
    origin = request.headers.get("Origin")
    if not origin:
        logger.warning("Rejected write request without Origin header: %s %s", request.method, request.path)
        return error_response("非法请求来源", 403)
    parsed = urlparse(origin)
    # GUI-002: 主校验——Origin netloc 必须与 request.host 完全一致（hostname + port）。
    # request.host 形如 "127.0.0.1:8888"；netloc 包含 hostname:port，比对它可同时
    # 校验 hostname 与 port，防止本机其他端口的页面跨端口 CSRF。
    if parsed.netloc == request.host:
        return None
    # SEC-004: 兜底校验——主校验失败时（netloc 不匹配），再校验 hostname 是否为本机。
    # 必要性：若攻击者通过非浏览器工具伪造 Host 头使 request.host = "evil.com:8080"，
    # 主校验会放行同样伪造的 Origin；此时 hostname 白名单仍能拒绝非本机来源。
    # 注：浏览器不允许 JS 伪造 Host 头，实际 CSRF 场景中此攻击向量不可行。
    # 收紧：当 _actual_gui_port 已知时（GUI 启动后），额外校验 Origin 端口必须等于
    # GUI 实际监听端口，避免本机任意端口的页面绕过 CSRF。_actual_gui_port 未设置时
    # （启动早期 / 测试环境），退回原宽松行为：仅校验 hostname 白名单。
    if parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        if _actual_gui_port is None or parsed.port == _actual_gui_port:
            return None
        logger.warning(
            "Rejected local-origin request with mismatched port: origin=%s, actual_gui_port=%s",
            origin, _actual_gui_port,
        )
    logger.warning("Rejected request with origin: %s (host=%s)", origin, request.host)
    return error_response("非法请求来源", 403)


@app.before_request
def _check_json_body():
    """校验 POST/PUT 请求体是否为合法 JSON。"""
    if request.method in ("POST", "PUT") and request.content_type and "application/json" in request.content_type:
        if request.content_length and request.content_length > 0:
            if request.get_json(silent=True) is None:
                return error_response("请求体必须是有效的 JSON")


@app.after_request
def _set_security_headers(resp):
    """添加安全响应头：CSP / X-Frame-Options / X-Content-Type-Options。

    - CSP：允许本机资源和内联脚本（pywebview 桥接 + 现有 onclick 内联事件需要）
    - X-Frame-Options: DENY：禁止被任何页面 iframe 嵌套
    - X-Content-Type-Options: nosniff：禁止浏览器 MIME 嗅探

    SEC-005: CSP 收紧——当 _actual_gui_port 已知时，将 http://127.0.0.1:* / http://localhost:*
    通配替换为具体端口 http://127.0.0.1:<port> / http://localhost:<port>，避免本机任意
    端口的恶意 HTTP 服务向本应用注入脚本。_actual_gui_port 未设置时（启动早期 / 测试环境）
    退回原宽松行为（通配端口），不破坏启动流程与测试。

    已知技术债：script-src 'unsafe-inline' 削弱 XSS 防护。短期内保留以兼容
    index.html 内联 <script>（pywebview 桥接初始化）与内联 onclick 事件属性。
    长期应改为 nonce-based CSP + 移除所有内联事件属性，需前端重构单独立项。
    """
    # SEC-005: 已知实际端口时收紧 CSP 至具体端口，避免本机任意端口注入
    if _actual_gui_port is not None:
        local_src = f"http://127.0.0.1:{_actual_gui_port} http://localhost:{_actual_gui_port}"
        csp = (
            f"default-src 'self' {local_src}; "
            f"script-src 'self' 'unsafe-inline' {local_src}; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:;"
        )
    else:
        # 启动早期 / 测试环境：_actual_gui_port 未设置，保持原宽松行为
        csp = (
            "default-src 'self' http://127.0.0.1:* http://localhost:*; "
            "script-src 'self' 'unsafe-inline' http://127.0.0.1:* http://localhost:*; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:;"
        )
    resp.headers.setdefault('Content-Security-Policy', csp)
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    return resp


@app.before_request
def _validate_phone_param():
    """校验路由中的 phone 参数格式（中国大陆 11 位手机号）。

    前端 5 个登录入口已有 validatePhoneFmt 拦截，此钩子为 API 层防御性校验，
    防止绕过前端直接调 API 触发后端未知行为。
    """
    phone = request.view_args.get("phone") if request.view_args else None
    if phone is not None and not validate_phone(phone):
        return error_response("手机号格式不正确", 400)


@app.route("/")
def index():
    """返回前端首页。"""
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    """返回前端静态资源。"""
    return send_from_directory(STATIC_DIR, path)


@app.route("/api/accounts", methods=["GET"])
def get_accounts():
    """分页查询账号列表，仅返回基础字段（不含敏感字段、不含状态字段）。

    必传 offset/limit；未传任一返回 400；非数字值返回 400；数字型非法值走 clamp：
    - limit clamp 到 [1, 200]（limit=0 或负数 clamp 到 1；超上限 clamp 到 200）
    - offset 负数 clamp 到 0；offset >= total 时返回空 accounts 列表（total 仍正确返回）
    """
    # 必传校验
    if "offset" not in request.args or "limit" not in request.args:
        return error_response("缺少 offset/limit 参数", 400)

    # 非数字值返回 400
    try:
        offset = int(request.args.get("offset"))
        limit = int(request.args.get("limit"))
    except (TypeError, ValueError):
        return error_response("offset/limit 必须是整数", 400)

    # clamp
    if limit < 1:
        limit = 1
    elif limit > 200:
        limit = 200
    if offset < 0:
        offset = 0

    # 搜索态：q 非空时调 list_page_search 走 name/phone/remark 子串模糊匹配
    # 非搜索态：q 为空或不传时走 list_page 全体分页（行为同现状）
    q = request.args.get("q", "").strip()
    if q:
        # list_page_search 一次返回 (accounts, total, enabled_count)，
        # 合并了原 count_search_enabled 的全表 LIKE 扫描，避免重复扫表
        accounts, total, enabled = repo.list_page_search(offset, limit, q)
    else:
        accounts, total = repo.list_page(offset, limit)
        enabled = None
    filtered_accounts = [{k: v for k, v in acc.items() if k in BASE_FIELDS} for acc in accounts]
    resp = {
        "accounts": filtered_accounts,
        "total": total,
        "offset": offset,
        "limit": limit,
    }
    if enabled is not None:
        resp["enabled"] = enabled
    return jsonify(resp)


@app.route("/api/accounts/stats", methods=["GET"])
def get_accounts_stats():
    """返回账号统计数（顶部统计卡片用，避免前端遍历分页缓存导致数字不准）。

    纯本地 DB 查询，不依赖 status，瞬时返回。
    """
    return jsonify({
        "total": repo.count(),
        "enabled": repo.count_enabled(),
    })


@app.route("/api/accounts/<phone>", methods=["GET"])
def get_account(phone):
    """获取单个账号详情，过滤敏感字段后返回。"""
    account = repo.get(phone)
    if account is None:
        return error_response("账号不存在", 404)
    filtered = {k: v for k, v in account.items() if k not in SENSITIVE_FIELDS}
    return jsonify({"account": filtered})


@app.route("/api/accounts", methods=["POST"])
def add_account():
    """添加新账号。"""
    data = request.get_json(silent=True) or {}
    phone = data.get("phone", "").strip()
    if not phone:
        return error_response("手机号不能为空")

    if not validate_phone(phone):
        return error_response("手机号格式不正确")

    # 检查手机号是否已存在
    if repo.get(phone) is not None:
        return error_response("手机号已存在")

    # 新增账号默认值从 settings 读（default_claim_target / default_account_enabled）
    settings = load_settings()
    default_ct = settings.get("default_claim_target", "all")
    claim_target = data.get("claim_target", default_ct)
    # 非法值回退到默认值，防止前端传参异常导致领取阶段出错
    if claim_target not in ("all", "pc", "mobile"):
        claim_target = default_ct if default_ct in ("all", "pc", "mobile") else "all"
    default_enabled = settings.get("default_account_enabled", True)
    # SEC-009: name / remark 长度截断，防止超长字符串导致存储/渲染/日志膨胀
    name_val = str(data.get("name", "")).strip()[:NAME_MAX_LEN]
    remark_val = str(data.get("remark", "")).strip()[:REMARK_MAX_LEN]
    # get_or_create 与 update_fields 一并放入 try：若 update_fields 失败，
    # except 中调用 repo.delete(phone) 清理残留空壳账号，避免脏污数据
    try:
        repo.get_or_create(phone)
        repo.update_fields(
            phone,
            name=name_val,
            remark=remark_val,
            enabled=str(data.get("enabled", default_enabled)).lower() not in ("false", "0", "", "no"),
            claim_target=claim_target,
        )
    except Exception:
        logger.exception("添加账号失败")
        try:
            repo.delete(phone)
        except Exception:
            logger.exception("清理残留账号失败: %s", mask_phone(phone))
        return error_response("保存账号失败，请检查磁盘空间或文件权限", 500)
    return jsonify({"ok": True})


@app.route("/api/accounts/<phone>", methods=["PUT"])
def update_account(phone):
    """更新账号信息。检测到换号请求（phone 字段与原值不同）一律 400 拒绝。"""
    data = request.get_json(silent=True) or {}
    account = repo.get(phone)
    if account is None:
        return error_response("账号不存在", 404)

    updates = {}
    # 不允许修改手机号：检测到换号请求（phone 字段且与原 phone 不同）一律拒绝
    if "phone" in data and data["phone"] != phone:
        return error_response("不允许修改手机号，请删除后重新添加")

    if "name" in data:
        # SEC-009: name 长度截断到 NAME_MAX_LEN
        updates["name"] = str(data["name"]).strip()[:NAME_MAX_LEN]
    if "remark" in data:
        # SEC-009: remark 长度截断到 REMARK_MAX_LEN
        updates["remark"] = str(data["remark"]).strip()[:REMARK_MAX_LEN]
    if "enabled" in data:
        updates["enabled"] = str(data["enabled"]).lower() not in ("false", "0", "", "no")
    if "claim_target" in data:
        ct = data["claim_target"]
        updates["claim_target"] = ct if ct in ("all", "pc", "mobile") else "all"
    if "password" in data:
        pwd = data["password"]
        # 空串保持"清除密码"语义；非空需通过格式校验
        if pwd and not PWD_RE.match(pwd):
            return error_response("密码格式不正确，必须是6-20位字母、数字、下划线或点号")
        updates["password"] = pwd if pwd else None

    try:
        repo.update_fields(phone, **updates)
    except Exception:
        logger.exception("更新账号失败")
        return error_response("保存账号失败，请检查磁盘空间或文件权限", 500)
    return jsonify({"ok": True})


@app.route("/api/accounts/<phone>", methods=["DELETE"])
def delete_account(phone):
    """删除账号。"""
    try:
        repo.delete(phone)
    except Exception:
        logger.exception("删除账号失败")
        return error_response("删除账号失败，请检查磁盘空间或文件权限", 500)
    return jsonify({"ok": True})


@app.route("/api/accounts/batch", methods=["POST"])
def batch_accounts():
    """批量启用/禁用/删除账号。

    请求体：{"action": "enable"|"disable"|"delete", "phones": ["138...", ...]}
    - action 必传，phones 必传且为数组，上限 1000（防止误操作）
    - 单条失败不影响其他，返回 failed_phones 列表告知前端

    响应成功：{"ok": true, "affected": int, "failed_phones": ["139...", ...]}
    """
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    phones = data.get("phones")

    if not action or phones is None:
        return error_response("缺少 action/phones 参数", 400)
    if action not in ("enable", "disable", "delete"):
        return error_response("action 必须是 enable/disable/delete", 400)
    if not isinstance(phones, list):
        return error_response("phones 必须是数组", 400)
    # GUI-013: batch 上限 1000（写操作，长时间持锁风险高）；
    # /api/claim 上限 10000（只读查询）；/api/accounts/status clamp 到 50（每条 phone
    # 触发一次网络请求，50 上限兼顾响应速度与超时风险）。三处上限按操作类型差异化设计。
    if len(phones) > 1000:
        return error_response("phones 数量超过上限 1000", 400)

    try:
        result = batch_update_accounts(phones, action)
    except Exception:
        logger.exception("批量操作失败")
        return error_response("批量操作失败，请检查磁盘空间或文件权限", 500)
    if not result.get("ok"):
        return error_response(result.get("error", "批量操作失败"), 500)
    return jsonify(result)


@app.route("/api/accounts/<phone>/mobile_status", methods=["GET"])
def get_mobile_status(phone):
    """查询账号手机端状态（时长余额 + 任务进度）。

    预留给前端卡片翻转 UI 按需调用，避免卡片翻转时被迫触发全量刷新。
    参考 get_account_status 的结构，仅查询手机端。
    """
    account = repo.get(phone)
    if account is None:
        return error_response("账号不存在", 404)
    return jsonify({"status": get_account_mobile_status(account)})


@app.route("/api/login/<phone>", methods=["POST"])
def send_code(phone):
    """发送登录验证码。"""
    try:
        result = send_login_code(phone)
    except (requests.ConnectionError, requests.Timeout):
        return error_response("网络异常，请检查网络后重试", 503)
    except NeedLoginError:
        return error_response("登录态失效，请刷新页面后重试", 401)
    except Exception:
        logger.exception("Send code unexpected error for %s", mask_phone(phone))
        return error_response("发送验证码失败，请稍后重试", 500)

    if result.get("_error"):
        code = result.get("code")
        # 错误码60和1000表示触发验证码请求冷却，验证码可能已发送，仍允许用户输入已收到的验证码
        # 返回 ok=False + cooldown=True 让前端展示冷却提示而非成功消息（BF-007）
        if code in (60, 1000):
            return jsonify({"ok": False, "error": "验证码请求冷却中，请稍后再试", "cooldown": True})
        logger.warning("Send code failed for %s: %s", mask_phone(phone), result.get("msg"))
        return error_response(_translate_login_error(result, "send_code") or "发送失败")
    return jsonify({"ok": True, "msg": "验证码发送成功"})


@app.route("/api/login/<phone>/verify", methods=["POST"])
def verify_code(phone):
    """验证登录，支持验证码或密码两种方式。"""
    data = request.get_json(silent=True) or {}
    code = data.get("code", "").strip()
    password = data.get("password", "").strip()

    if password:
        scene = "verify_password"
        try:
            result = verify_login(phone, password=password)
        except (requests.ConnectionError, requests.Timeout):
            return error_response("网络异常，请检查网络后重试", 503)
        except NeedLoginError:
            return error_response("登录态失效，请刷新页面后重试", 401)
        except Exception:
            logger.exception("Verify login unexpected error for %s", mask_phone(phone))
            return error_response("登录失败，请稍后重试", 500)
    elif code:
        scene = "verify_code"
        try:
            result = verify_login(phone, code=code)
        except (requests.ConnectionError, requests.Timeout):
            return error_response("网络异常，请检查网络后重试", 503)
        except NeedLoginError:
            return error_response("登录态失效，请刷新页面后重试", 401)
        except Exception:
            logger.exception("Verify login unexpected error for %s", mask_phone(phone))
            return error_response("登录失败，请稍后重试", 500)
    else:
        return error_response("请输入验证码或密码")

    if result.get("_error"):
        logger.warning("Login failed for %s: %s", mask_phone(phone), result.get("msg"))
        return error_response(_translate_login_error(result, scene) or "登录失败")

    return jsonify({"ok": True})


@app.route("/api/accounts/status", methods=["POST"])
def get_accounts_status():
    """批量查询账号状态（懒加载用，启用密码重登兜底）。

    请求体：{"phones": ["138...", ...]}，phones 数量上限 clamp 到 50。
    - phones 为空数组：返回 200 + 空 statuses 列表
    - 未传 phones 参数：返回 400
    - 后端总超时 90s，超时未完成的 phone 填 query_timeout=true 占位
    - phone 不存在的占位项含 phone_not_found=true
    """
    data = request.get_json(silent=True) or {}
    if "phones" not in data:
        return error_response("缺少 phones 参数", 400)

    phones = data.get("phones")
    # 非列表类型（字符串/数字等）拒绝：len() 和切片在字符串上的语义与列表不同，
    # 会导致逐字符处理而非逐手机号处理
    if not isinstance(phones, list):
        return error_response("phones 必须是数组", 400)
    # GUI-009: clamp 到上限 50，响应中携带截断标记与原始数量供前端提示
    total_count = len(phones)
    truncated = total_count > 50
    if truncated:
        phones = phones[:50]

    settings = load_settings()
    statuses = batch_get_account_status(
        phones,
        max_workers=settings.get("max_concurrent", 10),
    )
    # XMC-006: tasks 字段已废弃（前端不消费，仅供内部诊断），路由层统一剥离
    for s in statuses:
        s.pop("tasks", None)
    return jsonify({
        "statuses": statuses,
        "truncated": truncated,
        "queried_count": len(phones),
        "total_count": total_count,
    })


def _start_claim_thread(enabled, settings, tag=""):
    """启动领取后台线程。

    tag 非空时用于日志区分触发来源（如 "CLI"）。返回 True 表示线程已启动；
    False 表示 Thread.start() 失败（claim_mgr 已回滚）。
    前提：调用方已通过 claim_mgr.start() 获取 running 锁。
    """
    def run_claim():
        try:
            run_concurrent_claim(enabled, settings, claim_mgr=claim_mgr)
        except Exception as e:
            logger.exception("%s claim run failed", tag or "GUI")
        finally:
            claim_mgr.finish()

    t = threading.Thread(target=run_claim, daemon=True)
    try:
        t.start()
    except Exception:
        # Thread.start() 失败时回滚 running 状态，避免 claim_mgr 永久 running
        claim_mgr.finish()
        logger.exception("启动领取线程失败 (%s)", tag or "GUI")
        return False
    return True


@app.route("/api/claim", methods=["POST"])
def start_claim():
    """启动领取任务，后台线程执行。

    支持可选 phones 参数（搜索态下"开始领取"只领搜索结果中启用的账号）：
    - 请求体含 phones（非空数组）：只领这些 phone 中 enabled=True 的账号
    - 请求体无 phones 或为空：走 repo.list_enabled() 全体（非搜索态）
    CLI 触发的 /api/cli-trigger-claim 不走此路径，始终领全体。

    CONC-002: claim_mgr.start() 成功（_running=True）后的所有可能抛异常的
    操作（reload_settings / get_enabled_accounts_by_phones / repo.list_enabled）
    用 try/except 兜底，异常时回滚 claim_mgr.finish()，避免 _running 永久
    True 导致后续 /api/claim 请求永远 409。
    """
    if not claim_mgr.start():
        return error_response("领取正在进行中", 409)

    try:
        # 领取前显式刷新 settings 缓存，确保使用最新配置（用户可能在领取前刚改过设置）
        settings = reload_settings()
        data = request.get_json(silent=True) or {}
        phones = data.get("phones")
        if phones is not None:
            # 显式传了 phones：只领这些 phone 中 enabled 的账号（搜索态）
            if not isinstance(phones, list) or not phones:
                claim_mgr.finish()  # 回滚 running 状态
                return error_response("phones 必须是非空数组", 400)
            # 上限 10000（高于 /api/accounts/batch 的 1000）：claim 为只读查询操作，无写锁风险，
            # 仅用于筛选 enabled 账号；batch 是写操作（启用/禁用/删除），长时间持锁风险高故上限 1000
            if len(phones) > 10000:
                claim_mgr.finish()
                return error_response("phones 数量超过上限", 400)
            enabled = get_enabled_accounts_by_phones(phones)
        else:
            # 未传 phones：走全体（非搜索态）
            enabled = repo.list_enabled()

        if not _start_claim_thread(enabled, settings):
            return error_response("启动领取失败，请重试", 500)
        return jsonify({"ok": True, "msg": "领取已开始"})
    except Exception:
        # CONC-002: 准备阶段异常（reload_settings / DB 查询等）兜底回滚，
        # 避免 claim_mgr._running 永久 True 阻塞后续领取请求
        logger.exception("start_claim 准备阶段异常，回滚 claim_mgr")
        claim_mgr.finish()
        return error_response("启动领取失败，请重试", 500)


@app.route("/api/claim/progress", methods=["GET"])
def get_claim_progress():
    """获取当前领取进度。"""
    return jsonify(claim_mgr.get_progress())


@app.route("/api/claim/last-result", methods=["GET"])
def get_claim_last_result():
    """获取上次领取结果快照。

    返回结构与 /api/claim/progress 一致：{ running, progress }。
    progress 取 _last_progress（finish 时浅拷贝的快照）；
    若从未领取过则 progress 为空列表。
    """
    return jsonify(claim_mgr.get_last_progress())


@app.route("/api/cli-trigger-claim", methods=["POST"])
def cli_trigger_claim():
    """CLI 通知 GUI 触发一次领取（统一 Mutex 下 CLI 不自己执行领取）。

    校验 Authorization: Bearer <token>（token 由 GUI 写入 .gui_port 文件，
    CLI 读取后携带），防止本机其他进程未授权触发领取。

    响应：
    - 202 Accepted：已触发领取（GUI 当前空闲，已开始执行）
    - 409 Conflict + {"ok": False, "error": "busy", "running": true}：GUI 正在领取中，跳过本次触发
    - 403 Forbidden：token 缺失或不匹配

    CONC-002: claim_mgr.start() 成功后的 reload_settings / list_enabled
    异常兜底回滚，避免 _running 永久 True 阻塞后续 CLI/GUI 触发。
    """
    # 校验 token：未配置 token（GUI 未启动）或 token 不匹配均拒绝
    expected = _cli_trigger_token
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not expected or not secrets.compare_digest(token, expected):
        return error_response("非法请求", 403)

    if not claim_mgr.start():
        return jsonify({"ok": False, "error": "busy", "running": True}), 409

    try:
        settings = reload_settings()
        enabled = repo.list_enabled()
    except Exception:
        # CONC-002: 准备阶段异常兜底回滚
        logger.exception("cli_trigger_claim 准备阶段异常，回滚 claim_mgr")
        claim_mgr.finish()
        return error_response("启动领取失败，请重试", 500)

    if not _start_claim_thread(enabled, settings, tag="CLI"):
        return error_response("启动领取失败，请重试", 500)
    return jsonify({"ok": True, "msg": "已触发领取"}), 202


def _serialize_schema(schema):
    """将 SETTINGS_SCHEMA 序列化为可 JSON 化的字典，type 从 Python 类型对象转为字符串标识。

    int -> "int" / float -> "float" / bool -> "bool" / str -> "str"。
    enum 类型（schema 含 options 字段）：type 输出 "enum"，并附带 options 供前端渲染下拉框。
    min/max 仅在原 schema 中存在时输出；advanced/label/description 缺省时给安全默认值。
    """
    result = {}
    for key, meta in schema.items():
        item = {"type": meta["type"].__name__}
        if "min" in meta:
            item["min"] = meta["min"]
        if "max" in meta:
            item["max"] = meta["max"]
        item["advanced"] = meta.get("advanced", False)
        item["label"] = meta.get("label", "")
        item["description"] = meta.get("description", "")
        if "nullable" in meta:
            item["nullable"] = meta["nullable"]
        if "actual_key" in meta:
            item["actual_key"] = meta["actual_key"]
        if "forbidden" in meta:
            # frozenset → list，JSON 可序列化；前端按此列表做黑名单校验
            item["forbidden"] = sorted(meta["forbidden"])
        if "options" in meta:
            # enum 类型：覆盖 type 为 "enum"，输出 options 供前端渲染自定义下拉框
            item["type"] = "enum"
            item["options"] = meta["options"]
        if "display_on" in meta:
            # bool 类型：开关开启/关闭时 disabled 占位输入框显示的状态文字
            item["display_on"] = meta["display_on"]
            item["display_off"] = meta["display_off"]
        result[key] = item
    return result


@app.route("/api/settings", methods=["GET"])
def get_settings():
    """获取设置，刷新缓存以反映外部手动编辑。

    返回值在配置项值基础上增加 schema 字段，一次请求同时拿到值和元数据，
    供前端 schema 驱动渲染。同时返回 actual_gui_port（GUI 实际监听端口），
    供前端在 gui_port 字段旁显示「当前:xxx」提示。
    """
    # 打开设置页时刷新缓存，应对外部手动编辑 settings.json 后的缓存过期
    return jsonify({**reload_settings(), "actual_gui_port": _actual_gui_port, "schema": _serialize_schema(SETTINGS_SCHEMA)})


@app.route("/api/settings", methods=["PUT"])
def update_settings():
    """更新设置并持久化。"""
    data = request.get_json(silent=True) or {}
    settings = load_settings()

    # 未知字段检查：不在 SETTINGS_SCHEMA 中的字段直接 400 提示，避免静默忽略
    unknown_keys = [k for k in data.keys() if k not in SETTINGS_SCHEMA]
    if unknown_keys:
        return error_response(f"未知字段: {', '.join(unknown_keys)}")

    # 按字段类型转换值：int 类字段（max_concurrent 等）、float 类字段
    # （request_interval 等）、str 类字段（schedule_time/schan_key）、bool 类字段（schan_enabled）
    # GUI-003: 类型转换失败立即返回 400，避免静默跳过导致用户误以为已保存成功
    for key, value in data.items():
        if key in ("max_concurrent", "max_rounds", "mobile_max_rounds", "max_retries", "batch_delete_reconfirm_threshold"):
            try:
                settings[key] = int(value)
            except (ValueError, TypeError):
                return error_response(f"字段 {key} 的值 {value!r} 不是有效整数", 400)
        elif key in ("request_interval", "retry_delay"):
            try:
                settings[key] = float(value)
            except (ValueError, TypeError):
                return error_response(f"字段 {key} 的值 {value!r} 不是有效浮点数", 400)
        elif key == "schedule_time":
            settings[key] = str(value)
        elif key in ("schan_enabled", "batch_delete_reconfirm", "default_account_enabled",
                     "show_tip", "delete_account_confirm", "auto_show_result_modal",
                     "auto_relogin", "close_window_confirm"):
            settings[key] = bool(value)
        elif key == "schan_key":
            schan_value = str(value)
            if len(schan_value) > 100:
                return error_response("schan_key 长度超过 100 字符", 400)
            # Server酱 Turbo 版 SendKey 以 SCT 开头。
            # 此处仅做长度校验；格式校验由 validate_settings 通过 SENDKEY_PATTERN 执行
            # （pattern_skip_empty=True：空字符串跳过校验，兼容历史数据与现有测试用例）
            settings[key] = schan_value
        elif key in ("default_claim_target", "default_login_method"):
            # enum 字段存储值为字符串，validate_settings 会校验是否在 options 中
            settings[key] = str(value)
        elif key == "gui_port":
            # nullable int：null/空字符串 → None（自动分配）；否则转 int
            if value is None or value == '':
                settings[key] = None
            else:
                try:
                    settings[key] = int(value)
                except (ValueError, TypeError):
                    return error_response(f"字段 {key} 的值 {value!r} 不是有效整数", 400)

    is_valid, error_msg, error_cat = validate_settings(settings)
    if not is_valid:
        return error_response(error_msg)

    try:
        save_settings(settings)
    except OSError:
        logger.exception("保存设置失败")
        return error_response("保存设置失败，请检查磁盘空间或文件权限", 500)
    return jsonify({"ok": True})


@app.route("/api/schedule", methods=["GET"])
def get_schedule():
    """查询 Windows 计划任务状态。"""
    result = query_schedule()
    if "error" in result:
        return error_response(result.get("error", "查询计划任务失败"), 500)
    return jsonify(result)


@app.route("/api/schedule", methods=["POST"])
def post_schedule():
    """创建 Windows 计划任务。"""
    data = request.get_json(silent=True) or {}
    schedule_time = data.get("time", "08:00")
    result = create_schedule(schedule_time)
    if "error" in result:
        code = 400 if result.get("error_category") == "validation" else 500
        return error_response(result.get("error", "创建计划任务失败"), code)
    return jsonify(result)


@app.route("/api/schedule", methods=["DELETE"])
def del_schedule():
    """删除 Windows 计划任务。"""
    result = delete_schedule()
    if "error" in result:
        return error_response(result.get("error", "删除计划任务失败"), 500)
    return jsonify(result)


@app.route("/api/version", methods=["GET"])
def get_version():
    """返回当前版本号。"""
    return jsonify({"version": VERSION})
