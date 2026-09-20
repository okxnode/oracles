"""守住「谁能用 ``oci`` 的哪一部分」这条边界。

## 背景：文档里曾经写着一句**错的**话

``oci_gateway.py`` 的模块文档原来写：

> **全项目唯一** 直接 import ``oci`` 的地方

2026-09-20 核实：**有 5 处**在 import ``oci`` ——
``oci_gateway.py``、``services/compute.py``、``services/storage.py``、
``services/security.py``（2 处延迟导入）。

一句「唯一」的错误声明比没有声明更糟：下一个人会照着它做判断。
所以这里把**真正的**边界写成可执行的检查，而不是一句注释。

## 真正的边界（按用途分，不按文件分）

| 用途 | 谁可以用 | 为什么 |
| --- | --- | --- |
| 构造客户端（``oci.core.ComputeClient(...)``） | **只有** ``oci_gateway.py`` | 客户端要带统一的超时/重试/认证 |
| 碰 ``oci.config`` / ``oci.auth`` / ``oci.retry`` / ``oci.exceptions`` / ``oci.sign`` | **只有** ``oci_gateway.py`` | 认证与错误翻译一分散就各写一套 |
| 直接调 ``oci.wait_until`` | **只有** ``oci_gateway.py`` | 必须走 ``wait_for_state``（它修了 list/tuple 那个坑） |
| 直接用 ``oci.pagination.*`` | **只有** ``oci_gateway.py`` | 必须走 ``call_all``（统一翻页 + 错误翻译） |
| ``oci.*.models.*`` 纯数据类构造请求体 | 任何 service 模块 | 纯数据类，绕道网关只是多一层转发 |
| ``oci.util.to_dict`` | 任何 service 模块 | 同上 |

## 为什么用 AST 而不是文本匹配

第一版用正则扫，结果被 ``errors.py`` 的一句**文档字符串**误报
（``"把 oci.exceptions.ServiceError 转成一段可读文本"``）——
而 ``errors.py`` 根本不 import ``oci``。

**检查的误报会训练人去忽略它**，所以这里走 ``ast``：
只看真实代码里的属性访问与 import，注释和文档字符串一律不算。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = PROJECT_ROOT / "oracles"
GATEWAY = PKG_DIR / "oci_gateway.py"

#: 只有网关能用 —— 精确到「名字」而不是「前缀」，避免误伤
#: ``oci.core.models.*`` 这类纯数据类。
GATEWAY_ONLY = frozenset({
    "oci.config",
    "oci.auth",
    "oci.retry",
    "oci.exceptions",
    "oci.sign",
    "oci.wait_until",
    "oci.pagination",
})

#: 以这些结尾的，是「客户端构造」，同样只有网关能用。
CLIENT_SUFFIX = "Client"


def _module_files() -> list[Path]:
    return sorted(p for p in PKG_DIR.rglob("*.py") if p != GATEWAY)


def _oci_names(tree: ast.AST) -> set[str]:
    """收集模块里所有以 ``oci`` 为根的属性访问，如 ``oci.core.ComputeClient``。"""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        parts: list[str] = []
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name) and cur.id == "oci":
            found.add("oci." + ".".join(reversed(parts)))
    return found


def _imports_oci(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "oci" or a.name.startswith("oci.") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "oci" or node.module.startswith("oci.")):
                return True
    return False


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad: list[str] = []
    for name in sorted(_oci_names(tree)):
        if name in GATEWAY_ONLY or name.rsplit(".", 1)[-1].endswith(CLIENT_SUFFIX):
            bad.append(name)
    return bad


@pytest.mark.parametrize("path", _module_files(), ids=lambda p: p.name)
def test_services_do_not_construct_clients_or_touch_auth(path: Path) -> None:
    """网关之外的模块不许造客户端、不许碰认证/重试/异常翻译/分页/等待。

    允许的只有 ``oci.*.models.*`` 和 ``oci.util.*`` 这类纯数据工具。
    """
    bad = _violations(path)
    assert not bad, (
        f"{path.relative_to(PROJECT_ROOT)} 越界使用了 {bad}。\n"
        f"  客户端构造 / 认证 / 重试 / 异常翻译 / 分页 / wait_until "
        f"都必须在 oracles/oci_gateway.py 里做 ——\n"
        f"  否则超时、重试、错误文案会各写一套并慢慢漂移。\n"
        f"  （纯数据类 oci.*.models.* 和 oci.util.* 不受此限）"
    )


def test_gateway_still_owns_the_oci_import() -> None:
    """网关必须仍然 import oci —— 否则上面那条会因为「什么都没扫到」而空过。

    ⚠️ 这就是踩坑清单里反复出现的那类失效：**检查跑过了，但结构上不可能给出「否」**。
       所以这里断言「扫描面非空」。
    """
    tree = ast.parse(GATEWAY.read_text(encoding="utf-8"), filename=str(GATEWAY))
    assert _imports_oci(tree), "oci_gateway.py 不再 import oci 了？"
    names = _oci_names(tree)
    assert "oci.wait_until" in names, "网关里找不到 oci.wait_until 的调用"
    assert any(n.endswith(CLIENT_SUFFIX) for n in names), (
        "网关里找不到任何 *Client —— 客户端工厂是不是被搬走了？"
    )


def test_the_scan_actually_covers_every_module() -> None:
    """护栏的「扫描面」也要被断言：模块数不能是 0，也不能漏掉 services/。

    不然有人把 ``PKG_DIR`` 改错、或者目录改名，检查会静默变成空转。
    """
    files = _module_files()
    assert len(files) >= 10, f"只扫到 {len(files)} 个模块，扫描面是不是坏了？"
    rel = {p.relative_to(PKG_DIR).as_posix() for p in files}
    for must in ("services/compute.py", "services/storage.py", "config.py", "errors.py"):
        assert must in rel, f"扫描面里缺 {must}"


def test_violation_detector_actually_fires() -> None:
    """**变异测试**：给检测器一段真的越界代码，它必须报出来。

    没有这一条，「检测器永远返回空」和「代码真的干净」就区分不开。
    """
    src = (
        "import oci\n"
        "def f():\n"
        "    c = oci.core.ComputeClient({})\n"
        "    oci.wait_until(c, None, 'lifecycle_state', 'RUNNING')\n"
        "    return oci.core.models.LaunchInstanceDetails()\n"
    )
    names = _oci_names(ast.parse(src))
    assert "oci.core.ComputeClient" in names, "漏掉了客户端构造"
    assert "oci.wait_until" in names, "漏掉了 wait_until"

    bad = [n for n in names
           if n in GATEWAY_ONLY or n.rsplit(".", 1)[-1].endswith(CLIENT_SUFFIX)]
    assert set(bad) == {"oci.core.ComputeClient", "oci.wait_until"}, (
        f"检测器的判定不对：{sorted(bad)}"
    )
    # 纯数据类必须**不**被判为越界
    assert "oci.core.models.LaunchInstanceDetails" not in bad


def test_docstrings_do_not_trigger_the_guard() -> None:
    """AST 扫描不受文档字符串影响 —— 这是改用 AST 而不是正则的直接原因。

    ``errors.py`` 的文档里写着 ``oci.exceptions.ServiceError``，
    但那个文件并不 import ``oci``，用正则会误报。
    """
    src = '"""文档里提到 oci.exceptions.ServiceError 和 oci.config 都不算越界。"""\n'
    assert _oci_names(ast.parse(src)) == set()
