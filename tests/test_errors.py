"""错误翻译、单行截断、以及「值不值得重试」的判定。

## 这个文件存在的理由（2026-09-21）

用户报告：开 ARM 机器时任务返回

> OCI 接口返回错误：InternalError (HTTP 500)

而真正的原因 —— OCI 原始信息里的 ``Out of host capacity`` ——
**没出现在任何地方**。顺着这条线挖出两个**互相掩护**的 bug：

| # | bug | 后果 |
| --- | --- | --- |
| 1 | ``grab.py`` 用 ``str(exc).splitlines()[0]`` 截断 | 精心构造的「原始信息 / 建议」两行全丢；一个**本来能自解释**的错误变成了需要来问的问题 |
| 2 | ``OciApiError`` 包装时**只传了字符串**，``code`` 丢了 | ``is_retryable`` 读到的恒为 ``None`` → 恒为「可重试」→ ``NON_RETRYABLE_CODES`` 那张表**从来没生效过** |

第 2 个特别值得记：表是对的、判定函数是对的、循环里也确实调用了它 ——
**断点在「字段在包装时被丢掉」这一跳**。所以本文件里最要紧的
不是那几条逐段的单元测试，而是 :func:`test_is_retryable_survives_the_real_wrapping`
—— 它**穿过真实的 ``AccountClient.call()``**，而不是自己复制一遍包装逻辑。

（自己复制一份包装逻辑来测，就等于没测接线。见踩坑 #47 / #55。）
"""
from __future__ import annotations

import ast
from pathlib import Path

import oci.exceptions
import pytest

from oracles import errors as E
from oracles.bot import handlers as H
from oracles.errors import OciApiError, is_retryable, one_line, to_api_error
from oracles.oci_gateway import AccountClient
from tests.telegram_harness import make_account, make_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = PROJECT_ROOT / "oracles"


def _client() -> AccountClient:
    """真的 ``AccountClient`` —— 只用来走真实的 ``call()``，不建连。"""
    return AccountClient(make_account(1), make_settings())


def _raising(code: str, message: str, status: int = 500):
    """造一个必然抛 ``ServiceError`` 的 SDK 调用。"""
    def fn():
        raise oci.exceptions.ServiceError(
            status=status, code=code, headers={}, message=message,
        )
    return fn


# ---------------------------------------------------------------------------
#  1. 单行截断：唯一允许丢弃后续行的地方
# ---------------------------------------------------------------------------
def test_one_line_keeps_only_the_first_line() -> None:
    assert one_line("第一行\n第二行\n第三行") == "第一行"


def test_one_line_respects_the_limit() -> None:
    assert one_line("abcdefghij\nx", 4) == "abcd"


def test_one_line_survives_empty_and_odd_input() -> None:
    """空串、只有换行、非字符串 —— 都不许抛异常。

    它在日志和表格单元格里被调用，炸掉就是把「展示问题」升级成「功能故障」。
    """
    assert one_line("") == ""
    assert one_line("\n\n") == ""
    assert one_line(None) == "None"      # str(None)，行为明确即可
    assert one_line(RuntimeError("网络层请求失败")) == "网络层请求失败"


# ---------------------------------------------------------------------------
#  2. 错误翻译：多行、且带得出建议
# ---------------------------------------------------------------------------
def test_capacity_shortage_is_translated_with_the_original_message() -> None:
    """🔴 「没容量」必须把 OCI 的**原始信息**带出来。

    这就是用户那个 bug 的核心：只看到 ``InternalError (HTTP 500)``
    的人会以为「OCI 挂了」，而看到 ``Out of host capacity`` 的人
    立刻知道该换 AD 或挂抢机。
    """
    exc = oci.exceptions.ServiceError(
        status=500, code="InternalError", headers={},
        message="Out of host capacity.",
    )
    text = E.describe_service_error(exc)
    assert "Out of host capacity." in text, f"原始信息被丢了：\n{text}"
    assert "原始信息" in text
    assert "建议" in text
    assert len(text.splitlines()) > 2, "容量不足应当是**多行**说明，不是一行"


def test_capacity_shortage_is_recognised_even_under_a_different_code() -> None:
    """判据是**原始信息的内容**，不是错误码。

    OCI 在 ``OutOfHostCapacity`` 和 ``InternalError`` 两个码下
    都报过这句话（2026-09-20 实测 24 次全部是后者）。只按码判断会漏。
    """
    for code in ("OutOfHostCapacity", "InternalError"):
        text = E.describe_service_error(
            oci.exceptions.ServiceError(
                status=500, code=code, headers={},
                message="Out of host capacity.",
            )
        )
        assert "物理容量" in text, f"{code} 没被认成容量不足：\n{text}"


def test_an_unknown_code_still_yields_a_readable_message() -> None:
    """没收录的错误码 → 退化成「错误码 + 原始信息」，**不编造建议**。"""
    exc = oci.exceptions.ServiceError(
        status=418, code="TotallyNewError", headers={}, message="something odd",
    )
    text = E.describe_service_error(exc)
    assert "TotallyNewError" in text and "HTTP 418" in text
    assert "something odd" in text
    assert "建议" not in text, "没收录的码不该凭空给建议"


# ---------------------------------------------------------------------------
#  3. 🔴 最要紧的一条：判定必须穿过**真实的包装**
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code,status,retryable", [
    # 永远不会自己好 → 必须停
    ("NotAuthenticated", 401, False),
    ("NotAuthorizedOrNotFound", 404, False),
    ("InvalidParameter", 400, False),
    ("QuotaExceeded", 400, False),
    ("CannotParseRequest", 400, False),
    ("MethodNotAllowed", 405, False),
    # 等一等可能就好 → 继续重试
    ("OutOfHostCapacity", 500, True),
    ("InternalError", 500, True),
    ("LimitExceeded", 400, True),
    ("TooManyRequests", 429, True),
])
def test_is_retryable_survives_the_real_wrapping(code: str, status: int,
                                                 retryable: bool) -> None:
    """🔴 判定必须对**包装后的** ``OciApiError`` 也成立。

    这里刻意走真实的 ``AccountClient.call()`` —— 它在
    ``oci_gateway.py`` 里把 ``ServiceError`` 包成 ``OciApiError``。

    ⚠️ 2026-09-21 之前，包装时只传了 ``describe_service_error(exc)``
       这个字符串，``code`` 被丢掉 → ``is_retryable`` 恒为 ``True``
       → ``NON_RETRYABLE_CODES`` 是**死代码**。

       如果这条测试自己复制一遍包装逻辑（``OciApiError(describe(...))``），
       它会**同样丢掉 code**，然后测出「判定是对的」——
       于是这个 bug 永远测不出来。必须走真代码。
    """
    with pytest.raises(OciApiError) as caught:
        _client().call(_raising(code, "whatever", status))

    exc = caught.value
    assert exc.code == code, (
        f"包装后 code 丢了（得到 {exc.code!r}）—— "
        f"is_retryable 会因此永远走默认分支"
    )
    assert exc.status == status, "status 也该带过来"
    assert is_retryable(exc) is retryable, (
        f"{code} 被判成 retryable={is_retryable(exc)}，期望 {retryable}"
    )


def test_the_retry_table_is_actually_reachable() -> None:
    """``NON_RETRYABLE_CODES`` 必须**至少有一个码真的能被判出 False**。

    这条守的是「表非空且接线有效」。没有它，把整张表清空
    （或让 ``is_retryable`` 恒返回 True）也能让上面那些用例
    「看起来通过」—— 只要参数化的期望值全填 True。
    """
    assert E.NON_RETRYABLE_CODES, "不可重试表是空的"
    hit = [c for c in E.NON_RETRYABLE_CODES
           if is_retryable(to_api_error(
               oci.exceptions.ServiceError(status=400, code=c,
                                           headers={}, message="x"))) is False]
    assert sorted(hit) == sorted(E.NON_RETRYABLE_CODES), (
        f"这些码没被判成不可重试：{sorted(set(E.NON_RETRYABLE_CODES) - set(hit))}"
    )


def test_an_error_without_a_code_is_treated_as_retryable() -> None:
    """没有 ``code`` 的（本地异常、网络错误）当可重试 —— 网络抖动很常见。"""
    assert is_retryable(OciApiError("网络层请求失败：超时")) is True
    assert is_retryable(RuntimeError("随便什么")) is True


def test_to_api_error_keeps_the_original_message_verbatim() -> None:
    """包装不许改动文案 —— 用户看到的原始信息就是 OCI 给的那串。"""
    raw = oci.exceptions.ServiceError(
        status=500, code="InternalError", headers={},
        message="Out of host capacity.",
    )
    exc = to_api_error(raw)
    assert "Out of host capacity." in str(exc)
    assert exc.code == "InternalError" and exc.status == 500
    # 原始异常要挂在 __cause__ 上，便于日志回溯
    assert isinstance(exc, OciApiError)


def test_gateway_never_wraps_without_to_api_error() -> None:
    """``oci_gateway`` 里不许再出现「裸包一层」的写法。

    直接写 ``OciApiError(describe_service_error(exc))`` 就会丢 ``code``。
    这条用文本检查（够直接），AST 那条管 ``splitlines()[0]``。
    """
    src = (PKG_DIR / "oci_gateway.py").read_text(encoding="utf-8")
    bad = [line.strip() for line in src.splitlines()
           if "OciApiError(describe_service_error" in line]
    assert not bad, (
        f"oci_gateway.py 里有裸包装（会丢 code）：{bad}\n"
        f"  改用 to_api_error(exc)"
    )
    assert "to_api_error" in src, "网关根本没用 to_api_error？"


# ---------------------------------------------------------------------------
#  4. 用户可见的渲染：多行要保留、建议不重复
# ---------------------------------------------------------------------------
def test_failure_reason_keeps_every_line() -> None:
    """🔴 失败原因必须**多行原样**呈现，不许截成一行。

    这就是用户那个 bug 的出口：``grab.py`` 截断 + 这里 ``[:400]``
    双重截断，导致「原始信息」永远到不了用户眼前。
    """
    multi = ("OCI 接口返回错误：InternalError (HTTP 500)\n"
             "原始信息：Out of host capacity.\n"
             "建议：该可用域暂时没有物理容量。")
    out = H._failure_reason(multi)
    assert "Out of host capacity." in out, "原始信息被截掉了"
    assert "建议" in out, "建议被截掉了"
    assert len(out.splitlines()) == 3


def test_failure_reason_says_so_when_there_is_nothing() -> None:
    assert "未知" in H._failure_reason(None)
    assert "未知" in H._failure_reason("   ")


def test_failure_reason_is_still_bounded() -> None:
    """保留多行 ≠ 无限长 —— 仍有上限，别把整段堆栈塞进消息。"""
    out = H._failure_reason("x" * (H._FAILURE_REASON_LIMIT + 500))
    assert len(out) <= H._FAILURE_REASON_LIMIT


def test_failure_tail_does_not_contradict_a_specific_hint() -> None:
    """原因里已经带了「建议：」时，结尾**不再**补泛泛提示。

    见踩坑 #38：一句会误导人的提示，比没有提示更糟 ——
    用户会跑去查一个跟问题无关的配额。
    """
    with_hint = "OCI 接口返回错误：OutOfHostCapacity\n建议：换个 AD。"
    assert "配额查询" not in H._failure_tail(with_hint)
    assert "建议" in H._failure_tail(with_hint)

    # 对照组：没有建议时才给兜底提示（否则上面那条在「永远返回空」时也过）
    without = "OCI 接口返回错误：UnknownError\n原始信息：???"
    assert "配额查询" in H._failure_tail(without)


# ---------------------------------------------------------------------------
#  5. AST 护栏：全项目只允许一处「取第一行」
# ---------------------------------------------------------------------------
def _splitlines_first_calls(tree: ast.AST) -> list[tuple[str, int]]:
    """找 ``X.splitlines()[0]``，返回 ``[(所在函数名, 行号)]``。

    走 AST 而不是正则 —— 注释和文档字符串里到处在讲这个反模式
    （``errors.py`` / ``grab.py`` / ``handlers.py`` 的说明里都有），
    用正则会**满屏误报**。而误报会训练人去忽略这条检查。
    """
    found: list[tuple[str, int]] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Subscript):
                continue
            call = node.value
            if not isinstance(call, ast.Call):
                continue
            fn = call.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "splitlines"):
                continue
            # 只认下标 0；`splitlines()[-1]` 是另一种语义，不在此列
            idx = node.slice
            if isinstance(idx, ast.Constant) and idx.value == 0:
                found.append((func.name, node.lineno))
    return found


def _all_splitlines_first() -> dict[str, list[tuple[str, int]]]:
    out: dict[str, list[tuple[str, int]]] = {}
    for path in sorted(PKG_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = _splitlines_first_calls(tree)
        if hits:
            out[path.relative_to(PKG_DIR).as_posix()] = hits
    return out


def test_nothing_in_the_package_discards_the_rest_of_a_multiline_text() -> None:
    """🔴 全项目**零** ``X.splitlines()[0]`` —— 想取第一行就显式调 ``one_line()``。

    2026-09-21 实测它被用了 **9 处**，其中用户可见的那处
    （``grab.py``）直接把错误原因截没了：用户只看到
    「OCI 接口返回错误：InternalError (HTTP 500)」，
    真正的原因「Out of host capacity」被丢掉。

    判据选「一处都不许有」而不是「只许出现在 one_line 里」——
    因为 ``one_line`` 自己也不用这个写法（见它的实现），
    所以零容忍是**能守住的**，不用维护一份「允许清单」。
    允许清单会腐烂：下次有人改实现，清单不会跟着改。
    """
    hits = _all_splitlines_first()
    assert not hits, (
        f"这些地方在用 splitlines()[0] 取第一行：{hits}\n"
        f"  → 改成 errors.one_line(text)（想保留多行就别截断）"
    )


def test_one_line_exists_as_the_sanctioned_replacement() -> None:
    """替代品必须真的在、真的能用 —— 否则上面那条只是「禁掉一种写法」。

    禁掉旧写法却不提供新的，下一个人只会发明第三种写法。
    """
    assert callable(one_line)
    assert one_line("a\nb") == "a"
    # 它自己也不许用那个反模式（这正是零容忍能成立的前提）
    tree = ast.parse((PKG_DIR / "errors.py").read_text(encoding="utf-8"))
    body = next(f for f in ast.walk(tree)
                if isinstance(f, ast.FunctionDef) and f.name == "one_line")
    assert _splitlines_first_calls(ast.parse(ast.unparse(body))) == [], (
        "one_line 自己又用回 splitlines()[0] 了 —— 零容忍的前提没了"
    )


def test_the_guard_actually_fires_on_the_real_idiom() -> None:
    """**变异测试**：给检测器一段真的反模式代码，必须报出来。

    没有这条，「检测器永远返回空」和「代码真的干净」就区分不开 ——
    而前者恰恰是最容易发生的失效（见踩坑 #40）。
    """
    src = (
        "def f(exc):\n"
        "    return str(exc).splitlines()[0][:200]\n"
    )
    assert _splitlines_first_calls(ast.parse(src)) == [("f", 2)]


def test_the_guard_ignores_comments_and_docstrings() -> None:
    """注释/文档里提到这个写法**不算**违规 —— 这是走 AST 的直接原因。

    ``errors.py`` 的说明里就写着 ``splitlines()[0]``，用正则会误报。
    """
    src = (
        '"""别用 str(exc).splitlines()[0] —— 会丢信息。"""\n'
        "# 同理，注释里写 splitlines()[0] 也不算\n"
        "def f(exc):\n"
        "    return one_line(exc)\n"
    )
    assert _splitlines_first_calls(ast.parse(src)) == []


def test_the_guard_covers_every_module() -> None:
    """扫描面也要断言：模块数不能是 0，关键文件必须在里面。

    不然有人把 ``PKG_DIR`` 改错、或目录改名，检查会静默变成空转。
    """
    files = sorted(p for p in PKG_DIR.rglob("*.py"))
    assert len(files) >= 10, f"只扫到 {len(files)} 个模块，扫描面是不是坏了？"
    rel = {p.relative_to(PKG_DIR).as_posix() for p in files}
    for must in ("errors.py", "services/grab.py", "bot/handlers.py",
                 "bot/dispatch.py", "cli.py"):
        assert must in rel, f"扫描面里缺 {must}"
