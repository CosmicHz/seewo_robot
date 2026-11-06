"""第 3 层：第三方框架协议适配器测试（OneBot 11 / Satori）。

**全程离线** —— 用 FakeProvider 替代希沃服务，不发出任何网络请求。
覆盖：
- 协议翻译的正确性（双向）
- 能力缺口在协议层的表现（明确的错误码，不伪造）
- 会话恢复 / 心跳 / 断线重连
"""

import json

import pytest

from seewo.core.service import MessageService
from seewo.core.store import MemoryStore
from seewo.integrations.onebot11 import OneBot11Adapter
from seewo.integrations.satori import SatoriAdapter
from seewo.models import (
    EventType,
    Message,
    PlatformCapabilities,
    Segment,
    SegmentType,
)
from seewo.testing.fake_provider import FakeProvider


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def captured():
    """事件捕获列表。必须在 service 建好**之前**订阅，才能抓到 CONNECTED。"""
    return []


@pytest.fixture
def service(provider, captured):
    svc = MessageService(provider, MemoryStore())
    svc.on_any(captured.append)
    svc.connect()
    return svc


# ===========================================================================
# OneBot 11
# ===========================================================================


class TestOneBotEvents:
    def test_message_event_shape(self, service, provider):
        """收到的消息 → OneBot 11 私聊消息事件，字段名对齐官方 spec"""
        ob = OneBot11Adapter(service)
        provider.emit_text(
            "你好", message_id="m1", user_id="u1", timestamp=1700000000000
        )
        service.poll_once()

        payload = ob.translate_event(
            next(e for e in _captured(service) if e.type is EventType.MESSAGE)
        )
        assert payload["post_type"] == "message"
        assert payload["message_type"] == "private"
        assert payload["sub_type"] == "friend"
        assert payload["message_id"] == "m1"
        assert payload["user_id"] == "u1"
        assert payload["time"] == 1700000000
        assert payload["message"] == [{"type": "text", "data": {"text": "你好"}}]
        assert payload["raw_message"] == "你好"
        assert payload["sender"]["user_id"] == "u1"

    def test_image_message_segment(self, service, provider):
        ob = OneBot11Adapter(service)
        provider.emit(
            Message(
                id="m1",
                platform="fake",
                user_id="u1",
                timestamp=1000,
                segments=(Segment.image("http://x/a.png"),),
            )
        )
        service.poll_once()
        payload = ob.translate_event(
            next(e for e in _captured(service) if e.type is EventType.MESSAGE)
        )
        assert payload["message"] == [
            {"type": "image", "data": {"file": "http://x/a.png"}}
        ]

    def test_lifecycle_event(self, service):
        ob = OneBot11Adapter(service)
        payload = ob.build_lifecycle("connect")
        assert payload["post_type"] == "meta_event"
        assert payload["meta_event_type"] == "lifecycle"
        assert payload["sub_type"] == "connect"

    def test_sent_message_has_no_onebot_event(self, service):
        """OneBot 11 没有"已发送"标准事件 → 不伪造"""
        ob = OneBot11Adapter(service)
        service.send([Segment.text_seg("hi")])
        events = [e for e in _captured(service) if e.type is EventType.MESSAGE_SENT]
        for e in events:
            assert ob.translate_event(e) is None


class TestOneBotApi:
    def test_send_msg_success(self, service, provider):
        ob = OneBot11Adapter(service)
        result = ob.handle_api(
            "send_msg",
            {"message_type": "private", "message": "你好", "user_id": "u1"},
        )
        assert result["status"] == "ok"
        assert result["retcode"] == 0
        assert result["data"]["message_id"]
        assert provider.sent[0].segments[0].text == "你好"

    def test_send_msg_without_platform_id_returns_null(self, service, provider):
        """平台不返回消息 id（如希沃）→ message_id 为 null，不伪造一个查不到的 id"""
        from seewo.models import Role

        def no_id_send(outgoing):
            return Message(id="", platform="fake", role=Role.SELF)

        provider._send = no_id_send
        ob = OneBot11Adapter(service)
        result = ob.handle_api(
            "send_msg",
            {"message_type": "private", "message": "你好", "user_id": "u1"},
        )
        assert result["status"] == "ok"
        assert result["data"]["message_id"] is None

    def test_send_msg_rejects_group(self, service, provider):
        """不支持群聊时明确失败，不静默降级"""
        ob = OneBot11Adapter(service)
        result = ob.handle_api(
            "send_msg",
            {"message_type": "group", "message": "x", "group_id": "g1"},
        )
        assert result["status"] == "failed"
        assert result["retcode"] == 100
        assert "private" in result["msg"]
        assert provider.sent == ()

    def test_send_private_msg_works_without_message_type(self, service, provider):
        """`send_private_msg` 是独立 action（不少框架直接调它），不该落到「未实现」"""
        ob = OneBot11Adapter(service)
        result = ob.handle_api("send_private_msg", {"message": "你好", "user_id": "u1"})
        assert result["status"] == "ok"
        assert result["retcode"] == 0
        assert provider.sent[0].segments[0].text == "你好"

    def test_send_group_msg_is_explicitly_unsupported(self, service, provider):
        """`send_group_msg` 必须明确拒绝，不能偷偷降级成私聊"""
        ob = OneBot11Adapter(service)
        result = ob.handle_api("send_group_msg", {"message": "x", "group_id": "g1"})
        assert result["status"] == "failed"
        assert result["retcode"] == 100
        assert provider.sent == ()

    def test_empty_message_is_illegal_param(self, service):
        ob = OneBot11Adapter(service)
        result = ob.handle_api("send_msg", {"message_type": "private", "message": ""})
        assert result["status"] == "failed"
        assert result["retcode"] == 1400

    def test_unknown_api_returns_failure(self, service):
        ob = OneBot11Adapter(service)
        result = ob.handle_api("nonexistent_api", {})
        assert result["status"] == "failed"
        assert result["retcode"] == 100

    def test_get_version_info(self, service):
        ob = OneBot11Adapter(service)
        data = ob.handle_api("get_version_info", {})["data"]
        assert data["protocol_version"] == "v11"

    def test_get_login_info(self, service):
        ob = OneBot11Adapter(service)
        data = ob.handle_api("get_login_info", {})["data"]
        assert "user_id" in data and "nickname" in data

    def test_get_group_list_is_empty(self, service):
        """没有群概念 → 返回空列表（不是错误）"""
        ob = OneBot11Adapter(service)
        assert ob.handle_api("get_group_list", {})["data"] == []

    def test_get_msg_returns_message(self, service, provider):
        ob = OneBot11Adapter(service)
        provider.emit_text("你好", message_id="m1")
        service.poll_once()
        data = ob.handle_api("get_msg", {"message_id": "m1"})["data"]
        assert data["message_id"] == "m1"
        assert data["message"][0]["data"]["text"] == "你好"

    def test_get_msg_missing_is_illegal_param(self, service):
        ob = OneBot11Adapter(service)
        result = ob.handle_api("get_msg", {"message_id": "nope"})
        assert result["status"] == "failed"
        assert result["retcode"] == 1400

    def test_capability_error_maps_to_unsupported_retcode(self, service, provider):
        """平台能力不足 → retcode 100（不假装成功）"""
        provider.capabilities = PlatformCapabilities(send_text=True, send_image=False)
        ob = OneBot11Adapter(service)
        result = ob.handle_api(
            "send_msg",
            {
                "message_type": "private",
                "message": [{"type": "image", "data": {"file": "http://x/a.png"}}],
                "user_id": "u1",
            },
        )
        assert result["status"] == "failed"
        assert result["retcode"] == 100
        assert provider.sent == ()

    def test_delete_msg_uses_provider_recall(self, service, provider):
        ob = OneBot11Adapter(service)
        result = ob.handle_api("delete_msg", {"message_id": "m1"})
        assert result["status"] == "ok"
        assert "m1" in provider.remembered

    def test_delete_msg_unsupported_is_reported(self, service, provider):
        provider.capabilities = PlatformCapabilities(send_text=True)
        ob = OneBot11Adapter(service)
        result = ob.handle_api("delete_msg", {"message_id": "m1"})
        assert result["status"] == "failed"
        assert result["retcode"] == 100


class TestOneBotParsing:
    def test_parse_string_message(self, service):
        ob = OneBot11Adapter(service)
        msg = ob.parse_incoming(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 1,
                "user_id": 2,
                "message": "你好",
                "time": 100,
            }
        )
        assert msg.text == "你好"

    def test_parse_segment_array(self, service):
        ob = OneBot11Adapter(service)
        msg = ob.parse_incoming(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 1,
                "user_id": 2,
                "message": [
                    {"type": "text", "data": {"text": "看"}},
                    {"type": "image", "data": {"file": "http://x/a.png"}},
                ],
                "time": 100,
            }
        )
        assert msg.segments[0].type is SegmentType.TEXT
        assert msg.segments[1].type is SegmentType.IMAGE
        assert msg.segments[1].url == "http://x/a.png"

    def test_parse_rejects_group(self, service):
        from seewo.integrations.base import TranslationError

        ob = OneBot11Adapter(service)
        with pytest.raises(TranslationError):
            ob.parse_incoming({"post_type": "message", "message_type": "group"})

    def test_parse_rejects_non_message(self, service):
        from seewo.integrations.base import TranslationError

        ob = OneBot11Adapter(service)
        with pytest.raises(TranslationError):
            ob.parse_incoming({"post_type": "meta_event"})


# ===========================================================================
# Satori
# ===========================================================================


class TestSatoriElements:
    def test_encode_text_only(self):
        content = SatoriAdapter.encode_content((Segment.text_seg("你好"),))
        assert content == "你好"

    def test_encode_image(self):
        content = SatoriAdapter.encode_content(
            (Segment.image("http://x/a.png", name="a.png"),)
        )
        assert 'src="http://x/a.png"' in content
        assert 'title="a.png"' in content
        assert content.startswith("<img ")

    def test_encode_escapes_text(self):
        content = SatoriAdapter.encode_content((Segment.text_seg("a<b&c"),))
        assert "&lt;" in content and "&amp;" in content

    def test_encode_at(self):
        content = SatoriAdapter.encode_content((Segment.at("u1", name="Bob"),))
        assert 'id="u1"' in content and 'name="Bob"' in content

    @pytest.mark.parametrize(
        ("raw", "expect_type", "expect_value"),
        [
            ("<img src='http://x/a.png'/>", SegmentType.IMAGE, "http://x/a.png"),
            ('<img src="http://x/a.png"/>', SegmentType.IMAGE, "http://x/a.png"),
            ("<audio src='http://x/a.mp3'/>", SegmentType.AUDIO, "http://x/a.mp3"),
            ("<file src='http://x/a.pdf'/>", SegmentType.FILE, "http://x/a.pdf"),
        ],
    )
    def test_parse_elements(self, raw, expect_type, expect_value):
        segments = SatoriAdapter.parse_content(raw)
        assert segments[0].type is expect_type
        assert segments[0].url == expect_value

    def test_parse_mixed_content(self):
        segments = SatoriAdapter.parse_content(
            '看这个 <img src="http://x/a.png"/> 谢谢'
        )
        assert [s.type for s in segments] == [
            SegmentType.TEXT,
            SegmentType.IMAGE,
            SegmentType.TEXT,
        ]
        assert segments[0].text == "看这个 "

    def test_parse_at_element(self):
        segments = SatoriAdapter.parse_content('<at id="u1" name="Bob"/>')
        assert segments[0].type is SegmentType.AT
        assert segments[0].user_id == "u1"

    def test_roundtrip(self):
        original = (
            Segment.text_seg("看这个"),
            Segment.image("http://x/a.png", name="a.png"),
        )
        content = SatoriAdapter.encode_content(original)
        parsed = SatoriAdapter.parse_content(content)
        assert parsed[0].text == "看这个"
        assert parsed[1].url == "http://x/a.png"
        assert parsed[1].name == "a.png"


class TestSatoriSignals:
    def test_ping_returns_pong(self, service):
        sa = SatoriAdapter(service)
        frames = sa.handle_signal(json.dumps({"op": 1}))
        assert frames[0]["op"] == 2

    def test_identify_returns_ready(self, service):
        sa = SatoriAdapter(service)
        frames = sa.handle_signal(
            json.dumps({"op": 3, "body": {"token": None, "sn": 0}})
        )
        assert frames[0]["op"] == 4
        assert "logins" in frames[0]["body"]
        assert "proxy_urls" in frames[0]["body"]

    def test_identify_with_wrong_token_is_rejected(self, service):
        sa = SatoriAdapter(service)
        sa.config.token = "secret"
        frames = sa.handle_signal(json.dumps({"op": 3, "body": {"token": "wrong"}}))
        assert frames[0]["op"] == 0
        assert frames[0]["body"]["type"] == "error"

    def test_identify_with_correct_token_is_accepted(self, service):
        sa = SatoriAdapter(service)
        sa.config.token = "secret"
        frames = sa.handle_signal(json.dumps({"op": 3, "body": {"token": "secret"}}))
        assert frames[0]["op"] == 4

    def test_process_signal_reports_rejection(self, service):
        """#35：令牌错误 → rejected=True / identified=False，服务端据此断开且不登记连接"""
        sa = SatoriAdapter(service)
        sa.config.token = "secret"
        result = sa.process_signal(json.dumps({"op": 3, "body": {"token": "wrong"}}))
        assert result.rejected is True
        assert result.identified is False

    def test_process_signal_reports_identified(self, service):
        sa = SatoriAdapter(service)
        sa.config.token = "secret"
        result = sa.process_signal(json.dumps({"op": 3, "body": {"token": "secret"}}))
        assert result.identified is True
        assert result.rejected is False

    def test_ping_before_identify_does_not_identify(self, service):
        """IDENTIFY 之前的 PING 只回 PONG，不能让连接进入广播集合"""
        sa = SatoriAdapter(service)
        sa.config.token = "secret"
        result = sa.process_signal(json.dumps({"op": 1}))
        assert result.identified is False
        assert result.frames[0]["op"] == 2

    def test_token_getter_takes_precedence_and_is_live(self, service):
        """令牌每次实时读取：getter 换值后旧令牌立即失效"""
        current = {"key": "k1"}
        sa = SatoriAdapter(service)
        sa.config.token = "static"
        sa.config.token_getter = lambda: current["key"]
        assert sa.check_token("k1")
        assert not sa.check_token("static")
        current["key"] = "k2"
        assert not sa.check_token("k1")
        assert sa.check_token("k2")

    def test_no_token_configured_allows_all(self, service):
        sa = SatoriAdapter(service)
        assert sa.check_token(None)
        assert sa.check_token("anything")

    def test_resume_sn_advances_counter(self, service):
        """断线恢复：IDENTIFY 带 sn → 后续事件 sn 接着往下发，不重发旧事件"""
        sa = SatoriAdapter(service)
        sa.handle_signal(json.dumps({"op": 3, "body": {"token": None, "sn": 42}}))
        assert sa.current_sn() == 42
        frame = sa.event_frame(
            next(e for e in _captured(service) if e.type is EventType.CONNECTED)
        )
        assert frame["body"]["sn"] == 43

    def test_invalid_json_raises(self, service):
        from seewo.integrations.base import TranslationError

        sa = SatoriAdapter(service)
        with pytest.raises(TranslationError):
            sa.handle_signal("{not json")

    def test_unknown_opcode_raises(self, service):
        from seewo.integrations.base import TranslationError

        sa = SatoriAdapter(service)
        with pytest.raises(TranslationError):
            sa.handle_signal(json.dumps({"op": 99}))

    def test_client_event_signal_is_ignored(self, service):
        sa = SatoriAdapter(service)
        assert sa.handle_signal(json.dumps({"op": 0, "body": {}})) == []


class TestSatoriEvents:
    def test_message_event_shape(self, service, provider):
        sa = SatoriAdapter(service)
        provider.emit_text(
            "你好", message_id="m1", user_id="u1", timestamp=1700000000000
        )
        service.poll_once()
        event = next(e for e in _captured(service) if e.type is EventType.MESSAGE)
        body = sa.translate_event(event)
        assert body["type"] == "message-created"
        assert body["message"]["id"] == "m1"
        assert body["message"]["content"] == "你好"
        assert body["user"]["id"] == "u1"
        assert body["channel"]["id"] == "c1"

    def test_event_frame_wraps_in_op_envelope(self, service, provider):
        sa = SatoriAdapter(service)
        provider.emit_text("x", message_id="m1")
        service.poll_once()
        event = next(e for e in _captured(service) if e.type is EventType.MESSAGE)
        frame = sa.event_frame(event)
        assert frame["op"] == 0
        assert "sn" in frame["body"]

    def test_login_block_contains_required_fields(self, service):
        sa = SatoriAdapter(service)
        block = sa._login_block()
        assert set(block) >= {"sn", "user", "platform"}


class TestSatoriApi:
    def test_message_create_returns_array(self, service, provider):
        """Satori 规范：message.create 返回数组"""
        sa = SatoriAdapter(service)
        result = sa.handle_api(
            "message.create", {"channel_id": "c1", "content": "你好"}
        )
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["segments"][0]["text"] == "你好"
        assert provider.sent[0].segments[0].text == "你好"

    def test_message_create_requires_content(self, service):
        sa = SatoriAdapter(service)
        assert "error" in sa.handle_api("message.create", {"channel_id": "c1"})

    def test_message_create_with_image_element(self, service, provider):
        sa = SatoriAdapter(service)
        sa.handle_api(
            "message.create",
            {"channel_id": "c1", "content": '<img src="http://x/a.png"/>'},
        )
        assert provider.sent[0].segments[0].type is SegmentType.IMAGE

    def test_unknown_api_returns_error(self, service):
        sa = SatoriAdapter(service)
        assert "error" in sa.handle_api("nonexistent.method", {})

    def test_channel_get(self, service):
        sa = SatoriAdapter(service)
        data = sa.handle_api("channel.get", {"channel_id": "c1"})
        assert data["id"] == "c1"


# ===========================================================================
# 共享基础设施
# ===========================================================================


class TestIntegrationInfrastructure:
    def test_broadcast_reaches_all_connections(self, service):
        ob = OneBot11Adapter(service)

        class FakeWS:
            def __init__(self):
                self.frames = []

            def send(self, payload):
                self.frames.append(payload)

            def close(self):
                pass

        conns = [FakeWS(), FakeWS()]
        for conn in conns:
            ob.register_connection(conn)
        assert ob.connection_count == 2

        ob.broadcast({"hello": "world"})
        for conn in conns:
            assert conn.frames == [{"hello": "world"}]

    def test_failing_connection_does_not_block_others(self, service):
        ob = OneBot11Adapter(service)

        class BadWS:
            def send(self, payload):
                raise RuntimeError("closed")

            def close(self):
                pass

        class GoodWS:
            def __init__(self):
                self.frames = []

            def send(self, payload):
                self.frames.append(payload)

            def close(self):
                pass

        bad, good = BadWS(), GoodWS()
        ob.register_connection(bad)
        ob.register_connection(good)
        assert ob.broadcast({"x": 1}) == 1
        assert good.frames == [{"x": 1}]

    def test_unregister_removes_connection(self, service):
        ob = OneBot11Adapter(service)

        class FakeWS:
            def close(self):
                pass

        conn = FakeWS()
        ob.register_connection(conn)
        ob.unregister_connection(conn)
        assert ob.connection_count == 0

    def test_backoff_is_exponential_and_capped(self, service):
        ob = OneBot11Adapter(service)
        ob.config.reconnect_base_delay = 1.0
        ob.config.reconnect_max_delay = 8.0
        assert ob.backoff_delay(1) == 1.0
        assert ob.backoff_delay(2) == 2.0
        assert ob.backoff_delay(3) == 4.0
        assert ob.backoff_delay(10) == 8.0

    def test_should_retry_respects_limit(self, service):
        ob = OneBot11Adapter(service)
        ob.config.max_reconnect_attempts = 2
        assert ob.should_retry(1) is True
        assert ob.should_retry(2) is False

    def test_should_retry_unlimited_by_default(self, service):
        ob = OneBot11Adapter(service)
        assert ob.config.max_reconnect_attempts == 0
        assert ob.should_retry(9999) is True

    def test_resume_cursor_reads_from_store(self, service, provider):
        ob = OneBot11Adapter(service)
        provider.emit_text("a", message_id="m1")
        service.poll_once()
        assert ob.resume_cursor() is not None

    def test_resume_cursor_disabled_returns_none(self, service):
        ob = OneBot11Adapter(service)
        ob.config.resend_cursor = False
        assert ob.resume_cursor() is None

    def test_deliver_uses_core_send(self, service, provider):
        ob = OneBot11Adapter(service)
        ob.deliver(
            Message(
                id="",
                platform="seewo",
                conversation_id="c1",
                segments=(Segment.text_seg("hi"),),
            )
        )
        assert provider.sent[0].segments[0].text == "hi"

    def test_stop_disconnects_service(self, service):
        ob = OneBot11Adapter(service)
        ob.stop()
        assert service.state.value == "disconnected"


def _captured(service):
    """返回服务已派发的所有事件。

    订阅在 `service` fixture 里完成（早于 connect），因此 CONNECTED 也在内。
    """
    return service._captured_events


@pytest.fixture(autouse=True)
def _attach_capture(service, captured):
    """把捕获列表挂到 service 上，供 `_captured()` 取用。"""
    service._captured_events = captured
    yield
