"""登录密钥对（``oracles/sshkeys.py``）的生成 / 落盘 / 指纹测试。

## 为什么指纹那一条必须调用**真的** ``ssh-keygen``

Bot 里显示的指纹是给用户**核对**用的：用户会拿
``ssh-keygen -lf <公钥>`` 的输出跟它比。如果我们的算法跟 OpenSSH 差一个字符，
用户会得出「密钥是坏的」这个结论 —— 而密钥其实是好的。

自己跟自己对（比如断言「指纹长度是 51」）**结构上不可能发现这种偏差**。
所以这里真的起一个子进程调 ``ssh-keygen``，把两边的输出逐字符比。

## 为什么「拒绝覆盖」要被测

覆盖会让**已经开出去的机器连不上** —— 那些实例的 ``authorized_keys`` 里
还是旧公钥，而磁盘上是新私钥，配不上。这种故障极难查：
私钥文件看起来好好的，就是登不进去。
"""
from __future__ import annotations

import logging
import shutil
import stat
import subprocess
from dataclasses import replace

import pytest

from oracles import sshkeys
from oracles.errors import ConfigError
from tests.telegram_harness import make_settings

#: 有没有 ssh-keygen —— 没有就跳过那两条交叉验证（而不是让它们假装通过）
_HAS_SSH_KEYGEN = shutil.which("ssh-keygen") is not None
needs_ssh_keygen = pytest.mark.skipif(
    not _HAS_SSH_KEYGEN, reason="本机没有 ssh-keygen，无法做交叉验证"
)


@pytest.fixture()
def settings(tmp_path):
    """指向临时目录的 Settings —— 密钥会真的落盘，但落在 tmp 里。"""
    return replace(make_settings(), home=tmp_path)


# --------------------------------------------------------------------------
#  1. 指纹必须与 OpenSSH 一致
# --------------------------------------------------------------------------
@needs_ssh_keygen
def test_fingerprint_matches_ssh_keygen(settings) -> None:
    """🔴 交叉验证：我们的指纹 == ``ssh-keygen -lf`` 的输出。

    这条是给用户看的那个数字的唯一保证。算法自己改坏了，
    除了真跑一遍 ssh-keygen 没有别的办法发现。
    """
    key = sshkeys.ensure(settings, 15)
    out = subprocess.run(["ssh-keygen", "-lf", str(key.public_path)],
                         capture_output=True, text=True, check=True)
    real = out.stdout.split()[1]
    assert key.fingerprint == real, (
        f"指纹与 ssh-keygen 不一致：\n"
        f"  我们算的  : {key.fingerprint}\n"
        f"  ssh-keygen: {real}\n"
        f"用户拿 ssh-keygen 核对时会以为密钥是坏的。"
    )


@needs_ssh_keygen
def test_private_key_is_usable_by_ssh(settings) -> None:
    """生成的私钥必须真能被 ssh 工具链解析（用 ``-y`` 反推公钥比对）。

    ⚠️ 光断言「文件里有 ``BEGIN OPENSSH PRIVATE KEY``」是不够的 ——
       那只能证明写了个头。``ssh-keygen -y`` 会真的把它读成密钥。
    """
    key = sshkeys.ensure(settings, 15)
    out = subprocess.run(["ssh-keygen", "-y", "-f", str(key.private_path)],
                         capture_output=True, text=True, check=True)
    derived = out.stdout.strip()
    expected = " ".join(key.public_openssh.split()[:2])
    assert derived == expected, (
        "从私钥反推出的公钥与公钥文件不一致 —— 这一对配不上，"
        f"用户拿它登录会 Permission denied。\n  反推: {derived}\n  文件: {expected}"
    )


def test_fingerprint_format_is_openssh_style(settings) -> None:
    """指纹形态：``SHA256:`` 前缀 + 无 ``=`` 填充的 base64。"""
    fp = sshkeys.ensure(settings, 15).fingerprint
    assert fp.startswith("SHA256:")
    assert "=" not in fp, "OpenSSH 的 SHA256 指纹不带 '=' 填充"
    assert len(fp) == len("SHA256:") + 43


# --------------------------------------------------------------------------
#  2. 落盘位置与权限
# --------------------------------------------------------------------------
def test_files_land_under_oracles_home_with_tight_permissions(settings, tmp_path) -> None:
    """私钥 600、公钥 644、目录 700 —— 且必须在 ORACLES_HOME 之内。

    密钥**不能**落在仓库里（这是 accounts.json 同样的理由）：
    仓库要开源，密钥从架构上就不该有进去的路径。
    """
    key = sshkeys.ensure(settings, 15)

    assert key.private_path == tmp_path / "keys" / "15" / "login_key"
    assert key.public_path == tmp_path / "keys" / "15" / "login_key.pub"

    assert stat.S_IMODE(key.private_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key.public_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(key.private_path.parent.stat().st_mode) == 0o700


def test_private_key_is_not_group_or_other_readable(settings) -> None:
    """单独再钉一条：私钥的权限位里不许出现 group/other 的任何位。

    「等于 600」和「不含 0o077」在 umask 千奇百怪的环境下不是一回事 ——
    这里按**不允许**的语义断言，跟上面的等值断言互为补充。
    """
    key = sshkeys.ensure(settings, 15)
    mode = stat.S_IMODE(key.private_path.stat().st_mode)
    assert not (mode & 0o077), f"私钥权限 {mode:03o} 允许同组/其他用户读取"


# --------------------------------------------------------------------------
#  3. 幂等与「拒绝覆盖」
# --------------------------------------------------------------------------
def test_ensure_is_idempotent(settings) -> None:
    """同一账号第二次 ensure 必须**读回原来那把**，不是重新生成。

    这是「每账号一对、后续复用」这个策略的落点：
    重新生成会让所有已开出去的机器全部失联。
    """
    first = sshkeys.ensure(settings, 15)
    second = sshkeys.ensure(settings, 15)

    assert first.reused is False, "第一次应该是新生成"
    assert second.reused is True, "第二次应该读已有的"
    assert second.fingerprint == first.fingerprint
    assert second.private_pem == first.private_pem


def test_save_refuses_to_overwrite(settings) -> None:
    """🔴 已有密钥时 ``save`` 必须报错，绝不覆盖。"""
    sshkeys.ensure(settings, 15)
    with pytest.raises(ConfigError, match="拒绝覆盖"):
        sshkeys.save(settings, 15, "ssh-ed25519 AAAAEXAMPLE x", "-----BEGIN-----\n")


def test_overwrite_refusal_does_not_touch_the_existing_key(settings) -> None:
    """被拒绝之后，原来那把必须**原封不动** —— 报错了却把文件改坏是最糟的。

    ⚠️ 只断言「抛了异常」是不够的：异常可能在文件已经被写坏之后才抛。
       这里比指纹。
    """
    before = sshkeys.ensure(settings, 15)
    with pytest.raises(ConfigError):
        sshkeys.save(settings, 15, "ssh-ed25519 AAAAEXAMPLE x", "-----BEGIN-----\n")
    after = sshkeys.load(settings, 15)
    assert after is not None
    assert after.fingerprint == before.fingerprint


def test_keys_of_different_accounts_are_independent(settings) -> None:
    """不同账号各一对，互不干扰。"""
    a = sshkeys.ensure(settings, 1)
    b = sshkeys.ensure(settings, 2)
    assert a.fingerprint != b.fingerprint
    assert a.private_path.parent != b.private_path.parent
    assert sshkeys.ensure(settings, 1).fingerprint == a.fingerprint


# --------------------------------------------------------------------------
#  4. 读取：缺失 vs 损坏
# --------------------------------------------------------------------------
def test_load_returns_none_when_nothing_exists(settings) -> None:
    """一个都没有 → ``None``（「确实没有」，不是「读失败」）。"""
    assert sshkeys.load(settings, 15) is None


def test_load_detects_a_half_written_pair(settings) -> None:
    """🔴 只有一半不是「没有」，是**损坏** —— 必须报错。

    如果这里静默返回 None，上层会重新生成一把，
    把已经开出去的机器留在「旧公钥 + 新私钥」的错配状态里。
    """
    key = sshkeys.ensure(settings, 15)
    key.private_path.unlink()
    with pytest.raises(ConfigError, match="不完整"):
        sshkeys.load(settings, 15)


def test_load_rejects_an_empty_public_key(settings) -> None:
    """空的公钥文件也是损坏，不是「没有公钥」。"""
    key = sshkeys.ensure(settings, 15)
    key.public_path.write_text("", encoding="ascii")
    with pytest.raises(ConfigError, match="空"):
        sshkeys.load(settings, 15)


def test_loose_permissions_are_reported(settings, caplog) -> None:
    """私钥被改成 644 时要留下 WARNING —— 不阻断，但必须说。

    静默容忍一个全世界可读的私钥，等于把这个事实藏起来。
    """
    key = sshkeys.ensure(settings, 15)
    key.private_path.chmod(0o644)
    with caplog.at_level(logging.WARNING, logger="oracles.sshkeys"):
        sshkeys.load(settings, 15)
    assert any("权限" in r.message for r in caplog.records), (
        "私钥权限被放宽到 644 了，却没有任何警告"
    )


def test_no_warning_when_permissions_are_tight(settings, caplog) -> None:
    """对照组：权限正常时**不该**有警告。

    ⚠️ 缺了这条，上面那条在「永远报警告」的实现下也会通过。
    """
    sshkeys.ensure(settings, 15)
    with caplog.at_level(logging.WARNING, logger="oracles.sshkeys"):
        sshkeys.load(settings, 15)
    assert not [r for r in caplog.records if "权限" in r.message]


# --------------------------------------------------------------------------
#  5. 公钥文本本身
# --------------------------------------------------------------------------
def test_public_key_carries_an_account_specific_comment(settings) -> None:
    """公钥行尾要有能区分账号的注释。

    cryptography 导出的 OpenSSH 公钥**不带** comment；
    不补的话几把钥匙长得一模一样（前几十个字符都相同），
    用户对着 ``authorized_keys`` 分不出谁是谁。
    """
    key = sshkeys.ensure(settings, 15)
    assert key.comment == "oracles-login-15"
    assert key.public_openssh.startswith("ssh-ed25519 ")
    assert key.public_openssh.count("\n") == 0


def test_blob_of_is_tolerant_of_junk() -> None:
    """格式古怪的公钥行只能得到空串，**不许抛异常**。

    实例 metadata 里的公钥是历史留下的，可能是手抄错的行。
    为了展示一行诊断信息就炸掉整个操作，是把展示问题升级成功能故障。
    """
    assert sshkeys.blob_of("") == ""
    assert sshkeys.blob_of("garbage") == ""
    assert sshkeys.blob_of("ssh-ed25519") == ""
    assert sshkeys.blob_of("ssh-ed25519 AAAAx c") == "AAAAx"


def test_fingerprint_of_rejects_junk_loudly() -> None:
    """但 ``fingerprint_of`` 本身要**大声报错** —— 它是校验入口，不能糊过去。

    和 ``blob_of`` 的宽容是刻意的分工：一个用于校验（严格），
    一个用于展示（宽容）。
    """
    with pytest.raises(ValueError):
        sshkeys.fingerprint_of("garbage")
    with pytest.raises(ValueError):
        sshkeys.fingerprint_of("ssh-ed25519 not-base64!!! x")


def test_render_mentions_the_fingerprint_and_the_risk(settings) -> None:
    """交付文案必须同时给出**指纹**（用于核对）和**风险**（账号级复用）。

    只写「密钥已生成」等于没说：用户没法核对，也不知道这把钥匙
    同时是那个账号下所有机器的钥匙。
    """
    key = sshkeys.ensure(settings, 15)
    text = sshkeys.render(key, account_label="[15] 测试")
    assert key.fingerprint in text
    assert "[15] 测试" in text
    assert "账号级" in text or "复用" in text, "没提醒这是账号级复用的钥匙"
