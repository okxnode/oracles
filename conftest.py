"""pytest 全局配置：把仓库根加进 sys.path。

这样下面三种调用方式都能 import 到 `oracles` 和 `tests.telegram_harness`：

    pytest tests/            # 裸命令（CI 用这个）
    python -m pytest tests/  # 本地常用
    pytest                   # 靠 pyproject 的 testpaths

⚠️ 为什么不能只靠 `python -m pytest`：
   `-m` 会把当前工作目录塞进 sys.path，于是本地跑得好好的，
   CI 上跑 `pytest tests/` 就 ImportError。
   CI 里虽然有 `pip install -e .` 装好 `oracles`，
   但 `tests/telegram_harness.py` 是测试辅助模块，装不进去 —— 所以这里显式兜住。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
