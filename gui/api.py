import copy
import logging
import os
import sys
import threading

from typing import Any

import requests
from flask import Flask, jsonify, request, send_from_directory

from core.config import load_accounts, save_accounts, load_settings, save_settings, validate_settings, accounts_lock
from core.service import validate_phone, claim_for_account, run_concurrent_claim, send_login_code, verify_login, get_all_status, get_account_mobile_status, query_schedule, create_schedule, delete_schedule

logger = logging.getLogger(__name__)


def _translate_login_error(result: dict, scene: str) -> str:
    """转译登录错误为用户友好提示。

    服务端对字段校验失败返回 code=1 + 英文技术栈 msg（如
    "InvalidArgument: BINDING: Key: 'XXXRequest.xxx' Error:Field validation
    for 'xxx' failed on the 'len' tag"），直接透传给用户无法理解，需按场景转译。
    其他错误（code=1000/1001/500 等）服务端已返回友好中文，直接透传。

    Args:
        result: client.py 返回的 result dict
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
    return msg or "操作失败"

if getattr(sys, 'frozen', False):
    STATIC_DIR = os.path.join(sys._MEIPASS, 'gui', 'static')
else:
    STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = Flask(__name__, static_folder=STATIC_DIR)


class ClaimManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._progress: list[dict[str, Any]] = []

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._progress = []
            return True

    def finish(self) -> None:
        with self._lock:
            self._running = False

    def get_progress(self) -> dict:
        with self._lock:
            # 后端统计总进度（PC + 手机端）
            total_watched = 0
            total_items = 0
            for e in self._progress:
                # PC 端：初始已观看 + 本次领取
                pc_initial = e.get("pc_initial", 0)
                if e.get("phase") == "mobile":
                    pc_claimed = e.get("pc_claimed", 0)
                else:
                    pc_claimed = e.get("current", 0)
                total_watched += pc_initial + pc_claimed
                total_items += e.get("pc_total", 0)

                # 手机端：初始已观看 + 本次领取（仅 phase=mobile 时有增量）
                if e.get("phase") == "mobile":
                    mobile_claimed = e.get("current", 0)
                else:
                    mobile_claimed = 0
                total_watched += e.get("mobile_initial", 0) + mobile_claimed
                total_items += e.get("mobile_total", 0)

            return {
                "running": self._running,
                "progress": copy.deepcopy(self._progress),
                "total_progress": {"watched": total_watched, "total": total_items},
            }

    def add_progress_entry(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._progress.append(entry)

    def update_progress_entry(self, phone: str, updates: dict[str, Any]) -> None:
        with self._lock:
            for entry in self._progress:
                if entry["phone"] == phone:
                    entry.update(updates)
                    break


claim_mgr = ClaimManager()

VERSION = "1.0.1"


@app.before_request
def _check_json_body():
    if request.method in ("POST", "PUT") and request.content_type and "application/json" in request.content_type:
        if request.content_length and request.content_length > 0:
            if request.get_json(silent=True) is None:
                return jsonify({"error": "请求体必须是有效的 JSON"}), 400


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(STATIC_DIR, path)


@app.route("/api/accounts", methods=["GET"])
def get_accounts():
    accounts = load_accounts()
    accounts.sort(key=lambda a: a.get("phone", ""))
    filtered_accounts = []
    for account in accounts:
        filtered = {k: v for k, v in account.items() if k not in ["auth_token", "user_id", "saved_at", "password"]}
        filtered_accounts.append(filtered)
    return jsonify({"accounts": filtered_accounts})


@app.route("/api/accounts/<phone>", methods=["GET"])
def get_account(phone):
    accounts = load_accounts()
    for a in accounts:
        if a["phone"] == phone:
            # 过滤敏感字段（与 /api/accounts 保持一致，防止 password 泄露给前端）
            filtered = {k: v for k, v in a.items() if k not in ["auth_token", "user_id", "saved_at", "password"]}
            return jsonify({"account": filtered})
    return jsonify({"error": "账号不存在"}), 404


@app.route("/api/accounts", methods=["POST"])
def add_account():
    data = request.json
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"error": "手机号不能为空"}), 400

    if not validate_phone(phone):
        return jsonify({"error": "手机号格式不正确"}), 400

    with accounts_lock:
        accounts = load_accounts()
        for a in accounts:
            if a["phone"] == phone:
                return jsonify({"error": "手机号已存在"}), 400

        claim_target = data.get("claim_target", "all")
        if claim_target not in ("all", "pc", "mobile"):
            claim_target = "all"
        accounts.append({
            "name": data.get("name", "").strip(),
            "phone": phone,
            "remark": data.get("remark", "").strip(),
            "enabled": data.get("enabled", True),
            "claim_target": claim_target,
        })
        save_accounts(accounts)
    return jsonify({"ok": True})


@app.route("/api/accounts/<phone>", methods=["PUT"])
def update_account(phone):
    data = request.json
    with accounts_lock:
        accounts = load_accounts()
        for a in accounts:
            if a["phone"] == phone:
                if "phone" in data and data["phone"] != phone:
                    new_phone = data["phone"].strip()
                    if not new_phone:
                        return jsonify({"error": "手机号不能为空"}), 400
                    if not validate_phone(new_phone):
                        return jsonify({"error": "手机号格式不正确"}), 400
                    for other in accounts:
                        if other["phone"] == new_phone:
                            return jsonify({"error": "手机号已存在"}), 400
                    a["phone"] = new_phone
                    a.pop("device_id", None)
                    a.pop("auth_token", None)
                    a.pop("user_id", None)
                    a.pop("saved_at", None)
                    a.pop("password", None)
                if "name" in data:
                    a["name"] = data["name"]
                if "remark" in data:
                    a["remark"] = data["remark"]
                if "enabled" in data:
                    a["enabled"] = data["enabled"]
                if "claim_target" in data:
                    ct = data["claim_target"]
                    a["claim_target"] = ct if ct in ("all", "pc", "mobile") else "all"
                if "password" in data:
                    pwd = data["password"]
                    if pwd:
                        a["password"] = pwd
                    else:
                        a.pop("password", None)
                save_accounts(accounts)
                return jsonify({"ok": True})
    return jsonify({"error": "账号不存在"}), 404


@app.route("/api/accounts/<phone>", methods=["DELETE"])
def delete_account(phone):
    with accounts_lock:
        accounts = load_accounts()
        accounts = [a for a in accounts if a["phone"] != phone]
        save_accounts(accounts)
    return jsonify({"ok": True})


@app.route("/api/accounts/<phone>/mobile_status", methods=["GET"])
def get_mobile_status(phone):
    """查询账号手机端状态（时长余额 + 任务进度）。

    预留给前端卡片翻转 UI 按需调用，避免卡片翻转时被迫触发全量刷新。
    参考 get_account_status 的结构，仅查询手机端。
    """
    accounts = load_accounts()
    account = next((a for a in accounts if a["phone"] == phone), None)
    if not account:
        return jsonify({"error": "账号不存在"}), 404
    return jsonify({"status": get_account_mobile_status(account)})


@app.route("/api/login/<phone>", methods=["POST"])
def send_code(phone):
    try:
        result = send_login_code(phone)
    except (requests.ConnectionError, requests.Timeout):
        return jsonify({"error": "网络异常，请检查网络后重试"}), 400
    except Exception:
        logger.exception("Send code unexpected error for %s", phone)
        return jsonify({"error": "发送验证码失败，请稍后重试"}), 400

    if result.get("_error"):
        code = result.get("code")
        # 错误码60和1000表示触发验证码请求冷却，验证码已发送但需等待
        if code in (60, 1000):
            return jsonify({"ok": True, "msg": "验证码请求冷却中，请稍后再试"})
        logger.warning("Send code failed for %s: %s", phone, result.get("msg"))
        return jsonify({"error": _translate_login_error(result, "send_code") or "发送失败"}), 400
    return jsonify({"ok": True, "msg": "验证码发送成功"})


@app.route("/api/login/<phone>/verify", methods=["POST"])
def verify_code(phone):
    data = request.json
    code = data.get("code", "").strip()
    password = data.get("password", "").strip()

    if password:
        scene = "verify_password"
        try:
            result = verify_login(phone, password=password)
        except (requests.ConnectionError, requests.Timeout):
            return jsonify({"error": "网络异常，请检查网络后重试"}), 400
        except Exception:
            logger.exception("Verify login unexpected error for %s", phone)
            return jsonify({"error": "登录失败，请稍后重试"}), 400
    elif code:
        scene = "verify_code"
        try:
            result = verify_login(phone, code=code)
        except (requests.ConnectionError, requests.Timeout):
            return jsonify({"error": "网络异常，请检查网络后重试"}), 400
        except Exception:
            logger.exception("Verify login unexpected error for %s", phone)
            return jsonify({"error": "登录失败，请稍后重试"}), 400
    else:
        return jsonify({"error": "请输入验证码或密码"}), 400

    if result.get("_error"):
        logger.warning("Login failed for %s: %s", phone, result.get("msg"))
        return jsonify({"error": _translate_login_error(result, scene) or "登录失败"}), 400

    return jsonify({"ok": True})


@app.route("/api/status", methods=["GET"])
def get_status():
    settings = load_settings()
    return jsonify({"status": get_all_status(max_workers=settings.get("max_concurrent", 10))})


@app.route("/api/claim", methods=["POST"])
def start_claim():
    if not claim_mgr.start():
        return jsonify({"error": "领取正在进行中"}), 400

    settings = load_settings()
    accounts = load_accounts()
    enabled = [a for a in accounts if a.get("enabled", True)]

    def run_claim():
        try:
            run_concurrent_claim(enabled, settings, claim_mgr=claim_mgr)
        except Exception as e:
            logger.exception("Claim run failed")
        finally:
            claim_mgr.finish()

    t = threading.Thread(target=run_claim, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "领取已开始"})


@app.route("/api/claim/progress", methods=["GET"])
def get_claim_progress():
    return jsonify(claim_mgr.get_progress())


@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(load_settings())


@app.route("/api/settings", methods=["PUT"])
def update_settings():
    data = request.json
    settings = load_settings()

    for key, value in data.items():
        if key in ("max_concurrent", "max_rounds", "mobile_max_rounds"):
            try:
                settings[key] = int(value)
            except (ValueError, TypeError):
                settings[key] = value
        elif key == "request_interval":
            try:
                settings[key] = float(value)
            except (ValueError, TypeError):
                settings[key] = value
        elif key == "schedule_time":
            settings[key] = str(value)
        elif key == "schan_enabled":
            settings[key] = bool(value)
        elif key == "schan_key":
            settings[key] = str(value)

    is_valid, error_msg, error_cat = validate_settings(settings)
    if not is_valid:
        return jsonify({"error": error_msg}), 400

    save_settings(settings)
    return jsonify({"ok": True})


@app.route("/api/schedule", methods=["GET"])
def get_schedule():
    result = query_schedule()
    if "error" in result:
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/schedule", methods=["POST"])
def post_schedule():
    data = request.json or {}
    schedule_time = data.get("time", "08:00")
    result = create_schedule(schedule_time)
    if "error" in result:
        code = 400 if result.get("error_category") == "validation" else 500
        return jsonify(result), code
    return jsonify(result)


@app.route("/api/schedule", methods=["DELETE"])
def del_schedule():
    result = delete_schedule()
    if "error" in result:
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/version", methods=["GET"])
def get_version():
    return jsonify({"version": VERSION})
