"""异常体系。

统一把 OCI SDK 抛出的 `ServiceError` 翻译成**人能看懂的中文**，
并把常见错误码映射成「下一步该怎么办」，而不是把原始 JSON 甩给用户。
"""
from __future__ import annotations

from typing import Any


class OraclesError(Exception):
    """本项目所有异常的基类。"""


class ConfigError(OraclesError):
    """配置缺失或非法（缺 token、账号清单格式错、私钥不存在等）。"""


class OciApiError(OraclesError):
    """OCI 接口调用失败。message 已经是翻译过的中文提示。

    ⚠️ ``code`` / ``status`` 必须**原样带过来** —— 判定「这个错误值不值得
       继续重试」靠的就是它们（:func:`is_retryable`）。

       2026-09-21 实测的坑：包装时只传了 ``describe_service_error(exc)``
       这个**字符串**，``code`` 就丢了。于是 ``is_retryable`` 读到的
       永远是 ``None`` → **恒为「可重试」** → :data:`NON_RETRYABLE_CODES`
       那张表从来没生效过。而每一段代码看起来都对：
       表是对的、判定函数是对的、循环里也确实调用了它。
       **断点在「字段在包装时被丢掉」这一跳。** 见踩坑 #60。
    """

    def __init__(self, message: str, *, code: str | None = None,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class NotAllowedError(OraclesError):
    """操作被安全开关拒绝（非白名单用户、写操作被禁用等）。"""


class DryRun(OraclesError):
    """DRY-RUN 预演结束，未真正执行。

    用异常而非返回值传递，是为了保证**任何**调用路径都无法
    「不小心继续往下走」把写操作执行掉。
    """

    def __init__(self, plan: str):
        super().__init__(plan)
        self.plan = plan


# --------------------------------------------------------------------------
#  OCI 错误码 → 中文解释 + 建议动作
# --------------------------------------------------------------------------
#  只收录实际踩过的坑。没收录的错误码会退化成「原始 code + message」。
_ERROR_HINTS: dict[str, str] = {
    "LimitExceeded": (
        "配额已满。注意免费额度是**按可用域(AD)绑定**的，不是整个区域：\n"
        "换一个 AD 再试，或用 /quota 看哪个 AD 还有余量。\n"
        "若报的是 bootVolumeQuota，那是**块存储**额度满（不是 CPU），"
        "去 /audit 找孤儿引导卷。"
    ),
    "OutOfHostCapacity": (
        "该 AD 物理主机容量暂时不足。这不是配额问题，换个 AD 或过一会儿重试。\n"
        "若所有 AD 都报这个，说明确实没机器了。"
    ),
    "NotAuthenticated": (
        "认证失败。逐项检查：API 私钥文件路径是否正确、fingerprint 是否与"
        "控制台里那把公钥一致、系统时间是否偏差过大。"
    ),
    "NotAuthorizedOrNotFound": (
        "无权访问或资源不存在。常见原因：compartment OCID 填错，"
        "或该 API Key 没有对应权限策略。"
    ),
    "TooManyRequests": "触发 OCI 限流。降低并发（ORACLES_MAX_CONCURRENCY）后重试。",
    "Conflict": "资源状态冲突（同名已存在，或仍在删除中）。",
    "PreauthenticatedRequestStillExists": (
        "桶上还挂着预认证请求(PAR)记录，OCI 拒绝删除。\n"
        "⚠️ 过期的 PAR 同样会阻塞。需先删 PAR 再删桶，/rmbucket 会自动处理。"
    ),
    "BucketNotEmpty": "桶里还有对象，必须先清空才能删除。",
    "InvalidParameter": "参数非法（limit 名写错、缺必填字段等）。",
    "QuotaExceeded": "超出了租户配额。",
    "CannotParseRequest": "请求体无法解析，通常是字段名或类型不对。",
    "InternalError": (
        "OCI 侧返回 500。**先看上面的「原始信息」**，两种可能完全不同：\n"
        "  · 写着 `Out of host capacity` → **该可用域暂时没有这个规格的物理主机**。\n"
        "    这不是配额问题、也不是你的配置问题 —— ARM（A1.Flex）尤其常见。\n"
        "    → 用「🔁 自动抢机」挂着等别人释放，或换一个 AD / 换账号。\n"
        "  · 其他原始信息 → OCI 侧偶发故障，重试通常可恢复。"
    ),
    "MethodNotAllowed": "该资源不支持此操作。",
    "InvalidatedRetryToken": "重试令牌失效，重新发起即可。",
}


# --------------------------------------------------------------------------
#  「原始信息」里出现的这几个词，说明是**容量**问题而不是 OCI 故障
# --------------------------------------------------------------------------
#  ⚠️ 2026-09-21 实测：ARM（A1.Flex）没容量时，OCI 返回的是
#     `InternalError` + HTTP 500，**不是**文档里的 `OutOfHostCapacity`。
#     光看错误码完全分不出「没容量」和「OCI 挂了」——
#     唯一的线索就是 message 里这句话。所以必须把它捞出来。
#     护栏：tests/test_errors.py
_CAPACITY_MARKERS: tuple[str, ...] = (
    "out of host capacity",
    "outofhostcapacity",
    "host capacity",
)


def _looks_like_capacity_shortage(message: str) -> bool:
    """原始信息里是否在说「物理主机容量不足」。"""
    lowered = message.lower()
    return any(marker in lowered for marker in _CAPACITY_MARKERS)


def describe_service_error(exc: Any) -> str:
    """把 oci.exceptions.ServiceError 转成一段可读文本。

    ⚠️ 返回的是**多行**文本（错误码 / 原始信息 / 建议）。
       要放进单行位置（日志、表格单元格、按钮标签）时，
       **必须**显式调用 :func:`one_line` —— 别用 ``splitlines()[0]``，
       那会把「原始信息」和「建议」一起丢掉。
       2026-09-21 实测：`grab.py` 里正是这么丢的，用户只看到
       「OCI 接口返回错误：InternalError (HTTP 500)」，
       而真正的原因「Out of host capacity」被扔了。
    """
    code = getattr(exc, "code", None) or "UnknownError"
    message = getattr(exc, "message", None) or str(exc)
    status = getattr(exc, "status", None)

    lines = [f"OCI 接口返回错误：{code}" + (f" (HTTP {status})" if status else "")]
    lines.append(f"原始信息：{message}")

    # 容量不足优先于通用建议 —— 它需要的是「换个思路」而不是「重试一下」
    if _looks_like_capacity_shortage(str(message)):
        lines.append(
            "建议：**该可用域暂时没有物理容量**（原始信息已明说）。\n"
            "  这不是配额、也不是配置问题 —— ARM（A1.Flex）尤其常见，\n"
            "  免费账号常要等几天。→ 用「🔁 自动抢机」挂着等释放，\n"
            "  或换 AD / 换账号。反复点「现在开机」不会有不同结果。"
        )
        return "\n".join(lines)

    hint = _ERROR_HINTS.get(code)
    if hint:
        lines.append(f"建议：{hint}")
    return "\n".join(lines)


def to_api_error(exc: Any) -> OciApiError:
    """把 ``oci.exceptions.ServiceError`` 包成 :class:`OciApiError`。

    **这是唯一的包装入口** —— 直接写 ``OciApiError(describe_service_error(exc))``
    会把 ``code`` / ``status`` 丢掉（见 :class:`OciApiError` 的说明）。

    本函数刻意不 import ``oci``：只靠 ``getattr`` 读属性，
    这样 ``errors.py`` 保持零依赖，``oci_gateway`` 仍是唯一碰 SDK 的模块。
    """
    return OciApiError(
        describe_service_error(exc),
        code=getattr(exc, "code", None),
        status=getattr(exc, "status", None),
    )


def one_line(text: Any, limit: int | None = None) -> str:
    """把多行文本压成**一行**，供日志 / 表格 / 单行 UI 使用。

    这是 ``str(exc).splitlines()[0]`` 的**显式替代品** ——
    名字本身就说明了「我知道这里只能放一行」，
    而 ``splitlines()[0]`` 看起来只是「取个摘要」，很容易在
    用户可见的位置误用（2026-09-21 实测，9 处）。

    ⚠️ 全项目**只此一处**允许丢弃第一行之后的内容。
       ``tests/test_errors.py`` 里有一条 AST 护栏盯着这件事：
       别处再出现 ``splitlines()[0]`` 就报红。
    """
    lines = str(text).splitlines()
    first = lines[0] if lines else ""
    return first[:limit] if limit else first


# --------------------------------------------------------------------------
#  哪些错误「再试一万次也不会变」
# --------------------------------------------------------------------------
#: 命中这些码时，抢机循环**必须停**（标 blocked），不能继续睡着重试。
#:
#: ⚠️ 2026-09-21 之前，循环只特判了 `NotAllowedError`（写开关），
#:    其余异常一律当「再等等」。于是 API Key 配错、compartment 写错这类
#:    **永远不会自己好**的错误，会让任务一直空转、日志刷屏，
#:    用户也等不到任何结论。这是「可重试 / 不可重试」散落在
#:    `except` 子句里的典型后果 —— 改成**表驱动**。
NON_RETRYABLE_CODES: frozenset[str] = frozenset({
    "NotAuthenticated",          # API Key / fingerprint / 时间偏差
    "NotAuthorizedOrNotFound",   # compartment OCID 错、无权限策略
    "InvalidParameter",          # 参数写错
    "CannotParseRequest",        # 请求体字段名/类型错
    "MethodNotAllowed",          # 该资源不支持此操作
    "QuotaExceeded",             # 租户配额（跟「AD 余量」不同，改 AD 也没用）
    "InvalidatedRetryToken",     # 重试令牌失效，重发即可但本任务该收尾
})


def is_retryable(exc: Any) -> bool:
    """这个异常值不值得**继续挂着重试**。

    ``True``  = 等一会儿可能就好了（容量不足、限流、OCI 偶发 5xx）
    ``False`` = 重试一万次也不会变（认证、权限、参数、配额）

    判据是 ``code`` **白名单式的类别表**，不是散落的字符串匹配。
    """
    code = getattr(exc, "code", None)
    if code:
        return code not in NON_RETRYABLE_CODES
    # 没有 code 的（本地异常、网络错误）当作可重试 —— 网络抖动是常见的
    return True

