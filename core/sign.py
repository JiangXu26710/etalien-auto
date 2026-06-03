import hashlib
import time
import uuid
from urllib.parse import quote, unquote


def get_sort_parameters(query_params: dict | None) -> str:
    """
    仿照 SignInterceptor.getSortParameters 实现
    对URL参数进行排序并添加 ts, nonce, ver
    """
    params = {}
    if query_params:
        for k, v in query_params.items():
            params[k] = v

    params["ts"] = str(int(time.time()))
    nonce = uuid.uuid4().hex
    params["nonce"] = nonce
    params["ver"] = "2023-08-28"

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
    仿照 SignInterceptor.getSign 实现
    SHA-256 签名
    """
    decoded = unquote(data)
    sha256 = hashlib.sha256(decoded.encode("utf-8")).digest()
    return sha256.hex()


def sign_url(method: str, host: str, path: str, query_params: dict | None = None, port: int | None = None) -> str:
    """
    对请求URL进行签名
    返回带签名的完整URL
    """
    sorted_query = get_sort_parameters(query_params)

    if port and port not in (80, 443):
        base = f"{host}:{port}{path}?{sorted_query}"
    else:
        base = f"{host}{path}?{sorted_query}"

    sign_str = get_sign(f"{method}{base}")
    return f"{base}&sig={sign_str}"
