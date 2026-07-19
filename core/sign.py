"""
签名算法模块。

签名逻辑参考 SignInterceptor.getSortParameters / getSign：
- get_sort_parameters() 对查询参数按 key 排序，并追加 ts、nonce、ver；
- sign_url() 再拼接 method + host[/:port] + path + query，并对整体做 SHA-256 签名。
"""

import hashlib
import time
import uuid
from urllib.parse import quote, unquote

# 签名版本号，逆向自 APK 的 SignInterceptor，固定值
SIGN_VERSION = "2023-08-28"


def get_sort_parameters(query_params: dict | None) -> str:
    """
    仿照 SignInterceptor.getSortParameters 实现。

    将查询参数按 key 排序，并追加 ts、nonce、ver 后拼接为查询字符串。
    None 值会保留为 key=。

    Args:
        query_params: 原始查询参数

    Returns:
        排序后的查询字符串，格式为 key=value&key=value&...
    """
    params = {}
    if query_params:
        for k, v in query_params.items():
            params[k] = v

    params["ts"] = str(int(time.time()))
    nonce = uuid.uuid4().hex
    params["nonce"] = nonce
    params["ver"] = SIGN_VERSION

    sorted_keys = sorted(params.keys())
    parts = []
    for key in sorted_keys:
        val = params[key]
        if val is not None:
            parts.append(f"{key}={quote(str(val), safe='')}")
        else:
            parts.append(f"{key}=")

    return "&".join(parts)


def get_sign(data: str) -> str:
    """
    仿照 SignInterceptor.getSign 实现。

    先对输入字符串做 URL decode，再计算 SHA-256 摘要并返回 hex 字符串。

    Args:
        data: 待签名的原始字符串

    Returns:
        SHA-256 摘要的十六进制字符串
    """
    decoded = unquote(data)
    sha256 = hashlib.sha256(decoded.encode("utf-8")).digest()
    return sha256.hex()


def sign_url(method: str, host: str, path: str, query_params: dict | None = None, port: int | None = None) -> str:
    """
    对请求 URL 进行签名，内部调用 get_sort_parameters 和 get_sign。

    签名流程:
    1. 对 query_params 排序并追加 ts / nonce / ver，生成 sorted_query
    2. 拼接签名字符串: "{METHOD}{HOST}[:PORT]{PATH}?{sorted_query}"
    3. 对签名字符串做 SHA-256 哈希，结果作为 sig 参数追加到 URL 末尾

    Args:
        method: HTTP 方法，如 "GET"、"POST"
        host: 主机名
        path: 请求路径，如 "/api/v1/xxx"
        query_params: 原始查询参数
        port: 端口号；当为 80 或 443 时省略端口，其他值会拼接到 host 后

    Returns:
        带签名的 URL 字符串，格式为 host[/:port]path?sorted_query&sig=xxx，
        不含协议前缀，由调用方拼接 BASE_URL
    """
    sorted_query = get_sort_parameters(query_params)

    if port and port not in (80, 443):
        base = f"{host}:{port}{path}?{sorted_query}"
    else:
        base = f"{host}{path}?{sorted_query}"

    sign_str = get_sign(f"{method}{base}")
    return f"{base}&sig={sign_str}"
