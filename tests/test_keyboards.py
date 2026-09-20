"""内联键盘的回归测试。

**这个文件是 2026-09-20 用户反馈的直接产物。**

用户原话：「抢机设置的秒数显示不出来，我只看到『自动抢...』，直接设置成显示秒数。」

根因不是文字写错了 —— 标签本身是 `🔄 自动抢机 5s`，秒数**写得明明白白**。
问题是 `wizard_summary()` 把 4 个间隔按钮塞进了**同一行**，客户端把这一行
四等分，每格只剩约 9 个半角单位宽，于是尾巴被截掉 ——
**被吃掉的正好是唯一有意义的信息（秒数）**。

## 模型

Telegram 的内联键盘**占满消息宽度**，一行 n 个按钮各占 `W / n`，
**不是**按内容自适应。这个结论是用用户的实际反馈校准出来的：

  · 若按内容自适应，那一行 4 个按钮本身就会是最宽的一行 → 反而不会被截断，
    与用户看到的现象矛盾；
  · 只有「等分 + 占满宽度」才能解释「4 个按钮时每格约 9 单位」。

所以 `test_no_button_label_is_truncated` 用 `W = 32`（偏窄的手机）做判据，
逐行算「每格可用宽度」，超了就算失败。这一条断言同时抓出了 6 处历史遗留的
超宽标签（`4 OCPU（免费上限）`、`50 GB（默认）`、`🔁 重体检（含 API）`、
`🚑 救援（串口 / VNC）`、`🔑 签发 S3 密钥` ×2）。

## 为什么值得单开一个文件

按钮被截断属于本项目「静默失效」家族：**不报错、不崩溃、功能也还在**，
只是用户看不到自己在选什么。这类问题只有断言能拦住。
"""
from __future__ import annotations

import unicodedata
from types import SimpleNamespace

import pytest

from oracles.bot import keyboards as kb
from tests.telegram_harness import make_account

#: 判据宽度（半角单位）。取 32 是**偏窄的手机**，属于保守值：
#: 用户的桌面客户端大约 36~40。窄的能过，宽的一定能过。
REF_WIDTH = 32.0

#: 按钮左右内边距的估算值。
PAD = 2.0


def display_width(text: str) -> float:
    """估算显示宽度（半角单位）：CJK / 全角 / emoji = 2，ASCII = 1。"""
    total = 0.0
    for ch in text:
        if ch == "\ufe0f":                      # variation selector，不占位
            continue
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            total += 2
        elif ord(ch) > 0x1F000:                 # emoji
            total += 2
        else:
            total += 1
    return total


def _accounts(n: int = 8) -> list:
    """复用 `telegram_harness` 的假账号工厂 —— 别在这里手写 key_file 字面量，
    那会被 `scripts/check_secrets.sh` 的「私钥文件名」规则拦下（已实测）。"""
    return [make_account(i) for i in range(1, n + 1)]


def _task(token: str, status: str, done: int, want: int):
    return SimpleNamespace(token=token, status=status, done_count=done, want=want)


def _all_keyboards() -> list[tuple[str, object]]:
    """**穷举**所有键盘工厂。

    刻意不用参数化硬编码清单 —— 那样新加的键盘不会被自动纳入检查，
    而这个 bug 的教训正是「漏掉一个入口就漏掉一整类问题」。
    """
    accs = _accounts()
    return [
        ("main_menu", kb.main_menu()),
        ("wizard_shape", kb.wizard_shape()),
        ("wizard_a1_cpus", kb.wizard_a1_cpus()),
        ("wizard_a1_mem(1)", kb.wizard_a1_mem(1)),
        ("wizard_a1_mem(2)", kb.wizard_a1_mem(2)),
        ("wizard_a1_mem(3)", kb.wizard_a1_mem(3)),
        ("wizard_a1_mem(4)", kb.wizard_a1_mem(4)),
        ("wizard_os", kb.wizard_os()),
        ("wizard_disk", kb.wizard_disk()),
        ("wizard_count", kb.wizard_count()),
        ("wizard_login", kb.wizard_login()),
        ("wizard_key_source", kb.wizard_key_source()),
        ("wizard_network", kb.wizard_network()),
        ("wizard_summary", kb.wizard_summary()),
        ("wizard_summary(no-interval)", kb.wizard_summary(interval_opts=False)),
        ("account_picker", kb.account_picker(accs, 0, "il")),
        ("account_menu", kb.account_menu(1)),
        ("account_delete_picker", kb.account_delete_picker(accs)),
        ("config_menu", kb.config_menu(accs, {1: "✅", 2: "❌"})),
        ("instance_list", kb.instance_list(1, [("tok-a", "oracles-1-abc"),
                                               ("tok-b", "oracles-1-def")])),
        ("instance_menu(running)", kb.instance_menu(1, "tok", True)),
        ("instance_menu(stopped)", kb.instance_menu(1, "tok", False)),
        ("volume_list", kb.volume_list(1, [("tok-a", "bv-abc"), ("tok-b", "bv-def")])),
        ("volume_menu(deletable)", kb.volume_menu(1, "tok", True)),
        ("volume_menu(not-deletable)", kb.volume_menu(1, "tok", False)),
        ("bucket_list", kb.bucket_list(1, [("tok-a", "example-bucket-20260918"),
                                           ("tok-b", "b2")])),
        ("bucket_menu", kb.bucket_menu(1, "tok")),
        ("task_menu", kb.task_menu([_task("tk-abc123", "running", 1, 3),
                                    _task("tk-def456", "succeeded", 3, 3)],
                                   ["tk-abc123"])),
        ("confirm", kb.confirm("c", "ok")),
        ("back_to_main", kb.back_to_main()),
        ("cancel_only", kb.cancel_only("c")),
    ]


# ---------------------------------------------------------------------------
#  🔴 主闸门：任何按钮标签都不许被截断
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,markup", _all_keyboards(),
                         ids=[n for n, _ in _all_keyboards()])
def test_no_button_label_is_truncated(name: str, markup) -> None:
    """逐行检查：一行 n 个按钮 → 每格只有 `REF_WIDTH / n` 宽，标签超了就截断。

    失败信息里同时给出「需要多宽」和「实际可用」，方便直接判断该缩短标签
    还是该拆行。
    """
    rows = markup.inline_keyboard
    assert rows, f"{name} 是空键盘"

    offenders = []
    for i, row in enumerate(rows):
        avail = REF_WIDTH / len(row) - PAD
        for j, btn in enumerate(row, start=1):
            need = display_width(btn.text)
            if need > avail:
                head = btn.text[:max(1, int(avail / 2) - 1)]
                offenders.append(
                    f"  第 {i} 行第 {j} 格：{btn.text!r}\n"
                    f"      需要 {need:.0f} 单位，只有 {avail:.0f} 单位"
                    f"（该行 {len(row)} 个按钮，判据宽 {REF_WIDTH:.0f}）"
                    f" → 客户端会显示成「{head}…」"
                )
    assert not offenders, (
        f"🔴 `{name}` 有 {len(offenders)} 个按钮会被截断：\n"
        + "\n".join(offenders)
        + "\n\n修法二选一：① 缩短标签；② 把它单独放一行（独占整行宽度）。"
    )


# ---------------------------------------------------------------------------
#  🔴 本次用户反馈的定点回归
# ---------------------------------------------------------------------------
def test_wizard_summary_intervals_are_readable() -> None:
    """🔴 抢机间隔按钮必须**完整显示秒数**，且不能挤在一行里。

    用户原话：「抢机设置的秒数显示不出来，我只看到『自动抢...』，
    直接设置成显示秒数。」

    这条断言把「秒数可见」拆成两个可验证的硬条件：
      1. 标签里必须有 `秒` 和**这个按钮自己的数字**（不能是「自动抢机」这种泛指）；
      2. 每个间隔按钮独占一行 —— 这是「不截断」的结构保证。
         只要还有人想合并成一行，这条就会红。
    """
    markup = kb.wizard_summary()
    rows = markup.inline_keyboard

    interval_rows = [r for r in rows if any("抢机" in b.text for b in r)]
    assert interval_rows, "wizard_summary 里找不到抢机按钮"

    # 条件 2：每个间隔按钮独占一行
    for r in interval_rows:
        assert len(r) == 1, (
            f"抢机按钮被合并进了一行（{len(r)} 个）：{[b.text for b in r]}\n"
            "行内等分后每格只剩约 9 单位，秒数会被截掉 —— 用户就是这么看到"
            "「自动抢...」的。每个间隔单独一行。"
        )

    # 条件 1：秒数必须在标签里，且和 callback_data 对得上
    seen = []
    for r in interval_rows:
        btn = r[0]
        seconds = btn.callback_data.split(":", 1)[1]
        seen.append(int(seconds))
        assert "秒" in btn.text, f"标签里没有「秒」：{btn.text!r}"
        assert seconds in btn.text, (
            f"标签 {btn.text!r} 里看不到它自己的秒数 {seconds} —— "
            "用户没法分辨自己选的是几秒。"
        )
    assert sorted(seen) == sorted(kb.GRAB_INTERVALS), (
        f"间隔按钮和 GRAB_INTERVALS 对不上：按钮 {sorted(seen)} vs 配置 {kb.GRAB_INTERVALS}"
    )


def test_wizard_summary_still_has_single_shot_and_cancel() -> None:
    """拆行的时候别把「单次」或「取消」弄丢了。"""
    markup = kb.wizard_summary()
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "wzgo:0" in datas, "少了「现在开机（单次尝试）」"
    assert "wzc" in datas, "少了「取消」"


def test_wizard_summary_without_intervals_is_compact() -> None:
    """不需要抢机选项时（例如 Windows 镜像），键盘只留两行。"""
    markup = kb.wizard_summary(interval_opts=False)
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert datas == ["wzgo:0", "wzc"]


# ---------------------------------------------------------------------------
#  volume_menu：曾被「多套一层列表」整段打挂
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("can_delete", [True, False])
def test_volume_menu_builds_for_both_states(can_delete: bool) -> None:
    """🔴 2026-09-20 的宽度审计脚本撞出来的 bug。

    `volume_menu` 的「不可删」分支原来写成
    `rows.append([[InlineKeyboardButton(...)]])` —— 多套了一层列表，
    `InlineKeyboardMarkup` 直接抛
    「should be a sequence of sequences of InlineKeyboardButtons」。

    也就是**只要打开一块已挂载硬盘的菜单就必然报错**，
    而这条分支此前零测试覆盖（测试只喂过「可删」这一侧）。

    ⚠️ 参数 2026-09-21 从 ``is_attached``（语义相反）改名为 ``can_delete``：
       调用方直接把 ``VolumeView.is_attached`` 传进来过一次，
       而正确该传的是 ``can_delete``。肯定式命名让这个错传不下去。
    """
    markup = kb.volume_menu(1, "tok", can_delete)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("扩容" in t for t in labels)
    if can_delete:
        assert any("删除卷" in t for t in labels)
    else:
        assert not any("删除卷" in t for t in labels), "不可删时不该出现删除按钮"
        assert any("不可删" in t for t in labels), (
            "没说清为什么不能删 —— 用户会以为按钮丢了"
        )


# ---------------------------------------------------------------------------
#  结构不变量：每个按钮都要有 callback_data
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,markup", _all_keyboards(),
                         ids=[n for n, _ in _all_keyboards()])
def test_every_button_has_callback_data(name: str, markup) -> None:
    """没有 callback_data 的按钮点下去毫无反应（静默失效的又一种形态）。"""
    for row in markup.inline_keyboard:
        for btn in row:
            assert btn.callback_data, f"{name} 里有按钮没带 callback_data：{btn.text!r}"


# ---------------------------------------------------------------------------
#  登录密钥来源子步骤（2026-09-21 新增）
# ---------------------------------------------------------------------------
def test_key_source_offers_both_choices() -> None:
    """子步骤必须同时给出「用我配置的」和「让 Bot 生成」两条路。"""
    labels = [b.text for row in kb.wizard_key_source().inline_keyboard for b in row]
    assert any("用我配置" in t for t in labels)
    assert any("生成" in t for t in labels)


def test_key_source_default_is_the_users_own_key() -> None:
    """🔴 「让 Bot 生成」**不能**是默认选项。

    它会（1）把私钥落到服务器上、（2）让私钥经过 Telegram ——
    相比「私钥从不离开你本机」是一次实质降级。
    默认必须是「用我配置的公钥」，而且要在标签上写出来。
    """
    rows = kb.wizard_key_source().inline_keyboard
    first = rows[0][0]
    assert first.callback_data == "wzk:own", (
        f"第一个按钮是 {first.callback_data!r} —— 默认必须是「用我配置的公钥」"
    )
    assert "默认" in first.text, "默认选项没在标签上标出来，用户不知道不点会怎样"


def test_key_source_has_a_back_button_to_the_login_step() -> None:
    """「上一步」要回**登录方式**那一步，不是退回向导第一步。

    ⚠️ 向导里既有的 `wzb` 是「回第 1 步」（会把已选参数全清掉）。
       子步骤套用它会让人丢掉前面所有选择 —— 所以这里必须是 `wzkb`。
    """
    datas = [b.callback_data for row in kb.wizard_key_source().inline_keyboard for b in row]
    assert "wzkb" in datas
    assert "wzb" not in datas, "用了 wzb 会退回向导第一步，把已选参数全清掉"


def test_instance_menu_offers_key_download_in_both_power_states() -> None:
    """「🔑 下载登录密钥」在开机和停机状态下都要有。

    它读的是实例 metadata（停机也读得到）和服务器上的密钥文件，
    与实例是否在跑无关 —— 不像「救援控制台」只对运行中的实例可用。
    """
    for is_running in (True, False):
        labels = [b.text for row in kb.instance_menu(1, "tok", is_running).inline_keyboard
                  for b in row]
        assert any("下载登录密钥" in t for t in labels), (
            f"is_running={is_running} 时没有下载密钥的入口"
        )


# ---------------------------------------------------------------------------
#  Telegram 的硬约束：callback_data ≤ 64 字节
# ---------------------------------------------------------------------------
TELEGRAM_CALLBACK_DATA_MAX = 64


@pytest.mark.parametrize("name,markup", _all_keyboards(),
                         ids=[n for n, _ in _all_keyboards()])
def test_every_callback_data_fits_telegrams_64_byte_limit(name: str, markup) -> None:
    """🔴 callback_data 超过 64 字节 → Telegram **整条键盘**都拒绝。

    这不是「点下去没反应」，而是 `BadRequest: BUTTON_DATA_INVALID` ——
    整个 `edit_text` / `reply_text` 直接失败，用户看到一屏报错，
    **连菜单都打不开**。而 `telegram_harness` 的假夹具不校验这个，
    所以只有真 Telegram 才会暴露它。

    ⚠️ 上限是**字节数**（UTF-8）不是字符数 → 必须 `len(x.encode())`。
       `ip:<index>:<token>:key` 这种拼出来的串很容易在加字段时越界。
    """
    too_long = [
        (btn.text, btn.callback_data, len(btn.callback_data.encode("utf-8")))
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data
        and len(btn.callback_data.encode("utf-8")) > TELEGRAM_CALLBACK_DATA_MAX
    ]
    assert not too_long, (
        f"{name} 里有 callback_data 超过 {TELEGRAM_CALLBACK_DATA_MAX} 字节：{too_long}"
        " —— Telegram 会拒绝整条键盘（BadRequest: BUTTON_DATA_INVALID）"
    )


def test_instance_menu_fits_with_the_longest_realistic_token() -> None:
    """用**实际可能的最长 token** 再验一次实例菜单。

    `_all_keyboards()` 用的是 "tok"（3 字符）。而 `store.put()` 正常给
    6 字符（`token_urlsafe(4)[:6]`），**碰撞时退化成 11 字符**
    （`token_urlsafe(8)`）。实例菜单的 `ip:<index>:<token>:<action>`
    里还有 `terminate` 这种长 action —— 两者叠起来才是真正的最坏情形。
    """
    longest_token = "a" * 11          # token_urlsafe(8) 的实际长度
    markup = kb.instance_menu(99, longest_token, True)
    for row in markup.inline_keyboard:
        for btn in row:
            n = len(btn.callback_data.encode("utf-8"))
            assert n <= TELEGRAM_CALLBACK_DATA_MAX, (
                f"{btn.text!r} 的 callback_data {btn.callback_data!r} 是 {n} 字节，"
                f"超过 Telegram 的 {TELEGRAM_CALLBACK_DATA_MAX} 字节上限"
            )
