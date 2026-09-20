"""配置加载。

设计原则（直接关系到「开源不泄露密钥」这条硬要求）：

  1. **账号清单和私钥默认放在仓库之外**（``ORACLES_HOME``，默认 ``~/.oracles``）。
     仓库里只有 ``accounts.example.json`` 这种占位文件。
  2. 环境变量优先级最高；其次读 ``<ORACLES_HOME>/oracles.env``；
     最后读仓库根的 ``.env``（方便本地开发）。
  3. 所有校验在**启动时一次性做完**并集中报错，而不是等某个命令跑到一半才炸。
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

# 抑制 SDK 对私钥尾部标记的告警（该告警仅提示，不影响功能）
os.environ.setdefault("SUPPRESS_LABEL_WARNING", "True")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOME = Path.home() / ".oracles"

VALID_AUTH_MODES = ("api_key", "instance_principal")


# --------------------------------------------------------------------------
#  极简 .env 解析（不引入 python-dotenv，保持依赖最小）
# --------------------------------------------------------------------------
def load_env_file(path: Path, *, override: bool = False) -> None:
    """把 KEY=VALUE 读进 os.environ。已存在的变量默认不覆盖。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是整数，当前值：{raw!r}") from exc


# --------------------------------------------------------------------------
#  账号模型
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Account:
    """一个 OCI 账号（一组 API 凭据）。"""

    index: int
    region: str
    user: str | None = None
    fingerprint: str | None = None
    tenancy: str | None = None
    key_file: str | None = None
    alias: str | None = None
    auth_mode: str = "api_key"
    compartment_id: str | None = None
    # 预置默认值（按账号覆盖全局）
    default_shape: str = "VM.Standard.E2.1.Micro"
    default_image_os: str = "Canonical Ubuntu"
    default_ssh_key: str | None = None

    @property
    def compartment(self) -> str:
        """操作默认落在哪个 compartment —— 免费账号一般就是 tenancy 根。"""
        return self.compartment_id or self.tenancy or ""

    @property
    def label(self) -> str:
        """展示用标签，例如 ``[3] 新加坡备用``。"""
        return f"[{self.index}] {self.alias or self.region}"

    @property
    def key_path(self) -> Path | None:
        return Path(self.key_file).expanduser() if self.key_file else None

    def validate(self, default_key_file: str | None) -> list[str]:
        """返回问题列表，空列表表示通过。"""
        problems: list[str] = []

        if self.auth_mode not in VALID_AUTH_MODES:
            problems.append(
                f"auth_mode={self.auth_mode!r} 非法，只能是 {VALID_AUTH_MODES}"
            )
            return problems

        if not self.region:
            problems.append("缺少 region")

        if self.auth_mode == "api_key":
            for f in ("user", "fingerprint", "tenancy"):
                if not getattr(self, f):
                    problems.append(f"缺少 {f}")
            key = self.key_file or default_key_file
            if not key:
                problems.append(
                    "没有可用私钥：账号条目里没有 key_file，"
                    "环境变量 OCI_API_KEY_FILE 也没设"
                )
            elif not Path(key).expanduser().is_file():
                problems.append(f"私钥文件不存在：{key}")
        else:  # instance_principal
            if not self.compartment_id:
                problems.append("instance_principal 模式必须显式指定 compartment_id")

        return problems


# --------------------------------------------------------------------------
#  全局设置
# --------------------------------------------------------------------------
@dataclass
class Settings:
    home: Path
    telegram_token: str
    allowed_user_ids: frozenset[int]
    write_enabled: bool
    allow_destructive: bool
    log_level: str = "INFO"
    timeout: int = 120
    max_concurrency: int = 4
    default_key_file: str | None = None
    accounts_file: Path | None = None
    accounts: list[Account] = field(default_factory=list)

    # ---------- 便捷访问 ----------
    def account(self, index: int) -> Account:
        for a in self.accounts:
            if a.index == index:
                return a
        raise ConfigError(f"账号清单里没有序号 {index}")

    def account_or_none(self, index: int) -> Account | None:
        for a in self.accounts:
            if a.index == index:
                return a
        return None

    @property
    def writable(self) -> bool:
        return self.write_enabled

    @property
    def destructive_allowed(self) -> bool:
        return self.write_enabled and self.allow_destructive


# --------------------------------------------------------------------------
#  加载入口
# --------------------------------------------------------------------------
def _resolve_home() -> Path:
    raw = os.environ.get("ORACLES_HOME")
    return Path(raw).expanduser() if raw else DEFAULT_HOME


def load_accounts(path: str | Path, default_key_file: str | None) -> list[Account]:
    """读账号清单并校验。返回按 index 升序的列表。

    接受 str 或 Path —— 外部调用方（CLI、脚本）传字符串是常态，
    这里统一归一化，不要让类型差异变成运行期崩溃。
    """
    path = Path(path).expanduser()
    if not path.is_file():
        raise ConfigError(
            f"账号清单不存在：{path}\n"
            f"复制 accounts.example.json 过去填上真实凭据即可：\n"
            f"  cp accounts.example.json {path}\n"
            f"  chmod 600 {path}"
        )

    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"账号清单不是合法 JSON：{path} —— {exc}") from exc

    if not isinstance(raw, list):
        raise ConfigError("账号清单顶层必须是数组（list）")

    accounts: list[Account] = []
    seen: set[int] = set()
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"第 {i} 条账号不是对象（dict）")
        known = {f for f in Account.__dataclass_fields__}
        unknown = set(item) - known
        if unknown:
            raise ConfigError(
                f"第 {i} 条账号含未知字段：{sorted(unknown)}；可用字段：{sorted(known)}"
            )
        acc = Account(**item)
        if acc.index in seen:
            raise ConfigError(f"账号序号重复：{acc.index}")
        seen.add(acc.index)
        accounts.append(acc)

    accounts.sort(key=lambda a: a.index)

    # 集中校验，一次性把所有问题报出来
    all_problems: list[str] = []
    for acc in accounts:
        for p in acc.validate(default_key_file):
            all_problems.append(f"  · {acc.label}: {p}")
    if all_problems:
        raise ConfigError(
            "账号配置有问题，请修正后重试：\n" + "\n".join(all_problems)
        )
    if not accounts:
        raise ConfigError("账号清单是空的")

    return accounts


def load_settings(*, require_token: bool = True) -> Settings:
    """加载全部配置。

    ``require_token=False`` 用于本地 CLI 调试（不需要 Telegram Token）。
    """
    # 1) 仓库根 .env（本地开发）
    load_env_file(REPO_ROOT / ".env")

    # 2) ORACLES_HOME 下的 oracles.env（生产）
    home = _resolve_home()
    load_env_file(home / "oracles.env")

    # 3) 收集
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if require_token and not token:
        raise ConfigError(
            "缺少 TELEGRAM_BOT_TOKEN。\n"
            "从 @BotFather 拿到 token 后写入 <ORACLES_HOME>/oracles.env，"
            "或本地开发时写进仓库根的 .env（该文件已被 .gitignore 忽略）。"
        )

    raw_ids = (os.environ.get("TELEGRAM_ALLOWED_USER_IDS") or "").strip()
    ids: set[int] = set()
    for part in raw_ids.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise ConfigError(
                f"TELEGRAM_ALLOWED_USER_IDS 里有非数字项：{part!r}"
            ) from exc

    settings = Settings(
        home=home,
        telegram_token=token,
        allowed_user_ids=frozenset(ids),
        write_enabled=_env_bool("ORACLES_WRITE_ENABLED", False),
        allow_destructive=_env_bool("ORACLES_ALLOW_DESTRUCTIVE", False),
        log_level=os.environ.get("ORACLES_LOG_LEVEL", "INFO"),
        timeout=_env_int("ORACLES_TIMEOUT", 120),
        max_concurrency=max(1, _env_int("ORACLES_MAX_CONCURRENCY", 4)),
        default_key_file=os.environ.get("OCI_API_KEY_FILE") or None,
    )

    # 账号清单路径：显式环境变量 > ORACLES_HOME/accounts.json
    explicit = os.environ.get("ORACLES_ACCOUNTS_JSON") or os.environ.get("OCI_ACCOUNTS_JSON")
    settings.accounts_file = (
        Path(explicit).expanduser() if explicit else home / "accounts.json"
    )
    settings.accounts = load_accounts(settings.accounts_file, settings.default_key_file)

    return settings


def validate_startup(settings: Settings) -> list[str]:
    """返回启动告警（非致命），用于在 Bot 启动时提醒用户。"""
    warnings: list[str] = []
    if not settings.allowed_user_ids:
        warnings.append(
            "⚠️ 未配置 TELEGRAM_ALLOWED_USER_IDS —— Bot 不会响应任何人的消息。"
            "给 @userinfobot 发消息拿到自己的 ID 后填入。"
        )
    if not settings.write_enabled:
        warnings.append("ℹ️ 写操作已禁用（ORACLES_WRITE_ENABLED=false），当前只读模式。")
    elif not settings.allow_destructive:
        warnings.append(
            "ℹ️ 已开启写操作，但「销毁实例 / 删除存储桶」仍被禁用"
            "（ORACLES_ALLOW_DESTRUCTIVE=false）。"
        )
    return warnings


def iter_account_indexes(accounts: Iterable[Account]) -> list[int]:
    return [a.index for a in accounts]
