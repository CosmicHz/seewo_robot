"""Store 契约 —— 消息持久化。

核心服务只依赖本协议，不关心数据落在内存还是磁盘、是不是 JSON 文件。
两种实现：
- `MemoryStore`：进程内，测试与轻量场景
- `JsonStore`：JSON 文件，兼容旧 `chat_history.json` 格式

**与旧实现的兼容**：`JsonStore` 读写的就是旧格式
（`{"messages": [ ... ]}`，消息字段同名），旧文件继续可读，新旧进程可互换。
"""

from __future__ import annotations

import abc
import json
import os
import tempfile
from collections.abc import Iterable, Iterator

from ..errors import CorruptStoreError, StoreError
from ..models import Message


class Store(abc.ABC):
    """消息存储契约。"""

    @abc.abstractmethod
    def append(self, messages: Iterable[Message]) -> int:
        """追加消息，返回实际写入条数。

        实现必须保证：**按 `sort_key` 有序且不重复**（同 id 覆盖而非追加）。
        """

    @abc.abstractmethod
    def load(self, limit: int = 0, offset: int = 0) -> tuple[Message, ...]:
        """按顺序读出消息。

        Args:
            limit: 最多返回条数；0 表示不限
            offset: 跳过前 N 条
        """

    @abc.abstractmethod
    def contains(self, message_id: str) -> bool:
        """判断某 id 是否已存在（去重查询）。"""

    @abc.abstractmethod
    def last_cursor(self) -> str | None:
        """返回上次持久化的游标（断线恢复用）；无则 `None`。"""

    @abc.abstractmethod
    def save_cursor(self, cursor: str | None) -> None:
        """保存游标。"""

    def __len__(self) -> int:
        return len(self.load())


class MemoryStore(Store):
    """内存实现。按 `sort_key` 维护有序列表，追加时按 id 去重。"""

    def __init__(self) -> None:
        self._messages: dict[str, Message] = {}
        self._cursor: str | None = None

    def append(self, messages: Iterable[Message]) -> int:
        written = 0
        for msg in messages:
            if msg.id in self._messages:
                # 同 id 已存在：保留先到的（幂等重放），不产生重复
                continue
            self._messages[msg.id] = msg
            written += 1
        return written

    def load(self, limit: int = 0, offset: int = 0) -> tuple[Message, ...]:
        ordered = sorted(self._messages.values(), key=lambda m: m.sort_key)
        if offset:
            ordered = ordered[offset:]
        if limit:
            ordered = ordered[:limit]
        return tuple(ordered)

    def contains(self, message_id: str) -> bool:
        return message_id in self._messages

    def last_cursor(self) -> str | None:
        return self._cursor

    def save_cursor(self, cursor: str | None) -> None:
        self._cursor = cursor


class JsonStore(Store):
    """JSON 文件实现，兼容旧 `chat_history.json` 格式。

    格式：``{"messages": [ {消息字段...} ], "cursor": "..."}``

    - 旧文件没有 `cursor` 字段 → 视为无游标（不影响读取旧数据）
    - `cursor` 放在**独立文件**里（由 `cursor_path` 指定），
      避免与旧格式竞争、也避免每次写消息都重写游标
    - 写文件用「临时文件 + os.replace」保证原子性，避免进程中断留下半个文件
    """

    def __init__(self, path: str, cursor_path: str | None = None) -> None:
        self.path = path
        self.cursor_path = cursor_path
        self._messages: dict[str, Message] = {}
        self._loaded = False

    # —— 内部 ——

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise CorruptStoreError(
                f"消息存储文件无法解析，请修复或删除后重建（sync_all 可重新拉取全量）: "
                f"{self.path} → {e}"
            ) from e
        if not isinstance(data, dict):
            raise CorruptStoreError(f"消息存储文件结构非法（应为对象）: {self.path}")
        if "messages" not in data:
            raise CorruptStoreError(f"消息存储文件缺少 messages 列表: {self.path}")
        raw_messages = data["messages"]
        if not isinstance(raw_messages, list):
            raise CorruptStoreError(f"消息存储文件缺少 messages 列表: {self.path}")
        for item in raw_messages:
            if not isinstance(item, dict):
                raise CorruptStoreError(f"消息存储文件元素不是对象: {self.path}")
            msg = Message.from_dict(item)
            self._messages[msg.id] = msg

    def _ordered(self) -> list[Message]:
        self._ensure_loaded()
        return sorted(self._messages.values(), key=lambda m: m.sort_key)

    # —— Store 接口 ——

    def append(self, messages: Iterable[Message]) -> int:
        self._ensure_loaded()
        written = 0
        for msg in messages:
            if msg.id in self._messages:
                continue
            self._messages[msg.id] = msg
            written += 1
        if written:
            self._flush()
        return written

    def load(self, limit: int = 0, offset: int = 0) -> tuple[Message, ...]:
        ordered = self._ordered()
        if offset:
            ordered = ordered[offset:]
        if limit:
            ordered = ordered[:limit]
        return tuple(ordered)

    def contains(self, message_id: str) -> bool:
        self._ensure_loaded()
        return message_id in self._messages

    def last_cursor(self) -> str | None:
        if not self.cursor_path or not os.path.exists(self.cursor_path):
            return None
        try:
            with open(self.cursor_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        cursor = data.get("cursor") if isinstance(data, dict) else None
        return str(cursor) if cursor else None

    def save_cursor(self, cursor: str | None) -> None:
        if not self.cursor_path:
            return
        payload = json.dumps({"cursor": cursor}, ensure_ascii=False)
        _atomic_write(self.cursor_path, payload)

    def _flush(self) -> None:
        """原子写盘。"""
        payload = json.dumps(
            {"messages": [m.to_dict() for m in self._ordered()]},
            ensure_ascii=False,
            indent=2,
        )
        try:
            _atomic_write(self.path, payload)
        except OSError as e:
            raise StoreError(f"写入消息存储失败: {self.path} → {e}") from e

    def reload(self) -> None:
        """丢弃内存缓存，下次访问重新从磁盘读。"""
        self._loaded = False
        self._messages = {}


def _atomic_write(path: str, content: str) -> None:
    """原子写文件：先写同目录临时文件，再 os.replace 覆盖。

    避免进程在写入中途被杀导致目标文件只剩半个 JSON。
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def iter_messages(store: Store) -> Iterator[Message]:
    """便捷迭代器。"""
    yield from store.load()
