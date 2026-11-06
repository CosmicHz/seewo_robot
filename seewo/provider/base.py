"""Provider 契约 —— 平台接入的**唯一**扩展点。

一个 Provider 把「某平台的协议」翻译成「平台中立的模型」：
- 上层（核心服务、接口层、集成层）只依赖本契约；
- 换平台 = 新写一个 Provider，核心层零改动；
- 平台没有的能力，用 `PlatformCapabilities` 如实申报，调用时抛
  `CapabilityError`，**不伪造**。

契约分三块：

1. **连接生命周期**：`connect()` / `disconnect()` / `state`
2. **增量拉取**：`poll(cursor, limit)` —— 返回新消息 + 新游标，
   游标语义由 Provider 自己定义（希沃用 msg_id，Satori 用 sn），
   上层只负责原样保存与回传，从而实现断线恢复。
3. **发送**：`send(outgoing)` —— 返回落库后的 `Message`。

本模块不含任何平台专有符号。
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Self

from ..errors import CapabilityError
from ..models import (
    CAPABILITY_FIELD_BY_SEGMENT,
    ConnectionState,
    Message,
    PlatformCapabilities,
    Segment,
    SegmentType,
)


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    """待发送的消息。

    Attributes:
        segments: 消息段序列
        conversation_id: 目标会话 id（Provider 若只有一个会话可忽略）
        reply_to: 被回复消息 id（可选）
        user_id: 目标用户 id（单聊场景）
    """

    segments: tuple[Segment, ...] = ()
    conversation_id: str = ""
    reply_to: str = ""
    user_id: str = ""


@dataclass(frozen=True, slots=True)
class PollResult:
    """一次增量拉取的结果。

    Attributes:
        messages: 新消息（Provider 应已按序返回；核心层会再排一次）
        cursor: 新游标；`None` 表示上游不支持游标，下次从头拉
        has_more: 上游还有更多（核心层可继续拉取）
        raw: 平台原始载荷，调试用
    """

    messages: tuple[Message, ...] = ()
    cursor: str | None = None
    has_more: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class Provider(abc.ABC):
    """平台接入契约。

    子类必须实现：`platform`、`capabilities`、`poll()`、`send()`。
    `connect()` / `disconnect()` 有默认实现（无连接概念的平台可不覆盖）。
    """

    #: 平台标识，全局唯一，如 `seewo`、`fake`
    platform: str = "unknown"

    #: 平台真实能力（必须如实申报）
    capabilities: PlatformCapabilities = PlatformCapabilities()

    def __init__(self) -> None:
        self._state = ConnectionState.DISCONNECTED

    # —— 状态 ——

    @property
    def state(self) -> ConnectionState:
        """当前连接状态。"""
        return self._state

    def _transition(self, target: ConnectionState) -> None:
        """执行状态跃迁，非法跃迁抛 `InvalidStateTransition`。"""
        from ..errors import InvalidStateTransition

        if self._state is target:
            return
        if not self._state.can_transition_to(target):
            raise InvalidStateTransition(
                f"非法的连接状态跃迁: {self._state.value} -> {target.value}"
            )
        self._state = target

    # —— 生命周期 ——

    def connect(self) -> None:
        """建立连接。默认实现：直接置为 CONNECTED。

        需要真实握手的 Provider 覆写本方法，并在失败时置回 DISCONNECTED。
        """
        self._transition(ConnectionState.CONNECTING)
        self._transition(ConnectionState.CONNECTED)

    def disconnect(self) -> None:
        """断开连接。默认实现：置为 DISCONNECTED。"""
        if self._state is ConnectionState.DISCONNECTED:
            return
        self._transition(ConnectionState.CLOSING)
        self._transition(ConnectionState.DISCONNECTED)

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.disconnect()

    # —— 数据面 ——

    @abc.abstractmethod
    def poll(self, cursor: str | None = None, limit: int = 50) -> PollResult:
        """增量拉取消息。

        Args:
            cursor: 上次返回的游标；`None` 表示首次拉取
            limit: 本次最多拉取条数

        Returns:
            PollResult: 新消息 + 新游标
        """

    def send(self, outgoing: OutgoingMessage) -> Message:
        """发送消息（模板方法）。

        这是**唯一**的发送入口，能力闸门在这里强制生效：
        调用方即便直接调 `provider.send()`，也绕不过能力检查 ——
        「不伪造能力」不能依赖上层自觉。

        子类实现 `_send()` 完成平台专有的发送动作。

        Raises:
            CapabilityError: 平台不具备所需能力
            ProviderAuthError: 凭证失效
            ProviderBusinessError: 上游业务拒绝
            ProviderConnectionError: 连接失败
        """
        self.ensure_can_send(outgoing.segments)
        text = "".join(s.text for s in outgoing.segments if s.type is SegmentType.TEXT)
        if text:
            self.ensure_text_length(text)
        return self._send(outgoing)

    @abc.abstractmethod
    def _send(self, outgoing: OutgoingMessage) -> Message:
        """平台专有的发送实现。"""

    # —— 能力校验 ——

    def ensure_can_send(self, segments: tuple[Segment, ...]) -> None:
        """校验所有段都在平台能力范围内，否则抛 `CapabilityError`。

        这是"不伪造能力"的第一道闸门：即便某个 Provider 实现有 bug
        试图发送不支持的段，这里也会拦住。
        """
        for seg in segments:
            if not self.capabilities.supports_segment(seg):
                field_name = CAPABILITY_FIELD_BY_SEGMENT.get(seg.type, "unknown")
                raise CapabilityError(
                    field_name,
                    f"平台 {self.platform} 不支持 {seg.type.value} 类型消息",
                )

    def ensure_text_length(self, text: str) -> None:
        """校验文本长度，超限抛 `CapabilityError`。

        注意：长度超限属于**平台限制**，不是业务错误 —— 调用方可以选择
        自行截断/拆分后重试，因此这里抛能力异常而非业务异常。
        """
        limit = self.capabilities.max_text_length
        if limit and len(text) > limit:
            raise CapabilityError(
                "max_text_length",
                f"文本长度 {len(text)} 超过平台上限 {limit}",
            )

    # —— 可选能力 ——

    def recall(self, message_id: str) -> bool:
        """撤回消息。平台不支持时抛 `CapabilityError`（不静默忽略）。"""
        if not self.capabilities.supports_recall:
            raise CapabilityError("supports_recall", f"平台 {self.platform} 不支持撤回")
        return False

    def history(
        self, conversation_id: str = "", before: str | None = None, limit: int = 50
    ) -> tuple[Message, ...]:
        """拉取历史消息。平台不支持时抛 `CapabilityError`。"""
        if not self.capabilities.supports_history:
            raise CapabilityError(
                "supports_history", f"平台 {self.platform} 不支持拉取历史"
            )
        return ()


class CallbackProvider(Provider):
    """基于回调的 Provider 骨架。

    供「上游是推送式」（如 WebSocket）的平台使用：
    平台把事件推给 `on_message`，Provider 负责入队，
    `poll()` 从队列取走并给出游标。

    这样推送式平台与拉取式平台在核心层看起来完全一样 ——
    核心层只认 `poll()`，不关心上游是轮询还是长连接。
    """

    def __init__(
        self,
        platform: str = "callback",
        capabilities: PlatformCapabilities | None = None,
    ):
        super().__init__()
        self.platform = platform
        if capabilities is not None:
            self.capabilities = capabilities
        self._on_message: Callable[[Message], None] | None = None
        self._queue: list[Message] = []
        self._cursor = 0

    def set_message_callback(self, callback: Callable[[Message], None] | None) -> None:
        """注册消息回调。推送式平台收到事件时调用它。"""
        self._on_message = callback

    def push(self, message: Message) -> None:
        """上游推来一条消息。"""
        if self._on_message is not None:
            self._on_message(message)
        else:
            self._queue.append(message)

    def poll(self, cursor: str | None = None, limit: int = 50) -> PollResult:
        """从队列取走至多 `limit` 条。"""
        messages = tuple(self._queue[:limit])
        del self._queue[: len(messages)]
        self._cursor += len(messages)
        return PollResult(
            messages=messages,
            cursor=str(self._cursor),
            has_more=bool(self._queue),
        )

    def _send(self, outgoing: OutgoingMessage) -> Message:
        """回调式平台的发信由子类实现（基类无默认出站通道）。"""
        raise CapabilityError("send", f"平台 {self.platform} 未实现发送通道")
