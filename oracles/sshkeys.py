"""账号级 SSH 登录密钥对：生成、落盘、读取、指纹。

用途：开机向导第 6 步选「仅 SSH 公钥」时多出一个子选项 ——
「让 Bot 生成一对新的」。选它就在这里生成一对 ed25519、存到服务器，
把公钥注入实例，私钥通过 Telegram 发回用户，之后也能随时重下。

---

🔴 **OCI 没有「提供密钥对」的 API** —— 这是本模块存在的理由，别搞错

2026-09-20 实测（SDK 2.186.0）：

  · ``ComputeClient`` 里名字含 ``key`` 的方法 **0 个**；
  · ``LaunchInstanceDetails`` 没有密钥类参数，只有 ``metadata``
    （``ssh_authorized_keys`` 塞在这个 dict 里）；
  · 全包搜 ``generate_key_pair`` / ``generate_ssh_key`` 只有 2 处，
    都在 ``cloud_migrations`` / ``cloud_bridge`` 的 **OLVM 迁移**模型里，
    与开机无关。

OCI 控制台里那个「Generate a key pair for me」→「Download private key」
是**浏览器本地生成**的，不是服务端下发的。要复刻这个体验，
只能自己在服务器上生成。

---

🔴 **与 ``compute._generate_rescue_keypair`` 的关键区别：密钥类型不一样**

那把**必须是 RSA** —— ``create_instance_console_connection`` 只收 RSA，
实测 ed25519 会被服务端直接拒
（``InvalidParameter: Invalid ssh public key type "ssh-ed25519"``）。

这把是**普通 SSH 登录**用的，走实例的 sshd，ed25519 完全没问题，
而且更短、更强（项目现在开机的公钥本来就是 ed25519）。
**别把这两处的类型要求互相抄。**

---

**落盘位置**：``<ORACLES_HOME>/keys/<账号序号>/login_key``（600）
+ ``login_key.pub``（644），目录 700。

刻意放在 ``ORACLES_HOME`` 下 —— 那里**在仓库之外**，
密钥从架构上进不了 git（和 accounts.json、API 私钥同一个理由）。

⚠️ **「私钥在服务器上」本身是一次安全权衡，不是免费的**

原来「仅 SSH 公钥」注入的是用户自己的公钥，私钥从不离开用户本机；
改成 Bot 生成后，私钥会落在 VPS 上（谁拿到那台机器就拿到钥匙），
并且会经过 Telegram 服务器。

这是**用户明知并主动选择**的路径（向导里的子选项），不是默认路径 ——
默认仍然是「用我配置的公钥」。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from .errors import ConfigError

log = logging.getLogger(__name__)

#: 密钥目录名（相对 ``ORACLES_HOME``）
KEY_DIRNAME = "keys"
#: 私钥文件名；公钥是它加 ``.pub``
PRIVATE_FILENAME = "login_key"

#: 权限：目录 700、私钥 600、公钥 644。
#: 公钥本来就该能被人看见（它要注入到实例里），私钥不行。
_DIR_MODE = 0o700
_PRIVATE_MODE = 0o600
_PUBLIC_MODE = 0o644


@dataclass(frozen=True)
class LoginKey:
    """一个账号的登录密钥对（已落盘）。"""

    account_index: int
    public_openssh: str
    private_pem: str
    fingerprint: str
    private_path: Path
    public_path: Path
    #: True = 本次读的是已有文件；False = 本次新生成
    reused: bool = False

    @property
    def comment(self) -> str:
        """公钥行尾的注释（如 ``oracles-login-15``），没有则空串。"""
        parts = self.public_openssh.split(maxsplit=2)
        return parts[2] if len(parts) > 2 else ""

    @property
    def public_blob(self) -> str:
        """公钥的 base64 段 —— 判断「两把是不是同一把」用它。"""
        return blob_of(self.public_openssh)

    @property
    def private_filename(self) -> str:
        """建议用户存成的文件名（Telegram 发文件时用）。"""
        return f"oracles-{self.account_index}-login.key"


def blob_of(public_line: str) -> str:
    """取公钥行的 base64 段（即 key blob），取不到返回空串。

    ⚠️ 判断「两把公钥是不是同一把」要比较 **blob**，不要比较指纹字符串。
       指纹是对 blob 的哈希，理论上可能撞；更要紧的是 blob 比较
       **不需要解析**，所以喂进来一行格式古怪的公钥（比如 OCI 里
       手抄错的）也不会抛异常 —— 只会判定为「不是同一把」，
       这正是我们想要的语义。
    """
    parts = public_line.split()
    return parts[1] if len(parts) >= 2 else ""


def fingerprint_of(public_openssh: str) -> str:
    """算 OpenSSH 的 SHA256 指纹，**输出格式与 ``ssh-keygen -lf`` 一致**。

    ⚠️ 不要自己另发明一种指纹格式。用户会拿 ``ssh-keygen -lf <公钥>``
       的输出跟这里显示的比对 —— 对不上就会以为密钥是坏的。
       对齐格式的成本只有两行，不对齐的成本是一次误判。

    算法：对公钥的 base64 段（key blob）做 sha256，
    再 base64 编码、去掉 ``=`` 填充，前缀 ``SHA256:``。
    """
    parts = public_openssh.split()
    if len(parts) < 2:
        raise ValueError(
            f"公钥格式不对（至少要「类型 base64」两段）：{public_openssh[:60]!r}"
        )
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except Exception as exc:  # noqa: BLE001 —— 各种 b64 错误统一成一个可读信息
        raise ValueError(f"公钥的 base64 段解不开：{exc}") from exc
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def generate_ed25519(comment: str) -> tuple[str, str]:
    """生成一对 ed25519，返回 ``(OpenSSH 公钥, OpenSSH 私钥)``。"""
    key = ed25519.Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    # ⚠️ cryptography 导出的 OpenSSH 公钥**不带 comment**。
    #    不补的话用户拿到的一行公钥看不出属于哪个账号，
    #    多账号时几把钥匙长得一模一样（前 20 个字符都一样）。
    public = f"{public} {comment}".strip()
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return public, private


def account_key_dir(settings: Any, account_index: int) -> Path:
    """该账号的密钥目录（**不保证存在**）。"""
    return Path(settings.home) / KEY_DIRNAME / str(int(account_index))


def key_paths(settings: Any, account_index: int) -> tuple[Path, Path]:
    """返回 ``(私钥路径, 公钥路径)``。"""
    d = account_key_dir(settings, account_index)
    return d / PRIVATE_FILENAME, d / f"{PRIVATE_FILENAME}.pub"


def default_comment(account_index: int) -> str:
    return f"oracles-login-{int(account_index)}"


# --------------------------------------------------------------------------
#  落盘
# --------------------------------------------------------------------------
def _write_exclusive(path: Path, text: str, mode: int) -> None:
    """用 ``O_EXCL`` + 目标权限**一次性**创建文件。

    ⚠️ 不要写成「先普通写、再 chmod」—— 那中间有一个窗口，
       私钥在那段时间里是按 umask 的权限存在的（通常是 644，
       同机任何用户都能读）。``O_EXCL`` 同时充当「拒绝覆盖」的守卫。
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(text)
        # os.open 的 mode 会被进程 umask 削掉，这里兜一道底保证确定。
        os.chmod(path, mode)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def save(settings: Any, account_index: int,
         public_openssh: str, private_pem: str) -> tuple[Path, Path]:
    """把密钥对写到 ``<ORACLES_HOME>/keys/<序号>/``，返回 ``(私钥, 公钥)``。

    🔴 **绝不覆盖已存在的密钥。** 覆盖会让**已经开出去的机器连不上** ——
       那些实例的 ``authorized_keys`` 里还是旧公钥，而新私钥配不上它。
       这种故障很难查（私钥文件看起来好好的，就是登不进），
       所以宁可报错让人手动处理。
    """
    d = account_key_dir(settings, account_index)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, _DIR_MODE)

    priv, pub = key_paths(settings, account_index)
    if priv.exists() or pub.exists():
        raise ConfigError(
            f"账号 {account_index} 的登录密钥已存在，拒绝覆盖：{d}\n"
            f"  覆盖会让**已经开出去的机器连不上**（它们的 authorized_keys 里是旧公钥）。\n"
            f"  要换钥匙请先手动移走或删掉这个目录，再重来。"
        )

    # 先公钥后私钥：万一私钥写失败，留下的是「只有公钥」的半成品，
    # load() 会把它判定为不完整并报错 —— 而不是当成一把能用的钥匙。
    _write_exclusive(pub, public_openssh + "\n", _PUBLIC_MODE)
    try:
        _write_exclusive(priv, private_pem, _PRIVATE_MODE)
    except Exception:
        pub.unlink(missing_ok=True)
        raise
    return priv, pub


# --------------------------------------------------------------------------
#  读取
# --------------------------------------------------------------------------
def _perm_warning(path: Path) -> str | None:
    """私钥权限过松时给一句提示（不阻断，但要说）。"""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    if mode & 0o077:
        return (f"{path} 权限是 {mode:03o} —— 同组/其他用户可读。"
                f"建议 chmod 600（我们生成时就是 600，被改过才会这样）")
    return None


def load(settings: Any, account_index: int) -> LoginKey | None:
    """读已有密钥对；一个都没有返回 ``None``。

    ⚠️ 只有一半（只有公钥或只有私钥）**不是**「没有」，是**损坏** ——
       报错而不是静默返回 None，否则上层会重新生成一把，
       把已经开出去的机器留在「旧公钥 + 新私钥」的错配状态里。
    """
    priv, pub = key_paths(settings, account_index)
    if not priv.exists() and not pub.exists():
        return None
    if not priv.is_file() or not pub.is_file():
        missing = priv if not priv.is_file() else pub
        raise ConfigError(
            f"账号 {account_index} 的登录密钥不完整：缺 {missing.name}（{missing.parent}）\n"
            f"  这通常是上次写入中途失败留下的。删掉整个目录重来即可 ——\n"
            f"  但先确认没有机器依赖它，否则那些机器会连不上。"
        )

    public = pub.read_text(encoding="ascii").strip()
    private = priv.read_text(encoding="ascii")
    if not public or not private.strip():
        raise ConfigError(f"账号 {account_index} 的登录密钥文件是空的：{priv.parent}")

    warn = _perm_warning(priv)
    if warn:
        log.warning("登录密钥权限过松：%s", warn)

    return LoginKey(
        account_index=int(account_index),
        public_openssh=public,
        private_pem=private,
        fingerprint=fingerprint_of(public),
        private_path=priv,
        public_path=pub,
        reused=True,
    )


def ensure(settings: Any, account_index: int,
           *, comment: str | None = None) -> LoginKey:
    """有就读出来，没有就生成一对并落盘。**幂等**。

    这是「每账号一对、后续复用」这个策略的落点：
    同一账号再开机器时拿到的是同一把钥匙，不用管很多把。
    """
    existing = load(settings, account_index)
    if existing is not None:
        return existing

    public, private = generate_ed25519(comment or default_comment(account_index))
    try:
        priv, pub = save(settings, account_index, public, private)
    except ConfigError:
        # 竞态：并发的另一次开机刚好先写完了 —— 用它写的那把，
        # 而不是把这次开机整个失败掉。
        raced = load(settings, account_index)
        if raced is not None:
            log.info("账号 %s 的登录密钥被并发创建，改用已存在的那把", account_index)
            return raced
        raise

    return LoginKey(
        account_index=int(account_index),
        public_openssh=public,
        private_pem=private,
        fingerprint=fingerprint_of(public),
        private_path=priv,
        public_path=pub,
        reused=False,
    )


def render(key: LoginKey, *, account_label: str = "") -> str:
    """渲染成 Telegram 文案（生成完成 / 下载时用）。"""
    head = "🆕 *新生成了一对登录密钥*" if not key.reused else "🔑 *本账号的登录密钥*"
    if account_label:
        head += f"　（{account_label}）"
    return "\n".join([
        head,
        "",
        "类型：`ed25519`",
        f"指纹：`{key.fingerprint}`",
        f"存放：`{key.private_path}`（600，仅 oracles 用户可读）",
        "",
        "私钥和公钥两个文件已作为附件发给你。",
        "私钥请存成 `" + key.private_filename + "` 并 `chmod 600`，登录时用 `ssh -i` 指定。",
        "",
        "⚠️ 这是**账号级**密钥：该账号后续开的新机器都会复用这一对，",
        "　 所以它同时是这些机器的钥匙 —— 别放进仓库、别外传。",
    ])
