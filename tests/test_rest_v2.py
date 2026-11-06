"""第 2 层：REST v2 接口层（seewo/interfaces/rest.py）端点测试。

用 Flask test_client + FakeProvider，全程离线。

覆盖：
- 统一响应信封（成功 / 失败）
- 统一错误码 → HTTP 状态码映射
- 鉴权
- 参数校验
- 能力缺口 → 501
- SDK 契约一致性
"""

import pytest
from flask import Flask

from seewo.core.service import MessageService
from seewo.core.store import MemoryStore
from seewo.interfaces.rest import (
    HTTP_STATUS_BY_CODE,
    ApiErrorCode,
    map_exception,
    register,
)
from seewo.models import PlatformCapabilities
from seewo.sdk import SeewoApiError, SeewoRobotClient
from seewo.testing.fake_provider import FakeProvider

TEST_API_KEY = "test-key"


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def service(provider):
    svc = MessageService(provider, MemoryStore())
    svc.connect()
    return svc


@pytest.fixture
def app(service):
    flask_app = Flask(__name__)
    register(flask_app, service, api_key=TEST_API_KEY)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth():
    return {"X-API-Key": TEST_API_KEY}


# ============================ 鉴权 ============================


class TestAuth:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/v2/status"),
            ("get", "/api/v2/capabilities"),
            ("get", "/api/v2/messages"),
            ("get", "/api/v2/history"),
            ("post", "/api/v2/messages"),
            ("post", "/api/v2/sync"),
            ("post", "/api/v2/connection"),
        ],
    )
    def test_missing_key_rejected(self, client, method, path):
        resp = getattr(client, method)(path)
        assert resp.status_code == 401
        body = resp.get_json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "unauthorized"

    def test_wrong_key_rejected(self, client):
        resp = client.get("/api/v2/status", headers={"X-API-Key": "nope"})
        assert resp.status_code == 401

    def test_query_param_key_accepted(self, client):
        resp = client.get(f"/api/v2/status?api_key={TEST_API_KEY}")
        assert resp.status_code == 200


# ============================ 响应信封 ============================


class TestEnvelope:
    def test_success_envelope(self, client, auth):
        body = client.get("/api/v2/status", headers=auth).get_json()
        assert body["status"] == "ok"
        assert "data" in body
        assert "error" not in body

    def test_error_envelope(self, client, auth):
        body = client.post(
            "/api/v2/messages", headers=auth, json={"segments": []}
        ).get_json()
        assert body["status"] == "error"
        assert body["data"] is None
        assert body["error"]["code"] == "invalid_request"
        assert "message" in body["error"]


# ============================ /status ============================


class TestStatus:
    def test_reports_platform_and_state(self, client, auth):
        data = client.get("/api/v2/status", headers=auth).get_json()["data"]
        assert data["platform"] == "fake"
        assert data["connection"]["state"] == "connected"
        assert data["connection"]["session_expired"] is False
        assert "capabilities" in data

    def test_capabilities_endpoint(self, client, auth):
        data = client.get("/api/v2/capabilities", headers=auth).get_json()["data"]
        assert data["send_text"] is True
        assert "send_image" in data


# ============================ /messages ============================


class TestMessages:
    def test_poll_returns_messages(self, client, auth, provider):
        provider.emit_text("你好", message_id="m1")
        data = client.get("/api/v2/messages?count=10", headers=auth).get_json()["data"]
        assert data["count"] == 1
        assert data["messages"][0]["id"] == "m1"
        assert data["messages"][0]["segments"][0]["text"] == "你好"

    def test_poll_is_deduplicated(self, client, auth, provider):
        provider.emit_text("a", message_id="m1")
        client.get("/api/v2/messages", headers=auth)
        provider.emit_text("a", message_id="m1")
        data = client.get("/api/v2/messages", headers=auth).get_json()["data"]
        assert data["count"] == 0

    def test_send_with_content_shorthand(self, client, auth, provider):
        resp = client.post("/api/v2/messages", headers=auth, json={"content": "你好"})
        assert resp.status_code == 200
        assert provider.sent[0].segments[0].text == "你好"

    def test_send_with_segments(self, client, auth, provider):
        resp = client.post(
            "/api/v2/messages",
            headers=auth,
            json={
                "conversation_id": "c1",
                "segments": [
                    {"type": "text", "text": "看这个"},
                    {"type": "image", "url": "http://x/a.png"},
                ],
            },
        )
        assert resp.status_code == 200
        assert len(provider.sent[0].segments) == 2

    def test_send_empty_rejected(self, client, auth, provider):
        resp = client.post("/api/v2/messages", headers=auth, json={})
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "invalid_request"
        assert provider.sent == ()

    def test_send_unsupported_capability_returns_501(self, client, auth, provider):
        """平台能力不足 → 501 capability（不假装成功）"""
        provider.capabilities = PlatformCapabilities(send_text=True, send_image=False)
        resp = client.post(
            "/api/v2/messages",
            headers=auth,
            json={"segments": [{"type": "image", "url": "http://x/a.png"}]},
        )
        assert resp.status_code == 501
        body = resp.get_json()
        assert body["error"]["code"] == "capability"
        assert body["error"]["details"]["capability"] == "send_image"
        assert provider.sent == ()

    def test_send_upstream_error_returns_502(self, client, auth, provider):
        from seewo.errors import ProviderBusinessError

        provider.fail_next_send(ProviderBusinessError("内容超长"))
        resp = client.post("/api/v2/messages", headers=auth, json={"content": "x"})
        assert resp.status_code == 502
        assert resp.get_json()["error"]["code"] == "upstream"

    def test_out_of_range_count_is_400(self, client, auth, provider):
        """#5：OpenAPI 声明 count ∈ [1, 200]，越界返回 400，不静默回落默认值"""
        for bad in ("99999", "0", "-1"):
            resp = client.get(f"/api/v2/messages?count={bad}", headers=auth)
            assert resp.status_code == 400, bad
            assert resp.get_json()["error"]["code"] == "invalid_request"

    def test_non_integer_count_is_400(self, client, auth):
        resp = client.get("/api/v2/messages?count=abc", headers=auth)
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "invalid_request"

    def test_count_boundaries_and_default_accepted(self, client, auth):
        for ok in ("1", "200"):
            assert (
                client.get(f"/api/v2/messages?count={ok}", headers=auth).status_code
                == 200
            ), ok
        # 缺省时用默认值
        assert client.get("/api/v2/messages", headers=auth).status_code == 200


# ============================ /history ============================


class TestHistory:
    def test_history_returns_stored_messages(self, client, auth, provider):
        provider.emit_text("a", message_id="m1", timestamp=1000)
        provider.emit_text("b", message_id="m2", timestamp=2000)
        client.get("/api/v2/messages", headers=auth)
        data = client.get("/api/v2/history", headers=auth).get_json()["data"]
        assert [m["id"] for m in data["messages"]] == ["m1", "m2"]
        assert data["total"] == 2

    def test_history_pagination(self, client, auth, provider):
        for i in range(5):
            provider.emit_text(f"m{i}", message_id=f"id{i}", timestamp=1000 + i)
        client.get("/api/v2/messages", headers=auth)
        data = client.get("/api/v2/history?limit=2&offset=1", headers=auth).get_json()[
            "data"
        ]
        assert [m["id"] for m in data["messages"]] == ["id1", "id2"]


# ============================ /connection ============================


class TestConnection:
    def test_disconnect_then_connect(self, client, auth, service):
        resp = client.post(
            "/api/v2/connection", headers=auth, json={"action": "disconnect"}
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["state"] == "disconnected"

        resp = client.post(
            "/api/v2/connection", headers=auth, json={"action": "connect"}
        )
        assert resp.get_json()["data"]["state"] == "connected"

    def test_unknown_action_rejected(self, client, auth):
        resp = client.post("/api/v2/connection", headers=auth, json={"action": "bogus"})
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "invalid_request"


# ============================ /sync ============================


class TestSync:
    def test_sync_returns_count(self, client, auth, provider):
        provider.emit_text("a", message_id="m1")
        data = client.post("/api/v2/sync", headers=auth, json={}).get_json()["data"]
        assert data["count"] == 1


# ============================ 错误映射 ============================


class TestErrorMapping:
    def test_all_codes_have_http_status(self):
        """每个错误码都必须有对应的 HTTP 状态码"""
        for code in ApiErrorCode:
            assert code in HTTP_STATUS_BY_CODE

    def test_capability_error_maps_to_501(self):
        from seewo.errors import CapabilityError

        err = map_exception(CapabilityError("send_audio"))
        assert err.code is ApiErrorCode.CAPABILITY
        assert HTTP_STATUS_BY_CODE[err.code] == 501

    def test_session_expired_maps_to_need_login(self):
        from seewo.errors import SessionExpiredError

        err = map_exception(SessionExpiredError("过期"))
        assert err.code is ApiErrorCode.NEED_LOGIN

    def test_provider_timeout_maps_to_504(self):
        from seewo.errors import ProviderTimeoutError

        err = map_exception(ProviderTimeoutError("超时"))
        assert err.code is ApiErrorCode.TIMEOUT

    def test_unknown_exception_maps_to_internal(self):
        err = map_exception(RuntimeError("boom"))
        assert err.code is ApiErrorCode.INTERNAL


# ============================ SDK 契约 ============================


class TestSdk:
    """SDK 与 REST 层共用同一契约：这里用假 transport 驱动 SDK 的解析路径。"""

    @staticmethod
    def _sdk_against(test_client, status_code=200, payload=None):
        """构造一个把请求转交给 Flask test_client 的 SDK。"""

        class FakeResponse:
            def __init__(self, resp):
                self.status_code = resp.status_code
                self._payload = resp.get_json()

            def json(self):
                return self._payload

        class FakeSession:
            def request(self, method, url, headers=None, timeout=None, **kwargs):
                path = url.split("://", 1)[1].split("/", 1)[1]
                params = kwargs.pop("params", None)
                json_body = kwargs.pop("json", None)
                resp = test_client.open(
                    path,
                    method=method,
                    headers=headers,
                    query_string=params,
                    json=json_body,
                )
                return FakeResponse(resp)

            def close(self):
                pass

        return SeewoRobotClient(
            base_url="http://test", api_key=TEST_API_KEY, session=FakeSession()
        )

    def test_sdk_status_parses_success_envelope(self, service):
        flask_app = Flask(__name__)
        register(flask_app, service, api_key=TEST_API_KEY)
        flask_app.config["TESTING"] = True
        sdk = self._sdk_against(flask_app.test_client())

        data = sdk.status()
        assert data["platform"] == "fake"
        assert data["connection"]["state"] == "connected"

    def test_sdk_send_text_roundtrip(self, service, provider):
        flask_app = Flask(__name__)
        register(flask_app, service, api_key=TEST_API_KEY)
        flask_app.config["TESTING"] = True
        sdk = self._sdk_against(flask_app.test_client())

        result = sdk.send_text("你好", conversation_id="c1")
        assert result.ok is True
        assert provider.sent[0].segments[0].text == "你好"

    def test_sdk_messages_parses_into_models(self, service, provider):
        flask_app = Flask(__name__)
        register(flask_app, service, api_key=TEST_API_KEY)
        flask_app.config["TESTING"] = True
        provider.emit_text("你好", message_id="m1")
        sdk = self._sdk_against(flask_app.test_client())

        messages = sdk.messages(count=10)
        assert len(messages) == 1
        assert messages[0].text == "你好"

    def test_sdk_raises_on_error_envelope(self, service):
        """SDK 把错误信封转成 SeewoApiError"""

        class FakeResponse:
            status_code = 400
            text = ""

            def json(self):
                return {
                    "status": "error",
                    "error": {"code": "invalid_request", "message": "坏了"},
                    "data": None,
                }

        class FakeSession:
            def request(self, *args, **kwargs):
                return FakeResponse()

            def close(self):
                pass

        sdk = SeewoRobotClient(
            base_url="http://test", api_key=TEST_API_KEY, session=FakeSession()
        )
        with pytest.raises(SeewoApiError) as exc:
            sdk.status()
        assert exc.value.code == "invalid_request"
        assert exc.value.status == 400

    def test_sdk_raises_on_non_json_response(self, service):
        """服务端返回非 JSON 时给出明确错误，而不是 json 解码异常"""

        class FakeResponse:
            status_code = 502
            text = "<html>bad gateway</html>"

            def json(self):
                raise ValueError("not json")

        class FakeSession:
            def request(self, *args, **kwargs):
                return FakeResponse()

            def close(self):
                pass

        sdk = SeewoRobotClient(
            base_url="http://test", api_key=TEST_API_KEY, session=FakeSession()
        )
        with pytest.raises(SeewoApiError) as exc:
            sdk.status()
        assert exc.value.code == "invalid_response"


# ============================ 契约收紧（批次 6 回归） ============================


class TestRequestValidation:
    """非法输入必须返回 400 信封，不能 500，也不能静默替换。"""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/api/v2/messages"),
            ("post", "/api/v2/sync"),
            ("post", "/api/v2/connection"),
        ],
    )
    def test_non_object_body_is_400(self, client, auth, method, path):
        """#4：请求体是数组时返回 invalid_request，而不是 500"""
        resp = getattr(client, method)(path, headers=auth, json=[1])
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "invalid_request"

    def test_sync_without_body_still_works(self, client, auth):
        """缺省请求体仍按默认参数执行（兼容既有调用方）"""
        assert client.post("/api/v2/sync", headers=auth).status_code == 200

    def test_segments_with_non_object_element_is_400(self, client, auth, provider):
        """#23：混入非对象元素时整条拒绝，不静默丢弃后照发"""
        resp = client.post(
            "/api/v2/messages",
            headers=auth,
            json={"segments": [1, {"type": "text", "text": "hi"}]},
        )
        assert resp.status_code == 400
        assert provider.sent == ()

    @pytest.mark.parametrize(
        "query", ["limit=501", "limit=0", "limit=abc", "offset=-1", "offset=x"]
    )
    def test_invalid_history_params_are_400(self, client, auth, query):
        """#5：分页参数非法时返回 400，不回落成默认页"""
        resp = client.get(f"/api/v2/history?{query}", headers=auth)
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "invalid_request"


def _sdk_with_response(status_code, body):
    from seewo.sdk import SeewoRobotClient

    class FakeResponse:
        def __init__(self):
            self.status_code = status_code

        def json(self):
            return body

    class FakeSession:
        def request(self, *args, **kwargs):
            return FakeResponse()

        def close(self):
            pass

    return SeewoRobotClient(base_url="http://test", session=FakeSession())


class TestSdkContract:
    @pytest.mark.parametrize("body", [{}, None, [], {"data": {"id": "x"}}])
    def test_non_envelope_200_is_not_success(self, body):
        """#8：HTTP 200 但不是统一信封 → invalid_response，不能当发送成功"""
        from seewo.sdk import SeewoApiError

        sdk = _sdk_with_response(200, body)
        with pytest.raises(SeewoApiError) as exc:
            sdk.send_text("hi")
        assert exc.value.code == "invalid_response"

    def test_network_error_is_wrapped(self):
        """#7：网络异常统一为 SeewoApiError，原始异常保留为 __cause__"""
        import requests

        from seewo.sdk import SeewoApiError, SeewoRobotClient

        class BrokenSession:
            def request(self, *args, **kwargs):
                raise requests.ConnectionError("refused")

            def close(self):
                pass

        sdk = SeewoRobotClient(base_url="http://test", session=BrokenSession())
        with pytest.raises(SeewoApiError) as exc:
            sdk.status()
        assert exc.value.code == "network"
        assert isinstance(exc.value.__cause__, requests.ConnectionError)

    def test_status_code_alias(self):
        """#6：MIGRATION.md 文档化的 `.status_code` 可用，且与 `.status` 一致"""
        from seewo.sdk import SeewoApiError

        sdk = _sdk_with_response(
            401,
            {"status": "error", "error": {"code": "unauthorized", "message": "x"}},
        )
        with pytest.raises(SeewoApiError) as exc:
            sdk.status()
        assert exc.value.status_code == 401
        assert exc.value.status == 401
