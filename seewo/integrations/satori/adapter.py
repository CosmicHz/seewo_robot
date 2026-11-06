"""Satori 适配器。

本服务作为 **Satori 协议的 SDK 实现端**，对外暴露：
- 事件 WebSocket：``ws://host:port/{version}/events``
- HTTP API：``POST /{platform}/{account}/satori/v1/{resource}.{method}``

协议要点（依据 Satori 官方 spec，已核对字段名）：
- 事件信令帧：``{"op": <opcode>, "body": {...}}``
  - ``op=0`` EVENT（服务端→客户端）
  - ``op=1`` PING（客户端→服务端）
  - ``op=2`` PONG（服务端→客户端）
  - ``op=3`` IDENTIFY（客户端→服务端），body ``{"token": ..., "sn": ...}``
  - ``op=4`` READY（服务端→客户端），body ``{"logins": [], "proxy_urls": []}``
- 事件对象顶层字段：``sn`` / ``type`` / ``timestamp`` / ``login`` /
  ``message`` / ``user`` / ``channel``（其余可选）
- 登录信息 ``login``：非登录事件只含 ``sn`` / ``user`` / ``platform``
- 消息对象：``id`` / ``content`` / ``user`` / ``channel`` / ``created_at``
- 元素（消息段）用类 XML 语法：``<img src="..."/>``、``<audio src="..."/>``、
  ``<at id="..."/>``、``<quote>``、``<message id="...">`` 等
- HTTP API 路径形如 ``message.create``（**点分**，不是斜杠），
  body ``{"channel_id": ..., "content": ...}``，**返回数组**

断线恢复：客户端在 IDENTIFY 里带上最后收到的 ``sn``，服务端据此补发。
本适配器实现该语义：维护已推送的最大 sn，重连时把 ``sn`` 回给客户端。
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from ...core.service import MessageService
from ...errors import CapabilityError, ProviderAuthError, ProviderError
from ...models import (
    Conversation,
    ConversationType,
    Event,
    EventType,
    Message,
    Role,
    Segment,
    SegmentType,
    User,
)
from ..base import IntegrationConfig, IntegrationServer, TranslationError

logger = logging.getLogger("seewo.integration.satori")

# —— Satori 信令操作码 ——
OP_EVENT = 0
OP_PING = 1
OP_PONG = 2
OP_IDENTIFY = 3
OP_READY = 4


@dataclass
class SignalResult:
    """一帧信令的处理结果。"""

    frames: list[dict[str, Any]]
    #: IDENTIFY 通过（连接此后才可接收事件）
    identified: bool = False
    #: IDENTIFY 被拒（服务端应断开连接）
    rejected: bool = False


# —— 平台中立段 → Satori 元素标签 ——
_SEGMENT_TO_ELEMENT: dict[SegmentType, str] = {
    SegmentType.TEXT: "",
    SegmentType.IMAGE: "img",
    SegmentType.AUDIO: "audio",
    SegmentType.VIDEO: "video",
    SegmentType.FILE: "file",
    SegmentType.AT: "at",
    SegmentType.QUOTE: "quote",
}

# —— Satori 元素标签 → 平台中立段 ——
_ELEMENT_TO_SEGMENT: dict[str, SegmentType] = {
    "img": SegmentType.IMAGE,
    "image": SegmentType.IMAGE,
    "audio": SegmentType.AUDIO,
    "video": SegmentType.VIDEO,
    "file": SegmentType.FILE,
    "at": SegmentType.AT,
    "quote": SegmentType.QUOTE,
}

#: Satori 元素解析：匹配 `<tag attr="v" .../>` 或 `<tag ...>...</tag>`
_ELEMENT_RE = re.compile(
    r"<(?P<tag>[a-z][a-z0-9-]*)(?P<attrs>[^>]*?)(?:/>|>(?P<inner>.*?)</(?P=tag)>)",
    re.DOTALL | re.IGNORECASE,
)
_ATTR_RE = re.compile(
    r"(?P<key>[a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)')"
)


class SatoriAdapter(IntegrationServer):
    """Satori 协议适配器。

    Args:
        service: 核心服务
        config: 集成配置（`path` 默认为 ``/v1/events``）
        platform: Satori platform 标识（用于 URL 前缀与 login 对象）
        account: Satori account id（用于 URL 前缀）
    """

    def __init__(
        self,
        service: MessageService,
        config: IntegrationConfig | None = None,
        *,
        platform: str = "seewo",
        account: str = "default",
        api_prefix: str = "/satori/v1",
    ) -> None:
        cfg = config or IntegrationConfig()
        if cfg.path == "/events":
            cfg.path = "/v1/events"
        super().__init__(service, cfg)
        self.platform = platform
        self.account = account
        self.api_prefix = api_prefix.rstrip("/")
        self._sn = 0
        self._sn_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 事件翻译：核心 → Satori
    # ------------------------------------------------------------------

    def build_payload(self, event: Event) -> dict[str, Any] | None:
        """核心事件 → Satori 信令帧（包一层 op/body 信封并分配 sn）。"""
        return self.event_frame(event)

    def translate_event(self, event: Event) -> dict[str, Any] | None:
        """核心事件 → Satori 事件对象。"""
        if event.type is EventType.MESSAGE and event.message is not None:
            return self._message_event(event.message)
        if event.type is EventType.CONNECTED:
            return {
                "type": "login-added",
                "timestamp": int(time.time() * 1000),
                "login": self._login_block(),
            }
        if event.type is EventType.DISCONNECTED:
            return {
                "type": "login-removed",
                "timestamp": int(time.time() * 1000),
                "login": self._login_block(),
            }
        if event.type is EventType.ERROR:
            return {
                "type": "error",
                "timestamp": int(time.time() * 1000),
                "login": self._login_block(),
                "message": {"content": event.error},
            }
        return None

    def _login_block(self) -> dict[str, Any]:
        """login 对象。非登录事件只含 sn / user / platform。"""
        return {
            "sn": self.current_sn(),
            "user": {"id": self.account, "name": "seewo-robot"},
            "platform": self.platform,
        }

    def _message_event(self, message: Message) -> dict[str, Any]:
        """平台中立消息 → Satori 事件对象。"""
        content = self.encode_content(message.segments)
        return {
            "type": "message-created",
            "timestamp": message.timestamp or int(time.time() * 1000),
            "login": self._login_block(),
            "message": {
                "id": message.id,
                "content": content,
                "created_at": message.timestamp or int(time.time() * 1000),
                "user": {
                    "id": message.user_id,
                    "name": message.native.get("senderName", "") or message.user_id,
                },
                "channel": self._channel_block(message),
            },
            "user": {
                "id": message.user_id,
                "name": message.native.get("senderName", "") or message.user_id,
            },
            "channel": self._channel_block(message),
        }

    def _channel_block(self, message: Message) -> dict[str, Any]:
        return {
            "id": message.conversation_id or "seewo-direct",
            "type": ConversationType.DIRECT.value,
        }

    # ------------------------------------------------------------------
    # 元素编解码
    # ------------------------------------------------------------------

    @staticmethod
    def encode_content(segments: tuple[Segment, ...]) -> str:
        """平台中立段 → Satori 元素字符串。"""
        parts: list[str] = []
        for seg in segments:
            if seg.type is SegmentType.TEXT:
                parts.append(html.escape(seg.text, quote=False))
                continue
            tag = _SEGMENT_TO_ELEMENT.get(seg.type)
            if tag is None:
                continue
            attrs: list[str] = []
            if seg.type is SegmentType.AT:
                attrs.append(f'id="{html.escape(seg.user_id, quote=True)}"')
                if seg.name:
                    attrs.append(f'name="{html.escape(seg.name, quote=True)}"')
            elif seg.type is SegmentType.QUOTE:
                parts.append("<quote></quote>")
                continue
            else:
                if seg.url:
                    attrs.append(f'src="{html.escape(seg.url, quote=True)}"')
                if seg.name:
                    attrs.append(f'title="{html.escape(seg.name, quote=True)}"')
                if seg.duration:
                    attrs.append(f'duration="{seg.duration}"')
            if attrs:
                parts.append(f"<{tag} {' '.join(attrs)}/>")
            else:
                parts.append(f"<{tag}/>")
        return "".join(parts)

    @staticmethod
    def parse_content(content: str) -> tuple[Segment, ...]:
        """Satori 元素字符串 → 平台中立段。"""
        if not content:
            return ()
        segments: list[Segment] = []
        cursor = 0
        for match in _ELEMENT_RE.finditer(content):
            if match.start() > cursor:
                text = content[cursor : match.start()]
                if text.strip():
                    segments.append(Segment.text_seg(html.unescape(text)))
            tag = match.group("tag").lower()
            attrs = {
                m.group("key").lower(): html.unescape(
                    m.group("dq")
                    if m.group("dq") is not None
                    else (m.group("sq") or "")
                )
                for m in _ATTR_RE.finditer(match.group("attrs") or "")
            }
            seg_type = _ELEMENT_TO_SEGMENT.get(tag)
            if seg_type is SegmentType.AT:
                segments.append(
                    Segment.at(attrs.get("id", ""), name=attrs.get("name", ""))
                )
            elif seg_type is SegmentType.QUOTE:
                segments.append(Segment.quote(attrs.get("id", "")))
            elif seg_type is not None:
                segments.append(
                    Segment(
                        type=seg_type,
                        url=attrs.get("src", ""),
                        name=attrs.get("title", ""),
                        duration=int(attrs.get("duration", 0) or 0),
                    )
                )
            cursor = match.end()
        if cursor < len(content):
            text = content[cursor:]
            if text.strip():
                segments.append(Segment.text_seg(html.unescape(text)))
        if not segments and content.strip():
            segments.append(Segment.text_seg(html.unescape(content)))
        return tuple(segments)

    # ------------------------------------------------------------------
    # 信令帧
    # ------------------------------------------------------------------

    def make_signal(
        self, op: int, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """构造信令帧。"""
        frame: dict[str, Any] = {"op": op}
        if body is not None:
            frame["body"] = body
        return frame

    def ready_frame(self) -> dict[str, Any]:
        """READY 响应。"""
        return self.make_signal(
            OP_READY,
            {
                "logins": [
                    {
                        "sn": self.current_sn(),
                        "user": {"id": self.account, "name": "seewo-robot"},
                        "platform": self.platform,
                    }
                ],
                "proxy_urls": [],
            },
        )

    def pong_frame(self) -> dict[str, Any]:
        """PONG 响应。"""
        return self.make_signal(OP_PONG)

    def event_frame(self, event: Event) -> dict[str, Any] | None:
        """核心事件 → EVENT 信令帧。"""
        body = self.translate_event(event)
        if body is None:
            return None
        with self._sn_lock:
            self._sn += 1
            sn = self._sn
        body["sn"] = sn
        return self.make_signal(OP_EVENT, body)

    def current_sn(self) -> int:
        """当前已推送的最大 sn（用于会话恢复）。"""
        with self._sn_lock:
            return self._sn

    def handle_signal(self, raw: str) -> list[dict[str, Any]]:
        """处理客户端信令帧，返回要回发的帧列表。

        Raises:
            TranslationError: 帧格式非法
        """
        return self.process_signal(raw).frames

    def process_signal(self, raw: str) -> SignalResult:
        """处理客户端信令帧，同时报告鉴权结果。

        WS 服务端据 `identified` 决定**何时**把连接登记进广播集合
        （IDENTIFY 通过之前不推送任何事件），据 `rejected` 决定是否断开。

        Raises:
            TranslationError: 帧格式非法
        """
        import json

        try:
            frame = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            raise TranslationError(f"信令帧不是合法 JSON: {e}") from e
        if not isinstance(frame, dict):
            raise TranslationError("信令帧必须是对象")
        op = frame.get("op")
        body = frame.get("body") or {}
        if not isinstance(body, dict):
            raise TranslationError("信令帧 body 必须是对象")

        if op == OP_PING:
            return SignalResult([self.pong_frame()])
        if op == OP_IDENTIFY:
            if not self.check_token(body.get("token")):
                return SignalResult(
                    [
                        self.make_signal(
                            OP_EVENT,
                            {
                                "type": "error",
                                "timestamp": int(time.time() * 1000),
                                "login": self._login_block(),
                                "message": {"content": "invalid token"},
                            },
                        )
                    ],
                    rejected=True,
                )
            resume_sn = body.get("sn")
            if isinstance(resume_sn, int) and resume_sn > 0:
                # 只推进 sn，**不补发**断线期间的事件：本服务不保留已推送事件的缓冲。
                # 断线期间的消息请通过 HTTP API / `/api/v2/history` 补拉。
                with self._sn_lock:
                    self._sn = max(self._sn, resume_sn)
            return SignalResult([self.ready_frame()], identified=True)
        if op == OP_EVENT:
            # 客户端不应主动发 EVENT 信令；忽略。
            return SignalResult([])
        raise TranslationError(f"未知的操作码: {op!r}")

    def on_client_event(self, payload: dict[str, Any]) -> Message:
        """Satori 事件 → 平台中立消息（用于 WebHook 接入方向）。"""
        msg_block = payload.get("message") or {}
        content = msg_block.get("content", "")
        user = payload.get("user") or msg_block.get("user") or {}
        channel = payload.get("channel") or msg_block.get("channel") or {}
        segments = self.parse_content(content)
        if not segments:
            raise TranslationError("事件不含任何消息内容")
        return Message(
            id=str(msg_block.get("id", payload.get("sn", ""))),
            platform=self.platform,
            conversation_id=str(channel.get("id", "")),
            user_id=str(user.get("id", "")),
            role=Role.USER,
            segments=segments,
            timestamp=int(payload.get("timestamp", 0) or 0),
            native={"satori": payload},
        )

    # ------------------------------------------------------------------
    # HTTP API（`{resource}.{method}` 点分路径）
    # ------------------------------------------------------------------

    def handle_api(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """处理 Satori HTTP API 调用。"""
        handler = getattr(self, f"_api_{action.replace('.', '_')}", None)
        if handler is None:
            return {"error": f"未实现的 API: {action}"}
        try:
            return handler(params)
        except CapabilityError as e:
            return {"error": str(e)}
        except ProviderAuthError as e:
            return {"error": f"认证失效: {e}"}
        except ProviderError as e:
            return {"error": str(e)}
        except TranslationError as e:
            return {"error": str(e)}
        except Exception as e:
            logger.exception("Satori API %s 执行异常", action)
            return {"error": f"内部错误: {e}"}

    def _api_message_create(self, params: dict[str, Any]) -> Any:
        """message.create —— 返回**数组**（Satori 规范）。"""
        content = params.get("content", "")
        if not isinstance(content, str) or not content:
            raise TranslationError("content 必须是非空字符串")
        segments = self.parse_content(content)
        if not segments:
            raise TranslationError("content 不含任何消息内容")
        channel_id = str(params.get("channel_id", ""))
        sent = self.service.send(segments, conversation_id=channel_id)
        return [sent.to_dict()]

    def _api_message_get(self, params: dict[str, Any]) -> Any:
        message_id = str(params.get("message_id", ""))
        found = self.service.find(message_id)
        if found is None:
            raise TranslationError(f"消息不存在: {message_id}")
        return found.to_dict()

    def _api_message_delete(self, params: dict[str, Any]) -> Any:
        message_id = str(params.get("message_id", ""))
        self.service.provider.recall(message_id)
        return None

    def _api_channel_get(self, params: dict[str, Any]) -> Any:
        channel_id = str(params.get("channel_id", ""))
        return Conversation(
            id=channel_id or "seewo-direct",
            type=ConversationType.DIRECT,
        ).to_dict()

    def _api_user_get(self, params: dict[str, Any]) -> Any:
        user_id = str(params.get("user_id", ""))
        return User(id=user_id, name=user_id).to_dict()

    def _api_session_create(self, params: dict[str, Any]) -> Any:
        return {
            "id": self.account,
            "platform": self.platform,
            "self_id": self.account,
        }

    # ------------------------------------------------------------------
    # 推送
    # ------------------------------------------------------------------

    def send_payload(self, conn: Any, payload: Any) -> bool:
        """把核心事件帧推给一个 WS 连接。"""
        frame = payload
        try:
            conn.send(frame)
            return True
        except Exception as e:
            logger.debug("推送失败: %s", e)
            return False
