"""内联键盘构造。

**回调数据编码规则**（受 Telegram 64 字节上限约束，必须紧凑）：

    m                    主菜单
    q                    配额总览（全部账号）
    q:<idx>              配额（单账号）
    ap:<page>            账号选择器第 page 页
    a:<idx>              账号操作菜单
    il:<idx>             实例列表
    im:<idx>:<tok>       实例操作菜单（tok → 实例 OCID）
    ip:<idx>:<tok>:<act> 实例动作计划（act = start/stop/reboot/terminate）
    lp:<idx>             创建实例计划
    bl:<idx>             桶列表
    bm:<idx>:<tok>       桶操作菜单
    bp:<idx>:<tok>       删桶计划
    ob:<idx>:<tok>       列出桶内对象
    sk:<idx>             签发 S3 密钥
    au / au:<idx>        计费残留审计
    se / se:<idx>        安全暴露面审计
    hd:<idx>:<tok>       收紧 22 端口计划
    ok:<ptok> / no:<ptok>  确认 / 取消待执行操作
    x                    空动作（占位按钮）
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..config import Account

#: 账号选择器每页多少个
ACCOUNTS_PER_PAGE = 8


def main_menu() -> InlineKeyboardMarkup:
    """``/start`` 的七大功能入口（用户要求的固定顺序）。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ 1. 配置管理", callback_data="cfg")],
        [InlineKeyboardButton("🚀 2. 开机 / 自动抢机", callback_data="ap:0:wz")],
        [
            InlineKeyboardButton("🖥 3. 实例管理", callback_data="ap:0:il"),
            InlineKeyboardButton("💾 5. 硬盘管理", callback_data="ap:0:vl"),
        ],
        [
            InlineKeyboardButton("📊 4. 配额查询", callback_data="q"),
            InlineKeyboardButton("🧵 6. 任务管理", callback_data="tk"),
        ],
        [InlineKeyboardButton("🪣 7. 存储桶管理", callback_data="ap:0:bl")],
        [
            InlineKeyboardButton("ℹ️ 状态", callback_data="status"),
            InlineKeyboardButton("❓ 帮助", callback_data="hlp"),
        ],
    ])


#: 开机向导里可选的操作系统（OCI list_images 的 operating_system 过滤值）
WIZARD_OS_CHOICES: list[tuple[str, str]] = [
    ("Ubuntu", "Canonical Ubuntu"),
    ("Oracle Linux", "Oracle Linux"),
    ("CentOS Stream", "CentOS Stream"),
    ("Debian", "Debian"),
    ("Windows Server", "Microsoft Windows Server"),
]

#: 抢机间隔选项（0 = 不开自动循环）
GRAB_INTERVALS: list[int] = [5, 10, 20, 30]


def wizard_shape() -> InlineKeyboardMarkup:
    """向导第 1 步：CPU / 规格类型。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("AMD E2.1.Micro（免费档）", callback_data="wzs:E2")],
        [InlineKeyboardButton("ARM A1.Flex（自定义核/内存）", callback_data="wzs:A1")],
        [InlineKeyboardButton("❌ 取消向导", callback_data="wzc")],
    ])


def wizard_a1_cpus() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1 OCPU", callback_data="wza:1"),
            InlineKeyboardButton("2 OCPU", callback_data="wza:2"),
        ],
        [
            InlineKeyboardButton("3 OCPU", callback_data="wza:3"),
            # ⚠️ 标签保持等长，别在这一格加「（免费上限）」——
            #    加了就是 18 单位，而 2 列时每格只有 ~14 单位 → 被截断。
            #    「免费上限 4 核」这句话在上一句提示里已经说了。
            InlineKeyboardButton("4 OCPU", callback_data="wza:4"),
        ],
        [InlineKeyboardButton("◀️ 上一步", callback_data="wzb")],
    ])


def _mem_options(n_ocpus: int) -> list[int]:
    """A1.Flex 每核 0.5~8 GB：给几个合理的预设档位。"""
    opts = {max(2, n_ocpus), max(4, n_ocpus * 2), min(32, n_ocpus * 4)}
    return sorted(o for o in opts if 1 <= o <= 32)


def wizard_a1_mem(n_ocpus: int) -> InlineKeyboardMarkup:
    """向导第 2 步：A1.Flex 内存（按已选核数给出合法档位）。"""
    rows = []
    opts = _mem_options(n_ocpus)
    for i in range(0, len(opts), 3):
        row = [InlineKeyboardButton(f"{o} GB", callback_data=f"wzm:{o}") for o in opts[i:i + 3]]
        rows.append(row)
    rows.append([InlineKeyboardButton("◀️ 上一步", callback_data="wzb")])
    return InlineKeyboardMarkup(rows)


def wizard_os() -> InlineKeyboardMarkup:
    """向导第 3 步：操作系统。"""
    rows = []
    for i in range(0, len(WIZARD_OS_CHOICES), 2):
        row = [InlineKeyboardButton(name, callback_data=f"wzo:{i // 2}") for name, _ in WIZARD_OS_CHOICES[i:i + 2]]
        rows.append(row)
    rows.append([InlineKeyboardButton("◀️ 上一步", callback_data="wzb")])
    return InlineKeyboardMarkup(rows)


def wizard_disk() -> InlineKeyboardMarkup:
    """向导第 4 步：引导卷大小。"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("50 GB", callback_data="wzv:50"),
            InlineKeyboardButton("100 GB", callback_data="wzv:100"),
            InlineKeyboardButton("200 GB", callback_data="wzv:200"),
        ],
        [InlineKeyboardButton("◀️ 上一步", callback_data="wzb")],
    ])


def wizard_count() -> InlineKeyboardMarkup:
    """向导第 5 步：数量。"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1 台", callback_data="wzn:1"),
            InlineKeyboardButton("2 台", callback_data="wzn:2"),
            InlineKeyboardButton("3 台", callback_data="wzn:3"),
            InlineKeyboardButton("4 台", callback_data="wzn:4"),
        ],
        [InlineKeyboardButton("◀️ 上一步", callback_data="wzb")],
    ])


def wizard_login() -> InlineKeyboardMarkup:
    """向导第 6 步：登录方式（用户名/密码 or SSH key）。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 仅 SSH 公钥（默认）", callback_data="wzl:key")],
        [InlineKeyboardButton("👤 设置用户名 + 密码", callback_data="wzl:pwd")],
        [InlineKeyboardButton("◀️ 上一步", callback_data="wzb")],
    ])


def wizard_key_source() -> InlineKeyboardMarkup:
    """向导第 6 步的**子步骤**：公钥从哪来。

    ⚠️ 默认仍然是「用我配置的公钥」。

    「让 Bot 生成一对新的」会把**私钥落到服务器上**（``<ORACLES_HOME>/keys/``）
    并且经过 Telegram —— 相比「私钥从不离开你本机」是一次实质降级。
    所以它必须是**用户主动点**的选项，绝不能做成默认或者顺带发生的事。

    OCI 侧没有别的办法：``ComputeClient`` 里没有任何密钥生成方法，
    控制台的「Generate a key pair for me」是浏览器本地生成的。
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 用我配置的公钥（默认）", callback_data="wzk:own")],
        [InlineKeyboardButton("🆕 让 Bot 生成一对新的", callback_data="wzk:gen")],
        [InlineKeyboardButton("◀️ 上一步", callback_data="wzkb")],
    ])


def wizard_network() -> InlineKeyboardMarkup:
    """向导第 7 步：网络（IPv4 公网恒开，IPv6 可选）。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 IPv6 + IPv4", callback_data="wzv6:on")],
        [InlineKeyboardButton("仅 IPv4", callback_data="wzv6:off")],
        [InlineKeyboardButton("◀️ 上一步", callback_data="wzb")],
    ])


def wizard_summary(interval_opts: bool = True) -> InlineKeyboardMarkup:
    """向导最后：确认 + 执行方式（单次 or 自动抢机间隔）。

    🔴 **一个抢机间隔一行**，不要合并成多列。

    2026-09-20 的用户反馈：这里原来把 4 个间隔塞进**同一行**，结果
    客户端把这一行按 4 等分，每个按钮只剩约 1/4 宽，标签被截断成
    「🔄 自动抢...」—— **秒数正好是被吃掉的那一段**，用户根本看不出
    选的是 5 秒还是 30 秒。

    内联键盘的行宽不是「按内容自适应」：一行 n 个按钮就各占 1/n，
    行宽由**最宽的那一行**决定（这里是「✅ 现在开机（单次尝试）」）。
    所以标签长一点、列数多一点，最先牺牲的就是尾巴上的信息。
    秒数是这个按钮**唯一有意义的信息**，必须放在不会被截断的位置。

    配套回归测试：`tests/test_bot_handlers.py` 的
    `test_wizard_summary_intervals_are_readable`（会拦住「合并成一行」的改动）。
    """
    rows = [[InlineKeyboardButton("✅ 现在开机（单次尝试）", callback_data="wzgo:0")]]
    if interval_opts:
        for s in GRAB_INTERVALS:
            rows.append([
                InlineKeyboardButton(f"🔄 自动抢机 {s} 秒", callback_data=f"wzgo:{s}")
            ])
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="wzc")])
    return InlineKeyboardMarkup(rows)


def config_menu(accounts: list, statuses: dict[int, str]) -> InlineKeyboardMarkup:
    """配置管理：账号体检列表 + 增删。"""
    rows = []
    for acc in accounts:
        icon = statuses.get(acc.index, "·")
        label = f"{icon} {acc.index}. {acc.alias or acc.region}"
        if len(label) > 32:
            label = label[:31] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=f"cfgi:{acc.index}")])
    rows.append([InlineKeyboardButton("➕ 添加账号", callback_data="cfgadd")])
    rows.append([InlineKeyboardButton("🗑 删除账号…", callback_data="cfgdel")])
    rows.append([
        InlineKeyboardButton("🔁 重新体检", callback_data="cfgtest"),
        InlineKeyboardButton("◀️ 主菜单", callback_data="m"),
    ])
    return InlineKeyboardMarkup(rows)


def account_delete_picker(accounts: list) -> InlineKeyboardMarkup:
    rows = []
    for acc in accounts:
        label = f"删除 {acc.index}. {acc.alias or acc.region}"
        if len(label) > 32:
            label = label[:31] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=f"cfd:{acc.index}")])
    rows.append([InlineKeyboardButton("◀️ 返回", callback_data="cfg")])
    return InlineKeyboardMarkup(rows)


def volume_menu(index: int, token: str, can_delete: bool) -> InlineKeyboardMarkup:
    """单块硬盘的操作菜单。

    ``can_delete`` 请直接传 ``VolumeView.can_delete`` —— 它已经包含
    「没挂载」**且**「挂载状态确认过」两个条件。这个参数原来叫
    ``is_attached``（语义相反），调用方传错一次就会给已挂载的卷
    配上删除按钮（2026-09-21 实测踩过）。**故意取成肯定式命名。**
    """
    rows = [
        [InlineKeyboardButton("📈 扩容…", callback_data=f"vex:{index}:{token}")],
    ]
    if can_delete:
        rows.append([InlineKeyboardButton("🗑 删除卷（不可逆）", callback_data=f"vd:{index}:{token}")])
    else:
        # ⚠️ 注意这里**只套一层**列表。2026-09-20 的宽度审计脚本撞出来的：
        #    原来写成 `rows.append([[InlineKeyboardButton(...)]])`，多套了一层，
        #    于是 `InlineKeyboardMarkup` 直接抛
        #    「should be a sequence of sequences of InlineKeyboardButtons」——
        #    也就是**只要打开一块「已挂载」的硬盘菜单就必然报错**。
        #    这个分支以前从没被测试覆盖过（测试只喂了 is_attached=False）。
        rows.append([InlineKeyboardButton("（不可删：已挂载或状态未确认）", callback_data="x")])
    rows.append([
        InlineKeyboardButton("◀️ 返回列表", callback_data="vlr"),
        InlineKeyboardButton("🏠 主菜单", callback_data="m"),
    ])
    return InlineKeyboardMarkup(rows)


def task_menu(tasks: list, stop_tokens: list[str]) -> InlineKeyboardMarkup:
    """任务管理：列出全部抢机任务，在跑的给停止按钮。"""
    rows = []
    for t in tasks:
        icon = {"running": "🔄", "succeeded": "✅", "stopped": "⏹", "blocked": "🔒"}.get(t.status, "·")
        if t.status == "running":
            btn = InlineKeyboardButton(f"{icon} {t.token} · 停掉它", callback_data=f"tkstop:{t.token}")
        else:
            btn = InlineKeyboardButton(f"{icon} {t.token}（{t.done_count}/{t.want}）", callback_data="x")
        rows.append([btn])
    rows.append([InlineKeyboardButton("◀️ 主菜单", callback_data="m")])
    return InlineKeyboardMarkup(rows)


def account_picker(accounts: list[Account], page: int, action: str) -> InlineKeyboardMarkup:
    """账号选择器。

    ``action`` 决定选中账号后跳到哪个界面：
      ``a``  账号总菜单    ``il`` 实例列表    ``bl`` 桶列表
      ``au`` 审计          ``se`` 安全审计
    """
    total_pages = max(1, (len(accounts) + ACCOUNTS_PER_PAGE - 1) // ACCOUNTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * ACCOUNTS_PER_PAGE
    chunk = accounts[start:start + ACCOUNTS_PER_PAGE]

    rows: list[list[InlineKeyboardButton]] = []
    for acc in chunk:
        label = f"{acc.index}. {acc.alias or acc.region}"
        rows.append([InlineKeyboardButton(label, callback_data=f"{action}:{acc.index}")])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ 上页", callback_data=f"ap:{page - 1}:{action}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="x"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("下页 ▶️", callback_data=f"ap:{page + 1}:{action}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 主菜单", callback_data="m")])
    return InlineKeyboardMarkup(rows)


def account_menu(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 配额", callback_data=f"q:{index}"),
            InlineKeyboardButton("🖥 实例列表", callback_data=f"il:{index}"),
        ],
        [
            InlineKeyboardButton("➕ 开新机", callback_data=f"lp:{index}"),
            InlineKeyboardButton("🪣 存储桶", callback_data=f"bl:{index}"),
        ],
        [
            InlineKeyboardButton("🧹 残留审计", callback_data=f"au:{index}"),
            InlineKeyboardButton("🔒 暴露面", callback_data=f"se:{index}"),
        ],
        [
            InlineKeyboardButton("🔑 S3 密钥", callback_data=f"sk:{index}"),
            InlineKeyboardButton("🛡 加固 SSH", callback_data=f"hd:{index}"),
        ],
        [
            InlineKeyboardButton("◀️ 换个账号", callback_data="ap:0:a"),
            InlineKeyboardButton("🏠 主菜单", callback_data="m"),
        ],
    ])


def instance_list(index: int, instance_tokens: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """实例列表：每台一行按钮，点进去看操作菜单。

    ``instance_tokens`` 是 [(短令牌, 显示名), ...]。
    """
    rows: list[list[InlineKeyboardButton]] = []
    for token, label in instance_tokens:
        rows.append([InlineKeyboardButton(label, callback_data=f"im:{index}:{token}")])
    rows.append([
        InlineKeyboardButton("➕ 开新机", callback_data=f"lp:{index}"),
        InlineKeyboardButton("🔄 刷新", callback_data=f"il:{index}"),
    ])
    rows.append([
        InlineKeyboardButton("◀️ 返回", callback_data=f"a:{index}"),
        InlineKeyboardButton("🏠 主菜单", callback_data="m"),
    ])
    return InlineKeyboardMarkup(rows)


def instance_menu(index: int, token: str, is_running: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if is_running:
        rows.append([
            InlineKeyboardButton("⏹ 关机", callback_data=f"ip:{index}:{token}:stop"),
            InlineKeyboardButton("🔄 重启", callback_data=f"ip:{index}:{token}:reboot"),
        ])
    else:
        rows.append([
            InlineKeyboardButton("▶️ 开机", callback_data=f"ip:{index}:{token}:start"),
        ])
    # 救援控制台**只对运行中的实例可用**（OCI 会拒绝停机实例），
    # 所以停机时干脆不显示 —— 亮一个点了必然报错的按钮是坏体验。
    # 「下载登录密钥」不受运行状态影响：它读的是实例 metadata（停机也读得到）
    # 和服务器上的密钥文件，与实例是否在跑无关。
    rows.append([
        InlineKeyboardButton("🔑 下载登录密钥", callback_data=f"ip:{index}:{token}:key"),
    ])
    last_row = [
        InlineKeyboardButton("🗑 销毁", callback_data=f"ip:{index}:{token}:terminate"),
    ]
    if is_running:
        last_row.insert(0, InlineKeyboardButton(
            "🚑 救援控制台", callback_data=f"ip:{index}:{token}:rescue"))
    rows.append(last_row)
    rows.append([
        InlineKeyboardButton("◀️ 返回列表", callback_data=f"il:{index}"),
        InlineKeyboardButton("🏠 主菜单", callback_data="m"),
    ])
    return InlineKeyboardMarkup(rows)


def volume_list(index: int, volume_tokens: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """硬盘列表：每块卷一行按钮。``volume_tokens`` = [(短令牌, 显示名), ...]"""
    rows: list[list[InlineKeyboardButton]] = []
    for token, label in volume_tokens:
        if len(label) > 40:
            label = label[:39] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=f"vm:{index}:{token}")])
    rows.append([
        InlineKeyboardButton("🔄 刷新", callback_data=f"vl:{index}"),
        InlineKeyboardButton("🏠 主菜单", callback_data="m"),
    ])
    return InlineKeyboardMarkup(rows)


def bucket_list(index: int, bucket_tokens: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for token, label in bucket_tokens:
        rows.append([InlineKeyboardButton(label, callback_data=f"bm:{index}:{token}")])
    rows.append([
        InlineKeyboardButton("🔄 刷新", callback_data=f"bl:{index}"),
        InlineKeyboardButton("🔑 S3 密钥", callback_data=f"sk:{index}"),
    ])
    rows.append([
        InlineKeyboardButton("◀️ 返回", callback_data=f"a:{index}"),
        InlineKeyboardButton("🏠 主菜单", callback_data="m"),
    ])
    return InlineKeyboardMarkup(rows)


def bucket_menu(index: int, token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 看对象", callback_data=f"ob:{index}:{token}"),
            InlineKeyboardButton("🗑 删除桶", callback_data=f"bp:{index}:{token}"),
        ],
        [
            InlineKeyboardButton("◀️ 返回列表", callback_data=f"bl:{index}"),
            InlineKeyboardButton("🏠 主菜单", callback_data="m"),
        ],
    ])


def confirm(cancel_data: str, ok_token: str | None = None) -> InlineKeyboardMarkup:
    """二次确认键盘。

    破坏性操作的确认按钮**刻意做成一个单独的大按钮**，
    不和「取消」并排 —— 手机上误触的代价太大。
    """
    rows: list[list[InlineKeyboardButton]] = []
    if ok_token:
        rows.append([InlineKeyboardButton("✅ 确认执行", callback_data=f"ok:{ok_token}")])
        rows.append([InlineKeyboardButton("❌ 取消", callback_data=f"no:{ok_token}")])
    else:
        rows.append([InlineKeyboardButton("❌ 取消", callback_data=cancel_data)])
    rows.append([InlineKeyboardButton("🏠 主菜单", callback_data="m")])
    return InlineKeyboardMarkup(rows)


def back_to_main(extra: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    rows.append([InlineKeyboardButton("🏠 主菜单", callback_data="m")])
    return InlineKeyboardMarkup(rows)


def cancel_only(cancel_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ 返回", callback_data=cancel_data)],
        [InlineKeyboardButton("🏠 主菜单", callback_data="m")],
    ])
