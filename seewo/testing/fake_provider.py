"""Fake Provider —— 离线测试用的平台替身。

用途：让**业务层与集成层完全脱离真实希沃服务**也能被测试。
它实现完整的 `Provider` 契约，因此契约测试可以**同一套断言**跑在
FakeProvider 与真实 SeewoProvider 上 —— 这是"平台契约不靠自觉"的保证。

同时支持两种投递方式：
- **拉取式**：调用 `emit()` 塞入消息，`poll()` 取走；
- **推送式**：调用 `emit()` 触发回调，供 WebSocket 类适配器测试。
"""

from __future__ import annotations

import itertools
import threading

from ..errors import ProviderAuthError, ProviderBusinessError
from ..models import (
    ConnectionState,
    Message,
    PlatformCapabilities,
    Role,
    Segment,
    SegmentType,
)
from ..provider.base import OutgoingMessage, PollResult, Provider


class FakeProvider(Provider):
    """可编程的假 Provider。

    Args:
        capabilities: 平台能力声明（默认全开，便于测试各能力路径）
        fail_next_send: 让下一次 send 抛出 ProviderBusinessError
        fail_next_poll: 让下一次 poll 抛出 ProviderConnectionError
    """

    def __init__(
        self,
        platform: str = "fake",
        capabilities: PlatformCapabilities | None = None,
    ) -> None:
        super().__init__()
        self.platform = platform
        self.capabilities = capabilities or PlatformCapabilities(
            send_text=True,
            send_image=True,
            send_audio=True,
            send_video=True,
            send_file=True,
            send_mention=True,
            send_quote=True,
            supports_receipt=True,
            supports_recall=True,
            supports_history=True,
        )
        self._inbox: list[Message] = []
        #: 全部投递记录 (序号, 消息) —— 支持用游标回溯，实现 at-least-once 语义
        self._log: list[tuple[int, Message]] = []
        self._sent: list[OutgoingMessage] = []
        self._counter = itertools.count(1)
        self._lock = threading.Lock()
        self._fail_next_send: Exception | None = None
        self._fail_next_poll: Exception | None = None
        self._auth_expired = False
        self.refresh_calls = 0
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.remembered: list[str] = []
        #: 每条入站消息的序号（用于实现真正的游标恢复）
        self._seq = 0
        self._seq_of: dict[str, int] = {}

    # —— 测试控制 ——

    def emit(self, message: Message) -> None:
        """投递一条入站消息（模拟平台推来）。"""
        with self._lock:
            self._seq += 1
            self._seq_of[message.id] = self._seq
            self._log.append((self._seq, message))
            self._inbox.append(message)

    def emit_text(
        self,
        text: str,
        *,
        message_id: str = "",
        conversation_id: str = "c1",
        user_id: str = "u1",
        timestamp: int = 0,
    ) -> Message:
        """便捷方法：投递一条纯文本消息并返回它。"""
        mid = message_id or f"m{next(self._counter)}"
        msg = Message(
            id=mid,
            platform=self.platform,
            conversation_id=conversation_id,
            user_id=user_id,
            segments=(Segment.text_seg(text),),
            timestamp=timestamp,
        )
        self.emit(msg)
        return msg

    def fail_next_send(self, error: Exception | None = None) -> None:
        """让下一次 send 失败。"""
        self._fail_next_send = error or ProviderBusinessError("注入的发送失败")

    def fail_next_poll(self, error: Exception | None = None) -> None:
        """让下一次 poll 失败。"""
        self._fail_next_poll = error or ProviderBusinessError("注入的拉取失败")

    def expire_auth(self) -> None:
        """让下一次认证类操作失败。"""
        self._auth_expired = True

    @property
    def sent(self) -> tuple[OutgoingMessage, ...]:
        """已发送的消息。"""
        return tuple(self._sent)

    # —— Provider 契约 ——

    def connect(self) -> None:
        self.connect_calls += 1
        super().connect()

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        super().disconnect()

    def refresh_session(self) -> bool:
        """刷新凭证。`expire_auth` 置位时返回 False。"""
        self.refresh_calls += 1
        return not self._auth_expired

    def poll(self, cursor: str | None = None, limit: int = 50) -> PollResult:
        """拉取消息。

        游标语义：记录「已拉取到第几条」。带游标调用时**只返回游标之后**
        的消息，从而可以测试断线恢复（断连期间投递的消息，重连后能补到）。

        投递语义为 **at-least-once**：用较旧的游标再次调用会**重复投递**
        已发过的消息 —— 与真实平台一致，也正是核心层去重机制要防的情况。
        """
        if self._fail_next_poll is not None:
            error = self._fail_next_poll
            self._fail_next_poll = None
            raise error
        start_seq = 0
        if cursor:
            try:
                start_seq = int(cursor)
            except (TypeError, ValueError):
                start_seq = 0
        with self._lock:
            candidates = [(s, m) for s, m in self._log if s > start_seq]
            taken = candidates[:limit]
            new_cursor = taken[-1][0] if taken else start_seq
            has_more = len(candidates) > len(taken)
            messages = tuple(m for _, m in taken)
        return PollResult(
            messages=messages,
            cursor=str(new_cursor),
            has_more=has_more,
        )

    def _send(self, outgoing: OutgoingMessage) -> Message:
        if self._fail_next_send is not None:
            error = self._fail_next_send
            self._fail_next_send = None
            raise error
        with self._lock:
            self._sent.append(outgoing)
            mid = f"out{next(self._counter)}"
        return Message(
            id=mid,
            platform=self.platform,
            conversation_id=outgoing.conversation_id,
            user_id=outgoing.user_id,
            role=Role.SELF,
            segments=outgoing.segments,
            timestamp=0,
        )

    def recall(self, message_id: str) -> bool:
        if not self.capabilities.supports_recall:
            from ..errors import CapabilityError

            raise CapabilityError("supports_recall", "fake 不支持撤回")
        self.remembered.append(message_id)
        return True

    def history(
        self, conversation_id: str = "", before: str | None = None, limit: int = 50
    ) -> tuple[Message, ...]:
        """平台侧历史：按投递顺序返回已投递的消息。

        - ``conversation_id`` 非空时只取该会话
        - ``before`` 为消息 id 时只取它**之前**投递的消息（不含自身）
        - 返回最近的 ``limit`` 条，旧→新
        """
        if not self.capabilities.supports_history:
            from ..errors import CapabilityError

            raise CapabilityError("supports_history", "fake 不支持拉取历史")
        with self._lock:
            log = list(self._log)
            cutoff = self._seq_of.get(before) if before else None
        if before and cutoff is None:
            # 未知的 before 游标：无法确定「之前」是哪些，不返回全部冒充结果
            return ()
        selected = [
            m
            for seq, m in log
            if (not conversation_id or m.conversation_id == conversation_id)
            and (cutoff is None or seq < cutoff)
        ]
        if limit > 0:
            selected = selected[-limit:]
        return tuple(selected)


__all__ = ["ConnectionState", "FakeProvider", "ProviderAuthError", "SegmentType"]
