# -*- coding: utf-8 -*-
"""第 2 层：文件上传（upload.py）—— 上传策略、multipart 组装、台账写入。"""

import json

import pytest

import upload as upload_module
from conftest import FakeAccount, mcampus_url

POLICY_ACTION = "POST_MOBILE_V1_RESOURCE_CSTORE_UPLOADPOLICY"
UPLOAD_URL = "https://cos.example.com/upload"


def _policy_payload(form_fields=None, expire_seconds=3600):
    """对齐生产：expireSeconds 在 data 层，uploadUrl/formFields 在 policyList[0]"""
    return {
        "statusCode": 200,
        "data": {
            "expireSeconds": expire_seconds,
            "policyList": [
                {
                    "uploadUrl": UPLOAD_URL,
                    "formFields": form_fields
                    or [{"value": f"v{i}"} for i in range(11)],
                }
            ],
        },
    }


@pytest.fixture
def cos(http, monkeypatch):
    """注册上传策略 + COS 上传两个端点，返回 responses 打桩器"""
    http.add(
        http.POST,
        mcampus_url(POLICY_ACTION),
        json=_policy_payload(),
        content_type="application/json",
    )
    monkeypatch.setattr(upload_module.time, "time", lambda: 1700000000.0)
    return http


def _make_file(tmp_path, name="a.png"):
    path = tmp_path / name
    path.write_bytes(b"binary-content")
    return path


class TestGetResource:
    def test_parses_policy(self, cos):
        up = upload_module.Upload(FakeAccount())
        assert up.uploadUrl == UPLOAD_URL
        assert up.expiretime == 1700000000 + 3600
        assert up.headers["Host"] == UPLOAD_URL[8:]
        assert up.isupload is False

    def test_sends_app_id(self, cos):
        from conftest import decode_action_request

        upload_module.Upload(FakeAccount())
        _, params = decode_action_request(cos.calls[0].request.body)
        assert params["appId"] == "10388"

    def test_error_response_leaves_upload_url_absent(self, http):
        http.add(
            http.POST,
            mcampus_url(POLICY_ACTION),
            json={"statusCode": -500, "message": "token 无效"},
        )
        up = upload_module.Upload(FakeAccount())
        assert not hasattr(up, "uploadUrl")

    def test_non_json_response_is_tolerated(self, http, tmp_path):
        """网关返回非 JSON（如 502 HTML）时降级：不崩、没有 uploadUrl、记录 error"""
        http.add(
            http.POST,
            mcampus_url(POLICY_ACTION),
            body="<html>502 Bad Gateway</html>",
            status=502,
            content_type="text/html",
        )
        up = upload_module.Upload(FakeAccount())
        assert not hasattr(up, "uploadUrl")
        assert up.error
        assert up.upload(str(_make_file(tmp_path))) is None

    def test_upload_without_policy_returns_none(self, http, tmp_path):
        """没有上传策略时 upload() 直接降级，不再 AttributeError"""
        http.add(
            http.POST,
            mcampus_url(POLICY_ACTION),
            json={"statusCode": -500, "message": "token 无效"},
        )
        path = tmp_path / "a.png"
        path.write_bytes(b"x")
        up = upload_module.Upload(FakeAccount())
        assert up.upload(str(path)) is None
        assert up.downloadUrl == ""


def test_mock_server_policy_matches_client_expectations():
    """mock 的策略结构须与生产一致：
    expireSeconds 在 data 层；policyList[0] 含 uploadUrl 与 >=11 个 formFields。
    """
    from mock_server import ACTION_HANDLERS

    payload = ACTION_HANDLERS[POLICY_ACTION]({})
    assert "expireSeconds" in payload["data"]
    assert len(payload["data"]["policyList"][0]["formFields"]) >= 11


class TestUpload:
    def test_success_sets_download_url_and_ledger(self, cos, tmp_path, uploads_ledger):
        path = _make_file(tmp_path, "照片.png")
        cos.add(
            cos.POST,
            UPLOAD_URL,
            json={
                "code": 0,
                "data": {"downloadUrl": "http://cdn/x.png", "fileId": "f1"},
            },
        )
        up = upload_module.Upload(FakeAccount())
        up.upload(str(path), "image/png")
        assert up.isupload is True
        assert up.downloadUrl == "http://cdn/x.png"
        ledger = json.loads(uploads_ledger.read_text(encoding="utf-8"))
        assert list(ledger) == ["照片.png"]
        assert ledger["照片.png"]["fileId"] == "f1"

    def test_ledger_preserves_colliding_basenames(self, cos, tmp_path, uploads_ledger):
        sub = tmp_path / "sub"
        sub.mkdir()
        cos.add(
            cos.POST,
            UPLOAD_URL,
            json={
                "code": 0,
                "data": {"downloadUrl": "http://cdn/1.png", "fileId": "f1"},
            },
        )
        cos.add(
            cos.POST,
            UPLOAD_URL,
            json={
                "code": 0,
                "data": {"downloadUrl": "http://cdn/2.png", "fileId": "f2"},
            },
        )

        upload_module.Upload(FakeAccount()).upload(
            str(_make_file(tmp_path, "same.png")), "image/png"
        )
        upload_module.Upload(FakeAccount()).upload(
            str(_make_file(sub, "same.png")), "image/png"
        )

        ledger = json.loads(uploads_ledger.read_text(encoding="utf-8"))
        assert list(ledger) == ["same.png", "same.png#2"]
        assert ledger["same.png"]["downloadUrl"] == "http://cdn/1.png"
        assert ledger["same.png#2"]["downloadUrl"] == "http://cdn/2.png"
        assert ledger["same.png#2"]["filename"] == "same.png"

    def test_source_file_not_locked_after_failed_upload(self, cos, tmp_path):
        """上传失败后源文件句柄必须已关闭"""
        import os

        cos.add(cos.POST, UPLOAD_URL, json={"code": 1, "message": "denied"})
        path = _make_file(tmp_path, "locked.png")
        up = upload_module.Upload(FakeAccount())
        up.upload(str(path), "image/png")
        assert up.isupload is False
        os.remove(path)  # 句柄未关闭时这里会抛 PermissionError

    def test_second_upload_on_same_instance_is_skipped(self, cos, tmp_path):
        cos.add(
            cos.POST,
            UPLOAD_URL,
            json={"code": 0, "data": {"downloadUrl": "http://cdn/x.png"}},
        )
        up = upload_module.Upload(FakeAccount())
        up.upload(str(_make_file(tmp_path)), "image/png")
        before = len(cos.calls)
        assert up.upload(str(_make_file(tmp_path, "b.png")), "image/png") is None
        assert len(cos.calls) == before

    def test_failure_response_keeps_isupload_false(self, cos, tmp_path):
        cos.add(cos.POST, UPLOAD_URL, json={"code": -1, "message": "denied"})
        up = upload_module.Upload(FakeAccount())
        up.upload(str(_make_file(tmp_path)), "image/png")
        assert up.isupload is False
        assert up.downloadUrl == ""  # 失败时保持空串，调用方按 falsy 判失败

    @pytest.mark.parametrize(
        ("filename", "expected_type"),
        [
            ("a.png", b"image/png"),
            ("a.jpg", b"image/jpeg"),
            ("a.mp3", b"audio/mpeg"),
            ("a.m4a", b"audio/mp4"),
            ("a.wav", b"audio/x-wav"),
            ("a.unknown", b"application/octet-stream"),
        ],
    )
    def test_derives_content_type_from_extension(
        self, cos, tmp_path, filename, expected_type
    ):
        """type 未显式给出时按扩展名推导（stdlib mimetypes），推不出回落 octet-stream"""
        cos.add(
            cos.POST,
            UPLOAD_URL,
            json={"code": 0, "data": {"downloadUrl": "http://cdn/x"}},
        )
        upload_module.Upload(FakeAccount()).upload(str(_make_file(tmp_path, filename)))
        assert expected_type in cos.calls[-1].request.body

    def test_caller_provided_type_is_used_as_is(self, cos, tmp_path):
        """显式传 type 时以调用方为准（api_server 传 audio/mp3 不被改成 audio/mpeg）"""
        cos.add(
            cos.POST,
            UPLOAD_URL,
            json={"code": 0, "data": {"downloadUrl": "http://cdn/x"}},
        )
        upload_module.Upload(FakeAccount()).upload(
            str(_make_file(tmp_path, "a.mp3")), "audio/mp3"
        )
        assert b"audio/mp3" in cos.calls[-1].request.body

    def test_multipart_contains_all_policy_fields(self, cos, tmp_path):
        cos.add(
            cos.POST,
            UPLOAD_URL,
            json={"code": 0, "data": {"downloadUrl": "http://cdn/x"}},
        )
        upload_module.Upload(FakeAccount()).upload(
            str(_make_file(tmp_path)), "image/png"
        )
        body = cos.calls[-1].request.body
        for name in (b"policy", b"q-signature", b"callback", b"x:bucketid", b"file"):
            assert name in body
        assert (
            cos.calls[-1]
            .request.headers["Content-Type"]
            .startswith("multipart/form-data")
        )
