"""对象列举的回归测试。

**这些测试的存在理由**：`list_objects` 踩过两个真实的坑 ——

  1. SDK 返回的是 ``ListObjects`` 模型而不是 list，
     直接迭代会 ``TypeError: 'ListObjects' object is not iterable``
  2. 分页游标在响应体的 ``next_start_with`` 里，不在 ``opc-next-page`` 头里，
     所以通用分页器对它无效

以及一条铁律：**「空桶」和「查不出来」必须是两个结果**。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from oracles.errors import OciApiError
from oracles.services.storage import list_objects


class FakeObj:
    def __init__(self, name: str, size: int):
        self.name = name
        self.size = size
        self.time_created = None


class FakePage:
    """模拟 SDK 的 ListObjects 模型。"""

    def __init__(self, objects, next_start_with=None, prefixes=None):
        self.objects = objects
        self.next_start_with = next_start_with
        self.prefixes = prefixes


class FakeClient:
    """桩客户端：按顺序吐出预置的页。"""

    def __init__(self, pages, namespace="testns"):
        self._pages = list(pages)
        self._namespace = namespace
        self.calls = 0
        self.account = SimpleNamespace(label="[1] 测试账号", index=1)
        self.object_storage = SimpleNamespace(list_objects=lambda **kw: None)

    def namespace(self) -> str:
        return self._namespace

    def call(self, fn, **kwargs):
        self.calls += 1
        return self._pages.pop(0)


def test_empty_bucket_returns_empty_list():
    """空桶 → ``objects == []`` → 返回 []。"""
    client = FakeClient([FakePage(objects=[], prefixes=None)])
    assert list_objects(client, "b") == []


def test_empty_bucket_with_empty_prefixes():
    client = FakeClient([FakePage(objects=[], prefixes=[])])
    assert list_objects(client, "b") == []


def test_objects_are_parsed():
    client = FakeClient([FakePage(objects=[FakeObj("a.txt", 100),
                                           FakeObj("b/c.txt", 2048)])])
    result = list_objects(client, "b")
    assert [o["name"] for o in result] == ["a.txt", "b/c.txt"]
    assert result[1]["size"] == 2048


def test_none_objects_raises_not_treated_as_empty():
    """核心断言：``objects is None`` 必须抛错，**不能**当成空桶。

    历史险情就是这里：把「查不出来」当成「空桶」→ 删掉有数据的桶。
    """
    client = FakeClient([FakePage(objects=None)])
    with pytest.raises(OciApiError, match="不代表空桶"):
        list_objects(client, "b")


def test_none_objects_can_be_softened_explicitly():
    """只有显式传 raise_on_unknown=False 才降级成 []，且要留 warning。"""
    client = FakeClient([FakePage(objects=None)])
    assert list_objects(client, "b", raise_on_unknown=False) == []


def test_pagination_follows_next_start_with():
    """分页靠响应体里的 next_start_with，不是响应头。"""
    client = FakeClient([
        FakePage(objects=[FakeObj("p1", 1)], next_start_with="cursor-1"),
        FakePage(objects=[FakeObj("p2", 2)], next_start_with=None),
    ])
    result = list_objects(client, "b")
    assert [o["name"] for o in result] == ["p1", "p2"]
    assert client.calls == 2


def test_pagination_stops_without_cursor():
    client = FakeClient([FakePage(objects=[FakeObj("only", 1)], next_start_with=None)])
    assert len(list_objects(client, "b")) == 1
    assert client.calls == 1


def test_pagination_three_pages():
    client = FakeClient([
        FakePage(objects=[FakeObj("a", 1)], next_start_with="c1"),
        FakePage(objects=[FakeObj("b", 1)], next_start_with="c2"),
        FakePage(objects=[FakeObj("c", 1)], next_start_with=None),
    ])
    assert [o["name"] for o in list_objects(client, "b")] == ["a", "b", "c"]
    assert client.calls == 3


def test_limit_stops_early():
    client = FakeClient([
        FakePage(objects=[FakeObj("a", 1)], next_start_with="c1"),
        FakePage(objects=[FakeObj("b", 1)], next_start_with="c2"),
    ])
    result = list_objects(client, "b", limit=1)
    assert len(result) == 1
    assert client.calls == 1


def test_prefix_only_result_is_still_empty_bucket():
    """只有前缀（目录占位）没有对象，仍应视为空。"""
    client = FakeClient([FakePage(objects=[], prefixes=["dir1/", "dir2/"])])
    assert list_objects(client, "b") == []
