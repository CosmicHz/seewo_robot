"""希沃 Provider —— 把希沃协议翻译成平台中立模型。

**这是整个项目里唯一允许出现希沃专有符号的地方**（除了 `seewo/providers/`
自身）。核心层、接口层、集成层都不认识 `resUrl`、数字 type、`statusCode`。

复用现有实现，不重写协议：
- `msg.msg`：留言的取/发（底层是 m-campus `pxencode` 协议）
- `stu.stu`：学生信息
- `upload.upload_file`：云存储上传
- `models.SeewoCode`：希沃业务码（**只在本文件解释**）

能力缺口（明确记录在 `docs/CAPABILITIES.md`）：
- 希沃无「撤回」接口 → `supports_recall = False`
- 希沃留言无「提及」→ `supports_mention = False`
- 希沃单条留言硬上限 200 字 → `max_text_length = 199`
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..errors import (
    CapabilityError,
    ProviderAuthError,
    ProviderBusinessError,
    ProviderConnectionError,
    ProviderError,
    ProviderTimeoutError,
)
from ..models import (
    Conversation,
    ConversationType,
    Message,
    PlatformCapabilities,
    Role,
    Segment,
    SegmentType,
    User,
)
from ..provider.base import OutgoingMessage, PollResult, Provider

logger = logging.getLogger("seewo.provider")

#: 段类型 → 能力字段名（用于 CapabilityError.capability）
_CAPABILITY_BY_TYPE = {
    SegmentType.IMAGE: "send_image",
    SegmentType.AUDIO: "send_audio",
    SegmentType.VIDEO: "send_video",
    SegmentType.FILE: "send_file",
}


class SeewoProvider(Provider):
    """希沃云班「亲情留言」平台接入。

    Args:
        account: 已登录的账户对象（`login.acc`）
        student: 学生信息对象（`stu.stu`）
        stu_msg: 留言 DAO（`msg.msg`）
        uploader: 上传函数（`upload.upload_file`）；不传则不支持图片/音频发送
    """

    platform = "seewo"

    def __init__(
        self,
        account: Any = None,
        student: Any = None,
        stu_msg: Any = None,
        uploader: Any = None,
    ) -> None:
        super().__init__()
        self._account = account
        self._student = student
        self._stu_msg = stu_msg
        self._uploader = uploader
        self.capabilities = PlatformCapabilities(
            send_text=True,
            send_image=uploader is not None,
            send_audio=uploader is not None,
            send_video=False,  # 希沃 type=4 存在，但本项目未实现上传→发送链路
            send_file=False,
            send_mention=False,
            send_quote=False,
            max_text_length=199,  # 希沃服务端硬上限 200 字，客户端留 1 字余量
            supports_receipt=True,  # 响应体 statusCode 即业务确认
            supports_recall=False,  # 协议层虽有 DELETE_KIDNOTE_V1_NOTE，本项目未开放
            # Provider.history() 未实现平台侧回溯：如实申报 False。
            # 本地历史由核心层 Store 提供（`/api/v2/history`），不经 Provider。
            supports_history=False,
            supports_stream=False,  # 希沃为轮询式，无推送
        )

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """校验会话可用。"""

        if self._account is None or self._stu_msg is None:
            raise ProviderConnectionError("希沃会话未初始化", retryable=False)
        if getattr(self._account, "token_expired", False):
            raise ProviderAuthError("希沃凭证已失效")

        from ..models import ConnectionState

        if self._state is ConnectionState.DISCONNECTED:
            self._transition(ConnectionState.CONNECTING)
        self._transition(ConnectionState.CONNECTED)

    def refresh_session(self) -> bool:
        """刷新凭证（重新构造 account/student/stu_msg）。

        复用旧 `login.acc` 的重登逻辑：删缓存 → 重新扫码。
        这里返回是否成功；失败时上层抛 `SessionExpiredError`。
        """
        if self._account is None:
            return False
        try:
            from login import acc
            from msg import msg as msg_dao
            from stu import stu as stu_dao

            # 非交互：只重读 tokens.json，绝不触发扫码（否则会阻塞请求线程）。
            # 缓存凭证已失效时返回 False，由上层抛 SessionExpiredError 引导重新登录。
            account = acc(type=0, auto_login=False)
            if getattr(account, "token_expired", False):
                return False
            student = stu_dao(account)
            self._account = account
            self._student = student
            self._stu_msg = msg_dao(account, student)
            return True
        except Exception as e:
            logger.error("希沃凭证刷新失败: %s", e)
            return False

    # ------------------------------------------------------------------
    # 入站
    # ------------------------------------------------------------------

    def poll(self, cursor: str | None = None, limit: int = 50) -> PollResult:
        """拉取最新一页留言。

        **每次都拉第 1 页**（`start=1` = 最新一页，见 `msg.get` 的已验证语义），
        增量去重由核心层按消息 id 完成（`MessageService.ingest`）。

        此前把页码当游标、每次 +1 往更旧的页翻：翻到空页后再也回不到
        第 1 页，新消息永远拉不到。希沃没有「某 id 之后」的增量接口，
        因此本 Provider **不提供游标**（返回 `cursor=None`，入参被忽略）。

        已知边界：两次 poll 之间新到的留言多于 `limit` 条时，超出部分本次
        拉不到；v2 不提供历史补拉（v1 的 `/api/sync_all` 只写 v1 文件）。
        """
        del cursor  # 见 docstring：希沃无增量游标
        if self._stu_msg is None:
            raise ProviderConnectionError("希沃会话未初始化", retryable=False)
        try:
            response = self._stu_msg.get(limit, start=1)
        except ProviderError:
            raise
        except Exception as e:
            raise self._translate(e) from e

        from models import SeewoCode

        code = getattr(response, "statusCode", 0)
        if SeewoCode.is_relogin_code(code):
            raise ProviderAuthError(f"希沃凭证失效（业务码 {code}）")
        if code and not SeewoCode.is_ok(code):
            raise ProviderBusinessError(
                f"希沃返回业务错误码 {code}: {getattr(response, 'message', '')}"
            )

        messages = tuple(self._to_message(raw) for raw in response.result)
        return PollResult(
            messages=messages,
            cursor=None,
            has_more=len(response.result) >= limit,
        )

    def _to_message(self, raw: Any) -> Message:
        """希沃原始消息 → 平台中立消息。"""
        raw_type = getattr(raw, "type", 1)
        res_url = getattr(raw, "resUrl", "") or ""
        content = getattr(raw, "content", "") or ""
        voice_length = int(getattr(raw, "voiceLength", 0) or 0)

        segments: list[Segment] = []
        # 希沃的数字 type：0/1=文本, 2=图片, 3=音频, 4=视频, 5=文件, 6=富媒体
        media_type = {
            2: SegmentType.IMAGE,
            3: SegmentType.AUDIO,
            4: SegmentType.VIDEO,
            5: SegmentType.FILE,
            6: SegmentType.FILE,
        }.get(raw_type, SegmentType.TEXT)
        if media_type is SegmentType.TEXT:
            segments.append(Segment.text_seg(content))
        else:
            segments.append(
                Segment(
                    type=media_type,
                    url=res_url,
                    duration=voice_length,
                )
            )

        parent_uid = getattr(self._account, "uid", "")
        sender_uid = getattr(raw, "senderUid", "") or ""
        if sender_uid and sender_uid == parent_uid:
            role = Role.SELF
        else:
            role = Role.USER

        return Message(
            id=str(getattr(raw, "id", "") or ""),
            platform=self.platform,
            conversation_id=self._conversation_id(),
            user_id=sender_uid,
            role=role,
            segments=tuple(segments),
            timestamp=int(getattr(raw, "createTime", 0) or 0),
            native={
                "seewoType": raw_type,
                "senderType": getattr(raw, "senderType", ""),
                "senderName": getattr(raw, "senderName", ""),
            },
        )

    def _conversation_id(self) -> str:
        """希沃是单家长↔单学生，用学生 uid 作会话 id。"""
        return getattr(self._student, "userUid", "") or "seewo-direct"

    # ------------------------------------------------------------------
    # 出站
    # ------------------------------------------------------------------

    def _send(self, outgoing: OutgoingMessage) -> Message:
        """发送留言。

        文本走 type=1；图片/音频需先上传到云存储拿到 `downloadUrl`，
        再以 type=2/3 发送。
        """
        if self._stu_msg is None:
            raise ProviderConnectionError("希沃会话未初始化", retryable=False)

        text_parts: list[str] = []
        media: list[Segment] = []
        for seg in outgoing.segments:
            if seg.type is SegmentType.TEXT:
                text_parts.append(seg.text)
            else:
                media.append(seg)

        if media:
            # 以下组合希沃协议是否支持**未经验证**，不猜测，直接拒绝：
            # - 文本 + 媒体：`msg.send` 在 type=2/3 时不带 content，文本会被静默丢弃
            # - 多个媒体：一条留言只有一个 resUrl
            if len(media) > 1:
                raise CapabilityError("multiple_media", "希沃一条留言只能携带一个媒体")
            if "".join(text_parts):
                raise CapabilityError(
                    "text_with_media", "希沃媒体留言不携带文本，请分开发送"
                )

        try:
            if media:
                return self._send_media(text_parts, media)
            content = "".join(text_parts)
            result = self._stu_msg.send(content, 1)
        except (ProviderError, CapabilityError):
            # CapabilityError 不是 ProviderError：必须原样上抛，
            # 否则会被下面的兜底改写成「上游失败」，掩盖能力缺口
            raise
        except Exception as e:
            raise self._translate(e) from e

        if result.needs_relogin:
            raise ProviderAuthError(f"希沃凭证失效（业务码 {result.seewo_code}）")
        if not result.ok:
            raise ProviderBusinessError(
                f"希沃拒绝发送（业务码 {result.seewo_code}）: {result.message}"
            )
        return self._sent_message(text_parts, media)

    def _send_media(self, text_parts: list[str], media: list[Segment]) -> Message:
        """上传并发送媒体留言。"""
        if self._uploader is None:
            raise ProviderError("希沃 Provider 未配置上传能力，无法发送媒体")

        seg = media[0]
        # 上传器只接受**本地文件路径**；http URL 等远程地址不代为下载。
        if not seg.url or not os.path.isfile(seg.url):
            raise CapabilityError(
                _CAPABILITY_BY_TYPE.get(seg.type, "send_file"),
                f"希沃媒体发送只接受服务端本地文件路径，收到: {seg.url!r}",
            )
        url = self._uploader(seg.url)
        if not url:
            raise ProviderBusinessError(f"媒体上传失败: {seg.url}")

        seewo_type = {
            SegmentType.IMAGE: 2,
            SegmentType.AUDIO: 3,
            SegmentType.VIDEO: 4,
            SegmentType.FILE: 5,
        }[seg.type]
        result = self._stu_msg.send(
            "".join(text_parts),
            seewo_type,
            url,
            seg.duration or 0,
        )
        if result.needs_relogin:
            raise ProviderAuthError(f"希沃凭证失效（业务码 {result.seewo_code}）")
        if not result.ok:
            raise ProviderBusinessError(
                f"希沃拒绝发送（业务码 {result.seewo_code}）: {result.message}"
            )
        return self._sent_message(text_parts, media, url)

    def _sent_message(
        self,
        text_parts: list[str],
        media: list[Segment],
        url: str = "",
    ) -> Message:
        """构造一条"已发送"消息，供上层事件使用。"""
        segments: list[Segment] = []
        text = "".join(text_parts)
        if text:
            segments.append(Segment.text_seg(text))
        for seg in media:
            if url:
                segments.append(
                    Segment(
                        type=seg.type,
                        url=url,
                        duration=seg.duration,
                        name=seg.name,
                    )
                )
            else:
                segments.append(seg)
        return Message(
            # 希沃发送接口不返回服务端消息 id：如实留空，不伪造回执 id
            id="",
            platform=self.platform,
            conversation_id=self._conversation_id(),
            user_id=getattr(self._account, "uid", ""),
            role=Role.SELF,
            segments=tuple(segments),
            timestamp=0,
        )

    @staticmethod
    def _translate(exc: Exception) -> ProviderError:
        """把底层 DAO 异常归一化为 Provider 异常。"""
        import requests

        if isinstance(exc, requests.Timeout):
            return ProviderTimeoutError(f"希沃请求超时: {exc}", retryable=True)
        if isinstance(exc, requests.ConnectionError):
            return ProviderConnectionError(f"希沃连接失败: {exc}", retryable=True)
        if isinstance(exc, requests.RequestException):
            return ProviderConnectionError(f"希沃请求失败: {exc}", retryable=True)
        return ProviderError(f"希沃调用失败: {exc}", retryable=False)

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """返回平台与会话描述，供 `/status` 类接口使用。"""
        return {
            "platform": self.platform,
            "conversation": {
                "id": self._conversation_id(),
                "type": ConversationType.DIRECT.value,
                "title": getattr(self._student, "name", ""),
                "members": (
                    (getattr(self._account, "uid", ""),)
                    if getattr(self._account, "uid", "")
                    else ()
                ),
            },
            "self_user": User(
                id=getattr(self._account, "uid", ""),
                name="家长",
                is_self=True,
            ).to_dict(),
        }


__all__ = ["Conversation", "SeewoProvider"]
