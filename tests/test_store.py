"""会话存储测试。

守住两条：
  1. 短令牌能正确存取（Telegram 回调数据只有 64 字节，塞不下 OCID）
  2. 一个待确认操作只能被执行一次（防止手快连点两下把销毁跑两遍）
"""
from __future__ import annotations

import time

from oracles.bot.store import TOKEN_TTL, PendingAction, Store


def _pending(token: str = "tok", chat_id: int = 1) -> PendingAction:
    return PendingAction(
        token=token, chat_id=chat_id, user_id=42,
        kind="terminate", account_index=3, summary="删一台机器",
    )


def test_put_and_get_roundtrip():
    store = Store()
    token = store.put(100, "ocid1.instance.oc1..abc")
    assert store.get(100, token) == "ocid1.instance.oc1..abc"


def test_tokens_are_scoped_per_chat():
    """A 会话的令牌不能拿到 B 会话去用。"""
    store = Store()
    token = store.put(100, "payload-a")
    assert store.get(200, token) is None


def test_tokens_are_unique():
    store = Store()
    tokens = {store.put(1, f"v{i}") for i in range(50)}
    assert len(tokens) == 50


def test_get_unknown_token_returns_none():
    assert Store().get(1, "nope") is None


def test_token_length_fits_callback_data():
    """令牌必须足够短 —— Telegram 回调数据上限 64 字节，
    还要给 "im:3:" 这类前缀留空间。"""
    store = Store()
    token = store.put(1, "x")
    assert len(token) <= 12


def test_pending_add_and_get():
    store = Store()
    pending = _pending()
    store.add_pending(pending)
    assert store.get_pending("tok") is pending


def test_claim_succeeds_only_once():
    """核心断言：防止重复执行破坏性操作。"""
    store = Store()
    store.add_pending(_pending())
    assert store.claim(1, "tok") is True
    assert store.claim(1, "tok") is False


def test_claim_unknown_token_returns_false():
    assert Store().claim(1, "ghost") is False


def test_drop_pending_removes_it():
    store = Store()
    store.add_pending(_pending())
    store.drop_pending("tok")
    assert store.get_pending("tok") is None


def test_expired_pending_is_swept():
    store = Store()
    pending = _pending()
    pending.created_at = time.time() - TOKEN_TTL - 10
    store.add_pending(pending)
    assert store.get_pending("tok") is None


def test_expired_token_is_swept():
    store = Store()
    token = store.put(1, "payload")
    store._token_time[1][token] = time.time() - TOKEN_TTL - 10  # noqa: SLF001
    assert store.get(1, token) is None


def test_per_chat_lock_is_stable():
    """同一个会话必须拿到同一把锁。"""
    store = Store()
    assert store.lock_for(7) is store.lock_for(7)
    assert store.lock_for(7) is not store.lock_for(8)


def test_claim_does_not_leak_across_pendings():
    store = Store()
    store.add_pending(_pending("a"))
    store.add_pending(_pending("b"))
    assert store.claim(1, "a") is True
    assert store.claim(1, "b") is True
    assert store.claim(1, "a") is False
