#!/usr/bin/env bash
# ============================================================================
#  oracles 一键安装脚本（systemd 裸机部署）
#
#  用法：
#      sudo ./deploy/install.sh
#      sudo ./deploy/install.sh --src /path/to/oracles   # 指定源码目录
#
#  做五件事：
#      0. 先扫一遍源码里有没有密钥（有就中止）
#      1. 建 oracles 系统用户 + /opt/oracles 目录
#      2. 建 venv 装依赖
#      3. 准备 /etc/oracles（账号清单 + 私钥 + 环境变量），权限 600
#      4. 装 systemd unit；凭据已配好才启动，否则只装不启（见第 4 节注释）
#
#  幂等：重复执行只会更新代码和依赖，不会覆盖已有配置。
# ============================================================================
set -euo pipefail

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; NC=$'\033[0m'
APP_USER="oracles"
APP_DIR="/opt/oracles"
CONF_DIR="/etc/oracles"
UNIT="/etc/systemd/system/oracles-bot.service"

die() { echo "${RED}✗ $*${NC}" >&2; exit 1; }
info() { echo "${GREEN}▸${NC} $*"; }
warn() { echo "${YELLOW}!${NC} $*"; }

[ "$(id -u)" -eq 0 ] || die "需要 root 权限，请用 sudo 执行"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
while [ $# -gt 0 ]; do
    case "$1" in
        --src) SRC_DIR="$2"; shift 2 ;;
        *) die "未知参数：$1" ;;
    esac
done
[ -f "$SRC_DIR/pyproject.toml" ] || die "在 $SRC_DIR 没找到 pyproject.toml"

# ---------------------------------------------------------------------------
# 0. 起飞前检查：源码里不能有密钥
#
# 下面会把 $SRC_DIR 整个 rsync 到 /opt/oracles。如果源码里躺着 si.pem
# 或者写死的 API Key，这一下就把它复制到系统目录了 —— 而且是 root 权限，
# 之后 chown 给 oracles 用户。所以在复制**之前**先扫一遍。
#
# 宁可这里报错中止，也不要装出一个带密钥的副本。
#
# ⚠️ 必须 `cd "$SRC_DIR"` 再跑扫描。check_secrets.sh 扫的是**当前目录**，
#    不 cd 就等于扫「你在哪儿敲的命令」，两个方向都会出错：
#      · 站在干净目录（比如 /）跑 → **静默放行**，源码里的密钥照样被 rsync 到
#        $APP_DIR。闸门看起来在跑，其实什么都没查 —— 最坏的那种失效。
#      · 站在脏目录跑 → 被无关文件误报，源码明明干净却中止安装。
#    2026-09-20 实测撞上后者：`cd /tmp && sudo bash .../install.sh` 时，
#    它扫的是 /tmp 里上一次救援留下的诊断脚本，安装被无端中止。
# ---------------------------------------------------------------------------
if [ -f "$SRC_DIR/scripts/check_secrets.sh" ]; then
    info "扫描源码里的密钥…"
    if ! ( cd "$SRC_DIR" && bash scripts/check_secrets.sh ) >/dev/null 2>&1; then
        warn "源码里发现疑似密钥，下面是详细命中："
        echo ""
        ( cd "$SRC_DIR" && bash scripts/check_secrets.sh ) || true
        echo ""
        die "中止安装 —— 先处理掉上面的密钥，否则会被复制到 $APP_DIR"
    fi
    info "源码干净"
fi

# ---------------------------------------------------------------------------
# 0.5 起飞前检查：python3 能不能建 venv
#
# Ubuntu/Debian 的 python3 默认**不带 ensurepip**，第 2 步会挂在这里：
#     The virtual environment was not created successfully because ensurepip
#     is not available.  ... apt install python3.14-venv
# 那段报错只告诉你包名，没告诉你两件事：
#   1) 包名带**版本号**（python3.14-venv），得自己拼
#   2) 云镜像首次启动后 apt 索引常常是**空的**，直接装会说
#      "has no installation candidate" —— 得先 apt-get update
# 结果就是用户对着一段「照着做还是不行」的报错发呆。
# 与其在第 2 步炸掉（那时用户和目录都已经建好了），不如现在就拦住。
# ---------------------------------------------------------------------------
if ! python3 -c 'import ensurepip' 2>/dev/null; then
    # 注意两点：
    #  1) 末尾的 `|| echo 3.x` 不能省。`set -e` 下，命令替换失败会让整个赋值
    #     语句返回非零从而**直接退出脚本** —— 连下面那句 warn 都执行不到，
    #     用户只会看到一片空白。加上 || 让这个替换永远不会失败。
    #  2) PYVER 已经含主版本号（"3.14"），包名前缀是 python 而不是 python3，
    #     写成 python3${PYVER}-venv 会拼出 python33.14-venv 这种不存在的包名。
    PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 3.x)"
    warn "python3 缺少 ensurepip 模块，建不了 venv。"
    cat <<EOF

  修复（注意包名带版本号）：
      sudo apt-get update
      sudo apt-get install -y python3-venv python${PYVER}-venv

  如果 apt 说 "has no installation candidate"，就是索引还没拉过 ——
  云镜像首次启动后 /var/lib/apt/lists 往往是空的，先跑 apt-get update。

EOF
    die "装完上面的包再重新执行本脚本"
fi
info "python3 可以建 venv"

# ---------------------------------------------------------------------------
# 1. 用户与目录
# ---------------------------------------------------------------------------
if id "$APP_USER" &>/dev/null; then
    info "用户 $APP_USER 已存在"
else
    info "创建系统用户 $APP_USER"
    useradd --system --shell /usr/sbin/nologin --home-dir "$APP_DIR" "$APP_USER"
fi

mkdir -p "$APP_DIR"
info "同步代码到 $APP_DIR"
# 排除密钥/虚拟环境/缓存，避免把本地的敏感文件带上去。
# 这份清单和 .gitignore 是**同一个意图的两处表达**（一个管 git，一个管 rsync），
# 改一处记得改另一处。注意 --exclude 'accounts.json' 只匹配这个确切的文件名，
# accounts.example.json 不受影响。
#
# ⚠️ `*.egg-info` 排的是**本地**那份（本地跑过 `pip install -e .` 的产物）。
#    但目标机上一定会有这个目录 —— 第 2 步的 `pip install -e "$APP_DIR"`
#    会自己生成一份（2026-09-20 实测：目录时间戳落在安装过程中，
#    不是从本地同步过去的）。所以 /opt/oracles/oracles.egg-info 是**正常的**，
#    别去删它，删了下一次安装又回来。`--exclude` 还会让它免于被 --delete 清掉。
rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
    --exclude '.env' --include '.env.example' --exclude '.env.*' \
    --include 'accounts.example.json' \
    --exclude 'accounts.json' --exclude 'accounts.*.json' \
    --exclude '*.pem' --exclude '*.key' --exclude '*.priv' \
    --exclude 'keys/' --exclude 'oracles.env' --exclude 'bot.env' \
    --exclude '*credentials*.txt' --exclude 's3-*.txt' \
    --exclude '.pytest_cache' --exclude '.pytest_tmp' \
    --exclude '.ruff_cache' --exclude '.mypy_cache' --exclude '.coverage' \
    --exclude '*.egg-info' --exclude '.DS_Store' \
    --exclude 'backups' --exclude 'logs' --exclude '*.log' \
    "$SRC_DIR/" "$APP_DIR/"

# ⚠️ 兜底：确认后面步骤要用的文件真的同步过去了。
#
# 为什么需要这个：rsync 的排除规则很容易误伤。加 `--exclude 'accounts.*.json'`
# 时会连 `accounts.example.json` 一起排掉（它正好匹配这个通配），
# 于是下面 `install ... accounts.example.json` 那一步会莫名失败 ——
# 报错信息离真正的原因隔了 60 行。`.env.*` 对 `.env.example` 是同一个坑。
# 宁可在复制完就立刻喊出来。
for _need in accounts.example.json pyproject.toml \
             deploy/oracles-bot.service deploy/oracles.env.example; do
    [ -e "$APP_DIR/$_need" ] || die "同步后缺少 $_need —— 检查 rsync 的 --include/--exclude 规则"
done

# ---------------------------------------------------------------------------
# 2. 虚拟环境
# ---------------------------------------------------------------------------
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    info "创建虚拟环境"
    python3 -m venv "$APP_DIR/.venv"
fi
info "安装依赖"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"

# ---------------------------------------------------------------------------
# 3. 配置目录（密钥的家）
# ---------------------------------------------------------------------------
mkdir -p "$CONF_DIR/keys" "$CONF_DIR/backups"
chmod 700 "$CONF_DIR" "$CONF_DIR/keys"
chmod 700 "$CONF_DIR/backups"

if [ ! -f "$CONF_DIR/accounts.json" ]; then
    install -m 600 "$APP_DIR/accounts.example.json" "$CONF_DIR/accounts.json"
    warn "已创建 $CONF_DIR/accounts.json（还是占位内容）"
    warn "请填入真实凭据：  sudo nano $CONF_DIR/accounts.json"
fi

if [ ! -f "$CONF_DIR/oracles.env" ]; then
    install -m 600 "$APP_DIR/deploy/oracles.env.example" "$CONF_DIR/oracles.env"
    warn "已创建 $CONF_DIR/oracles.env"
    warn "请填入 TELEGRAM_BOT_TOKEN 和 TELEGRAM_ALLOWED_USER_IDS："
    warn "  sudo nano $CONF_DIR/oracles.env"
fi

chown -R "$APP_USER:$APP_USER" "$CONF_DIR" "$APP_DIR"
chmod 600 "$CONF_DIR/accounts.json" "$CONF_DIR/oracles.env" 2>/dev/null || true
# 私钥必须是 600
find "$CONF_DIR/keys" -type f -name '*.pem' -exec chmod 600 {} \; 2>/dev/null || true

# ---------------------------------------------------------------------------
# 4. systemd
# ---------------------------------------------------------------------------
info "安装 systemd unit"
install -m 644 "$APP_DIR/deploy/oracles-bot.service" "$UNIT"
systemctl daemon-reload

# 读配置里某个键的值（忽略注释行；同名取最后一条，和 systemd 的行为一致）
env_value() {
    [ -f "$CONF_DIR/oracles.env" ] || return 0
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$CONF_DIR/oracles.env" | tail -n 1
}

# 凭据是否**已配置**。
#
# 这里校验的是形态而不是「非空」，因为 oracles.env.example 里那句占位文字
# （"在这里填BotFather给的token"）本身就不为空 —— 光判空会把占位符当真 token
# 放行，服务起来照样崩。token 形态与 oracles/redact.py 里那条正则保持一致。
TOKEN_OK=0
IDS_OK=0
if printf '%s' "$(env_value TELEGRAM_BOT_TOKEN)" \
        | grep -Eq '^[0-9]{8,12}:[A-Za-z0-9_-]{30,45}$'; then
    TOKEN_OK=1
fi
if printf '%s' "$(env_value TELEGRAM_ALLOWED_USER_IDS)" \
        | grep -Eq '^[0-9]+(,[0-9]+)*$'; then
    IDS_OK=1
fi

# 没配好就不启动 —— 这是有意的，不是偷懒。
#
# unit 里是 Restart=always + RestartSec=10。token 为空时进程会立刻抛错退出，
# systemd 每 10 秒拉一次 → 无限崩溃循环，journal 被刷爆；而且下面那个
# is-active 检查会报「服务没起来」并 exit 1，让人以为整个安装失败了
# ——其实代码、依赖、配置、unit 全都已经就位，只差两行凭据。
# 所以这里显式分成「装好待配」和「装好并启动」两种情况。
if [ "$TOKEN_OK" -eq 1 ] && [ "$IDS_OK" -eq 1 ]; then
    systemctl enable oracles-bot >/dev/null
    systemctl restart oracles-bot
    sleep 3
    if ! systemctl is-active --quiet oracles-bot; then
        warn "服务没起来，看看日志："
        journalctl -u oracles-bot -n 30 --no-pager
        exit 1
    fi
    info "服务已启动 ✅"
    SERVICE_READY=1
else
    systemctl disable oracles-bot >/dev/null 2>&1 || true
    systemctl stop oracles-bot >/dev/null 2>&1 || true
    SERVICE_READY=0
    warn "Telegram 凭据还没配好，服务已安装但**未启动**"
    warn "  （避免 Restart=always 每 10 秒崩溃重启一次刷爆日志）"
fi

# ---------------------------------------------------------------------------
# 5. 自检
# ---------------------------------------------------------------------------
echo ""
info "运行连通性自检…"
set +e
sudo -u "$APP_USER" \
    ORACLES_HOME="$CONF_DIR" \
    "$APP_DIR/.venv/bin/python" -m oracles.cli doctor
DOCTOR=$?
set -e

echo ""
echo "────────────────────────────────────────────────"
if [ $DOCTOR -eq 0 ]; then
    echo "${GREEN}✅ 安装完成，所有账号鉴权正常。${NC}"
else
    echo "${YELLOW}⚠️ 安装完成，但有账号自检失败 —— 看上面的输出。${NC}"
fi
cat <<EOF

常用命令：
  systemctl status oracles-bot        查看状态
  journalctl -u oracles-bot -f        跟踪日志
  sudo nano $CONF_DIR/oracles.env    改配置（改完 systemctl restart oracles-bot）
EOF

# ⚠️ 这一段必须读**实际配置**，不能写死。
#
# 2026-09-20 实测：一次升级部署里，脚本结尾照样打印「出厂是只读模式」，
# 而 /etc/oracles/oracles.env 里 ORACLES_WRITE_ENABLED 早就是 true 了。
# 运维照着这句话去 Telegram 里找「打开写操作」——找不到，
# 反而以为自己漏了哪一步。（同踩坑 #38：**一句会误导人的提示，比没有提示更糟。**）
#
# 判断用 `env_value`（第 4 节定义的，忽略注释行、同名取最后一条），
# 而不是 `grep` 整个文件 —— 否则注释里那句示例值也会被算进去。
WRITE_NOW="$(env_value ORACLES_WRITE_ENABLED | tr '[:upper:]' '[:lower:]')"
if [ "$WRITE_NOW" = "true" ]; then
cat <<EOF

⚠️ 当前写操作是**开启**的（ORACLES_WRITE_ENABLED=true）。
   计划页仍然是只读的、要人确认才执行；但请确认白名单里只有你自己的账号。
EOF
else
cat <<EOF

⚠️ 出厂是 **只读模式**（ORACLES_WRITE_ENABLED 未开启）。
   先在 Telegram 里点几个按钮确认计划内容都对，再打开写操作。
EOF
fi

if [ "$SERVICE_READY" -eq 0 ]; then
cat <<EOF

下一步 —— 只差两行凭据：
  1) sudo nano $CONF_DIR/oracles.env     填 TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_IDS
  2) sudo systemctl enable --now oracles-bot
EOF
fi
