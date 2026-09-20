"""安全规则判定测试。

22 端口覆盖判定是安全加固的基础 —— 判错了要么漏掉暴露面，
要么误删掉别的规则（update_security_list 是整体替换语义）。
"""
from __future__ import annotations

import base64

import pytest

from oracles.services.security import (
    PLAINTEXT_PATTERNS,
    _decode_user_data,
    _mask_secret_line,
    rule_covers_port,
)


class FakeRange:
    def __init__(self, mn, mx):
        self.min, self.max = mn, mx


class FakeTcp:
    def __init__(self, rng):
        self.destination_port_range = rng


class FakeRule:
    def __init__(self, protocol, tcp=None, source="0.0.0.0/0"):
        self.protocol = protocol
        self.tcp_options = tcp
        self.source = source


def test_tcp_range_covering_22():
    assert rule_covers_port(FakeRule("6", FakeTcp(FakeRange(22, 22))))
    assert rule_covers_port(FakeRule("6", FakeTcp(FakeRange(20, 30))))
    assert rule_covers_port(FakeRule("6", FakeTcp(FakeRange(1, 65535))))


def test_tcp_range_not_covering_22():
    assert not rule_covers_port(FakeRule("6", FakeTcp(FakeRange(80, 80))))
    assert not rule_covers_port(FakeRule("6", FakeTcp(FakeRange(23, 30))))
    assert not rule_covers_port(FakeRule("6", FakeTcp(FakeRange(1, 21))))


def test_all_protocol_covers_everything():
    """协议 all 等于全端口 —— 这是 OCI 默认安全列表的形态之一。"""
    assert rule_covers_port(FakeRule("all"))
    assert rule_covers_port(FakeRule("All"))


def test_none_tcp_options_means_all_ports():
    """⚠️ tcp_options 为 None 表示**全端口**，不是「没有端口」。
    这里判错会把「全网开放 22」误判成「无关规则」。"""
    assert rule_covers_port(FakeRule("6", None))


def test_none_port_range_means_all_ports():
    assert rule_covers_port(FakeRule("6", FakeTcp(None)))


def test_non_tcp_protocol_does_not_cover():
    """UDP(17) / ICMP(1) 不覆盖 TCP 22。"""
    assert not rule_covers_port(FakeRule("17", FakeTcp(FakeRange(22, 22))))
    assert not rule_covers_port(FakeRule("1"))


def test_custom_port_argument():
    assert rule_covers_port(FakeRule("6", FakeTcp(FakeRange(443, 443))), port=443)
    assert not rule_covers_port(FakeRule("6", FakeTcp(FakeRange(443, 443))), port=22)


# --------------------------------------------------------------------------
#  user_data 解码与明文识别
# --------------------------------------------------------------------------
def test_decode_base64_user_data():
    raw = base64.b64encode(b"#!/bin/bash\necho hi").decode()
    assert "echo hi" in _decode_user_data(raw)


def test_decode_plain_text_user_data():
    """有的 user_data 本身就不是 base64，不能崩。"""
    assert "echo hi" in _decode_user_data("#!/bin/bash\necho hi")


def test_decode_empty():
    assert _decode_user_data(None) == ""
    assert _decode_user_data("") == ""


@pytest.mark.parametrize("script,should_match", [
    ("PermitRootLogin yes", True),
    ("PasswordAuthentication yes", True),
    ("echo 'root:P@ssw0rd123' | chpasswd", True),
    ("passwd --stdin ubuntu", True),
    ("password=SuperSecret123", True),
    ("#!/bin/bash\napt-get update", False),
    ("PermitRootLogin no", False),
    ("PasswordAuthentication no", False),
])
def test_plaintext_patterns(script, should_match):
    matched = any(p.search(script) for p, _ in PLAINTEXT_PATTERNS)
    assert matched is should_match


def test_mask_secret_line_hides_password():
    """展示命中片段时必须打码 —— 否则审计报告本身就成了密码泄露渠道。"""
    out = _mask_secret_line("echo 'root:SuperSecret123' | chpasswd")
    assert "SuperSecret123" not in out
    assert "已隐藏" in out
