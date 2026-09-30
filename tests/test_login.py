# -*- coding: utf-8 -*-
"""第 2 层：登录与账户（login.py）—— 扫码状态机、Token 校验、请求头装配。"""

import json
import re

import pytest

import login as login_module

STATUS_URL = "https://campus.seewo.com/soul-bootstrap/seewo-phoenix-blood-server/mobile/user/v1/{uid}/functionality"
QRCODE_PATTERN = re.compile(r"^https://id\.seewo\.com/scan/qrcode\?.*$")
CHECK_PATTERN = re.compile(r"^https://id\.seewo\.com/scan/pcCheckQrcode\?.*$")
LOGIN_API_PATTERN = re.compile(r"^https://id\.seewo\.com/auth/loginApi\?.*$")

PNG = b"\x89PNG\r\n\x1a\nfake"


def _token_body(user_id="u1", token="t1"):
    return {"userId": user_id, "token": token}


def _write_tokens(path, user_id="u1", token="t1"):
    path.write_text(json.dumps(_token_body(user_id, token)), encoding="utf-8")


def _status_json(code):
    return {
        "statusCode": code,
        "message": {200: "ok", -500: "token无效", -505: "token已过期"}.get(
            code, "其他"
        ),
    }


class TestQrcode:
    def test_get_cookies_reads_response_cookies(self, http):
        http.add(
            http.GET,
            LOGIN_API_PATTERN,
            json={},
            headers={"Set-Cookie": "JSESSIONID=abc"},
        )
        assert login_module.get_cookies() == {"JSESSIONID": "abc"}

    def test_download_qrcode_saves_image_and_returns_cookies(self, http, login_files):
        http.add(
            http.GET,
            LOGIN_API_PATTERN,
            json={},
            headers={"Set-Cookie": "JSESSIONID=abc"},
        )
        http.add(
            http.GET,
            QRCODE_PATTERN,
            body=PNG,
            headers={"Set-Cookie": "qr=1"},
            content_type="image/png",
        )
        cookies = login_module.download_qrcode()
        assert login_files["qrcode"].read_bytes() == PNG
        assert cookies == {"qr": "1"}

    def test_check_qrcode_parses_status(self, http):
        http.add(
            http.GET,
            CHECK_PATTERN,
            json={"data": {"statusCode": 201, "message": "已扫码"}},
        )
        assert login_module.check_qrcode({"qr": "1"})["data"]["statusCode"] == 201


class TestStatusParsing:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [(200, True), (-500, False), (-505, False), (40000, False)],
    )
    def test_status_codes(self, code, expected):
        assert (
            login_module.acc.status(object(), json.dumps({"statusCode": code}))
            is expected
        )


class TestAcc:
    def test_loads_cached_token(self, http, login_files):
        _write_tokens(login_files["token"], "u1", "t1")
        http.add(http.GET, STATUS_URL.format(uid="u1"), json=_status_json(200))
        account = login_module.acc()
        assert account.uid == "u1"
        assert account.token_expired is False
        assert "x-auth-token=t1" in account.headers["cookie"]
        assert "x-auth-token=t1" in account.mheaders["cookie"]
        assert account.headers["host"] == "campus.seewo.com"

    def test_verifies_token_against_status_endpoint(self, http, login_files):
        _write_tokens(login_files["token"])
        http.add(http.GET, STATUS_URL.format(uid="u1"), json=_status_json(200))
        login_module.acc()
        assert http.calls[0].request.url == STATUS_URL.format(uid="u1")
        assert "x-auth-token=t1" in http.calls[0].request.headers["cookie"]

    def test_missing_token_file_without_auto_login(self, login_files):
        account = login_module.acc(auto_login=False)
        assert account.token_expired is True
        assert not hasattr(account, "uid")

    def test_missing_token_file_with_auto_login(self, http, login_files, monkeypatch):
        """auto_login=True 时缺文件会走扫码登录"""
        _write_tokens(login_files["token"], "u9", "t9")
        monkeypatch.setattr(login_module, "login", lambda: True)
        http.add(http.GET, STATUS_URL.format(uid="u9"), json=_status_json(200))
        account = login_module.acc(auto_login=True)
        assert account.uid == "u9"
        assert account.token_expired is False

    def test_expired_token_without_auto_login_does_not_relogin(
        self, http, login_files, monkeypatch
    ):
        _write_tokens(login_files["token"])
        monkeypatch.setattr(
            login_module, "login", lambda: pytest.fail("auto_login=False 不应触发扫码")
        )
        http.add(http.GET, STATUS_URL.format(uid="u1"), json=_status_json(-505))
        account = login_module.acc(auto_login=False)
        assert account.token_expired is True

    def test_expired_token_with_auto_login_relogins_and_recovers(
        self, http, login_files, monkeypatch
    ):
        _write_tokens(login_files["token"], "u1", "old")
        new_body = _token_body("u1", "new")
        monkeypatch.setattr(
            login_module,
            "login",
            lambda: _write_tokens(login_files["token"], "u1", "new"),
        )
        http.add(http.GET, STATUS_URL.format(uid="u1"), json=_status_json(-500))
        http.add(http.GET, STATUS_URL.format(uid="u1"), json=_status_json(200))
        account = login_module.acc()
        assert account.token_expired is False
        assert "x-auth-token=new" in account.headers["cookie"]
        assert json.loads(login_files["token"].read_text(encoding="utf-8")) == new_body

    def test_gives_up_after_max_retries(self, http, login_files, monkeypatch):
        _write_tokens(login_files["token"])
        monkeypatch.setattr(
            login_module, "login", lambda: _write_tokens(login_files["token"])
        )
        for _ in range(3):  # max_retries 次校验，每次登录后仍返回 Token 过期
            http.add(http.GET, STATUS_URL.format(uid="u1"), json=_status_json(-500))
        account = login_module.acc(max_retries=3)
        assert account.token_expired is True
        assert len(http.calls) == 3

    def test_type_1_logs_in_directly(self, http, login_files, monkeypatch):
        monkeypatch.setattr(
            login_module,
            "login",
            lambda: _write_tokens(login_files["token"], "u7", "t7"),
        )
        http.add(http.GET, STATUS_URL.format(uid="u7"), json=_status_json(200))
        account = login_module.acc(type=1)
        assert account.uid == "u7"

    def test_type_1_without_auto_login_skips_network(self, login_files):
        account = login_module.acc(type=1, auto_login=False)
        assert account.token_expired is True


class TestLoginFlow:
    @pytest.fixture(autouse=True)
    def _no_qrcode_render(self, monkeypatch):
        monkeypatch.setattr(login_module, "print_qrcode", lambda path: None)

    def test_success_writes_token_file(self, http, login_files):
        http.add(http.GET, LOGIN_API_PATTERN, json={}, headers={"Set-Cookie": "s=1"})
        http.add(http.GET, QRCODE_PATTERN, body=PNG, content_type="image/png")
        for code in (200, 201, 202):
            http.add(
                http.GET,
                CHECK_PATTERN,
                json={
                    "data": {
                        "statusCode": code,
                        "message": "m",
                        "userId": "u1",
                        "token": "t1",
                    }
                },
            )
        assert login_module.login() is True
        assert (
            json.loads(login_files["token"].read_text(encoding="utf-8"))["token"]
            == "t1"
        )

    @pytest.mark.parametrize("final_code", [400, 500])
    def test_failure_does_not_write_token_file(self, http, login_files, final_code):
        http.add(http.GET, LOGIN_API_PATTERN, json={}, headers={"Set-Cookie": "s=1"})
        http.add(http.GET, QRCODE_PATTERN, body=PNG, content_type="image/png")
        http.add(
            http.GET,
            CHECK_PATTERN,
            json={"data": {"statusCode": final_code, "message": "过期"}},
        )
        assert login_module.login() is False
        assert not login_files["token"].exists()
