"""cloud-init 配置生成。

用途：开机向导里用户选了「设置用户名 / 密码」时，把凭据写进实例的
启动脚本，机器第一次起来就有一个可用的本地账号。

**安全边界**（和 compute.py 的注释一致）：
  · 只放**用户自己输入**的用户名/密码；绝不把 API 私钥、SSH 密钥写进去。
  · cloud-init 是明文 YAML，存放在实例 metadata 里 —— 因此这里生成的
    配置只做「建账号 + 设密码」这类一次性动作，不做持久化后门。
  · ⚠️ 正因为它是 base64 而不是加密，密码等于公开，且每次开新机都会重放一遍。
    这是「用户明知并主动选择」的行为，不是默认路径（默认路径是 SSH 公钥）。

OCI 对 user_data 的要求：base64 编码后 ≤ 16 KB（32 KB 硬上限的一半），
我们的模板远小于此。

---

🔴 **三条铁律**（2026-09-20 事故换来的，改这个文件前必读）

事故现象：向导里选「用户名+密码」开出来的机器，**密钥和密码两条路都进不去**。

1. **`users:` 列表必须含 `- default`。**
   cloud-init 的语义是：`users:` 一旦出现就**替换**默认用户列表，不是追加。
   漏掉它 → 默认用户（ubuntu）不会被创建 → OCI metadata 里的
   `ssh_authorized_keys` 没有落点 → 被兜到 `root` → 再被 `disable_root`
   包上强制命令（就是那句 `Please login as the user "NONE"`）→
   密钥「认证能过、但登不进去」。

2. **只写 `plain_text_passwd` 不足以用密码 SSH 登录。**
   Ubuntu 云镜像的 `/etc/ssh/sshd_config.d/60-cloudimg-settings.conf` 里写着
   `PasswordAuthentication no`，必须 `ssh_pwauth: true` 才覆盖得掉。
   没有它，向导页承诺的「用户名+密码，首启即用」就是一句空话。
   （手工改 drop-in 时还有一层坑：OpenSSH 对同一关键字只取**第一个**出现的值，
   而 drop-in 按**文件名字典序**加载 —— 所以前缀必须小于 60，
   `99-xxx.conf` 会被 `60-cloudimg-settings.conf` 静默吃掉。）

3. **`ssh_pwauth: true` 只在真给了密码时才发。**
   没设密码却打开密码认证，等于凭空多一个攻击面。

4. **用户名不能撞系统预置的「组」名（`admin`、`sudo`、`staff` …）。**
   2026-09-20 的 4 台机器里，有 1 台用的用户名是 `admin`，而 Ubuntu 26.04
   的 `/etc/group` 里**预置了 `admin:x:107:`**。`useradd` 在默认的
   `USERGROUPS_ENAB=yes` 下会连同名组一起建，撞车就 `exit 9`：
   `useradd: group admin exists - if you want to add this user to that group, use -g.`
   后果不是「这个用户没建成」这么轻 —— cloud-init 的 `users_groups` 模块
   **整个抛异常 FAIL**，它后面的 `ssh_authorized_keys` 写入、sudoers 配置
   **全部没执行**。铁律 1 那个「公钥落到 root 被包强制命令」的连锁反应，
   真正的第一张多米诺骨牌就是这里。
   对策不是拒绝用户（他想要这个名字），而是显式 `primary_group` 复用已有的组。

5. **🔴 别用 `runcmd` 自己写「开密码登录」那套 —— 一定会踩服务名。**
   2026-09-20 之前的老格式是：

   ```yaml
   runcmd:
     - echo 'root:PASS' | chpasswd
     - printf 'PermitRootLogin yes\nPasswordAuthentication yes\n' \
         > /etc/ssh/sshd_config.d/10-root-login.conf
     - systemctl restart sshd || true
   ```

   最后一行是**静默失效**：Ubuntu 上 sshd 的服务名是 **`ssh`**，不是 `sshd`
   （`systemctl restart sshd` → `Unit sshd.service not found`），
   而 `|| true` 把它吞掉了。于是配置文件写好了、sshd **从没重载** →
   `PasswordAuthentication` 还是 `no` → **机器只能密钥登录**，
   而且日志里一句报错都没有。
   → 现在一律交给 cloud-init 的 `ssh_pwauth` 模块，它自己知道该重启哪个服务。

6. **密码走 `chpasswd:` 模块，并且必须 `expire: false`。**
   `chpasswd.expire` 默认是 **true**，会把密码设成「已过期」——
   用户首次登录被强制改密码。在脚本/自动化场景下看起来就跟
   「密码不对」一模一样。向导页承诺的是「首启即用」，所以要显式关掉。
   同时 `users:` 里加 `lock_passwd: false`（默认是 true，会把密码位写成 `!`）。

7. **顶层别写 `version` —— 它是个「什么都不做」的遗留键。**
   ⚠️ 注意别把理由说错：`version` **在 cloud-init 的 schema 里是被接受的**
   （`schema-cloud-config-v1.json` 里就是 `"version": {}` —— 空 schema，
   等于不校验），所以写 `version: v1` 甚至 `version: v999zzz`
   **都不会报错、也没有警告**。2026-09-20 用
   `cloud-init schema --config-file` 实测确认过。
   删掉它的理由是**去掉误导**：它不产生任何效果（没有任何模块读它），
   留着会让人误以为它和 `#cloud-config` 头一起构成某种「版本声明」。
   （对照组很关键：同一台机器上，`totally_unknown_module: ...` 会被
   `Invalid user-data` 判死，说明校验器有鉴别力，不是「什么都放行」。）

8. **🔴 用户名填 `root` 时，光有 `ssh_pwauth` 还是登不进去 —— 必须自己写
   `PermitRootLogin yes`。**

   2026-09-20 现场（`<实例公网IP>` / `oracles-15-<时间戳>`）：
   向导里选了「用户名 root + 密码」，机器开出来后

     · **密码确实是设上了的** —— 走串口控制台 `root` + 密码能登进去
       （串口绕过 sshd，`PermitRootLogin` 管不到它）
     · 但 SSH 死活登不进，`ssh -v` 只说 `Permission denied (publickey,password)`

   根因：`sshd -T` 给出 `permitrootlogin prohibit-password`。
   这个值**不是 cloud-init 写的**，是 **OpenSSH 的编译默认值**
   （`/etc/ssh/sshd_config:54` 那行 `#PermitRootLogin prohibit-password`
   是**注释掉的**）。查过 cloud-init 源码（`cc_ssh.py:257 apply_credentials`）：
   `disable_root` 这个键**只**决定往 root 的 `authorized_keys` 里塞不塞
   强制命令前缀（`ssh_util.DISABLE_USER_OPTS`，就是那句
   `Please login as the user "ubuntu" rather than the user "root"`），
   它**从头到尾没有写过 `PermitRootLogin`**。

   而 `ssh_pwauth: true` 也只改 `PasswordAuthentication`
   （`cc_set_passwords.py:70 cfg_name = "PasswordAuthentication"`）。
   → **没有任何一个 cloud-init 键能打开 root 的密码登录。** 必须自己写 drop-in。

   落点与时机（两处都是实测确认的，不是推测）：

   · 用 `write_files` 写 `/etc/ssh/sshd_config.d/00-oracles-root-login.conf`。
     `write_files` 是 `cloud_init_modules` 的第 4 个，`set_passwords` 是第 16 个，
     而 `cc_set_passwords.py:104` 的注释明确写着
     「This module runs **Before=sshd.service**」——
     也就是说 **sshd 首次启动时这份 drop-in 已经在盘上了**，不需要额外重启。
   · 文件名前缀用 `00-`，这是**防御性**选择而不是硬要求 ——
     2026-09-20 在同一镜像上做了对照实验，结论比「想当然」细一档：

       | 关键字 | 有别人也设吗 | 写 `99-` |
       | --- | --- | --- |
       | `PasswordAuthentication` | 有（50- 说 yes、60- 说 no） | 🔴 **被静默忽略** |
       | `PermitRootLogin` | **没有** | ✅ 照样生效 |

     机制是「OpenSSH 对同一关键字只取第一个出现的值 + drop-in 按文件名字典序」，
     但它只在**存在竞争者**时才咬人。所以 `99-` 今天是「靠巧合跑通」的，
     `00-` 才不依赖巧合。（差一点就把「99- 会被吃掉」当成通用规律写进注释。）

   ⚠️ 顺带记一条**教训**（比这个 bug 本身更值钱）：
   这次是「走串口能登、走 SSH 不能登」才把范围一下缩到 `PermitRootLogin` 的。
   如果当时只测 SSH，就会去怀疑密码、怀疑 `chpasswd`、怀疑 `expire`——
   三个都是错的。**两条独立通道对照，比在一条通道上反复试错快得多。**

配套回归测试：`tests/test_cloudinit.py`。
"""
from __future__ import annotations

import base64

#: `users:` 列表里的保留标记，不能当用户名（会生成两条 default 条目）。
_RESERVED_USER_NAMES = frozenset({"default"})

#: 系统预置的**组**名（Ubuntu/Debian base-passwd 及各服务安装脚本建立的）。
#:
#: ⚠️ 铁律 4：拿这些名字当用户名，`useradd` 会因为「同名组已存在」而 exit 9
#: （`USERGROUPS_ENAB=yes` 时会连组一起建），进而让 cloud-init 的
#: `users_groups` 模块整体 FAIL。2026-09-20 的 `admin` 事故就是这个。
#: 处理方式是显式 `primary_group` 复用已有的组，而不是拒绝用户。
#:
#: 这份清单取自 Ubuntu 26.04 云镜像的 `/etc/group` 实测结果。名字被拆成
#: 多行只是为了读起来方便，集合本身是扁平的。
_SYSTEM_GROUP_NAMES = frozenset({
    # base-passwd 的基础组（同时也是用户，cloud-init 走更新路径，不会 useradd，
    # 但一并列进来更省心 —— 对已存在用户 primary_group 会被忽略，无副作用）
    "root", "daemon", "bin", "sys", "adm", "tty", "disk", "lp", "mail",
    "news", "uucp", "man", "proxy", "kmem", "dialout", "fax", "voice",
    "cdrom", "floppy", "tape", "sudo", "audio", "dip", "operator", "list",
    "irc", "src", "shadow", "utmp", "video", "sasl", "plugdev", "staff",
    "games", "users", "nogroup",
    # 各服务/组件建立的组
    "www-data", "backup", "crontab", "dhcpcd", "messagebus", "syslog",
    "input", "sgx", "clock", "kvm", "render", "lxd", "uuidd", "rdma",
    "tcpdump", "landscape", "polkitd", "fwupd-refresh", "tss", "netdev",
    "systemd-journal", "systemd-network", "systemd-resolve",
    # ⚠️ 这个就是本次事故的元凶，单列出来提醒：Ubuntu 26.04 里 admin:x:107
    "admin",
    # snap 相关（含下划线，_safe_name 是允许的）
    "snap_daemon", "_chrony", "_ssh", "_apt",
})

#: 「让 root 能用密码登录」的 sshd drop-in 路径。
#:
#: ⚠️ 铁律 8：`ssh_pwauth` 只改 `PasswordAuthentication`，
#:    cloud-init **没有任何键**能改 `PermitRootLogin`（那是 OpenSSH 的编译默认值
#:    `prohibit-password`）。用户名是 root 时必须自己写这份文件。
#:
#: ⚠️ 前缀 `00-` 是**防御性**选择，理由是 2026-09-20 实测出来的（同一镜像上跑对照）：
#:
#:    · OpenSSH 对同一关键字**只取第一个**出现的值，drop-in 按**文件名字典序**加载。
#:    · 但「先到先得」只在**有别人也设这个关键字**时才咬人：
#:        - `PasswordAuthentication` 同时被 `50-cloud-init.conf`(yes) 和
#:          `60-cloudimg-settings.conf`(no) 设置 → 我们的 `no` 写成 `99-`
#:          会**被静默忽略**（sshd -T 仍报 yes），写成 `00-` 才生效。
#:        - `PermitRootLogin` 目前**没有任何别人设** → 写成 `99-` 也照样生效。
#:    → 所以 `99-` 是「今天能跑通，但依赖『没有竞争者』这个巧合」。
#:      用 `00-` 不依赖巧合：哪天 cloud-init 或某个加固脚本也去写
#:      `PermitRootLogin`，我们依然是赢的那个。
#:
#:    （差点就把「99- 会被吃掉」当成通用规律写进注释了 —— 对照实验里
#:      第 ② 组 `permitrootlogin yes` 打了我自己的脸。**别把
#:      「某个关键字上的观察」推广成「所有关键字都这样」。**）
_ROOT_LOGIN_DROPIN = "/etc/ssh/sshd_config.d/00-oracles-root-login.conf"


#: 允许的用户名字符集（防 cloud-config YAML 注入）
def _safe_name(name: str) -> str:
    name = name.strip()
    if not (1 <= len(name) <= 32):
        raise ValueError(f"用户名长度必须在 1~32，收到 {name!r}")
    if not all(c.isalnum() or c in "._-" for c in name):
        raise ValueError(f"用户名只能含字母、数字和 . _ - ：{name!r}")
    return name


def build_cloud_init(*, hostname: str | None = None,
                     username: str | None = None,
                     password: str | None = None) -> str:
    """生成 cloud-config 文本。

    ``username`` / ``password`` 都可选：
      · 只给用户名 → 建一个账号（配合 metadata 的 SSH 公钥用）
      · 都给       → 建账号、设登录密码，并打开 SSH 密码认证
      · 都不给     → 返回空串，调用方据此跳过 user_data
    """
    if not username and password:
        raise ValueError("给了密码就必须同时给用户名")

    # ⚠️ 顶层**不写 `version`**。
    #    它是个遗留键：cloud-init 的 schema 里就是 `"version": {}`（空 schema，
    #    等于不校验），所以写 `version: v1` 甚至瞎写的值都**不报错、无警告**
    #    —— 2026-09-20 用 `cloud-init schema --config-file` 实测确认。
    #    删它的理由是**去掉误导**（它没有任何效果，却让人以为它是版本声明），
    #    不是「会报 unknown key」。别把理由记错。
    lines = ["#cloud-config"]
    if hostname:
        safe_host = "".join(c for c in hostname[:64] if c.isalnum() or c == "-").strip("-") or "oracles"
        lines.append(f"hostname: {safe_host}")

    # 用户名先校验一次，后面几处（root 判定、users: 块、chpasswd:）都用这一份。
    name = _safe_name(username) if username else None

    # ⚠️ 铁律 2 + 3：只设密码是登不进去的，必须 ssh_pwauth；
    #    但也只在真给了密码时才开（没密码还开密码认证 = 白送一个攻击面）。
    if password:
        lines.append("ssh_pwauth: true")

    if name == "root":
        # ⚠️ 铁律 8：`ssh_pwauth` **改不到 root**。
        #    cloud-init 里没有任何一个键会去写 `PermitRootLogin` ——
        #    `disable_root` 只决定往 root 的 authorized_keys 塞不塞强制命令前缀
        #    （见 cc_ssh.py 的 apply_credentials），`ssh_pwauth` 只改
        #    `PasswordAuthentication`。`permitrootlogin prohibit-password`
        #    是 OpenSSH 的**编译默认值**，所以必须自己写 drop-in 覆盖它。
        #
        #    `disable_root: false` 单独一条也要给：默认 true 时 root 的
        #    authorized_keys 会被套上 `command="echo 'Please login as the user
        #    ...'"`，root 连**密钥**登录都进不去。
        lines.append("disable_root: false")
        if password:
            # 只在真要「root + 密码」时才开这个口子（铁律 3 的精神：
            # 没密码就别扩攻击面 —— 只给密钥时 prohibit-password 本来就够用）。
            #
            # 用 `write_files` 而不是 `runcmd`（铁律 5）：write_files 是
            # cloud_init_modules 第 4 个，set_passwords 是第 16 个，而后者
            # 明确 `Before=sshd.service` —— sshd 首次启动时这份文件已经在盘上了。
            lines += [
                "write_files:",
                f"  - path: {_ROOT_LOGIN_DROPIN}",
                "    permissions: '0644'",
                "    owner: 'root:root'",
                "    content: |",
                "      PermitRootLogin yes",
            ]

    if name:
        if name in _RESERVED_USER_NAMES:
            # `default` 是列表标记不是用户名；照抄会生成两条 default 条目。
            raise ValueError(
                f"{name!r} 是 cloud-init `users:` 列表的保留标记，不能当用户名"
            )
        block = [
            "users:",
            # ⚠️ 铁律 1：这一行不能删。cloud-init 的 users: 是「替换」语义，
            #    漏掉 default 就不会创建 ubuntu 用户，metadata 里的
            #    ssh_authorized_keys 便无处可落，机器会彻底登不进去。
            #    详见模块 docstring 的事故记录。
            "- default",
            f"- name: {name}",
            "  shell: /bin/bash",
        ]
        if name in _SYSTEM_GROUP_NAMES:
            # ⚠️ 铁律 4：这个名字同时是系统预置的**组**名。useradd 在
            #    USERGROUPS_ENAB=yes 下会连组一起建，撞上已有的组就 exit 9，
            #    并让 cloud-init 的 users_groups 模块整体 FAIL —— 后面
            #    写 ssh_authorized_keys、配 sudoers 的步骤统统不会执行。
            #    显式指定主组复用已有的那个组，useradd 就加 `-g <组>`，不再建组。
            block.append(f"  primary_group: {name}")
        block.append("  sudo: ALL=(ALL) NOPASSWD:ALL")   # 免费机就一个用户，给 root 便利
        if password:
            # 不锁密码。cloud-init 的 `lock_passwd` 默认是 **true**，
            # 会把密码位写成 `!` —— 那样后面 chpasswd 设的密码能不能用，
            # 就取决于模块先后顺序了。显式 false 把这件事钉死。
            block.append("  lock_passwd: false")
        lines.extend(block)

        if password:
            # ⚠️ 密码走**官方的 `chpasswd` 模块**，不用 `users:` 里的
            #    `plain_text_passwd`。区别在 `expire`：
            #    `chpasswd.expire` 默认是 **true**，那会把密码设成「已过期」，
            #    用户第一次登录会被强制改密码 —— 在只有密码没有键盘交互的
            #    场景下，看起来就跟「密码不对/登不进去」一模一样。
            #    这里显式 `expire: false`，才是向导页承诺的「首启即用」。
            #    这也是 cloud-init 官方文档里设置密码的写法。
            # YAML：密码里可能有特殊字符 → 单引号包裹并转义内部单引号
            escaped = password.replace("'", "''")
            lines += [
                "chpasswd:",
                "  expire: false",
                "  users:",
                f"  - name: {name}",
                f"    password: '{escaped}'",
                "    type: text",
            ]

    if not username and not hostname:
        return ""   # 没有任何需要注入的内容，别塞空模板
    return "\n".join(lines) + "\n"


def to_user_data(config_text: str) -> str | None:
    """cloud-config → OCI user_data（base64）。空配置返回 None。"""
    if not config_text.strip():
        return None
    raw = base64.b64encode(config_text.encode("utf-8"))
    if len(raw) > 16 * 1024:
        raise ValueError(f"user_data 超过 16 KB（{len(raw)} B），请简化配置")
    return raw.decode("ascii")
