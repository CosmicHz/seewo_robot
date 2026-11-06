"""OneBot 11 适配器。

本服务作为 **OneBot 11 实现端**（implementation）：对外暴露 OneBot 11
标准 API 与事件，第三方框架（NoneBot2 / AstrBot / LangBot / NapCat 等）
可以把本服务当作一个标准的 OneBot 11 后端接入。

协议要点（依据 OneBot 11 官方 spec）：
- 事件字段：`post_type` / `message_type` / `sub_type` / `message_id` /
  `user_id` / `group_id` / `message` / `raw_message` / `sender` /
  `time` / `self_id`
- API：`send_msg` / `send_private_msg` / `send_group_msg` / `get_msg` /
  `get_login_info` / `get_version_info` / `delete_msg` /
  `get_group_list` / `get_stranger_info` / `set_restart`
- 响应：`{"status": "ok", "retcode": 0, "data": ...}`，
  失败时 `{"status": "failed", "retcode": <非0>, "msg": "..."}`

**能力诚实声明**：希沃是「单家长 ↔ 单学生」的一对一场景，
因此：
- 只实现 `private` 消息类型；`group` 类型以明确的 retcode 拒绝，
  **不假装支持**群聊；
- `get_group_list` 返回空列表（没有群概念）；
- `send_group_msg` 返回 retcode=100（不支持），而不是静默丢弃。

消息段采用 OneBot 11 标准 CQ 码：
- 文本：`{"type": "text", "data": {"text": "..."}}`
- 图片：`{"type": "image", "data": {"file": "http://..."}}`
- 音频：`{"type": "record", "data": {"file": "http://..."}}`
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ...core.service import MessageService
from ...errors import CapabilityError, ProviderAuthError, ProviderError
from ...models import (
    ConversationType,
    Event,
    EventType,
    Message,
    Role,
    Segment,
    SegmentType,
)
from ..base import IntegrationConfig, IntegrationServer, TranslationError

logger = logging.getLogger("seewo.integration.onebot11")

#: OneBot 11 消息段类型 ← → 平台中立 SegmentType 的双向映射
_SEGMENT_TO_ONEBOT: dict[SegmentType, str] = {
    SegmentType.TEXT: "text",
    SegmentType.IMAGE: "image",
    SegmentType.AUDIO: "record",
    SegmentType.VIDEO: "video",
    SegmentType.FILE: "file",
    SegmentType.AT: "at",
    SegmentType.QUOTE: "reply",
}

_ONEBOT_TO_SEGMENT: dict[str, SegmentType] = {
    "text": SegmentType.TEXT,
    "image": SegmentType.IMAGE,
    "record": SegmentType.AUDIO,
    "video": SegmentType.VIDEO,
    "file": SegmentType.FILE,
    "at": SegmentType.AT,
    "reply": SegmentType.QUOTE,
    "face": SegmentType.UNKNOWN,
}

#: retcode 常量（OneBot 11 标准）
RETCODE_OK = 0
RETCODE_ASYNC = 1
RETCODE_ILLEGAL_PARAM = 1400
RETCODE_UNSUPPORTED = 100


def _is_empty_segment(seg: Segment) -> bool:
    """判断一个段是否「没有实际内容」。

    空文本段、空 URL 的资源段都算空 —— 它们不应被当作有效消息内容
    （否则 `message: ""` 会被当成一条合法的空消息发出去）。
    """
    if seg.type is SegmentType.TEXT:
        return not seg.text
    if seg.type is SegmentType.AT:
        return not seg.user_id
    if seg.type is SegmentType.QUOTE:
        return not seg.message_id
    return not seg.url


class OneBot11Adapter(IntegrationServer):
    """OneBot 11 协议适配器。"""

    def __init__(
        self,
        service: MessageService,
        config: IntegrationConfig | None = None,
        *,
        self_id: str = "seewo",
        api_prefix: str = "/onebot/v11",
    ) -> None:
        super().__init__(service, config)
        self.self_id = self_id
        self.api_prefix = api_prefix.rstrip("/")

    # ------------------------------------------------------------------
    # 事件翻译：核心 → OneBot
    # ------------------------------------------------------------------

    def translate_event(self, event: Event) -> dict[str, Any] | None:
        """核心事件 → OneBot 11 事件 JSON。"""
        if event.type is EventType.MESSAGE and event.message is not None:
            return self._message_event(event.message)
        if event.type is EventType.CONNECTED:
            return {
                "time": int(time.time()),
                "self_id": self.self_id,
                "post_type": "meta_event",
                "meta_event_type": "lifecycle",
                "sub_type": "connect",
            }
        if event.type is EventType.DISCONNECTED:
            return {
                "time": int(time.time()),
                "self_id": self.self_id,
                "post_type": "meta_event",
                "meta_event_type": "lifecycle",
                "sub_type": "disable",
            }
        if event.type is EventType.ERROR:
            return {
                "time": int(time.time()),
                "self_id": self.self_id,
                "post_type": "meta_event",
                "meta_event_type": "heartbeat",
                "status": {"online": False, "good": False},
                "interval": 0,
                "error": event.error,
            }
        # MESSAGE_SENT / MESSAGE_FAILED / RECONNECTED 没有 OneBot 11 标准事件，
        # **不伪造**未知 post_type —— 直接不发。
        return None

    def build_lifecycle(self, sub_type: str = "connect") -> dict[str, Any]:
        """构造 lifecycle 事件（正向 WS 建连时下发）。

        Args:
            sub_type: `connect` / `enable` / `disable`
        """
        return {
            "time": int(time.time()),
            "self_id": self.self_id,
            "post_type": "meta_event",
            "meta_event_type": "lifecycle",
            "sub_type": sub_type,
        }

    def _message_event(self, message: Message) -> dict[str, Any]:
        """平台中立消息 → OneBot 11 私聊消息事件。"""
        onebot_segments = [self._segment_to_onebot(s) for s in message.segments]
        raw_message = "".join(
            s.text if s.type is SegmentType.TEXT else f"[CQ:{seg['type']}]"
            for s, seg in zip(message.segments, onebot_segments, strict=False)
        )
        is_self = message.role is Role.SELF
        return {
            "time": int(message.timestamp / 1000)
            if message.timestamp
            else int(time.time()),
            "self_id": self.self_id,
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": message.id,
            "user_id": message.user_id,
            "message": onebot_segments,
            "raw_message": raw_message,
            "font": 0,
            "sender": {
                "user_id": message.user_id,
                "nickname": message.native.get("senderName", "") or message.user_id,
                "sex": "unknown",
                "age": 0,
            },
            "_is_self": is_self,
        }

    @staticmethod
    def _segment_to_onebot(seg: Segment) -> dict[str, Any]:
        """平台中立段 → OneBot 11 段。"""
        ob_type = _SEGMENT_TO_ONEBOT.get(seg.type, "text")
        data: dict[str, Any] = {}
        if seg.type is SegmentType.TEXT:
            data["text"] = seg.text
        elif seg.type is SegmentType.AT:
            data["qq"] = seg.user_id
        elif seg.type is SegmentType.QUOTE:
            data["id"] = seg.message_id
        else:
            data["file"] = seg.url
            if seg.name:
                data["name"] = seg.name
        return {"type": ob_type, "data": data}

    # ------------------------------------------------------------------
    # 消息翻译：OneBot → 核心
    # ------------------------------------------------------------------

    def parse_incoming(self, payload: dict[str, Any]) -> Message:
        """OneBot 11 事件 JSON → 平台中立消息。"""
        post_type = payload.get("post_type")
        if post_type != "message":
            raise TranslationError(f"不是消息事件: post_type={post_type!r}")

        message_type = payload.get("message_type", "private")
        if message_type != "private":
            # 希沃场景无群聊；收到群事件时明确拒绝，不静默降级成私聊
            raise TranslationError(f"不支持的消息类型: {message_type!r}")

        raw = payload.get("message")
        segments = self._parse_segments(raw)
        timestamp = int(payload.get("time", 0)) * 1000
        return Message(
            id=str(payload.get("message_id", "")),
            platform="seewo",
            conversation_id=str(
                payload.get("conversation_id") or payload.get("user_id") or ""
            ),
            user_id=str(payload.get("user_id", "")),
            role=Role.SELF if payload.get("_is_self") else Role.USER,
            segments=segments,
            timestamp=timestamp,
            native={"onebot": payload},
        )

    @staticmethod
    def _parse_segments(raw: Any) -> tuple[Segment, ...]:
        """解析 OneBot 消息段（支持字符串与段数组两种形态）。"""
        if isinstance(raw, str):
            return (Segment.text_seg(raw),) if raw else ()
        if not isinstance(raw, list):
            raise TranslationError(f"无法解析的消息内容: {type(raw).__name__}")

        segments: list[Segment] = []
        for item in raw:
            if isinstance(item, str):
                segments.append(Segment.text_seg(item))
                continue
            if not isinstance(item, dict):
                continue
            seg_type = _ONEBOT_TO_SEGMENT.get(
                item.get("type", "text"), SegmentType.UNKNOWN
            )
            data = item.get("data") or {}
            if seg_type is SegmentType.TEXT:
                segments.append(Segment.text_seg(str(data.get("text", ""))))
            elif seg_type is SegmentType.AT:
                segments.append(
                    Segment.at(str(data.get("qq", "")), name=str(data.get("name", "")))
                )
            elif seg_type is SegmentType.QUOTE:
                segments.append(Segment.quote(str(data.get("id", ""))))
            else:
                segments.append(
                    Segment(
                        type=seg_type,
                        url=str(data.get("file", "")),
                        name=str(data.get("name", "")),
                        duration=int(data.get("duration", 0) or 0),
                    )
                )
        # 过滤掉「解析出但内容为空」的段（如空的 text 段）
        return tuple(s for s in segments if not _is_empty_segment(s))

    # ------------------------------------------------------------------
    # API 端点（OneBot 11 标准）
    # ------------------------------------------------------------------

    def handle_api(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """处理 OneBot 11 API 调用。

        Returns:
            OneBot 11 标准响应体
        """
        handler = getattr(self, f"_api_{action}", None)
        if handler is None:
            return self._failed(RETCODE_UNSUPPORTED, f"未实现的 API: {action}")
        try:
            data = handler(params)
        except CapabilityError as e:
            return self._failed(RETCODE_UNSUPPORTED, str(e))
        except ProviderAuthError as e:
            return self._failed(1401, f"认证失效: {e}")
        except ProviderError as e:
            return self._failed(1400, str(e))
        except TranslationError as e:
            return self._failed(RETCODE_ILLEGAL_PARAM, str(e))
        except Exception as e:
            logger.exception("OneBot API %s 执行异常", action)
            return self._failed(1400, f"内部错误: {e}")
        return {"status": "ok", "retcode": RETCODE_OK, "data": data}

    @staticmethod
    def _ok(data: Any = None) -> dict[str, Any]:
        return {"status": "ok", "retcode": RETCODE_OK, "data": data}

    @staticmethod
    def _failed(retcode: int, message: str) -> dict[str, Any]:
        return {"status": "failed", "retcode": retcode, "msg": message}

    def _api_send_msg(self, params: dict[str, Any]) -> Any:
        """send_msg 的实现。"""
        message_type = params.get("message_type", "private")
        if message_type != "private":
            raise CapabilityError(
                "conversation", f"本服务仅支持 private 消息，不支持 {message_type!r}"
            )
        return self._do_send(params)

    def _api_send_private_msg(self, params: dict[str, Any]) -> Any:
        """send_private_msg —— 语义等同 send_msg(message_type=private)。

        单独提供是因为不少框架（含 NoneBot2 的 OneBot 11 驱动器）直接调
        `send_private_msg`，不走 `send_msg`。
        """
        return self._do_send(params)

    def _api_send_group_msg(self, params: dict[str, Any]) -> Any:
        """send_group_msg —— 希沃无群概念，明确拒绝。

        返回 retcode=100（不支持），而不是静默丢弃或偷偷降级成私聊。
        """
        raise CapabilityError(
            "conversation", "本服务仅支持 private 消息，不支持 group 消息"
        )

    def _do_send(self, params: dict[str, Any]) -> Any:
        """send_msg / send_private_msg 的公共实现。"""
        segments = self._parse_segments(params.get("message"))
        if not segments:
            raise TranslationError("message 不能为空")

        outgoing = Message(
            id="",
            platform="seewo",
            conversation_id=str(
                params.get("user_id", "") or params.get("group_id", "")
            ),
            user_id=str(params.get("user_id", "")),
            role=Role.SELF,
            segments=segments,
        )
        sent = self.service.send(
            outgoing.segments,
            conversation_id=outgoing.conversation_id,
            user_id=outgoing.user_id,
        )
        return {"message_id": sent.id}

    def _api_get_msg(self, params: dict[str, Any]) -> Any:
        message_id = str(params.get("message_id", ""))
        found = self.service.find(message_id)
        if found is None:
            raise TranslationError(f"消息不存在: {message_id}")
        return {
            "time": int(found.timestamp / 1000) if found.timestamp else 0,
            "message_type": "private",
            "message_id": found.id,
            "real_id": found.id,
            "sender": {
                "user_id": found.user_id,
                "nickname": found.native.get("senderName", "") or found.user_id,
            },
            "message": [self._segment_to_onebot(s) for s in found.segments],
        }

    def _api_get_login_info(self, params: dict[str, Any]) -> Any:
        return {"user_id": self.self_id, "nickname": "seewo-robot"}

    def _api_get_version_info(self, params: dict[str, Any]) -> Any:
        return {
            "app_name": "seewo-robot",
            "app_version": "2.0.0",
            "protocol_version": "v11",
        }

    def _api_get_status(self, params: dict[str, Any]) -> Any:
        return {
            "online": self.service.state.value == "connected",
            "good": self.service.state.value == "connected",
        }

    def _api_get_group_list(self, params: dict[str, Any]) -> Any:
        """本服务无群概念 —— 返回空列表（不是错误）。"""
        return []

    def _api_get_stranger_info(self, params: dict[str, Any]) -> Any:
        user_id = str(params.get("user_id", ""))
        return {"user_id": user_id, "nickname": user_id, "sex": "unknown", "age": 0}

    def _api_delete_msg(self, params: dict[str, Any]) -> Any:
        message_id = str(params.get("message_id", ""))
        self.service.provider.recall(message_id)
        return None

    def _api_set_restart(self, params: dict[str, Any]) -> Any:
        return None

    # ------------------------------------------------------------------
    # 推送
    # ------------------------------------------------------------------

    def send_payload(self, conn: Any, payload: Any) -> bool:
        """把载荷推给一个 WS 连接。

        推的是**事件**（已含 `post_type`），按 OneBot 11 正向 WS 规范
        直接发送，不带 `status`/`retcode` 信封 —— 那是 HTTP API 响应的格式，
        与事件推送不同。API 请求的应答信封由 WS 请求处理逻辑负责。
        """
        try:
            conn.send(payload)
            return True
        except Exception as e:
            logger.debug("推送失败: %s", e)
            return False

    # ------------------------------------------------------------------
    # 便捷查询
    # ------------------------------------------------------------------

    def conversation_type(self) -> ConversationType:
        """本服务只提供一对一会话。"""
        return ConversationType.DIRECT
