"""OCI 官方 SDK 网关。

**客户端构造 / 认证 / 重试 / 错误翻译** 的唯一出口。上层 service 只跟本模块
打交道，好处是：
  · 客户端懒加载 + 缓存，同一账号不会重复建连
  · 统一的超时、重试、错误翻译（见 errors.describe_service_error）
  · 想换认证方式（API Key ↔ Instance Principal）只改这里

⚠️ 本项目只用 **Oracle 官方 Python SDK**，不 shell out 调 `oci` CLI。
   所以不存在「CLI 未安装 / 版本不一致 / 环境变量串味」这些问题。

**关于「谁能 import oci」**：本模块的文档以前写的是「全项目唯一直接 import
``oci`` 的地方」—— **这句是错的**，实际有 5 处（2026-09-20 核实）：
``oci_gateway.py``、``services/compute.py``、``services/storage.py``、
``services/security.py``（2 处延迟导入）。

准确的纪律是这条 —— **按用途分，而不是按文件分**：

| 用途 | 谁可以用 |
| --- | --- |
| 构造客户端（``oci.core.ComputeClient(...)`` 等） | **只有本模块** |
| 碰 ``oci.config`` / ``oci.auth`` / ``oci.retry`` / ``oci.exceptions`` | **只有本模块** |
| 用 ``oci.*.models.*`` 纯数据类构造请求体 | 任何 service 模块 |
| 用 ``oci.util.to_dict`` 把 SDK 对象转 dict | 任何 service 模块 |

也就是说：service 层**不许自己造客户端、不许自己 catch/翻译 SDK 异常** ——
那两件事一分散，超时/重试/错误文案就会各写一套、慢慢漂移。
纯数据类则没必要绕道网关，硬绕反而多一层转发。

``tests/test_oci_import_discipline.py`` 用源码级检查守住这条边界。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from typing import Any

import oci
import oci.exceptions
import oci.pagination
import oci.retry

from .config import Account, Settings
from .errors import OciApiError, to_api_error

log = logging.getLogger(__name__)


def _build_retry_strategy() -> Any:
    """构造重试策略。

    OCI SDK 里没有 `oci.retry.RetryStrategy` 这个类（容易想当然写错），
    正确入口是 ``RetryStrategyBuilder``，构造完调 ``get_retry_strategy()``。

    配置意图：
      · 最多 6 次尝试、总耗时上限 300 秒
      · 保留 SDK 默认的重试条件（409/IncorrectState、409/LockConflict、429 限流）
      · 额外对任意 5xx 重试

    不同版本 SDK 的参数名可能有差异，所以这里容错：
    拿不到自定义策略就退回 SDK 默认的，绝不因为重试配置而启动失败。
    """
    try:
        return oci.retry.RetryStrategyBuilder(
            max_attempts_check=True,
            max_attempts=6,
            total_elapsed_time_check=True,
            total_elapsed_time_seconds=300,
            service_error_check=True,
            service_error_retry_on_any_5xx=True,
        ).get_retry_strategy()
    except Exception as exc:  # noqa: BLE001
        log.debug("自定义重试策略不可用（%s），改用 SDK 默认策略", exc)
        return oci.retry.DEFAULT_RETRY_STRATEGY


RETRY = _build_retry_strategy()


class AccountClient:
    """单个 OCI 账号的 SDK 客户端集合（全部懒加载）。"""

    def __init__(self, account: Account, settings: Settings):
        self.account = account
        self.settings = settings
        self._config: dict[str, Any] | None = None
        self._signer: Any = None
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    #  配置
    # ------------------------------------------------------------------
    def _build_config(self) -> dict[str, Any]:
        """按认证方式构造 SDK config 字典。"""
        acc = self.account

        if acc.auth_mode == "instance_principal":
            # 跑在 OCI 实例上时，用实例主体认证——完全不需要私钥文件。
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            self._signer = signer
            return {"region": signer.region, "tenancy": signer.tenancy_id}

        key_file = acc.key_file or self.settings.default_key_file
        if not key_file:
            raise OciApiError(
                f"{acc.label}: 没有可用私钥。账号条目加 key_file，"
                f"或设环境变量 OCI_API_KEY_FILE。"
            )
        cfg = {
            "user": acc.user,
            "fingerprint": acc.fingerprint,
            "tenancy": acc.tenancy,
            "region": acc.region,
            "key_file": str(key_file),
        }
        # SDK 会校验文件可读性；这里提前给个更友好的报错
        try:
            self._config = oci.config.validate_config(cfg)
        except Exception:  # noqa: BLE001 —— 校验失败也用原 cfg，让真实调用报错
            pass
        return cfg

    @property
    def config(self) -> dict[str, Any]:
        if self._config is None:
            self._config = self._build_config()
        return self._config

    @property
    def tenancy_id(self) -> str:
        """租户 OCID。

        instance_principal 模式下账号条目里没有 tenancy，得从签名器返回的
        config 里取，所以不能直接读 account.tenancy。
        """
        return self.account.tenancy or self.config.get("tenancy") or self.account.compartment

    @property
    def compartment_id(self) -> str:
        """操作默认落在的 compartment。免费账号一般就是租户根。"""
        return self.account.compartment or self.tenancy_id

    # ------------------------------------------------------------------
    #  客户端工厂
    # ------------------------------------------------------------------
    def _client(self, key: str, factory: Callable[..., Any]) -> Any:
        with self._lock:
            if key not in self._clients:
                kwargs: dict[str, Any] = {
                    "retry_strategy": RETRY,
                    "timeout": self.settings.timeout,
                }
                if self._signer is not None:
                    kwargs["signer"] = self._signer
                # config 必须先构造（instance_principal 下 signer 在里面产生）
                self._clients[key] = factory(self.config, **kwargs)
            return self._clients[key]

    @property
    def compute(self) -> Any:
        return self._client("compute", oci.core.ComputeClient)

    @property
    def blockstorage(self) -> Any:
        return self._client("blockstorage", oci.core.BlockstorageClient)

    @property
    def network(self) -> Any:
        return self._client("network", oci.core.VirtualNetworkClient)

    @property
    def compute_management(self) -> Any:
        return self._client("compute_mgmt", oci.core.ComputeManagementClient)

    @property
    def identity(self) -> Any:
        return self._client("identity", oci.identity.IdentityClient)

    @property
    def limits(self) -> Any:
        return self._client("limits", oci.limits.LimitsClient)

    @property
    def object_storage(self) -> Any:
        return self._client("object_storage", oci.object_storage.ObjectStorageClient)

    # ------------------------------------------------------------------
    #  调用封装
    # ------------------------------------------------------------------
    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """调用 SDK 方法并返回 ``.data``，失败抛 OciApiError。

        ⚠️ 刻意**不**提供「失败返回空列表」的版本。空列表和「查询失败」
           必须能被区分开——历史上正是 `or []` 这种兜底写法让「查询失败」
           被当成「空桶」，差点删掉用户有数据的存储桶。
        """
        try:
            return fn(*args, **kwargs).data
        except oci.exceptions.ServiceError as exc:
            raise to_api_error(exc) from exc
        except oci.exceptions.RequestException as exc:
            raise OciApiError(f"网络层请求失败：{exc}") from exc

    def call_all(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> list[Any]:
        """自动翻页，返回全部结果。

        对应 SDK 的 ``oci.pagination.list_call_get_all_results``。

        ⚠️ 只对**用 ``opc-next-page`` 响应头翻页**的接口有效。
           有些接口的游标在响应体里（如对象存储的 ``list_objects`` 用
           ``next_start_with``），通用分页器对它们无效，必须用
           ``call`` / ``call_response`` 手工循环。
        """
        try:
            resp = oci.pagination.list_call_get_all_results(fn, *args, **kwargs)
            return resp.data or []
        except oci.exceptions.ServiceError as exc:
            raise to_api_error(exc) from exc
        except oci.exceptions.RequestException as exc:
            raise OciApiError(f"网络层请求失败：{exc}") from exc

    def call_response(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """调用 SDK 并返回**完整 response 对象**（含 headers）。

        需要读 ``opc-next-page`` 这类响应头做手工翻页时用这个 ——
        有些接口的分页游标在响应头里，有些在响应体里，通用分页器不一定都覆盖。
        """
        try:
            return fn(*args, **kwargs)
        except oci.exceptions.ServiceError as exc:
            raise to_api_error(exc) from exc
        except oci.exceptions.RequestException as exc:
            raise OciApiError(f"网络层请求失败：{exc}") from exc

    # ------------------------------------------------------------------
    #  常用原子操作
    # ------------------------------------------------------------------
    def availability_domains(self) -> list[str]:
        """可用域名称列表。

        ⚠️ AD 名带随机前缀（如 ``MKzl:US-SANJOSE-1-AD-1``），**不能自己拼**。
        ⚠️ 大小写不统一（有的账号是 ``eu-amsterdam-1-AD-1``），做匹配别写死。
        """
        ads = self.call_all(self.identity.list_availability_domains,
                            compartment_id=self.account.tenancy)
        return [ad.name for ad in ads]

    def namespace(self) -> str:
        """对象存储命名空间。

        命名空间是**按 tenancy 共享**的：同租户下多个 user 返回同一个值，
        共用同一份 20 GiB 免费额度，不能按「账号数 × 20 GiB」算总量。
        """
        return self.call(self.object_storage.get_namespace).strip()

    def wait_for_state(self, client: Any, getter: Callable[..., Any], resource_id: str,
                       target_states: str | Iterable[str], **kwargs: Any) -> Any:
        """轮询等待资源进入目标状态。

        🔴 **`state` 必须传 ``tuple``（或裸字符串）—— 传 ``list`` 会永远等不到。**

        2026-09-20 真机验证撞出来的。``oci.wait_until`` 内部对多值只认
        ``tuple``，**list 会掉进单值比较分支**（SDK 源码 ``oci/waiter.py``）::

            if isinstance(state, tuple):
                if getattr(response.data, property) in state:   # ← 多值
                    return response
            elif getattr(response.data, property) == state:     # ← 单值
                return response

        于是 ``["RUNNING"]`` 走的是 ``"RUNNING" == ["RUNNING"]`` —— **永远为假**，
        一直轮询到 ``MaximumWaitTimeExceeded``。对照组实测（同一个假客户端，
        第 2 次轮询就变 RUNNING）::

            state=["RUNNING"]     → MaximumWaitTimeExceeded（4.0s 超时）
            state=("RUNNING",)    → ✅ 返回 RUNNING（2.0s）
            state="RUNNING"       → ✅ 返回 RUNNING（2.0s）

        而这里原来写的正是 ``list(target_states)`` ——
        **这个方法从来没有成功过一次**。后果不是「等久一点」，而是：

          · ``execute_launch`` 对**已经开成功的机器**报失败
            → 抢机循环认为「这次没抢到」→ **下一轮再开一台（重复实例）**
          · 开关机 / 销毁每次白等满 ``max_wait_seconds``（420~600 秒）

        这个 bug 之所以能活这么久：所有测试都用假客户端，
        而假客户端**根本不调 ``oci.wait_until``** —— 真实语义从未被触达。
        回归测试：``tests/test_wait_for_state.py``，
        喂的是**真实 SDK** 的 ``oci.wait_until`` + 假客户端（不是 mock）。

        ``states`` 为空也要拦：``x in ()`` 恒为假，同样是死等。

        🔴 **返回的是资源对象（``.data``），不是 ``Response`` 包装。**

        2026-09-20 撞出来的**第二个**坑：``oci.wait_until`` 返回的是
        **``Response``**（它把 getter 的返回值原样递回），而本模块的
        ``client.call`` 一律返回 ``.data``。两种约定混在一起，
        第一个想用返回值的人必然踩 —— 当时 ``open_rescue_console`` 写成::

            conn = client.wait_for_state(...)      # 拿到的是 Response
            if conn.lifecycle_state != "ACTIVE":   # AttributeError

        报错是 ``'Response' object has no attribute 'lifecycle_state'`` ——
        而 ``Response`` 这个类名在调用点附近根本没出现过，
        第一眼看完全不知道它是从哪冒出来的。

        这个坑能潜伏这么久，是因为之前 3 处调用**全都丢弃返回值**：:

            client.wait_for_state(client.compute, ..., inst.id, "RUNNING", ...)

        **丢弃返回值 = 这条约定从来没被验证过。**
        现在统一成 ``.data``，与 ``client.call`` 一致。
        回归测试：``tests/test_wait_for_state.py`` 里那条
        ``test_returns_the_resource_not_the_response``。
        """
        if isinstance(target_states, str):
            target_states = (target_states,)
        states = tuple(target_states)
        if not states:
            raise ValueError("wait_for_state 至少要有一个目标状态（空集合会死等）")
        try:
            response = oci.wait_until(
                client, getter(resource_id), "lifecycle_state", states,
                max_wait_seconds=kwargs.pop("max_wait_seconds", 300),
                max_interval_seconds=kwargs.pop("max_interval_seconds", 5),
                **kwargs,
            )
        except oci.exceptions.ServiceError as exc:
            raise to_api_error(exc) from exc
        except Exception as exc:  # noqa: BLE001 —— 含 MaximumWaitTimeExceeded
            raise OciApiError(f"等待资源状态超时/失败：{exc}") from exc
        # 解包成资源对象 —— 与 `client.call` 的约定对齐（见 docstring）。
        return response.data


class ClientRegistry:
    """按账号序号缓存 AccountClient，进程内复用。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._clients: dict[int, AccountClient] = {}
        self._lock = threading.Lock()

    def get(self, index: int) -> AccountClient:
        with self._lock:
            if index not in self._clients:
                self._clients[index] = AccountClient(
                    self.settings.account(index), self.settings
                )
            return self._clients[index]

    def all(self) -> list[AccountClient]:
        return [self.get(a.index) for a in self.settings.accounts]

    def close(self) -> None:
        """释放连接池。SDK 客户端没有显式 close，靠 GC 回收。"""
        with self._lock:
            self._clients.clear()
