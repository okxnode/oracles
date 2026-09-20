"""文本渲染：把数据模型转成 Telegram 消息。

Telegram 单条消息上限 4096 字符，所以所有长文本都要过 ``chunk()``。
"""
from __future__ import annotations

from ..config import Settings
from ..models import BOOT_VOLUME_GB, BucketView, InstanceView, OrphanVolume
from ..utils import fmt_gb, humanize_state

#: 留一点余量，别贴着 4096 上限
CHUNK_LIMIT = 3800


def chunk(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """按行切分长文本，尽量不把一行切断。"""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit and buf:
            parts.append("".join(buf))
            buf, size = [], 0
        # 单行就超限的极端情况，硬切
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        buf.append(line)
        size += len(line)
    if buf:
        parts.append("".join(buf))
    return parts


def esc(text: str) -> str:
    """转义 Markdown 里的特殊字符。

    资源名里常有 ``_`` ``*`` ``[`` 之类，不转义会导致 Telegram
    报 "can't parse entities" 整个消息发不出去。
    """
    for ch in ("_", "*", "[", "]", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


# --------------------------------------------------------------------------
#  菜单文本
# --------------------------------------------------------------------------
def welcome(settings: Settings, warnings: list[str]) -> str:
    """``/start`` 的回复文本：一行状态提示 + 把版面让给七大功能按钮。"""
    mode = "🔧 可写" if settings.write_enabled else "👀 只读（DRY-RUN）"
    if settings.write_enabled and not settings.allow_destructive:
        mode += "，不可逆操作仍禁用"
    lines = [
        f"☁️ *OCI 多账号管理台*　{mode}",
        "",
        "👇 七大功能入口在下面。写操作一律两段式：先出计划 → 你点确认才执行。",
        "完整说明见「❓ 帮助」或 `/help`。",
    ]
    if warnings:
        lines.append("")
        lines.extend(warnings)
    return "\n".join(lines)


def help_text() -> str:
    return """📖 *使用帮助 —— 七大功能菜单*

``/start`` 打开主菜单（下面七个入口都在按钮里，命令只是快捷方式）：

*1️⃣ 配置管理*
账号清单的**增 / 删 / 查**。点「体检」逐个检查私钥文件、OCID、fingerprint；
「🔁 重新体检」会真调一次最轻 API 验证整条凭据链。
添加账号：发一段 JSON（index/alias/region/user/tenancy/fingerprint/key_file）。

*2️⃣ 开机 / 自动抢机*
分步向导：规格（E2 Micro / A1.Flex 自定义核数内存）→ 系统 → 磁盘大小 →
台数 → 登录方式（SSH 公钥 **或** 用户名+密码 cloud-init 注入）→ IPv6。
最后二选一：
· 「现在开机」= 单次尝试，失败就停
· 「自动抢机 5/10/20/30s」= 后台循环重试，抢到 N 台为止，随时可手动停止

*3️⃣ 实例管理*
列表（含公网 IP / 状态）→ 单台菜单：开机 / 关机 / 软关 / 重启 / **销毁** /
🚑 **救援**（开串口控制台 + VNC 隧道，SSH 不通时直接看屏幕敲 shell；
只对运行中的实例显示）。

*4️⃣ 配额查询*
全部账号总览，或逐账号的可用域明细 —— E2/A1 余量、块存储余量、可开机数。

*5️⃣ 硬盘管理*
列出引导卷 + 数据卷（含挂载关系）。未挂载的卷可以**扩容**或**删除**；
执行删卷时会实时复核挂载状态，挂了就直接拒绝。

*6️⃣ 任务管理*
查看自动抢机任务的进度（x/N 台、尝试次数、最后失败原因），一键停止运行中的任务。

*7️⃣ 存储桶管理*
列表 + 对象统计；删桶前自动清理预认证请求（PAR）；可签发 S3 兼容密钥。

*脚本式命令（老接口保留，参数走命令行）*
`/q [账号]` · `/i [账号]` · `/b [账号]` · `/obj <账号> <桶名>`
`/audit [账号]` 计费残留　`/security [账号]` 暴露面
`/launch <账号>` · `/start_vm <账号> <名称或OCID>` · `/stop_vm …` · `/reboot …` · `/terminate …`
`/rmbucket <账号> <桶名>` · `/s3key <账号>` · `/harden <账号> <CIDR>`

*通用*
`/status` 运行状态与开关　`/cancel` 取消待确认操作　`/help` `/h` 本帮助

⚠️ **安全设计**：所有写操作两段式（计划 → 「✅ 确认执行」才真正调 API）；
破坏性操作还要过「不可逆开关」。服务端两个总闸在 `<配置目录>/oracles.env`：
`ORACLES_WRITE_ENABLED`、`ORACLES_ALLOW_DESTRUCTIVE`。"""


def status_text(settings: Settings, warnings: list[str]) -> str:
    lines = [
        "⚙️ *运行状态*",
        "",
        f"配置目录：`{settings.home}`",
        f"账号清单：`{settings.accounts_file}`",
        f"账号数量：{len(settings.accounts)}",
        f"授权用户：{len(settings.allowed_user_ids)} 人",
        f"写操作：{'✅ 开启' if settings.write_enabled else '❌ 关闭（DRY-RUN）'}",
        f"不可逆操作：{'✅ 允许' if settings.allow_destructive else '❌ 禁止'}",
        f"并发上限：{settings.max_concurrency}",
        f"超时：{settings.timeout}s",
    ]
    if warnings:
        lines.append("")
        lines.extend(warnings)
    lines.append("")
    lines.append("开关写在 `<配置目录>/oracles.env`，改完重启服务生效。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
#  实例
# --------------------------------------------------------------------------
def render_instance(view: InstanceView) -> str:
    lines = [
        f"🖥 *{esc(view.display_name)}*",
        "",
        f"状态：{humanize_state(view.lifecycle_state)}",
        f"规格：`{view.shape}`",
        f"可用域：{view.ad.split(':')[-1]}",
        f"公网 IP：`{view.public_ip or '无'}`",
        f"内网 IP：`{view.private_ip or '无'}`",
        f"创建时间：{(view.time_created or '-')[:19]}",
    ]
    if view.in_pool:
        lines.append("")
        lines.append("⚠️ *该实例由实例池管理* —— 直接销毁会白删，"
                     "池会在几十秒内再开一台一样的。")
    return "\n".join(lines)


def render_instance_list(views: list[InstanceView], label: str) -> str:
    if not views:
        return f"【{label}】没有实例。"
    running = sum(1 for v in views if v.is_running)
    lines = [f"【{label}】共 {len(views)} 台，运行中 {running} 台", ""]
    for v in sorted(views, key=lambda x: x.display_name):
        icon = "🟢" if v.is_running else "⚪"
        pool = " 🅿️" if v.in_pool else ""
        lines.append(f"{icon} *{esc(v.display_name)}*{pool}")
        lines.append(f"    `{v.shape}` · {v.ad.split(':')[-1]} · "
                     f"`{v.public_ip or '无公网IP'}`")
    lines.append("")
    lines.append("（🅿️ = 池管理实例）")
    return "\n".join(lines)


# --------------------------------------------------------------------------
#  存储桶
# --------------------------------------------------------------------------
def render_bucket_list(views: list[BucketView], label: str) -> str:
    if not views:
        return f"【{label}】没有存储桶。"
    lines = [f"【{label}】共 {len(views)} 个桶", ""]
    for b in views:
        extra = []
        if b.object_count is not None:
            extra.append(f"{b.object_count} 个对象")
        if b.size_bytes is not None:
            extra.append(f"{b.size_gib:.3f} GiB")
        if b.versioning == "Enabled":
            extra.append("⚠️ 开了版本控制")
        if b.par_count:
            extra.append(f"{b.par_count} 个 PAR")
        tail = f"（{'，'.join(extra)}）" if extra else ""
        lines.append(f"🪣 *{esc(b.name)}*{tail}")
        if b.created:
            lines.append(f"   创建于 {b.created[:10]}")
    return "\n".join(lines)


def render_bucket_objects(bucket: str, objects: list[dict]) -> str:
    if not objects:
        return (f"🪣 *{esc(bucket)}*\n\n"
                f"✅ 确认是空桶（0 个对象）。\n"
                f"⚠️ 这里必须区分「空桶」和「查询失败」——"
                f"查询失败会直接报错，不会显示成空。")
    total = sum(o["size"] for o in objects)
    lines = [f"🪣 *{esc(bucket)}* —— {len(objects)} 个对象，"
             f"合计 {total / 1024 ** 2:.2f} MiB", ""]
    for o in objects[:40]:
        lines.append(f"· `{esc(o['name'])}` ({o['size'] / 1024:.1f} KiB)")
    if len(objects) > 40:
        lines.append(f"… 另有 {len(objects) - 40} 个")
    return "\n".join(lines)


# --------------------------------------------------------------------------
#  审计
# --------------------------------------------------------------------------
def render_orphan_confirm(volumes: list[OrphanVolume], label: str) -> str:
    total = sum(v.size_gb for v in volumes)
    lines = [f"🗑 *删除孤儿卷计划*（{label}）", ""]
    for v in volumes:
        lines.append(f"· {esc(v.display_name)} | {v.size_gb:.0f} GB | "
                     f"{v.ad.split(':')[-1]}")
    lines.extend([
        "",
        # ⚠️ 用常量，别写死数字（这里原来是 47，常量改成 50 后就会显示错）。
        f"合计 {len(volumes)} 块，回收 {total:.0f} GB"
        f"（≈ {int(total // BOOT_VOLUME_GB)} 台机器的空间）",
        "",
        "执行前会**重新核对一次挂载关系**，已被挂载的卷会被自动剔除。",
        "⚠️ 此操作不可逆。",
    ])
    return "\n".join(lines)


def fmt_quota_error(label: str, error: str) -> str:
    return f"❌ 【{label}】查询失败：\n{error}"


__all__ = [
    "chunk", "esc", "fmt_gb", "fmt_quota_error", "help_text",
    "render_bucket_list", "render_bucket_objects", "render_instance",
    "render_instance_list", "render_orphan_confirm", "status_text", "welcome",
]
