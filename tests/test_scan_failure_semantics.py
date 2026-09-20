"""扫描失败 vs 扫描干净 —— 这两种结果必须能区分开。

这是整个项目最贵的一条教训，值得单独一个测试文件：

    []    = 扫描成功，确实什么都没有
    None  = 扫描失败，结果**未知**

一开始代码里写的是 ``on_error=lambda c, exc: []``。后果是某个账号鉴权过期时，
它会被渲染成「该账号很干净」——在计费审计里漏掉正在扣费的未绑定公网 IP，
在安全审计里漏掉对全网开放的 22 端口，**而且完全不报警**。

这些测试就是防止有人哪天又把 ``[]`` 写回去。
"""
from __future__ import annotations

import re
from pathlib import Path

from oracles.services import scan_failed
from oracles.services.audit import render_leaks
from oracles.services.security import render_exposure, render_plaintext

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
#  scan_failed 钩子本身
# ---------------------------------------------------------------------------
def test_scan_failed_returns_none_not_empty_list():
    """核心断言：失败必须返回 None。

    如果有人把它改成 ``return []``，这个测试会挂 —— 那正是我们要拦住的。
    """
    got = scan_failed(None, RuntimeError("鉴权过期"))
    assert got is None
    assert got != []


def test_scan_failed_tolerates_any_exception():
    for exc in (RuntimeError("x"), ValueError("y"), TimeoutError(), Exception()):
        assert scan_failed(None, exc) is None


# ---------------------------------------------------------------------------
#  render_exposure
# ---------------------------------------------------------------------------
def test_render_exposure_marks_failed_accounts():
    text = render_exposure({"账号A": [], "账号B": None})
    # 失败必须显式说出来
    assert "扫描失败" in text
    assert "账号B" in text
    # 不能把失败算成"已扫账号"
    assert "1 个账号" in text


def test_render_exposure_failure_is_not_reported_as_clean():
    """只有失败、没有成功时，绝不能输出"没有对全网开放的 22 端口"。"""
    text = render_exposure({"账号A": None})
    assert "✅ 没有对全网开放的 22 端口。" not in text
    assert "结果未知" in text


def test_render_exposure_all_clean_says_clean():
    text = render_exposure({"账号A": [], "账号B": []})
    assert "✅ 没有对全网开放的 22 端口。" in text
    assert "扫描失败" not in text


def test_render_exposure_empty_result_is_clean_not_unknown():
    """空 dict（没有任何账号）走的是"干净"分支，不是"未知"分支。"""
    text = render_exposure({})
    assert "✅" in text
    assert "扫描失败" not in text


# ---------------------------------------------------------------------------
#  render_plaintext
# ---------------------------------------------------------------------------
def test_render_plaintext_marks_failed_accounts():
    text = render_plaintext({"账号A": None})
    assert "扫描失败" in text
    assert "账号A" in text
    # 不能说"没有发现"就收工 —— 那是把未知说成没问题
    assert "结论不完整" in text


def test_render_plaintext_clean_still_works():
    text = render_plaintext({"账号A": []})
    assert "✅ 没有发现。" in text
    assert "扫描失败" not in text


# ---------------------------------------------------------------------------
#  render_leaks
# ---------------------------------------------------------------------------
class _FakeLeak:
    """最小 LeakItem 替身，避免为了构造真对象而牵扯 SDK 模型。"""

    severity = "critical"
    category = "预留公网 IP"
    name = "WordPress_public_ip"
    detail = "未绑定，持续计费"


def test_render_leaks_marks_failed_accounts():
    text = render_leaks({"账号A": [], "账号B": None})
    assert "审计失败" in text
    assert "账号B" in text
    assert "结果未知" in text


def test_render_leaks_failure_is_not_reported_as_clean():
    text = render_leaks({"账号A": None})
    assert "✅ 没有发现残留项。" not in text
    assert "结论不完整" in text


def test_render_leaks_reports_critical_count():
    text = render_leaks({"账号A": [_FakeLeak()]})
    assert "1 项正在直接计费" in text
    assert "WordPress_public_ip" in text


def test_render_leaks_all_clean():
    text = render_leaks({"账号A": [], "账号B": []})
    assert "✅ 没有发现残留项。" in text
    assert "审计失败" not in text


# ---------------------------------------------------------------------------
#  接线测试：调用方必须真的传 scan_failed
#
#  上面那些测试锁住的是「渲染层区分 None 和 []」，但挡不住有人把调用方
#  改回 ``on_error=lambda c, exc: []`` —— 那样渲染层永远收不到 None，
#  上面全部测试照样绿。所以这里直接扫源码。
# ---------------------------------------------------------------------------
_BAD_HOOKS = [
    re.compile(r"on_error\s*=\s*lambda[^:]*:\s*\[\s*\]"),
    re.compile(r"on_error\s*=\s*lambda[^:]*:\s*\([^()]*,\s*\[\s*\]\s*\)"),
]

# 这些是必须被上面正则命中的样本。**正则失效时必须有人发现** ——
# 一个匹配不到东西的检查比没有检查更危险（本项目已经栽过一次）。
_BAD_SAMPLES = [
    "on_error=lambda c, exc: [],",
    "on_error=lambda i, exc: (i, []),",
]


def _blank_strings_and_comments(src: str) -> str:
    """把注释和字符串字面量抹成空格，保留行号与列位置。

    ⚠️ 为什么不能简单地"跳过整行"：
       ``x = 1  # 注释`` 这种行既有代码又有注释，整行跳过会漏掉真代码。
       所以要按 token 的字符区间精确抹白。

    ⚠️ 为什么必须抹白：
       本文件的 docstring 里就写着那个坏模式（"不要这么写：on_error=lambda ..."），
       不抹掉的话检查会命中自己的说明文字。
    """
    import io
    import tokenize

    buf = [list(line) for line in src.splitlines()]
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                continue
            (sr, sc), (er, ec) = tok.start, tok.end
            for ln in range(sr, er + 1):
                if ln - 1 >= len(buf):
                    continue
                row = buf[ln - 1]
                start = sc if ln == sr else 0
                end = ec if ln == er else len(row)
                for col in range(start, min(end, len(row))):
                    row[col] = " "
    except (tokenize.TokenError, IndentationError):
        return src
    return "\n".join("".join(row) for row in buf)


def test_blanker_removes_docstrings_and_comments():
    """自检：抹白函数真的有效，否则下面的接线测试会被自己的注释干扰。"""
    src = (
        '"""说明：不要写 on_error=lambda c, exc: []"""\n'
        "x = 1  # on_error=lambda c, exc: []\n"
        "y = 2\n"
    )
    blanked = _blank_strings_and_comments(src)
    assert "on_error" not in blanked
    assert "y = 2" in blanked


def test_bad_hook_patterns_actually_match():
    """自检：正则本身必须有效。否则下面的接线测试是假绿。"""
    for pattern in _BAD_HOOKS:
        assert any(pattern.search(s) for s in _BAD_SAMPLES), (
            f"这个正则匹配不到任何已知坏样本，接线测试形同虚设：{pattern.pattern}")


def test_no_caller_swallows_scan_errors_into_empty_list():
    """源码级检查：不允许出现 on_error 返回 [] 的写法。"""
    offenders: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if "tests" in rel.parts or ".pytest_tmp" in rel.parts:
            continue
        # macOS 同步会留下 ._* 的 AppleDouble 元数据文件（不是源码，也不是 UTF-8），跳过
        if any(part.startswith("._") for part in rel.parts):
            continue
        code = _blank_strings_and_comments(path.read_text(encoding="utf-8"))
        for n, line in enumerate(code.splitlines(), 1):
            if any(p.search(line) for p in _BAD_HOOKS):
                offenders.append(f"{rel}:{n}: {line.strip()}")
    assert not offenders, (
        "发现把扫描失败吞成空列表的写法 —— 应该用 scan_failed（返回 None），\n"
        "否则失败的账号会被渲染成「该账号很干净」：\n  " + "\n  ".join(offenders)
    )
