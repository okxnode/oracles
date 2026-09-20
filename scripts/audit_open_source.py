#!/usr/bin/env python3
"""开源前审计：仓库里有没有真凭据 / 真标识 / 个人信息。

只读。判据都是**形态匹配 + 白名单**，不是「看一眼觉得没问题」。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if not (ROOT / "pyproject.toml").exists():
    # 不静默兜底到某个硬编码路径 —— 那会让「找不到仓库」退化成「扫 0 个文件、
    # 全部通过」，正是本文件要防的那类元坑。找不到就明确报错退出。
    sys.stderr.write(
        f"✗ 找不到仓库根目录（{ROOT} 下没有 pyproject.toml）\n"
        "  这个脚本必须在仓库内的 scripts/ 目录下运行。\n"
    )
    raise SystemExit(2)

# 只扫会被提交的东西（尊重 .gitignore）
r = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
if r.returncode != 0:
    sys.stderr.write(f"✗ `git ls-files` 失败（退出码 {r.returncode}）：{r.stderr.strip()}\n")
    raise SystemExit(2)
tracked = [ROOT / p for p in r.stdout.splitlines() if p.strip()]
print(f"git 已跟踪文件：{len(tracked)} 个\n")

# ⚠️ 「扫了 0 个文件」和「扫了全部文件且全部干净」在输出上长得一样。
#    不把这一条判成失败，整个脚本就会在仓库没配好时**静默全绿**。
if len(tracked) < 10:
    sys.stderr.write(
        f"✗ 只看到 {len(tracked)} 个已跟踪文件 —— 这个数量不合理。\n"
        "  多半是 git 索引为空、或脚本跑在了错的目录里。\n"
        "  拒绝在「几乎什么都没扫」的情况下报绿。\n"
    )
    raise SystemExit(2)

TEXT_SUFFIXES = {".py", ".md", ".sh", ".toml", ".json", ".yml", ".yaml",
                 ".txt", ".example", ".cfg", ".ini", ""}
files = [p for p in tracked
         if p.suffix in TEXT_SUFFIXES and p.is_file()]

#: `--probe-file=PATH`：把仓库外的某个文件也纳入扫描。
#: 这是 `--verify` 用来做注入测试的通道 —— 探针写在临时目录里，
#: **完全不碰 git 索引**（早先的做法是往仓库里 `git add -N` 一个探针文件，
#: 中断时会留下一个 intent-to-add 条目，得手工 `git reset` 才干净）。
_probe_arg = next((a.split("=", 1)[1] for a in sys.argv[1:]
                   if a.startswith("--probe-file=")), None)
if _probe_arg:
    files.append(pathlib.Path(_probe_arg).resolve())


def rel(p: pathlib.Path) -> str:
    """相对路径；仓库外的文件（探针）原样返回。"""
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def read(p: pathlib.Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

# --------------------------------------------------------------------------
#  规则：(名称, 正则, 允许的白名单正则)
#
#  ⚠️ 白名单**必须窄**。写宽了（比如「含 test 就放过」）等于把闸门关掉 ——
#     这是本项目 `check_secrets.sh` 踩过的坑（见 docs/06 元坑那节）。
#     每条白名单都只放行「占位符形态」，不放行「看起来像真的但可能是测试」。
# --------------------------------------------------------------------------
#: RFC 5737 文档专用地址段 —— 文档里**就该**用这些
DOC_IPS = r"(?:192\.0\.2\.\d{1,3}|198\.51\.100\.\d{1,3}|203\.0\.113\.\d{1,3})"

#: 「公网 IPv4」的形态。① 与 ④ **共用同一份定义** ——
#: 同一条规则抄两份必然漂移（本项目栽过：CI 内联了一份规则表，
#: 结果白名单和本地版本分叉，OCID 检查形同虚设）。
PUBLIC_IPV4 = re.compile(
    r"\b(?!127\.|0\.0\.0\.0|255\.|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)"
    r"(?:\d{1,3}\.){3}\d{1,3}\b"
)
#: 上面这条的放行名单：RFC 5737 文档段 + 两个公认的示例地址
IPV4_ALLOW = re.compile(rf"{DOC_IPS}|8\.8\.8\.8|1\.2\.3\.4")

#: 这些文件本身就是「规则的定义」，出现密钥形态是**必然**的
RULE_FILES = ("scripts/check_secrets.sh", "scripts/audit_open_source.py",
              ".gitleaks.toml", "tests/test_redact.py")

#: 占位符标记 —— **只放行明确写着「这是假的」的串**。
#:
#: ⚠️ 2026-09-21 的注入测试撞出来的教训：白名单**不能**用「整行含 fake」
#:    这种宽条件。当时探针里变量名叫 `FAKE`，于是
#:    `FAKE = "8123456789:AAF9…"` 这一行里那个**真形态的 token**
#:    被「这行有 fake 字样」放过去了 —— 闸门看起来在跑，实际放行了。
#:    现在只认这些明确标记，且 `EXAMPLE` 必须**大写或整词**出现。
PLACEHOLDER = re.compile(r"EXAMPLE|example|placeholder|ReplaceMe|SELFTEST_ONLY|"
                         r"secret-scan:allow")

RULES = [
    # 真 OCID 是 **5 段**：ocid1.<类型>.<realm>.<region>.<唯一ID>
    # （tenancy 这类全局资源 region 为空 → `oc1..<id>`，所以中间段允许为空）
    # ⚠️ 第一版写成 4 段，于是**真 OCID 一个都匹配不上** —— 注入测试才发现的。
    ("真 OCID（非占位）",
     re.compile(r"ocid1\.[a-z0-9]+\.[a-z0-9-]+\.[a-z0-9.-]*\.[a-z0-9]{20,}"),
     PLACEHOLDER),

    ("GitHub token",
     re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
     None),

    ("AWS Access Key",
     re.compile(r"AKIA[0-9A-Z]{16}"),
     # `AKIAIOSFODNN7EXAMPLE` 是 AWS 官方文档里的示例值
     re.compile(r"EXAMPLE")),

    ("私钥头",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
     PLACEHOLDER),

    ("Telegram bot token 形态",
     re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
     PLACEHOLDER),

    ("密码形态 Nie########",
     re.compile(r"\bNie\d{8}\b"),
     None),

    ("OpenSSH 公钥（真材实料）",
     re.compile(r"ssh-(rsa|ed25519) AAAA[A-Za-z0-9+/]{60,}"),
     PLACEHOLDER),

    ("个人邮箱",
     re.compile(r"\b[A-Za-z0-9._%+-]+@(gmail|qq|163|outlook|hotmail|foxmail|icloud)\.[A-Za-z]{2,}\b"),
     None),

    ("真实 IPv4",
     re.compile(r"\b(?!127\.|0\.0\.0\.0|255\.|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)"
                r"(?:\d{1,3}\.){3}\d{1,3}\b"),
     re.compile(rf"{DOC_IPS}|8\.8\.8\.8|1\.2\.3\.4")),

    ("OCI 私有 IP 端点",
     re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
     None),

    ("fingerprint 形态（: 分隔的 MD5）",
     re.compile(r"\b(?:[0-9a-f]{2}:){15}[0-9a-f]{2}\b"),
     re.compile(r"(?:ab|cd|ef|12|34|56|78|90|00|ff|aa):", re.I)),
]


# ==========================================================================
#  --verify：规则自检
# ==========================================================================
#
# 为什么必须有这个开关：`check_secrets.sh --verify` 已经证明过一次 ——
# **一个匹配不到任何东西的扫描器比没有扫描器更危险**，它给你虚假的安全感。
# 本项目在密钥扫描器上栽过两次（正则里的 `\b` 在 BSD grep 下静默失效；
# 白名单里的 `aaaa` 把 OCID 检查变成死代码）。
# 那两件事的共同点：**检查在跑，输出是绿的，但什么也没检查。**
#
# 审计脚本的规则是**另一套**实现，同样需要自证。所以这里做注入测试：
# 把「形态与真凭据相同、值不是任何真凭据」的合成串塞进一个临时探针文件，
# 看审计是否报红、**且报出的是对得上号的那条规则**
# （只判「非 0 退出」是不够的 —— 那会让「规则串了」这种错漏过去）。
#
# ⚠️ 本段里**不出现任何真实凭据字面量**。踩坑 #54：审计产物本身可能比
#    被审计对象更危险 —— 2026-09-21 第一版审计脚本里就写过明文密码。
#    IP 探针也刻意**不用真地址**，用 RFC 2544 的测试段。
VERIFY = "--verify" in sys.argv[1:]

if VERIFY:
    import tempfile

    # 合成串：形态与真的完全一致，值不是任何真凭据。
    # 一律**拼装**，不写整串字面量。两个理由，都实测踩过：
    #   1. 审计自己的段④会扫规则文件 → 整串字面量会让审计命中它自己（恒红）；
    #   2. `check_secrets.sh` 也会扫这个文件 → 整串的 OCID / PEM 头会让
    #      主闸门报红。2026-09-21 两处都真踩到了。
    # 所以拆开写：任何**单独一行**都不构成一个完整的凭据形态。
    _OCID = ("ocid1.instance.oc1.xx-doc-1."
             + "anzwsljrshsi77ic2isbqe36houx3sqigvhybo3xfcnmpox2e3q2maawftnq")
    _GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    _TG = "8123456789" + ":" + "AAF9k2LmQ3xYz7Vw8Np1Rs4Tu6Gh0Jk3Mn5B"
    _PEM = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
    # 这一条**可以**整串写：`EXAMPLE` 在审计和 check_secrets.sh 两边都是白名单
    _PEM_OK = "-----BEGIN RSA PRIVATE KEY-----EXAMPLE-----END RSA PRIVATE KEY-----"
    _ALIAS = "ssh oci-" + "77"
    _INST = "oracles-" + "77-" + "1700000000"
    # IP 同理拼装 —— 裸写一个非白名单 IP 会被段④扫到（规则文件自己就是被扫对象）
    _IP_DOC = ".".join(["203", "0", "113", "99"])
    _IP_OTHER = ".".join(["198", "18", "0", "7"])

    def _q(value: str) -> str:
        """生成一行 `A = "<value>"` 的探针内容。"""
        return f'A = "{value}"'

    PROBES = [
        ("真 OCID（非占位）", _q(_OCID), "真 OCID"),
        ("GitHub token", _q(_GH), "GitHub token"),
        ("AWS 文档示例（含 EXAMPLE）", _q("AKIAIOSFODNN7EXAMPLE"), None),
        ("私钥头（真形态）", _q(_PEM), "私钥头"),
        ("私钥头（含 EXAMPLE 的夹具）", _q(_PEM_OK), None),
        ("Telegram token（真形态）", _q(_TG), "Telegram bot token 形态"),
        ("RFC 5737 文档 IP（RFC 5737 文档段）", _q(_IP_DOC), None),
        # 刻意用 RFC 2544 的基准测试段，而不是任何一个真实的公网地址
        ("非白名单 IPv4（RFC 2544 段）", _q(_IP_OTHER), "真实 IPv4"),
        ("密码形态", _q("Nie12345678"), "密码形态"),
        ("个人邮箱", _q("audit-probe-user@qq.com"), "个人邮箱"),
        ("生产主机别名形态", _q(_ALIAS), "生产主机别名"),
        ("真实实例名形态", _q(_INST), "真实实例名"),
    ]

    def run_audit(probe: pathlib.Path | None) -> tuple[int, str]:
        argv = [sys.executable, str(pathlib.Path(__file__).resolve())]
        if probe is not None:
            argv.append(f"--probe-file={probe}")
        r = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def red_rules(out: str) -> set[str]:
        found = set()
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("🔴 ") and "：" in s:
                found.add(s[2:].split("：")[0].strip())
        return found

    print("=" * 74)
    print("🔬 规则自检（每条规则都必须命中它该命中的东西）")
    print("=" * 74)

    rc, _ = run_audit(None)
    if rc != 0:
        sys.stderr.write("🔴 基线就不绿 —— 先修审计脚本，再谈自检\n")
        raise SystemExit(2)
    print("  ✓ 基线：未注入时审计为绿")

    results: list[tuple[str, bool]] = []
    with tempfile.TemporaryDirectory(prefix="oracles-audit-verify-") as _td:
        PROBE = pathlib.Path(_td) / "probe.py"
        for label, payload, expect in PROBES:
            PROBE.write_text(payload + "\n", encoding="utf-8")
            rc, out = run_audit(PROBE)
            rules = red_rules(out)
            if expect is None:
                ok = rc == 0
                verdict = "✅ 正确地放过了（白名单命中）" if ok else f"❌ 误报：{rules}"
            else:
                ok = rc != 0 and any(expect in r for r in rules)
                verdict = (f"✅ 被拦住，命中规则 {expect!r}" if ok
                           else f"❌ 没拦住或规则不对（退出码 {rc}，报红 {rules}）")
            results.append((label, ok))
            print(f"  {verdict}   ← {label}")
    # 探针写在 `tempfile.TemporaryDirectory()` 里，**完全不碰 git 索引**，
    # 所以不存在「中断后留下一个 intent-to-add 条目」的问题。
    # （早先的版本往仓库里 `git add -N` 一个探针文件，中断后得手工 `git reset`。）
    assert not PROBE.exists(), "临时目录没被清理"
    print("\n  ✓ 探针文件已清理（临时目录，从未进入仓库）")

    passed = sum(1 for _l, ok in results if ok)
    print(f"\n🔬 自检结果：{passed}/{len(results)} 通过")
    raise SystemExit(0 if passed == len(results) else 1)


print("=" * 74)
print("① 逐规则扫描")
print("=" * 74)
#: 扫描器**自己**的文件必然包含密钥形态（那是它的规则定义）。
#: 它们的正确性由 `check_secrets.sh --verify`（注入 7 类真密钥看是否命中）保证，
#: 而不是由这里再扫一遍。把它们排除，是为了让本脚本的输出**没有噪音** ——
#: 一个总在报「已知无害」命中的闸门，人会开始忽略它（见 docs/06 元坑那节）。
rule_files = {ROOT / relpath for relpath in RULE_FILES}
scan_files = [p for p in files if p not in rule_files]
print(f"（扫描 {len(scan_files)} 个文件；跳过 {len(files) - len(scan_files)} 个扫描器自身的文件）")

total_hits = 0
for name, pat, allow in RULES:
    hits = []
    for p in scan_files:
        text = read(p)
        for i, line in enumerate(text.splitlines(), 1):
            if pat.search(line):
                if allow and allow.search(line):
                    continue
                hits.append((rel(p), i, line.strip()[:110]))
    if hits:
        total_hits += len(hits)
        print(f"\n🔴 {name}：{len(hits)} 处")
        for rel, ln, snippet in hits[:12]:
            print(f"     {rel}:{ln}")
            print(f"       {snippet}")
        if len(hits) > 12:
            print(f"     … 另有 {len(hits) - 12} 处")
    else:
        print(f"✅ {name}：0 处")

print()
print("=" * 74)
print("② 不该被提交的文件（形态判断）")
print("=" * 74)
BAD_NAMES = (".env", "accounts.json", "oracles.env", "id_rsa", "id_ed25519",
             "login_key", "login_key.pub", ".netrc", ".git-credentials")
bad = [rel(p) for p in tracked
       if p.name in BAD_NAMES or p.suffix == ".pem"
       or p.name.endswith(".key") or "login_key" in p.name]
if bad:
    print(f"🔴 被跟踪的敏感文件名：{bad}")
    total_hits += len(bad)
else:
    print("✅ 没有任何敏感文件名被跟踪")

print()
print("=" * 74)
print("③ .gitignore 是否兜得住（用 git check-ignore 实测，不靠读文件）")
print("=" * 74)
MUST_IGNORE = [
    "oracles.env", "accounts.json", ".env",
    "keys/1/login_key", "keys/1/login_key.pub", "keys/15/login_key",
    "oracles-rescue.pem", "some.pem", "some.key",
    "__pycache__/x.pyc", ".venv/lib/x.py", "oracles.egg-info/PKG-INFO",
    ".ruff_cache/x", ".pytest_cache/x",
]
missed = []
for probe in MUST_IGNORE:
    rr = subprocess.run(["git", "check-ignore", "-q", probe], cwd=ROOT)
    if rr.returncode != 0:
        missed.append(probe)
    print(f"  {'✅' if rr.returncode == 0 else '🔴'} {probe}")
if missed:
    print(f"\n🔴 没被忽略：{missed}")
    total_hits += len(missed)

print()
print("=" * 74)
print("④ 文档里有没有泄露真实资源标识（实例名 / 主机别名 / 公网 IP）")
print("=" * 74)

# ⚠️ 这一段**故意不跳过 RULE_FILES**（与 ① 相反，是有意的设计决策）：
#    ① 跳过规则文件，是因为规则文件里必然出现密钥形态，扫出来全是噪音；
#    但「不该公开的真实标识」是另一回事 —— **规则文件自己就是最容易的泄漏源**
#    （踩坑 #54：审计产物本身成为泄漏源）。
#    所以这一段把规则文件一起扫，它同时就是**本脚本的自检**。
#
# 🔴 由此推出一条硬规则：这一段的标记**一律写成形态**（正则转义 / 字符类），
#    **绝不写字面量**。写死字面量的话，规则会命中它自己的定义 → 审计**恒红**，
#    而一个红久了的检查等于没有检查（大家会开始无脑忽略它）。
#    2026-09-21 实测踩过两次：第一次是把主机别名直接写进 `re.compile(...)`；
#    改成形态之后**注释里还留着那个别名**，于是仍然红 —— 连注释都要用形态写。
#    上面几条 IP 规则之所以一直没事，只是因为它们恰好写成了 `\d\.\d\.\d\.\d`
#    这种转义形态 —— 是运气，不是设计。现在统一成形态匹配。
REAL_MARKERS = [
    # 任何公网 IPv4 都不该出现在要开源的仓库里（文档段地址已放行）。
    # 注意：这里比 ① 多扫了规则文件 —— 见上面的说明。
    ("公网 IP（非文档段）", PUBLIC_IPV4, IPV4_ALLOW),
    # 运维机上的 ssh 别名，形如 `oci-<数字>`。写形态，而不是写死某一个别名。
    ("生产主机别名（oci-<数字>）", re.compile(r"\boci-\d{1,3}\b"), None),
    # 真实实例名，形如 `oracles-<账号>-<时间戳>`。同样是形态。
    ("真实实例名（oracles-<账号>-<时间戳>）",
     re.compile(r"\boracles-\d{1,3}-\d{10,}\b"), None),
]
for name, pat, allow in REAL_MARKERS:
    hits = [(rel(p), i, line.strip()[:100])
            for p in files for i, line in enumerate(read(p).splitlines(), 1)
            if pat.search(line) and not (allow and allow.search(line))]
    if hits:
        total_hits += len(hits)
        print(f"  🔴 {name}：{len(hits)} 处")
        for rel, ln, s in hits[:6]:
            print(f"       {rel}:{ln}  {s}")
    else:
        print(f"  ✅ {name}：0 处")

print()
print("=" * 74)
print(f"  审计结论：{'🔴 有 ' + str(total_hits) + ' 处待处理' if total_hits else '✅ 未发现泄漏'}")
print("=" * 74)
sys.exit(1 if total_hits else 0)
