"""``wait_for_state`` 的回归测试 —— 喂**真实 SDK** 的 ``oci.wait_until``。

## 为什么不能 mock ``oci.wait_until``

2026-09-20 真机验证撞出的 bug 是「传 ``list`` 而不是 ``tuple``」。
``oci.wait_until`` 内部对多值只认 ``tuple``，``list`` 会掉进单值比较分支::

    if isinstance(state, tuple):
        if getattr(response.data, property) in state:   # ← 多值
            return response
    elif getattr(response.data, property) == state:     # ← 单值
        return response

于是 ``["RUNNING"]`` 走的是 ``"RUNNING" == ["RUNNING"]`` —— 永远为假，
一直轮询到 ``MaximumWaitTimeExceeded``。

**如果 mock 掉 ``oci.wait_until``，这个 bug 完全不可观测** ——
mock 只会记录「被调用了」，不会复现它的比较语义。
（这正是踩坑清单 #47：假对象只记录调用、不校验入参。）

所以这里搭一个最小的假客户端，让**真实的** ``oci.wait_until`` 跑起来。
它要求的接口面很窄，只需要三样东西：

  · ``response.request.method``            —— 必须是 ``get``（SDK 只支持 GET 轮询）
  · ``response.data.<property>``           —— 被比较的字段
  · ``client.base_client.request(request)`` —— 重新拉取时调这个

⚠️ 每个用例都会**真的 sleep**（``oci.wait_until`` 的退避是 1s → 2s → …）。
   所以 ``max_wait_seconds`` 都压到几秒，整个文件约 5 秒。
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from oracles.errors import OciApiError
from oracles.oci_gateway import AccountClient
from tests.telegram_harness import make_account, make_settings

GATEWAY = Path(__file__).resolve().parent.parent / "oracles" / "oci_gateway.py"

# --------------------------------------------------------------------------
#  最小假客户端（只为喂饱真实的 oci.wait_until）
# --------------------------------------------------------------------------
INSTANCE_ID = "ocid1.instance.oc1..EXAMPLEwaittestinstance"
#: ⚠️ 带 `EXAMPLE` 是仓库约定 —— 密钥扫描的 OCID 规则会命中「第三段之后
#:    有 20+ 字符」的字符串，白名单按 `EXAMPLE` 过滤（见 telegram_harness）。


class _FakeRequest:
    """``oci.wait_until`` 会检查 ``response.request.method`` 必须是 get。"""

    method = "GET"


class _FakeResponse:
    def __init__(self, state: str) -> None:
        self.request = _FakeRequest()
        self.data = SimpleNamespace(lifecycle_state=state)


class _FakeBaseClient:
    """``oci.wait_until`` 通过 ``client.base_client.request(request)`` 重新拉取。

    依次返回 ``states`` 里的值，用完就停在最后一个（模拟「卡住不变」）。
    """

    def __init__(self, states: list[str]) -> None:
        self._states = states
        self.calls = 0

    def request(self, request: object) -> _FakeResponse:  # noqa: ARG002 —— 只需签名
        idx = min(self.calls, len(self._states) - 1)
        self.calls += 1
        return _FakeResponse(self._states[idx])


class _FakeClient:
    def __init__(self, states: list[str]) -> None:
        self.base_client = _FakeBaseClient(states)


def _account_client() -> AccountClient:
    """真的 ``AccountClient`` 实例 —— ``__init__`` 只存字段，不建连、不读密钥。"""
    return AccountClient(make_account(1), make_settings())


def _initial(state: str = "PROVISIONING"):
    """第一次响应（``wait_until`` 的入参就是一个 Response 对象）。"""
    return lambda _resource_id: _FakeResponse(state)


# --------------------------------------------------------------------------
#  核心回归
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "given",
    [
        pytest.param("RUNNING", id="裸字符串"),
        pytest.param(["RUNNING"], id="单元素-list（🔴 原来线上就是这种）"),
        pytest.param(("RUNNING",), id="单元素-tuple"),
        pytest.param({"RUNNING"}, id="set"),
        pytest.param(iter(["RUNNING"]), id="迭代器"),
    ],
)
def test_any_iterable_of_states_actually_converges(given) -> None:
    """🔴 核心回归：**传 list 也必须能等到**。

    修复前 ``list(target_states)`` 会一路传到 ``oci.wait_until``，
    而它只认 ``tuple`` → ``"RUNNING" == ["RUNNING"]`` 恒假 → 必然超时。
    这条用例在修复前会以 ``OciApiError: 等待资源状态超时/失败`` 失败。
    """
    fake = _FakeClient(["PROVISIONING", "RUNNING", "RUNNING"])
    resp = _account_client().wait_for_state(
        fake, _initial(), INSTANCE_ID, given,
        max_wait_seconds=15, max_interval_seconds=1,
    )
    assert resp.lifecycle_state == "RUNNING"
    # 至少重新拉取过一次 —— 证明它真的在轮询，不是拿第一次的响应糊弄过去
    assert fake.base_client.calls >= 1


def test_multiple_target_states_converges_on_either() -> None:
    """多目标状态（关机时可能是 STOPPING → STOPPED）。

    ⚠️ 这一条**只有** tuple 才能过：list 会掉进单值比较分支，
    连多值包含都不会发生。
    """
    fake = _FakeClient(["PROVISIONING", "STOPPING", "STOPPED"])
    resp = _account_client().wait_for_state(
        fake, _initial(), INSTANCE_ID, ["RUNNING", "STOPPED"],
        max_wait_seconds=15, max_interval_seconds=1,
    )
    assert resp.lifecycle_state == "STOPPED"


def test_returns_the_resource_not_the_response() -> None:
    """🔴 **返回值必须是资源对象（``.data``），不是 ``Response`` 包装。**

    2026-09-20 撞出来的第二个坑。``oci.wait_until`` 返回的是 ``Response``，
    而本模块 ``client.call`` 一律返回 ``.data`` —— 两种约定混在一起，
    第一个想用返回值的人必然踩::

        conn = client.wait_for_state(...)      # 拿到 Response
        conn.lifecycle_state                   # AttributeError

    真实报错是 ``'Response' object has no attribute 'lifecycle_state'``，
    而 ``Response`` 这个类名在调用点附近根本没出现过，极难定位。

    **这个坑能潜伏这么久的原因**：之前 3 处调用全都丢弃返回值
    （``execute_launch`` / ``execute_power`` / ``execute_terminate``），
    等于这条约定从来没被验证过。所以这条断言不是「锦上添花」——
    它守的是一个**没人看过**的接口。

    写法上刻意断言「有 lifecycle_state、没有 data」两个方向：
    只断言前者的话，返回一个**既有 `.data` 又有 `.lifecycle_state`** 的
    对象也能过，那就没测到「有没有解包」这件事。
    """
    fake = _FakeClient(["PROVISIONING", "RUNNING"])
    got = _account_client().wait_for_state(
        fake, _initial(), INSTANCE_ID, "RUNNING",
        max_wait_seconds=15, max_interval_seconds=1,
    )
    assert got.lifecycle_state == "RUNNING", (
        "返回的对象上取不到 lifecycle_state —— 多半是没解包 .data"
    )
    assert not hasattr(got, "data"), (
        "返回的像是 Response 包装（它带 .data）—— 应该解包成资源对象，"
        "与 client.call 的约定保持一致"
    )
    assert not hasattr(got, "request"), "返回的对象带 .request，那是 Response 的特征"


def test_empty_states_raises_instead_of_hanging() -> None:
    """空集合要**立刻报错**：``x in ()`` 恒为假，同样是死等。

    修复前这里会安静地睡满 ``max_wait_seconds``（线上是 420~600 秒）。
    """
    fake = _FakeClient(["RUNNING"])
    with pytest.raises(ValueError, match="至少要有一个目标状态"):
        _account_client().wait_for_state(
            fake, _initial(), INSTANCE_ID, [],
            max_wait_seconds=1, max_interval_seconds=1,
        )


def test_never_reaching_the_state_raises_ociapierror() -> None:
    """状态永远不到 → 翻成 ``OciApiError``（而不是把 ``MaximumWaitTimeExceeded`` 漏出去）。

    ⚠️ 注意这条**只验证错误翻译**，不验证「能不能等到」——
    后者由上面两条负责。别把两者混在一起：否则「永远超时」也会让这条通过。
    """
    fake = _FakeClient(["PROVISIONING"])       # 永远停在 PROVISIONING
    with pytest.raises(OciApiError, match="等待资源状态超时"):
        _account_client().wait_for_state(
            fake, _initial(), INSTANCE_ID, "RUNNING",
            max_wait_seconds=2, max_interval_seconds=1,
        )


# --------------------------------------------------------------------------
#  接线层：确保调用方不会再退回 list
# --------------------------------------------------------------------------
def test_gateway_source_does_not_pass_a_list_to_wait_until() -> None:
    """源码级检查：``wait_for_state`` 传给 ``oci.wait_until`` 的状态必须是 ``tuple``。

    行为测试已经能拦住它，这条是**双保险** —— 它毫秒级、不 sleep，
    而且报错信息直接指向那个 SDK 语义坑，比行为测试的「超时」好排查得多。

    ⚠️ 这里走 **AST** 而不是文本匹配。第一版用
    ``assert "list(target_states)" not in text``，结果被**本文件自己的
    docstring**误伤（docstring 里正好引用了这个错误写法）。
    **会误报的检查会被训练着去忽略**，所以改成只看真实的调用节点。
    """
    tree = ast.parse(GATEWAY.read_text(encoding="utf-8"), filename=str(GATEWAY))

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "wait_until"
    ]
    assert len(calls) == 1, f"网关里应该有且只有一个 oci.wait_until 调用，找到 {len(calls)} 个"

    state_arg = calls[0].args[3]        # (client, response, property, state, ...)
    assert isinstance(state_arg, ast.Name) and state_arg.id == "states", (
        "传给 oci.wait_until 的 state 不是一个已归一化的名字 —— "
        "是不是又直接把外部入参塞进去了？"
    )

    assigns = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "states" for t in node.targets)
    ]
    assert assigns, "找不到 `states = ...` 的赋值"
    assert any(
        isinstance(a.value, ast.Call)
        and isinstance(a.value.func, ast.Name)
        and a.value.func.id == "tuple"
        for a in assigns
    ), (
        "`states` 不是用 tuple(...) 造出来的 —— oci.wait_until 只认 tuple，"
        "传 list 会永远等不到（见本文件模块 docstring）。"
    )
