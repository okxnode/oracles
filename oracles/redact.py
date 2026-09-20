"""脱敏工具。

**这个模块的存在意义**：本项目要开源到 GitHub，而 OCI 的报错信息里
经常夹带 OCID、fingerprint 甚至完整的 PEM 私钥内容。

所以规则是：
  1. 所有日志输出 → 过 `RedactingFilter`
  2. 所有发往 Telegram 的文本 → 过 `scrub()`
  3. 所有写入审计/备份文件的内容 → 过 `scrub()`

宁可把不敏感的东西也打码，也不能漏一个。
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------
#  匹配规则
# --------------------------------------------------------------------------

# OCID：ocid1.tenancy.oc1..aaaa...（类型 + 区域 + 随机串）
_RE_OCID = re.compile(r"\bocid1\.[a-z0-9_-]+\.[a-z0-9_-]+\.[A-Za-z0-9._-]+")

# PEM 私钥整块
_RE_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

# 16 字节指纹 aa:bb:cc:...
_RE_FINGERPRINT = re.compile(r"\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){15}\b")

# Telegram Bot Token：<数字>:<base64 串>
# ⚠️ 长度写成区间而不是精确的 {35}。实测 BotFather 发的 token 长度会浮动
#    （见过 34 位和 35 位），写死会漏掉。宁可多打码，不可漏一个。
_RE_TG_TOKEN = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,45}\b")

# OCI Customer Secret Key 的 id = S3 Access Key（40 位十六进制）
_RE_S3_ACCESS_KEY = re.compile(r"\b[0-9a-f]{40}\b")

# base64 形态的密钥（44 字符，含一个 = 结尾）
_RE_B64_SECRET = re.compile(r"\b[A-Za-z0-9+/]{43}=\b")

_RULES: list[tuple[re.Pattern[str], str]] = [
    (_RE_PEM, "<PEM 私钥已隐藏>"),
    (_RE_OCID, "<OCID 已隐藏>"),
    (_RE_FINGERPRINT, "<指纹已隐藏>"),
    (_RE_TG_TOKEN, "<Telegram Token 已隐藏>"),
    (_RE_S3_ACCESS_KEY, "<S3 Access Key 已隐藏>"),
    (_RE_B64_SECRET, "<密钥已隐藏>"),
]


def scrub(text: str) -> str:
    """把文本里所有敏感片段替换成占位符。"""
    if not text:
        return text
    out = str(text)
    for pattern, replacement in _RULES:
        out = pattern.sub(replacement, out)
    return out


def mask(value: str | None, head: int = 6, tail: int = 4) -> str:
    """保留首尾、中间打码。用于「必须让人认出是哪个」但「不能泄露全量」的场景。

    >>> mask("ocid1.tenancy.oc1..aaaaaaaabbbbcccc")
    'ocid1.…cccc'
    """
    if not value:
        return "-"
    s = str(value)
    if len(s) <= head + tail + 1:
        return s[0] + "*" * (len(s) - 1) if len(s) > 1 else "*"
    return f"{s[:head]}…{s[-tail:]}"


def short_ocid(value: str | None) -> str:
    """把 OCID 压缩成「类型 + 尾 6 位」，用于列表展示。

    >>> short_ocid("ocid1.instance.oc1.some-region-1.abcdefghijklmnop")  # secret-scan:allow
    'instance…klmnop'
    """
    if not value:
        return "-"
    m = _RE_OCID.match(str(value))
    if not m:
        return mask(value)
    kind = str(value).split(".")[1] if "." in str(value) else "?"
    return f"{kind}…{str(value)[-6:]}"
