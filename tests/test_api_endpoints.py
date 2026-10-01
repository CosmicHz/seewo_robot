# -*- coding: utf-8 -*-
"""第 2 层：api_server 核心端点（app.test_client + 假会话 + 临时聊天记录）。

隔离方式见 tests/conftest.py：
- session / datasource 换成假对象（打桩点 A）
- 聊天记录改指 tmp_path（打桩点 B）
"""

import json

import pytest

from api_server import MSG_MAX_LEN
from conftest import raw_msg, write_chat_history, msg_obj
from models import SendResult


def _expired(fake):
    fake.account.token_expired = True
    return fake


# ============================ 鉴权与会话 ============================


class TestApiKey:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/status"),
            ("get", "/api/messages"),
            ("get", "/api/history"),
            ("get", "/api/load_earlier"),
            ("post", "/api/send"),
            ("post", "/api/refresh"),
            ("post", "/api/config/reload"),
            ("get", "/api/login/qrcode"),
            ("get", "/api/login/status"),
        ],
    )
    def test_missing_key_is_rejected(self, app_env, method, path):
        client, _ = app_env
        resp = getattr(client, method)(path)
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "Unauthorized"

    def test_wrong_key_is_rejected(self, app_env):
        client, _ = app_env
        assert (
            client.get("/api/status", headers={"X-API-Key": "nope"}).status_code == 401
        )

    def test_key_via_query_param_is_accepted(self, app_env):
        from conftest import TEST_API_KEY

        client, _ = app_env
        assert client.get(f"/api/status?api_key={TEST_API_KEY}").status_code == 200


class TestSessionCheck:
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("get", "/api/status", None),
            ("get", "/api/messages", None),
            ("post", "/api/send", {"content": "x"}),
            ("post", "/api/sync_all", {}),
        ],
    )
    def test_expired_token_asks_for_login(
        self, app_env, auth_headers, method, path, body
    ):
        client, fake = app_env
        _expired(fake)
        resp = getattr(client, method)(path, headers=auth_headers, json=body)
        assert resp.status_code == 401
        payload = resp.get_json()
        assert payload["need_login"] is True
        assert payload["status"] == "error"

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/history"),
            ("get", "/api/load_earlier"),
            ("post", "/api/config/reload"),
        ],
    )
    def test_local_only_endpoints_work_without_login(
        self, app_env, auth_headers, method, path
    ):
        """本地历史与配置接口不依赖希沃会话，Token 过期也应可用"""
        client, fake = app_env
        _expired(fake)
        assert getattr(client, method)(path, headers=auth_headers).status_code == 200


class TestStatus:
    def test_returns_student_info(self, app_env, auth_headers):
        client, _ = app_env
        payload = client.get("/api/status", headers=auth_headers).get_json()
        assert payload["status"] == "ok"
        assert payload["student"]["name"] == "测试学生"
        assert payload["student"]["userUid"] == "mock_student_001"

    def test_tolerates_missing_student(self, app_env, auth_headers):
        client, fake = app_env
        fake.student = None
        payload = client.get("/api/status", headers=auth_headers).get_json()
        assert payload["student"]["name"] == "unknown"


# ============================ /api/messages ============================


class TestMessages:
    def test_fetches_latest_page(self, app_env, auth_headers):
        client, fake = app_env
        fake.stu_msg.pages = {1: [raw_msg(2), raw_msg(1)]}
        payload = client.get("/api/messages?count=10", headers=auth_headers).get_json()
        assert fake.stu_msg.get_calls == [(10, 1)]
        assert payload["count"] == 2
        assert [m["id"] for m in payload["messages"]] == [1, 2]

    def test_default_count_is_10(self, app_env, auth_headers):
        client, fake = app_env
        client.get("/api/messages", headers=auth_headers)
        assert fake.stu_msg.get_calls == [(10, 1)]

    def test_formats_sender_and_time(self, app_env, auth_headers):
        from datetime import datetime

        client, fake = app_env
        create_time = 1700000000000
        fake.stu_msg.pages = {
            1: [
                raw_msg(
                    1,
                    senderUid="mock_parent_001",
                    senderType="parent",
                    senderName="某人",
                    createTime=create_time,
                ),
                raw_msg(
                    2,
                    senderUid="mock_student_001",
                    senderType="student",
                    senderName="测试学生",
                    createTime=create_time,
                ),
                raw_msg(
                    3,
                    senderUid="other",
                    senderType="teacher",
                    senderName="王老师",
                    createTime=create_time,
                ),
            ]
        }
        msgs = client.get("/api/messages", headers=auth_headers).get_json()["messages"]
        assert [m["sender"] for m in msgs] == ["parent", "student", "unknown"]
        assert [m["senderName"] for m in msgs] == ["家长", "测试学生", "王老师"]
        # 毫秒时间戳 → 本地时间字符串
        expected = datetime.fromtimestamp(create_time / 1000).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        assert msgs[0]["time"] == expected

    def test_empty_create_time_leaves_time_blank(self, app_env, auth_headers):
        client, fake = app_env
        fake.stu_msg.pages = {1: [raw_msg(1, createTime=0)]}
        msgs = client.get("/api/messages", headers=auth_headers).get_json()["messages"]
        assert msgs[0]["time"] == ""

    def test_does_not_persist(self, app_env, auth_headers, chat_file):
        """实时取最新一页不落盘（落盘只发生在 sync_all）"""
        client, fake = app_env
        fake.stu_msg.pages = {1: [raw_msg(1)]}
        client.get("/api/messages", headers=auth_headers)
        assert not chat_file.exists()


# ============================ /api/send ============================


class TestSendText:
    def test_short_message_sent_as_is(self, app_env, auth_headers):
        client, fake = app_env
        resp = client.post("/api/send", headers=auth_headers, json={"content": "你好"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        assert fake.stu_msg.sent == [
            {
                "content": "你好",
                "type": 1,
                "resUrl": "",
                "voiceLength": 0,
                "resConfig": "",
            }
        ]

    def test_boundary_message_not_truncated(self, app_env, auth_headers):
        client, fake = app_env
        payload = client.post(
            "/api/send", headers=auth_headers, json={"content": "a" * MSG_MAX_LEN}
        ).get_json()
        assert fake.stu_msg.sent[0]["content"] == "a" * MSG_MAX_LEN
        assert "strategy" not in payload  # 短消息走直发分支，不带策略字段

    def test_empty_content_rejected(self, app_env, auth_headers):
        client, fake = app_env
        resp = client.post("/api/send", headers=auth_headers, json={"content": ""})
        assert resp.status_code == 400
        assert fake.stu_msg.sent == []

    def test_missing_body_rejected(self, app_env, auth_headers):
        """无 JSON body 与 content 为空同属客户端错误，都返回 400"""
        client, _ = app_env
        resp = client.post(
            "/api/send", headers=auth_headers, data="", content_type="application/json"
        )
        assert resp.status_code == 400

    def test_send_failure_reports_status_code(self, app_env, auth_headers):
        client, fake = app_env
        fake.stu_msg.send_result = SendResult(
            ok=False, seewo_code=40000, message="留言内容不能超过200字符"
        )
        payload = client.post(
            "/api/send", headers=auth_headers, json={"content": "x"}
        ).get_json()
        assert payload["status"] == "error"
        assert payload["seewoCode"] == 40000
        assert payload["message"] == "留言内容不能超过200字符"

    def test_success_reports_status_code(self, app_env, auth_headers):
        client, _ = app_env
        payload = client.post(
            "/api/send", headers=auth_headers, json={"content": "x"}
        ).get_json()
        assert payload["status"] == "ok"
        assert payload["seewoCode"] == 200

    def test_token_invalid_triggers_relogin_and_resend(self, app_env, auth_headers):
        """-500（Token 失效）→ 刷新会话后重发一次，服务端未写入故重发安全"""
        client, fake = app_env
        refreshed = []
        fake.refresh = lambda: (refreshed.append(True), fake)[1]
        fake.stu_msg.send_impl = lambda n, content, type: (
            SendResult(ok=False, seewo_code=-500, message="token无效")
            if n == 1
            else SendResult(ok=True, seewo_code=200, message="发送成功")
        )
        payload = client.post(
            "/api/send", headers=auth_headers, json={"content": "x"}
        ).get_json()
        assert refreshed == [True]
        assert len(fake.stu_msg.sent) == 2
        assert payload["status"] == "ok"

    def test_token_expired_505_is_treated_the_same(self, app_env, auth_headers):
        client, fake = app_env
        fake.stu_msg.send_impl = lambda n, content, type: (
            SendResult(ok=False, seewo_code=-505, message="token已过期")
            if n == 1
            else SendResult(ok=True, seewo_code=200, message="发送成功")
        )
        assert (
            client.post(
                "/api/send", headers=auth_headers, json={"content": "x"}
            ).get_json()["status"]
            == "ok"
        )

    def test_relogin_failure_asks_client_to_login(
        self, app_env, auth_headers, monkeypatch
    ):
        """刷新后 Token 仍失效 → 401 need_login，由客户端引导扫码"""
        client, fake = app_env
        fake.stu_msg.send_result = SendResult(
            ok=False, seewo_code=-500, message="token无效"
        )

        def failing_refresh():
            fake.account.token_expired = True  # 重新登录也没换到有效 Token
            return fake

        monkeypatch.setattr(fake, "refresh", failing_refresh)
        resp = client.post("/api/send", headers=auth_headers, json={"content": "x"})
        assert resp.status_code == 401
        assert resp.get_json()["need_login"] is True

    def test_business_error_is_not_retried(self, app_env, auth_headers, monkeypatch):
        """40000 是业务拒绝，重发也会失败，不应触发重新登录"""
        client, fake = app_env
        fake.stu_msg.send_result = SendResult(
            ok=False, seewo_code=40000, message="超长"
        )
        monkeypatch.setattr(
            fake, "refresh", lambda: pytest.fail("业务错误不应触发重登")
        )
        payload = client.post(
            "/api/send", headers=auth_headers, json={"content": "x"}
        ).get_json()
        assert payload["seewoCode"] == 40000
        assert len(fake.stu_msg.sent) == 1

    def test_truncate_strategy_default(self, app_env, auth_headers):
        client, fake = app_env
        long = "字" * (MSG_MAX_LEN + 50)
        payload = client.post(
            "/api/send", headers=auth_headers, json={"content": long}
        ).get_json()
        sent = fake.stu_msg.sent[0]["content"]
        assert payload["strategy"] == "truncate"
        assert payload["truncated"] is True
        assert len(sent) == MSG_MAX_LEN
        assert sent.endswith("...")
        assert sent == "字" * (MSG_MAX_LEN - 3) + "..."

    def test_split_strategy_sends_multiple(self, app_env, auth_headers):
        client, fake = app_env
        long = "行1\n" + "x" * 300
        payload = client.post(
            "/api/send",
            headers=auth_headers,
            json={"content": long, "strategy": "split"},
        ).get_json()
        assert payload["strategy"] == "split"
        assert payload["chunks"] == len(fake.stu_msg.sent)
        assert payload["results"] == [True] * payload["chunks"]
        assert "".join(s["content"] for s in fake.stu_msg.sent) == long
        assert all(len(s["content"]) <= MSG_MAX_LEN for s in fake.stu_msg.sent)

    def test_strategy_override_differs_from_global(
        self, app_env, auth_headers, monkeypatch
    ):
        """单次 strategy 覆盖全局 long_message_strategy"""
        import api_server

        client, fake = app_env
        monkeypatch.setattr(api_server.config, "long_message_strategy", "split")
        long = "y" * (MSG_MAX_LEN + 1)
        payload = client.post(
            "/api/send",
            headers=auth_headers,
            json={"content": long, "strategy": "truncate"},
        ).get_json()
        assert payload["strategy"] == "truncate"
        assert len(fake.stu_msg.sent) == 1

    def test_global_strategy_used_when_not_overridden(
        self, app_env, auth_headers, monkeypatch
    ):
        import api_server

        client, fake = app_env
        monkeypatch.setattr(api_server.config, "long_message_strategy", "split")
        long = "z" * (MSG_MAX_LEN + 1)
        payload = client.post(
            "/api/send", headers=auth_headers, json={"content": long}
        ).get_json()
        assert payload["strategy"] == "split"
        assert len(fake.stu_msg.sent) == 2

    def test_partial_split_failure_reports_error(self, app_env, auth_headers):
        client, fake = app_env
        fake.stu_msg.send_impl = lambda n, content, type: SendResult(
            ok=n != 1, seewo_code=200 if n != 1 else 40000, message=""
        )
        long = "w" * (MSG_MAX_LEN + 1)
        payload = client.post(
            "/api/send",
            headers=auth_headers,
            json={"content": long, "strategy": "split"},
        ).get_json()
        assert payload["status"] == "error"
        assert "部分发送失败" in payload["message"]


# ============================ 本地历史 ============================


class TestHistory:
    def test_pagination(self, app_env, auth_headers, chat_file):
        client, _ = app_env
        write_chat_history(chat_file, [msg_obj(i) for i in range(1, 11)])
        payload = client.get(
            "/api/history?limit=3&offset=0", headers=auth_headers
        ).get_json()
        assert payload["total"] == 10
        assert payload["count"] == 3
        assert [m["id"] for m in payload["messages"]] == [1, 2, 3]

    def test_offset(self, app_env, auth_headers, chat_file):
        client, _ = app_env
        write_chat_history(chat_file, [msg_obj(i) for i in range(1, 11)])
        payload = client.get(
            "/api/history?limit=3&offset=8", headers=auth_headers
        ).get_json()
        assert [m["id"] for m in payload["messages"]] == [9, 10]

    def test_fills_sender_name_from_role(self, app_env, auth_headers, chat_file):
        client, _ = app_env
        write_chat_history(
            chat_file,
            [
                msg_obj(1, sender="student", senderName=""),
                msg_obj(2, sender="parent", senderName=""),
                msg_obj(3, sender="unknown", senderName=""),
            ],
        )
        msgs = client.get("/api/history", headers=auth_headers).get_json()["messages"]
        assert [m["senderName"] for m in msgs] == ["测试学生", "家长", "未知"]

    def test_keeps_existing_sender_name(self, app_env, auth_headers, chat_file):
        client, _ = app_env
        write_chat_history(
            chat_file, [msg_obj(1, sender="student", senderName="原名字")]
        )
        assert (
            client.get("/api/history", headers=auth_headers).get_json()["messages"][0][
                "senderName"
            ]
            == "原名字"
        )

    def test_empty_local_history(self, app_env, auth_headers):
        client, _ = app_env
        payload = client.get("/api/history", headers=auth_headers).get_json()
        assert (payload["total"], payload["count"], payload["messages"]) == (0, 0, [])


class TestLoadEarlier:
    def _seed(self, chat_file, ids):
        write_chat_history(chat_file, [msg_obj(i) for i in ids])

    def test_returns_newest_when_no_cursor(self, app_env, auth_headers, chat_file):
        client, _ = app_env
        self._seed(chat_file, range(1, 11))
        payload = client.get(
            "/api/load_earlier?count=3", headers=auth_headers
        ).get_json()
        assert [m["id"] for m in payload["messages"]] == [8, 9, 10]
        assert payload["has_more"] is True

    def test_returns_strictly_earlier_than_cursor(
        self, app_env, auth_headers, chat_file
    ):
        client, _ = app_env
        self._seed(chat_file, range(1, 11))
        payload = client.get(
            "/api/load_earlier?count=3&before_id=5", headers=auth_headers
        ).get_json()
        assert [m["id"] for m in payload["messages"]] == [2, 3, 4]

    def test_pages_are_contiguous_without_overlap(
        self, app_env, auth_headers, chat_file
    ):
        """游标=返回消息中最早 id，逐页往前取，应无重叠无间隙"""
        client, _ = app_env
        self._seed(chat_file, range(1, 11))
        seen = []
        cursor = 0
        for _ in range(4):
            url = (
                f"/api/load_earlier?count=3&before_id={cursor}"
                if cursor
                else "/api/load_earlier?count=3"
            )
            payload = client.get(url, headers=auth_headers).get_json()
            if not payload["messages"]:
                break
            ids = [m["id"] for m in payload["messages"]]
            seen = ids + seen
            cursor = ids[0]
        assert seen == list(range(1, 11))

    def test_has_more_false_when_exhausted(self, app_env, auth_headers, chat_file):
        client, _ = app_env
        self._seed(chat_file, range(1, 4))
        payload = client.get(
            "/api/load_earlier?count=5", headers=auth_headers
        ).get_json()
        assert payload["has_more"] is False

    def test_empty_local_returns_hint(self, app_env, auth_headers):
        client, _ = app_env
        payload = client.get(
            "/api/load_earlier?count=5", headers=auth_headers
        ).get_json()
        assert payload["messages"] == []
        assert payload["has_more"] is False
        assert payload["message"] == "暂无消息"

    def test_expired_token_can_read_local_history(
        self, app_env, auth_headers, chat_file
    ):
        client, fake = app_env
        self._seed(chat_file, range(1, 4))
        _expired(fake)
        resp = client.get("/api/load_earlier?count=2", headers=auth_headers)
        assert resp.status_code == 200
        assert [m["id"] for m in resp.get_json()["messages"]] == [2, 3]

    def test_cursor_before_oldest_returns_empty(self, app_env, auth_headers, chat_file):
        client, _ = app_env
        self._seed(chat_file, [5, 6, 7])
        payload = client.get(
            "/api/load_earlier?count=5&before_id=5", headers=auth_headers
        ).get_json()
        assert payload["messages"] == []
        assert payload["has_more"] is False


class TestSyncAll:
    def test_pages_backwards_and_persists(self, app_env, auth_headers, chat_file):
        client, fake = app_env
        fake.stu_msg.get_impl = lambda count, start: {
            1: [raw_msg(i) for i in range(90, 101)],
            2: [raw_msg(i) for i in range(80, 90)],
            3: [],
        }.get(start, [])
        payload = client.post(
            "/api/sync_all", headers=auth_headers, json={"batch_size": 10, "delay": 0}
        ).get_json()
        assert payload["status"] == "ok"
        assert payload["total_count"] == 21
        assert (
            json.loads(chat_file.read_text(encoding="utf-8"))["messages"][0]["id"] == 80
        )

    def test_skips_ids_already_local(self, app_env, auth_headers, chat_file):
        client, fake = app_env
        write_chat_history(chat_file, [msg_obj(99), msg_obj(100)])
        fake.stu_msg.get_impl = lambda count, start: {
            1: [raw_msg(99), raw_msg(100)],
            2: [],
        }.get(start, [])
        payload = client.post(
            "/api/sync_all", headers=auth_headers, json={"delay": 0}
        ).get_json()
        assert payload["synced_count"] == 0
        assert payload["total_count"] == 2

    def test_accepts_empty_body(self, app_env, auth_headers):
        client, fake = app_env
        fake.stu_msg.get_impl = lambda count, start: [raw_msg(1)] if start == 1 else []
        resp = client.post("/api/sync_all", headers=auth_headers, json={})
        assert resp.status_code == 200


# ============================ 会话与配置 ============================


class TestRefresh:
    def test_calls_session_refresh(self, app_env, auth_headers):
        client, fake = app_env
        called = []
        fake.refresh = lambda: called.append(True)
        assert (
            client.post("/api/refresh", headers=auth_headers).get_json()["status"]
            == "ok"
        )
        assert called == [True]


class TestConfigReload:
    def test_api_key_hot_reload_takes_effect(
        self, app_env, tmp_path, monkeypatch, auth_headers
    ):
        import init

        path = tmp_path / "config.json"
        path.write_text(
            json.dumps({"api_key": "new-key", "use_mock": False}), encoding="utf-8"
        )
        monkeypatch.setattr(init, "CONFIG_FILE", str(path))

        client, _ = app_env
        assert (
            client.post("/api/config/reload", headers=auth_headers).status_code == 200
        )
        assert client.get("/api/status", headers=auth_headers).status_code == 401
        assert (
            client.get("/api/status", headers={"X-API-Key": "new-key"}).status_code
            == 200
        )

    def test_echoes_reloaded_values(self, app_env, tmp_path, monkeypatch, auth_headers):
        import init

        path = tmp_path / "config.json"
        path.write_text(
            json.dumps({"long_message_strategy": "split", "log_level": "DEBUG"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(init, "CONFIG_FILE", str(path))

        client, _ = app_env
        payload = client.post("/api/config/reload", headers=auth_headers).get_json()
        assert payload["status"] == "ok"
        assert payload["long_message_strategy"] == "split"
        assert payload["log_level"] == "DEBUG"


class TestExecuteRemoved:
    """路径 B 不再承担命令执行职责（原 /api/execute 已移除，命令只在路径 A 的留言监听中处理）"""

    @pytest.mark.parametrize("command", ["status", "getpass 1 2", "rm -rf /"])
    def test_endpoint_is_gone(self, app_env, auth_headers, command):
        client, _ = app_env
        resp = client.post(
            "/api/execute", headers=auth_headers, json={"command": command}
        )
        assert resp.status_code == 404

    def test_client_sdk_has_no_execute(self):
        from client import SeewoClient

        assert not hasattr(SeewoClient, "execute_command")
