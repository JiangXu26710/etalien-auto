"""手机号脱敏工具模块。

提供日志/通知输出场景下的手机号脱敏能力，避免 cli.log 长期轮转保留导致
手机号明文累积（隐私泄露风险）。main.py 与 core 模块共用此模块。

设计要点：
  - mask_phone：将 11 位手机号转为 ``138****1234`` 格式（保留前 3 后 4）
  - mask_label：对 label 中的所有 11 位手机号子串做脱敏
  - 正则使用前后断言 ``(?<!\\d)...(?!\\d)`` 避免误伤长数字串
    （如 12+ 位订单号、时间戳等不应被部分脱敏）
"""
import re

# 中国大陆 11 位手机号正则（前后断言避免误伤长数字串）
# 1 开头，第二位 3-9，共 11 位数字，前后均不能是数字
_PHONE_RE = re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)')


def mask_phone(phone: str) -> str:
    """将单个手机号脱敏为 ``138****1234`` 格式（保留前 3 后 4）。

    Args:
        phone: 11 位手机号字符串（假设调用方已校验长度与格式）

    Returns:
        脱敏后的字符串，如 ``138****1234``
    """
    if not phone or len(phone) < 7:
        # 不足 7 位时无法保留前 3 后 4，原样返回（防御性兜底）
        return phone
    return phone[:3] + '****' + phone[-4:]


def mask_label(label: str) -> str:
    """对 label 中的所有 11 位手机号子串做脱敏。

    用于日志输出，避免 cli.log 长期轮转保留导致手机号泄露。

    Args:
        label: 可能包含手机号的字符串（如 ``"测试 13800138000 (备注)"``）

    Returns:
        所有 11 位手机号子串被脱敏后的字符串（如 ``"测试 138****8000 (备注)"``）
    """
    return _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), label)
