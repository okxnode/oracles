#!/usr/bin/env bash
# ============================================================================
#  提交前密钥扫描
#
#  用法：
#      ./scripts/check_secrets.sh            # 扫工作区（含未跟踪文件）
#      ./scripts/check_secrets.sh --staged   # 只扫已 git add 的内容
#      ./scripts/check_secrets.sh --verify   # 自检：验证每条规则真的能命中
#
#  建议装成 git pre-commit 钩子：
#      ln -sf ../../scripts/check_secrets.sh .git/hooks/pre-commit
#
#  ⚠️ 这个脚本是**最后一道**防线，不是唯一一道。
#     真正的第一道是 .gitignore —— 密钥从一开始就不该出现在工作区里。
#
#  ⚠️ 改这个脚本后**一定要跑 --verify**。
#     一个匹配不到东西的扫描器比没有扫描器更危险：它给你虚假的安全感。
# ============================================================================
set -uo pipefail

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; NC=$'\033[0m'
FOUND=0

# 只扫文本文件，排除依赖目录
EXCLUDES='--exclude-dir=.git --exclude-dir=node_modules --exclude-dir=__pycache__ --exclude-dir=.venv --exclude-dir=venv --exclude-dir=.pytest_cache --exclude-dir=.pytest_tmp --exclude-dir=.mypy_cache --exclude-dir=.ruff_cache'

# ---------------------------------------------------------------------------
#  白名单
# ---------------------------------------------------------------------------
# 允许出现的占位符 / 明显虚构的值。
# ⚠️ 往这个列表里加东西要非常克制 —— 每加一条就是给泄露多开一个口子。
#    只放「人一眼就知道是假的」模式。
#
# ⚠️ 特别注意：**不要**加 `aaaa` 进来。
#    真实 OCID 的随机段几乎都以 `aaaa` 开头，加了就等于把 OCID 检查废掉。
#    示例文件里的 OCID 靠 `EXAMPLE` 就能过滤。
PLACEHOLDER='EXAMPLE|REPLACE|replace|your-|xxxx|Example|EXAMPLE'
PLACEHOLDER="$PLACEHOLDER"'|deadbeef|DEADBEEF'
# 明显虚构的指纹（递增十六进制）
PLACEHOLDER="$PLACEHOLDER"'|00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff'
PLACEHOLDER="$PLACEHOLDER"'|aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99'
# 中文占位说法
PLACEHOLDER="$PLACEHOLDER"'|你的|在此|示例|占位'

# 显式豁免标记。
# 测试夹具和文档示例里**必须**出现 OCID / PEM 这类字符串，
# 否则没法验证脱敏功能本身。这类行加 `secret-scan:allow` 注释即可跳过。
# ⚠️ 标记必须和匹配内容**在同一行**（扫描是按行过滤的）。
# ⚠️ 这是给人看的承诺 —— 加之前先问自己「这真是虚构的吗」。
ALLOW_MARKER='secret-scan:allow'

# 按文件名整份跳过。
# ⚠️ 为什么是跳过而不是加标记：本脚本的「规则表」按定义就包含一堆
#    看起来像密钥的正则和样本，自己扫自己必然全红。它的正确性由 `--verify`
#    保证，不是由自扫保证。CI 工作流同理（里面写着同一批正则）。
# ⚠️ 这个变量必须被**两个扫描模式共用**。曾经只有 scan_workspace 排除了自己、
#    scan_staged 没排除 —— 于是 --staged 永远报 7 条假警报，真泄露混在里面
#    根本看不出来。**假警报比漏检更会害死人，因为它训练你忽略输出。**
SKIP_FILES='check_secrets\.sh|secret-scan\.yml'

is_skipped() {
    printf '%s' "$1" | grep -qE "$SKIP_FILES"
}

# ---------------------------------------------------------------------------
#  规则表
#
#  ⚠️ 正则里**不要用 `\b`**！
#     macOS 自带的 BSD grep 在 ERE 模式下不支持 `\b`，会静默匹配不到任何东西 ——
#     结果是这条检查看起来在跑，其实一直失效。CI 上是 GNU grep，但两边必须一致。
#
#  每行格式：  名称<TAB>正则<TAB>应当被命中的样本（供 --verify 用）
# ---------------------------------------------------------------------------
RULES=$(cat <<'RULES_EOF'
OCID	ocid1\.[a-z0-9_-]+\.[a-z0-9_-]+\.[A-Za-z0-9._-]{20,}	ocid1.tenancy.oc1..aaaaaaaajq7mz3kx9pvw2nrt5ybc8dfg6hkl4sme
PEM私钥	\-\-\-\-\-BEGIN [A-Z ]*PRIVATE KEY\-\-\-\-\-	-----BEGIN PRIVATE KEY-----
API指纹	[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){15}	ff:ee:dd:cc:bb:aa:99:88:77:66:55:44:33:22:11:00
TelegramToken	[0-9]{8,12}:[A-Za-z0-9_-]{30,45}	9876543210:ZZZfakeTokenForScannerSensitivityTest1234
S3AccessKey	[0-9a-f]{40}	cafebabecafebabecafebabecafebabecafebabe
私钥文件名	(key_file|keyFile)[[:space:]]*[:=][[:space:]]*["'"'"']?[^"'"'"' ]*\.pem	key_file=/etc/oracles/keys/oci_api_key.pem
疑似SecretKey	(secret|Secret)[A-Za-z_]*[[:space:]]*[:=][[:space:]]*["'"'"'][A-Za-z0-9+/]{30,}={0,2}	secret_key = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ab"
RULES_EOF
)

iter_rules() {
    while IFS=$'\t' read -r name pattern sample; do
        [ -n "$name" ] || continue
        printf '%s\t%s\t%s\n' "$name" "$pattern" "$sample"
    done <<< "$RULES"
}

# ---------------------------------------------------------------------------
#  --verify：验证每条规则真的能命中目标样本
# ---------------------------------------------------------------------------
verify_rules() {
    echo "🔬 规则自检（每条规则都必须能命中它该命中的东西）"
    echo ""
    local failed=0
    while IFS=$'\t' read -r name pattern sample; do
        if printf '%s\n' "$sample" | grep -qE "$pattern"; then
            echo "  ${GREEN}✓${NC} $name"
        else
            echo "  ${RED}✗${NC} $name —— 规则匹配不到目标样本，这条检查是失效的！"
            echo "      规则：$pattern"
            echo "      样本：$sample"
            failed=1
        fi
    done < <(iter_rules)

    echo ""
    # 反向验证：占位符白名单不能把真实特征也吃掉
    echo "🔬 白名单自检（真实特征不能被白名单误伤）"
    local whitelist_broken=0
    while IFS=$'\t' read -r name pattern sample; do
        if printf '%s\n' "$sample" | grep -qE "$PLACEHOLDER"; then
            echo "  ${RED}✗${NC} $name 的真实样本被白名单过滤了 —— 这条检查会被误伤"
            whitelist_broken=1
        else
            echo "  ${GREEN}✓${NC} $name 不被白名单误伤"
        fi
    done < <(iter_rules)

    echo ""
    # 跳过名单自检：SKIP_FILES 只能匹配扫描器自己和 CI 工作流。
    # 一旦有人图省事往里加目录名，整个检查就废了 —— 那种改动必须被拦住。
    echo "🔬 跳过名单自检（不能宽到把源码也豁免掉）"
    local skip_broken=0
    for probe in \
        "scripts/check_secrets.sh" \
        ".github/workflows/secret-scan.yml" \
        "oracles/services/security.py" \
        "oracles/oci_gateway.py" \
        "oracles/config.py" \
        "accounts.example.json" \
        ".env.example" \
        "docs/05-安全与开源注意事项.md"
    do
        if is_skipped "$probe"; then
            case "$probe" in
                scripts/check_secrets.sh|.github/workflows/secret-scan.yml)
                    echo "  ${GREEN}✓${NC} $probe 按预期跳过"
                    ;;
                *)
                    echo "  ${RED}✗${NC} $probe 被跳过了 —— SKIP_FILES 过宽，会漏检真实泄露"
                    skip_broken=1
                    ;;
            esac
        else
            case "$probe" in
                scripts/check_secrets.sh|.github/workflows/secret-scan.yml)
                    echo "  ${RED}✗${NC} $probe 没被跳过 —— 自扫必然产生假警报"
                    skip_broken=1
                    ;;
                *)
                    echo "  ${GREEN}✓${NC} $probe 正常参与扫描"
                    ;;
            esac
        fi
    done

    echo ""
    if [ "$failed" -eq 0 ] && [ "$whitelist_broken" -eq 0 ] && [ "$skip_broken" -eq 0 ]; then
        echo "${GREEN}✅ 全部规则有效，白名单没有误伤，跳过名单范围正确。${NC}"
        return 0
    fi
    echo "${RED}❌ 规则表有问题，先修好再用来扫密钥。${NC}"
    return 1
}

# ---------------------------------------------------------------------------
#  扫描
# ---------------------------------------------------------------------------
scan_workspace() {
    echo "🔍 扫描：工作区"
    while IFS=$'\t' read -r name pattern sample; do
        local hits
        hits=$(grep -rInE $EXCLUDES "$pattern" . 2>/dev/null \
               | grep -vE "$PLACEHOLDER" \
               | grep -vF "$ALLOW_MARKER" \
               | grep -vE "$SKIP_FILES" \
               || true)
        if [ -n "$hits" ]; then
            echo "${RED}  ✗ $name${NC}"
            echo "$hits" | head -8 | sed 's/^/      /'
            FOUND=1
        else
            echo "${GREEN}  ✓ $name${NC}"
        fi
    done < <(iter_rules)
}

scan_staged() {
    echo "模式：只扫描已暂存内容"

    # ⚠️ 取暂存文件列表有两个坑，都踩过：
    #
    #  坑 1：`git diff --name-only` 默认对非 ASCII 文件名做转义（core.quotepath=true），
    #        输出的是带引号的字面量 `"docs/01-\345\277\253..."`。
    #        拿它去 `[ -f "$f" ]` 永远为假 —— 结果是**所有中文名文件被静默跳过**。
    #        本项目 6 份文档全中文名，等于一直没被扫过。
    #        解法：`-c core.quotepath=false` 拿原始路径。
    #
    #  坑 2：`for f in $staged` 按空格分词，路径含空格就断成两个不存在的文件。
    #        解法：`-z` + `while IFS= read -r -d ''` 按 NUL 切分。
    local -a files=()
    local f
    while IFS= read -r -d '' f; do
        [ -n "$f" ] && files+=("$f")
    done < <(git -c core.quotepath=false diff --cached --name-only --diff-filter=ACM -z 2>/dev/null)

    if [ "${#files[@]}" -eq 0 ]; then
        echo "${GREEN}没有暂存内容。${NC}"
        return 0
    fi

    # 再过滤一次：整份跳过的文件 + 磁盘上已不存在的
    local -a readable=()
    for f in "${files[@]}"; do
        is_skipped "$f" && continue
        [ -f "$f" ] && readable+=("$f")
    done

    # 兜底：暂存里有文件却一个都读不到，说明上面的路径处理又坏了。
    # 宁可报错也不能假装「扫描通过」—— 这正是本项目反复栽的那个跟头。
    if [ "${#readable[@]}" -eq 0 ]; then
        echo "${RED}  ✗ 暂存区有 ${#files[@]} 个文件，但一个都没读到 —— 扫描器路径处理有问题${NC}"
        FOUND=1
        return 1
    fi
    echo "  实际扫描 ${#readable[@]} / ${#files[@]} 个文件"

    while IFS=$'\t' read -r name pattern sample; do
        for f in "${readable[@]}"; do
            # ⚠️ 必须把 grep 结果存进变量再判断 ——
            #    直接写 `grep ... | sed ... && FOUND=1` 的话，
            #    管道最后一环是 sed，它总是成功，FOUND 会被无条件置 1。
            local hits
            hits=$(grep -InE "$pattern" "$f" 2>/dev/null \
                   | grep -vE "$PLACEHOLDER" \
                   | grep -vF "$ALLOW_MARKER" || true)
            if [ -n "$hits" ]; then
                echo "${RED}  ✗ $name${NC}  $f"
                echo "$hits" | head -3 | sed 's/^/      /'
                FOUND=1
            fi
        done
    done < <(iter_rules)

    if [ "$FOUND" -eq 0 ]; then
        echo "${GREEN}  ✓ 暂存内容干净${NC}"
    fi
}

check_tracked_files() {
    echo ""
    echo "🔍 检查是否有敏感文件被跟踪："
    for f in accounts.json oracles.env .env si.pem; do
        if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
            echo "${RED}  ✗ $f 已被 git 跟踪！立即执行：git rm --cached $f${NC}"
            FOUND=1
        fi
    done
    if git ls-files 2>/dev/null | grep -qE '\.pem$|\.key$|accounts\.json$'; then
        echo "${RED}  ✗ 仓库里有 .pem/.key/accounts.json 被跟踪${NC}"
        git ls-files | grep -E '\.pem$|\.key$|accounts\.json$' | sed 's/^/      /'
        FOUND=1
    else
        echo "${GREEN}  ✓ 没有敏感文件被跟踪${NC}"
    fi
}

# ---------------------------------------------------------------------------
#  入口
# ---------------------------------------------------------------------------
case "${1:-}" in
    --verify)
        verify_rules
        exit $?
        ;;
    --staged)
        scan_staged
        ;;
    *)
        scan_workspace
        check_tracked_files
        ;;
esac

echo ""
if [ "$FOUND" -eq 0 ]; then
    echo "${GREEN}✅ 扫描通过，可以提交。${NC}"
    exit 0
fi
echo "${RED}❌ 发现疑似密钥！请先处理再提交。${NC}"
echo "${YELLOW}如果已经 push 过，光删文件没用 —— 密钥仍在 git 历史里，"
echo "必须去 OCI 控制台吊销并重新签发。${NC}"
exit 1
