import copy
import logging
import os
import sys
import threading

from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from core.config import load_accounts, save_accounts, load_settings, save_settings, validate_settings, accounts_lock
from core.service import validate_phone, claim_for_account, run_concurrent_claim, send_login_code, verify_login, get_all_status, query_schedule, create_schedule, delete_schedule

logger = logging.getLogger(__name__)

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
            return {
                "running": self._running,
                "progress": copy.deepcopy(self._progress),
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
        filtered = {k: v for k, v in account.items() if k not in ["auth_token", "user_id", "saved_at"]}
        filtered_accounts.append(filtered)
    return jsonify({"accounts": filtered_accounts})


@app.route("/api/accounts/<phone>", methods=["GET"])
def get_account(phone):
    accounts = load_accounts()
    for a in accounts:
        if a["phone"] == phone:
            filtered = {k: v for k, v in a.items() if k not in ["auth_token", "user_id", "saved_at"]}
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

        accounts.append({
            "name": data.get("name", "").strip(),
            "phone": phone,
            "remark": data.get("remark", "").strip(),
            "enabled": data.get("enabled", True),
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
                if "name" in data:
                    a["name"] = data["name"]
                if "remark" in data:
                    a["remark"] = data["remark"]
                if "enabled" in data:
                    a["enabled"] = data["enabled"]
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


@app.route("/api/login/<phone>", methods=["POST"])
def send_code(phone):
    result = send_login_code(phone)
    if result.get("_error"):
        code = result.get("code")
        # 错误码60和1000表示触发验证码请求冷却，验证码已发送但需等待
        if code in (60, 1000):
            return jsonify({"ok": True, "msg": "验证码请求冷却中，请稍后再试"})
        logger.warning("Send code failed for %s: %s", phone, result.get("msg"))
        return jsonify({"error": result.get("msg", "发送失败")}), 400
    return jsonify({"ok": True, "msg": "验证码发送成功"})


@app.route("/api/login/<phone>/verify", methods=["POST"])
def verify_code(phone):
    code = request.json.get("code", "").strip()
    if not code:
        return jsonify({"error": "验证码不能为空"}), 400

    result = verify_login(phone, code)
    if result.get("_error"):
        logger.warning("Login failed for %s: %s", phone, result.get("msg"))
        return jsonify({"error": result.get("msg", "登录失败")}), 400

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
        if key in ("max_concurrent", "max_rounds"):
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
