"""开机向导「状态 → spec」的接线测试。

## 为什么需要这个文件

``_wz_build_spec`` 和 ``_wz_render_summary`` 之前**一条测试都没有**。
而 2026-09-20 那个最贵的 bug 恰好就在这条链路的末端：

    向导状态 → spec["user_data_b64"] → grab.try_launch_once → plan.user_data_b64
    → LaunchInstanceDetails(**kwargs)   ← 参数名写错，机器根本开不出来

前面每一段读起来都是对的，所以「读代码检查接线」永远不会失败（踩坑 #47）。
这里把可测的部分真的测起来：spec 的**内容**、**不含什么**、以及**渲染出来的文案**。

## 特别守住的几条

1. **密码不许进 spec。** spec 会被写进任务快照、日志、内存里的 ``GrabTask`` ——
   每多一份都是多一个泄漏面。密码只在 ``user_data_b64``（base64 的 cloud-init）里，
   用户自己刚输入过，忘了可以看聊天记录。
2. **``login_user`` 不许传给 ``plan_launch``。** 它只是为了在确认页显示。
   ``grab.try_launch_once`` 用白名单过滤 spec，这条把白名单也测了。
3. **确认页要显示用户名。** 用户原话：「新开的机器登录用户名 密码是什么」——
   原来只写「用户名+密码」，用户既没法核对，事后也无从查起。
4. **没给磁盘大小时用常量。** 这里原来写死 47，OCI 抬了下限后会变成第二个
   过时的默认值（踩坑 #48）。
"""
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from oracles.bot import handlers as H
from oracles.cloudinit import build_cloud_init
from oracles.models import BOOT_VOLUME_GB, E2_MICRO_SHAPE
from tests.telegram_harness import FakeContext, Harness, make_settings

CHAT = 1
USERNAME = "admin"
#: 夹具密码。**刻意**用一眼可辨的假值，别改回高熵串 ——
#: gitleaks 的 `generic-api-key` 规则会抓「`PASSWORD = <高熵串>`」这个形态，
#: 于是 CI 的 scan job 每次都红。而这里并不需要它像真密码：测试只要求它
#: **可辨识**（该出现在 cloud-init 里、不该出现在确认页和日志里）。
#: 按仓库约定（见 `tests/test_execute_launch.py` 那段说明），
#: `secret-scan:allow` 只给「**必须**出现的真形态」用，所以这里改值而不是加标记。
#: ⚠️ 连注释里都别把那串旧值抄一遍 —— 抄了照样会被扫到（第一版就栽在这）。
PASSWORD = "fixture-only-password"


def _harness() -> Harness:
    return Harness(make_settings())


def _spec(harness: Harness, **state) -> dict:
    base = {"account_index": 1, "shape": E2_MICRO_SHAPE, "count": 1}
    base.update(state)
    harness.store.put_wizard(CHAT, base)
    return H._wz_build_spec(FakeContext(harness.bot_data), CHAT)


# --------------------------------------------------------------------------
#  1. 密码不许进 spec
# --------------------------------------------------------------------------
def test_password_never_enters_the_spec() -> None:
    """🔴 spec 里不许出现密码明文 —— 它会被记进任务快照和日志。"""
    h = _harness()
    spec = _spec(h, login_mode="pwd", username=USERNAME, password=PASSWORD)

    flat = repr(spec)
    assert PASSWORD not in flat, (
        "密码出现在 spec 里了 —— spec 会被写进任务快照 / 日志 / GrabTask，"
        "每多一份都是多一个泄漏面。密码只应该存在于 base64 的 user_data 里。"
    )
    # 用户名可以留（只用于显示，且用户自己会看到）
    assert spec["login_user"] == USERNAME


def test_user_data_is_the_real_cloud_init() -> None:
    """spec 里的 ``user_data_b64`` 必须是**真的** cloud-init，且能解出用户名。

    这条把「向导 → spec」这一段钉死：内容不对，后面全白搭。
    """
    h = _harness()
    spec = _spec(h, login_mode="pwd", username=USERNAME, password=PASSWORD)

    raw = base64.b64decode(spec["user_data_b64"]).decode("utf-8")
    assert raw == build_cloud_init(username=USERNAME, password=PASSWORD), (
        "spec 里的 cloud-init 与 build_cloud_init 的输出不一致 —— "
        "是不是有人在这里又手搓了一份？"
    )
    assert f"name: {USERNAME}" in raw
    assert PASSWORD in raw          # 密码在 cloud-init 里（这是它该在的地方）


def test_ssh_key_mode_has_no_user_data() -> None:
    """选「SSH 公钥」时不许注入 cloud-init（否则会意外开出一个密码账号）。"""
    h = _harness()
    spec = _spec(h, login_mode="key")
    assert "user_data_b64" not in spec
    assert "login_user" not in spec


def test_password_mode_without_username_is_rejected() -> None:
    """选了「用户名+密码」却没给用户名 → 在**计划阶段**就报错，别等调 API。"""
    h = _harness()
    with pytest.raises(ValueError, match="没给用户名"):
        _spec(h, login_mode="pwd", password=PASSWORD)


def test_username_only_is_allowed() -> None:
    """只要用户名不要密码是合法的（``build_cloud_init`` 支持）。"""
    h = _harness()
    spec = _spec(h, login_mode="pwd", username=USERNAME)
    raw = base64.b64decode(spec["user_data_b64"]).decode("utf-8")
    assert f"name: {USERNAME}" in raw
    assert "chpasswd" not in raw, "没给密码却生成了 chpasswd —— 密码位会是空的"


# --------------------------------------------------------------------------
#  2. login_user 不许漏到 plan_launch
# --------------------------------------------------------------------------
def test_login_user_is_filtered_out_before_plan_launch() -> None:
    """``login_user`` 只是给确认页看的，不能传给 ``plan_launch``。

    ``plan_launch`` 不接受这个参数 —— 漏过去就是 ``TypeError``。
    过滤发生在 ``grab.try_launch_once`` 的白名单里，这里把那条白名单也测了。
    """
    from oracles.services.grab import PLAN_KWARG_WHITELIST, try_launch_once  # noqa: F401

    h = _harness()
    spec = _spec(h, login_mode="pwd", username=USERNAME, password=PASSWORD)
    assert "login_user" in spec, "前提：spec 里确实有 login_user"

    forwarded = {k: v for k, v in spec.items() if k in PLAN_KWARG_WHITELIST}
    assert "login_user" not in forwarded
    assert "user_data_b64" not in forwarded, (
        "user_data_b64 也不该走 plan_launch —— 它是 LaunchPlan 的字段，"
        "由 try_launch_once 单独接上去。"
    )


def test_boot_volume_falls_back_to_the_constant() -> None:
    """没给磁盘大小时必须用 ``BOOT_VOLUME_GB``，不许写死数字。

    这里原来是 ``int(st.get("boot_volume_gb", 47))`` ——
    OCI 把下限抬到 50 之后，这一处就成了**第二个过时的默认值**（踩坑 #48）。
    """
    h = _harness()
    spec = _spec(h, login_mode="key")          # 故意不给 boot_volume_gb
    assert spec["boot_volume_gb"] == BOOT_VOLUME_GB
    assert spec["boot_volume_gb"] >= 50, "又退回小于 OCI 下限的值了"


# --------------------------------------------------------------------------
#  3. 确认页必须让用户看得见用户名
# --------------------------------------------------------------------------
def test_summary_shows_the_username() -> None:
    """🔴 用户原话：「新开的机器登录用户名 密码是什么」。

    原来只写「登录：用户名+密码（cloud-init 注入，首启即用）」——
    用户既没法在执行前核对，事后也无从查起。现在必须显示用户名。
    """
    h = _harness()
    spec = _spec(h, login_mode="pwd", username=USERNAME, password=PASSWORD)
    text = H._wz_render_summary(spec, "[1] 测试")

    assert USERNAME in text, f"确认页没显示用户名：\n{text}"
    assert "密码" in text
    assert PASSWORD not in text, "确认页**不许**回显密码明文"


def test_summary_says_ssh_key_when_key_mode() -> None:
    h = _harness()
    spec = _spec(h, login_mode="key")
    text = H._wz_render_summary(spec, "[1] 测试")
    assert "SSH 公钥" in text
    assert "cloud-init" not in text


# --------------------------------------------------------------------------
#  4. 公钥来源：「用我配置的」vs「让 Bot 生成一对新的」
# --------------------------------------------------------------------------
GENERATED_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGENERATEDEXAMPLEKEY oracles-login-1"
GENERATED_FP = "SHA256:FAKEFINGERPRINTFORWIZARDTEST0000000000000"


def test_generated_key_pins_the_public_key_into_the_spec() -> None:
    """选了「让 Bot 生成」→ spec 里要带上**那一把**公钥。

    这条把「向导状态 → spec」钉死：不带上，``plan_launch`` 会退回
    默认解析链，用户以为用的是 Bot 生成的钥匙，实际注的是别的东西。
    """
    h = _harness()
    spec = _spec(h, login_mode="key", key_source="gen",
                 ssh_public_key=GENERATED_PUB, key_fingerprint=GENERATED_FP)
    assert spec["ssh_public_key"] == GENERATED_PUB
    assert spec["key_source"] == "gen"
    assert spec["key_fingerprint"] == GENERATED_FP


def test_private_key_never_enters_the_spec() -> None:
    """🔴 spec 里不许出现**私钥** —— 和密码同一条理由。

    spec 会被写进任务快照、日志、内存里的 ``GrabTask``，每多一份都是
    多一个泄漏面。私钥只应该存在于 ``<ORACLES_HOME>/keys/`` 和用户手里。

    ⚠️ 这里把私钥文本**同时**塞进向导状态的几个字段，模拟「有人顺手
       ``spec.update(st)``」—— 那是这条链路最容易出的错。

    ⚠️ 用的是**运行时真生成**的一把，而不是手写的假 PEM 字面量。
       两个原因：（1）最忠实 —— 它就是要防的那种字符串；
       （2）源码里不留 ``BEGIN ... PRIVATE KEY`` 字面量，
       否则会被自己的 `check_secrets.sh` 拦下，而按仓库约定
       `secret-scan:allow` 只给「**必须**出现的真形态」用
       （见 `tests/test_execute_launch.py` 那段说明）。
    """
    from oracles.sshkeys import generate_ed25519

    h = _harness()
    pub, private = generate_ed25519("oracles-login-1")
    assert "PRIVATE KEY" in private, "前提：生成出来的确实是 PEM 形态的私钥"

    spec = _spec(h, login_mode="key", key_source="gen",
                 ssh_public_key=pub, key_fingerprint=GENERATED_FP,
                 private_pem=private, private_key=private, password=private)

    assert private not in repr(spec), (
        "私钥出现在 spec 里了 —— spec 会被写进任务快照和日志。"
    )
    assert "PRIVATE KEY" not in repr(spec), "spec 里出现了私钥头"


def test_own_key_source_does_not_pin_any_public_key() -> None:
    """选「用我配置的公钥」时**不许**往 spec 里塞公钥。

    塞了就等于绕过 ``ORACLES_SSH_PUBLIC_KEY`` / 账号 key_file 那条解析链，
    用户改了配置却不生效。
    """
    h = _harness()
    spec = _spec(h, login_mode="key", key_source="own")
    assert "ssh_public_key" not in spec
    assert spec["key_source"] == "own"


def test_generated_source_without_the_key_falls_back_to_own() -> None:
    """状态写着 gen 但公钥丢了 → 退回「用配置的」，别产出一个半吊子 spec。

    ⚠️ 这条守的是判据里的**两个条件**：``key_source == "gen"``
       **且** ``ssh_public_key`` 存在。少了后半个，会产出
       ``ssh_public_key=None`` 的 spec —— 表面能用（退回解析链），
       但用户以为用的是生成的那把，事后对不上账。
    """
    h = _harness()
    spec = _spec(h, login_mode="key", key_source="gen")   # 故意不给公钥
    assert "ssh_public_key" not in spec
    assert spec["key_source"] == "own"


def test_password_mode_drops_any_leftover_key_state() -> None:
    """从「公钥」切到「密码」时，公钥状态要被清掉。

    不清的话 spec 里会同时带着 ``ssh_public_key`` 和 ``user_data`` ——
    两条登录路径一起注入，出了事谁也说不清哪条生效。
    """
    h = _harness()
    spec = _spec(h, login_mode="pwd", username=USERNAME, password=PASSWORD,
                 key_source="gen", ssh_public_key=GENERATED_PUB)
    assert "ssh_public_key" not in spec
    assert "key_source" not in spec
    assert "user_data_b64" in spec, "密码模式该有的东西不能被误删"


def test_summary_shows_the_generated_fingerprint() -> None:
    """确认页必须显示指纹 —— 那是用户唯一能核对的凭据。"""
    h = _harness()
    spec = _spec(h, login_mode="key", key_source="gen",
                 ssh_public_key=GENERATED_PUB, key_fingerprint=GENERATED_FP)
    text = H._wz_render_summary(spec, "[1] 测试")
    assert GENERATED_FP in text, f"确认页没显示指纹，用户没法核对：\n{text}"
    assert "Bot 生成" in text
    assert "私钥" in text, "没告诉用户私钥在哪 / 怎么拿"


def test_summary_of_own_key_mode_does_not_claim_it_was_generated() -> None:
    """对照组：选「用我配置的」时**不能**显示「Bot 生成」。

    ⚠️ 缺了这条，上面那条在「无条件打印 Bot 生成」的实现下也会通过 ——
       而那正是最坏的形态：用户以为 Bot 有私钥，其实它从来没生成过。
    """
    h = _harness()
    spec = _spec(h, login_mode="key", key_source="own")
    text = H._wz_render_summary(spec, "[1] 测试")
    assert "Bot 生成" not in text
    assert "SSH 公钥" in text


# --------------------------------------------------------------------------
#  5. 端到端接线：整条链子真的跑一遍
# --------------------------------------------------------------------------
def test_the_generated_key_survives_the_whole_chain_into_the_request_body() -> None:
    """🔴 向导状态 → spec → grab 白名单 → LaunchPlan → **真实 SDK 的请求体**。

    本文件里其他测试都是**逐段**的。踩坑 #47 说得很清楚：
    「逐段读代码，接线都对」是一个**不可能失败**的检查 ——
    2026-09-20 那个最贵的 bug（``user_data`` 写成顶层参数）就是
    每段都对、拼起来错，因为没人把整条链跑到底。

    所以这条刻意穿过每一层，断言最终请求体里是**哪一把**公钥。
    """
    import oci

    from oracles.services.compute import LaunchPlan, _launch_details_kwargs
    from oracles.services.grab import PLAN_KWARG_WHITELIST

    h = _harness()
    spec = _spec(h, login_mode="key", key_source="gen",
                 ssh_public_key=GENERATED_PUB, key_fingerprint=GENERATED_FP)

    # ① 走 grab.try_launch_once 用的**同一份白名单常量**（不是另抄一份 ——
    #    另抄的话「往白名单加键」这件事测试根本管不着）
    plan_kwargs = {k: v for k, v in spec.items() if k in PLAN_KWARG_WHITELIST}
    assert plan_kwargs.get("ssh_public_key") == GENERATED_PUB, (
        "公钥没通过 grab 的白名单 —— plan_launch 会退回默认解析链，"
        "用户以为用的是 Bot 生成的那把，实际注的是别的东西。"
    )

    # ② 构造计划（plan_launch 里 ssh_public_key 就是这个 kwarg 直传）
    plan = LaunchPlan(
        account_index=1, account_label="[1] 测试", region="example-region-1",
        display_name="t", availability_domain="ad-1", shape=plan_kwargs["shape"],
        boot_volume_gb=plan_kwargs["boot_volume_gb"],
        image_id="ocid1.image.oc1..EXAMPLE", image_name="img",
        subnet_id="ocid1.subnet.oc1..EXAMPLE", subnet_name="sn",
        ssh_public_key=plan_kwargs["ssh_public_key"],
        ssh_key_fingerprint=spec.get("key_fingerprint"),
    )

    # ③ 真实 SDK 校验过的请求体（参数名写错在这里就炸）
    client = SimpleNamespace(compartment_id="ocid1.compartment.oc1..EXAMPLE")
    kwargs = _launch_details_kwargs(client, plan)
    oci.core.models.LaunchInstanceDetails(**kwargs)
    assert kwargs["metadata"]["ssh_authorized_keys"] == GENERATED_PUB, (
        "最终请求体里的公钥不是生成的那把"
    )
    assert spec.get("key_fingerprint") in plan.render()
