"""配置管理：账号清单的增删改 + API 凭据状态体检 + 热重载。

设计约束：
  · ``accounts.json`` 里存的是**明文凭据坐标**（user/tenancy/fingerprint/key_file），
    私钥本体在 ``keys/*.pem``，两个文件都是 600、目录 root-only —— Bot 进程以
    oracles 用户跑能读；仓库里没有它们。
  · 修改账号清单必须**原子写**（临时文件 + rename）：半截 JSON 会让整个 Bot 起不来。
  · **任何会让清单丢失信息的写入，落盘前先备份**（删条目 / 改字段）。
    只增不改的 ``add_account_entry`` 不备份 —— 它删掉重加即可复原，没有信息损失。
    备份落在 ``<home>/backups/accounts-<UTC时间戳>.json``，只留最近 5 份。
    备份失败**不阻断**操作（目录不可写就删不了账号会很别扭），但一定打 WARNING。
  · 热重载：改完 reload settings 并替换 bot_data 引用，无需重启服务；
    新配置有问题时保留旧引用兜底，Bot 不挂。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Account, load_settings
from ..errors import ConfigError, one_line
from ..oci_gateway import AccountClient, ClientRegistry

log = logging.getLogger(__name__)


@dataclass
class KeyStatus:
    """一个账号凭据链的体检结果。"""

    index: int
    label: str
    region: str
    key_file_ok: bool            # 私钥文件存在且可读（instance_principal 恒 True）
    fingerprint_set: bool        # 填了 fingerprint（和公钥配对的那串 hex）
    ocids_complete: bool         # user / tenancy OCID 都填了
    api_reachable: bool | None = None   # True=调通 API；False=失败；None=没联网测
    error: str | None = None

    @property
    def ready(self) -> bool:
        return self.key_file_ok and self.fingerprint_set and self.ocids_complete


# --------------------------------------------------------------------------
#  体检（只读）
# --------------------------------------------------------------------------
def check_accounts(settings, *, test_api: bool = True) -> list[KeyStatus]:
    """逐个账号检查凭据链。单账号失败不影响其它账号的结论。

    ``test_api=False`` 时纯本地检查（私钥文件、字段齐全性），不联网 ——
    适合「我刚加了个账号还没填完」这种场景，不想等 API 超时。
    """
    results: list[KeyStatus] = []
    for acc in settings.accounts:
        status = KeyStatus(
            index=acc.index, label=acc.label, region=acc.region,
            key_file_ok=True, fingerprint_set=bool(acc.fingerprint),
            ocids_complete=bool(acc.user and acc.tenancy),
            api_reachable=None if not test_api else _probe_api(settings, acc),
        )
        if acc.auth_mode != "api_key":
            results.append(status)   # instance_principal 没有本地私钥可查
            continue

        key_path = Path(acc.key_file).expanduser() if acc.key_file \
            else (Path(settings.default_key_file).expanduser()
                  if settings.default_key_file else None)
        if key_path is None or not key_path.is_file():
            status.key_file_ok = False
            status.error = f"私钥文件不存在：{key_path}"
        elif not os.access(key_path, os.R_OK):
            status.key_file_ok = False
            status.error = f"私钥不可读（权限问题）：{key_path}"

        if test_api and status.ready and status.api_reachable is None:
            # 上面 _probe_api 已经跑过；这里是 auth_mode=api_key 且字段齐全的分支
            pass
        results.append(status)
    return results


def _probe_api(settings, acc: Account) -> bool | None:
    """用该账号调一次最轻的 API（列可用域），验证整条凭据链。失败返回 False。"""
    try:
        client = AccountClient(acc, settings)
        ads = client.availability_domains()
        return len(ads) >= 0   # 能列出 AD 就算通（空列表也合法）
    except Exception as exc:  # noqa: BLE001 —— 体检要吞掉所有异常转成结论
        log.info("账号 %s API 体检失败：%s", acc.index, one_line(exc))
        return False


# --------------------------------------------------------------------------
#  增 / 删 / 改（写 accounts.json，走 dispatch 的确认流）
# --------------------------------------------------------------------------
def _read_list(accounts_file: Path) -> list[dict]:
    data = json.loads(accounts_file.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ConfigError("accounts.json 顶层必须是数组")
    return data


# --------------------------------------------------------------------------
#  备份（写前快照，给误操作留一个回滚点）
# --------------------------------------------------------------------------
#: 保留份数。清单只有几百字节，留 5 份够回溯一连串误操作。
_BACKUP_KEEP = 5

#: 备份文件名前缀，同时是 ``_prune_backups`` 的匹配模式。
_BACKUP_PREFIX = "accounts-"


def backup_dir(accounts_file: Path) -> Path:
    """账号清单的备份目录 —— 与 ``security.py`` 的 ``backups/`` 是同一个。

    刻意从 ``accounts_file`` 反推，而不是从 Settings 取 ``home``：
    这样测试里指一个临时文件也能正确落到它旁边，不必构造完整 Settings。
    """
    return accounts_file.parent / "backups"


def _prune_backups(directory: Path) -> list[Path]:
    """只留最近 ``_BACKUP_KEEP`` 份，返回被删掉的（便于测试断言）。"""
    files = sorted(directory.glob(f"{_BACKUP_PREFIX}*.json"))
    stale = files[:-_BACKUP_KEEP] if len(files) > _BACKUP_KEEP else []
    for path in stale:
        try:
            path.unlink()
        except OSError as exc:      # 清理失败不该影响主流程
            log.warning("清理旧备份失败：%s（%s）", path.name, exc)
    return stale


def _backup_accounts_file(accounts_file: Path) -> Path | None:
    """把当前的 ``accounts.json`` 拷一份进 ``backups/``，返回备份路径。

    失败返回 ``None``（已经打了 WARNING），由调用方决定要不要提示用户 ——
    **不抛异常**：备份目录不可写就删不了账号，是很别扭的耦合。
    """
    if not accounts_file.is_file():
        return None            # 还没建文件（首次写入），没什么可备份的
    try:
        dest_dir = backup_dir(accounts_file)
        dest_dir.mkdir(parents=True, exist_ok=True)
        # 带微秒：同一秒内连删两个账号也不会互相覆盖，
        # 且字典序 == 时间序，_prune_backups 直接按文件名排序即可。
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        dest = dest_dir / f"{_BACKUP_PREFIX}{stamp}.json"
        shutil.copy2(accounts_file, dest)
        os.chmod(dest, 0o600)   # 里面是明文凭据坐标，权限从紧
        _prune_backups(dest_dir)
        return dest
    except OSError as exc:
        log.warning("accounts.json 备份失败：%s", exc)
        return None


def add_account_entry(accounts_file: Path, entry: dict[str, Any]) -> int:
    """追加一个账号条目并原子落盘。返回新序号。

    ``entry`` 至少含 index / region；user/tenancy/fingerprint/key_file 可选。
    未知字段直接忽略（宽容新增，严格读取 —— load_accounts 会兜底报错）。

    这里**不做备份**：追加不丢信息，删掉重加即可复原（对比 ``remove`` / ``update``）。
    """
    _validate_entry(entry)
    data = _read_list(accounts_file)
    if any(item.get("index") == entry["index"] for item in data):
        raise ValueError(f"序号 {entry['index']} 已存在，换一个或先删掉旧的")

    known = set(Account.__dataclass_fields__)
    clean: dict[str, Any] = {}
    for k, v in entry.items():
        if k not in known or v is None or (isinstance(v, str) and not v.strip()):
            continue
        clean[k] = v
    data.append(clean)
    _atomic_write(accounts_file, data)
    log.info("accounts.json 新增账号 index=%s", entry["index"])
    return int(entry["index"])


def remove_account_entry(accounts_file: Path, index: int) -> Path | None:
    """按序号删除一个账号条目。**落盘前先备份**，返回备份路径（失败为 ``None``）。

    ⚠️ 删账号是不可逆的：OCI 资源、私钥文件都不动，但**凭据坐标**一旦没了就得
       重新抄一遍 OCID 和指纹。所以这里留一份原文件到 ``backups/`` ——
       误删时 ``cp`` 回去、重启服务即可。
    """
    data = _read_list(accounts_file)
    before = len(data)
    kept = [item for item in data if item.get("index") != index]
    if len(kept) == before:
        raise ValueError(f"找不到序号 {index} 的账号")
    if not kept:
        raise ValueError("这是最后一个账号，删掉它 Bot 就没了 —— 请先加新的再删")

    # 校验都过了、确实要写盘了，才做备份（被拒绝的操作不留垃圾文件）
    backup = _backup_accounts_file(accounts_file)
    if backup is None:
        log.warning("本次删除**没有备份**（backups/ 不可写？），index=%s 不可回滚", index)
    _atomic_write(accounts_file, kept)
    log.info("accounts.json 删除账号 index=%s（备份：%s）", index,
             backup.name if backup else "无")
    return backup


def update_account_entry(accounts_file: Path, index: int,
                         patch: dict[str, Any]) -> Path | None:
    """按序号改一个账号的部分字段（空值 = 清除该字段）。

    **同样先备份**：``patch`` 里一个 ``None`` 就能把 ``key_file`` 清掉，
    和删条目一样是「信息没了就找不回来」。返回备份路径（失败为 ``None``）。
    """
    data = _read_list(accounts_file)
    hit = False
    for item in data:
        if item.get("index") == index:
            known = set(Account.__dataclass_fields__) - {"index"}
            for k, v in patch.items():
                if k not in known:
                    continue
                if v is None or (isinstance(v, str) and not v.strip()):
                    item.pop(k, None)
                else:
                    item[k] = v
            hit = True
    if not hit:
        raise ValueError(f"找不到序号 {index} 的账号")

    backup = _backup_accounts_file(accounts_file)
    if backup is None:
        log.warning("本次更新**没有备份**（backups/ 不可写？），index=%s 不可回滚", index)
    _atomic_write(accounts_file, data)
    log.info("accounts.json 更新账号 index=%s fields=%s（备份：%s）", index,
             sorted(patch), backup.name if backup else "无")
    return backup


def _validate_entry(entry: dict[str, Any]) -> None:
    for key in ("index", "region"):
        v = entry.get(key)
        if not v or (isinstance(v, str) and not v.strip()):
            raise ValueError(f"新增账号必须提供 {key}")
    try:
        int(entry["index"])
    except (TypeError, ValueError):
        raise ValueError("序号必须是整数") from None


def _atomic_write(path: Path, data: list[dict]) -> None:
    """临时文件 + rename：accounts.json 任何时刻都必须是合法 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".accounts-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.chmod(tmp_name, 0o600)   # 凭据文件，权限从紧
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
#  热重载（无需重启服务）
# --------------------------------------------------------------------------
def reload_settings() -> Any:
    """重新读 oracles.env + accounts.json，返回新的 Settings。

    ⚠️ 新配置本身有问题时会抛 ConfigError —— 调用方必须保留旧 settings 兜底。
    """
    return load_settings()


def swap_in_application(app, new_settings: Any) -> None:
    """把新 Settings + 新 ClientRegistry 换进运行中的 Application。

    handler 每次都从 ``context.bot_data`` 读 —— 换个引用即生效，无需重启。
    pending token / wizard state 挂在 Store 上，Store 不重建，会话不断。
    """
    app.bot_data["settings"] = new_settings
    app.bot_data["registry"] = ClientRegistry(new_settings)
