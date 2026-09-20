"""卷安全链：孤儿扫描 / 删卷复核 / UI 接线。

**为什么单独一个文件**：2026-09-21 做了一次「哪些函数从没被调用过」的分析，
扫出这条链**整段零覆盖** —— ``scan_orphans``、``verify_orphans_before_delete``、
``delete_volume_safe``、``VolumeView.is_attached`` 没有任何测试碰过。
而它恰好是**唯一会删数据**的那条路（引导卷删掉就是整台机器没了）。

挖出来的三个洞都不是「少写了个校验」，而是**校验在结构上给不出否**：

  1. ``VolumeView.is_attached`` 拿**卷自己**的 ``lifecycle_state`` 和
     ``"ATTACHED"`` 比 —— 而那个字段的合法值里**根本没有** ``ATTACHED``
     （它是 ``VolumeAttachment`` 的状态）→ 恒为 False
     → 已挂载的卷在列表里显示「未挂载」，菜单还给出「🗑 删除卷」。
  2. 挂载关系查询失败时，三处都当成「没挂」→ 复核静默放行。
     同一个函数里「列可用域失败」却是 fail-closed —— **两种失败语义并存**，
     所以只看代码很难觉得有问题。
  3. ``ATTACHING``（正在挂载）不算占用 → 开机过程中的引导卷被判成孤儿/可删。
"""
from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from typing import Any

import pytest

from oracles import models
from oracles.bot import dispatch, handlers
from oracles.errors import OciApiError
from oracles.services import audit as audit_svc
from oracles.services import compute as compute_svc
from tests.telegram_harness import (
    FakeAttachment,
    Harness,
    make_settings,
)

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "oracles"

VOL_ID = "ocid1.bootvolume.oc1..EXAMPLEBOOTVOLUME"
INSTANCE_ID = "ocid1.instance.oc1..EXAMPLEINSTANCE"
CHAT = 1


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
#  1. VolumeView：那个「结构上不可能成立」的条件
# ---------------------------------------------------------------------------
def test_the_volume_state_can_never_be_attached() -> None:
    """🔴 钉住让 bug 成立的那个前提：卷自己的状态里**没有** ``ATTACHED``。

    这条不测我们的代码，测的是**SDK 的事实**。因为 ``is_attached`` 曾经写成

        bool(attached_instance_id) and lifecycle_state == "ATTACHED"

    而 ``lifecycle_state`` 取的是**卷自己**的状态（``list_boot_volumes`` 返回的
    ``BootVolume``），它的合法值是 PROVISIONING/RESTORING/AVAILABLE/
    TERMINATING/TERMINATED/FAULTY —— 里面没有 ``ATTACHED``。
    于是那个 AND 恒假，``is_attached`` 恒为 False。

    ⚠️ 这条断言的是**两个字段分属不同对象**这个事实。哪天 OCI 真给
       ``Volume`` 加了 ``ATTACHED`` 状态，这里会红 —— 那时回来重新审视
       判定是**对的**（而不是直接把这条删掉）。
    """
    from oci.core.models import BootVolume, Volume, VolumeAttachment

    def allowed(cls) -> set[str]:
        prop = cls.__dict__.get("lifecycle_state")
        doc = getattr(prop, "__doc__", "") or ""
        m = re.search(r"Allowed values for this property are:(.*?)\.", doc, re.S)
        assert m, f"{cls.__name__}.lifecycle_state 的文档里找不到合法值列表"
        return set(re.findall(r'"([A-Z_]+)"', m.group(1)))

    volume_states, attach_states = allowed(Volume), allowed(VolumeAttachment)
    assert "ATTACHED" not in allowed(BootVolume)
    assert "ATTACHED" not in volume_states, (
        f"Volume.lifecycle_state 现在可以是 ATTACHED 了（{sorted(volume_states)}）—— "
        f"「卷自己的状态里没有 ATTACHED」这个前提变了，回来重看 VolumeView.is_attached"
    )
    assert "ATTACHED" in attach_states, (
        "挂载状态居然不在 VolumeAttachment 上？整套判定都要重看"
    )
    assert "ATTACHING" in attach_states and "DETACHING" in attach_states


def test_an_attached_volume_is_recognised_as_attached() -> None:
    """🔴 回归：真实形态的「已挂载」卷必须被认成已挂载。

    形态就是 ``list_all_volumes`` 造出来的样子：卷自己 ``AVAILABLE``，
    挂载关系解析到 ``attached_instance_id``。
    """
    v = models.VolumeView(
        id=VOL_ID, display_name="bv-1", size_gb=50, ad="AD-1", kind="boot",
        lifecycle_state="AVAILABLE", attached_instance_id=INSTANCE_ID)
    assert v.is_attached is True
    assert v.can_delete is False, "已挂载的卷给了删除入口"
    assert "已挂载" in v.attach_state_label
    assert v.attach_icon == "🟢"


@pytest.mark.parametrize("state", ["ATTACHING", "ATTACHED", "DETACHING"])
def test_every_live_attachment_state_counts_as_occupied(state: str) -> None:
    """``ATTACHING`` / ``DETACHING`` 也算被占着 —— 不能只认 ``ATTACHED``。

    ``ATTACHING`` = 正在往实例上挂（那台机器多半正在启动）；
    ``DETACHING`` = 还没真正脱离（卸载失败会退回 ATTACHED）。
    """
    v = models.VolumeView(
        id=VOL_ID, display_name="bv-1", size_gb=50, ad="AD-1",
        lifecycle_state="AVAILABLE", attached_instance_id=INSTANCE_ID)
    assert state in compute_svc.LIVE_ATTACHMENT_STATES
    assert v.can_delete is False


def test_an_unverified_volume_is_not_deletable() -> None:
    """🔴 挂载状态**没查到**时不许给删除入口。

    判据是「确认它没挂」，不是「没查到它挂着」。这两者混淆，
    一次查询抖动就能删掉运行中实例的系统盘。
    """
    v = models.VolumeView(
        id=VOL_ID, display_name="bv-1", size_gb=50, ad="AD-1",
        lifecycle_state="AVAILABLE", attachment_verified=False)
    assert v.is_attached is False, "「没查到」不等于「挂着」—— 两者要分开"
    assert v.can_delete is False, "状态未知却给了删除入口"
    assert v.attach_icon == "❓"
    assert "未查到" in v.attach_state_label


def test_a_confirmed_free_volume_is_deletable() -> None:
    """对照组：确认没挂的卷**必须**能删 —— 否则这个功能就废了。

    ⚠️ 缺了这条，上面几条在「``can_delete`` 恒为 False」的实现下也全过。
    """
    v = models.VolumeView(id=VOL_ID, display_name="bv-1", size_gb=50, ad="AD-1",
                          lifecycle_state="AVAILABLE")
    assert v.is_attached is False
    assert v.can_delete is True
    assert v.attach_icon == "⚪"


# ---------------------------------------------------------------------------
#  2. attached_index：区分「没挂」与「没查成」
# ---------------------------------------------------------------------------
def _client(**kw: Any) -> Any:
    """造一个假客户端（复用 handler 夹具，保证与 UI 那条路同源）。"""
    h = Harness(make_settings())
    c = h.registry.get(1)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_attached_index_reports_unverified_ads() -> None:
    """某个 AD 查不到 → 记进 ``unverified_ads``，而不是当它没挂。"""
    c = _client(availability_domain_names=["AD-1", "AD-2"],
                failing_attachment_ads={"AD-2"},
                attachments=[FakeAttachment(VOL_ID, INSTANCE_ID, "ATTACHED")])
    idx = compute_svc.attached_index(c)
    assert idx.boot.get(VOL_ID) == INSTANCE_ID
    assert idx.unverified_ads == ["AD-2"]
    assert idx.complete is False


def test_attached_index_is_complete_when_every_ad_answers() -> None:
    """对照组：全都查到了 → ``complete`` 为 True。"""
    c = _client(availability_domain_names=["AD-1", "AD-2"])
    idx = compute_svc.attached_index(c)
    assert idx.unverified_ads == []
    assert idx.complete is True


# ---------------------------------------------------------------------------
#  3. delete_volume_safe：最后一道闸必须 fail-closed
# ---------------------------------------------------------------------------
def test_delete_refuses_when_the_volume_is_attached() -> None:
    c = _client(attachments=[FakeAttachment(VOL_ID, INSTANCE_ID, "ATTACHED")])
    with pytest.raises(RuntimeError, match="已挂载到实例"):
        compute_svc.delete_volume_safe(c, VOL_ID)
    assert c.deleted_volumes == [], "拒绝了却还是下发了删除"


def test_delete_refuses_while_the_volume_is_still_attaching() -> None:
    """🔴 开机过程中（ATTACHING）也不能删。"""
    c = _client(attachments=[FakeAttachment(VOL_ID, INSTANCE_ID, "ATTACHING")])
    with pytest.raises(RuntimeError, match="已挂载到实例"):
        compute_svc.delete_volume_safe(c, VOL_ID)
    assert c.deleted_volumes == []


def test_delete_refuses_when_the_attachment_query_fails() -> None:
    """🔴 核心回归：**查询失败 → 拒绝**，而不是「查不到就删」。

    原来 ``_safe_call_all`` 吞掉异常返回 ``[]``，于是复核看到「没挂」→ 放行。
    整个函数的 docstring 写着「已挂载的直接拒绝 —— 删运行中实例的系统盘是灾难」，
    而它的失败模式**正好就是**那个灾难场景。
    """
    c = _client(failing_attachment_ads={"AD-1"})
    with pytest.raises(RuntimeError, match="无法确认挂载状态"):
        compute_svc.delete_volume_safe(c, VOL_ID)
    assert c.deleted_volumes == [], "复核失败却还是删了"


def test_delete_refuses_when_the_ad_list_fails() -> None:
    c = _client(ad_list_error=OciApiError("模拟列可用域失败"))
    with pytest.raises(RuntimeError, match="无法确认挂载状态"):
        compute_svc.delete_volume_safe(c, VOL_ID)
    assert c.deleted_volumes == []


def test_delete_proceeds_only_when_confirmed_free() -> None:
    """对照组：确认没挂 → 真的删掉。少了这条，上面全在「永远拒绝」下也过。"""
    c = _client()
    compute_svc.delete_volume_safe(c, VOL_ID)
    assert c.deleted_volumes == [VOL_ID]


# ---------------------------------------------------------------------------
#  4. scan_orphans：不许产出假孤儿
# ---------------------------------------------------------------------------
def _volume(vid: str, name: str, gb: float = 50) -> Any:
    from types import SimpleNamespace
    return SimpleNamespace(id=vid, display_name=name, size_in_gbs=gb,
                           lifecycle_state="AVAILABLE", time_created=None)


def test_scan_does_not_report_an_attached_volume_as_orphan() -> None:
    c = _client(boot_volumes=[_volume(VOL_ID, "bv-1")],
                attachments=[FakeAttachment(VOL_ID, INSTANCE_ID, "ATTACHED")])
    report = audit_svc.scan_orphans(c)
    assert report.orphans == []
    assert report.attached_count == 1


def test_scan_does_not_report_false_orphans_when_the_query_fails() -> None:
    """🔴 查询失败的 AD 里，卷**不能**被判成孤儿。

    原来这里 `except: log.info(...)` 就跳过 —— 那个 AD 的挂载集合是空的，
    于是域里**每一块**卷都被算成孤儿，出现在清理列表里。
    """
    c = _client(boot_volumes=[_volume(VOL_ID, "bv-1")],
                failing_attachment_ads={"AD-1"})
    report = audit_svc.scan_orphans(c)
    assert report.orphans == [], "挂载关系没查到却报出了孤儿"
    assert report.unverified_ads == ["AD-1"]
    assert report.unverified_count == 1, "没记下「有几块卷没参与判定」"
    assert report.partial is True


def test_scan_still_finds_real_orphans() -> None:
    """对照组：真孤儿要能被扫出来（否则上面几条在「永远返回空」下也过）。"""
    other = "ocid1.bootvolume.oc1..EXAMPLEORPHAN"
    c = _client(boot_volumes=[_volume(VOL_ID, "attached"), _volume(other, "orphan")],
                attachments=[FakeAttachment(VOL_ID, INSTANCE_ID, "ATTACHED")])
    report = audit_svc.scan_orphans(c)
    assert [o.id for o in report.orphans] == [other]
    assert report.attached_count == 1


def test_scan_reports_a_total_failure_as_error() -> None:
    """列可用域都失败 → 这是**整账号**失败，用 ``error`` 而不是 ``unverified``。"""
    c = _client(ad_list_error=OciApiError("模拟 500"))
    report = audit_svc.scan_orphans(c)
    assert report.error and "列可用域失败" in report.error
    assert report.orphans == []


# ---------------------------------------------------------------------------
#  5. render_orphans：报告不许声称「扫干净了」
# ---------------------------------------------------------------------------
def test_render_says_nothing_found_only_when_the_scan_was_complete() -> None:
    """🔴 有 AD 没查成时，**不许**打印「✅ 没有孤儿卷」。

    这句是「扫描完成、结论是否」的断言。扫描没做完就下这个结论，
    用户会以为额度没问题 —— 而那个域根本没被检查。
    """
    ok = audit_svc.OrphanReport(index=1, label="[1] a", region="r")
    assert "✅ 没有孤儿卷" in audit_svc.render_orphans([ok])

    partial = audit_svc.OrphanReport(index=1, label="[1] a", region="r",
                                     unverified_ads=["AD-2"], unverified_count=5)
    text = audit_svc.render_orphans([partial])
    assert "✅ 没有孤儿卷" not in text, f"扫描不完整却声称没问题：\n{text}"
    assert "没查到" in text
    assert "5 块卷" in text or "5 块" in text, "没说清有多少块卷没参与判定"


def test_render_shows_how_many_volumes_are_attached() -> None:
    """``attached_count`` 原来算了从不渲染 —— 它是判断「列表合理吗」的对照量。"""
    r = audit_svc.OrphanReport(index=1, label="[1] a", region="r",
                               orphans=[audit_svc.OrphanVolume(
                                   id="ocid1.bootvolume.oc1..EXAMPLE", display_name="o",
                                   size_gb=50, ad="AD-1")],
                               attached_count=7)
    text = audit_svc.render_orphans([r])
    assert "7 块正挂在实例上" in text


# ---------------------------------------------------------------------------
#  6. verify_orphans_before_delete：三类必须分得开
# ---------------------------------------------------------------------------
def _orphan(vid: str, ad: str = "AD-1") -> Any:
    return audit_svc.OrphanVolume(id=vid, display_name=vid[-8:], size_gb=50, ad=ad)


def test_verify_separates_attached_from_unverified() -> None:
    """🔴 「确认已挂载」和「没查到」是两种原因，必须分开报。

    合成一句「已挂载的已剔除」会让用户以为查过了 —— 实际是没查成。
    """
    free = "ocid1.bootvolume.oc1..EXAMPLEFREE"
    c = _client(attachments=[FakeAttachment(VOL_ID, INSTANCE_ID, "ATTACHED")])
    v = audit_svc.verify_orphans_before_delete(
        c, [_orphan(VOL_ID), _orphan(free)])
    assert [x.id for x in v.safe] == [free]
    assert [x.id for x in v.attached] == [VOL_ID]
    assert v.unverified == []


def test_verify_marks_unverified_instead_of_safe() -> None:
    """🔴 复核查不到 → 进 ``unverified``（不删），**不是** ``safe``。

    原来这里是 `except: continue` —— 查询失败等于放行。
    """
    c = _client(failing_attachment_ads={"AD-1"})
    v = audit_svc.verify_orphans_before_delete(c, [_orphan(VOL_ID)])
    assert v.safe == [], "复核没做成却判为可删"
    assert [x.id for x in v.unverified] == [VOL_ID]


def test_verify_marks_everything_unverified_when_ads_cannot_be_listed() -> None:
    c = _client(ad_list_error=OciApiError("模拟 500"))
    v = audit_svc.verify_orphans_before_delete(c, [_orphan(VOL_ID), _orphan("x")])
    assert v.safe == []
    assert len(v.unverified) == 2


# ---------------------------------------------------------------------------
#  7. handler 层接线：UI 真的用了新判定吗
# ---------------------------------------------------------------------------
def _volume_harness(**kw: Any) -> Harness:
    h = Harness(make_settings())
    c = h.registry.get(1)
    for k, v in kw.items():
        setattr(c, k, v)
    return h


def _markup(msg) -> Any:
    return msg.sent[-1].kwargs.get("reply_markup")


def _first_button_with(markup, prefix: str) -> str:
    for row in markup.inline_keyboard:
        for b in row:
            if b.callback_data and b.callback_data.startswith(prefix):
                return b.callback_data
    raise AssertionError(f"键盘里没有以 {prefix!r} 开头的按钮")


def test_volume_list_marks_unverified_volumes_and_warns() -> None:
    """列表里「没查到」的卷要显示 ❓ 并给出解释。"""
    h = _volume_harness(boot_volumes=[_volume(VOL_ID, "bv-1")],
                        failing_attachment_ads={"AD-1"})
    _q, msg = run(h.callback(handlers.on_callback, "vl:1"))
    assert "❓" in msg.all_text
    assert "没查到" in msg.all_text
    assert "不会**给出删除入口" in msg.all_text or "不会" in msg.all_text


def test_the_volume_menu_hides_delete_for_an_attached_volume() -> None:
    """🔴 端到端接线：已挂载的卷，菜单里**不能**出现删除按钮。

    这条走的是真实 handler → 真实 ``list_all_volumes`` → 真实
    ``VolumeView.can_delete`` → 真实 ``volume_menu``。
    之前 ``is_attached`` 恒为 False，所以这里一直是有删除按钮的。
    """
    h = _volume_harness(boot_volumes=[_volume(VOL_ID, "bv-1")],
                        attachments=[FakeAttachment(VOL_ID, INSTANCE_ID, "ATTACHED")])
    _q, listing = run(h.callback(handlers.on_callback, "vl:1"))
    token = _first_button_with(_markup(listing), "vm:1:").rsplit(":", 1)[1]

    _q, detail = run(h.callback(handlers.on_callback, f"vm:1:{token}"))
    labels = [b.text for row in _markup(detail).inline_keyboard for b in row]
    assert not any("删除卷" in t for t in labels), (
        f"已挂载的卷仍然给出了删除按钮：{labels}"
    )
    assert any("不可删" in t for t in labels)


def test_the_volume_menu_hides_delete_when_the_state_is_unverified() -> None:
    """🔴 挂载状态没查到 → 同样不给删除入口，并说明原因。"""
    h = _volume_harness(boot_volumes=[_volume(VOL_ID, "bv-1")],
                        failing_attachment_ads={"AD-1"})
    _q, listing = run(h.callback(handlers.on_callback, "vl:1"))
    token = _first_button_with(_markup(listing), "vm:1:").rsplit(":", 1)[1]

    _q, detail = run(h.callback(handlers.on_callback, f"vm:1:{token}"))
    labels = [b.text for row in _markup(detail).inline_keyboard for b in row]
    assert not any("删除卷" in t for t in labels), labels
    assert "没查到" in detail.all_text
    assert "不提供删除" in detail.all_text


def test_the_volume_menu_offers_delete_for_a_confirmed_free_volume() -> None:
    """对照组：确认没挂的卷**必须**有删除按钮。"""
    h = _volume_harness(boot_volumes=[_volume(VOL_ID, "bv-1")])
    _q, listing = run(h.callback(handlers.on_callback, "vl:1"))
    token = _first_button_with(_markup(listing), "vm:1:").rsplit(":", 1)[1]

    _q, detail = run(h.callback(handlers.on_callback, f"vm:1:{token}"))
    labels = [b.text for row in _markup(detail).inline_keyboard for b in row]
    assert any("删除卷" in t for t in labels), labels


# ---------------------------------------------------------------------------
#  8. 可达性护栏：注册了的 kind 必须有人创建
# ---------------------------------------------------------------------------
#: 在 `_HANDLERS` 里注册、但**当前没有任何地方创建**的 kind。
_UNREACHABLE_BY_DESIGN = {
    "orphan_delete": (
        "Bot 里没有入口 —— 孤儿卷删除被「硬盘管理」的单卷删除取代了。"
        "docs/05 曾把它写成 Bot 的能力，是文档陈旧。"
        "保留实现是为了 CLI / 后续接线能直接用（且它现在 fail-closed）。"
    ),
}


def test_every_pending_action_kind_has_a_creator() -> None:
    """🔴 `_HANDLERS` 里注册的每个 kind，都必须有地方**创建**它。

    一个永远不会被创建的 kind，要么是死代码，要么是一处丢失的接线 ——
    两种都值得知道。2026-09-21 实测：``orphan_delete`` 在路由表里，
    但全仓库**没有任何地方**创建它。这类事实不该靠人去 grep。
    """
    src = "\n".join(p.read_text(encoding="utf-8") for p in sorted(PKG.rglob("*.py")))
    unreachable = [k for k in dispatch._HANDLERS
                   if f'kind="{k}"' not in src and f"kind='{k}'" not in src]

    assert sorted(unreachable) == sorted(_UNREACHABLE_BY_DESIGN), (
        f"可达性对不上了。\n"
        f"  实际不可达：{sorted(unreachable)}\n"
        f"  登记为「设计上不可达」：{sorted(_UNREACHABLE_BY_DESIGN)}\n"
        f"→ 若某条被接上了线：把它从 _UNREACHABLE_BY_DESIGN 里删掉。\n"
        f"→ 若是新出现的死代码：删掉 handler，或加进登记表并写明原因。"
    )


def test_every_registered_handler_is_awaitable_and_takes_the_same_shape() -> None:
    """``_HANDLERS`` 的值必须是 ``(pending, settings, registry)`` 形状的协程函数。

    ``execute_pending`` 直接 ``await handler(pending, settings, registry)`` ——
    签名不对要到**运行时**才炸，而且是在删东西的那一刻。
    """
    for kind, fn in dispatch._HANDLERS.items():
        assert inspect.iscoroutinefunction(fn), f"{kind} 的 handler 不是协程函数"
        params = list(inspect.signature(fn).parameters)
        assert params == ["pending", "settings", "registry"], (
            f"{kind} 的签名是 {params}，execute_pending 按 "
            f"(pending, settings, registry) 调它"
        )


def test_orphan_delete_is_wired_through_the_verdict_object() -> None:
    """孤儿卷删除必须用 ``verify_orphans_before_delete`` 的 verdict。

    ⚠️ 这条防的是「接线时又退回旧签名」—— ``verify_orphans_before_delete``
       2026-09-21 从返回 ``list`` 改成返回 ``DeleteVerdict``（为了区分
       「已挂载」和「没查到」）。旧写法 ``safe = await ...`` 会把一个
       dataclass 当成列表用，`if not safe` 恒为假 → **全部照删**。
    """
    src = inspect.getsource(dispatch._do_orphan_delete)
    assert "verdict" in src, "没用 verdict"
    assert ".safe" in src, "没从 verdict 里取 safe"
    assert "verdict.unverified" in src, (
        "没区分「没查到」和「已挂载」—— 用户会以为复核做过了"
    )
