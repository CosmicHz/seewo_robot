"""协议端点挂载测试 —— 验证 api_server 真的把协议端点挂上了。

这些用例防的是一类特别隐蔽的回归：**适配器写好了、HTTP 端点也注册了，
但 WebSocket 端点从未 attach** —— 代码看起来齐全，实际连不上。

全程离线：只检查 Flask 路由表，不发起任何网络请求。
"""

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OPENAPI_PATH = REPO_ROOT / "seewo" / "interfaces" / "openapi.yaml"


@pytest.fixture(scope="module")
def api_server():
    import api_server as mod

    return mod


def _rules(app):
    return {r.rule for r in app.url_map.iter_rules()}


def _ws_rules(app):
    """以 WebSocket 方式注册的路由（flask-sock 注册时带 ``websocket=True``）。"""
    return {r.rule for r in app.url_map.iter_rules() if r.websocket}


def _bearer(api_server):
    """协议端点复用 `config.api_key` 作为令牌（每次请求实时读取）。"""
    return {"Authorization": f"Bearer {api_server.config.api_key}"}


class TestMountedEndpoints:
    def test_v1_endpoints_still_mounted(self, api_server):
        """v1 接口不能因为加了 v2/协议层而丢失"""
        rules = _rules(api_server.app)
        for expected in (
            "/api/status",
            "/api/messages",
            "/api/send",
            "/api/send_image",
            "/api/send_audio",
            "/api/history",
            "/api/load_earlier",
            "/api/sync_all",
            "/api/refresh",
            "/api/config/reload",
            "/api/login/qrcode",
            "/api/login/status",
        ):
            assert expected in rules, f"v1 端点丢失: {expected}"

    def test_v2_endpoints_mounted(self, api_server):
        rules = _rules(api_server.app)
        for expected in (
            "/api/v2/status",
            "/api/v2/capabilities",
            "/api/v2/messages",
            "/api/v2/history",
            "/api/v2/sync",
            "/api/v2/events",
            "/api/v2/connection",
        ):
            assert expected in rules, f"v2 端点未挂载: {expected}"

    def test_onebot_http_endpoint_mounted(self, api_server):
        rules = _rules(api_server.app)
        assert "/onebot/v11/<action>" in rules
        assert "/onebot/v11/get_status" in rules

    def test_onebot_websocket_endpoint_mounted(self, api_server):
        """OneBot 11 正向 WS —— 只注册了适配器但没 attach 是常见漏项"""
        assert "/onebot/v11/ws" in _ws_rules(api_server.app)

    def test_satori_http_endpoints_mounted(self, api_server):
        rules = _rules(api_server.app)
        # 无前缀形式
        assert "/satori/v1/<action>" in rules
        # 带 platform/account 前缀形式（Satori 规范）
        assert "/<platform>/<account>/satori/v1/<action>" in rules

    def test_satori_websocket_endpoint_mounted(self, api_server):
        """Satori 事件 WS"""
        assert "/v1/events" in _ws_rules(api_server.app)


class TestProtocolEndpointBehavior:
    """端点真的能响应（不只是路由存在），且**必须鉴权**。

    令牌复用 `config.api_key`，并且每次请求实时读取（跟随热重载）。
    """

    TOKEN = "proto-test-key"

    @pytest.fixture(autouse=True)
    def _key(self, api_server, monkeypatch):
        monkeypatch.setattr(api_server.config, "api_key", self.TOKEN)

    def _bearer(self, token=None):
        return {"Authorization": f"Bearer {token or self.TOKEN}"}

    def test_onebot_action_responds(self, api_server):
        client = api_server.app.test_client()
        resp = client.post(
            "/onebot/v11/get_version_info", json={}, headers=self._bearer()
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "ok"
        assert body["retcode"] == 0

    def test_onebot_accepts_access_token_query(self, api_server):
        """OneBot 11 约定也接受 `?access_token=`"""
        client = api_server.app.test_client()
        resp = client.post(
            f"/onebot/v11/get_version_info?access_token={self.TOKEN}", json={}
        )
        assert resp.status_code == 200

    def test_onebot_without_token_is_401(self, api_server):
        client = api_server.app.test_client()
        resp = client.post("/onebot/v11/get_version_info", json={})
        assert resp.status_code == 401
        assert resp.get_json()["status"] == "failed"

    def test_onebot_wrong_token_is_403(self, api_server):
        client = api_server.app.test_client()
        resp = client.post(
            "/onebot/v11/get_version_info", json={}, headers=self._bearer("wrong")
        )
        assert resp.status_code == 403

    def test_onebot_send_without_token_does_not_send(self, api_server):
        """未鉴权的发送请求必须在进入适配器前被拦下。"""
        client = api_server.app.test_client()
        resp = client.post(
            "/onebot/v11/send_private_msg",
            json={"user_id": 1, "message": "hi"},
        )
        assert resp.status_code == 401

    def test_onebot_get_status_requires_token(self, api_server):
        client = api_server.app.test_client()
        assert client.get("/onebot/v11/get_status").status_code == 401
        assert (
            client.get("/onebot/v11/get_status", headers=self._bearer()).status_code
            == 200
        )

    def test_onebot_non_object_body_is_400(self, api_server):
        client = api_server.app.test_client()
        resp = client.post(
            "/onebot/v11/get_version_info", json=[1], headers=self._bearer()
        )
        assert resp.status_code == 400
        assert resp.get_json()["retcode"] == 1400

    def test_token_follows_config_reload(self, api_server, monkeypatch):
        """换 key 后旧 key 立即失效（#3 同类问题在协议端点上的回归）"""
        client = api_server.app.test_client()
        monkeypatch.setattr(api_server.config, "api_key", "rotated")
        old = client.post(
            "/onebot/v11/get_version_info", json={}, headers=self._bearer()
        )
        new = client.post(
            "/onebot/v11/get_version_info", json={}, headers=self._bearer("rotated")
        )
        assert old.status_code == 403
        assert new.status_code == 200

    def test_satori_action_responds(self, api_server):
        client = api_server.app.test_client()
        resp = client.post("/satori/v1/session.create", json={}, headers=self._bearer())
        assert resp.status_code == 200
        body = resp.get_json()
        assert "error" not in body
        assert body["platform"]

    def test_satori_without_token_is_401(self, api_server):
        client = api_server.app.test_client()
        assert client.post("/satori/v1/session.create", json={}).status_code == 401

    def test_satori_rejects_query_token(self, api_server):
        """Satori 只认 Authorization 头，不接受 query 里的令牌。"""
        client = api_server.app.test_client()
        resp = client.post(
            f"/satori/v1/session.create?access_token={self.TOKEN}", json={}
        )
        assert resp.status_code == 401

    def test_satori_unknown_action_returns_error(self, api_server):
        client = api_server.app.test_client()
        resp = client.post("/satori/v1/nope.nope", json={}, headers=self._bearer())
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_v2_api_key_follows_config_reload(self, api_server, monkeypatch):
        """#3：v2 鉴权必须实时读取 `config.api_key`。"""
        client = api_server.app.test_client()
        assert (
            client.get(
                "/api/v2/capabilities", headers={"X-API-Key": self.TOKEN}
            ).status_code
            == 200
        )
        monkeypatch.setattr(api_server.config, "api_key", "rotated")
        assert (
            client.get(
                "/api/v2/capabilities", headers={"X-API-Key": self.TOKEN}
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v2/capabilities", headers={"X-API-Key": "rotated"}
            ).status_code
            == 200
        )


class TestOpenApiMatchesImplementation:
    """OpenAPI 描述必须与实际挂载的端点一致 —— 否则文档就是骗人的。"""

    @staticmethod
    def _documented_paths() -> set[str]:
        text = OPENAPI_PATH.read_text(encoding="utf-8")
        # 只取 paths 段下的二级路径（`  /xxx:` 形式），避免误抓 components
        return set(re.findall(r"^  (/api/v2/[a-z_]+):", text, re.MULTILINE))

    def test_spec_file_exists(self):
        assert OPENAPI_PATH.exists(), "OpenAPI 描述文件缺失"

    def test_documented_paths_match_mounted_routes(self, api_server):
        documented = self._documented_paths()
        assert documented, "未能从 OpenAPI 中解析出任何路径"
        mounted = {r for r in _rules(api_server.app) if r.startswith("/api/v2/")}
        assert documented == mounted, (
            f"OpenAPI 描述与实际端点不一致: "
            f"仅文档有={sorted(documented - mounted)} "
            f"仅实现有={sorted(mounted - documented)}"
        )

    def test_spec_declares_all_error_codes(self):
        """统一错误码必须在 OpenAPI 里全部出现，否则第三方无法据此处理"""
        text = OPENAPI_PATH.read_text(encoding="utf-8")
        from seewo.interfaces.rest import ApiErrorCode

        for code in ApiErrorCode:
            if code is ApiErrorCode.OK:
                continue
            assert code.value in text, f"OpenAPI 未声明错误码: {code.value}"
