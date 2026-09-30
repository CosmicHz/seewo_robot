# -*- coding: utf-8 -*-
"""第 2 层：登录端点（/api/login/qrcode、/api/login/status）与后台轮询状态机。"""

import base64
import json
import types

import pytest

import api_server

PNG = b"\x89PNG\r\n\x1a\nfake"


@pytest.fixture(autouse=True)
def _reset_login_state():
    api_server._login_state.update(
        {"in_progress": False, "completed": False, "success": False}
    )
    yield
    api_server._login_state.update(
        {"in_progress": False, "completed": False, "success": False}
    )


@pytest.fixture
def no_sleep(monkeypatch):
    """后台轮询每轮 sleep 2 秒，测试里立即返回"""
    monkeypatch.setattr(api_server, "time", types.SimpleNamespace(sleep=lambda s: None))


class TestQrcodeEndpoint:
    def test_returns_base64_image_and_starts_polling(
        self, app_env, auth_headers, login_files, monkeypatch, no_sleep
    ):
        client, fake = app_env
        login_files["qrcode"].write_bytes(PNG)
        monkeypatch.setattr(api_server, "download_qrcode", lambda: {"qr": "1"})
        monkeypatch.setattr(
            api_server,
            "check_qrcode",
            lambda cookies: {
                "data": {"statusCode": 202, "userId": "u1", "token": "t1"}
            },
        )
        refreshed = []
        fake.refresh = lambda: refreshed.append(True)

        payload = client.get("/api/login/qrcode", headers=auth_headers).get_json()

        assert payload["status"] == "ok"
        assert base64.b64decode(payload["qrcode"]) == PNG
        for _ in range(50):  # 等后台线程收尾
            if api_server._login_state["completed"]:
                break
            import time

            time.sleep(0.05)
        assert api_server._login_state == {
            "in_progress": False,
            "completed": True,
            "success": True,
        }
        assert (
            json.loads(login_files["token"].read_text(encoding="utf-8"))["token"]
            == "t1"
        )
        assert refreshed == [True]

    def test_rejects_concurrent_login_flow(self, app_env, auth_headers, monkeypatch):
        client, _ = app_env
        monkeypatch.setattr(
            api_server, "download_qrcode", lambda: pytest.fail("不应重复启动轮询")
        )
        api_server._login_state["in_progress"] = True
        payload = client.get("/api/login/qrcode", headers=auth_headers).get_json()
        assert "进行中" in payload["message"]

    def test_download_failure_resets_state(self, app_env, auth_headers, monkeypatch):
        client, _ = app_env

        def boom():
            raise RuntimeError("网络不可达")

        monkeypatch.setattr(api_server, "download_qrcode", boom)
        resp = client.get("/api/login/qrcode", headers=auth_headers)
        assert resp.status_code == 500
        assert api_server._login_state["in_progress"] is False

    def test_missing_qrcode_file_returns_500(
        self, app_env, auth_headers, login_files, monkeypatch
    ):
        client, _ = app_env
        monkeypatch.setattr(api_server, "download_qrcode", lambda: {})
        resp = client.get("/api/login/qrcode", headers=auth_headers)
        assert resp.status_code == 500
        assert api_server._login_state["in_progress"] is False


class TestStatusEndpoint:
    def test_idle(self, app_env, auth_headers):
        client, _ = app_env
        assert (
            client.get("/api/login/status", headers=auth_headers).get_json()["status"]
            == "idle"
        )

    def test_pending(self, app_env, auth_headers):
        client, _ = app_env
        api_server._login_state["in_progress"] = True
        assert (
            client.get("/api/login/status", headers=auth_headers).get_json()["status"]
            == "pending"
        )

    def test_completed_success(self, app_env, auth_headers):
        client, _ = app_env
        api_server._login_state.update({"completed": True, "success": True})
        payload = client.get("/api/login/status", headers=auth_headers).get_json()
        assert payload["status"] == "ok"
        assert payload["message"] == "登录成功"

    def test_completed_failure(self, app_env, auth_headers):
        client, _ = app_env
        api_server._login_state.update({"completed": True, "success": False})
        assert (
            client.get("/api/login/status", headers=auth_headers).get_json()["status"]
            == "error"
        )


class TestPollLoginThread:
    def test_success_writes_token_and_refreshes(
        self, app_env, login_files, monkeypatch, no_sleep
    ):
        _, fake = app_env
        refreshed = []
        fake.refresh = lambda: refreshed.append(True)
        monkeypatch.setattr(
            api_server,
            "check_qrcode",
            lambda cookies: {
                "data": {"statusCode": 202, "userId": "u1", "token": "t1"}
            },
        )

        api_server._login_state.update(
            {"in_progress": True, "completed": False, "success": False}
        )
        api_server._poll_login({"qr": "1"})

        assert login_files["token"].exists()
        assert api_server._login_state == {
            "in_progress": False,
            "completed": True,
            "success": True,
        }
        assert refreshed == [True]

    def test_failure_marks_completed_without_success(
        self, app_env, login_files, monkeypatch, no_sleep
    ):
        _, fake = app_env
        refreshed = []
        fake.refresh = lambda: refreshed.append(True)
        monkeypatch.setattr(
            api_server,
            "check_qrcode",
            lambda cookies: {"data": {"statusCode": 400, "message": "二维码过期"}},
        )

        api_server._login_state.update(
            {"in_progress": True, "completed": False, "success": False}
        )
        api_server._poll_login({"qr": "1"})

        assert not login_files["token"].exists()
        assert api_server._login_state == {
            "in_progress": False,
            "completed": True,
            "success": False,
        }
        assert refreshed == []

    def test_polls_until_confirmed(self, app_env, monkeypatch, no_sleep):
        _, fake = app_env
        codes = iter([200, 201, 202])

        def check(cookies):
            code = next(codes)
            return {"data": {"statusCode": code, "userId": "u1", "token": "t1"}}

        monkeypatch.setattr(api_server, "check_qrcode", check)
        api_server._login_state.update(
            {"in_progress": True, "completed": False, "success": False}
        )
        api_server._poll_login({"qr": "1"})

        assert api_server._login_state["success"] is True

    def test_exception_ends_polling(self, app_env, monkeypatch, no_sleep):
        _, _ = app_env
        monkeypatch.setattr(
            api_server,
            "check_qrcode",
            lambda cookies: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        api_server._login_state.update(
            {"in_progress": True, "completed": False, "success": False}
        )
        api_server._poll_login({"qr": "1"})
        assert api_server._login_state["completed"] is True
        assert api_server._login_state["success"] is False
