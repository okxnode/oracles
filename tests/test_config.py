"""配置加载测试。"""
from __future__ import annotations

import json

import pytest

from oracles.config import Account, load_accounts
from oracles.errors import ConfigError


def _write(tmp_path, payload) -> str:
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_load_valid_accounts(tmp_path):
    key = tmp_path / "k.pem"
    key.write_text("dummy")
    path = _write(tmp_path, [
        {"index": 2, "region": "us-ashburn-1", "user": "ocid1.user.oc1..a",
         "fingerprint": "aa:bb", "tenancy": "ocid1.tenancy.oc1..b",
         "key_file": str(key)},
        {"index": 1, "region": "ap-singapore-1", "user": "ocid1.user.oc1..c",
         "fingerprint": "cc:dd", "tenancy": "ocid1.tenancy.oc1..d",
         "key_file": str(key)},
    ])
    accounts = load_accounts(path, None)
    # 必须按 index 升序
    assert [a.index for a in accounts] == [1, 2]


def test_duplicate_index_rejected(tmp_path):
    key = tmp_path / "k.pem"
    key.write_text("dummy")
    base = {"region": "r", "user": "u", "fingerprint": "f",
            "tenancy": "t", "key_file": str(key)}
    path = _write(tmp_path, [{"index": 1, **base}, {"index": 1, **base}])
    with pytest.raises(ConfigError, match="重复"):
        load_accounts(path, None)


def test_unknown_field_rejected(tmp_path):
    """拼错字段名必须报错，而不是静默忽略。"""
    key = tmp_path / "k.pem"
    key.write_text("dummy")
    path = _write(tmp_path, [{
        "index": 1, "region": "r", "user": "u", "fingerprint": "f",
        "tenancy": "t", "key_file": str(key), "tpyo": "oops",
    }])
    with pytest.raises(ConfigError, match="未知字段"):
        load_accounts(path, None)


def test_missing_key_file_reported(tmp_path):
    path = _write(tmp_path, [{
        "index": 1, "region": "r", "user": "u", "fingerprint": "f",
        "tenancy": "t", "key_file": "/nonexistent/nope.pem",
    }])
    with pytest.raises(ConfigError, match="私钥文件不存在"):
        load_accounts(path, None)


def test_all_problems_reported_at_once(tmp_path):
    """一次把所有问题报出来，不要让人改一个跑一次。"""
    path = _write(tmp_path, [
        {"index": 1, "region": "r"},
        {"index": 2, "region": "r"},
    ])
    with pytest.raises(ConfigError) as exc:
        load_accounts(path, None)
    message = str(exc.value)
    assert "[1]" in message and "[2]" in message


def test_empty_list_rejected(tmp_path):
    path = _write(tmp_path, [])
    with pytest.raises(ConfigError, match="空"):
        load_accounts(path, None)


def test_invalid_json_rejected(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="合法 JSON"):
        load_accounts(str(path), None)


def test_non_list_top_level_rejected(tmp_path):
    path = _write(tmp_path, {"index": 1})
    with pytest.raises(ConfigError, match="数组"):
        load_accounts(path, None)


def test_instance_principal_needs_compartment():
    acc = Account(index=1, region="r", auth_mode="instance_principal")
    problems = acc.validate(None)
    assert any("compartment_id" in p for p in problems)


def test_instance_principal_valid_with_compartment():
    acc = Account(index=1, region="r", auth_mode="instance_principal",
                  compartment_id="ocid1.compartment.oc1..x")
    assert acc.validate(None) == []


def test_invalid_auth_mode_rejected():
    acc = Account(index=1, region="r", auth_mode="magic")
    assert any("auth_mode" in p for p in acc.validate(None))


def test_default_key_file_used_when_account_omits_it(tmp_path):
    key = tmp_path / "global.pem"
    key.write_text("dummy")
    path = _write(tmp_path, [{
        "index": 1, "region": "r", "user": "u",
        "fingerprint": "f", "tenancy": "t",
    }])
    accounts = load_accounts(path, str(key))
    assert accounts[0].key_file is None
    assert accounts[0].validate(str(key)) == []


def test_label_falls_back_to_region():
    acc = Account(index=7, region="ap-seoul-1")
    assert acc.label == "[7] ap-seoul-1"


def test_label_prefers_alias():
    acc = Account(index=7, region="ap-seoul-1", alias="首尔备用")
    assert acc.label == "[7] 首尔备用"
