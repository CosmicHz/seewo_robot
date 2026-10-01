# -*- coding: utf-8 -*-
"""第 2 层：媒体端点（/api/send_image、/api/send_audio）。

上传动作打桩（upload_file_to_cloud），只验证端点自身的分支与参数传递。
"""

from pathlib import Path

import pytest

from models import SendResult


@pytest.fixture
def stubbed_upload(monkeypatch):
    """打桩云存储上传，记录被上传的本地路径，返回预设 downloadUrl"""
    import api_server

    calls = []

    def fake_upload(path, content_type="image/png"):
        calls.append(
            {
                "path": path,
                "type": content_type,
                "exists_during_upload": Path(path).exists(),
            }
        )
        return f"http://cdn/{path.split(chr(92))[-1].split('/')[-1]}"

    monkeypatch.setattr(api_server, "upload_file_to_cloud", fake_upload)
    return calls


class TestSendImage:
    def test_json_path_required(self, app_env, auth_headers):
        client, fake = app_env
        resp = client.post("/api/send_image", headers=auth_headers, json={})
        assert resp.status_code == 400
        assert fake.stu_msg.sent == []

    def test_missing_file_rejected(self, app_env, auth_headers, tmp_path):
        client, _ = app_env
        resp = client.post(
            "/api/send_image",
            headers=auth_headers,
            json={"file_path": str(tmp_path / "absent.png")},
        )
        assert resp.status_code == 400

    def test_sends_uploaded_url_as_image(
        self, app_env, auth_headers, tmp_path, stubbed_upload
    ):
        client, fake = app_env
        picture = tmp_path / "photo.png"
        picture.write_bytes(b"png")
        payload = client.post(
            "/api/send_image", headers=auth_headers, json={"file_path": str(picture)}
        ).get_json()
        assert payload["status"] == "ok"
        assert payload["url"] == "http://cdn/photo.png"
        assert stubbed_upload[0]["path"] == str(picture)
        assert stubbed_upload[0]["type"] == "image/png"
        assert fake.stu_msg.sent[0]["type"] == 2
        assert fake.stu_msg.sent[0]["resUrl"] == "http://cdn/photo.png"

    def test_upload_failure_returns_500(
        self, app_env, auth_headers, tmp_path, monkeypatch
    ):
        import api_server

        client, fake = app_env
        picture = tmp_path / "photo.png"
        picture.write_bytes(b"png")
        monkeypatch.setattr(
            api_server,
            "upload_file_to_cloud",
            lambda path, content_type="image/png": "",
        )
        resp = client.post(
            "/api/send_image", headers=auth_headers, json={"file_path": str(picture)}
        )
        assert resp.status_code == 500
        assert fake.stu_msg.sent == []

    def test_multipart_upload_uses_and_cleans_temp_file(
        self, app_env, auth_headers, tmp_path, monkeypatch, stubbed_upload
    ):
        """multipart 文件在上传期间存在，请求结束后清理且不污染 cwd"""
        import io

        monkeypatch.chdir(tmp_path)
        client, fake = app_env
        resp = client.post(
            "/api/send_image",
            headers=auth_headers,
            data={"file": (io.BytesIO(b"png-bytes"), "pic.png")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        assert stubbed_upload[0]["exists_during_upload"] is True
        assert not Path(stubbed_upload[0]["path"]).exists()
        assert list(tmp_path.glob("temp_*")) == []
        assert fake.stu_msg.sent[0]["type"] == 2

    def test_multipart_upload_cleans_temp_file_when_upload_raises(
        self, app_env, auth_headers, tmp_path, monkeypatch
    ):
        import io
        import api_server

        seen = []

        def failing_upload(path, content_type="image/png"):
            seen.append(path)
            assert Path(path).exists()
            raise RuntimeError("upload unavailable")

        monkeypatch.setattr(api_server, "upload_file_to_cloud", failing_upload)
        monkeypatch.chdir(tmp_path)
        client, _ = app_env
        resp = client.post(
            "/api/send_image",
            headers=auth_headers,
            data={"file": (io.BytesIO(b"png-bytes"), "pic.png")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 500
        assert seen and not Path(seen[0]).exists()
        assert list(tmp_path.glob("temp_*")) == []

    def test_multipart_without_file_rejected(self, app_env, auth_headers):
        client, _ = app_env
        resp = client.post(
            "/api/send_image",
            headers=auth_headers,
            data={},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400


class TestSendAudio:
    def test_missing_body_rejected(self, app_env, auth_headers):
        client, _ = app_env
        resp = client.post(
            "/api/send_audio",
            headers=auth_headers,
            data="",
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_missing_file_rejected(self, app_env, auth_headers, tmp_path):
        client, _ = app_env
        resp = client.post(
            "/api/send_audio",
            headers=auth_headers,
            json={"file_path": str(tmp_path / "absent.mp3")},
        )
        assert resp.status_code == 400

    def test_sends_one_audio_message_after_upload(
        self, app_env, auth_headers, tmp_path, stubbed_upload
    ):
        client, fake = app_env
        audio = tmp_path / "song.mp3"
        audio.write_bytes(b"mp3")
        payload = client.post(
            "/api/send_audio", headers=auth_headers, json={"file_path": str(audio)}
        ).get_json()
        assert payload["status"] == "ok"
        assert stubbed_upload[0]["path"] == str(audio)
        assert stubbed_upload[0]["type"] == "audio/mp3"
        assert len(fake.stu_msg.sent) == 1
        assert fake.stu_msg.sent[0]["type"] == 3
        assert fake.stu_msg.sent[0]["voiceLength"] == 666
        assert fake.stu_msg.sent[0]["resUrl"] == "http://cdn/song.mp3"

    def test_voice_length_override(
        self, app_env, auth_headers, tmp_path, stubbed_upload
    ):
        client, fake = app_env
        audio = tmp_path / "song.mp3"
        audio.write_bytes(b"mp3")
        client.post(
            "/api/send_audio",
            headers=auth_headers,
            json={"file_path": str(audio), "voice_length": 1234},
        )
        assert fake.stu_msg.sent[0]["voiceLength"] == 1234

    def test_upload_failure_returns_500(
        self, app_env, auth_headers, tmp_path, monkeypatch
    ):
        import api_server

        client, fake = app_env
        audio = tmp_path / "song.mp3"
        audio.write_bytes(b"mp3")
        monkeypatch.setattr(
            api_server,
            "upload_file_to_cloud",
            lambda path, content_type="image/png": "",
        )
        resp = client.post(
            "/api/send_audio", headers=auth_headers, json={"file_path": str(audio)}
        )
        assert resp.status_code == 500
        assert fake.stu_msg.sent == []


class TestMediaErrorHandling:
    """媒体端点不再吞掉发送失败（旧实现无论成败都返回 status: ok）"""

    def test_image_send_failure_is_reported(
        self, app_env, auth_headers, tmp_path, stubbed_upload
    ):
        client, fake = app_env
        picture = tmp_path / "p.png"
        picture.write_bytes(b"png")
        fake.stu_msg.send_result = SendResult(
            ok=False, seewo_code=40000, message="留言内容不能超过200字符"
        )
        resp = client.post(
            "/api/send_image", headers=auth_headers, json={"file_path": str(picture)}
        )
        assert resp.status_code == 500
        payload = resp.get_json()
        assert payload["status"] == "error"
        assert payload["seewoCode"] == 40000

    def test_audio_send_failure_is_reported(
        self, app_env, auth_headers, tmp_path, stubbed_upload
    ):
        client, fake = app_env
        audio = tmp_path / "s.mp3"
        audio.write_bytes(b"mp3")
        fake.stu_msg.send_impl = lambda n, content, type: SendResult(
            ok=False, seewo_code=-500, message="token无效"
        )
        # 刷新会话后重发仍是 -500（重登没换到有效 Token）→ 回 401 让客户端引导扫码
        resp = client.post(
            "/api/send_audio", headers=auth_headers, json={"file_path": str(audio)}
        )
        assert resp.status_code == 401
        assert resp.get_json()["need_login"] is True

    def test_image_token_invalid_triggers_relogin(
        self, app_env, auth_headers, tmp_path, stubbed_upload
    ):
        client, fake = app_env
        picture = tmp_path / "p.png"
        picture.write_bytes(b"png")
        refreshed = []
        fake.refresh = lambda: (refreshed.append(True), fake)[1]
        fake.stu_msg.send_impl = lambda n, content, type: (
            SendResult(ok=False, seewo_code=-505, message="token已过期")
            if n == 1
            else SendResult(ok=True, seewo_code=200, message="发送成功")
        )
        resp = client.post(
            "/api/send_image", headers=auth_headers, json={"file_path": str(picture)}
        )
        assert resp.status_code == 200
        assert refreshed == [True]
        assert len(fake.stu_msg.sent) == 2
