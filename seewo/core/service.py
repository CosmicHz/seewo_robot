"""核心服务层：平台中立的业务中枢。

`MessageService` 是整个系统唯一持有"消息怎么进来、怎么出去、怎么存、
怎么派发"逻辑的地方。它**不认识任何平台**：
- 不知道希沃的 `resUrl`、数字 type、`statusCode`；
- 不知道 OneBot 的 `post_type`、Satori 的 `sn`。

它只依赖三样东西：
- `Provider` 契约（`seewo/provider/base.py`）
- `Store` 契约（`seewo/core/store.py`）
- 平台中立模型（`seewo/models.py`）

因此接一个新平台 = 写一个 Provider；接一个新框架 = 写一个适配器。
两者都不需要碰这里。

本层解决的具体问题：

===================  ==================================================
问题                 做法
===================  ==================================================
重复投递             `store.contains` + 有界 LRU 双重去重
乱序到达             统一按 `(timestamp, id)` 复合键排序，同毫秒也确定
断线恢复             游标（cursor）由 Store 持久化，重连后续拉
并发                入站单写者锁 + 事件串行派发，保证同序
状态一致性           连接状态机拒绝非法跃迁
错误传播             Provider 异常 → `ERROR` 事件 + 上抛，不吞
认证失效             刷新凭证后**重试一次**，仍失效则 `SessionExpiredError`
===================  ==================================================
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable

from ..errors import (
    ProviderAuthError,
    ProviderError,
    SeewoError,
    SessionExpiredError,
)
from ..models import (
    ConnectionState,
    Event,
    EventType,
    Message,
    Role,
    Segment,
)
from ..provider.base import OutgoingMessage, PollResult, Provider
from .store import MemoryStore, Store

logger = logging.getLogger("seewo.core")

#: 事件处理器签名：接收 Event，返回 None。
EventHandler = Callable[[Event], None]

#: 去重 LRU 的默认容量。足够覆盖一轮轮询 + 一次重连窗口。
DEFAULT_SEEN_CAPACITY = 4096


class MessageService:
    """消息服务。

    Args:
        provider: 平台接入对象
        store: 消息存储（默认内存）
        seen_capacity: 去重 LRU 容量
        now_ms: 取当前毫秒时间戳的函数（测试可注入）
    """

    def __init__(
        self,
        provider: Provider,
        store: Store | None = None,
        *,
        seen_capacity: int = DEFAULT_SEEN_CAPACITY,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._provider = provider
        self._store: Store = store if store is not None else MemoryStore()
        self._seen_capacity = seen_capacity
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._handlers: dict[EventType, list[EventHandler]] = {}
        self._any_handlers: list[EventHandler] = []

        self._lock = threading.RLock()
        #: 可重入：`poll_once` 持锁后会调用同样持锁的公开 `ingest`
        self._ingest_lock = threading.RLock()
        self._dispatch_lock = threading.RLock()

        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._session_expired = False

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def provider(self) -> Provider:
        return self._provider

    @property
    def store(self) -> Store:
        return self._store

    @property
    def capabilities(self):
        """平台能力声明。"""
        return self._provider.capabilities

    @property
    def state(self) -> ConnectionState:
        """连接状态。"""
        return self._provider.state

    @property
    def session_expired(self) -> bool:
        """会话是否已失效（需重新认证）。"""
        return self._session_expired

    # ------------------------------------------------------------------
    # 事件订阅
    # ------------------------------------------------------------------

    def on(self, event_type: EventType, handler: EventHandler) -> None:
        """订阅某类事件。"""
        with self._lock:
            self._handlers.setdefault(event_type, []).append(handler)

    def on_any(self, handler: EventHandler) -> None:
        """订阅所有事件。"""
        with self._lock:
            self._any_handlers.append(handler)

    def off_any(self, handler: EventHandler) -> None:
        """取消订阅所有事件。

        长连接场景（SSE / WebSocket）断开时必须调用，否则 handler 列表会
        随连接数无限增长。
        """
        with self._lock:
            try:
                self._any_handlers.remove(handler)
            except ValueError:
                pass

    def _dispatch(self, event: Event) -> None:
        """派发事件。

        用独立的 `_dispatch_lock` 串行化：**保证事件顺序与派发顺序一致**，
        这是"消息顺序可重现"的关键。处理器抛异常不会中断其它处理器，
        但会被记录并转成 `ERROR` 事件。
        """
        with self._dispatch_lock:
            with self._lock:
                handlers = list(self._handlers.get(event.type, ()))
                handlers.extend(self._any_handlers)
            for handler in handlers:
                try:
                    handler(event)
                except Exception as e:
                    logger.exception("事件处理器抛异常: %s", handler)
                    if event.type is EventType.ERROR:
                        # ERROR 事件自身处理失败时不再派发 ERROR：
                        # 否则一个恒抛异常的 on_any 处理器会递归到 RecursionError
                        continue
                    self._emit_error(
                        f"事件处理器异常: {e}", data={"event_type": event.type.value}
                    )

    def _emit_error(self, message: str, *, data: dict | None = None) -> Event:
        event = Event(
            type=EventType.ERROR,
            timestamp=self._now_ms(),
            error=message,
            data=data or {},
        )
        self._dispatch(event)
        return event

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """连接平台。失败时派发 `ERROR` 并把状态退回 DISCONNECTED。"""
        try:
            self._provider.connect()
        except ProviderAuthError as e:
            self._session_expired = True
            self._emit_error(f"认证失败: {e}", data={"auth_expired": True})
            raise
        except ProviderError as e:
            self._emit_error(f"连接失败: {e}", data={"retryable": e.retryable})
            raise
        self._session_expired = False
        self._dispatch(
            Event(
                type=EventType.CONNECTED,
                timestamp=self._now_ms(),
                data={"platform": self._provider.platform},
            )
        )

    def disconnect(self) -> None:
        """断开平台连接。"""
        try:
            self._provider.disconnect()
        except ProviderError as e:
            logger.warning("断开连接时出错: %s", e)
        self._dispatch(
            Event(
                type=EventType.DISCONNECTED,
                timestamp=self._now_ms(),
                data={"platform": self._provider.platform},
            )
        )

    # ------------------------------------------------------------------
    # 入站：去重 + 排序 + 派发
    # ------------------------------------------------------------------

    def _is_duplicate(self, message: Message) -> bool:
        """双重去重：内存 LRU 命中，或 Store 中已存在。

        LRU 覆盖"最近处理过"的绝大多数情况（快，不落盘）；
        Store 兜住重启后 / 久远之前的重复（慢，但正确）。
        """
        if message.id in self._seen:
            self._seen.move_to_end(message.id)
            return True
        if self._store.contains(message.id):
            self._remember(message.id)
            return True
        return False

    def _remember(self, message_id: str) -> None:
        self._seen[message_id] = None
        self._seen.move_to_end(message_id)
        while len(self._seen) > self._seen_capacity:
            self._seen.popitem(last=False)

    def ingest(self, messages: Iterable[Message]) -> tuple[Message, ...]:
        """接收一批入站消息：去重 → 排序 → 落库 → 派发。

        Returns:
            实际**新接收**（非重复）的消息，按 `(timestamp, id)` 升序。
        """
        # 串行化：公开 `ingest` 也可能被直接调用（如回调式 Provider），
        # 去重检查与记录之间不能被其它线程插入，否则同一 id 会派发两次。
        with self._ingest_lock:
            return self._ingest_locked(messages)

    def _ingest_locked(self, messages: Iterable[Message]) -> tuple[Message, ...]:
        accepted: list[Message] = []
        batch_ids: set[str] = set()
        for message in messages:
            if not message.id:
                logger.warning("丢弃无 id 的消息: %r", message)
                continue
            if message.id in batch_ids or self._is_duplicate(message):
                logger.debug("消息重复，跳过: id=%s", message.id)
                continue
            batch_ids.add(message.id)
            accepted.append(message)

        if not accepted:
            return ()

        accepted.sort(key=lambda m: m.sort_key)
        # 先落库、成功后再记入去重 LRU：落库失败时 id 不能留在 `_seen`，
        # 否则上游重投会被当成重复而永久丢失（Store 是 at-least-once 的兜底）。
        self._store.append(accepted)
        for message in accepted:
            self._remember(message.id)

        for message in accepted:
            self._dispatch(
                Event(
                    type=EventType.MESSAGE,
                    message=message,
                    conversation_id=message.conversation_id,
                    timestamp=message.timestamp or self._now_ms(),
                )
            )
        return tuple(accepted)

    def poll_once(self, limit: int = 50) -> tuple[Message, ...]:
        """从 Provider 拉取一次并接收。

        游标语义：Provider 返回的 `cursor` 被持久化到 Store，
        下次拉取时回传，从而实现断线恢复。

        Raises:
            ProviderAuthError: 凭证失效
            ProviderError: 上游失败
        """
        with self._ingest_lock:
            cursor = self._store.last_cursor()
            try:
                result: PollResult = self._poll_with_refresh(cursor, limit)
            except SessionExpiredError:
                raise
            except ProviderError as e:
                self._emit_error(f"拉取失败: {e}", data={"retryable": e.retryable})
                raise

            accepted = self.ingest(result.messages)
            if result.cursor is not None:
                self._store.save_cursor(result.cursor)
            return accepted

    def _poll_with_refresh(self, cursor: str | None, limit: int) -> PollResult:
        """拉取；认证失效时刷新凭证并**重试恰好一次**（与 `send` 对称）。

        拉取是只读操作，重试安全。刷新失败或重试仍认证失效 →
        派发 ERROR 并抛 `SessionExpiredError`。
        """
        try:
            return self._provider.poll(cursor=cursor, limit=limit)
        except ProviderAuthError as e:
            logger.warning("拉取遇认证失效，刷新凭证后重试一次: %s", e)
            self._session_expired = True
            if not self._refresh_session():
                self._emit_error(f"会话已失效: {e}", data={"auth_expired": True})
                raise SessionExpiredError(str(e)) from e
            # 刷新成功即恢复；若重试仍认证失效，下面会重新置 True
            self._session_expired = False
        try:
            result = self._provider.poll(cursor=cursor, limit=limit)
        except ProviderAuthError as e2:
            self._session_expired = True
            self._emit_error(f"会话已失效: {e2}", data={"auth_expired": True})
            raise SessionExpiredError(str(e2)) from e2
        self._session_expired = False
        return result

    def poll_forever(
        self,
        *,
        interval: float = 1.0,
        limit: int = 50,
        max_consecutive_errors: int = 5,
        stop_event: threading.Event | None = None,
    ) -> None:
        """持续轮询直到 `stop_event` 被置位或连续错误超限。

        连续错误达到上限时**抛出**最后一个异常，由调用方决定是重连还是退出 ——
        本层不擅自做重连决策（重连策略属于部署层）。
        """
        stop = stop_event or threading.Event()
        consecutive_errors = 0
        while not stop.is_set():
            try:
                self.poll_once(limit=limit)
                consecutive_errors = 0
            except (ProviderError, SessionExpiredError) as e:
                # ERROR 事件已由 poll_once 派发，这里不再重复派发
                consecutive_errors += 1
                logger.warning(
                    "拉取失败(%d/%d): %s", consecutive_errors, max_consecutive_errors, e
                )
                if consecutive_errors >= max_consecutive_errors:
                    raise
            stop.wait(interval)

    # ------------------------------------------------------------------
    # 出站
    # ------------------------------------------------------------------

    def send(
        self,
        segments: Iterable[Segment],
        *,
        conversation_id: str = "",
        reply_to: str = "",
        user_id: str = "",
    ) -> Message:
        """发送消息。

        流程：能力校验 → 文本长度校验 → Provider 发送 →
        （认证失效时）刷新凭证后**重试一次** → 成功则派发 `MESSAGE_SENT`。

        Raises:
            CapabilityError: 平台不具备所需能力
            SessionExpiredError: 刷新凭证后仍认证失效
            ProviderError: 发送失败
        """
        segs = tuple(segments)
        outgoing = OutgoingMessage(
            segments=segs,
            conversation_id=conversation_id,
            reply_to=reply_to,
            user_id=user_id,
        )

        try:
            # 能力闸门与长度校验在 `Provider.send()` 内部强制生效，
            # 这里不重复 —— 避免两处规则漂移。
            message = self._provider.send(outgoing)
        except ProviderAuthError as e:
            # 凭证失效：刷新后重试**一次**。
            # 上游未写入业务数据（认证在鉴权阶段就失败），重发是安全的。
            logger.warning("发送遇认证失效，刷新凭证后重试一次: %s", e)
            self._session_expired = True
            if not self._refresh_session():
                self._emit_error(f"会话已失效: {e}", data={"auth_expired": True})
                raise SessionExpiredError(str(e)) from e
            self._session_expired = False
            try:
                message = self._provider.send(outgoing)
            except ProviderAuthError as e2:
                self._session_expired = True
                self._emit_error(f"会话已失效: {e2}", data={"auth_expired": True})
                raise SessionExpiredError(str(e2)) from e2
            except ProviderError as e2:
                # 重试的非认证失败也要通知订阅者（否则只有首发失败才有事件）
                self._emit_send_failed(conversation_id, e2)
                raise
        except ProviderError as e:
            self._emit_send_failed(conversation_id, e)
            raise

        self._session_expired = False
        self._dispatch(
            Event(
                type=EventType.MESSAGE_SENT,
                message=message,
                conversation_id=conversation_id or message.conversation_id,
                timestamp=message.timestamp or self._now_ms(),
            )
        )
        return message

    def _emit_send_failed(self, conversation_id: str, error: ProviderError) -> None:
        self._dispatch(
            Event(
                type=EventType.MESSAGE_FAILED,
                conversation_id=conversation_id,
                timestamp=self._now_ms(),
                error=str(error),
                data={"retryable": error.retryable},
            )
        )

    def _refresh_session(self) -> bool:
        """尝试刷新凭证。

        Provider 通过 `refresh_session()` 钩子提供该能力；未实现则视为刷新失败。
        """
        refresher = getattr(self._provider, "refresh_session", None)
        if refresher is None:
            return False
        try:
            return bool(refresher())
        except SeewoError as e:
            logger.error("刷新凭证失败: %s", e)
            return False
        except Exception:
            logger.exception("刷新凭证时异常")
            return False

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def history(self, limit: int = 50, offset: int = 0) -> tuple[Message, ...]:
        """读历史消息（有序）。"""
        return self._store.load(limit=limit, offset=offset)

    def find(self, message_id: str) -> Message | None:
        """按 id 找一条消息。"""
        for msg in self._store.load():
            if msg.id == message_id:
                return msg
        return None


def build_service(
    provider: Provider,
    store: Store | None = None,
    **kwargs,
) -> MessageService:
    """便捷构造。"""
    return MessageService(provider, store, **kwargs)


__all__ = [
    "EventHandler",
    "MemoryStore",
    "MessageService",
    "Role",
    "build_service",
]
