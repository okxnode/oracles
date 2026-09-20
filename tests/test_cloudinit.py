"""cloud-init 生成的回归测试。

这个文件是 2026-09-20 那场事故的直接产物。

**事故**：向导里选「用户名+密码」开出来的 4 台机器，密钥和密码**两条路都进不去**。
根因是 `users:` 列表漏了 `- default` —— cloud-init 的 `users:` 是「替换」语义，
于是默认用户 ubuntu 没被创建，OCI metadata 里的 `ssh_authorized_keys` 没有落点，
被兜到 root、又被 `disable_root` 包上强制命令。密码那条路则因为 Ubuntu 云镜像
默认 `PasswordAuthentication no` 而**从来没被打开过**。

**更深一层**（靠挂载启动盘读 cloud-init 日志才挖出来的）：4 台里有 1 台的用户名
是 `admin`，而 Ubuntu 26.04 的 `/etc/group` 预置了 `admin:x:107:`。
`useradd admin` 在 `USERGROUPS_ENAB=yes` 下要连同名组一起建 → 撞车 → `exit 9` →
cloud-init 的 `users_groups` 模块**整体 FAIL** → 后面写 `ssh_authorized_keys`、
配 sudoers 的步骤全部没执行。这才是第一张多米诺骨牌，`- default` 是第二张。

**为什么之前没测出来**：`cloudinit.py` 当时零测试覆盖。而这个文件的特点是
——**生成错了不报错，只让机器静默地登不进去**。属于本项目「静默失效」家族里
最贵的一种：直到你真需要 SSH 的那一刻才发现。

所以下面的断言刻意针对「**缺失**」而不是「存在」：
少了 `- default`、少了 `ssh_pwauth`、少了 `primary_group`、`ssh_pwauth` 被缩进
—— 这几种情况下生成的 YAML 依然合法、cloud-init 依然不报错、机器依然能起来。
只有断言能拦住。

---

**2026-09-20 第二轮**（用户报「设了用户名密码，新机器仍然是密钥登录」）又加了
三条铁律，对应本文件后半部分的测试：

- **铁律 5**：绝不自己用 `runcmd` 写「改 sshd_config + 重启 sshd」。
  Ubuntu 上 sshd 的服务名是 **`ssh`**，`systemctl restart sshd || true`
  报 `Unit sshd.service not found` 又被 `|| true` 吞掉 → 配置写好了、
  sshd 从没重载 → **只能密钥登录，日志里一句报错都没有**。
  → 交给 `ssh_pwauth` 模块。断言：生成的配置里**不许出现 `runcmd` / `systemctl`**。
- **铁律 6**：密码走 `chpasswd:` 模块，且必须 `expire: false`。
  `chpasswd.expire` 默认 **true** → 密码被设成「已过期」→ 首登强制改密码，
  在自动化场景下跟「密码不对」长得一模一样。
  同时 `users:` 里要 `lock_passwd: false`（默认 true 会把密码位写成 `!`）。
- **铁律 7**：顶层没有 `version` 键。

⚠️ 注意这几条**都不是**「YAML 写错了」——生成的配置语法完全合法，
cloud-init 也不报错。它们只在**真开一台机器去登**的时候才暴露。
所以配套的真机验证脚本（`/tmp/probe_pwlogin.py`，一次性的）会真的开一台
A1.Flex、用密码 SSH 登进去、跑 `passwd -S` / `chage -l` 取证，再销毁。

**2026-09-20 第三轮**（用户报「`<实例公网IP>` 这台 root 密码登不进去」）
加了**铁律 8**：用户名是 `root` 时，`ssh_pwauth: true` **不够** ——
cloud-init 里没有任何一个键会去写 `PermitRootLogin`（`disable_root` 只管
root 的 `authorized_keys` 强制命令前缀），`permitrootlogin prohibit-password`
是 OpenSSH 的**编译默认值**。必须自己 `write_files` 写一份 drop-in。

第三轮起，新测试改用 **PyYAML 真解析**（`pip install -e ".[dev]"` 已带上
`PyYAML`）。理由：前两轮的纯文本断言被**注释污染**坑过三次 ——
`assert "X" in 全文` 这种写法，只要注释里也写着 `X`，把真正的代码行删掉
测试照样全绿。`write_files` 是嵌套结构（`content: |` 块标量），
用文本匹配去认它出错概率太高。前两轮的断言保留原样（它们已经在守着东西了）。
"""
from __future__ import annotations

import base64

import pytest

from oracles.cloudinit import build_cloud_init, to_user_data


# ---------------------------------------------------------------------------
#  极简结构解析：只认我们关心的两件事（顶层键 / users 列表条目）
# ---------------------------------------------------------------------------
def _top_level_keys(text: str) -> list[str]:
    """顶层键 = 不缩进且含冒号的行。缩进的 `ssh_pwauth` 不会出现在这里。"""
    keys = []
    for line in text.splitlines():
        if not line or line.startswith("#") or line.startswith(" "):
            continue
        if ":" in line:
            keys.append(line.split(":", 1)[0].strip())
    return keys


def _users_entries(text: str) -> list[str]:
    """取 `users:` 块里 `- xxx` 条目的值（跳过 `  key: value` 这类续行）。"""
    entries: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("users:"):
            inside = True
            continue
        if not inside:
            continue
        if line.startswith("- "):
            entries.append(line[2:].strip())
        elif line and not line.startswith(" "):
            break          # 回到顶层 → users 块结束
    return entries


# ---------------------------------------------------------------------------
#  铁律 1：users: 必须保留默认用户
# ---------------------------------------------------------------------------
def test_users_list_keeps_the_default_user() -> None:
    """🔴 核心回归。删掉这一行，机器就会彻底登不进去（2026-09-20 实测）。

    为什么用「列表条目」而不是子串匹配：`- default` 这几个字也可能出现在
    注释或别的键里，只有真的作为 users 列表的一项才算数。
    """
    cfg = build_cloud_init(username="root", password="x")
    assert "default" in _users_entries(cfg), (
        "users: 里丢了 `- default` —— cloud-init 会替换掉默认用户，"
        "metadata 的 ssh_authorized_keys 将无处可落。\n" + cfg
    )


def test_users_list_has_both_default_and_custom_user() -> None:
    """默认用户和自定义用户必须同时存在，不能二选一。"""
    cfg = build_cloud_init(username="deploy", password="x")
    entries = _users_entries(cfg)
    assert entries[0] == "default"
    assert any("name: deploy" in e for e in entries)


def test_users_list_is_not_empty_without_username() -> None:
    """只给 hostname 时不该凭空造出 users 块（否则又会顶掉默认用户）。"""
    cfg = build_cloud_init(hostname="box")
    assert "users:" not in cfg
    assert "default" not in cfg


# ---------------------------------------------------------------------------
#  铁律 2 + 3：ssh_pwauth 的开关与位置
# ---------------------------------------------------------------------------
def test_ssh_pwauth_is_enabled_when_password_is_set() -> None:
    """🔴 只写 plain_text_passwd 是登不进去的 —— 云镜像的 `PasswordAuthentication no`
    会赢，必须 ssh_pwauth: true 才覆盖得掉。没有它，「首启即用」就是空话。"""
    cfg = build_cloud_init(username="root", password="hunter2")
    assert "ssh_pwauth: true" in cfg


def test_ssh_pwauth_is_absent_without_password() -> None:
    """没设密码就不该开密码认证 —— 那是白送一个攻击面。"""
    cfg = build_cloud_init(username="root")
    assert "ssh_pwauth" not in cfg


def test_ssh_pwauth_is_a_top_level_key_not_indented() -> None:
    """⚠️ 缩进的 `ssh_pwauth` 会被当成 users 条目的属性，**静默失效**。

    这是最阴的一种写法：YAML 合法、cloud-init 不报错、功能就是不生效。
    所以断言「它是顶层键」而不是「字符串出现过」。
    """
    cfg = build_cloud_init(username="root", password="x")
    assert "ssh_pwauth" in _top_level_keys(cfg), (
        "ssh_pwauth 不是顶层键（多半被缩进了）—— 会静默失效。\n" + cfg
    )


# ---------------------------------------------------------------------------
#  铁律 4：用户名撞系统组名时必须指定 primary_group
# ---------------------------------------------------------------------------
def test_admin_gets_primary_group() -> None:
    """🔴 事故回归。`admin` 在 Ubuntu 26.04 的 /etc/group 里是预置组（GID 107）。

    不指定 primary_group 的话，cloud-init 会跑
    `useradd admin --shell /bin/bash -m`，而 USERGROUPS_ENAB=yes 让它先建同名组，
    撞上已存在的组就 exit 9：

        useradd: group admin exists - if you want to add this user to that group, use -g.

    接着 cloud-init 的 users_groups 模块**整体 FAIL**，后面写
    ssh_authorized_keys、配 sudoers 的步骤全部不执行 —— 机器就此登不进去。
    """
    cfg = build_cloud_init(username="admin", password="x")
    assert "  primary_group: admin" in cfg, (
        "用户名叫 admin 却没给 primary_group —— useradd 会因同名组已存在而失败，"
        "并连累整个 users_groups 模块。\n" + cfg
    )


def test_other_system_group_names_also_get_primary_group() -> None:
    """`admin` 不是孤例；sudo / staff / www-data 等预置组名同理。"""
    for name in ("sudo", "staff", "www-data", "adm", "users", "nogroup"):
        cfg = build_cloud_init(username=name, password="x")
        assert f"  primary_group: {name}" in cfg, f"{name} 缺 primary_group\n{cfg}"


def test_ordinary_username_has_no_primary_group() -> None:
    """普通名字不该凭空多出 primary_group —— 那个组可能压根不存在，
    `useradd -g <不存在的组>` 一样会失败。"""
    cfg = build_cloud_init(username="deploy", password="x")
    assert "primary_group" not in cfg


def test_primary_group_stays_inside_users_block() -> None:
    """⚠️ primary_group 必须是 users 条目的属性（缩进），不能跑到顶层去。"""
    cfg = build_cloud_init(username="admin", password="x")
    assert "primary_group" not in _top_level_keys(cfg)
    assert len(_users_entries(cfg)) == 2      # default + admin，没多出条目


def test_default_is_rejected_as_username() -> None:
    """`default` 是 users: 列表里的保留字，拿它当用户名会生成两条 default 条目。"""
    with pytest.raises(ValueError):
        build_cloud_init(username="default", password="x")


# ---------------------------------------------------------------------------
#  铁律 5：绝不自己写「开密码登录」那套（服务名坑）
# ---------------------------------------------------------------------------
def test_no_runcmd_at_all() -> None:
    """🔴 核心回归。老格式用 `runcmd` 手写「改 sshd_config + 重启 sshd」，
    而 Ubuntu 上 sshd 的服务名是 **`ssh`** 不是 `sshd`：

        systemctl restart sshd || true     # → Unit sshd.service not found

    `|| true` 把错误吞了 → 配置写好了、sshd 从没重载 → 机器**只能密钥登录**，
    日志里一句报错都没有。2026-09-20 用户报的「设了用户名密码没生效」就是它。

    结论：这类动作一律交给 cloud-init 的 `ssh_pwauth` 模块（它知道该重启谁），
    本文件生成的配置里**不允许出现 runcmd**。
    """
    cfg = build_cloud_init(username="deploy", password="hunter2")
    assert "runcmd" not in cfg, "又回去手写 runcmd 了 —— 会踩 sshd/ssh 服务名坑。\n" + cfg
    assert "systemctl" not in cfg, "不该在 cloud-init 里手动重启服务。\n" + cfg


# ---------------------------------------------------------------------------
#  铁律 6：密码走 chpasswd 模块，且 expire: false
# ---------------------------------------------------------------------------
def test_password_goes_through_chpasswd_module() -> None:
    """密码用官方的 `chpasswd:` 模块，而不是 users 条目里的 `plain_text_passwd`。

    区别就在 `expire`：`chpasswd.expire` 默认是 **true**，会把密码设成
    「已过期」→ 用户首次登录被强制改密码。在「用脚本/自动化登录」的场景下，
    这看起来跟「密码不对、登不进去」一模一样。
    """
    cfg = build_cloud_init(username="deploy", password="hunter2")
    assert "chpasswd" in _top_level_keys(cfg), "密码没走 chpasswd 模块。\n" + cfg
    assert "plain_text_passwd" not in cfg, (
        "还在用 users 条目的 plain_text_passwd —— 拿不到 `expire: false` 这个控制权。\n" + cfg
    )


def test_chpasswd_never_expires_the_password() -> None:
    """🔴 必须显式 `expire: false`（默认是 true）。"""
    cfg = build_cloud_init(username="deploy", password="hunter2")
    assert "  expire: false" in cfg, (
        "chpasswd 少了 `expire: false` —— 默认 true 会把密码设成已过期，"
        "首登会被强制改密码，表现成「密码不对」。\n" + cfg
    )
    assert "expire: true" not in cfg


def test_chpasswd_block_shape() -> None:
    """chpasswd 的 YAML 结构：expire 与 users 平级，条目在 `- name:` 下。"""
    cfg = build_cloud_init(username="deploy", password="hunter2")
    lines = cfg.splitlines()
    i = lines.index("chpasswd:")
    assert lines[i + 1] == "  expire: false"
    assert lines[i + 2] == "  users:"
    assert lines[i + 3] == "  - name: deploy"
    assert lines[i + 4] == "    password: 'hunter2'"
    assert lines[i + 5] == "    type: text"


def test_chpasswd_absent_without_password() -> None:
    cfg = build_cloud_init(username="deploy")
    assert "chpasswd" not in cfg


def test_lock_passwd_is_disabled_when_password_is_set() -> None:
    """`lock_passwd` 默认 true，会把密码位写成 `!` —— 那样密码到底能不能用，
    就取决于模块先后顺序了。显式 false 把这件事钉死。"""
    cfg = build_cloud_init(username="deploy", password="hunter2")
    assert "  lock_passwd: false" in cfg


def test_lock_passwd_absent_without_password() -> None:
    """没设密码时不该写 lock_passwd —— 保持默认（锁住）才是对的。"""
    cfg = build_cloud_init(username="deploy")
    assert "lock_passwd" not in cfg


# ---------------------------------------------------------------------------
#  铁律 7：顶层不写 `version`（去掉误导，不是「会报错」）
# ---------------------------------------------------------------------------
def test_no_version_key() -> None:
    """顶层别写 `version` —— 但**理由不是「会报 unknown key」**。

    2026-09-20 用 `cloud-init schema --config-file` 实测：
    `version: v1`、甚至 `version: v999zzz`，**都是 `Valid schema`**。
    因为 schema 里它就是 `"version": {}`（空 schema = 不校验）。

    删掉它的理由是**去掉误导**：它不产生任何效果（没有模块读它），
    留着会让人误以为它和 `#cloud-config` 头一起构成「版本声明」。

    ⚠️ 对照组很重要：同一台机器上 `totally_unknown_module: ...` 会被判
    `Invalid user-data` —— 说明这个校验器有鉴别力，不是「什么都放行」。
    """
    cfg = build_cloud_init(username="deploy", password="hunter2")
    assert "version" not in _top_level_keys(cfg), "顶层又冒出 version 键了。\n" + cfg


# ---------------------------------------------------------------------------
#  其余行为
# ---------------------------------------------------------------------------
def test_password_with_single_quote_is_escaped() -> None:
    """密码里可能有单引号；YAML 单引号字符串靠 `''` 转义。"""
    cfg = build_cloud_init(username="root", password="it's a 'test'")
    assert "password: 'it''s a ''test'''" in cfg


def test_password_without_username_is_rejected() -> None:
    """给了密码却没用户名 = 密码无处可设，必须报错而不是静默丢弃。"""
    with pytest.raises(ValueError):
        build_cloud_init(password="x")


def test_nothing_requested_returns_empty_string() -> None:
    """没有任何要注入的内容 → 返回空串，调用方据此跳过 user_data。"""
    assert build_cloud_init() == ""


def test_hostname_is_sanitized() -> None:
    """主机名只留字母数字和连字符，防止 YAML 注入。"""
    cfg = build_cloud_init(hostname="a b; rm -rf /\nusers:\n- x", username="root")
    assert "hostname: abrm-rf" in cfg
    assert len(_users_entries(cfg)) == 2      # 注入的 users 没生效


def test_first_line_is_the_cloud_config_marker() -> None:
    """`#cloud-config` 必须是**第一行**，否则 cloud-init 根本不把它当 cloud-config 处理。"""
    cfg = build_cloud_init(username="deploy", password="hunter2")
    assert cfg.splitlines()[0] == "#cloud-config"


# ---------------------------------------------------------------------------
#  to_user_data
# ---------------------------------------------------------------------------
def test_to_user_data_roundtrips() -> None:
    cfg = build_cloud_init(username="root", password="p@ss")
    encoded = to_user_data(cfg)
    assert encoded is not None
    assert base64.b64decode(encoded).decode("utf-8") == cfg


def test_to_user_data_returns_none_for_empty() -> None:
    assert to_user_data("") is None
    assert to_user_data("   \n") is None


def test_to_user_data_rejects_oversize() -> None:
    with pytest.raises(ValueError):
        to_user_data("x" * (17 * 1024))


# ---------------------------------------------------------------------------
#  铁律 8：用户名是 root 时，必须自己写 PermitRootLogin 的 drop-in
# ---------------------------------------------------------------------------
#: 故意**不引用** `cloudinit._ROOT_LOGIN_DROPIN`，而是把路径写死在这里。
#: 这样一旦有人改常量，这条测试会红 —— 改文件名是个需要「有意识决定」的动作
#: （文档、运维手册、已经手工修过的机器都指向这个路径），不该顺手改掉。
#: 同理：这个路径**必须**以 `00-` 开头，见 `test_dropin_sorts_before_...`。
_ROOT_LOGIN_DROPIN_LITERAL = "/etc/ssh/sshd_config.d/00-oracles-root-login.conf"


def _parse(text: str) -> dict:
    """把生成的 cloud-config 解析成真正的结构。

    ⚠️ 这里**必须**用真解析器。前两轮的纯文本断言被注释污染坑过三次：
        `assert "X" in 全文`，只要注释里也写着 `X`，删掉真正的代码行
        测试照样绿。`write_files` 的 `content: |` 是块标量，
        文本匹配更容易错。
    """
    import yaml

    data = yaml.safe_load(text)
    assert isinstance(data, dict), f"顶层不是 mapping，生成结果有问题：\n{text}"
    return data


def _root_login_dropin(text: str) -> dict | None:
    """从配置里取出「PermitRootLogin drop-in」那一条 write_files 记录。

    没写就返回 None —— **调用方必须区分 None 和「内容为空」**，
    不能拿 `or ""` 糊过去，那正是本项目的 `[]` vs `None` 纪律。
    """
    data = _parse(text)
    files = data.get("write_files")
    if not files:
        return None
    for entry in files:
        if entry.get("path") == _ROOT_LOGIN_DROPIN_LITERAL:
            return entry
    return None


def test_root_user_gets_a_permit_root_login_dropin() -> None:
    """🔴 核心回归：root + 密码 → 必须写出 `PermitRootLogin yes` 的 drop-in。

    修复前（2026-09-20 真机）：机器开出来 `sshd -T` 是
    `permitrootlogin prohibit-password`，SSH 死活进不去；
    走串口能进去（串口绕过 sshd）—— 密码明明是设上的。
    """
    entry = _root_login_dropin(build_cloud_init(username="root", password="pw"))
    assert entry is not None, (
        "root 用户的配置里没有 PermitRootLogin drop-in —— "
        "ssh_pwauth 改不到 root，这台机器只能密钥登录（或走串口救）"
    )
    content = entry.get("content")
    assert isinstance(content, str) and content.strip(), (
        f"drop-in 的内容是空的/不是字符串：{content!r}"
    )
    assert "PermitRootLogin yes" in content, f"内容不对：{content!r}"


def test_the_dropin_is_written_by_write_files_not_runcmd() -> None:
    """落点必须是 `write_files`，不能是 `runcmd`（铁律 5 的延伸）。

    `write_files` 是 `cloud_init_modules` 第 4 个，`set_passwords` 是第 16 个，
    而后者明确 `Before=sshd.service` → **sshd 首次启动时文件已在盘上**，
    不需要谁去重启服务。走 `runcmd` 就得自己 `systemctl restart`，
    那就又踩回「服务名是 `ssh` 不是 `sshd`」的静默失效里。
    """
    text = build_cloud_init(username="root", password="pw")
    data = _parse(text)
    assert "write_files" in data, "drop-in 没走 write_files"
    assert "runcmd" not in data, "又用 runcmd 了 —— 见铁律 5，别再自己重启 sshd"
    assert "systemctl" not in text, "配置里出现了 systemctl，说明在手工重启服务"


def test_root_user_also_disables_disable_root() -> None:
    """`disable_root: false` 要一起给。

    默认 `true` 时，cloud-init 会给 root 的 `authorized_keys` 套上
    `command="echo 'Please login as the user "ubuntu" rather than the user "root"'"`
    （`ssh_util.DISABLE_USER_OPTS`）—— root 连**密钥**都登不进去。
    """
    data = _parse(build_cloud_init(username="root", password="pw"))
    assert data.get("disable_root") is False, (
        f"disable_root 应该是 False，实际 {data.get('disable_root')!r}"
    )


def test_root_without_password_still_disables_disable_root_but_opens_no_port() -> None:
    """root 但**没给密码**：只关 `disable_root`，不写 drop-in。

    铁律 3 的精神 —— 没密码就别开密码认证。`prohibit-password` 本来就允许
    密钥登录 root，不需要 `PermitRootLogin yes`。
    """
    text = build_cloud_init(username="root")
    data = _parse(text)
    assert data.get("disable_root") is False
    assert _root_login_dropin(text) is None, "没给密码却把 root 密码登录的口子开了"
    assert "ssh_pwauth" not in data, "没给密码却打开了 PasswordAuthentication"


def test_non_root_user_never_touches_root_login() -> None:
    """对照：普通用户名**不许**出现 `disable_root` / `write_files`。

    这条是防止「修 root 的时候顺手把所有人都放开了」。
    """
    for user in ("ubuntu", "admin", "deploy"):
        text = build_cloud_init(username=user, password="pw")
        data = _parse(text)
        assert "disable_root" not in data, f"{user}：不该出现 disable_root"
        assert "write_files" not in data, f"{user}：不该出现 write_files"
        assert _root_login_dropin(text) is None, f"{user}：不该写 root 的 drop-in"


def test_dropin_sorts_before_cloud_inits_own_dropins() -> None:
    """文件名必须排在 `50-cloud-init.conf` / `60-cloudimg-settings.conf` 前面。

    ⚠️ 这条**不是**「不改就立刻坏」—— 2026-09-20 在同一镜像上实测过：
       `PermitRootLogin` 目前**没有任何别人设**，所以写成 `99-` 也照样生效。
       但 `PasswordAuthentication` 有竞争者（50- 说 yes、60- 说 no），
       写 `99-` 会被**静默忽略**（`sshd -T` 仍报 yes）。
       → 用 `00-` 是不依赖「恰好没人跟我抢」这个巧合。
       机制：OpenSSH 对同一关键字只取**第一个**出现的值，
       drop-in 按文件名字典序加载。
    """
    import posixpath

    base = posixpath.basename(_ROOT_LOGIN_DROPIN_LITERAL)
    assert base.startswith("00-"), f"前缀不是 00-：{base}"
    competitors = ["50-cloud-init.conf", "60-cloudimg-settings.conf"]
    for other in competitors:
        assert base < other, f"{base} 排在 {other} 后面 —— 关键字冲突时会被吃掉"


def test_root_login_dropin_literal_matches_the_shipped_constant() -> None:
    """测试里写死的路径必须和代码里的常量一致。

    写死是为了让「改文件名」变成有意识的动作；这条则是防止两边**悄悄漂移**
    （本项目踩坑 #29：同一个值有两处实现，必然漂移）。
    """
    from oracles import cloudinit

    assert cloudinit._ROOT_LOGIN_DROPIN == _ROOT_LOGIN_DROPIN_LITERAL


# --- 防「假通过」：检测器必须能说出「没有」 ---------------------------------
def test_the_detector_can_actually_say_no() -> None:
    """**变异测试**：拿一份「修复前」的配置喂给检测器，它必须返回 None。

    没有这一条，「检测器永远返回 None」和「配置真的没写」就区分不开
    （踩坑 #47：检查在跑，但结构上不可能给出「否」）。
    这份 `buggy` 是**手写**的，不是从被测代码生成的 —— 否则就是自证。
    """
    buggy = "\n".join([
        "#cloud-config",
        "hostname: h",
        "ssh_pwauth: true",
        "users:",
        "- default",
        "- name: root",
        "  shell: /bin/bash",
        "chpasswd:",
        "  expire: false",
        "  users:",
        "  - name: root",
        "    password: 'pw'",
        "    type: text",
    ])
    assert _root_login_dropin(buggy) is None, "检测器把「没写 drop-in」认成了「写了」"

    # 反向：只在注释里提到 PermitRootLogin，检测器也不该被骗
    commented = "# 注意：这里本该有 PermitRootLogin yes，但被删了\n" + buggy
    assert _root_login_dropin(commented) is None, "检测器被注释骗了"


def test_the_detector_finds_a_planted_dropin() -> None:
    """反向变异：手工塞一份正确的 drop-in，检测器必须找得到。

    和上一条合起来，「找得到」与「说不出没有」两个方向都验过，
    这个检测器才算可信。
    """
    planted = "\n".join([
        "#cloud-config",
        "write_files:",
        f"  - path: {_ROOT_LOGIN_DROPIN_LITERAL}",
        "    permissions: '0644'",
        "    content: |",
        "      PermitRootLogin yes",
    ])
    entry = _root_login_dropin(planted)
    assert entry is not None
    assert "PermitRootLogin yes" in entry["content"]
