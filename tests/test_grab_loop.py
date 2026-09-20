"""抢机循环（``run_grab_loop``）的行为契约。

## 为什么补这个文件（2026-09-21）

用户报告开 ARM 机器时只看到

> OCI 接口返回错误：InternalError (HTTP 500)

顺着查下去，``run_grab_loop`` **一条测试都没有** —— 而两个 bug 都在它身上：

1. ``task.last_error = str(exc).splitlines()[0][:200]`` —— 把
   ``describe_service_error()`` 的「原始信息 / 建议」两行截掉了。
   **用户可见的出口就在这一行。**
2. 循环里调了 ``is_retryable(exc)``，而 ``exc`` 是包装后的 ``OciApiError``，
   它的 ``code`` 在包装时被丢了 → 判定恒为「可重试」→
   「不可重试就停」这条分支**从来没走到过**。

第 2 条特别隐蔽：表是对的、函数是对的、这里也确实调用了它 ——
**断点在别处**。所以本文件刻意**不 mock** ``try_launch_once`` 的异常类型，
而是抛一个**真的** ``OciApiError``（走 ``to_api_error`` 包装），
让循环面对和线上完全一样的对象。

（如果这里自己造一个带 ``.code`` 的假异常，那个 bug 永远测不出来 ——
  假对象模拟了**你的代码**，却没模拟**真实链路**。见踩坑 #47 / #57。）
"""
from __future__ import annotations

import asyncio

import oci.exceptions
import pytest

from oracles.errors import NotAllowedError, to_api_error
from oracles.services import grab
from oracles.services.grab import GrabTask, run_grab_loop


def _api_error(code: str, message: str, status: int = 500) -> Exception:
    """造一个**真实包装过**的 OciApiError（带 code / status）。"""
    return to_api_error(oci.exceptions.ServiceError(
        status=status, code=code, headers={}, message=message,
    ))


def _task(*, count: int = 1, interval: int = 0, retry: bool = False) -> GrabTask:
    """默认 ``retry=False``（单次开机）。

    ⚠️ 默认值刻意选「会收尾」的那个：``retry=True`` + 可重试错误
       就是一个**故意设计成不退出**的循环，拿它当默认值会让
       写错一个用例就把整个测试进程挂死（本次实测踩过，
       ``interval=0`` 时表现为静默无限循环，进程被 SIGTERM 杀掉）。
       需要测重试的用例显式传 ``retry=True``。
    """
    return GrabTask(
        token="tok", chat_id=1, user_id=1, account_index=1,
        spec={"account_index": 1, "count": count, "retry": retry},
        interval_seconds=interval,
    )


#: 假实现最多被调用几次。超过就说明循环在空转 —— 主动抛错，
#: 把「测试进程静默挂死（被 SIGTERM 杀掉、看不出是哪个用例）」
#: 变成「一条带调用次数的清晰失败」。2026-09-21 实测踩过这个坑。
MAX_LAUNCH_CALLS = 20


class _SpinDetected(BaseException):
    """循环空转时抛这个。

    ⚠️ 必须继承 ``BaseException`` 而**不是** ``Exception`` ——
       ``run_grab_loop`` 里有个 ``except Exception`` 兜底，
       继承 Exception 的话会被它吞掉、记成「又一次失败」，然后继续转 ——
       防线本身失效，测试照样挂死。

       也不用 ``KeyboardInterrupt``：pytest 会把它当成「用户中断」，
       直接中止整个会话，而不是报一条失败。
    """


def _patch_launcher(monkeypatch, script: list) -> list[int]:
    """把 ``try_launch_once`` 换成按剧本走的假实现。

    ``script`` 的每一项是 ``(True, "名字")`` 或一个异常实例。
    返回一个列表，用来数**实际被调用了几次**。
    """
    calls: list[int] = []

    async def fake(spec, settings, registry, ensure_writable, *, seq=1, total=1):
        calls.append(seq)
        if len(calls) > MAX_LAUNCH_CALLS:
            raise _SpinDetected(
                f"try_launch_once 被调用了 {len(calls)} 次 —— 循环在空转。"
                f"（多半是「本该不可重试的错误被判成可重试」，"
                f"或 spec 里 retry=True 却没给终止条件）"
            )
        item = script[min(len(calls) - 1, len(script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(grab, "try_launch_once", fake)
    return calls


def _run(task: GrabTask) -> None:
    asyncio.run(run_grab_loop(task, object(), object(), lambda: None))


# ---------------------------------------------------------------------------
#  1. 🔴 用户可见的出口：失败原因必须**多行完整**
# ---------------------------------------------------------------------------
def test_a_capacity_failure_keeps_the_original_message(monkeypatch) -> None:
    """🔴 ``last_error`` 里必须有 OCI 的原始信息，不能只剩第一行。

    这是用户那个 bug 的直接回归测试：他只看到
    「OCI 接口返回错误：InternalError (HTTP 500)」，
    而 OCI 明明说了「Out of host capacity」。
    """
    _patch_launcher(monkeypatch, [
        _api_error("InternalError", "Out of host capacity.", 500),
    ])
    task = _task()
    _run(task)

    assert task.last_error, "没记录错误"
    assert "Out of host capacity." in task.last_error, (
        f"原始信息被截掉了 —— 用户会以为是 OCI 挂了：\n{task.last_error!r}"
    )
    assert "原始信息" in task.last_error
    assert len(task.last_error.splitlines()) > 1, "只剩一行 = 又截断了"


# ---------------------------------------------------------------------------
#  2. 不可重试 → 立刻收尾，不空转
# ---------------------------------------------------------------------------
def test_a_non_retryable_error_stops_the_loop_immediately(monkeypatch) -> None:
    """🔴 认证/权限类错误必须**立刻停**，不能睡着重试。

    这条在 2026-09-21 之前是**永远走不到**的分支（``code`` 被丢，
    判定恒为可重试）。用 ``count=5`` + 真重试模式：如果还在空转，
    调用次数会 >1。
    """
    calls = _patch_launcher(monkeypatch, [
        _api_error("NotAuthenticated", "认证失败", 401),
    ])
    task = _task(count=5, interval=0, retry=True)
    _run(task)

    assert task.status == "blocked", (
        f"状态是 {task.status} —— 认证失败不会自己好，必须标 blocked"
    )
    assert len(calls) == 1, (
        f"试了 {len(calls)} 次 —— 不可重试的错误不该继续空转"
    )
    assert task.attempts == 1


@pytest.mark.parametrize("code,status", [
    ("NotAuthenticated", 401),
    ("NotAuthorizedOrNotFound", 404),
    ("InvalidParameter", 400),
    ("QuotaExceeded", 400),
])
def test_every_non_retryable_code_stops_the_loop(monkeypatch, code, status) -> None:
    """整张 ``NON_RETRYABLE_CODES`` 表逐个走一遍真实循环。

    单点验证不够：表里任何一个码没接上，都会让那一类配置错误
    永远空转、日志刷屏，用户还等不到结论。
    """
    calls = _patch_launcher(monkeypatch, [_api_error(code, "x", status)])
    task = _task(count=5, interval=0, retry=True)
    _run(task)

    assert task.status == "blocked", f"{code} 没让循环停下来"
    assert len(calls) == 1, f"{code} 空转了 {len(calls)} 次"


def test_a_retryable_error_keeps_retrying_until_it_succeeds(monkeypatch) -> None:
    """对照组：容量不足 → 继续试，试到成功为止。

    ⚠️ 缺了这条，「无条件立刻 blocked」的实现也能让上面那些通过 ——
       而那正是最坏的形态：抢机功能彻底失效，且用户以为「就是抢不到」。
    """
    calls = _patch_launcher(monkeypatch, [
        _api_error("OutOfHostCapacity", "Out of host capacity.", 500),
        _api_error("InternalError", "Out of host capacity.", 500),
        (True, "oracles-1-1"),
    ])
    task = _task(count=1, interval=0, retry=True)
    _run(task)

    assert task.status == "succeeded", f"状态 {task.status}，最后一次错误 {task.last_error}"
    assert task.result_names == ["oracles-1-1"]
    assert len(calls) == 3, f"实际试了 {len(calls)} 次"
    # `attempts` 统计**全部**尝试（含成功那次）—— 任务列表里显示为「N 次尝试」。
    # 2 次失败 + 1 次成功 = 3。
    assert task.attempts == 3


def test_one_shot_mode_does_not_retry_a_capacity_failure(monkeypatch) -> None:
    """单次开机：失败就收尾（``stopped``），不空转烧 API。

    与自动抢机的区别要能被区分开：``stopped`` 是「这次没抢到」，
    ``blocked`` 是「重试也不会好」—— 两者的建议完全不同。
    """
    calls = _patch_launcher(monkeypatch, [
        _api_error("OutOfHostCapacity", "Out of host capacity.", 500),
    ])
    task = _task(count=1, interval=0, retry=False)
    _run(task)

    assert task.status == "stopped", f"单次模式该是 stopped，实际 {task.status}"
    assert len(calls) == 1


def test_the_write_switch_blocks_the_loop(monkeypatch) -> None:
    """写开关关 → ``blocked``，且原因是完整可读的。"""
    _patch_launcher(monkeypatch, [NotAllowedError("写操作已禁用（ORACLES_WRITE_ENABLED=false）")])
    task = _task(count=3, interval=0, retry=True)
    _run(task)

    assert task.status == "blocked"
    assert "ORACLES_WRITE_ENABLED" in (task.last_error or "")


def test_stop_is_respected_between_attempts(monkeypatch) -> None:
    """``stop()`` 把状态改成 ``stopped`` 后，循环要在**下一轮开头**退出。"""
    calls = _patch_launcher(monkeypatch, [
        _api_error("OutOfHostCapacity", "Out of host capacity.", 500),
    ])
    task = _task(count=5, interval=0, retry=True)

    async def driver() -> None:
        task.status = "stopped"
        await run_grab_loop(task, object(), object(), lambda: None)

    asyncio.run(driver())
    assert calls == [], "任务已停，却还去开机了"
    assert task.status == "stopped"


def test_a_partial_success_keeps_going(monkeypatch) -> None:
    """要 2 台，第 1 台成、第 2 台失败 → 不能把已成功的算掉。"""
    calls = _patch_launcher(monkeypatch, [
        (True, "oracles-1-1"),
        _api_error("OutOfHostCapacity", "Out of host capacity.", 500),
    ])
    task = _task(count=2, interval=0, retry=False)
    _run(task)

    assert task.done_count == 1, f"done_count={task.done_count}"
    assert task.result_names == ["oracles-1-1"]
    assert task.status == "stopped", "还差一台 → 该收尾等用户再发起"
    assert len(calls) == 2


# ---------------------------------------------------------------------------
#  3. 🔴 「单次」= 每台试一次，不是「只试一台」
# ---------------------------------------------------------------------------
def test_one_shot_opens_every_requested_machine(monkeypatch) -> None:
    """🔴 选 3 台 + 「现在开机」→ 必须真的开出 3 台。

    2026-09-21 实测的 bug：循环在**第一台成功后就无条件 break**，
    于是只开出 1 台，然后给用户看
    「⚠️ 部分成功（1/3 台）· 可改用自动抢机继续补齐」。

    而同一段流程的文案（汇总页）和 ``run_grab_loop`` 的 docstring
    都写着「**每台**只试一次」—— **文案是对的，实现是错的**。
    这条测试盯的就是这个不一致。
    """
    calls = _patch_launcher(monkeypatch, [(True, "oracles-1-1")])
    task = _task(count=3, interval=0, retry=False)
    _run(task)

    assert task.done_count == 3, (
        f"要 3 台只开出 {task.done_count} 台 —— 「单次」被实现成了「只试一台」"
    )
    assert task.status == "succeeded", f"状态是 {task.status}"
    assert len(calls) == 3, f"只尝试了 {len(calls)} 次"


def test_one_shot_still_stops_at_the_first_failure(monkeypatch) -> None:
    """对照组：单次模式**失败就收尾**，不重试。

    ⚠️ 缺了这条，「单次模式改成无限重试」也能让上面那条通过 ——
       而那会把「不空转烧 API」这个设计意图整个丢掉。
    """
    calls = _patch_launcher(monkeypatch, [
        (True, "oracles-1-1"),
        _api_error("OutOfHostCapacity", "Out of host capacity.", 500),
        (True, "oracles-1-3"),        # 不该被用到
    ])
    task = _task(count=3, interval=0, retry=False)
    _run(task)

    assert len(calls) == 2, f"失败后还在继续试（试了 {len(calls)} 次）"
    assert task.done_count == 1
    assert task.status == "stopped"


def test_multi_machine_names_are_suffixed(monkeypatch) -> None:
    """多台时 ``seq`` 要递增 —— OCI 里同名实例会直接报错。

    所以 ``try_launch_once`` 收到的 ``seq`` 必须是 1、2、3…
    （它据此把名字拼成 ``name-2`` / ``name-3``）。
    """
    calls = _patch_launcher(monkeypatch, [(True, "x")])
    task = _task(count=3, interval=0, retry=False)
    _run(task)

    assert calls == [1, 2, 3], f"seq 没有递增：{calls}"
