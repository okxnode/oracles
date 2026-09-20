# oracles

**用一个 Telegram Bot 管理你所有的 Oracle Cloud (OCI) 账号。**

全部通过 **Oracle 官方 Python SDK**（`oci` 包）实现，不依赖 `oci` CLI、不 shell out、不解析命令行输出。

**语言 / Language：** [中文](README.md) · [English](README.en.md)

```
                    ┌──────────────────────────────┐
   Telegram  ──────▶│   oracles-bot (systemd)      │
   （按钮 + 命令）    │                              │
                    │  bot/     交互层 · 二次确认   │
                    │  services/ 业务逻辑           │
                    │  oci_gateway  官方 SDK 网关   │
                    └──────────────┬───────────────┘
                                   │  oci Python SDK
                    ┌──────────────▼───────────────┐
                    │  账号 1  │ 账号 2 │ … │ 账号 N │
                    │  Compute · Block Volume ·    │
                    │  Object Storage · Network ·  │
                    │  Limits · Identity           │
                    └──────────────────────────────┘
```

---

## 它能做什么

`/start` 直接给出**七大功能入口**（按钮式交互为主，命令行只是快捷方式）：

| # | 能力 | 说明 |
| --- | --- | --- |
| 1 | ⚙️ **配置管理** | API / 密钥的增删查：账号清单增删改（**删/改前自动快照到 `backups/`**，误删可回滚）、逐账号凭据体检（私钥文件 / OCID / fingerprint，可选「联网体检」真调一次 API）、添加后热重载无需重启 |
| 2 | 🚀 **开机 / 自动抢机** | 分步向导：规格（E2 Micro 免费档 / A1.Flex 自定义核数内存）→ 系统 → 磁盘大小 → 台数 → 登录方式（**SSH 公钥**〔用你配置的 / 或让 Bot 生成一对并保存到服务器、随时重下〕 或 **用户名+密码 cloud-init 注入**）→ IPv6。最后二选一：**「现在开机」**（每台试一次，某台失败就收尾汇报）；**「自动抢机 5/10/20/30 秒」**（每个间隔独占一行，秒数完整可见）后台循环重试直到抢到 N 台，随时手动停。两者都会试着开满你选的台数，区别只在**失败之后怎么办** |
| 3 | 🖥 **实例管理** | 列表（含公网 IP / 状态）、开机、关机、重启、销毁、🔑 **下载登录密钥**（下载前会读实例 metadata 里的公钥**核对指纹**，不匹配就明说「这把钥匙开不了这台机器」），以及 🚑 **救援**——开串口控制台 + VNC 隧道，SSH 不通时直接看屏幕敲 shell（免费账号只能开一条，重复点会顶掉旧的） |
| 4 | 📊 **配额查询** | 全账号总览或逐账号明细：E2/A1 余量、块存储额度、「还能开几台」（逐可用域求和，见下方「最大的坑」） |
| 5 | 💾 **硬盘管理** | 引导卷 + 数据卷列表（含挂载关系：🟢已挂载 / ⚪未挂载 / ❓状态没查到）、**扩容**、删未挂载卷。删除按钮**只在「确认没挂」时出现**——已挂载的、以及挂载状态没查到的，都不给入口；点确认后还会**再实时复核一次**，挂了或查不到都直接拒绝。孤儿卷是块存储额度杀手，清理后额度立刻回来 |
| 6 | 🧵 **任务管理** | 自动抢机任务的进度看板：x/N 台、尝试次数、最后失败原因；一键停止运行中的任务 |
| 7 | 🪣 **存储桶管理** | 列桶、建桶、删桶（自动清理 PAR 阻塞）、浏览对象、签发 S3 兼容密钥 |

另有两条横切能力：🧹 **计费残留审计**（未绑定的公网 IP / 孤儿卷 / 镜像备份）
和 🛡 **安全暴露面**（22 端口对全网开放的规则 + `user_data` 明文密码，可一键收紧来源 IP）。

---

## 安全设计（开源项目的第一优先级）

这个 Bot 手里握着你**所有** OCI 账号的私钥，所以安全不是「附加功能」，而是架构的一部分。

### 1. 密钥从不出现在仓库里

```
仓库（会 push 到 GitHub）        配置目录（永不进仓库）
─────────────────────────       ──────────────────────────
accounts.example.json     ──▶   /etc/oracles/accounts.json   (600)
.env.example              ──▶   /etc/oracles/oracles.env     (600)
                                /etc/oracles/keys/*.pem       (600)
```

- 账号清单和私钥默认读 `$ORACLES_HOME`（默认 `~/.oracles`，生产用 `/etc/oracles`）——**在仓库目录之外**
- `.gitignore` 覆盖 `accounts.json` / `*.pem` / `.env` / `backups/`
- `scripts/check_secrets.sh` 做提交前扫描，可装成 git pre-commit 钩子
- `.github/workflows/secret-scan.yml` 在 CI 侧再拦一道

### 2. 写操作有两道独立开关

```bash
ORACLES_WRITE_ENABLED=false      # 关掉 → 所有写操作变成 DRY-RUN
ORACLES_ALLOW_DESTRUCTIVE=false  # 关掉 → 销毁/删除类操作单独再拦一层
```

出厂**默认全关**。改配置得先 SSH 上机器，所以即使 Telegram 账号被盗，攻击者也只能看，不能删。

### 3. 破坏性操作走「先出计划 → 人点确认」

所有写操作都是两段式：

```
用户点「开新机」
      ↓
plan_launch()  ← 只读：挑 AD、查配额、找镜像、验块存储
      ↓
展示完整计划（名称/规格/可用域/镜像/子网/引导卷/SSH 公钥 + 指纹）
      ↓
用户点「✅ 确认执行」
      ↓
execute_launch()  ← 才真正调 API
```

代码结构上就是分开的两个函数 —— DRY-RUN 不是「在写操作里加个 `if`」，
而是**结构上不可能误执行**。

### 4. 日志与消息全量脱敏

`oracles/redact.py` 会在日志离开进程前把 OCID、fingerprint、PEM 私钥、
Telegram Token、S3 密钥统统替换成占位符。审计报告里展示的疑似密码片段也先打码。

### 5. 白名单默认拒绝

`TELEGRAM_ALLOWED_USER_IDS` 留空时**不响应任何人**，而不是「没配就放开」。
一个能删你云主机的 Bot 裸奔在网上，被人搜到就是灾难。

---

## 最大的坑：免费额度是绑在**单个可用域**上的

这不是「OCI 的小怪癖」，而是会让所有「取第一个 AD 就开机」的实现永远失败的根因。

| 账号 | 区域 | AD-1 | AD-2 | AD-3 |
| --- | --- | --- | --- | --- |
| 账号 13 | us-ashburn-1 | 0 | 0 | **2** |
| 账号 17 | eu-frankfurt-1 | 0 | **2** | 0 |
| 账号 9 | us-ashburn-1 | **2（已用尽）** | 0 | 0 |

同一个区域内，**账号 9 和账号 13 的配额互不相干**；同一个账号内，
AD-1 有 2 核而 AD-2 是 0 也是常态。

**推论**：任何「取第一个 AD 就 launch」的脚本，对账号 13/17 会永远挑到 0 配额的 AD-1，
报错还会被误判成 `Out of host capacity`。

本项目正确做法：

```python
cap = account_capacity(client)              # 逐 AD 查 standard-e2-micro-core-count
ad  = pick_availability_domain(cap, shape=shape, cores=ocpus)   # 挑「可开台数最多」的 AD
plan = plan_launch(client, ad=ad)           # 再创建
```

容量公式（两个约束取小值）：

```
某 AD 可开台数 = min(该 AD 的 CPU 余量, floor(块存储余量 / 47))
账号可开总数   = Σ 各 AD 可开台数          ← 必须逐 AD 求和
```

> **E2 和 A1 是两套独立的限额**（`standard-e2-micro-core-count` /
> `standard-a1-core-count`），所以「哪个 AD 能开」是**跟规格绑定的**。
> 实测过的一个账号：E2 已 2/2 用满、**A1 还剩 2 核** ——
> 用 E2 的口径去挑 AD 会返回「没有任何可用域还有额度」，
> 让 A1 选项永远开不出机器。所以挑 AD 必须带上规格和每台的核数。
> （A1 还要**除以每台核数**，否则「剩 2 核」会被当成「能开 2 台 4 核机器」。）

> 还有一条相关的坑：`bootVolumeQuota Service limit reached` 报的是**块存储**额度满，
> 不是 CPU 额度满。免费账号每租户只有 200 GB 块存储（引导卷+数据卷共用），
> 池子满了就开不出机器，**哪怕 CPU 配额还有剩**。常见元凶是孤儿引导卷 ——
> 一块 150 GB 的残留卷就能吃掉 75% 额度。用 `/audit` 查。

---

## 快速开始

### 前置：在 OCI 控制台拿 API 凭据

每个账号都需要 4 个值（控制台 → 右上角头像 → **我的概要文件** → **API 密钥** → **添加 API 密钥**）：

| 字段 | 从哪来 |
| --- | --- |
| `user` | 用户 OCID |
| `fingerprint` | 添加 API 密钥后显示 |
| `tenancy` | 租户 OCID |
| `region` | 形如 `ap-singapore-1` |

下载的私钥文件（`*.pem`）**所有账号可以共用一把**。

### 一键安装

前置：Ubuntu 22.04+ / Debian 12+、Python 3.10+、root、能访问 pypi 和 api.telegram.org。
**别忘了 `python3-venv`**（Ubuntu 默认不带，且包名带版本号）：

```bash
sudo apt-get update
sudo apt-get install -y python3-venv python3.14-venv   # 版本号换成你系统的
```

```bash
git clone https://github.com/okxnode/oracles.git
cd oracles
sudo ./deploy/install.sh
```

`install.sh` 做六件事，**幂等**（重复执行只更新代码和依赖，不覆盖已有配置）：

| 步骤 | 做什么 | 为什么这么做 |
| --- | --- | --- |
| 0 | **先扫一遍源码里有没有密钥** | 下一步要把源码整个 rsync 到 `/opt/oracles`。要是源码里躺着 `si.pem`，这一下就把它复制进系统目录了 —— 所以**在复制之前**先拦 |
| 0.5 | **检查 `python3` 能不能建 venv** | Ubuntu 默认不带 `ensurepip`，报错又没告诉你「包名带版本号」+「先 `apt-get update`」。与其在第 2 步炸掉（那时用户和目录都建好了），不如现在就停 |
| 1 | 建 `oracles` 系统用户 + `/opt/oracles` | Bot 不用 root 跑 |
| 2 | 建 venv、装依赖 | 隔离，不污染系统 Python |
| 3 | 建 `/etc/oracles`（`700`，文件 `600`） | **密钥的家**，刻意放在仓库目录之外 |
| 4 | 装 systemd unit，**凭据齐了才启动** | 开机自启 + 崩溃重启。凭据没填时只装不启（见下方说明） |
| 5 | 跑一次 `doctor` | 确认每个账号鉴权都通，装完就知道能不能用 |

> **为什么凭据没填就不启动**：unit 是 `Restart=always` + `RestartSec=10`。
> Token 为空时进程会立刻抛错退出，systemd 每 10 秒拉一次 → 无限崩溃循环刷爆日志，
> 而且脚本会在"服务是否起来"的检查上报失败、`exit 1`，让人误以为整个安装挂了
> —— 其实代码、依赖、配置、unit 全都已就位，只差两行凭据。
> 所以脚本会先校验凭据**形态**（Token 要长得像 `<数字>:<base64ish>`、用户 ID 要纯数字），
> 通过才 `enable --now`，否则打印"只差两行凭据"就正常收尾。
> 注意这里不能用"非空"判断 —— `oracles.env.example` 里的占位文字本身就不为空。

装完填配置。**凭据已经填好**的话 `install.sh` 会直接启动，`doctor` 也就跟着跑了；
如果是先装后配，填完再手动拉起来：

```bash
sudo nano /etc/oracles/accounts.json    # 账号清单
sudo nano /etc/oracles/oracles.env      # Bot Token + 白名单
sudo systemctl enable --now oracles-bot
```

> 脚本里对 rsync 的排除规则有个兜底：同步完成后会检查
> `accounts.example.json` / `pyproject.toml` / unit 文件是否真的到位，
> 缺了就直接报错。因为排除规则很容易误伤 ——
> `--exclude 'accounts.*.json'` 会连 `accounts.example.json` 一起排掉。

### 本地跑（不装 systemd）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

cp accounts.example.json ~/.oracles/accounts.json   # 填真实凭据
cp .env.example .env                                 # 填 Bot Token
chmod 600 ~/.oracles/accounts.json

python -m oracles.cli doctor    # 先自检
python -m oracles              # 启动 Bot
```

### 先自检，再开写权限

```bash
$ python -m oracles.cli doctor
🩺 环境自检
  配置目录：/etc/oracles
  账号数量：17
  写操作：关闭(DRY-RUN)

  ✅ [1] us-sanjose-1             1 个可用域
  ✅ [2] us-sanjose-1             1 个可用域
  ...
结果：17 个正常，0 个失败
```

确认无误后，在 Telegram 里点几个按钮看计划内容，再打开 `ORACLES_WRITE_ENABLED=true`。

---

## 仓库里的脚本

都在 `scripts/` 和 `deploy/` 下，都可以单独跑。

| 脚本 | 用途 | 关键特性 |
| --- | --- | --- |
| `deploy/install.sh` | 一键部署到 VPS | 幂等；起飞前扫密钥 + 查 `ensurepip`；凭据齐了才启动；装完跑 `doctor` |
| `scripts/selftest.py` | **全流程只读演练** | 9 个环节，**绝不执行任何写操作** —— 只调 `plan_*` 打印计划，再验证写开关确实拦得住。断言数随账号数变化：17 账号约 40 项，3 账号 25 项 |
| `scripts/check_secrets.sh` | 提交前密钥扫描 | `--verify` 自检规则有效性；`--staged` 只扫暂存区；可装成 pre-commit 钩子。**CI 的主闸门** |
| `scripts/import_legacy_accounts.py` | 迁移旧号池配置 | 把「账号清单 + 共用 `si.pem`」转成本项目格式，自动设 600/700 权限 |
| `scripts/audit_open_source.py` | **开源前审计** | `--verify` 注入 12 类合成串自检；11 条凭据形态规则；已跟踪文件的形态检查；`.gitignore` 覆盖用 `git check-ignore` **实测**而不是读文本；文档里的真实标识（实例名 / 主机别名 / 公网 IP）。**CI 的发布面闸门** |
| `.gitleaks.toml` | gitleaks 的精确豁免配置 | CI 里的 gitleaks 是**补充网**（实测只覆盖 7 类密钥中的 1 类）；这份配置把它不认识的行内标记 `secret-scan:allow` 翻译过去 |

**改完代码先跑这三个**：

```bash
python scripts/selftest.py        # 在你的真实账号池上只读演练一遍（17 账号约 5 分钟）
pytest tests/                     # 622 个单元/集成测试（约 25 秒）
./scripts/check_secrets.sh        # 确认没把密钥带进去
python scripts/audit_open_source.py   # 确认没把「不该发布的东西」带进去
```

> ⚠️ **别把开源安全托付给第三方扫描器。** 实测注入 7 类本项目的真实密钥，
> `check_secrets.sh` 命中 **7/7**，而 gitleaks 只命中 **1/7**
> —— 它不认识 `ocid1.` 这种厂商前缀，也不认识 Telegram 的 `<数字>:<35位>` 形态。
> 详见 [踩坑清单第 35 条](docs/06-踩坑清单.md)。

### 关于 `check_secrets.sh --verify`

密钥扫描器有一个**容易被忽略的失败模式：它自己坏了但你看不出来**。
一条匹配不到任何东西的规则，和一条正常工作、只是没发现问题的规则，
在输出上完全一样。

本项目已经栽过两次：

- 正则里用了 `\b` —— macOS 的 BSD grep 在 ERE 模式下不支持，检查**一直静默失效**
- 白名单里放了 `aaaa` —— 而真实 OCID 的随机段都以 `aaaa` 开头，OCID 检查**形同虚设**

所以每条规则都必须能命中它自己的样本，白名单也不能把真实特征误伤：

```bash
$ ./scripts/check_secrets.sh --verify
🔬 规则自检（每条规则都必须能命中它该命中的东西）
  ✓ OCID      ✓ PEM私钥      ✓ API指纹      ✓ TelegramToken
  ✓ S3AccessKey      ✓ 私钥文件名      ✓ 疑似SecretKey
🔬 白名单自检（真实特征不能被白名单误伤）  ✓ × 7
🔬 跳过名单自检（不能宽到把源码也豁免掉）  ✓ × 8
✅ 全部规则有效，白名单没有误伤，跳过名单范围正确。
```

**改这个脚本后一定要跑 `--verify`。**

---

## 命令一览

| 命令 | 说明 |
| --- | --- |
| `/start` `/help` | 主菜单 / 命令手册 |
| `/status` | 运行状态与开关 |
| `/a` | 账号列表 |
| `/q` `/q 3` | 配额总览 / 指定账号 |
| `/i` `/i 3` | 实例总览 / 指定账号 |
| `/launch 3` | 创建实例（自动挑 AD） |
| `/start_vm 3 <名称>` | 开机 |
| `/stop_vm 3 <名称>` | 关机 |
| `/reboot 3 <名称>` | 重启 |
| `/terminate 3 <名称>` | 销毁（需二次确认 + 危险开关） |
| `/b 3` | 存储桶列表 |
| `/obj 3 <桶名>` | 查看桶内对象 |
| `/rmbucket 3 <桶名>` | 删除桶 |
| `/s3key 3` | 签发 S3 兼容密钥 |
| `/audit` | 计费残留审计 |
| `/security` | 安全暴露面扫描 |
| `/harden 3 <CIDR>` | 收紧 22 端口来源 |
| `/cancel` | 取消待确认操作 |

实例名支持**唯一前缀匹配** —— 匹配到多台会报错让你补全，**绝不猜**。

### 只读 CLI

```bash
python -m oracles.cli doctor                  # 环境与鉴权自检
python -m oracles.cli accounts                # 账号清单
python -m oracles.cli quota --json out.json   # 配额（可输出 JSON）
python -m oracles.cli instances --account 3   # 实例
python -m oracles.cli buckets --stats         # 桶（含对象数/体积/PAR）
python -m oracles.cli orphans                 # 孤儿卷
python -m oracles.cli audit --account 11      # 计费残留
python -m oracles.cli security                # 安全暴露面
```

CLI 刻意**只读**：命令行没有确认环节，一按回车就执行，太容易出事。
所有写操作走 Telegram。

---

## 项目结构

```
oracles/
├── oracles/
│   ├── config.py         配置加载（仓库外配置目录 + 启动期集中校验）
│   ├── oci_gateway.py    ★ 全项目唯一 import oci 的地方
│   ├── redact.py         脱敏（日志与消息的兜底防线）
│   ├── models.py         数据模型与常量
│   ├── cloudinit.py      ★ cloud-init 生成（用户名/密码注入，无密钥也能登）
│   ├── cli.py            只读运维 CLI
│   ├── services/
│   │   ├── quota.py      ★ 逐 AD 配额与补机余量
│   │   ├── compute.py    实例：plan_* 只读计划 / execute_* 执行 + 救援控制台 + 块卷管理
│   │   ├── storage.py    桶与对象、S3 密钥
│   │   ├── audit.py      孤儿卷、计费残留
│   │   ├── security.py   暴露面扫描、22 端口加固（改前自动备份）
│   │   ├── accounts_mgmt.py ★ 账号增删改 + 凭据体检 + 热重载
│   │   └── grab.py       ★ 自动抢机引擎（间隔循环，直到抢到 N 台或手动停）
│   └── bot/
│       ├── app.py        启动器
│       ├── handlers.py   命令与按钮路由（含开机向导状态机、配置/硬盘/任务管理）
│       ├── dispatch.py   ★ 写操作的唯一出口（写开关集中在此）
│       ├── keyboards.py  内联键盘（七大菜单 + 向导分步 + 卷列表 + 任务列表）
│       ├── render.py     文本渲染
│       ├── store.py      短令牌映射 + 待确认操作 + 等待输入 + 向导状态
│       └── tasks.py      ★ 抢机任务注册表（进程内单例，start/stop/list）
├── deploy/               systemd unit + 一键安装脚本
├── docs/                 详细文档
├── scripts/              自检、密钥扫描、旧配置导入
├── .gitleaks.toml        CI 里 gitleaks 的精确豁免配置（它不认识 secret-scan:allow）
├── conftest.py           把仓库根加进 sys.path（让裸 pytest 也能跑）
└── tests/                622 个测试（21 个文件）
    ├── telegram_harness.py     假的 Update/Context，让 handler 能离线跑
    ├── test_bot_handlers.py    handler 层用例（白名单/写开关/路由/渲染/日志留痕）
    ├── test_accounts_mgmt.py   账号清单增删改 + **写前备份**（含端到端按钮流）
    └── test_ci_guards.py       CI 护栏用例（步骤顺序、gitleaks 配置不许变橡皮图章）
```

### 测试分四层

| 层 | 测什么 | 为什么不能省 |
| --- | --- | --- |
| 纯函数 | 脱敏规则、端口覆盖判定、存储分页、令牌存储 | 快，但覆盖不到「接线」 |
| 渲染语义 | `[]`（确实没有）vs `None`（查询失败）必须区分 | 这类错误是**静默**的，只能靠断言锁住 |
| **handler 层** | 白名单挡住每条命令、只读模式拦下每个写操作、按钮路由 | **安全属性**，读代码确信不了，必须真跑一遍 |
| **CI 护栏** | 工作流步骤顺序、gitleaks 配置不许退化成橡皮图章 | 这两份文件只在 GitHub 上跑，**本地没人会点**，最容易悄悄退化 |

handler 层用 `tests/telegram_harness.py` 里的假 `Update`/`Context`，
不连 Telegram、不碰 OCI（OCI 调用用 monkeypatch 打桩），166 个用例 **2.5 秒**跑完。

CI 护栏层（`tests/test_ci_guards.py`）用纯文本断言，**不依赖 yaml / tomllib**
—— 因为 CI 的 Python 3.10 环境里只有 pytest + ruff。它锁住的是几件「不会让
任何现有测试变红」的退化：

- 把 gitleaks 挪到主闸门之前 → 它一误报，主闸门就再也不运行
- 把规则表内联一份到工作流 YAML → 与本地版本漂移（本项目栽过）
- 给 `.gitleaks.toml` 加一条 `example` 通用白名单 → 整个扫描器变橡皮图章

这 23 条断言本身也用**变异测试**验证过：故意制造上面每一种退化，
确认它们真的会变红（6/6 拦住）。

---

## 部署

```bash
sudo ./deploy/install.sh
```

脚本会：扫密钥 + 查 `ensurepip` → 建 `oracles` 系统用户 → 同步代码到 `/opt/oracles`
→ 建 venv 装依赖 → 准备 `/etc/oracles`（600 权限）→ 装 systemd unit
（**凭据齐了才启动**）→ 跑一次 `doctor`。
**幂等**，重复执行只更新代码和依赖，不覆盖配置。

systemd unit 带了较严的沙箱（`ProtectSystem=strict`、`ProtectHome=read-only`、
`NoNewPrivileges`），因为这是个持有全部云凭据的进程。

```bash
systemctl status oracles-bot
journalctl -u oracles-bot -f
```

详见 [docs/04-部署到VPS.md](docs/04-部署到VPS.md)。

---

## 文档

| 文档 | 内容 |
| --- | --- |
| [01-快速开始](docs/01-快速开始.md) | 从零到跑起来 |
| [02-账号与密钥配置](docs/02-账号与密钥配置.md) | 多账号配置、instance principal、密钥轮换 |
| [03-命令手册](docs/03-命令手册.md) | 全部命令与交互流程 |
| [04-部署到VPS](docs/04-部署到VPS.md) | systemd、日志、升级、故障排查 |
| [05-安全与开源注意事项](docs/05-安全与开源注意事项.md) | **push 前的检查清单** |
| [06-踩坑清单](docs/06-踩坑清单.md) | 64 个实测踩过的坑，按危险度排序（其中 26 条是「让检查失效」的元坑） |

---

## 依赖

- Python ≥ 3.10
- [`oci`](https://github.com/oracle/oci-python-sdk) —— Oracle 官方 SDK
- [`python-telegram-bot`](https://github.com/python-telegram-bot/python-telegram-bot) ≥ 21
- 可选：`boto3`（只想用 S3 兼容 API 直连对象存储时）

## 许可

MIT
