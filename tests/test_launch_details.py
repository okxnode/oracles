"""创建实例的参数拼装测试 —— 用**真实 OCI SDK** 校验。

**这个文件是 2026-09-20 真机验证的直接产物。**

## 撞出来的 bug

在 <生产VPS的IP> 上真开一台机器时，SDK 直接抛：

    TypeError: Unrecognized keyword arguments: user_data

`oci.core.models.LaunchInstanceDetails` 的 32 个合法参数里**没有 `user_data`**
（只有 `metadata` / `extended_metadata`）。OCI 的约定是把 cloud-init 文本
base64 后放进 `metadata["user_data"]`。

后果不是「密码没生效」这么轻 —— 是**「向导里选用户名+密码」这条路在 SDK
调用层就崩了，机器根本开不出来**。

## 为什么之前没发现

因为 `plan.user_data_b64` 一路接得都对：向导 → spec → grab → plan，
每个环节都有测试。**唯独最后那一句 `LaunchInstanceDetails(**kwargs)` 是错的，
而它在所有测试里都被假客户端绕过去了**（假客户端只记录「调用了一次 launch」，
从不校验 kwargs）。

这正是「接线正确」这种结论的危险之处：**逐段读代码看起来全对，
但没人真的把整条链子跑到底**。所以这里的断言刻意走**真实 SDK**：

    oci.core.models.LaunchInstanceDetails(**kwargs)

参数名写错 → 立刻 TypeError。不需要凭据、不联网、毫秒级。
"""
from __future__ import annotations

from types import SimpleNamespace

import oci
import pytest

from oracles.services.compute import LaunchPlan, _launch_details_kwargs

FAKE_USER_DATA = "I2Nsb3VkLWNvbmZpZwpzc2hfcHdhdXRoOiB0cnVlCg=="
FAKE_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEKEYEXAMPLEKEY oracles"


def make_plan(**over: object) -> LaunchPlan:
    base: dict = {
        "account_index": 15,
        "account_label": "[15] 测试账号",
        "region": "sa-vinhedo-1",
        "display_name": "oracles-15-1",
        "availability_domain": "GMQx:SA-VINHEDO-1-AD-1",
        "shape": "VM.Standard.A1.Flex",
        "boot_volume_gb": 50,
        "image_id": "ocid1.image.oc1..EXAMPLE",
        "image_name": "Canonical-Ubuntu-26.04-Minimal-aarch64",
        "subnet_id": "ocid1.subnet.oc1..EXAMPLE",
        "subnet_name": "subnet-example",
        "ssh_public_key": FAKE_PUBKEY,
        "shape_config": {"ocpus": 1, "memory_gb": 6},
    }
    base.update(over)
    return LaunchPlan(**base)


def make_client():
    """`_launch_details_kwargs` 只读 `compartment_id` 这一个字段。"""
    return SimpleNamespace(compartment_id="ocid1.compartment.oc1..EXAMPLE")


# ---------------------------------------------------------------------------
#  🔴 主闸门：kwargs 必须被真实 SDK 接受
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("over", [
    pytest.param({}, id="普通"),
    pytest.param({"user_data_b64": FAKE_USER_DATA}, id="带-cloud-init"),
    pytest.param({"assign_ipv6": True, "user_data_b64": FAKE_USER_DATA}, id="带-IPv6"),
    pytest.param({"shape": "VM.Standard.E2.1.Micro", "shape_config": None},
                 id="E2-无-shape_config"),
    pytest.param({"ssh_public_key": None, "user_data_b64": None}, id="什么都不注入"),
])
def test_kwargs_are_accepted_by_the_real_sdk(over: dict) -> None:
    """🔴 核心回归。参数名写错 → 这里就红，而不是等真开机才炸。

    `LaunchInstanceDetails.__init__` 只认 `swagger_types` 里声明过的键，
    多一个都会抛 `TypeError: Unrecognized keyword arguments: ...`。
    """
    kwargs = _launch_details_kwargs(make_client(), make_plan(**over))
    try:
        oci.core.models.LaunchInstanceDetails(**kwargs)
    except TypeError as exc:
        raise AssertionError(
            f"SDK 不接受这些参数：{exc}\n"
            f"传入的键：{sorted(kwargs)}"
        ) from exc


def test_user_data_is_not_a_top_level_kwarg() -> None:
    """🔴 定点回归：`user_data` 不能出现在顶层。

    它必须进 `metadata["user_data"]` —— 这是 OCI 的约定，不是可选项。
    """
    kwargs = _launch_details_kwargs(make_client(),
                                    make_plan(user_data_b64=FAKE_USER_DATA))
    assert "user_data" not in kwargs, (
        "又把 user_data 当成顶层参数了 —— SDK 会抛 "
        "TypeError: Unrecognized keyword arguments: user_data"
    )
    assert kwargs["metadata"]["user_data"] == FAKE_USER_DATA


def test_user_data_and_ssh_key_share_one_metadata_dict() -> None:
    """两者都在 metadata 里，不能因为加了 user_data 把公钥挤掉。"""
    kwargs = _launch_details_kwargs(make_client(),
                                    make_plan(user_data_b64=FAKE_USER_DATA))
    md = kwargs["metadata"]
    assert md["user_data"] == FAKE_USER_DATA
    assert md["ssh_authorized_keys"] == FAKE_PUBKEY


def test_metadata_key_is_omitted_when_nothing_to_inject() -> None:
    """没东西要注入就不要塞一个空 metadata 上去。"""
    kwargs = _launch_details_kwargs(
        make_client(), make_plan(ssh_public_key=None, user_data_b64=None))
    assert "metadata" not in kwargs


# ---------------------------------------------------------------------------
#  其余字段的落点
# ---------------------------------------------------------------------------
def test_ipv6_flag_lands_on_the_vnic() -> None:
    """`assign_ipv6_ip` 是 CreateVnicDetails 的参数，不是实例的。"""
    kwargs = _launch_details_kwargs(make_client(), make_plan(assign_ipv6=True))
    assert kwargs["create_vnic_details"].assign_ipv6_ip is True
    assert kwargs["create_vnic_details"].assign_public_ip is True


def test_ipv6_flag_absent_by_default() -> None:
    kwargs = _launch_details_kwargs(make_client(), make_plan())
    assert kwargs["create_vnic_details"].assign_ipv6_ip is None


def test_a1_shape_config_is_attached() -> None:
    kwargs = _launch_details_kwargs(make_client(), make_plan())
    assert kwargs["shape_config"].ocpus == 1.0
    assert kwargs["shape_config"].memory_in_gbs == 6.0


def test_e2_has_no_shape_config() -> None:
    """E2.1.Micro 是固定规格，传 shape_config 反而会被 OCI 拒。"""
    kwargs = _launch_details_kwargs(
        make_client(), make_plan(shape="VM.Standard.E2.1.Micro", shape_config=None))
    assert "shape_config" not in kwargs


def test_boot_volume_size_goes_to_source_details() -> None:
    kwargs = _launch_details_kwargs(make_client(), make_plan(boot_volume_gb=100))
    assert kwargs["source_details"].boot_volume_size_in_gbs == 100


# ---------------------------------------------------------------------------
#  Bot 生成的登录密钥（2026-09-21 新增）
# ---------------------------------------------------------------------------
def test_a_generated_login_key_is_what_actually_gets_injected() -> None:
    """🔴 注入的必须是**那一把**生成的公钥，不是夹具里那把。

    前面每一段（sshkeys 生成 → 向导状态 → spec → plan_launch）都有测试，
    但没有一条验证过**最终发出去的请求体**里是哪个公钥 ——
    而那才是决定用户能不能登进去的东西。
    """
    from oracles.sshkeys import generate_ed25519

    pub, _priv = generate_ed25519("oracles-login-15")
    assert pub != FAKE_PUBKEY, "前提：生成的公钥与夹具不同"
    kwargs = _launch_details_kwargs(make_client(), make_plan(ssh_public_key=pub))
    assert kwargs["metadata"]["ssh_authorized_keys"] == pub


def test_the_plan_shows_a_fingerprint_users_can_verify_against() -> None:
    """计划里要带指纹 —— 光写「已注入」核对不出注的是哪一把。"""
    from oracles.sshkeys import fingerprint_of, generate_ed25519

    pub, _priv = generate_ed25519("oracles-login-15")
    fp = fingerprint_of(pub)
    plan = make_plan(ssh_public_key=pub, ssh_key_fingerprint=fp)
    assert fp in plan.render(), f"计划里没显示指纹：\n{plan.render()}"


def test_a_malformed_configured_key_does_not_block_the_launch() -> None:
    """配置里那把公钥格式不标准时，只是**显示不出指纹**，不该开不了机。

    OCI 自己会校验公钥。我们因为算不出指纹就把整台机器拦下，
    是把「展示问题」升级成「功能故障」—— 而用户会以为是 OCI 的问题。
    """
    from oracles.services.compute import _fingerprint_or_none
    from oracles.sshkeys import generate_ed25519

    assert _fingerprint_or_none("这不是公钥") is None
    assert _fingerprint_or_none(None) is None
    # 对照组：合法公钥必须算得出来，否则上面两条在「永远返回 None」时也过
    pub, _priv = generate_ed25519("x")
    assert _fingerprint_or_none(pub) is not None


def test_a_plan_without_a_key_says_so_instead_of_showing_a_blank() -> None:
    """没注入公钥时要明说「未注入」，不能只留一个空字段。"""
    text = make_plan(ssh_public_key=None, ssh_key_fingerprint=None).render()
    assert "未注入" in text
    assert "SSH 公钥" in text
