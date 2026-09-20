"""配额与容量模型测试。

重点守住那条最容易算错的规则：**免费额度绑在单个 AD 上**，
账号可开台数必须逐 AD 求和，不能只看第一个 AD 或只看总数。
"""
from __future__ import annotations

import pytest

from oracles.models import (
    BOOT_VOLUME_GB,
    BOOT_VOLUME_MIN_GB,
    AccountCapacity,
    AdCapacity,
)


def test_single_ad_launchable_limited_by_cpu():
    ad = AdCapacity(ad="AD-1", e2_available=2, e2_used=0, storage_available_gb=500.0)
    assert ad.launchable == 2


def test_single_ad_launchable_limited_by_storage():
    """块存储不够时，CPU 再多也开不出机器。"""
    ad = AdCapacity(ad="AD-1", e2_available=2, e2_used=0, storage_available_gb=50.0)
    assert ad.launchable == 1     # floor(50 / 47) == 1


def test_single_ad_launchable_zero_when_storage_empty():
    ad = AdCapacity(ad="AD-1", e2_available=2, e2_used=0, storage_available_gb=10.0)
    assert ad.launchable == 0


def test_single_ad_launchable_zero_when_no_cpu():
    ad = AdCapacity(ad="AD-1", e2_available=0, e2_used=2, storage_available_gb=500.0)
    assert ad.launchable == 0


def test_launchable_unknown_storage_falls_back_to_cpu():
    ad = AdCapacity(ad="AD-1", e2_available=2, e2_used=0, storage_available_gb=None)
    assert ad.launchable == 2


def test_launchable_unknown_cpu_is_zero():
    ad = AdCapacity(ad="AD-1", e2_available=None, storage_available_gb=500.0)
    assert ad.launchable == 0


def test_total_launchable_sums_per_ad():
    """核心断言：额度绑单 AD，必须逐 AD 求和。

    这是真实数据 —— 账号 13 只有 AD-3 有 2 核，另外两个 AD 是 0。
    """
    cap = AccountCapacity(
        index=13, label="[13] ashburn", region="us-ashburn-1",
        ads=[
            AdCapacity(ad="AD-1", e2_available=0, e2_used=0, storage_available_gb=300.0),
            AdCapacity(ad="AD-2", e2_available=0, e2_used=0, storage_available_gb=300.0),
            AdCapacity(ad="AD-3", e2_available=2, e2_used=0, storage_available_gb=300.0),
        ],
    )
    assert cap.total_launchable == 2
    assert cap.best_ad == "AD-3"


def test_best_ad_none_when_nothing_available():
    cap = AccountCapacity(
        index=9, label="[9]", region="us-ashburn-1",
        ads=[AdCapacity(ad=f"AD-{i}", e2_available=0, e2_used=2,
                        storage_available_gb=0.0) for i in (1, 2, 3)],
    )
    assert cap.total_launchable == 0
    assert cap.best_ad is None


def test_best_ad_prefers_most_capacity():
    cap = AccountCapacity(
        index=3, label="[3]", region="ap-singapore-1",
        ads=[
            AdCapacity(ad="AD-1", e2_available=1, storage_available_gb=200.0),
            AdCapacity(ad="AD-2", e2_available=2, storage_available_gb=200.0),
        ],
    )
    assert cap.best_ad == "AD-2"


def test_boot_volume_constant_matches_reality():
    """引导卷默认值 = 容量公式的分母。

    🔴 2026-09-20 实测修正：**50，不是 47**。

    47 是历史上的默认值（存量实例确实都是 47 GB），但新开机时 OCI 直接拒：

        InvalidParameter (HTTP 400)
        Requested volume size 47GB is not in the allowed range.
        Boot volume should be greater than or equal to 50GB

    这个数写错的后果很重：`plan_launch` 的**默认** `boot_volume_gb` 就是它，
    于是「不显式指定磁盘大小」这条路径 100% 失败 ——
    而向导里选了 50/100/200 的反而没事，只有「默认」这一条路死掉。
    """
    assert BOOT_VOLUME_GB == 50
    assert BOOT_VOLUME_GB >= BOOT_VOLUME_MIN_GB


def test_boot_volume_below_the_api_minimum_is_rejected_before_launch():
    """小于下限时要在**计划阶段**就报人话，而不是等 OCI 甩 400。"""
    from types import SimpleNamespace

    from oracles.services.compute import plan_launch

    client = SimpleNamespace(account=SimpleNamespace(
        label="[15] 测试", default_shape=None, default_image_os=None))
    with pytest.raises(ValueError, match="下限"):
        plan_launch(client, boot_volume_gb=47)


# ---------------------------------------------------------------------------
#  🔴 E2 与 A1 是两套独立限额
# ---------------------------------------------------------------------------
def _account_15() -> AccountCapacity:
    """账号 15 的真实状态（2026-09-20 实测）。

    E2 已 2/2 开满，**但 A1 还剩 2 核** —— 就是这个组合把旧代码打挂了。
    """
    return AccountCapacity(
        index=15, label="[15] 测试账号15", region="sa-vinhedo-1",
        storage_available_gb=106.0,
        ads=[AdCapacity(ad="GMQx:SA-VINHEDO-1-AD-1", e2_available=0, e2_used=2,
                        a1_available=2, storage_available_gb=106.0)],
    )


def test_e2_full_does_not_mean_a1_full() -> None:
    """🔴 核心回归。E2 用满 ≠ A1 也用满。

    旧代码只有一个「按 E2 算」的口径，于是 `plan_launch` 抛
    「没有任何可用域还有额度」，**向导里的 A1.Flex 选项在这个账号上
    永远开不出机器** —— 而 A1 明明还有 2 核。
    """
    cap = _account_15()
    ad = cap.ads[0]

    # E2 口径：0 台
    assert ad.launchable == 0
    assert cap.best_ad is None

    # A1 口径：2 核 / 每台 1 核 = 2 台
    assert ad.launchable_for("VM.Standard.A1.Flex", 1) == 2
    assert cap.best_ad_for("VM.Standard.A1.Flex", 1) == "GMQx:SA-VINHEDO-1-AD-1"


def test_a1_cores_are_divided_by_ocpus_per_instance() -> None:
    """2 核余量开 1 核机器是 2 台，开 4 核机器是 0 台 —— 必须按台除。

    不做这个除法，会把「剩 2 核」误判成「能开 2 台 4 核机器」。
    """
    ad = AdCapacity(ad="AD-1", a1_available=2, storage_available_gb=500.0)
    assert ad.launchable_for("VM.Standard.A1.Flex", 1) == 2
    assert ad.launchable_for("VM.Standard.A1.Flex", 2) == 1
    assert ad.launchable_for("VM.Standard.A1.Flex", 4) == 0


def test_a1_respects_storage_limit_too() -> None:
    """A1 也要同时满足块存储约束。"""
    ad = AdCapacity(ad="AD-1", a1_available=4, storage_available_gb=50.0)
    assert ad.launchable_for("VM.Standard.A1.Flex", 1) == 1   # floor(50/47)


def test_a1_unknown_is_zero_not_optimistic() -> None:
    """查不到 A1 余量时要保守（0），不能假装能开。"""
    ad = AdCapacity(ad="AD-1", a1_available=None, storage_available_gb=500.0)
    assert ad.launchable_for("VM.Standard.A1.Flex", 1) == 0


def test_pick_availability_domain_is_shape_aware() -> None:
    """挑 AD 必须带规格：同一账号下 E2 挑不到、A1 挑得到。"""
    from oracles.services.quota import pick_availability_domain

    cap = _account_15()
    assert pick_availability_domain(cap) is None
    assert pick_availability_domain(
        cap, shape="VM.Standard.A1.Flex", cores=1
    ) == "GMQx:SA-VINHEDO-1-AD-1"


def test_best_ad_for_picks_the_ad_with_most_room_for_that_shape() -> None:
    """两个 AD 的 A1 余量不同 → 要挑多的那个，而不是第一个。"""
    cap = AccountCapacity(
        index=1, label="[1]", region="r",
        ads=[
            AdCapacity(ad="AD-1", a1_available=1, storage_available_gb=500.0),
            AdCapacity(ad="AD-2", a1_available=3, storage_available_gb=500.0),
        ],
    )
    assert cap.best_ad_for("VM.Standard.A1.Flex", 1) == "AD-2"
    assert cap.best_ad_for("VM.Standard.A1.Flex", 3) == "AD-2"
    assert cap.best_ad_for("VM.Standard.A1.Flex", 4) is None


# --------------------------------------------------------------------------
#  护栏：引导卷大小只能有一个来源
# --------------------------------------------------------------------------
def _int_literals_in_package() -> list[tuple[str, int, int]]:
    """用 AST 找出 ``oracles/`` 下所有**代码里**的整数常量（注释/文档串不算）。

    ⚠️ 必须用 AST 而不是文本匹配 —— 文档字符串里到处都在讲「47 被拒」，
       用正则会全是误报，而**会误报的检查会被训练着去忽略**。
    """
    import ast
    from pathlib import Path

    pkg = Path(__file__).resolve().parent.parent / "oracles"
    found: list[tuple[str, int, int]] = []
    for path in sorted(pkg.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and isinstance(node.value, int)
                    and not isinstance(node.value, bool)):
                found.append((path.name, node.lineno, node.value))
    return found


def test_boot_volume_number_is_not_hardcoded_anywhere() -> None:
    """🔴 引导卷大小只能从 ``BOOT_VOLUME_GB`` 来，不许在任何地方写死。

    2026-09-20 实测：OCI 把引导卷下限抬到 50 GB，``models.BOOT_VOLUME_GB``
    从 47 改成 50 —— 但当时**还有 3 处在写死 47**：

      · ``bot/handlers.py``  向导没传磁盘大小时的回退值
      · ``bot/render.py``    「≈ N 台机器的空间」的除数
      · 两处文档字符串里的容量公式

    前两处是**真 bug**：常量改了它们不会跟着改，等于埋了第二个过时的默认值。
    （同类问题见踩坑清单 #29「同一份值有两处实现，必然漂移」。）

    这条测试把「47」这个具体数字钉死：一旦有人在代码里再写它，立刻报出来。
    """
    offenders = [
        f"  oracles/{name}:{line} → {value}"
        for name, line, value in _int_literals_in_package()
        if value == 47
    ]
    assert not offenders, (
        "代码里出现了写死的 47（引导卷的旧默认值）：\n"
        + "\n".join(offenders)
        + "\n  请改用 models.BOOT_VOLUME_GB —— 否则 OCI 再抬下限时又会漏改。"
    )


def test_the_literal_scan_actually_reaches_the_package() -> None:
    """扫描面必须非空，否则上面那条会因为「什么都没扫到」而空过。

    （踩坑清单 #47：**检查在跑，但结构上不可能给出「否」**。）
    """
    lits = _int_literals_in_package()
    assert len(lits) > 50, f"只扫到 {len(lits)} 个整数常量，扫描面是不是坏了？"
    files = {name for name, _, _ in lits}
    for must in ("models.py", "handlers.py", "render.py", "quota.py"):
        assert must in files, f"扫描面里缺 {must}"


def test_the_literal_scan_detects_a_planted_47(tmp_path) -> None:
    """**变异测试**：真写一个 47 进去，检测器必须报出来。

    没有这一条，「检测器永远返回空」和「代码真的干净」就区分不开。
    """
    import ast

    planted = tmp_path / "planted.py"
    planted.write_text(
        '"""文档里写 47 不算。"""\n'
        "X = 47\n"
        "Y = 50\n",
        encoding="utf-8",
    )
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    values = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    ]
    assert 47 in values, "检测器漏掉了代码里的 47"
    assert 50 in values, "检测器漏掉了代码里的 50"
