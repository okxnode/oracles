# 部署到 VPS

## 前置要求

| 项 | 要求 | 怎么确认 |
| --- | --- | --- |
| 系统 | Ubuntu 22.04+ / Debian 12+（或任何有 systemd 的发行版） | `systemctl --version` |
| Python | 3.10+ | `python3 -V` |
| **`python3-venv`** | **必须装，否则第 2 步建不了 venv** | `python3 -c 'import ensurepip'` 不报错 |
| 权限 | root（`sudo`） | — |
| 网络 | 能访问 pypi.org 和 api.telegram.org | `curl -sI https://pypi.org \| head -1` |
| 磁盘 | 约 500 MB（venv + 依赖） | `df -h /` |

装 `python3-venv` 时注意**包名带版本号**，而且云镜像首次启动后
`apt` 索引往往是空的（不先 `update` 会一直报 `no installation candidate`）：

```bash
sudo apt-get update          # ← 不能省
sudo apt-get install -y python3-venv python3.14-venv   # 版本号换成你系统的
```

> `install.sh` 会**在动任何东西之前**先检查 `ensurepip` 是否可用，
> 缺了就直接停下并打印上面这两条命令。所以就算忘了装，也不会留下半装状态。
> 详见踩坑清单第 34 条。

## 一键部署

```bash
git clone https://github.com/okxnode/oracles.git
cd oracles
sudo ./deploy/install.sh
```

脚本做的事：

```
0. 先扫一遍源码里有没有密钥（有就中止，避免把密钥复制进 /opt/oracles）
1. 建 oracles 系统用户（nologin，不能登录）
2. rsync 代码到 /opt/oracles（排除 .env / accounts.json / *.pem）
3. 建 venv，pip install -e .
4. 建 /etc/oracles（700）+ keys/ + backups/，从示例生成配置文件（600）
5. 装 systemd unit；凭据已配好才 enable --now，否则只装不启
6. 跑一次 doctor 自检
```

**幂等** —— 重复执行只更新代码和依赖，不覆盖已有配置。

> **第 5 步为什么要分情况**：unit 是 `Restart=always`，而 Token 为空时
> `python -m oracles` 会立刻抛错退出 → 每 10 秒崩溃重启一次刷爆 journal，
> 并且脚本会在 `is-active` 检查上报「服务没起来」然后 `exit 1`，
> 让人误以为整个安装失败（其实全都就位了，只差凭据）。
> 所以脚本先校验凭据**形态**（不是判非空 —— `oracles.env.example` 里的
> 占位文字本身就不为空），通过才启动。详见踩坑清单第 33 条。

装完会提示你改哪两个文件。**如果装的时候凭据还没填**，改完要手动拉起来：

```bash
sudo nano /etc/oracles/accounts.json   # 账号清单
sudo nano /etc/oracles/oracles.env     # Bot Token + 白名单
sudo systemctl enable --now oracles-bot
```

---

## 手动部署

如果你想完全掌控每一步：

```bash
# 1. 用户与目录
sudo useradd --system --shell /usr/sbin/nologin --home-dir /opt/oracles oracles
sudo mkdir -p /opt/oracles
sudo rsync -a --exclude '.git' --exclude '.venv' --exclude '.env' \
     --exclude 'accounts.json' --exclude '*.pem' \
     ./ /opt/oracles/

# 2. 虚拟环境
cd /opt/oracles
sudo python3 -m venv .venv
sudo .venv/bin/pip install --upgrade pip
sudo .venv/bin/pip install -e .

# 3. 配置目录
sudo mkdir -p /etc/oracles/keys /etc/oracles/backups
sudo chmod 700 /etc/oracles /etc/oracles/keys
sudo install -m 600 accounts.example.json /etc/oracles/accounts.json
sudo install -m 600 deploy/oracles.env.example /etc/oracles/oracles.env
sudo chown -R oracles:oracles /etc/oracles /opt/oracles

# 4. 填配置（见 02-账号与密钥配置.md）
sudo nano /etc/oracles/accounts.json
sudo nano /etc/oracles/oracles.env

# 5. systemd
sudo install -m 644 deploy/oracles-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now oracles-bot
```

---

## systemd 单元说明

`deploy/oracles-bot.service` 的关键部分：

```ini
[Service]
User=oracles
WorkingDirectory=/opt/oracles
EnvironmentFile=/etc/oracles/oracles.env      # 所有配置和开关在这
ExecStart=/opt/oracles/.venv/bin/python -m oracles

Restart=always
RestartSec=10

# ---- 权限加固 ----
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict          # 整个文件系统只读
ProtectHome=read-only
ReadWritePaths=/etc/oracles   # 只有配置目录可写（审计备份要写这）
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
```

这个进程**拿着你所有 OCI 账号的私钥**，所以按高价值目标对待：
即使 Bot 被攻破，攻击者也只能读写 `/etc/oracles`，动不了系统其它部分。

> 注意没有加 `MemoryDenyWriteExecute` —— 某些 Python 扩展需要可写可执行内存，
> 加了会起不来。

---

## 常用运维

```bash
# 状态
systemctl status oracles-bot

# 实时日志
journalctl -u oracles-bot -f

# 最近 100 行
journalctl -u oracles-bot -n 100 --no-pager

# 只看错误
journalctl -u oracles-bot -p err --no-pager

# 重启（改了 oracles.env 或 accounts.json 后必须重启）
sudo systemctl restart oracles-bot

# 停
sudo systemctl stop oracles-bot
```

---

## 升级

```bash
cd /path/to/oracles
git pull
sudo ./deploy/install.sh      # 幂等，会同步代码并重启
```

或者手动：

```bash
cd /opt/oracles
sudo -u oracles git pull
sudo -u oracles .venv/bin/pip install -e .
sudo systemctl restart oracles-bot
```

升级后跑一次自检确认没坏：

```bash
sudo -u oracles ORACLES_HOME=/etc/oracles \
  /opt/oracles/.venv/bin/python scripts/selftest.py
```

---

## 故障排查

### 服务起不来

```bash
systemctl status oracles-bot
journalctl -u oracles-bot -n 50 --no-pager
```

常见原因：

| 日志里的报错 | 原因 |
| --- | --- |
| `缺少 TELEGRAM_BOT_TOKEN` | `oracles.env` 没填，或 `EnvironmentFile` 路径不对 |
| `账号清单不存在` | `accounts.json` 没放对位置，或 `ORACLES_HOME` 指错 |
| `私钥文件不存在` | `key_file` 路径不对 |
| `Permission denied: ...accounts.json` | 文件属主/权限不对，`chown oracles:oracles` |
| `Address already in use` | 有另一个实例在跑，`systemctl stop` 后重启 |

### Bot 不回消息

按顺序排查：

1. **你的用户 ID 在白名单里吗？**
   ```bash
   grep TELEGRAM_ALLOWED_USER_IDS /etc/oracles/oracles.env
   ```
   不在的话 Bot 会**静默忽略**你的消息（日志里会有 `拒绝未授权访问`）。

2. **Token 对吗？** 直接测：
   ```bash
   curl -s "https://api.telegram.org/bot<你的TOKEN>/getMe"
   ```
   返回 `{"ok":true,...}` 说明 Token 有效。

3. **服务器能访问 Telegram 吗？**
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" https://api.telegram.org
   ```
   国内服务器可能连不上 `api.telegram.org`，需要代理。

4. **看日志有没有报错：**
   ```bash
   journalctl -u oracles-bot -f
   ```

### 某个账号查询失败

```bash
sudo -u oracles ORACLES_HOME=/etc/oracles \
  /opt/oracles/.venv/bin/python -m oracles.cli doctor
```

会逐个账号报出具体原因。最常见的是 `NotAuthenticated`（私钥/fingerprint 不对）
和 `NotAuthorizedOrNotFound`（tenancy 抄错）。

### 开机报 `LimitExceeded`

**先别怀疑代码**，看是不是这两种情况：

1. **配额绑在别的 AD 上** —— 用 `/q <账号>` 看逐 AD 明细。
   正常流程会自动挑有额度的 AD，但如果你手动指定了 AD 就会撞上。
2. **块存储满了** —— 报 `bootVolumeQuota` 时问题在块存储不在 CPU。
   用 `/audit` 找孤儿引导卷。

### 开机报 `OutOfHostCapacity`

这是**物理主机容量不足**，不是配额问题。换个 AD 或过一会儿重试。
所有 AD 都报这个就是真的没机器了。

### 服务重启后按钮点了没反应

**这是设计如此。** 所有待确认操作和短令牌都存在内存里，重启即失效。
重启前的「确认删除」不该在重启后还能生效。

重新走一遍流程即可。

---

## 安全建议

### 1. 别把 Bot 暴露在公网

Telegram Bot 用**长轮询**（Bot 主动连 Telegram），所以：

- **不需要**开任何入站端口
- **不需要**公网 IP
- **不需要**配 Webhook

所以 VPS 的安全组可以只留 SSH，甚至 SSH 也收紧到你的固定 IP。

### 2. 收紧 SSH

```bash
# 先看哪些地方开着 22 端口
python -m oracles.cli security

# 收紧到你的固定 IP（一次一个账号）
/harden 15 203.0.113.7/32
```

### 3. 只读模式跑一段时间

出厂是 `ORACLES_WRITE_ENABLED=false`。建议先这样用几天，
点各种按钮看**计划内容**对不对，确认无误再打开写权限。

### 4. 危险开关随用随开

```bash
# 需要删机器时
sudo sed -i 's/^ORACLES_ALLOW_DESTRUCTIVE=.*/ORACLES_ALLOW_DESTRUCTIVE=true/' \
     /etc/oracles/oracles.env
sudo systemctl restart oracles-bot

# 做完立刻关掉
sudo sed -i 's/^ORACLES_ALLOW_DESTRUCTIVE=.*/ORACLES_ALLOW_DESTRUCTIVE=false/' \
     /etc/oracles/oracles.env
sudo systemctl restart oracles-bot
```

### 5. 用独立账号跑 Bot

别用 root。`install.sh` 建的 `oracles` 用户是 `nologin` 的系统用户，
只能通过 systemd 启动进程。

### 6. 备份配置

```bash
sudo tar czf oracles-config-$(date +%F).tar.gz -C /etc oracles
```

⚠️ 这个包里**有私钥**。存到加密的地方，别丢到网盘。

---

## 资源占用

Bot 本身几乎不耗资源：

| 项 | 实测 |
| --- | --- |
| 内存 | ~60 MB（Python + 依赖） |
| CPU | 空闲时接近 0 |
| 磁盘 | ~150 MB（venv） |
| 网络 | 只在操作时发 API 请求 |

1 核 1G 的免费实例跑起来毫无压力。17 个账号跑一次全量配额核查约 50 秒，
一次安全扫描约 45 秒。
