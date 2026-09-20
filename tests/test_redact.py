"""脱敏模块测试 —— 这是开源安全的底线，必须有测试守着。"""
from __future__ import annotations

from oracles.redact import mask, scrub, short_ocid

FAKE_OCID = "ocid1.tenancy.oc1..aaaaaaaabbbbccccddddeeeeffffgggghhhhiiiijjjjkkkk"  # secret-scan:allow
# ⚠️ 以下全部是**明显虚构**的值，不是任何真实凭据。
#    千万别从自己的配置里抄真实值进来 —— 这个仓库要开源。
FAKE_FP = "00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff"
FAKE_TOKEN = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"  # secret-scan:allow
FAKE_PEM = (  # 虚构的 PEM 结构，专门用来验证脱敏
    "-----BEGIN PRIVATE KEY-----\n"  # secret-scan:allow
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj\n"
    "-----END PRIVATE KEY-----"
)
FAKE_S3_ACCESS = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def test_scrub_masks_ocid():
    out = scrub(f"账号租户是 {FAKE_OCID} 请勿外传")
    assert FAKE_OCID not in out
    assert "<OCID 已隐藏>" in out


def test_scrub_masks_fingerprint():
    out = scrub(f"fingerprint={FAKE_FP}")
    assert FAKE_FP not in out
    assert "<指纹已隐藏>" in out


def test_scrub_masks_telegram_token():
    out = scrub(f"token={FAKE_TOKEN}")
    assert FAKE_TOKEN not in out
    assert "已隐藏" in out


def test_scrub_masks_pem_block():
    out = scrub(f"私钥内容：\n{FAKE_PEM}\n结束")
    assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj" not in out
    assert "<PEM 私钥已隐藏>" in out


def test_scrub_masks_s3_access_key():
    out = scrub(f"Access Key: {FAKE_S3_ACCESS}")
    assert FAKE_S3_ACCESS not in out


def test_scrub_keeps_normal_text():
    text = "账号 3 在 ap-singapore-1，还有 2 台可开"
    assert scrub(text) == text


def test_scrub_handles_empty():
    assert scrub("") == ""
    assert scrub(None) is None  # type: ignore[arg-type]


def test_mask_keeps_head_and_tail():
    out = mask("ocid1.tenancy.oc1..aaaaaaaabbbbcccc", head=6, tail=4)
    assert out.startswith("ocid1.")
    assert out.endswith("cccc")
    assert "…" in out


def test_mask_short_value_is_fully_hidden():
    assert mask("ab") == "a*"


def test_short_ocid_shows_type_and_tail():
    # 虚构 OCID，用来验证压缩显示
    out = short_ocid("ocid1.instance.oc1.ap-singapore-1.anuwxyz123456")  # secret-scan:allow
    assert out.startswith("instance…")
    assert out.endswith("123456")


def test_scrub_is_idempotent():
    once = scrub(f"{FAKE_OCID} 和 {FAKE_FP}")
    assert scrub(once) == once
