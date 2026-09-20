"""``execute_launch`` 的行为契约 —— 尤其是「什么算开机失败」。

## 为什么需要这个文件

``execute_launch`` 在 2026-09-20 之前**一条测试都没有**，
而它恰好是那个最贵的 bug 的所在地：``wait_for_state`` 因为 SDK 语义
（传 list 而非 tuple）必然超时，而 ``execute_launch`` 没包 ``try`` ——
于是**机器开出来了、函数却抛异常**，抢机循环把它当成「这次没抢到」，
**下一轮再开一台**。

所以这里锁两条**方向相反**的契约，缺一不可：

| 契约 | 为什么 |
| --- | --- |
| 等待超时 **不算**开机失败 | 否则会重复开机（A1 有 2 核，会真的开出重复实例） |
| ``launch_instance`` 本身失败 **必须**抛出去 | 否则「额度满 / 容量不足」会被吞成成功，抢机循环会以为开好了 |

只锁第一条 = 可能把真实失败也吞掉；只锁第二条 = 重复开机的 bug 会回来。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from oracles.errors import OciApiError
from oracles.oci_gateway import AccountClient
from oracles.services import compute as compute_svc
from oracles.services.compute import LaunchPlan, execute_launch
from tests.telegram_harness import make_account, make_settings

LAUNCHED_ID = "ocid1.instance.oc1..EXAMPLEjustlaunchedinstance"
#: ⚠️ 假 OCID 里**必须**带 `EXAMPLE`。
#:    `scripts/check_secrets.sh` 的 OCID 规则是
#:    ``ocid1\.[a-z0-9_-]+\.[a-z0-9_-]+\.[A-Za-z0-9._-]{20,}`` ——
#:    只要第三段之后有 20+ 个字符就算命中（真实 OCID 的随机段很长）。
#:    第一版把随机段写成 ``example`` + 一个 12 字符的单词，拼起来正好 21 字符，
#:    于是被自己的密钥扫描拦下。仓库的约定是夹具带 `EXAMPLE`
#:    （见 `tests/telegram_harness.py`），白名单按 `EXAMPLE` 过滤。
#:    **不要**改用 `secret-scan:allow` 标记 —— 那是给「必须出现的真形态」
#:    留的口子，这里只是命名习惯问题，用约定就好。
#:    （连注释里都别把那串字面量抄一遍 —— 抄了照样会被扫到。）


def _client() -> AccountClient:
    """真的 ``AccountClient`` —— 只用来提供 ``compartment_id``，不建连。"""
    acc = make_account(1)
    acc.tenancy = "ocid1.tenancy.oc1..exampletenancy"
    return AccountClient(acc, make_settings())


def _plan(**over: object) -> LaunchPlan:
    base: dict[str, object] = {
        "account_index": 1,
        "account_label": "[1] 测试",
        "region": "example-region-1",
        "display_name": "oracles-1-1",
        "availability_domain": "AD-1",
        "shape": "VM.Standard.E2.1.Micro",
        "boot_volume_gb": 50,
        "image_id": "ocid1.image.oc1..exampleimage",
        "image_name": "Canonical-Ubuntu-26.04",
        "subnet_id": "ocid1.subnet.oc1..examplesubnet",
        "subnet_name": "subnet-1",
    }
    base.update(over)
    return LaunchPlan(**base)          # type: ignore[arg-type]


class _FakeCompute:
    """只提供 ``launch_instance`` / ``get_instance`` 两个句柄。

    真正被调用的是 ``client.call(fn, ...)``，而 ``call`` 取 ``fn(...).data`` ——
    所以这里的方法要返回带 ``.data`` 的东西。
    """

    def __init__(self, *, launch_error: Exception | None = None) -> None:
        self.launch_error = launch_error
        self.launched: list[object] = []

    def launch_instance(self, details: object) -> object:
        if self.launch_error is not None:
            raise self.launch_error
        self.launched.append(details)
        return SimpleNamespace(data=SimpleNamespace(
            id=LAUNCHED_ID, display_name="oracles-1-1", lifecycle_state="PROVISIONING"))

    def get_instance(self, instance_id: str) -> object:
        return SimpleNamespace(data=SimpleNamespace(
            id=instance_id, display_name="oracles-1-1", lifecycle_state="PROVISIONING"))


class _FakeClient:
    """最小的假 ``AccountClient``（鸭子类型，不是子类 —— 免得继承到真实现）。"""

    def __init__(self, *, launch_error: Exception | None = None,
                 wait_error: Exception | None = None) -> None:
        self.compute = _FakeCompute(launch_error=launch_error)
        self.compartment_id = "ocid1.compartment.oc1..example"
        self._wait_error = wait_error
        self.wait_calls: list[tuple[str, object]] = []

    def call(self, fn, *args, **kwargs):          # noqa: ANN001, ANN002, ANN003
        try:
            return fn(*args, **kwargs).data
        except OciApiError:
            raise
        except Exception as exc:                  # noqa: BLE001
            raise OciApiError(str(exc)) from exc

    def wait_for_state(self, client, getter, resource_id, target_states, **kwargs):  # noqa: ANN001, ANN002, ANN003
        self.wait_calls.append((resource_id, target_states))
        if self._wait_error is not None:
            raise self._wait_error
        return None


@pytest.fixture(autouse=True)
def _stub_get_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """``get_instance`` 会去查实例池成员和公网 IP，跟本文件的契约无关，统一桩掉。

    ⚠️ 桩的是**边界函数**（另一个 service 函数），不是被测函数 ``execute_launch``
       —— 见踩坑清单 #39「桩打得太高，会把被测行为一起桩掉」。
    """
    monkeypatch.setattr(
        compute_svc, "get_instance",
        lambda client, iid: SimpleNamespace(
            id=iid, display_name="oracles-1-1", lifecycle_state="PROVISIONING"),
    )


# --------------------------------------------------------------------------
#  契约一：等待超时不算开机失败
# --------------------------------------------------------------------------
def test_wait_timeout_is_not_a_launch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 核心回归。

    修复前 ``execute_launch`` 直接把这个异常抛出去 → 抢机循环认为没抢到
    → **下一轮再开一台**。修复后必须：**返回实例**，只记一条警告。
    """
    seen: list[str] = []
    monkeypatch.setattr(compute_svc.log, "warning",
                        lambda msg, *a, **k: seen.append(msg % a if a else msg))

    fake = _FakeClient(wait_error=OciApiError("等待资源状态超时/失败：Maximum wait time"))
    view = execute_launch(fake, _plan())          # ← 不能抛

    assert view.id == LAUNCHED_ID
    assert len(fake.compute.launched) == 1, "应该只调用了一次 launch_instance"
    assert any("RUNNING" in s and "超时" in s for s in seen), \
        f"应该记一条「等待 RUNNING 超时」的警告，实际：{seen}"


def test_wait_is_attempted_with_a_running_target() -> None:
    """等待的目标状态必须是 RUNNING，而且是以**位置参数**传的。

    这条同时守住接线：``wait_for_state(client, getter, resource_id, states)``。
    """
    fake = _FakeClient()
    execute_launch(fake, _plan(), wait=False)     # 先看 wait=False 不等待
    assert fake.wait_calls == []

    fake2 = _FakeClient()
    execute_launch(fake2, _plan(), wait=True)
    assert len(fake2.wait_calls) == 1
    resource_id, states = fake2.wait_calls[0]
    assert resource_id == LAUNCHED_ID
    # ⚠️ 别写 `tuple(states)` —— states 可能是裸字符串 "RUNNING"，
    #    那样会拆成 ('R','U','N',...) 每个字符。
    targets = states if isinstance(states, (list, tuple, set, frozenset)) else {states}
    assert "RUNNING" in targets, f"等待的目标状态不对：{states!r}"


# --------------------------------------------------------------------------
#  契约二：真实的开机失败**必须**抛出去（不能被上面那条改动顺带吞掉）
# --------------------------------------------------------------------------
def test_launch_api_failure_still_raises() -> None:
    """``launch_instance`` 自己失败时，异常必须原样传出。

    ⚠️ 这条是**反向对照**：只有它存在，「等待超时不算失败」这条改动
       才不至于退化成「所有失败都不算失败」。
       （额度满、容量不足、参数非法都走这条路。）
    """
    fake = _FakeClient(launch_error=OciApiError(
        "OCI 接口返回错误：LimitExceeded (HTTP 400)\n原始信息：quota exceeded"))
    with pytest.raises(OciApiError, match="LimitExceeded"):
        execute_launch(fake, _plan())
    assert fake.wait_calls == [], "开机都没成功，不该去等状态"


def test_sdk_parameter_errors_are_not_swallowed() -> None:
    """参数名写错（``TypeError``）也必须炸出来，不能被当成「已受理」。

    历史：``user_data`` 写成顶层参数会抛
    ``TypeError: Unrecognized keyword arguments: user_data`` ——
    如果这里吞了，会变成「静默开出一台没有密码的机器」。
    """
    fake = _FakeClient(launch_error=TypeError("Unrecognized keyword arguments: user_data"))
    with pytest.raises(OciApiError, match="Unrecognized keyword"):
        execute_launch(fake, _plan())


# --------------------------------------------------------------------------
#  接线：user_data 落点（与 tests/test_launch_details.py 呼应）
# --------------------------------------------------------------------------
def test_user_data_rides_in_metadata_all_the_way_to_launch() -> None:
    """端到端接线：plan 上的 ``user_data_b64`` 必须出现在**发出的请求体**里。

    ``tests/test_launch_details.py`` 校验的是 kwargs 的形状；
    这条校验的是 ``execute_launch`` 真的把那份 kwargs 交给了 SDK。
    两段分开测，才能区分「构造错了」和「构造对了但没传」。
    """
    fake = _FakeClient()
    execute_launch(fake, _plan(user_data_b64="I2Nsb3VkLWNvbmZpZwo="), wait=False)

    assert len(fake.compute.launched) == 1
    details = fake.compute.launched[0]
    assert details.metadata["user_data"] == "I2Nsb3VkLWNvbmZpZwo="
    # 顶层**不能**有 user_data
    assert not hasattr(details, "user_data") or "user_data" not in getattr(
        details, "__dict__", {})
