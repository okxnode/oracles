"""假的 Telegram 对象 —— 让 handler 层能在不联网的情况下被真实调用。

**为什么不用真的 ``telegram.Update``**：那些是带必填字段校验的
``TelegramObject``，手工构造很容易因为缺字段而炸；而且 handler 实际只用到
很少几个属性。所以这里用最小鸭子类型替身，把「发出去的消息」记下来供断言。

（键盘对象用真的 ``InlineKeyboardMarkup`` —— 那个不需要网络，
而且顺带验证了键盘构造本身没写错。）

这样就能在**不连 Telegram、不碰 OCI** 的前提下验证：
  · 白名单真的挡住了每一条命令
  · 只读模式下写操作真的被拦
  · 按钮回调路由真的走得通
  · 渲染管线在假数据下输出什么（包括「扫描失败」有没有被标出来）
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from oracles.bot.store import Store
from oracles.config import Account, Settings
from oracles.errors import OciApiError

# ---------------------------------------------------------------------------
#  假值：全部含 EXAMPLE / 虚构指纹，避免被 scripts/check_secrets.sh 误报
# ---------------------------------------------------------------------------
FAKE_OCID_USER = "ocid1.user.oc1..EXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE"
FAKE_OCID_TENANCY = "ocid1.tenancy.oc1..EXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE"
FAKE_FINGERPRINT = "00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff"
FAKE_KEY_FILE = "/tmp/EXAMPLE_key.pem"


def make_account(index: int, *, alias: str | None = None) -> Account:
    return Account(
        index=index,
        region=f"example-region-{index}",
        user=FAKE_OCID_USER,
        fingerprint=FAKE_FINGERPRINT,
        tenancy=FAKE_OCID_TENANCY,
        key_file=FAKE_KEY_FILE,
        alias=alias or f"测试账号{index}",
    )


def make_settings(
    indexes: tuple[int, ...] = (1, 2, 3),
    *,
    allowed: tuple[int, ...] = (100,),
    write: bool = False,
    destructive: bool = False,
    warnings: list[str] | None = None,
) -> Settings:
    """构造一个不碰磁盘的 Settings。"""
    return Settings(
        home=Path("/tmp/EXAMPLE_home"),
        telegram_token="1234567890:EXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE",
        allowed_user_ids=frozenset(allowed),
        write_enabled=write,
        allow_destructive=destructive,
        accounts=[make_account(i) for i in indexes],
    )


# ---------------------------------------------------------------------------
#  记录发出去的消息
# ---------------------------------------------------------------------------
@dataclass
class Sent:
    text: str
    where: str                      # "reply" | "edit"
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class SentDocument:
    """一次 ``send_document`` 的记录。

    ``sha256`` 是**内容**的哈希 —— 「发了两个文件」和
    「发了两个**内容正确的**文件」是完全不同的结论：
    文件名对、内容却是空文件（或发错了文件），用户拿到照样登不进。
    """

    filename: str
    caption: str
    size: int
    sha256: str


class FakeChat:
    """假的 chat —— 目前只用来接 ``send_document``。

    ⚠️ 2026-09-20 加。登录密钥是通过 ``message.chat.send_document`` 发出去的，
       而原来的 ``FakeMessage`` 只有一个 ``chat_id`` **数字**，没有 ``chat`` 对象。
       不给它一个假的，测试会以 ``AttributeError`` 的形式炸出来 ——
       那等于「密钥到底有没有真的发出去」这件事**从来没被验证过**。
       （同类问题：桩打得太低，被测行为就测不到了。）
    """

    def __init__(self, chat_id: int = 1) -> None:
        self.id = chat_id
        self.documents: list[SentDocument] = []

    async def send_document(self, document: Any = None, filename: str | None = None,
                            caption: str | None = None, **_kw: Any) -> None:
        # PTB 的 InputFile **在构造时就把文件读进内存了** ——
        # `input_file_content` 是 `bytes`，不是文件对象。
        # （第一版按文件对象去 `tell()`，异常被吞掉，于是每个附件都记成 0 字节。
        #   是 `assert size > 0` 那条断言把它抓出来的：没有它，
        #   「发出去的是空文件」会一路静默通过。）
        raw = getattr(document, "input_file_content", None)
        if raw is None:
            raw = document
        if isinstance(raw, (bytes, bytearray)):
            data = bytes(raw)
        elif isinstance(raw, str):
            data = raw.encode()
        else:
            try:
                pos = raw.tell()
                data = raw.read()
                raw.seek(pos)
            except Exception:   # noqa: BLE001 —— 夹具不该因为读不出内容而炸
                data = b""
        name = filename or getattr(document, "filename", None) or "?"
        self.documents.append(
            SentDocument(str(name), caption or "", len(data), hashlib.sha256(data).hexdigest())
        )


class FakeMessage:
    def __init__(self, chat_id: int = 1) -> None:
        self.chat_id = chat_id
        self.message_id = 1
        self.sent: list[Sent] = []
        self.chat = FakeChat(chat_id)

    async def reply_text(self, text: str, **kwargs: Any) -> FakeMessage:
        self.sent.append(Sent(text, "reply", kwargs))
        return self

    async def edit_text(self, text: str, **kwargs: Any) -> FakeMessage:
        self.sent.append(Sent(text, "edit", kwargs))
        return self

    # ---------- 断言辅助 ----------
    @property
    def texts(self) -> list[str]:
        return [s.text for s in self.sent]

    @property
    def all_text(self) -> str:
        return "\n".join(self.texts)

    @property
    def last(self) -> str:
        return self.sent[-1].text if self.sent else ""

    @property
    def documents(self) -> list[SentDocument]:
        """通过 ``chat.send_document`` 发出去的文件。"""
        return self.chat.documents


class FakeCallbackQuery:
    def __init__(self, data: str, message: FakeMessage) -> None:
        self.data = data
        self.message = message
        self.answered: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False,
                     **kwargs: Any) -> None:
        self.answered.append((text, show_alert))

    async def edit_message_text(self, text: str, **kwargs: Any) -> FakeMessage:
        self.message.sent.append(Sent(text, "edit", kwargs))
        return self.message

    @property
    def alerts(self) -> list[str]:
        return [t for t, _ in self.answered if t]


class FakeUpdate:
    """只实现 handler 真正用到的那几个属性。"""

    def __init__(self, *, user_id: int | None = 100, username: str = "tester",
                 chat_id: int = 1, message: FakeMessage | None = None,
                 callback_query: FakeCallbackQuery | None = None) -> None:
        self.effective_user = (SimpleNamespace(id=user_id, username=username,
                                              first_name="T", is_bot=False)
                               if user_id is not None else None)
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.callback_query = callback_query
        self.message = message
        self.effective_message = (callback_query.message if callback_query else message)


class FakeContext:
    def __init__(self, bot_data: dict[str, Any], args: list[str] | None = None,
                 error: Exception | None = None) -> None:
        self.bot_data = bot_data
        self.args = list(args or [])
        self.error = error
        self.bot = None


# ---------------------------------------------------------------------------
#  假的 OCI 客户端 / 注册表
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
#  假的 OCI 网关
#
#  为什么需要它：只桩「服务层函数」是不够的 —— 2026-09-20 就是这么漏掉两个
#  bug 的：
#    · `_power_plan` 里 terminate 分支是死代码（服务层函数被桩掉了，测不到）
#    · `open_rescue_console` 不传必填的 publicKey（同上）
#  两者都是「路由通了、动作本身跑不通」。把网关这一层做成假的，
#  服务层的真实代码就能整段跑起来，这类 bug 才会被拦住。
# ---------------------------------------------------------------------------
@dataclass
class ConsoleConnection:
    """假的控制台连接，字段名与 ``oci.core.models.InstanceConsoleConnection`` 对齐。

    ⚠️ 真实返回的是**一条 SSH 命令**，不是 ``vnc://`` 链接 ——
       2026-09-20 之前渲染代码就是照着 ``vnc://`` 想象的。
    """

    id: str = "ocid1.instanceconsoleconnection.oc1..EXAMPLECONSOLE"
    lifecycle_state: str = "ACTIVE"
    connection_string: str = (
        "ssh -o ProxyCommand='ssh -W %h:%p -p 443 "
        "ocid1.instanceconsoleconnection.oc1..EXAMPLECONSOLE@"
        "instance-console.example-region-1.oci.oraclecloud.com' "
        "ocid1.instance.oc1..EXAMPLEINSTANCE"
    )
    vnc_connection_string: str = (
        "ssh -o ProxyCommand='ssh -W %h:%p -p 443 "
        "ocid1.instanceconsoleconnection.oc1..EXAMPLECONSOLE@"
        "instance-console.example-region-1.oci.oraclecloud.com' "
        "-N -L localhost:5900:ocid1.instance.oc1..EXAMPLEINSTANCE:5900 "
        "ocid1.instance.oc1..EXAMPLEINSTANCE"
    )
    fingerprint: str = "SHA256:EXAMPLEfingerprintEXAMPLEfingerprintEXAMPLE"


class FakeComputeNamespace:
    """假的 ``client.compute``。

    每个方法都**记录调用参数**（测试要靠它断言请求里带了什么），
    返回一个带 ``.data`` 的壳 —— 和真 SDK 一样。
    """

    def __init__(self, owner: FakeClient) -> None:
        self._owner = owner

    def create_instance_console_connection(self, details: Any):
        self._owner.requests.append(("create_instance_console_connection", details))
        if self._owner.console_create_error is not None:
            # 一次性错误 —— 模拟「先撞上 409，删掉旧连接之后就能建了」
            err = self._owner.console_create_error
            self._owner.console_create_error = None
            raise err
        return SimpleNamespace(data=self._owner.console_connection)

    def list_instance_console_connections(self, **kwargs: Any):
        self._owner.requests.append(("list_instance_console_connections", kwargs))
        return SimpleNamespace(data=list(self._owner.existing_consoles))

    def get_instance_console_connection(self, conn_id: str):
        """`wait_for_state` 的 getter —— 2026-09-20 加。

        救援路径要拿它轮询连接状态。缺了它，「忘了等」会在测试里
        以 `AttributeError` 的形式冒出来（含糊），而不是一条干净的断言。
        """
        self._owner.requests.append(("get_instance_console_connection", conn_id))
        return SimpleNamespace(data=self._owner.console_connection)

    def delete_instance_console_connection(self, conn_id: str):
        self._owner.requests.append(("delete_instance_console_connection", conn_id))
        self._owner.deleted_consoles.append(conn_id)
        return SimpleNamespace(data=None)

    def get_instance(self, instance_id: str):
        """``instance_authorized_keys`` 要读实例 metadata 里的公钥。

        ⚠️ 2026-09-21 加。原来没有这个方法，于是「下载登录密钥」那条路
           只能靠 monkeypatch 掉整个 ``instance_authorized_keys`` 才能测 ——
           那会把「指纹怎么比」这段被测逻辑一起桩掉。
           给它一个假的之后，服务层的真实代码能整段跑起来。
        """
        self._owner.requests.append(("get_instance", instance_id))
        return SimpleNamespace(data=SimpleNamespace(
            id=instance_id, metadata=dict(self._owner.instance_metadata)))

    # ---- 卷挂载关系（2026-09-21 加：孤儿卷/删卷安全链零覆盖）----
    def list_boot_volume_attachments(self, **kwargs: Any):
        return self._owner.attachment_rows("boot", **kwargs)

    def list_volume_attachments(self, **kwargs: Any):
        return self._owner.attachment_rows("block", **kwargs)


@dataclass
class FakeAttachment:
    """一条卷挂载关系。

    ⚠️ **状态在 attachment 上，不在卷上** —— 这是真 SDK 的形态
       （``Volume.lifecycle_state`` 的合法值是 PROVISIONING/AVAILABLE/…，
       没有 ATTACHED）。夹具必须忠实这一点，否则「卷自己带 ATTACHED 状态」
       这种错误假设在测试里永远不会暴露（2026-09-21 实测过）。
    """

    volume_id: str
    instance_id: str
    state: str = "ATTACHED"          # ATTACHING | ATTACHED | DETACHING | DETACHED
    kind: str = "boot"               # boot | block


class FakeBlockstorageNamespace:
    """假的 ``client.blockstorage``。"""

    def __init__(self, owner: FakeClient) -> None:
        self._owner = owner

    def list_boot_volumes(self, **_kw: Any):
        return SimpleNamespace(data=list(self._owner.boot_volumes))

    def list_volumes(self, **_kw: Any):
        return SimpleNamespace(data=list(self._owner.block_volumes))

    def delete_volume(self, volume_id: str):
        self._owner.requests.append(("delete_volume", volume_id))
        self._owner.deleted_volumes.append(volume_id)
        return SimpleNamespace(data=None)

    def delete_boot_volume(self, volume_id: str):
        self._owner.requests.append(("delete_boot_volume", volume_id))
        self._owner.deleted_volumes.append(volume_id)
        return SimpleNamespace(data=None)


class FakeClient:
    """假 OCI 客户端：实现 ``call`` / ``call_all`` 的语义。

    只实现**救援路径**用到的那几个接口。服务层要是有别的调用，
    会在这里 AttributeError 炸出来 —— 那正是我们想知道的。
    """

    def __init__(self, account: Account) -> None:
        self.account = account
        self.compartment_id = "ocid1.compartment.oc1..EXAMPLECOMPARTMENT"
        self.requests: list[tuple[str, Any]] = []
        self.deleted_consoles: list[str] = []
        self.console_connection = ConsoleConnection()
        self.existing_consoles: list[ConsoleConnection] = []
        self.console_create_error: Exception | None = None
        #: 假实例的 metadata —— 目前用于 ``ssh_authorized_keys``。
        #: 测试直接改它就能模拟「这台机器注的是哪把公钥」。
        self.instance_metadata: dict[str, str] = {}
        self.compute = FakeComputeNamespace(self)
        # ---- 卷（2026-09-21 加：孤儿卷/删卷安全链此前零覆盖）----
        self.blockstorage = FakeBlockstorageNamespace(self)
        #: 挂载关系记录（``FakeAttachment``）。测试直接改它就能摆出各种局面。
        self.attachments: list[FakeAttachment] = []
        #: 这些可用域的**挂载关系查询**会抛错（模拟 429 / 500 / 权限不足）。
        #: 用来验证「查不到 ≠ 没挂」这条 fail-closed 纪律。
        self.failing_attachment_ads: set[str] = set()
        #: 可用域名列表。默认一个 AD —— 多 AD 场景直接改它。
        self.availability_domain_names: list[str] = ["AD-1"]
        #: 非 None 时 ``availability_domains()`` 直接抛（模拟整片区域查不到）
        self.ad_list_error: Exception | None = None
        self.boot_volumes: list[Any] = []
        self.block_volumes: list[Any] = []
        self.deleted_volumes: list[str] = []
        #: `wait_for_state` 的调用记录：``(resource_id, 目标状态元组, kwargs)``。
        #:
        #: ⚠️ 2026-09-20 加。`wait_for_state` 是 `AccountClient` 的方法，
        #:    假客户端原来没有 —— 于是「忘了等」在测试里会变成
        #:    `AttributeError`（一条含糊的报错），而不是「没人断言过」。
        #:    给它一个默认实现，缺了等待就成了**可断言**的事。
        self.wait_calls: list[tuple[str, tuple[str, ...], dict[str, Any]]] = []
        #: 非 None 时 `wait_for_state` 直接返回它（用来模拟 `FAILED` 这类结局）
        self.wait_return: Any = None

    # ---- 与真 AccountClient 同名的封装 ----
    def call(self, fn, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs).data

    def call_all(self, fn, *args: Any, **kwargs: Any) -> list[Any]:
        return fn(*args, **kwargs).data or []

    def availability_domains(self) -> list[str]:
        if self.ad_list_error is not None:
            raise self.ad_list_error
        return list(self.availability_domain_names)

    def attachment_rows(self, kind: str, **kwargs: Any) -> Any:
        """挂载关系查询 —— **会在指定 AD 上抛错**，这是 fail-closed 的测试点。"""
        ad = kwargs.get("availability_domain")
        if ad in self.failing_attachment_ads:
            raise OciApiError(f"模拟挂载关系查询失败（{ad}）")
        self.requests.append((f"list_{kind}_volume_attachments", kwargs))
        return SimpleNamespace(data=[
            SimpleNamespace(boot_volume_id=a.volume_id, volume_id=a.volume_id,
                            instance_id=a.instance_id, lifecycle_state=a.state)
            for a in self.attachments if a.kind == kind
        ])

    def wait_for_state(self, client: Any, getter: Any, resource_id: str,
                       target_states: Any, **kwargs: Any) -> Any:
        """假的等待：记录调用，并把控制台连接推进到第一个目标状态。

        默认语义是「等到了第一个目标状态」—— 对救援路径就是 `ACTIVE`。
        要模拟失败，把 `wait_return` 设成一个 `lifecycle_state="FAILED"` 的对象。
        """
        states = ((target_states,) if isinstance(target_states, str)
                  else tuple(target_states))
        self.wait_calls.append((resource_id, states, kwargs))
        if self.wait_return is not None:
            return self.wait_return
        return replace(self.console_connection, lifecycle_state=states[0])

    # ---- 断言辅助 ----
    def find(self, name: str) -> list[Any]:
        """取出某个接口的调用参数（按调用顺序）。"""
        return [payload for call_name, payload in self.requests if call_name == name]


class FakeRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.clients: dict[int, FakeClient] = {}

    def get(self, index: int) -> FakeClient:
        if index not in self.clients:
            self.clients[index] = FakeClient(self.settings.account(index))
        return self.clients[index]

    def all(self) -> list[FakeClient]:
        return [self.get(a.index) for a in self.settings.accounts]

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
#  装配
# ---------------------------------------------------------------------------
class Harness:
    """把 settings / registry / store 装进 bot_data，并提供两种触发方式。"""

    def __init__(self, settings: Settings, *, warnings: list[str] | None = None) -> None:
        self.settings = settings
        self.registry = FakeRegistry(settings)
        self.store = Store()
        self.bot_data: dict[str, Any] = {
            "settings": settings,
            "registry": self.registry,
            "store": self.store,
            "startup_warnings": warnings if warnings is not None else [],
        }

    # ---------- 命令 ----------
    async def command(self, handler, *args: str, user_id: int | None = 100,
                      chat_id: int = 1) -> FakeMessage:
        msg = FakeMessage(chat_id)
        update = FakeUpdate(user_id=user_id, chat_id=chat_id, message=msg)
        await handler(update, FakeContext(self.bot_data, args=list(args)))
        return msg

    # ---------- 按钮 ----------
    async def callback(self, handler, data: str, *,
                       user_id: int | None = 100, chat_id: int = 1
                       ) -> tuple[FakeCallbackQuery, FakeMessage]:
        msg = FakeMessage(chat_id)
        query = FakeCallbackQuery(data, msg)
        update = FakeUpdate(user_id=user_id, chat_id=chat_id, callback_query=query)
        await handler(update, FakeContext(self.bot_data))
        return query, msg

    # ---------- 自由文本 ----------
    async def text(self, handler, content: str, *, user_id: int | None = 100,
                   chat_id: int = 1) -> FakeMessage:
        msg = FakeMessage(chat_id)
        msg.text = content
        update = FakeUpdate(user_id=user_id, chat_id=chat_id, message=msg)
        await handler(update, FakeContext(self.bot_data))
        return msg
