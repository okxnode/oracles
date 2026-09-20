"""自动抢机（grab）任务引擎。

场景：免费额度是稀缺资源，热门 AD 的机器开出来就可能被别人拿走。所以「开机」
经常不是点一次就能成的 —— 得**每隔几秒重试一次**，直到抢到 N 台为止，或者用户手动停。

设计：
  · 一个 ``GrabTask`` = 一份完整的开机参数快照 + 重试间隔（5/10/20/30 秒）。
  · ``run_grab_loop`` 在 Bot 的事件循环里跑：每轮 plan → execute，成功一台记一台；
    失败就记下原因、睡 interval 再来。**写开关关闭**时立即停掉（不空转烧 API）。
  · 参数冻结在创建时刻 —— 想改就停掉重开。状态全程可读（/tasks 菜单），
    重启即作废：和 pending token 一个语义，别让用户误以为后台还活着。

为什么不用 cron / APScheduler：这是**进程内、与会话绑定**的短生命周期任务，
引入外部调度器反而多一个要运维的东西。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class GrabTask:
    """一个自动抢机任务。"""

    token: str                     # 短令牌，按钮里用（callback_data ≤64B）
    chat_id: int
    user_id: int
    account_index: int
    spec: dict[str, Any]           # plan_launch/execute_launch 的参数快照 + count
    interval_seconds: int

    created_at: float = field(default_factory=time.time)
    status: str = "running"        # running | succeeded | stopped | blocked
    attempts: int = 0
    done_count: int = 0            # 已抢到的台数
    last_error: str | None = None
    result_names: list[str] = field(default_factory=list)

    @property
    def want(self) -> int:
        return max(1, int(self.spec.get("count", 1)))

    @property
    def finished(self) -> bool:
        return self.status in ("succeeded", "stopped", "blocked") or \
            (self.status == "running" and self.done_count >= self.want)


def spec_brief(spec: dict[str, Any]) -> str:
    """任务列表里的一行摘要。"""
    parts = []
    shape = (spec.get("shape") or "").upper()
    if "A1" in shape:
        parts.append(f"A1.Flex {spec.get('ocpus', '?')}C/{spec.get('memory_gb', '?')}G")
    elif "E2" in shape:
        parts.append("E2 Micro（免费）")
    else:
        parts.append(shape or "?")
    if int(spec.get("count", 1)) > 1:
        parts.append(f"×{spec['count']}")
    if spec.get("boot_volume_gb"):
        parts.append(f"{spec['boot_volume_gb']}G盘")
    return " ".join(parts)


# --------------------------------------------------------------------------
#  单台尝试 —— 抽成独立函数便于测试 mock
# --------------------------------------------------------------------------
#: ``spec`` 里可以**直传**给 ``plan_launch`` 的键。
#:
#: ⚠️ 白名单式取值，不是 ``**spec`` —— spec 里还有 ``user_data_b64`` /
#:    ``login_user`` / ``key_fingerprint`` 这些**不是 plan_launch 参数**的字段，
#:    直接展开会 ``TypeError``，而且报错发生在真正调 API 那一步。
#:
#: ⚠️ 抽成模块常量（而不是写在函数里），是为了让测试能引用**同一份**清单。
#:    2026-09-21 加 ``ssh_public_key`` 时发现的：测试里另抄了一份，
#:    于是「往白名单加键」这件事**测试根本管不着** ——
#:    哪天有人删掉一个键，测试照样绿。这就是「同一份值有两处实现，必然漂移」。
PLAN_KWARG_WHITELIST = (
    "ad", "shape", "image_os", "boot_volume_gb",
    "subnet_id", "ocpus", "memory_gb", "ssh_public_key",
)


async def try_launch_once(spec: dict[str, Any], settings: Any, registry: Any,
                          ensure_writable: Callable[[], None], *, seq: int = 1,
                          total: int = 1) -> tuple[bool, str]:
    """按计划抢一台。返回 (是否成功, 实例名)。

    ⚠️ ``ensure_writable`` **在执行前**调用：写开关关 → 抛 NotAllowedError，
       循环据此把任务标记为 blocked（而不是无限重试去撞一堵墙）。
    ⚠️ ``seq/total``：多台时给实例加 ``-2/-3…`` 后缀 —— OCI 里同名实例会直接报错。
    """
    from . import compute as compute_svc  # 延迟导入避免循环依赖

    client = registry.get(spec["account_index"])

    # ⚠️ 白名单式取值，不是 `**spec` —— spec 里还有 user_data_b64、
    #    login_user 这些**不是 plan_launch 参数**的字段，
    #    直接展开会 TypeError（而报错发生在真正调 API 那一步）。
    plan_kwargs: dict[str, Any] = {k: v for k, v in spec.items()
                                   if k in PLAN_KWARG_WHITELIST}
    base_name = spec.get("name")
    if base_name and total > 1:
        plan_kwargs["name"] = f"{base_name}-{seq}"

    # 只读阶段：挑 AD / 找镜像 / 查子网 —— 每次都实时查，不缓存
    plan = await asyncio.to_thread(compute_svc.plan_launch, client, **plan_kwargs)

    # user_data_b64（cloud-init 用户名/密码）和 assign_ipv6 是 LaunchPlan 的字段、
    # 不是 plan_launch 的参数 —— 在这里从 spec 接上去
    if spec.get("user_data_b64"):
        plan.user_data_b64 = spec["user_data_b64"]
    if spec.get("assign_ipv6"):
        plan.assign_ipv6 = True

    ensure_writable()   # ← 写开关守门（DRY-RUN 时在这里被拦下）

    view = await asyncio.to_thread(compute_svc.execute_launch, client, plan)
    # 第二元素是**实例名**（循环记进 result_names）；展示文案由调用方拼
    return True, view.display_name or f"oracles-{spec['account_index']}"


# --------------------------------------------------------------------------
#  主循环
# --------------------------------------------------------------------------
async def run_grab_loop(task: GrabTask, settings: Any, registry: Any,
                        ensure_writable: Callable[[], None]) -> None:
    """抢到 want() 台 → succeeded；被停/写开关关 → stopped/blocked。

    · ``one_shot=True``（单次开机）：每台只试一次，失败就收尾汇报 —— 不空转烧 API
    · ``one_shot=False``（自动抢机）：失败后睡 interval 再试，直到齐数或人工停止
    """
    from ..errors import NotAllowedError, is_retryable, one_line

    while task.done_count < task.want:
        if task.status != "running":          # stop() 把状态改成 stopped → 退出
            break
        failed = False           # 这一轮是不是失败了（单次模式据此决定要不要收尾）
        try:
            ok, name = await try_launch_once(
                task.spec, settings, registry, ensure_writable,
                seq=task.done_count + 1, total=task.want)
        except asyncio.CancelledError:
            raise
        except NotAllowedError as exc:
            # 写开关关 —— 这是配置问题，重试一万次也不会变，直接停并说明原因
            task.status = "blocked"
            task.last_error = str(exc)
            log.warning("抢机任务 %s 被写开关拦下：%s", task.token, one_line(exc, 200))
            return
        except Exception as exc:   # noqa: BLE001 —— 额度满/限流/容量不足，都是「再等等」或「这次没抢到」
            failed = True
            task.attempts += 1
            # ⚠️ 这里**故意不截断**：`last_error` 会原样渲染给用户当「原因」，
            #    而 `describe_service_error()` 的多行文本里，
            #    第 2 行是 OCI 的原始信息、第 3 行才是建议 ——
            #    2026-09-21 实测用 `splitlines()[0]` 截过，用户只看到
            #    「OCI 接口返回错误：InternalError (HTTP 500)」，
            #    真正的原因「Out of host capacity」被丢掉，
            #    于是把「该 AD 没容量」误判成「OCI 挂了」。见踩坑 #59。
            task.last_error = str(exc)
            log.info("抢机 #%d（%s）失败：%s",
                     task.attempts, task.token, one_line(exc, 200))

            # 「再试一万次也不会变」的错误要立刻收尾，不能继续睡着重试：
            # 认证/权限/参数/配额类错误空转下去只会刷屏，用户也等不到结论。
            if not is_retryable(exc):
                task.status = "blocked"
                log.warning("抢机任务 %s 遇到不可重试的错误，已停止重试：%s",
                            task.token, one_line(exc, 200))
                return
        else:
            if ok:
                task.done_count += 1
                task.result_names.append(name)
                task.attempts += 1
                log.info("抢机成功（%s）第 %d/%d 台：%s",
                         task.token, task.done_count, task.want, name)

        if task.done_count >= task.want:
            task.status = "succeeded"
            break

        one_shot = not task.spec.get("retry")
        if one_shot:
            # 🔴 单次模式 = 「**每台**只试一次」，不是「只试一台」。
            #
            # 2026-09-21 实测的 bug：这里原来**无条件** break ——
            # 于是「选 3 台 + 现在开机」只开出 1 台就收尾，
            # 然后给用户看「⚠️ 部分成功（1/3 台）· 可改用自动抢机继续补齐」。
            # 而同一段流程的文案（`_wz_summary`）和本函数的 docstring
            # 都写着「每台只试一次」—— **文案是对的，实现是错的**。
            #
            # 正确的语义：成功一台就接着开下一台；一旦某台失败就收尾，
            # 不再重试（这才是「不空转烧 API」的意思）。
            if failed:
                task.status = "stopped"
                log.info("单次开机结束（%s），成功 %d/%d",
                         task.token, task.done_count, task.want)
                break
            continue

        # 自动抢机模式：睡 interval 再来（stop() 会 cancel 掉这个 sleep）
        await asyncio.sleep(task.interval_seconds)
