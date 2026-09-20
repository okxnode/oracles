"""日志配置：所有输出自动脱敏。"""
from __future__ import annotations

import logging
import sys

from .redact import scrub


class RedactingFilter(logging.Filter):
    """在日志离开进程之前，把 OCID / 私钥 / token 打码。

    挂在 handler 上而不是 logger 上，这样第三方库（oci、httpx、
    telegram）打的日志也会被过滤掉——OCI SDK 的 DEBUG 日志会打印请求体，
    里面可能带 user_data 之类的敏感内容。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = scrub(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: scrub(v) if isinstance(v, str) else v
                                   for k, v in record.args.items()}
                else:
                    record.args = tuple(
                        scrub(a) if isinstance(a, str) else a for a in record.args
                    )
            if record.exc_text:
                record.exc_text = scrub(record.exc_text)
        except Exception:  # noqa: BLE001 —— 日志绝不能让主流程崩
            pass
        return True


def setup_logging(level: str = "INFO") -> logging.Logger:
    """初始化根 logger。重复调用是幂等的。"""
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # 去掉已有的 handler，避免重复输出
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)

    # 第三方库降噪
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)
    # OCI SDK 的 DEBUG 日志会把整个请求体打出来，强制抬到 INFO
    logging.getLogger("oci").setLevel(logging.WARNING)

    return logging.getLogger("oracles")
