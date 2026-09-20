"""CI 与密钥扫描配置的护栏测试。

这组测试防的是「元坑」—— **检查在跑，但没在检查**。
它们是纯文本断言，不依赖 pyyaml / tomllib，因为 CI 的 Python 3.10
环境里只装了 pytest + ruff（tomllib 是 3.11+ 才有的）。

为什么这些断言值得单独写一个文件：

    `.github/workflows/secret-scan.yml` 和 `.gitleaks.toml` 是仓库里
    **唯一两个无法被单元测试直接执行**的部件 —— 它们只在 GitHub 上跑，
    本地没人会去点。于是它们最容易悄悄退化：
    · 把 gitleaks 挪到前面 → 它一误报，主闸门就再也不运行
    · 把规则表内联一份到 YAML → 与本地版本漂移（本项目栽过）
    · 给 .gitleaks.toml 加一条 `example` 通用白名单 → 整个扫描器变橡皮图章

    这三件事都不会让任何现有测试变红。所以补上这个文件。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "secret-scan.yml"
GITLEAKS = ROOT / ".gitleaks.toml"
SCANNER = ROOT / "scripts" / "check_secrets.sh"
INSTALL = ROOT / "deploy" / "install.sh"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _read(path: Path) -> str:
    assert path.exists(), f"文件不存在：{path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def _line_of(text: str, needle: str) -> int:
    """返回 needle 第一次出现的行号（从 0 开始）。找不到就断言失败。"""
    for i, line in enumerate(text.splitlines()):
        if needle in line:
            return i
    raise AssertionError(f"在文件里找不到 {needle!r}")


def _line_of_exact(text: str, stripped: str) -> int:
    """返回**去掉首尾空白后完全等于** stripped 的行号。

    为什么需要这个：`--verify` 那行本身就包含 `run: bash scripts/check_secrets.sh`
    这个子串，用 _line_of 取两个位置会得到同一个行号 —— 断言恒真，测了个寂寞。
    """
    for i, line in enumerate(text.splitlines()):
        if line.strip() == stripped:
            return i
    raise AssertionError(f"在文件里找不到整行等于 {stripped!r} 的行")


def _strip_comments(text: str) -> str:
    """去掉注释行。

    注释里出现 `ocid1.` 这类字样是**说明性文字**，不是抄了一份规则表。
    不剥掉的话，解释「gitleaks 为什么不认识 ocid1. 前缀」的注释
    反而会被判成规则泄漏 —— 检查自己制造假警报。
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


# ---------------------------------------------------------------------------
# A. CI 工作流
# ---------------------------------------------------------------------------
def test_workflow_exists_and_is_yaml() -> None:
    text = _read(WORKFLOW)
    assert text.lstrip().startswith("name:"), "工作流应以 name: 开头"
    # 极轻量的结构检查：不引 yaml 库，只确认关键键位存在
    for key in ("on:", "jobs:", "runs-on:", "steps:"):
        assert key in text, f"工作流缺少 {key}"


def test_workflow_calls_repo_script_instead_of_inlining_rules() -> None:
    """CI 必须调用仓库里的扫描脚本，不能内联一份规则表。

    内联过一次，结果 CI 的白名单残留 `aaaa`、规则比本地少 2 条 —— 两处实现
    必然漂移。扫描器是唯一事实来源。
    """
    text = _read(WORKFLOW)
    assert "bash scripts/check_secrets.sh" in text, "CI 没有调用仓库里的扫描脚本"

    # 先剥掉注释 —— 注释里解释「gitleaks 为什么不认识 ocid1. 前缀」是说明性
    # 文字，不是规则泄漏。不剥的话检查会自己制造假警报。
    body = _strip_comments(text)

    # 再剥掉反斜杠：规则表里的正则是转义形态（`ocid1\.`、`\-\-\-\-\-BEGIN`），
    # 不归一化的话，直接找 `ocid1.` 会匹配不上 —— 检查静默失效。
    # （这条是变异测试抓出来的：一开始的版本漏掉了带转义的那份规则。）
    flat = body.replace("\\", "")

    for frag in ("ocid1.", "BEGIN PRIVATE KEY", "[0-9a-f]{40}", "{2}(:[0-9a-fA-F]{2}){15}"):
        assert frag not in flat, (
            f"工作流里出现了规则表片段 {frag!r} —— 说明规则被复制了一份，会漂移"
        )

    # 结构性断言：工作流里不该有任何自己实现的扫描逻辑。
    # 合法步骤只有「调用仓库脚本 / pip install / pytest」，都不需要 grep。
    assert "grep" not in flat, (
        "工作流里出现了 grep —— 说明它在自己实现扫描逻辑，而不是调用仓库脚本"
    )
    # 同理：允许逻辑（白名单/标记）也只该有一处实现
    assert "secret-scan:allow" not in flat, (
        "工作流里出现了豁免标记 —— 说明豁免逻辑被复制了一份"
    )


def test_workflow_self_check_runs_before_real_scan() -> None:
    """`--verify` 必须在真正扫描之前跑：先证明规则能命中，再去扫文件。"""
    text = _read(WORKFLOW)
    verify = _line_of_exact(text, "run: bash scripts/check_secrets.sh --verify")
    scan = _line_of_exact(text, "run: bash scripts/check_secrets.sh")
    assert verify < scan, "规则自检必须排在真实扫描之前"


def test_workflow_runs_main_gate_before_gitleaks() -> None:
    """⚠️ 主闸门必须排在 gitleaks **之前**。

    GitHub Actions 一旦某步失败就中止后续步骤。gitleaks 排前面的话，
    它报一个假阳性就会让真正的主闸门**根本不运行** —— 用弱检查的假阳性
    把强检查屏蔽掉。

    实测：注入 7 类本项目的真实密钥，check_secrets.sh 命中 7/7，
    gitleaks 只命中 1/7（不认识 ocid1. 前缀和 Telegram 的 <数字>:<35位> 形态）。
    """
    text = _read(WORKFLOW)
    ours = _line_of_exact(text, "run: bash scripts/check_secrets.sh")
    theirs = _line_of_exact(text, "uses: gitleaks/gitleaks-action@v2")
    assert ours < theirs, (
        "主闸门（check_secrets.sh）必须排在 gitleaks 之前；"
        "否则 gitleaks 一误报，主闸门就不会运行"
    )


def test_workflow_runs_open_source_audit_before_gitleaks() -> None:
    """开源审计也必须排在 gitleaks **之前**。

    它和 check_secrets.sh 是互补的两半：一个查「密钥形态」，一个查「发布面」
    （已跟踪的敏感文件 / .gitignore 实际覆盖 / 文档里的真实标识）。

    ⚠️ 这条护栏防的是**新闸门腐烂**：加进 CI 之后被后来的改动挪到 gitleaks
    后面 —— 那时 gitleaks 一误报，审计就不再运行，而 CI 仍然是绿的。
    一个「跑了但没在检查」的闸门比没有闸门更危险。
    """
    text = _read(WORKFLOW)
    audit = _line_of_exact(text, "run: python3 scripts/audit_open_source.py")
    theirs = _line_of_exact(text, "uses: gitleaks/gitleaks-action@v2")
    assert audit < theirs, (
        "开源审计必须排在 gitleaks 之前；否则 gitleaks 一误报，审计就不会运行"
    )
    # 用 python3 而不是 python：scan job 没有 setup-python 步骤，
    # 靠的是 runner 自带的解释器，只有 python3 是**保证**存在的名字。
    assert "run: python scripts/" not in text, (
        "别用裸 python —— CI 的 scan job 不装解释器，只有 python3 保证存在"
    )


def test_workflow_verifies_the_audit_rules_before_trusting_them() -> None:
    """审计也要先自检再扫描 —— 和 check_secrets.sh 完全对称。

    一个「匹配不到任何东西」的扫描器比没有扫描器更危险：它给你虚假的安全感。
    本项目在密钥扫描器上栽过两次（`\\b` 在 BSD grep 下静默失效、白名单里的
    `aaaa` 把 OCID 检查变成死代码），那两次的共同点是**检查在跑、输出是绿的、
    但什么也没检查**。审计脚本是另一套实现，同样需要自证。

    ⚠️ 这条护栏防的是「自检被删掉」：删掉之后 CI 仍然全绿，
    只是闸门从此没人验证过 —— 正是这个文件存在的理由。
    """
    text = _read(WORKFLOW)
    verify = _line_of_exact(text, "run: python3 scripts/audit_open_source.py --verify")
    audit = _line_of_exact(text, "run: python3 scripts/audit_open_source.py")
    assert verify < audit, "审计的规则自检必须排在真实扫描之前"


def test_workflow_points_gitleaks_at_repo_config() -> None:
    """显式指定配置文件，不依赖 action 的自动发现行为。

    ⚠️ 剥注释再断言 —— 工作流里解释这件事的注释本身就写着 `.gitleaks.toml`，
    不剥的话删掉 `env: GITLEAKS_CONFIG` 也照样绿。
    """
    text = _strip_comments(_read(WORKFLOW))
    assert "GITLEAKS_CONFIG" in text, "没有显式指定 .gitleaks.toml"
    assert ".gitleaks.toml" in text


# ---------------------------------------------------------------------------
# B. gitleaks 配置 —— 重点防「橡皮图章」
# ---------------------------------------------------------------------------
def test_gitleaks_config_exists_and_extends_defaults() -> None:
    text = _read(GITLEAKS)
    assert "useDefault = true" in text, (
        "必须用 [extend] useDefault = true 继承默认规则集 —— "
        "否则等于自己从零写规则，覆盖更差"
    )


def test_gitleaks_config_has_no_generic_allowlist() -> None:
    """豁免必须精确到「文件」或「行」，不能有通用白名单。

    ⚠️ 这是本文件最重要的一条断言。
    一旦有人往 allowlist 里加 `example` / `EXAMPLE` / `test` / `fake` 这类词，
    真实凭据里完全可能带这些字样 —— 扫描器当场变橡皮图章，而且**不会报错**。
    """
    text = _read(GITLEAKS)
    forbidden = ["example", "dummy", "placeholder", "your-", "xxxx", "aaaa"]
    for word in forbidden:
        # 允许出现在注释里（说明为什么不能加），但不允许出现在实际的正则里
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            assert word not in stripped.lower(), (
                f".gitleaks.toml 的配置行里出现了通用白名单词 {word!r}：{stripped}\n"
                "豁免必须精确到文件或行，否则扫描器就废了"
            )


def test_gitleaks_config_allow_marker_matches_local_scanner() -> None:
    """gitleaks 的豁免标记必须和本地扫描脚本是**同一个字符串**。

    两处各写一个标记的话，加标记的人只会加对一个，另一个静默失效。

    ⚠️ 两边都剥注释：`.gitleaks.toml` 里解释这个标记的注释也写着
    `secret-scan:allow`，不剥的话删掉真正的 regex 行也照样绿。
    """
    gitleaks_text = _strip_comments(_read(GITLEAKS))
    scanner_text = _strip_comments(_read(SCANNER))

    m = re.search(r"^ALLOW_MARKER='([^']+)'", scanner_text, re.MULTILINE)
    assert m, "在 check_secrets.sh 里找不到 ALLOW_MARKER 定义"
    marker = m.group(1)

    assert marker in gitleaks_text, (
        f".gitleaks.toml 里没有使用本地扫描器的标记 {marker!r} —— 两处约定已漂移"
    )


def test_gitleaks_allow_marker_is_scoped_to_the_line() -> None:
    """标记豁免必须限定在**命中所在行**，不能整份放行文件。

    少了 regexTarget = "line"，一个文件里只要出现一次标记，整个文件就免检 ——
    等于给泄露开一个后门。

    ⚠️ 剥注释再断言，理由同上。
    """
    text = _strip_comments(_read(GITLEAKS))
    assert re.search(r'regexTarget\s*=\s*"line"', text), (
        '缺少 regexTarget = "line"：行内标记会退化成整份文件豁免'
    )


def test_gitleaks_config_skips_scanner_self_reference() -> None:
    """扫描器自己和 CI 工作流必须整份跳过。

    这两个文件按定义就包含一堆看起来像密钥的正则和样本，自己扫自己必然全红。
    这份清单和 check_secrets.sh 的 SKIP_FILES 是同一个意图的两处表达。
    """
    text = _strip_comments(_read(GITLEAKS))
    assert r"check_secrets\.sh" in text, "没有跳过扫描器自身"
    assert r"secret-scan\.yml" in text, "没有跳过 CI 工作流自身"

    # 与本地 SKIP_FILES 保持同一组目标
    scanner_text = _strip_comments(_read(SCANNER))
    m = re.search(r"^SKIP_FILES='([^']+)'", scanner_text, re.MULTILINE)
    assert m, "在 check_secrets.sh 里找不到 SKIP_FILES 定义"
    for name in ("check_secrets", "secret-scan"):
        assert name in m.group(1), f"SKIP_FILES 里缺少 {name}"
        assert name in text, f".gitleaks.toml 里缺少 {name}"


# ---------------------------------------------------------------------------
# C. install.sh 的起飞前闸门 —— 重点防「扫了，但扫错目录」
# ---------------------------------------------------------------------------
def test_install_script_still_has_the_preflight_scan() -> None:
    text = _strip_comments(_read(INSTALL))
    assert "check_secrets.sh" in text, (
        "install.sh 不再做安装前密钥扫描 —— 源码里的密钥会被 rsync 到 /opt/oracles"
    )


def test_install_script_scans_before_it_copies() -> None:
    """扫描必须排在 rsync 到 $APP_DIR **之前** —— 事后扫已经晚了。

    ⚠️ 断言前先剥注释。第一版没剥，于是上面那段解释「为什么要 cd」的注释里
    也写着 `check_secrets.sh`，`_line_of` 命中的是注释而不是调用点 ——
    把整块闸门挪到 rsync 之后，测试照样全绿（变异测试抓出来的）。
    """
    text = _strip_comments(_read(INSTALL))
    scan = _line_of(text, "check_secrets.sh")
    copy = _line_of(text, '"$SRC_DIR/" "$APP_DIR/"')
    assert scan < copy, (
        "密钥扫描排在了 rsync 之后 —— 那时密钥已经躺在 /opt/oracles 里了"
    )


def test_install_script_scans_the_source_tree_not_the_cwd() -> None:
    """⚠️ 每条扫描调用都必须 `cd "$SRC_DIR"`。

    check_secrets.sh 扫的是**当前目录**，它不接受路径参数。install.sh 的注释
    写着「在复制之前先扫一遍 $SRC_DIR」，但不 cd 的话扫的是**调用者站在哪儿**：

      · 站在干净目录（`cd / && sudo bash /tmp/oracles-src/deploy/install.sh`）
        → 扫描通过，源码里的密钥照样被复制进 /opt/oracles。
        **闸门在跑、输出好看、什么都没查** —— 最坏的失效形态。
      · 站在脏目录 → 被无关文件误报，源码干净却中止安装。

    2026-09-20 实测撞上后者：`cd /tmp && sudo bash .../install.sh` 时它扫的是
    /tmp 里上一次救援留下的诊断脚本，安装无端中止。
    """
    text = _read(INSTALL)
    # 只看**真正执行**扫描的行（`bash .../check_secrets.sh`）。
    # 不能只匹配 "check_secrets.sh" —— `if [ -f .../check_secrets.sh ]` 那句
    # 是存在性判断，不执行任何东西，会被误判成「没 cd 的调用点」。
    # 找不到任何调用行时**故意断言失败**：护栏找不到自己要守的东西时应该喊出来。
    invocations = [
        line.strip() for line in _strip_comments(text).splitlines()
        if "bash" in line and "check_secrets.sh" in line
    ]
    assert invocations, (
        "在 install.sh 里找不到任何执行密钥扫描的行 —— 调用方式变了？"
        "请确认闸门还在，并同步更新这条护栏"
    )

    for line in invocations:
        assert 'cd "$SRC_DIR"' in line, (
            f"这一行没 cd 进 $SRC_DIR 就跑了扫描：\n    {line}\n"
            "check_secrets.sh 扫的是当前目录，不 cd 就等于扫「调用者站在哪儿」"
        )


def test_install_rsync_excludes_agree_with_gitignore() -> None:
    """install.sh 的 rsync 排除清单和 .gitignore 必须覆盖同一批产物。

    install.sh 自己写着：「这份清单和 .gitignore 是**同一个意图的两处表达**
    （一个管 git，一个管 rsync），改一处记得改另一处。」

    实测漂移过一次：`.gitignore` 有 `*.egg-info/`，rsync 清单漏了。

    ⚠️ 但别把结论说错：`/opt/oracles/oracles.egg-info/` **不是**从本地同步过去的
    陈旧产物 —— 是第 2 步 `pip install -e "$APP_DIR"` 在**目标机上**生成的
    （实测目录时间戳落在安装过程中）。所以它出现在生产目录里是**正常的**，
    删掉下次安装又回来。rsync 排除它的意义只是「不把本地那份带过去」。

    两边都写对不会让任何测试变红，所以补上这条。

    ⚠️ 断言前必须剥注释。第一版没剥 —— 而 install.sh 里解释这个排除项的注释
    本身就写着 `*.egg-info`，于是**把排除项删掉、测试照样全绿**（变异测试抓出来的）。
    这是本文件里第二次栽在「断言命中的是注释而不是代码」上（另见
    `test_install_script_scans_before_it_copies`）。
    """
    gitignore = _read(ROOT / ".gitignore")
    install = _strip_comments(_read(INSTALL))

    # 只挑「产物类」条目比对 —— 密钥类的排除在 install.sh 里写得更细
    # （`--include '.env.example' --exclude '.env.*'` 这种，没法逐字对应）。
    for artifact in ("__pycache__", ".pytest_cache", ".ruff_cache", "*.egg-info"):
        assert artifact in gitignore, f".gitignore 里缺少 {artifact}"
        assert artifact in install, (
            f"install.sh 的 rsync 排除清单里缺少 {artifact!r} —— "
            "本地的这份产物会跟着同步进 /opt/oracles"
        )


def test_rsync_continuation_block_has_no_comment_lines() -> None:
    """rsync 的 `\\` 续行块里**不能有注释行**。

    bash 里行尾 `\\` 会把换行转义掉，续行上的 `#` **不是注释** ——
    它会变成一个传给 rsync 的实参（一个叫 `#` 的路径），rsync 直接报错。

    2026-09-20 亲手犯过一次：给 `--exclude '*.egg-info'` 补说明时，
    顺手把注释放进了续行里。`bash -n` 只查语法，拦不住这种语义错误。

    修法：注释放到整条命令**上方**，续行块里只留选项。
    """
    lines = _read(INSTALL).splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("rsync -a --delete"))
    # 一直吃到续行结束（不以 \ 结尾的那一行）
    end = start
    while lines[end].rstrip().endswith("\\"):
        end += 1

    block = lines[start + 1:end + 1]
    offenders = [line for line in block if line.lstrip().startswith("#")]
    assert not offenders, (
        "rsync 续行块里出现了注释行 —— `\\` 续行下 `#` 不是注释，"
        "会变成 rsync 的实参：\n    " + "\n    ".join(offenders)
    )



def test_install_final_hint_reads_the_real_write_flag() -> None:
    """结尾那句「写操作开没开」必须读**实际配置**，不能写死。

    2026-09-20 实测：一次升级部署里，脚本结尾照样打印
    「出厂是只读模式（ORACLES_WRITE_ENABLED=false）」，
    而 `/etc/oracles/oracles.env` 里那个值早就是 `true` 了。
    运维照着这句话去 Telegram 里找「打开写操作」—— 找不到，
    反而以为自己漏了哪一步。

    同踩坑 #38：**一句会误导人的提示，比没有提示更糟。**

    ⚠️ 断言前必须剥注释 —— install.sh 里解释这段的注释本身就写着
    `ORACLES_WRITE_ENABLED`。不剥的话，把真正的分支逻辑删掉测试照样绿。
    （本文件里**第三次**栽在「断言命中的是注释而不是代码」上。）
    """
    install = _strip_comments(_read(INSTALL))

    assert "env_value ORACLES_WRITE_ENABLED" in install, (
        "install.sh 结尾没有读**实际**的 ORACLES_WRITE_ENABLED —— "
        "升级部署（写操作本来就开着）时会打印与配置不符的提示"
    )
    assert "ORACLES_WRITE_ENABLED=true" in install, (
        "缺少「写操作已开启」那个分支的文案 —— 只会打印只读那一句"
    )
    assert "出厂是 **只读模式**（ORACLES_WRITE_ENABLED=false）" not in install, (
        "文案里又把结论写死了（`ORACLES_WRITE_ENABLED=false`）—— "
        "它应该跟着配置走，不是常量"
    )


# ---------------------------------------------------------------------------
#  踩坑清单的计数必须与实际条目数一致
# ---------------------------------------------------------------------------
PITS = ROOT / "docs" / "06-踩坑清单.md"
QUICKSTART = ROOT / "docs" / "01-快速开始.md"
README = ROOT / "README.md"

#: 元坑那一节的标题（用来数元坑有几条）
_META_HEADING = "## 🧯 元坑"


def _count_pits(text: str) -> int:
    """数 `### 12. …` 这种编号条目。"""
    return len(re.findall(r"^### \d+\.", text, re.MULTILINE))


def _count_meta_pits(text: str) -> int:
    """数元坑那一节里有几条编号条目（到下一个 `## ` 为止）。"""
    start = text.index(_META_HEADING)
    rest = text[start + len(_META_HEADING):]
    nxt = rest.find("\n## ")
    section = rest if nxt == -1 else rest[:nxt]
    return _count_pits(section)


def test_pit_list_header_count_matches_reality() -> None:
    """🔴 头部写「N 个坑」时，N 必须等于**实际**条目数。

    2026-09-21 实测：头部写着「36 个」，文件里其实有 55 条 ——
    这个数字从某一轮起就再没跟上过。而同一份文档里的 #38 正好写着
    「**一句会误导人的提示，比没有提示更糟**」。

    ⚠️ 这条刻意**不硬编码** 55：硬编码只是把「陈旧」从文档搬到测试里，
      下次加坑又会两边不同步。这里断言的是**两者相等**这个不变量。
    """
    text = _read(PITS)
    actual = _count_pits(text)
    m = re.search(r"^(\d+) 个\*\*实测踩过\*\*的坑", text, re.MULTILINE)
    assert m, "找不到头部那句「N 个实测踩过的坑」—— 它被改写或删掉了？"
    assert int(m.group(1)) == actual, (
        f"踩坑清单头部写 {m.group(1)} 个坑，实际有 {actual} 条。"
        "陈旧计数会让人以为清单很短、不值得读完。"
    )


def test_meta_pit_count_matches_reality() -> None:
    """头部说「元坑（N 条）」，N 必须等于元坑那一节的条目数。"""
    text = _read(PITS)
    actual = _count_meta_pits(text)
    m = re.search(r"元坑\*\*（(\d+) 条）", text)
    assert m, "找不到头部那句「元坑（N 条）」"
    assert int(m.group(1)) == actual, (
        f"头部写元坑 {m.group(1)} 条，实际 {actual} 条"
    )


def test_other_docs_quote_the_same_count() -> None:
    """README 与快速开始引用的坑数必须与清单一致。

    三处各写一个数字、没有一条测试盯着 → 迟早各说各话。
    """
    total = _count_pits(_read(PITS))
    meta = _count_meta_pits(_read(PITS))

    qs = _read(QUICKSTART)
    m = re.search(r"——\s*(\d+) 个实测坑（含 (\d+) 个", qs)
    assert m, "快速开始里那句「—— N 个实测坑（含 M 个…）」找不到了"
    assert int(m.group(1)) == total, f"快速开始写 {m.group(1)} 个坑，实际 {total}"
    assert int(m.group(2)) == meta, f"快速开始写 {m.group(2)} 个元坑，实际 {meta}"

    rd = _read(README)
    m = re.search(r"\|\s*(\d+) 个实测踩过的坑[^|]*?(\d+) 条是", rd)
    assert m, "README 表格里那句「N 个实测踩过的坑…M 条是元坑」找不到了"
    assert int(m.group(1)) == total, f"README 写 {m.group(1)} 个坑，实际 {total}"
    assert int(m.group(2)) == meta, f"README 写 {m.group(2)} 个元坑，实际 {meta}"


def test_pit_numbers_are_unique_and_gapless() -> None:
    """编号必须 1..N 连续无重复 —— 否则「见踩坑 #47」会指到错的地方。

    测试和源码里有**大量** `踩坑清单 #N` 的交叉引用（本次就新增了
    好几处）。编号一旦重复，那些引用会静默指向另一条坑。

    ⚠️ 断言用 ``sorted(nums)`` 而**不是** ``nums``：这份文档的组织约定是
       「编号 = 记录时间顺序，章节 = 危险度」，所以正文里编号是**乱序**的
       （#41~#44 夹在 #20 和 #21 之间）。按文档顺序断言会在正确的内容上失败
       —— 那是测试写错了，不是文档错了。
    """
    nums = [int(x) for x in re.findall(r"^### (\d+)\.", _read(PITS), re.MULTILINE)]
    assert sorted(nums) == list(range(1, len(nums) + 1)), (
        f"编号不连续或有重复：{sorted(nums)}"
    )


def test_every_pit_sits_under_a_severity_heading() -> None:
    """🔴 每条坑都必须落在某个危险度章节里，不能悬在附录之后。

    2026-09-21 实测：`#49~#55` 是直接追加到**文件末尾**的，落在了
    「附：容易混淆的两组概念」后面 —— 一个只读 🔴🟠🟡🧯 四节的读者
    **根本不会看到它们**。内容没丢，但等于不存在。

    这条守的是「有没有被放进某个章节」，不判断放得对不对
    （那需要人读）。悬空的形态是：它上面最近的一个 `## ` 标题
    是「附」或收尾章节。
    """
    text = _read(PITS)
    bad_headings = ("附", "这些坑是怎么被发现的")
    sec = None
    orphans: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^## (.+)", line)
        if m:
            sec = m.group(1).strip()
        mm = re.match(r"^### (\d+)\. (.+)", line)
        if mm and sec and sec.startswith(bad_headings):
            orphans.append(f"#{mm.group(1)} {mm.group(2)[:30]}")
    assert not orphans, (
        "这些坑不在任何危险度章节里（读四节的人看不到它们）：\n  "
        + "\n  ".join(orphans)
    )
