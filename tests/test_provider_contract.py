"""Provider 契约测试 —— 同一套断言跑在 FakeProvider 与 SeewoProvider 上。

这是「平台接入契约不靠自觉」的保证：任何 Provider（无论是测试替身还是
真实希沃实现）都必须满足同一组行为约定。

希沃 Provider 的依赖（`msg.msg` / `stu.stu` / `upload`）用假对象替代，
**不发出任何网络请求**，因此本文件完全离线。
"""

import pytest

from seewo.errors import (
    CapabilityError,
    ProviderAuthError,
    ProviderBusinessError,
    ProviderConnectionError,
)
from seewo.models import (
    ConversationType,
    Message,
    PlatformCapabilities,
    Role,
    Segment,
    SegmentType,
)
from seewo.provider.base import OutgoingMessage, PollResult, Provider
from seewo.providers.seewo import SeewoProvider
from seewo.testing.fake_provider import FakeProvider

# ===========================================================================
# 希沃侧假对象（替代 msg.msg / stu.stu / acc）
# ===========================================================================


class FakeSeewoAccount:
    uid = "parent-uid"

    def __init__(self, token_expired=False):
        self.token_expired = token_expired


class FakeSeewoStudent:
    userUid = "student-uid"
    name = "测试学生"
    schoolUid = "school-uid"
    classUid = "class-uid"


class FakeSeewoMsg:
    """假希沃留言 DAO。"""

    def __init__(self, result=None, status_code=200):
        self._result = result or []
        self.statusCode = status_code
        self.message = ""
        self.sent = []
        self.get_calls = []

    def get(self, count, start=1):
        self.get_calls.append((count, start))
        from models import MessageResponse

        return MessageResponse(statusCode=self.statusCode, result=tuple(self._result))

    def send(self, content, type, resUrl="", voiceLength=0, resConfig=""):
        from models import SendResult

        self.sent.append(
            {
                "content": content,
                "type": type,
                "resUrl": resUrl,
                "voiceLength": voiceLength,
                "resConfig": resConfig,
            }
        )
        return SendResult(ok=True, seewo_code=200, message="发送成功")


def _seewo_provider(result=None, uploader=None, status_code=200):
    account = FakeSeewoAccount()
    student = FakeSeewoStudent()
    stu_msg = FakeSeewoMsg(result=result, status_code=status_code)
    provider = SeewoProvider(account, student, stu_msg, uploader=uploader)
    return provider, stu_msg


def _raw(mid=1, **kw):
    from models import RawMessage

    base = {
        "id": mid,
        "senderUid": "student-uid",
        "senderType": "student",
        "senderName": "测试学生",
        "type": 1,
        "content": f"消息{mid}",
        "createTime": 1700000000000,
    }
    base.update(kw)
    return RawMessage.from_dict(base)


# ===========================================================================
# 通用契约断言 —— 两个 Provider 都跑
# ===========================================================================


def _providers():
    """产出 (名称, provider) 对，供参数化使用。"""
    fake, _ = FakeProvider(), None
    seewo, _ = _seewo_provider()
    return [("fake", fake), ("seewo", seewo)]


class TestProviderContract:
    @pytest.mark.parametrize(("name", "provider"), _providers())
    def test_platform_is_identified(self, name, provider):
        assert isinstance(provider.platform, str)
        assert provider.platform

    @pytest.mark.parametrize(("name", "provider"), _providers())
    def test_capabilities_declared(self, name, provider):
        assert isinstance(provider.capabilities, PlatformCapabilities)

    @pytest.mark.parametrize(("name", "provider"), _providers())
    def test_initial_state_is_disconnected(self, name, provider):
        from seewo.models import ConnectionState

        assert provider.state is ConnectionState.DISCONNECTED

    @pytest.mark.parametrize(("name", "provider"), _providers())
    def test_connect_transitions_to_connected(self, name, provider):
        from seewo.models import ConnectionState

        provider.connect()
        assert provider.state is ConnectionState.CONNECTED

    @pytest.mark.parametrize(("name", "provider"), _providers())
    def test_disconnect_transitions_to_disconnected(self, name, provider):
        from seewo.models import ConnectionState

        provider.connect()
        provider.disconnect()
        assert provider.state is ConnectionState.DISCONNECTED

    @pytest.mark.parametrize(("name", "provider"), _providers())
    def test_poll_returns_poll_result(self, name, provider):
        provider.connect()
        result = provider.poll(cursor=None, limit=10)
        assert isinstance(result, PollResult)
        assert isinstance(result.messages, tuple)

    @pytest.mark.parametrize(("name", "provider"), _providers())
    def test_send_returns_message(self, name, provider):
        provider.connect()
        message = provider.send(OutgoingMessage(segments=(Segment.text_seg("hi"),)))
        assert isinstance(message, Message)
        assert message.platform == provider.platform

    @pytest.mark.parametrize(("name", "provider"), _providers())
    def test_unsupported_segment_is_rejected(self, name, provider):
        """能力闸门在 Provider 层也生效（不依赖调用方自觉）"""
        provider.connect()
        provider.capabilities = PlatformCapabilities(send_text=True)
        with pytest.raises(CapabilityError):
            provider.send(OutgoingMessage(segments=(Segment.image("http://x"),)))

    @pytest.mark.parametrize(("name", "provider"), _providers())
    def test_ensure_text_length_enforced(self, name, provider):
        provider.connect()
        provider.capabilities = PlatformCapabilities(send_text=True, max_text_length=5)
        with pytest.raises(CapabilityError):
            provider.ensure_text_length("x" * 100)

    @pytest.mark.parametrize(("name", "provider"), _providers())
    def test_context_manager_connects_and_disconnects(self, name, provider):
        from seewo.models import ConnectionState

        with provider:
            assert provider.state is ConnectionState.CONNECTED
        assert provider.state is ConnectionState.DISCONNECTED


class TestProviderBaseDefaults:
    def test_recall_unsupported_raises(self):
        class Minimal(Provider):
            platform = "minimal"

            def poll(self, cursor=None, limit=50):
                return PollResult()

            def _send(self, outgoing):
                return Message(id="x", platform="minimal")

        with pytest.raises(CapabilityError):
            Minimal().recall("m1")

    def test_history_unsupported_raises(self):
        class Minimal(Provider):
            platform = "minimal"

            def poll(self, cursor=None, limit=50):
                return PollResult()

            def _send(self, outgoing):
                return Message(id="x", platform="minimal")

        with pytest.raises(CapabilityError):
            Minimal().history()

    def test_callback_provider_queues_messages(self):
        from seewo.provider.base import CallbackProvider

        provider = CallbackProvider(platform="cb")
        msg = Message(id="m1", platform="cb")
        provider.push(msg)
        result = provider.poll()
        assert [m.id for m in result.messages] == ["m1"]

    def test_callback_provider_invokes_callback(self):
        from seewo.provider.base import CallbackProvider

        provider = CallbackProvider(platform="cb")
        received = []
        provider.set_message_callback(received.append)
        provider.push(Message(id="m1", platform="cb"))
        assert [m.id for m in received] == ["m1"]


# ===========================================================================
# 希沃 Provider 专属行为
# ===========================================================================


class TestSeewoProvider:
    def test_capabilities_reflect_real_seewo_limits(self):
        """希沃的能力申报必须与协议事实一致"""
        provider, _ = _seewo_provider()
        caps = provider.capabilities
        assert caps.send_text is True
        assert caps.send_image is False  # 未传 uploader → 不支持
        assert caps.send_mention is False  # 希沃留言无提及
        assert caps.send_quote is False
        assert caps.max_text_length == 199  # 希沃服务端硬上限 200
        # Provider.history() 未实现平台侧回溯 → 如实申报 False（#26）
        assert caps.supports_history is False
        assert caps.supports_stream is False  # 希沃是轮询式

    def test_capabilities_change_with_uploader(self):
        provider, _ = _seewo_provider(uploader=lambda p: "http://cdn/x")
        assert provider.capabilities.send_image is True
        assert provider.capabilities.send_audio is True

    def test_poll_converts_raw_to_neutral(self):
        provider, _ = _seewo_provider(result=[_raw(1), _raw(2)])
        provider.connect()
        result = provider.poll(cursor=None, limit=10)
        assert [m.id for m in result.messages] == ["1", "2"]
        assert result.messages[0].text == "消息1"
        assert result.messages[0].role is Role.USER
        assert result.messages[0].conversation_id == "student-uid"

    def test_poll_marks_own_messages_as_self(self):
        provider, _ = _seewo_provider(
            result=[_raw(1, senderUid="parent-uid", senderType="parent")]
        )
        provider.connect()
        result = provider.poll()
        assert result.messages[0].role is Role.SELF

    def test_poll_maps_image_type_to_image_segment(self):
        provider, _ = _seewo_provider(
            result=[_raw(1, type=2, content="", resUrl="http://x/a.png")]
        )
        provider.connect()
        result = provider.poll()
        seg = result.messages[0].segments[0]
        assert seg.type is SegmentType.IMAGE
        assert seg.url == "http://x/a.png"

    def test_poll_maps_audio_type_to_audio_segment(self):
        provider, _ = _seewo_provider(
            result=[
                _raw(1, type=3, content="", resUrl="http://x/a.mp3", voiceLength=3000)
            ]
        )
        provider.connect()
        result = provider.poll()
        seg = result.messages[0].segments[0]
        assert seg.type is SegmentType.AUDIO
        assert seg.duration == 3000

    def test_poll_auth_code_raises_auth_error(self):
        provider, _ = _seewo_provider(status_code=-500)
        provider.connect()
        with pytest.raises(ProviderAuthError):
            provider.poll()

    def test_poll_business_code_raises_business_error(self):
        provider, _ = _seewo_provider(status_code=40000)
        provider.connect()
        with pytest.raises(ProviderBusinessError):
            provider.poll()

    def test_poll_always_fetches_latest_page(self):
        """#27：每次都拉第 1 页（最新），不随游标往旧页翻。"""
        provider, stu_msg = _seewo_provider()
        provider.connect()
        for cursor in (None, "1", "3", "garbage"):
            provider.poll(cursor=cursor, limit=10)
        assert [start for _, start in stu_msg.get_calls] == [1, 1, 1, 1]

    def test_poll_returns_no_cursor(self):
        """希沃无增量游标：如实返回 None，不伪造页码游标。"""
        provider, _ = _seewo_provider(result=[_raw(1)])
        provider.connect()
        assert provider.poll(limit=10).cursor is None

    def test_new_message_on_first_page_is_picked_up_after_empty_poll(self):
        """#27 回归：空页之后第 1 页出现新消息，下一次 poll 能拿到。"""
        provider, stu_msg = _seewo_provider()
        provider.connect()
        assert provider.poll(limit=10).messages == ()
        stu_msg._result = [_raw(7)]
        assert [m.id for m in provider.poll(limit=10).messages] == ["7"]

    def test_poll_token_expired_code_raises_auth_error(self):
        provider, _ = _seewo_provider(status_code=-505)
        provider.connect()
        with pytest.raises(ProviderAuthError):
            provider.poll()

    def test_connect_without_session_raises(self):
        provider = SeewoProvider(None, None, None)
        with pytest.raises(ProviderConnectionError):
            provider.connect()

    def test_connect_with_expired_account_raises_auth_error(self):
        provider = SeewoProvider(
            FakeSeewoAccount(token_expired=True),
            FakeSeewoStudent(),
            FakeSeewoMsg(),
        )
        with pytest.raises(ProviderAuthError):
            provider.connect()

    def test_send_text_uses_seewo_type_1(self):
        provider, stu_msg = _seewo_provider()
        provider.connect()
        provider.send(OutgoingMessage(segments=(Segment.text_seg("你好"),)))
        assert stu_msg.sent[0]["type"] == 1
        assert stu_msg.sent[0]["content"] == "你好"

    def test_send_image_uploads_first(self, tmp_path):
        local = tmp_path / "a.png"
        local.write_bytes(b"png")
        uploaded = []

        def uploader(path):
            uploaded.append(path)
            return "http://cdn/uploaded.png"

        provider, stu_msg = _seewo_provider(uploader=uploader)
        provider.connect()
        provider.send(OutgoingMessage(segments=(Segment.image(str(local)),)))
        assert uploaded == [str(local)]
        assert stu_msg.sent[0]["type"] == 2
        assert stu_msg.sent[0]["resUrl"] == "http://cdn/uploaded.png"

    def test_send_image_upload_failure_raises(self, tmp_path):
        local = tmp_path / "a.png"
        local.write_bytes(b"png")
        provider, stu_msg = _seewo_provider(uploader=lambda p: "")
        provider.connect()
        with pytest.raises(ProviderBusinessError):
            provider.send(OutgoingMessage(segments=(Segment.image(str(local)),)))
        assert stu_msg.sent == []

    def test_send_remote_url_is_rejected_without_upload(self):
        """B6：上传器只接受本地文件；http URL 不当作路径上传。"""
        uploaded = []
        provider, stu_msg = _seewo_provider(
            uploader=lambda p: uploaded.append(p) or "http://cdn/x"
        )
        provider.connect()
        with pytest.raises(CapabilityError) as exc:
            provider.send(OutgoingMessage(segments=(Segment.image("http://x/a.png"),)))
        assert exc.value.capability == "send_image"
        assert uploaded == []
        assert stu_msg.sent == []

    def test_send_text_with_media_is_rejected(self, tmp_path):
        """#28：媒体留言不带文本，混发会静默丢文字 → 拒绝而不是谎报成功。"""
        local = tmp_path / "a.png"
        local.write_bytes(b"png")
        provider, stu_msg = _seewo_provider(uploader=lambda p: "http://cdn/x")
        provider.connect()
        with pytest.raises(CapabilityError) as exc:
            provider.send(
                OutgoingMessage(
                    segments=(Segment.text_seg("看图"), Segment.image(str(local)))
                )
            )
        assert exc.value.capability == "text_with_media"
        assert stu_msg.sent == []

    def test_send_multiple_media_is_rejected(self, tmp_path):
        """B6：一条留言只有一个 resUrl，多媒体不能只发第一个。"""
        a, b = tmp_path / "a.png", tmp_path / "b.png"
        a.write_bytes(b"1")
        b.write_bytes(b"2")
        provider, stu_msg = _seewo_provider(uploader=lambda p: "http://cdn/x")
        provider.connect()
        with pytest.raises(CapabilityError) as exc:
            provider.send(
                OutgoingMessage(segments=(Segment.image(str(a)), Segment.image(str(b))))
            )
        assert exc.value.capability == "multiple_media"
        assert stu_msg.sent == []

    def test_sent_message_has_no_fabricated_id(self):
        """B5：希沃不返回服务端消息 id，回执 id 如实留空。"""
        provider, _ = _seewo_provider()
        provider.connect()
        sent = provider.send(OutgoingMessage(segments=(Segment.text_seg("hi"),)))
        assert sent.id == ""

    def test_history_is_rejected_as_unsupported(self):
        provider, _ = _seewo_provider()
        with pytest.raises(CapabilityError):
            provider.history()

    def test_send_without_uploader_is_rejected_by_capability_gate(self):
        """未配置上传器 → 能力申报 send_image=False → 发送前即被拦下。

        这正是「能力闸门不依赖调用方自觉」的含义：调用方拿不到
        「已配置 uploader 但发送仍失败」这种不一致状态。
        """
        provider, stu_msg = _seewo_provider()
        provider.connect()
        with pytest.raises(CapabilityError) as exc:
            provider.send(OutgoingMessage(segments=(Segment.image("local/a.png"),)))
        assert exc.value.capability == "send_image"
        assert stu_msg.sent == []

    def test_describe_reports_conversation(self):
        provider, _ = _seewo_provider()
        info = provider.describe()
        assert info["platform"] == "seewo"
        assert info["conversation"]["type"] == ConversationType.DIRECT.value
        assert info["conversation"]["id"] == "student-uid"

    def test_seewo_protocol_details_are_confined_to_provider(self):
        """希沃专有字段只应出现在 native 载荷里，不应泄漏到中立模型"""
        provider, _ = _seewo_provider(result=[_raw(1)])
        provider.connect()
        message = provider.poll().messages[0]
        assert message.native["seewoType"] == 1
        # 中立模型的字段名不含希沃专有词
        assert "resUrl" not in message.to_dict()
        assert message.to_dict()["platform"] == "seewo"


class TestFakeProviderHistory:
    """FakeProvider 申报 supports_history=True，必须真的实现 history()。"""

    def test_history_returns_emitted_in_order(self):
        p = FakeProvider()
        p.emit_text("a", message_id="m1")
        p.emit_text("b", message_id="m2")
        p.emit_text("c", message_id="m3")
        assert [m.id for m in p.history()] == ["m1", "m2", "m3"]

    def test_history_before_excludes_cursor_and_later(self):
        p = FakeProvider()
        for i in range(1, 4):
            p.emit_text(str(i), message_id=f"m{i}")
        assert [m.id for m in p.history(before="m3")] == ["m1", "m2"]

    def test_history_limit_keeps_latest(self):
        p = FakeProvider()
        for i in range(1, 5):
            p.emit_text(str(i), message_id=f"m{i}")
        assert [m.id for m in p.history(limit=2)] == ["m3", "m4"]

    def test_history_filters_conversation(self):
        p = FakeProvider()
        p.emit_text("a", message_id="m1", conversation_id="c1")
        p.emit_text("b", message_id="m2", conversation_id="c2")
        assert [m.id for m in p.history(conversation_id="c2")] == ["m2"]

    def test_history_unknown_before_returns_empty(self):
        p = FakeProvider()
        p.emit_text("a", message_id="m1")
        assert p.history(before="nope") == ()


class TestReplyToCapabilityGate:
    def test_reply_to_rejected_when_platform_lacks_quote(self):
        """P1-3：reply_to 也必须过能力闸门，不能静默发成普通文本。"""
        provider, stu_msg = _seewo_provider()
        provider.connect()
        with pytest.raises(CapabilityError) as exc:
            provider.send(
                OutgoingMessage(segments=(Segment.text_seg("hi"),), reply_to="m1")
            )
        assert exc.value.capability == "send_quote"
        assert stu_msg.sent == []

    def test_reply_to_allowed_when_capability_declared(self):
        provider = FakeProvider()
        provider.connect()
        sent = provider.send(
            OutgoingMessage(segments=(Segment.text_seg("hi"),), reply_to="m1")
        )
        assert sent is not None


class TestSeewoTimeoutTranslation:
    def test_requests_timeout_maps_to_provider_timeout(self):
        """P2-3：超时要映射成 ProviderTimeoutError，REST 才能给出 504。"""
        import requests

        from seewo.errors import ProviderTimeoutError

        exc = SeewoProvider._translate(requests.Timeout("slow"))
        assert isinstance(exc, ProviderTimeoutError)
        assert exc.retryable is True
