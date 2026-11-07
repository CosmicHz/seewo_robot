"""集成层基类：连接生命周期 / 心跳 / 断线重连 / 会话恢复 / 事件串行派发。

被 OneBot 11、Satori 两个适配器共用。

**适配器只需实现协议翻译**，其余全部由本基类提供：

============================  ==========================================
职责                          归属
============================  ==========================================
WebSocket 收发                适配器（协议帧格式不同）
心跳（PING/PONG 等）          适配器（协议帧格式不同）
断线重连 + 退避               基类
会话恢复（游标）              基类（游标语义适配器可覆写）
事件串行派发（保序）          基类
认证                          适配器（鉴权头/令牌格式不同）
业务逻辑（去重/存储/发送）    `MessageService`（不在本层）
============================  ==========================================
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Self

from ..core.service import MessageService
from ..errors import ProviderAuthError
from ..models import Event, EventType, Message

logger = logging.getLogger("seewo.integration")


class TranslationError(ValueError):
    """协议翻译失败（原生载荷不符合预期）。"""


@dataclass
class IntegrationConfig:
    """集成层配置。

    Attributes:
        host: 监听地址
        port: 监听端口
        token: 鉴权令牌（None / 空串 = 不鉴权）
        token_getter: 可选；提供时**每次校验实时调用**以取得令牌，
            优先于 ``token``（用于跟随 ``config.json`` 热重载）
        path: 事件推送的 URL 路径（WS 型适配器用）
        heartbeat_interval: 心跳间隔（秒）；0 = 关闭
        reconnect_base_delay: 重连退避基数（秒）
        reconnect_max_delay: 重连退避上限（秒）
        max_reconnect_attempts: 最大连续重连次数；0 = 无限
        resend_cursor: 是否在重连时携带游标请求补发
    """

    host: str = "127.0.0.1"
    port: int = 0
    token: str | None = None
    token_getter: Callable[[], str | None] | None = None
    path: str = "/events"
    heartbeat_interval: float = 10.0
    reconnect_base_delay: float = 1.0
    reconnect_max_delay: float = 30.0
    max_reconnect_attempts: int = 0
    resend_cursor: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


class IntegrationServer:
    """集成服务器基类。

    Args:
        service: 核心消息服务
        config: 集成配置
    """

    def __init__(
        self, service: MessageService, config: IntegrationConfig | None = None
    ):
        self.service = service
        self.config = config or IntegrationConfig()
        self._connections: set[Any] = set()
        self._conn_lock = threading.Lock()
        self._dispatch_lock = threading.RLock()
        self._stop = threading.Event()
        self._cursor: str | None = None
        self._threads: list[threading.Thread] = []
        self._state_lock = threading.Lock()
        self._subscribed = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动服务（连接核心、订阅事件、启动后台线程）。"""
        self._stop.clear()
        self.service.connect()
        self.subscribe()
        self._start_background()

    def subscribe(self) -> None:
        """订阅核心事件（幂等），**不**连接平台。

        供宿主在平台会话尚未就绪时先挂上事件出口：之后任何入站/出站事件
        都会推给已认证的连接。
        """
        if self._subscribed:
            return
        self.service.on_any(self._on_core_event)
        self._subscribed = True

    def stop(self, *, disconnect_core: bool = True) -> None:
        """停止服务，释放资源。

        Args:
            disconnect_core: 是否同时断开核心服务。多个适配器共用一个
                `MessageService` 时传 ``False``，把连接生命周期交还宿主。
        """
        self._stop.set()
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=2.0)
        self._threads.clear()
        with self._conn_lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            self._safe_close(conn)
        if self._subscribed:
            self.service.off_any(self._on_core_event)
            self._subscribed = False
        if disconnect_core:
            try:
                self.service.disconnect()
            except Exception as e:
                logger.warning("断开核心连接时出错: %s", e)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def _start_background(self) -> None:
        """启动后台线程（心跳 / 重连 / 轮询）。适配器可覆写。"""

    def _spawn(self, target: Callable[[], None], name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    # ------------------------------------------------------------------
    # 事件派发
    # ------------------------------------------------------------------

    def _on_core_event(self, event: Event) -> None:
        """核心事件 → 协议事件 → 推给已连接的对端。"""
        try:
            payload = self.build_payload(event)
        except TranslationError as e:
            logger.warning("事件翻译失败，跳过: %s", e)
            return
        if payload is None:
            return
        self.broadcast(payload)

    def build_payload(self, event: Event) -> Any | None:
        """把核心事件变成要推送的载荷。子类实现。

        默认实现委托 `translate_event`；协议需要额外包一层信封（如 Satori
        的 ``{"op": 0, "body": ...}``）的子类覆写本方法。
        """
        return self.translate_event(event)

    def translate_event(self, event: Event) -> Any | None:
        """把核心事件翻译成协议原生载荷。子类实现。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------

    def expected_token(self) -> str:
        """当前生效的令牌；空串表示不鉴权。"""
        getter = self.config.token_getter
        token = getter() if getter is not None else self.config.token
        return token or ""

    def check_token(self, provided: Any) -> bool:
        """校验调用方提供的令牌。未配置令牌时一律放行。

        用 `hmac.compare_digest` 做常量时间比较，避免计时侧信道。
        """
        expected = self.expected_token()
        if not expected:
            return True
        if not isinstance(provided, str) or not provided:
            return False
        return hmac.compare_digest(provided.encode(), expected.encode())

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def register_connection(self, conn: Any) -> None:
        """登记一个新连接。"""
        with self._conn_lock:
            self._connections.add(conn)

    def unregister_connection(self, conn: Any) -> None:
        """注销连接。"""
        with self._conn_lock:
            self._connections.discard(conn)

    @property
    def connection_count(self) -> int:
        with self._conn_lock:
            return len(self._connections)

    def broadcast(self, payload: Any) -> int:
        """把载荷推给所有已连接的对端。

        返回成功推送的连接数。单个连接失败不影响其它连接。
        """
        with self._conn_lock:
            connections = list(self._connections)
        delivered = 0
        for conn in connections:
            if self.send_payload(conn, payload):
                delivered += 1
        return delivered

    def send_payload(self, conn: Any, payload: Any) -> bool:
        """把一个载荷发给单个连接。子类实现（协议帧格式不同）。"""
        raise NotImplementedError

    def _safe_close(self, conn: Any) -> None:
        try:
            conn.close()
        except Exception as e:
            logger.debug("关闭连接时出错: %s", e)

    # ------------------------------------------------------------------
    # 会话恢复
    # ------------------------------------------------------------------

    @property
    def cursor(self) -> str | None:
        """当前会话游标。"""
        with self._state_lock:
            return self._cursor

    def update_cursor(self, cursor: str | None) -> None:
        """更新会话游标（用于断线重连后补发）。"""
        with self._state_lock:
            self._cursor = cursor

    def resume_cursor(self) -> str | None:
        """返回用于恢复的游标。

        默认取核心 Store 里持久化的游标；适配器可覆写以配合协议语义。
        """
        if not self.config.resend_cursor:
            return None
        try:
            return self.service.store.last_cursor()
        except Exception as e:
            logger.debug("读取持久化游标失败: %s", e)
            return None

    # ------------------------------------------------------------------
    # 重连
    # ------------------------------------------------------------------

    def backoff_delay(self, attempt: int) -> float:
        """指数退避 + 抖动上限。"""
        base = self.config.reconnect_base_delay * (2 ** max(0, attempt - 1))
        return min(base, self.config.reconnect_max_delay)

    def should_retry(self, attempt: int) -> bool:
        """已尝试 `attempt` 次后，是否还允许再尝试一次。"""
        if self.config.max_reconnect_attempts <= 0:
            return True
        return attempt < self.config.max_reconnect_attempts

    def reconnect_loop(
        self, connect_fn: Callable[[], Any], on_connected: Callable[[Any], None]
    ) -> None:
        """断线重连循环。

        Args:
            connect_fn: 建立连接的函数
            on_connected: 连接成功后的回调（用于发 IDENTIFY 之类）
        """
        attempt = 0
        while not self._stop.is_set():
            # 先判断再计数：`max_reconnect_attempts=N` 恰好尝试 N 次
            if not self.should_retry(attempt):
                logger.error("重连次数达到上限 (%d)，放弃", attempt)
                return
            attempt += 1
            delay = self.backoff_delay(attempt)
            logger.info("第 %d 次重连，%.1fs 后尝试", attempt, delay)
            if self._stop.wait(delay):
                return
            try:
                conn = connect_fn()
            except ProviderAuthError:
                logger.error("认证失败，不再重连")
                return
            except Exception as e:
                logger.warning("重连失败: %s", e)
                continue
            try:
                on_connected(conn)
                return
            except Exception as e:
                logger.warning("重连后初始化失败: %s", e)
                self._safe_close(conn)

    # ------------------------------------------------------------------
    # 出站调用（供适配器的 HTTP API 端点使用）
    # ------------------------------------------------------------------

    def deliver(self, message: Message) -> Message:
        """把一条出站消息交给核心发送。

        供适配器的「发送消息」端点调用 —— 适配器只做翻译，
        发送/去重/事件派发全在核心。
        """
        return self.service.send(
            message.segments,
            conversation_id=message.conversation_id,
            reply_to=message.reply_to,
            user_id=message.user_id,
        )


def request_token(request: Any, *, allow_query: bool = True) -> str:
    """从 HTTP 请求中取出调用方令牌。

    - ``Authorization: Bearer <token>``（OneBot 11 / Satori 共同约定）
    - ``?access_token=<token>``（OneBot 11 约定；``allow_query=False`` 时不接受）

    取不到时返回空串。
    """
    auth = request.headers.get("Authorization", "") or ""
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    if allow_query:
        return request.args.get("access_token", "") or ""
    return ""


def now_ms() -> int:
    """当前毫秒时间戳。"""
    return int(time.time() * 1000)


__all__ = [
    "EventType",
    "IntegrationConfig",
    "IntegrationServer",
    "TranslationError",
    "now_ms",
    "request_token",
]
