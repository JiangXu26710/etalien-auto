import logging
import time
import uuid
from typing import Any, Callable

import requests

from core.sign import sign_url
import account_pb2
import apiv2_pb2
import error_pb2

logger = logging.getLogger(__name__)


class NeedLoginError(Exception):
    """token 过期且无法重登（无密码、密码错误、或已尝试过重登）时抛出。

    由 _post/_get 在检测到 auth error 且无法恢复时抛出，
    claim_for_account 捕获后返回 "need_login" 状态。
    """
    pass


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


class EtAlienClient:
    BASE_URL = "https://api.et-api.com"
    HOST = "api.et-api.com"

    @staticmethod
    def generate_device_id() -> str:
        return uuid.uuid4().hex[:25]

    def __init__(self, phone: str, auth_token: str | None = None, device_id: str | None = None,
                 password: str | None = None,
                 on_relogin: Callable[[str | None, int | None, bool], None] | None = None,
                 max_retries: int = 3,
                 retry_delay: float = 1.0):
        self.phone = phone
        self.auth_token = auth_token
        self.user_id: int | None = None
        self.device_id = device_id or self.generate_device_id()

        # token 过期自动重登能力（可选，仅领取流程注入）
        self._password = password
        self._on_relogin = on_relogin   # 回调：持久化新 token / 清除密码
        self._relogin_attempted = False  # 防止循环重登

        # 重试机制配置（从 settings.json 注入）
        self._max_retries = max_retries
        self._retry_delay = retry_delay

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

    def _request_with_retry(self, method: str, url: str, data: bytes | None = None, timeout: int = 30, retry_on_500: bool = True) -> tuple[int, bytes]:
        last_exc = None
        for attempt in range(self._max_retries + 1):
            try:
                if method == "POST":
                    resp = self.session.post(url, data=data, timeout=timeout)
                else:
                    resp = self.session.get(url, data=data, timeout=timeout)
                # 密码登录等业务接口在参数错误时也会返回 500（实测 code=500 的业务错误），
                # 这类响应不应重试，由调用方通过 retry_on_500=False 关闭
                if resp.status_code >= 500 and retry_on_500 and attempt < self._max_retries:
                    logger.warning("服务端错误 %d(第%d次)，%0.1fs后重试", resp.status_code, attempt + 1, self._retry_delay)
                    time.sleep(self._retry_delay)
                    continue
                return resp.status_code, resp.content
            except requests.ConnectionError as e:
                last_exc = e
                if attempt < self._max_retries:
                    logger.warning("请求失败(第%d次)，%0.1fs后重试: %s", attempt + 1, self._retry_delay, e)
                    time.sleep(self._retry_delay)
            except requests.Timeout as e:
                last_exc = e
                if attempt < self._max_retries:
                    logger.warning("请求超时(第%d次)，%0.1fs后重试: %s", attempt + 1, self._retry_delay, e)
                    time.sleep(self._retry_delay)
        raise last_exc

    def _post(self, path: str, body: bytes | None = None, query: dict | None = None, retry_on_500: bool = True) -> dict[str, Any]:
        url = self._build_url("POST", path, query)
        status_code, data = self._request_with_retry("POST", url, data=body, retry_on_500=retry_on_500)
        result = self._parse_response(status_code, data)
        # token 过期自动重登重试
        if self._is_auth_error(result):
            result = self._handle_auth_error_and_retry("POST", url, body, query, retry_on_500)
        return result

    def _get(self, path: str, query: dict | None = None, body: bytes | None = None, retry_on_500: bool = True) -> dict[str, Any]:
        url = self._build_url("GET", path, query)
        status_code, data = self._request_with_retry("GET", url, data=body, retry_on_500=retry_on_500)
        result = self._parse_response(status_code, data)
        if self._is_auth_error(result):
            result = self._handle_auth_error_and_retry("GET", url, body, query, retry_on_500)
        return result

    def _handle_auth_error_and_retry(self, method: str, url: str,
                                      body: bytes | None, query: dict | None,
                                      retry_on_500: bool) -> dict[str, Any]:
        """token 过期时的公共处理流程。

        1. 无密码或已重登过 → 抛 NeedLoginError
        2. 密码重登失败 → 抛 NeedLoginError（密码错误时回调清除密码）
        3. 重登成功 → 持久化新 token → 重试原请求
        """
        if not self._password:
            logger.info("[%s] token 过期，未配置密码，无法重登", self.phone)
            raise NeedLoginError(f"{self.phone}: token expired, no password")

        if self._relogin_attempted:
            # 触发场景有二，均不再重复重登：
            #   (1) 之前已重登过（无论成功/失败），后续其他 API 请求又收到 auth error
            #   (2) 重登请求本身返回 auth error（重登走 _post 递归进入此分支）
            logger.warning("[%s] token 过期，重登已尝试过（不再重复重登）", self.phone)
            raise NeedLoginError(f"{self.phone}: relogin already attempted")

        self._relogin_attempted = True
        logger.info("[%s] token 过期，尝试密码重登", self.phone)

        # 重登请求走 _post → _request_with_retry，享有 Layer 1 的本地重试兜底（网络异常/超时）
        # 注意：login_by_password 传 retry_on_500=False，HTTP 500 不重试（密码错误的 500 是业务错误）
        try:
            login_result = self.login_by_password(self._password)
        except requests.RequestException as e:
            # 重登请求重试后仍网络异常，放弃重登
            logger.warning("[%s] 密码重登网络异常（已重试 %d 次）: %s", self.phone, self._max_retries, e)
            raise NeedLoginError(f"{self.phone}: relogin network error")

        if login_result.get("_error"):
            logger.warning("[%s] 密码重登失败: %s", self.phone, login_result.get("msg"))
            # 密码错误时清除保存的密码（通过回调）
            if self._on_relogin and _is_password_incorrect(login_result):
                try:
                    self._on_relogin(new_token=None, user_id=None, clear_password=True)
                    logger.info("[%s] 密码错误，已清除保存的密码", self.phone)
                except Exception:
                    # 回调失败时密码未清除，日志需明确说明"未清除"以免误导
                    logger.exception("[%s] 密码错误，但清除密码回调失败，密码未清除", self.phone)
            raise NeedLoginError(f"{self.phone}: relogin failed")

        # 重登成功：login_by_password 内部已更新 self.auth_token / self.user_id，
        # 并同步到 session.headers["Authorization"]
        # 这里通过回调持久化新 token 到 accounts.json
        if self._on_relogin:
            try:
                self._on_relogin(new_token=self.auth_token, user_id=self.user_id)
            except Exception:
                # 回调异常（如文件写入失败）不应阻断重登后的重试流程
                # token 已在 session.headers 中，重试仍可进行
                logger.exception("[%s] 持久化新 token 回调异常", self.phone)
        logger.info("[%s] 密码重登成功，重试原请求", self.phone)

        # 重试原请求（新 token 已在 session.headers 中）
        # 重试请求走 _request_with_retry，即重登后的请求同样享有"本地原因 3 次重试"兜底
        status_code, data = self._request_with_retry(method, url, data=body, retry_on_500=retry_on_500)
        result = self._parse_response(status_code, data)

        # 防无限重试：重登后重试的原请求若仍返回 auth error，不再重登，直接抛异常
        if self._is_auth_error(result):
            logger.warning("[%s] 重登后重试请求仍返回 auth error，放弃", self.phone)
            raise NeedLoginError(f"{self.phone}: auth error persists after relogin")

        return result

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

    def get_login_code(self, phone: str | None = None) -> dict[str, Any]:
        phone = phone or self.phone
        formatted = self._format_phone(phone)
        req = account_pb2.GetLoginVerificationCodeRequest()
        req.phone_number = formatted
        return self._get("/account/v1/get_login_verification_code", body=req.SerializeToString())

    def login_by_code(self, code: str, phone: str | None = None) -> dict[str, Any]:
        phone = phone or self.phone
        formatted = self._format_phone(phone)
        req = account_pb2.LoginRequest()
        req.phone_number = formatted
        req.verification_code = code

        result = self._post(
            "/account/v1/login",
            body=req.SerializeToString(),
        )

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

    def login_by_password(self, password: str, phone: str | None = None) -> dict[str, Any]:
        """密码登录（与验证码登录相互独立）
        接口: POST /v2/account/login
        密码为明文，规则 6-20 位 [a-zA-Z0-9_.]
        """
        phone = phone or self.phone
        formatted = self._format_phone(phone)
        req = account_pb2.LoginV2Request(phone_number=formatted, password=password)
        body = req.SerializeToString()

        # 密码登录接口对参数/业务错误也返回 500（实测 code=500），不应重试，否则用户要等 ~3s 才看到提示
        result = self._post("/v2/account/login", body=body, retry_on_500=False)

        if not result.get("_error"):
            try:
                # LoginV2Response 与 LoginResponse 结构相同，复用解析
                resp = account_pb2.LoginResponse()
                resp.ParseFromString(result["raw"])
                self.auth_token = resp.authorization
                self.user_id = resp.user_id
                self.session.headers["Authorization"] = self.auth_token
                result["user_id"] = resp.user_id
                result["authorization"] = resp.authorization
            except Exception as e:
                logger.exception("Failed to parse LoginV2Response for %s", phone)
                result["parse_error"] = str(e)

        return result

    def fetch_pc_ad_config(self) -> dict[str, Any]:
        req = apiv2_pb2.PcAdConfigRequest()
        result = self._post("/v2/account/pc/ad/config", body=req.SerializeToString())

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
                # 校验 watch_cnt 与 is_watch 标志一致性（两口径并存：watch_cnt 来自服务端，
                # is_watch 来自 items；若不一致，状态查询与领取循环的进度展示可能跳变）
                for level_item in result["list"]:
                    is_watch_sum = sum(1 for it in level_item["items"] if it["is_watch"])
                    if level_item["watch_cnt"] != is_watch_sum:
                        logger.warning(
                            "PcAdConfig 校验不一致: level=%d watch_cnt=%d 但 is_watch=true 的 items 数=%d，"
                            "进度展示可能跳变",
                            level_item["level"], level_item["watch_cnt"], is_watch_sum,
                        )
            except Exception as e:
                logger.exception("Failed to parse PcAdConfigResponse")
                result["parse_error"] = str(e)

        return result

    def fetch_pc_duration(self) -> dict[str, Any]:
        req = apiv2_pb2.GetUserRemainDurationRequest()
        result = self._post("/v2/account/remain/duration", body=req.SerializeToString())

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

    def pc_ad_callback_backup(self, ad_id: str = "103334281", business: int = 1) -> dict[str, Any]:
        """广告奖励补发（PC/手机/翻译三种权益共用此接口）。

        Args:
            ad_id: 穿山甲广告位ID，必须与 business 类型严格对应。
                  PC加速="103334281", 手机加速="102815305", 翻译加速="103579416"
            business: 业务类型。1=PC加速, 2=手机加速, 3=翻译加速

        注意：ad_id 与 business 不匹配时服务端会返回 is_verify=False。
        当前项目使用 PC 加速（business=1, ad_id="103334281"）和手机加速（business=2, ad_id="102815305"）。
        """
        req = apiv2_pb2.PcAdCallbackBackupRequest()
        req.ad_id = ad_id
        req.business = business
        result = self._post("/v2/account/pc/ad/callback/backup", body=req.SerializeToString())

        if not result.get("_error"):
            try:
                resp = apiv2_pb2.PcAdCallbackBackupResponse()
                resp.ParseFromString(result["raw"])
                result["is_verify"] = resp.is_verify
            except Exception as e:
                logger.exception("Failed to parse PcAdCallbackBackupResponse")
                result["parse_error"] = str(e)

        return result

    def fetch_mobile_ad_activity(self) -> dict[str, Any]:
        """获取手机端广告任务列表。

        GET /award/v1/ad/activity
        响应结构见 apiv2.proto 的 AdActivityResponse。

        Returns:
            dict: 通用错误字段 + 业务字段
                - user_watch_cnt: 累计 is_verify=true 的调用次数
                - video_cnt: 任务总数（实测=任务列表长度）
                - activity_status: 活动状态（1=进行中）
                - video_bar: 任务列表 [{id, has_award, award, is_get}]
                - rewarded_count: 有奖励的任务数（has_award=True）
                - claimed_count: 已领取奖励的任务数（has_award=True 且 is_get=True）
        """
        result = self._get("/award/v1/ad/activity")

        if not result.get("_error"):
            try:
                resp = apiv2_pb2.AdActivityResponse()
                resp.ParseFromString(result["raw"])
                video_bar = [
                    {
                        "id": item.id,
                        "has_award": item.has_award,
                        "award": item.award,
                        "is_get": item.is_get,
                    }
                    for item in resp.video_bar
                ]
                rewarded = [t for t in video_bar if t["has_award"]]
                claimed = [t for t in rewarded if t["is_get"]]
                result.update({
                    "user_watch_cnt": resp.user_watch_cnt,
                    "video_cnt": resp.video_cnt,
                    "activity_status": resp.activity_status,
                    "video_bar": video_bar,
                    "rewarded_count": len(rewarded),
                    "claimed_count": len(claimed),
                })
            except Exception as e:
                logger.exception("Failed to parse AdActivityResponse")
                result["parse_error"] = str(e)

        return result

    def fetch_mobile_profile(self) -> dict[str, Any]:
        """获取用户资料（含手机端加速时长余额）。

        GET /account/v1/my_profile
        响应结构见 account.proto 的 MyProfileResponse。

        手机端加速时长余额 = max(0, member.expire_time - 当前时间戳)

        Returns:
            dict: 通用错误字段 + 业务字段
                - user_id, nickname, avatar, steamid, register_time
                - members: 会员信息列表 [{type, expire_time}]
                - member: 当前会员信息 {type, expire_time} 或 None
                - video_award: 视频奖励 {award, has} 或 None
                - mobile_not_get_ad_duration: 今日未领取广告时长（小时）
                - remaining_seconds: 加速时长余额（秒，由 member.expire_time 计算）
        """
        result = self._get("/account/v1/my_profile")

        if not result.get("_error"):
            try:
                resp = account_pb2.MyProfileResponse()
                resp.ParseFromString(result["raw"])
                member = None
                if resp.HasField("member"):
                    member = {
                        "type": resp.member.type,
                        "expire_time": resp.member.expire_time,
                    }
                video_award = None
                if resp.HasField("video_award"):
                    video_award = {
                        "award": resp.video_award.award,
                        "has": resp.video_award.has,
                    }
                members = [
                    {"type": m.type, "expire_time": m.expire_time}
                    for m in resp.members
                ]
                # 计算加速时长余额
                remaining = 0
                if member:
                    now = int(time.time())
                    remaining = max(0, member["expire_time"] - now)
                result.update({
                    "user_id": resp.user_id,
                    "nickname": resp.nickname,
                    "avatar": resp.avatar,
                    "steamid": resp.steamid,
                    "register_time": resp.register_time,
                    "members": members,
                    "member": member,
                    "video_award": video_award,
                    "mobile_not_get_ad_duration": resp.mobile_not_get_ad_duration,
                    "remaining_seconds": remaining,
                })
            except Exception as e:
                logger.exception("Failed to parse MyProfileResponse")
                result["parse_error"] = str(e)

        return result
