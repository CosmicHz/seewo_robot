"""平台中立的数据模型。

**这个模块是整个解耦的基石**，它必须满足两条硬约束：

1. 不 import 任何平台专有符号 —— 没有 `RawMessage`、没有 `SeewoCode`、
   没有 `resUrl`、没有希沃的数字消息类型。平台差异一律用
   `Segment`（消息段）+ `native`（原始载荷）承载。
2. 是唯一的跨层数据结构 —— 接口层、集成层、核心层之间传递的
   只有这里的类型，`to_dict()` 的输出即 REST/SDK 的 wire 契约。

平台能力差异的处理原则：
- 平台**有**的能力 → 用对应的 `SegmentType` 表达；
- 平台**没有**的能力 → 由 `PlatformCapabilities` 声明不支持，
  请求时抛 `CapabilityError`，**不静默降级、不假装成功**。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class SegmentType(str, Enum):
    """消息段类型（跨平台的最小公共消息单元）。

    选这几个值是因为它们是主流协议（Satori / OneBot 11 / Discord）的交集：
    文本、图片、音频、视频、文件、提及、引用。
    `at` / `quote` 不是所有平台都支持 —— 由 capabilities 声明。
    """

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    AT = "at"
    QUOTE = "quote"
    UNKNOWN = "unknown"


class Role(str, Enum):
    """消息发送方角色（平台中立）。

    - `SELF`：本服务自己发出（机器人/家长侧）
    - `USER`：对端发出（孩子/群友）
    - `SYSTEM`：平台系统消息
    """

    SELF = "self"
    USER = "user"
    SYSTEM = "system"


class ConversationType(str, Enum):
    """会话类型。"""

    DIRECT = "direct"  # 一对一
    GROUP = "group"  # 群聊
    CHANNEL = "channel"  # 频道（Discord/Satori 概念）


class EventType(str, Enum):
    """核心层派发的统一事件类型。

    集成层把各平台原生事件翻译成这些类型，业务逻辑只认这一套。
    """

    MESSAGE = "message"  # 收到一条消息
    MESSAGE_SENT = "message.sent"  # 成功发出了一条消息
    MESSAGE_FAILED = "message.failed"  # 发送失败
    CONNECTED = "connection.connected"
    DISCONNECTED = "connection.disconnected"
    RECONNECTED = "connection.reconnected"
    ERROR = "error"


class ConnectionState(str, Enum):
    """连接状态机。

    合法跃迁（其余一律拒绝，抛 `InvalidStateTransition`）::

        DISCONNECTED ──connect()──▶ CONNECTING ──▶ CONNECTED
                                        │              │
                                        │ failure      │ disconnect()
                                        ▼              ▼
                                    DISCONNECTED   DISCONNECTED
                                                 CONNECTED ──reconnect()──▶ CONNECTING
    """

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    CLOSING = "closing"

    def can_transition_to(self, target: ConnectionState) -> bool:
        """判断是否允许跃迁到 `target`。"""
        allowed = _STATE_TRANSITIONS.get(self, frozenset())
        return target in allowed


#: 状态跃迁表（唯一真源，见 `ConnectionState.can_transition_to`）
_STATE_TRANSITIONS: dict[ConnectionState, frozenset[ConnectionState]] = {
    ConnectionState.DISCONNECTED: frozenset(
        {ConnectionState.CONNECTING, ConnectionState.DISCONNECTED}
    ),
    ConnectionState.CONNECTING: frozenset(
        {
            ConnectionState.CONNECTED,
            ConnectionState.DISCONNECTED,
        }
    ),
    ConnectionState.CONNECTED: frozenset(
        {
            ConnectionState.CLOSING,
            ConnectionState.DISCONNECTED,
            ConnectionState.CONNECTING,
        }
    ),
    ConnectionState.CLOSING: frozenset({ConnectionState.DISCONNECTED}),
}


# ---------------------------------------------------------------------------
# 消息段
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Segment:
    """一条消息中的最小单元。

    采用「单一 dataclass + `type` 判别」而非继承体系：便于 JSON 序列化、
    跨语言映射、以及第三方框架直接消费。

    Attributes:
        type: 段类型
        text: 文本内容（`TEXT` 用）
        url: 资源地址（`IMAGE`/`AUDIO`/`VIDEO`/`FILE` 用）
        mime: MIME 类型（资源段用，可选）
        name: 资源文件名 / 被提及者显示名（可选）
        duration: 媒体时长（毫秒，`AUDIO`/`VIDEO` 用）
        user_id: 被提及的用户 id（`AT` 用）
        message_id: 被引用的消息 id（`QUOTE` 用）
        data: 平台专有附加数据，原样透传，不参与跨平台语义
    """

    type: SegmentType = SegmentType.TEXT
    text: str = ""
    url: str = ""
    mime: str = ""
    name: str = ""
    duration: int = 0
    user_id: str = ""
    message_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def text_seg(cls, text: str) -> Segment:
        return cls(type=SegmentType.TEXT, text=text)

    @classmethod
    def image(cls, url: str, *, mime: str = "", name: str = "") -> Segment:
        return cls(type=SegmentType.IMAGE, url=url, mime=mime, name=name)

    @classmethod
    def audio(
        cls, url: str, *, mime: str = "", duration: int = 0, name: str = ""
    ) -> Segment:
        return cls(
            type=SegmentType.AUDIO, url=url, mime=mime, duration=duration, name=name
        )

    @classmethod
    def video(
        cls, url: str, *, mime: str = "", duration: int = 0, name: str = ""
    ) -> Segment:
        return cls(
            type=SegmentType.VIDEO, url=url, mime=mime, duration=duration, name=name
        )

    @classmethod
    def file(cls, url: str, *, mime: str = "", name: str = "") -> Segment:
        return cls(type=SegmentType.FILE, url=url, mime=mime, name=name)

    @classmethod
    def at(cls, user_id: str, *, name: str = "") -> Segment:
        return cls(type=SegmentType.AT, user_id=user_id, name=name)

    @classmethod
    def quote(cls, message_id: str) -> Segment:
        return cls(type=SegmentType.QUOTE, message_id=message_id)

    def to_dict(self) -> dict[str, Any]:
        """转 wire 形态（dict）。

        只输出**有值的字段**，避免第三方消费方收到一堆空字符串。
        `data` 始终输出（可能是空 dict），保证结构稳定。
        """
        out: dict[str, Any] = {"type": self.type.value}
        if self.text:
            out["text"] = self.text
        if self.url:
            out["url"] = self.url
        if self.mime:
            out["mime"] = self.mime
        if self.name:
            out["name"] = self.name
        if self.duration:
            out["duration"] = self.duration
        if self.user_id:
            out["user_id"] = self.user_id
        if self.message_id:
            out["message_id"] = self.message_id
        out["data"] = dict(self.data)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Segment:
        """从 wire 形态还原。未知 type 归入 `UNKNOWN`，不丢数据。"""
        raw_type = d.get("type", SegmentType.TEXT.value)
        try:
            seg_type = SegmentType(raw_type)
        except ValueError:
            seg_type = SegmentType.UNKNOWN
        data = d.get("data")
        return cls(
            type=seg_type,
            text=d.get("text", "") or "",
            url=d.get("url", "") or "",
            mime=d.get("mime", "") or "",
            name=d.get("name", "") or "",
            duration=max(0, int(d.get("duration", 0) or 0)),
            user_id=d.get("user_id", "") or "",
            message_id=d.get("message_id", "") or "",
            data=dict(data) if isinstance(data, dict) else {},
        )


# ---------------------------------------------------------------------------
# 用户 / 会话
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class User:
    """平台中立的用户。

    `id` 是平台内的稳定标识；`name` 是显示名。
    """

    id: str = ""
    name: str = ""
    nickname: str = ""
    avatar: str = ""
    is_self: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        """显示名：`name` 优先，回落 `nickname`，再回落 id。"""
        return self.name or self.nickname or self.id

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id}
        if self.name:
            out["name"] = self.name
        if self.nickname:
            out["nickname"] = self.nickname
        if self.avatar:
            out["avatar"] = self.avatar
        if self.is_self:
            out["is_self"] = True
        if self.data:
            out["data"] = dict(self.data)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> User:
        data = d.get("data")
        return cls(
            id=str(d.get("id", "") or ""),
            name=d.get("name", "") or "",
            nickname=d.get("nickname", "") or "",
            avatar=d.get("avatar", "") or "",
            is_self=bool(d.get("is_self", False)),
            data=dict(data) if isinstance(data, dict) else {},
        )


@dataclass(frozen=True, slots=True)
class Conversation:
    """平台中立的会话（一次对话的上下文边界）。

    Attributes:
        id: 会话 id（平台内稳定）
        type: 会话类型
        title: 会话标题（群名等，可选）
        members: 成员 id 列表
    """

    id: str = ""
    type: ConversationType = ConversationType.DIRECT
    title: str = ""
    members: tuple[str, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "type": self.type.value}
        if self.title:
            out["title"] = self.title
        if self.members:
            out["members"] = list(self.members)
        if self.data:
            out["data"] = dict(self.data)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Conversation:
        raw_type = d.get("type", ConversationType.DIRECT.value)
        try:
            conv_type = ConversationType(raw_type)
        except ValueError:
            conv_type = ConversationType.DIRECT
        data = d.get("data")
        members = d.get("members") or []
        return cls(
            id=str(d.get("id", "") or ""),
            type=conv_type,
            title=d.get("title", "") or "",
            members=tuple(str(m) for m in members),
            data=dict(data) if isinstance(data, dict) else {},
        )


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    """平台中立的消息。

    Attributes:
        id: 平台内稳定的消息 id
        platform: 来源平台标识（如 `seewo`）
        conversation_id: 所属会话 id
        user_id: 发送者 id
        role: 发送方角色
        segments: 消息段序列
        timestamp: 毫秒时间戳
        reply_to: 被回复消息 id（可选）
        native: 平台原始载荷，原样保留，便于排查与高级用途
    """

    id: str = ""
    platform: str = ""
    conversation_id: str = ""
    user_id: str = ""
    role: Role = Role.USER
    segments: tuple[Segment, ...] = ()
    timestamp: int = 0
    reply_to: str = ""
    native: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """拼接所有文本段。供只需纯文本的消费方（多数聊天框架）使用。"""
        return "".join(s.text for s in self.segments if s.type is SegmentType.TEXT)

    @property
    def sort_key(self) -> tuple[int, str]:
        """全局排序键：`(timestamp, id)`。

        用 id 做第二级排序键，保证**同一毫秒内的多条消息也有确定顺序**，
        这是消息顺序可重现（可测试、可回放）的前提。
        """
        return (self.timestamp, self.id)

    def segments_of(self, seg_type: SegmentType) -> tuple[Segment, ...]:
        """取出指定类型的全部段。"""
        return tuple(s for s in self.segments if s.type is seg_type)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "platform": self.platform,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "role": self.role.value,
            "segments": [s.to_dict() for s in self.segments],
            "timestamp": self.timestamp,
        }
        if self.reply_to:
            out["reply_to"] = self.reply_to
        if self.native:
            out["native"] = dict(self.native)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        raw_role = d.get("role", Role.USER.value)
        try:
            role = Role(raw_role)
        except ValueError:
            role = Role.USER
        segments = d.get("segments") or []
        native = d.get("native")
        return cls(
            id=str(d.get("id", "") or ""),
            platform=d.get("platform", "") or "",
            conversation_id=str(d.get("conversation_id", "") or ""),
            user_id=str(d.get("user_id", "") or ""),
            role=role,
            segments=tuple(Segment.from_dict(s) for s in segments),
            timestamp=int(d.get("timestamp", 0) or 0),
            reply_to=str(d.get("reply_to", "") or ""),
            native=dict(native) if isinstance(native, dict) else {},
        )


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Event:
    """核心层派发的统一事件。

    Attributes:
        type: 事件类型
        message: 相关消息（`MESSAGE` / `MESSAGE_SENT` / `MESSAGE_FAILED` 时有）
        conversation_id: 相关会话 id
        timestamp: 毫秒时间戳
        error: 错误信息（`ERROR` 时有）
        data: 附加数据
    """

    type: EventType = EventType.MESSAGE
    message: Message | None = None
    conversation_id: str = ""
    timestamp: int = 0
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": self.type.value,
            "conversation_id": self.conversation_id,
            "timestamp": self.timestamp,
        }
        if self.message is not None:
            out["message"] = self.message.to_dict()
        if self.error:
            out["error"] = self.error
        if self.data:
            out["data"] = dict(self.data)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Event:
        raw_type = d.get("type", EventType.MESSAGE.value)
        try:
            etype = EventType(raw_type)
        except ValueError:
            etype = EventType.MESSAGE
        msg_raw = d.get("message")
        data = d.get("data")
        return cls(
            type=etype,
            message=Message.from_dict(msg_raw) if isinstance(msg_raw, dict) else None,
            conversation_id=str(d.get("conversation_id", "") or ""),
            timestamp=int(d.get("timestamp", 0) or 0),
            error=d.get("error", "") or "",
            data=dict(data) if isinstance(data, dict) else {},
        )


# ---------------------------------------------------------------------------
# 平台能力
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlatformCapabilities:
    """平台真实能力声明。

    **这张表是"不伪造能力"的载体**：Provider 必须如实申报自己支持什么，
    上层据此决定能否执行某个操作。缺失的能力抛 `CapabilityError`，
    而不是静默丢消息。

    Attributes:
        send_text: 能否发送纯文本
        send_image: 能否发送图片
        send_audio: 能否发送音频
        send_video: 能否发送视频
        send_file: 能否发送文件
        send_mention: 能否发送提及
        send_quote: 能否发送引用
        max_text_length: 单条文本最大长度；0 表示不限
        max_media_bytes: 单个媒体文件最大字节数；0 表示不限
        supports_receipt: 上游是否返回"已送达"确认
        supports_recall: 是否支持撤回
        supports_history: 是否支持拉取历史消息
        supports_stream: 是否支持流式/增量事件推送
    """

    send_text: bool = True
    send_image: bool = False
    send_audio: bool = False
    send_video: bool = False
    send_file: bool = False
    send_mention: bool = False
    send_quote: bool = False
    max_text_length: int = 0
    max_media_bytes: int = 0
    supports_receipt: bool = False
    supports_recall: bool = False
    supports_history: bool = False
    supports_stream: bool = False

    def supports_segment(self, seg: Segment) -> bool:
        """判断该段类型是否可发送。"""
        return {
            SegmentType.TEXT: self.send_text,
            SegmentType.IMAGE: self.send_image,
            SegmentType.AUDIO: self.send_audio,
            SegmentType.VIDEO: self.send_video,
            SegmentType.FILE: self.send_file,
            SegmentType.AT: self.send_mention,
            SegmentType.QUOTE: self.send_quote,
            SegmentType.UNKNOWN: False,
        }[seg.type]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PlatformCapabilities:
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


#: 能力名 → PlatformCapabilities 字段名。用于 `CapabilityError` 的定位。
CAPABILITY_FIELD_BY_SEGMENT: dict[SegmentType, str] = {
    SegmentType.TEXT: "send_text",
    SegmentType.IMAGE: "send_image",
    SegmentType.AUDIO: "send_audio",
    SegmentType.VIDEO: "send_video",
    SegmentType.FILE: "send_file",
    SegmentType.AT: "send_mention",
    SegmentType.QUOTE: "send_quote",
    SegmentType.UNKNOWN: "send_unknown",
}
