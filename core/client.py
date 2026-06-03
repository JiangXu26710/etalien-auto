import logging
import time
import uuid
from typing import Any

import requests

from core.sign import sign_url
import account_pb2
import apiv2_pb2
import error_pb2

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
RETRY_DELAY = 1.0


class EtAlienClient:
    BASE_URL = "https://api.et-api.com"
    HOST = "api.et-api.com"

    @staticmethod
    def generate_device_id() -> str:
        return uuid.uuid4().hex[:25]

    def __init__(self, phone: str, auth_token: str | None = None, device_id: str | None = None):
        self.phone = phone
        self.auth_token = auth_token
        self.user_id: int | None = None
        self.device_id = device_id or self.generate_device_id()

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "okhttp/4.12.0",
            "Accept": "application/x-protobuf",
            "Content-Type": "application/x-protobuf",
            "x-eta": f"os=0&ver=3.11.10&dvc={self.device_id}&ch=default",
        })

        if self.auth_token:
            self.session.headers["Authorization"] = self.auth_token

    def _build_url(self, method: str, path: str, query: dict | None = None) -> str:
        signed = sign_url(method, self.HOST, path, query)
        query_part = signed.split("?", 1)[1] if "?" in signed else ""
        return f"{self.BASE_URL}{path}?{query_part}"

    def _request_with_retry(self, method: str, url: str, data: bytes | None = None, timeout: int = 30) -> tuple[int, bytes]:
        last_exc = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                if method == "POST":
                    resp = self.session.post(url, data=data, timeout=timeout)
                else:
                    resp = self.session.get(url, data=data, timeout=timeout)
                if resp.status_code >= 500 and attempt < MAX_RETRIES:
                    logger.warning("服务端错误 %d(第%d次)，%0.1fs后重试", resp.status_code, attempt + 1, RETRY_DELAY)
                    time.sleep(RETRY_DELAY)
                    continue
                return resp.status_code, resp.content
            except requests.ConnectionError as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    logger.warning("请求失败(第%d次)，%0.1fs后重试: %s", attempt + 1, RETRY_DELAY, e)
                    time.sleep(RETRY_DELAY)
            except requests.Timeout as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    logger.warning("请求超时(第%d次)，%0.1fs后重试: %s", attempt + 1, RETRY_DELAY, e)
                    time.sleep(RETRY_DELAY)
        raise last_exc

    def _post(self, path: str, body: bytes | None = None, query: dict | None = None) -> tuple[int, bytes]:
        url = self._build_url("POST", path, query)
        return self._request_with_retry("POST", url, data=body)

    def _get(self, path: str, query: dict | None = None, body: bytes | None = None) -> tuple[int, bytes]:
        url = self._build_url("GET", path, query)
        return self._request_with_retry("GET", url, data=body)

    def _parse_protobuf_error(self, data: bytes) -> dict[str, Any] | None:
        try:
            err = error_pb2.Error()
            err.ParseFromString(data)
            return {"code": err.code, "msg": err.msg}
        except Exception:
            logger.debug("Failed to parse protobuf error from %d bytes", len(data))
            return None

    def _parse_response(self, status_code: int, data: bytes) -> dict[str, Any]:
        result: dict[str, Any] = {"status_code": status_code}
        if status_code >= 400:
            err = self._parse_protobuf_error(data)
            if err:
                result["_error"] = True
                result.update(err)
            else:
                result["_error"] = True
                result["msg"] = f"HTTP {status_code}: {data[:200]}"
        else:
            result["_error"] = False
            result["raw"] = data
        return result

    @staticmethod
    def _format_phone(phone: str) -> str:
        if phone.startswith("+"):
            return phone
        return f"+86{phone}"

    def is_logged_in(self) -> bool:
        return bool(self.auth_token)

    @staticmethod
    def _is_auth_error(result: dict[str, Any]) -> bool:
        if not result.get("_error"):
            return False
        code = result.get("code")
        if code in (401, 403):
            return True
        if isinstance(code, int) and code == 16:
            return True
        return False

    def check_token_valid(self) -> bool | None:
        if not self.auth_token:
            return False
        try:
            result = self.fetch_pc_duration()
        except Exception:
            logger.warning("check_token_valid: network error for %s", self.phone)
            return None
        if self._is_auth_error(result):
            return False
        if result.get("_error"):
            logger.warning("check_token_valid: server error for %s: %s", self.phone, result.get("msg"))
            return None
        return True

    def get_login_code(self, phone: str | None = None) -> dict[str, Any]:
        phone = phone or self.phone
        formatted = self._format_phone(phone)
        req = account_pb2.GetLoginVerificationCodeRequest()
        req.phone_number = formatted
        status_code, data = self._get("/account/v1/get_login_verification_code", body=req.SerializeToString())
        return self._parse_response(status_code, data)

    def login_by_code(self, code: str, phone: str | None = None) -> dict[str, Any]:
        phone = phone or self.phone
        formatted = self._format_phone(phone)
        req = account_pb2.LoginRequest()
        req.phone_number = formatted
        req.verification_code = code

        status_code, data = self._post(
            "/account/v1/login",
            body=req.SerializeToString(),
        )
        result = self._parse_response(status_code, data)

        if not result.get("_error"):
            try:
                resp = account_pb2.LoginResponse()
                resp.ParseFromString(result["raw"])
                self.auth_token = resp.authorization
                self.user_id = resp.user_id
                self.session.headers["Authorization"] = self.auth_token
                result["user_id"] = resp.user_id
                result["authorization"] = resp.authorization
            except Exception as e:
                logger.exception("Failed to parse LoginResponse for %s", phone)
                result["parse_error"] = str(e)

        return result

    def fetch_pc_ad_config(self) -> dict[str, Any]:
        req = apiv2_pb2.PcAdConfigRequest()
        status_code, data = self._post("/v2/account/pc/ad/config", body=req.SerializeToString())
        result = self._parse_response(status_code, data)

        if not result.get("_error"):
            try:
                resp = apiv2_pb2.PcAdConfigResponse()
                resp.ParseFromString(result["raw"])
                result["list"] = [
                    {
                        "level": item.level,
                        "watch_cnt": item.watch_cnt,
                        "title": item.title,
                        "text": item.text,
                        "tag": item.tag,
                        "items": [
                            {
                                "id": it.id,
                                "award_unix": it.award_unix,
                                "level": it.level,
                                "is_watch": it.is_watch,
                                "title": it.title,
                            }
                            for it in item.list
                        ],
                    }
                    for item in resp.list
                ]
            except Exception as e:
                logger.exception("Failed to parse PcAdConfigResponse")
                result["parse_error"] = str(e)

        return result

    def fetch_pc_duration(self) -> dict[str, Any]:
        req = apiv2_pb2.GetUserRemainDurationRequest()
        status_code, data = self._post("/v2/account/remain/duration", body=req.SerializeToString())
        result = self._parse_response(status_code, data)

        if not result.get("_error"):
            try:
                resp = apiv2_pb2.GetUserRemainDurationResponse()
                resp.ParseFromString(result["raw"])
                result.update({
                    "vip_duration_second": resp.vip_duration_second,
                    "free_duration_second": resp.free_duration_second,
                    "timestamp": resp.timestamp,
                    "pause_state": resp.pause_state,
                    "is_first_award": resp.is_first_award,
                    "pc_vip_state": resp.pc_vip_state,
                })
            except Exception as e:
                logger.exception("Failed to parse GetUserRemainDurationResponse")
                result["parse_error"] = str(e)

        return result

    def pc_ad_callback_backup(self, ad_id: str = "103334281", business: int = 0) -> dict[str, Any]:
        req = apiv2_pb2.PcAdCallbackBackupRequest()
        req.ad_id = ad_id
        req.business = business
        status_code, data = self._post("/v2/account/pc/ad/callback/backup", body=req.SerializeToString())
        result = self._parse_response(status_code, data)

        if not result.get("_error"):
            try:
                resp = apiv2_pb2.PcAdCallbackBackupResponse()
                resp.ParseFromString(result["raw"])
                result["is_verify"] = resp.is_verify
            except Exception as e:
                logger.exception("Failed to parse PcAdCallbackBackupResponse")
                result["parse_error"] = str(e)

        return result
