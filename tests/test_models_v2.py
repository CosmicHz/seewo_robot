"""第 1 层：平台中立模型（seewo/models.py）与异常体系。

覆盖：序列化往返、字段可选性、排序键、能力声明、状态机跃迁表。
"""

import dataclasses

import pytest

from seewo.errors import (
    CapabilityError,
    InvalidStateTransition,
    ProviderAuthError,
    ProviderBusinessError,
    ProviderConnectionError,
    ProviderError,
    SeewoError,
)
from seewo.models import (
    CAPABILITY_FIELD_BY_SEGMENT,
    ConnectionState,
    Conversation,
    ConversationType,
    Event,
    EventType,
    Message,
    PlatformCapabilities,
    Role,
    Segment,
    SegmentType,
    User,
)

# ============================ 消息段 ============================


class TestSegment:
    def test_text_segment_roundtrip(self):
        seg = Segment.text_seg("你好")
        assert seg.to_dict() == {"type": "text", "text": "你好", "data": {}}
        assert Segment.from_dict(seg.to_dict()) == seg

    def test_image_segment_carries_url_and_mime(self):
        seg = Segment.image("http://x/a.png", mime="image/png", name="a.png")
        d = seg.to_dict()
        assert d["type"] == "image"
        assert d["url"] == "http://x/a.png"
        assert d["mime"] == "image/png"
        assert Segment.from_dict(d) == seg

    def test_empty_fields_are_omitted(self):
        """空字段不输出，避免消费方收到一堆空串"""
        d = Segment.text_seg("hi").to_dict()
        assert "url" not in d
        assert "mime" not in d
        assert "duration" not in d

    def test_data_is_always_present(self):
        """data 始终输出，保证结构稳定"""
        assert "data" in Segment.text_seg("hi").to_dict()

    def test_unknown_type_does_not_lose_data(self):
        seg = Segment.from_dict({"type": "weird", "text": "x", "data": {"a": 1}})
        assert seg.type is SegmentType.UNKNOWN
        assert seg.text == "x"
        assert seg.data == {"a": 1}

    def test_negative_duration_is_normalised(self):
        assert Segment.from_dict({"type": "audio", "duration": -5}).duration == 0

    def test_quote_and_at_segments(self):
        assert Segment.quote("m1").to_dict()["message_id"] == "m1"
        assert Segment.at("u1", name="Bob").to_dict()["user_id"] == "u1"

    def test_segment_is_frozen(self):
        seg = Segment.text_seg("x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            seg.text = "y"  # type: ignore[misc]


# ============================ 消息 ============================


class TestMessage:
    def _msg(self, **kw):
        base = {
            "id": "m1",
            "platform": "fake",
            "conversation_id": "c1",
            "user_id": "u1",
            "segments": (Segment.text_seg("hello"),),
            "timestamp": 1700000000000,
        }
        base.update(kw)
        return Message(**base)

    def test_roundtrip(self):
        m = self._msg()
        assert Message.from_dict(m.to_dict()) == m

    def test_text_property_concatenates_text_segments(self):
        m = self._msg(
            segments=(
                Segment.text_seg("a"),
                Segment.image("http://x"),
                Segment.text_seg("b"),
            )
        )
        assert m.text == "ab"

    def test_text_property_empty_when_no_text(self):
        assert self._msg(segments=(Segment.image("http://x"),)).text == ""

    def test_sort_key_is_timestamp_then_id(self):
        assert self._msg(timestamp=1, id="b").sort_key == (1, "b")
        assert self._msg(timestamp=2, id="a").sort_key == (2, "a")

    def test_same_millisecond_is_deterministic(self):
        """同一毫秒内的两条消息也有确定顺序（可重现）"""
        a = self._msg(id="a", timestamp=100)
        b = self._msg(id="b", timestamp=100)
        assert a.sort_key < b.sort_key

    def test_unknown_role_falls_back_to_user(self):
        m = Message.from_dict({"id": "m", "role": "bogus"})
        assert m.role is Role.USER

    def test_segments_of_filters_by_type(self):
        m = self._msg(
            segments=(Segment.text_seg("a"), Segment.image("u"), Segment.image("v"))
        )
        assert len(m.segments_of(SegmentType.IMAGE)) == 2

    def test_native_payload_is_preserved(self):
        m = self._msg(native={"seewoType": 2})
        assert m.to_dict()["native"] == {"seewoType": 2}
        assert Message.from_dict(m.to_dict()).native == {"seewoType": 2}


# ============================ 用户 / 会话 ============================


class TestUserAndConversation:
    def test_user_display_name_fallback_chain(self):
        assert User(id="u1", name="N", nickname="K").display_name == "N"
        assert User(id="u1", nickname="K").display_name == "K"
        assert User(id="u1").display_name == "u1"

    def test_user_roundtrip(self):
        u = User(id="u1", name="N", nickname="K", avatar="a", is_self=True)
        assert User.from_dict(u.to_dict()) == u

    def test_conversation_roundtrip(self):
        c = Conversation(
            id="c1",
            type=ConversationType.GROUP,
            title="T",
            members=("u1", "u2"),
        )
        assert Conversation.from_dict(c.to_dict()) == c

    def test_conversation_unknown_type_falls_back_to_direct(self):
        assert Conversation.from_dict({"id": "c", "type": "weird"}).type is (
            ConversationType.DIRECT
        )


# ============================ 事件 ============================


class TestEvent:
    def test_roundtrip_with_message(self):
        e = Event(
            type=EventType.MESSAGE,
            message=Message(id="m1", platform="fake"),
            conversation_id="c1",
            timestamp=1,
        )
        assert Event.from_dict(e.to_dict()) == e

    def test_error_event_carries_message(self):
        e = Event(type=EventType.ERROR, error="boom")
        d = e.to_dict()
        assert d["error"] == "boom"
        assert "message" not in d

    def test_unknown_type_falls_back(self):
        assert Event.from_dict({"type": "nope"}).type is EventType.MESSAGE


# ============================ 能力 ============================


class TestCapabilities:
    def test_defaults_are_conservative(self):
        """默认不声明任何媒体能力 —— 默认值不得夸大平台能力"""
        caps = PlatformCapabilities()
        assert caps.send_text is True
        assert caps.send_image is False
        assert caps.send_audio is False

    def test_supports_segment_maps_correctly(self):
        caps = PlatformCapabilities(send_image=True)
        assert caps.supports_segment(Segment.image("u")) is True
        assert caps.supports_segment(Segment.audio("u")) is False

    def test_unknown_segment_is_never_supported(self):
        assert (
            PlatformCapabilities().supports_segment(Segment(type=SegmentType.UNKNOWN))
            is False
        )

    def test_roundtrip(self):
        caps = PlatformCapabilities(send_audio=True, max_text_length=199)
        assert PlatformCapabilities.from_dict(caps.to_dict()) == caps

    def test_from_dict_ignores_unknown_keys(self):
        caps = PlatformCapabilities.from_dict({"send_text": True, "bogus": 1})
        assert caps.send_text is True

    def test_every_segment_type_has_capability_mapping(self):
        """每个段类型都要有对应的能力字段，否则能力检查会 KeyError"""
        for seg_type in SegmentType:
            assert seg_type in CAPABILITY_FIELD_BY_SEGMENT


# ============================ 状态机 ============================


class TestConnectionState:
    @pytest.mark.parametrize(
        ("src", "dst"),
        [
            (ConnectionState.DISCONNECTED, ConnectionState.CONNECTING),
            (ConnectionState.CONNECTING, ConnectionState.CONNECTED),
            (ConnectionState.CONNECTING, ConnectionState.DISCONNECTED),
            (ConnectionState.CONNECTED, ConnectionState.CLOSING),
            (ConnectionState.CONNECTED, ConnectionState.DISCONNECTED),
            (ConnectionState.CLOSING, ConnectionState.DISCONNECTED),
            (ConnectionState.CONNECTED, ConnectionState.CONNECTING),
        ],
    )
    def test_legal_transitions(self, src, dst):
        assert src.can_transition_to(dst) is True

    @pytest.mark.parametrize(
        ("src", "dst"),
        [
            (ConnectionState.DISCONNECTED, ConnectionState.CONNECTED),
            (ConnectionState.DISCONNECTED, ConnectionState.CLOSING),
            (ConnectionState.CONNECTING, ConnectionState.CLOSING),
            (ConnectionState.CLOSING, ConnectionState.CONNECTED),
            (ConnectionState.CLOSING, ConnectionState.CONNECTING),
        ],
    )
    def test_illegal_transitions(self, src, dst):
        assert src.can_transition_to(dst) is False

    def test_self_transition_from_disconnected_is_allowed(self):
        """disconnected → disconnected 允许（幂等断开）"""
        assert ConnectionState.DISCONNECTED.can_transition_to(
            ConnectionState.DISCONNECTED
        )


# ============================ 异常体系 ============================


class TestErrors:
    def test_all_errors_derive_from_root(self):
        for exc in (
            CapabilityError("x"),
            ProviderError("x"),
            ProviderAuthError("x"),
            ProviderBusinessError("x"),
            ProviderConnectionError("x"),
            InvalidStateTransition("x"),
        ):
            assert isinstance(exc, SeewoError)

    def test_capability_error_carries_capability_name(self):
        e = CapabilityError("send_audio", "平台不支持")
        assert e.capability == "send_audio"
        assert "send_audio" in str(e)

    def test_retryable_defaults(self):
        assert ProviderError("x").retryable is False
        assert ProviderBusinessError("x").retryable is False
        assert ProviderAuthError("x").retryable is False
        assert ProviderConnectionError("x").retryable is True

    def test_provider_timeout_is_retryable(self):
        from seewo.errors import ProviderTimeoutError

        assert ProviderTimeoutError("x").retryable is True
