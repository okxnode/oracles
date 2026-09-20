"""账号清单增删改 + 写前备份。

背景：2026-09-20 在生产上发现 `account_remove` 执行完后 `backups/` 是空的 ——
删掉的账号没有任何回滚点，误删就得重新抄一遍 OCID 和指纹。

这里守住四条：
  1. 会丢信息的写入（删条目 / 改字段）落盘前必须留一份原文
  2. 备份里的内容是**改动之前**的样子（不是改完的）
  3. 备份失败**不阻断**删除，但必须打 WARNING（静默失败等于没备份）
  4. 被拒绝的操作（找不到序号 / 删最后一个）不产生备份垃圾
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest

from oracles.services import accounts_mgmt as am

LOGGER = "oracles.services.accounts_mgmt"


def _write_accounts(path: Path, indices: list[int]) -> None:
    """造一个最小可用的 accounts.json。"""
    path.write_text(
        json.dumps([{"index": i, "region": "ap-singapore-1"} for i in indices],
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _backups(path: Path) -> list[Path]:
    return sorted(am.backup_dir(path).glob("accounts-*.json"))


def _read(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
#  备份确实产生了，而且内容是改动之前的
# --------------------------------------------------------------------------
def test_remove_creates_a_backup(tmp_path: Path):
    acc = tmp_path / "accounts.json"
    _write_accounts(acc, [3, 8])

    am.remove_account_entry(acc, 3)

    assert len(_backups(acc)) == 1


def test_backup_holds_the_content_from_before_the_deletion(tmp_path: Path):
    """核心断言：备份里必须还能看到被删掉的那个账号。"""
    acc = tmp_path / "accounts.json"
    _write_accounts(acc, [3, 8])

    am.remove_account_entry(acc, 3)

    saved = _read(_backups(acc)[0])
    assert [e["index"] for e in saved] == [3, 8]     # 备份 = 删之前
    assert [e["index"] for e in _read(acc)] == [8]   # 现文件 = 删之后


def test_remove_returns_the_backup_path(tmp_path: Path):
    acc = tmp_path / "accounts.json"
    _write_accounts(acc, [3, 8])

    backup = am.remove_account_entry(acc, 3)

    assert backup is not None and backup.is_file()
    assert backup.parent == tmp_path / "backups"


def test_backup_lives_beside_the_accounts_file(tmp_path: Path):
    """备份目录从 accounts_file 反推 —— 测试里指个临时文件也要落在它旁边。"""
    acc = tmp_path / "etc" / "oracles" / "accounts.json"
    acc.parent.mkdir(parents=True)
    _write_accounts(acc, [3, 8])

    am.remove_account_entry(acc, 3)

    assert (acc.parent / "backups").is_dir()
    assert am.backup_dir(acc) == acc.parent / "backups"


def test_backup_is_mode_600(tmp_path: Path):
    """备份里有明文凭据坐标，权限必须和原文件一样从紧。"""
    acc = tmp_path / "accounts.json"
    _write_accounts(acc, [3, 8])

    backup = am.remove_account_entry(acc, 3)

    assert backup is not None
    assert (backup.stat().st_mode & 0o777) == 0o600


def test_update_also_creates_a_backup(tmp_path: Path):
    """改字段同样会丢信息（一个 None 就能清掉 key_file），所以也备份。"""
    acc = tmp_path / "accounts.json"
    _write_accounts(acc, [3, 8])
    before = _read(acc)

    backup = am.update_account_entry(acc, 3, {"region": "us-ashburn-1"})

    assert backup is not None
    assert _read(backup) == before
    assert _read(acc)[0]["region"] == "us-ashburn-1"


def test_backup_for_update_holds_the_old_value(tmp_path: Path):
    acc = tmp_path / "accounts.json"
    acc.write_text(json.dumps([{"index": 3, "region": "ap-singapore-1",
                                "key_file": "/etc/oracles/keys/3.pem"}]), encoding="utf-8")

    backup = am.update_account_entry(acc, 3, {"key_file": None})

    assert backup is not None
    assert _read(backup)[0]["key_file"] == "/etc/oracles/keys/3.pem"
    assert "key_file" not in _read(acc)[0]


def test_add_does_not_create_a_backup(tmp_path: Path):
    """追加不丢信息 —— 删掉重加即可复原，不需要备份。"""
    acc = tmp_path / "accounts.json"
    _write_accounts(acc, [3])

    am.add_account_entry(acc, {"index": 8, "region": "ap-singapore-1"})

    assert _backups(acc) == []


# --------------------------------------------------------------------------
#  被拒绝的操作不留垃圾
# --------------------------------------------------------------------------
def test_unknown_index_is_rejected_without_a_backup(tmp_path: Path):
    acc = tmp_path / "accounts.json"
    _write_accounts(acc, [3, 8])

    with pytest.raises(ValueError, match="找不到序号"):
        am.remove_account_entry(acc, 99)

    assert _backups(acc) == []


def test_removing_the_last_account_is_rejected_without_a_backup(tmp_path: Path):
    acc = tmp_path / "accounts.json"
    _write_accounts(acc, [3])

    with pytest.raises(ValueError, match="最后一个账号"):
        am.remove_account_entry(acc, 3)

    assert _backups(acc) == []
    assert [e["index"] for e in _read(acc)] == [3]   # 原文件没被动


def test_update_of_unknown_index_is_rejected_without_a_backup(tmp_path: Path):
    acc = tmp_path / "accounts.json"
    _write_accounts(acc, [3])

    with pytest.raises(ValueError, match="找不到序号"):
        am.update_account_entry(acc, 99, {"region": "x"})

    assert _backups(acc) == []


# --------------------------------------------------------------------------
#  备份失败：降级告警，但不阻断
# --------------------------------------------------------------------------
def test_backup_failure_does_not_block_the_deletion(tmp_path, monkeypatch, caplog):
    """目录不可写就删不了账号，是很别扭的耦合 —— 所以降级为告警。"""
    acc = tmp_path / "accounts.json"
    _write_accounts(acc, [3, 8])
    monkeypatch.setattr(shutil, "copy2",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        backup = am.remove_account_entry(acc, 3)

    assert backup is None
    assert [e["index"] for e in _read(acc)] == [8]      # 删除照样成功
    assert "备份失败" in caplog.text


def test_backup_failure_warns_that_the_change_is_not_rollbackable(tmp_path, monkeypatch, caplog):
    """静默失败等于没备份 —— 必须有一条明确说「不可回滚」的告警。"""
    acc = tmp_path / "accounts.json"
    _write_accounts(acc, [3, 8])
    monkeypatch.setattr(shutil, "copy2",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        am.remove_account_entry(acc, 3)

    assert "不可回滚" in caplog.text


def test_missing_accounts_file_is_not_an_error_for_backup(tmp_path: Path):
    """首次写入前没有原文件可备份 —— 返回 None，不抛。"""
    assert am._backup_accounts_file(tmp_path / "nope.json") is None  # noqa: SLF001


# --------------------------------------------------------------------------
#  只留最近 5 份
# --------------------------------------------------------------------------
def test_only_the_last_five_backups_are_kept(tmp_path: Path):
    acc = tmp_path / "accounts.json"
    _write_accounts(acc, [1, 2, 3, 4, 5, 6, 7, 8])

    for index in range(1, 8):        # 删 7 次 → 该有 7 份备份
        am.remove_account_entry(acc, index)

    assert len(_backups(acc)) == 5


def test_pruning_drops_the_oldest_first(tmp_path: Path):
    """留的必须是**最近** 5 份 —— 时间戳字典序 == 时间序，排序即剪枝顺序。"""
    acc = tmp_path / "accounts.json"
    _write_accounts(acc, list(range(1, 9)))

    names = []
    for index in range(1, 8):
        backup = am.remove_account_entry(acc, index)
        assert backup is not None
        names.append(backup.name)

    kept = [p.name for p in _backups(acc)]
    assert kept == names[-5:]
    assert names[0] not in kept and names[1] not in kept


def test_backups_in_the_same_second_do_not_overwrite_each_other(tmp_path: Path):
    """微秒级时间戳：连点两下删除不能把第一份备份盖掉。"""
    acc = tmp_path / "accounts.json"
    _write_accounts(acc, [1, 2, 3, 4, 5, 6, 7, 8])

    for index in range(1, 8):
        am.remove_account_entry(acc, index)

    assert len({p.name for p in _backups(acc)}) == 5    # 名字全不重复


def test_pruning_never_touches_files_that_are_not_backups(tmp_path: Path):
    """剪枝只认 ``accounts-*.json``，别把用户自己丢进 backups/ 的东西删了。

    ⚠️ 这条要真能拦住「glob 写宽了」的变异，bystander 必须**排序在真备份之前**。
    实测教训：第一版放的是 ``accounts.example.json`` / ``notes.json``，
    它们排在 ``accounts-<时间戳>`` 之后，宽 glob 也侥幸删不到 → 变异存活、测试是假护栏。
    大写字母的 ASCII 比小写小，所以 ``AAA-`` 必然落在待删区。
    """
    acc = tmp_path / "accounts.json"
    backups = am.backup_dir(acc)
    backups.mkdir(parents=True)
    bystander = backups / "AAA-not-a-backup.json"
    bystander.write_text("[]", encoding="utf-8")
    for i in range(7):
        (backups / f"accounts-2026092{i}-000000-00000{i}.json").write_text(
            "[]", encoding="utf-8")

    deleted = am._prune_backups(backups)      # noqa: SLF001

    assert bystander.is_file(), "剪枝把手伸到了非备份文件上"
    assert deleted, "7 份备份应该触发剪枝，一个都没删说明逻辑没跑"
    assert all(p.name.startswith("accounts-") for p in deleted)


# --------------------------------------------------------------------------
#  端到端：菜单按钮 → 确认 → 落盘 + 备份 + 回话里带上回滚路径
#
#  service 层单独测过了，但「用户到底能不能看见那个回滚点」是另一回事 ——
#  备份默默躺在磁盘上，用户不知道，等于没备份。
# --------------------------------------------------------------------------
def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _harness(tmp_path: Path, indices: tuple[int, ...] = (3, 8)):
    """装一个写开关全开、accounts_file 指向临时文件的 Harness。"""
    from oracles.bot import handlers
    from tests.telegram_harness import Harness, make_settings

    settings = make_settings(indices, write=True, destructive=True)
    settings.accounts_file = tmp_path / "accounts.json"
    _write_accounts(settings.accounts_file, list(indices))
    return Harness(settings), handlers


def _remove_via_buttons(tmp_path: Path, index: int):
    """走真实回调链：cfgdel → cfd:<i> → ok:<token>，返回确认后的消息。"""
    harness, handlers = _harness(tmp_path)
    _run(harness.callback(handlers.on_callback, "cfgdel"))
    _run(harness.callback(handlers.on_callback, f"cfd:{index}"))
    token = next(iter(harness.store._pending))          # noqa: SLF001
    _query, msg = _run(harness.callback(handlers.on_callback, f"ok:{token}"))
    return harness, msg


def test_removing_through_the_buttons_writes_a_backup(tmp_path: Path):
    harness, _msg = _remove_via_buttons(tmp_path, 3)

    assert [e["index"] for e in _read(harness.settings.accounts_file)] == [8]
    assert len(_backups(harness.settings.accounts_file)) == 1


def test_the_reply_tells_the_user_where_the_backup_is(tmp_path: Path):
    """备份默默躺在磁盘上、用户不知道，等于没备份 —— 回话必须给出路径。"""
    harness, msg = _remove_via_buttons(tmp_path, 3)

    backup = _backups(harness.settings.accounts_file)[0]
    assert backup.name in msg.all_text, f"回话里没有备份文件名：{msg.all_text}"
    assert "backups" in msg.all_text
    assert "cp" in msg.all_text, "没告诉用户怎么回滚"


def test_a_failed_backup_is_admitted_in_the_reply(tmp_path, monkeypatch):
    """备份失败时不能装作备份好了 —— 必须明说不可回滚。"""
    monkeypatch.setattr(shutil, "copy2",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")))

    _harness_ignored, msg = _remove_via_buttons(tmp_path, 3)

    assert "备份失败" in msg.all_text
    assert "不可回滚" in msg.all_text
