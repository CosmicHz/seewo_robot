# -*- coding: utf-8 -*-
"""第 2 层：留言 DAO（msg.py）—— 分页参数、响应解析、send 各类型分支。"""

import json

import pytest

import msg as msg_module
from conftest import (
    FakeAccount,
    FakeStudent,
    decode_action_request,
    mcampus_url,
    px_wrap,
)

GET_NOTES = "GET_KIDNOTE_V1_BYPARENTUID_BYCHILDUID_NOTES"
POST_NOTE = "POST_KIDNOTE_V1_NOTE"
DELETE_NOTE = "DELETE_KIDNOTE_V1_NOTE"


@pytest.fixture
def dao():
    return msg_module.msg(FakeAccount(), FakeStudent())


def _note(mid):
    return {
        "id": mid,
        "senderUid": "mock_student_001",
        "senderType": "student",
        "senderName": "测试学生",
        "type": 1,
        "content": f"消息{mid}",
        "createTime": 1700000000000,
    }


class TestGet:
    def test_sends_paging_and_identity_params(self, http, dao):
        http.add(
            http.POST,
            mcampus_url(GET_NOTES),
            json=px_wrap({"statusCode": 200, "result": []}),
        )
        dao.get(10, start=3)
        action, params = decode_action_request(http.calls[0].request.body)
        assert action == GET_NOTES
        assert params == {
            "start": 3,
            "pageSize": 10,
            "parentUid": "mock_parent_001",
            "childUid": "mock_student_001",
        }

    def test_default_start_is_first_page(self, http, dao):
        http.add(
            http.POST,
            mcampus_url(GET_NOTES),
            json=px_wrap({"statusCode": 200, "result": []}),
        )
        dao.get(5)
        assert decode_action_request(http.calls[0].request.body)[1]["start"] == 1

    def test_parses_result_into_raw_messages(self, http, dao):
        http.add(
            http.POST,
            mcampus_url(GET_NOTES),
            json=px_wrap({"statusCode": 200, "result": [_note(1), _note(2)]}),
        )
        resp = dao.get(10)
        assert resp.statusCode == 200
        assert [m.id for m in resp.result] == [1, 2]
        assert resp.result[0].content == "消息1"

    def test_uses_account_headers(self, http, dao):
        """走 mheaders（m-campus 网关），而非 campus 的 headers"""
        http.add(
            http.POST,
            mcampus_url(GET_NOTES),
            json=px_wrap({"statusCode": 200, "result": []}),
        )
        dao.get(1)
        assert http.calls[0].request.headers["cookie"] == "x-token=fake"

    def test_status_code_error_yields_empty_result(self, http, dao):
        """生产 50000（如 start<1）只体现在 statusCode 上，调用方需自查"""
        http.add(
            http.POST,
            mcampus_url(GET_NOTES),
            json=px_wrap({"statusCode": 50000, "message": "SQL 错误"}),
        )
        resp = dao.get(10, start=0)
        assert resp.statusCode == 50000
        assert resp.result == []

    def test_unwrapped_error_response_raises_keyerror(self, http, dao):
        """未知 action / 鉴权失败时响应体没有 data 字段，pxdecode 会 KeyError"""
        http.add(
            http.POST,
            mcampus_url(GET_NOTES),
            json={"statusCode": -500, "message": "token 无效"},
        )
        with pytest.raises(KeyError):
            dao.get(10)


class TestHelpers:
    def test_get_id_returns_first_result_id(self, http, dao):
        http.add(
            http.POST, mcampus_url(GET_NOTES), json=px_wrap({"result": [_note(7)]})
        )
        assert dao.get_id(10) == 7

    def test_get_id_returns_zero_when_empty(self, http, dao):
        http.add(http.POST, mcampus_url(GET_NOTES), json=px_wrap({"result": []}))
        assert dao.get_id(10) == 0

    def test_get_all_ids_sorted_ascending(self, http, dao):
        """页内顺序不作假设，DAO 内部按 id 升序"""
        http.add(
            http.POST,
            mcampus_url(GET_NOTES),
            json=px_wrap({"result": [_note(9), _note(3), _note(5)]}),
        )
        assert dao.get_all_ids(10) == [3, 5, 9]

    def test_get_content_by_index_is_oldest_first(self, http, dao):
        rows = [_note(9), _note(3), _note(5)]
        http.add(http.POST, mcampus_url(GET_NOTES), json=px_wrap({"result": rows}))
        assert dao.get_content_by_index(10, 0) == "消息3"
        assert dao.get_content_by_index(10, 2) == "消息9"

    def test_get_msg_detail_returns_raw_message(self, http, dao):
        http.add(
            http.POST,
            mcampus_url(GET_NOTES),
            json=px_wrap({"result": [_note(9), _note(3)]}),
        )
        detail = dao.get_msg_detail(10, 0)
        assert detail.id == 3 and detail.content == "消息3"

    def test_get_content_warns_deprecated(self, http, dao):
        http.add(
            http.POST, mcampus_url(GET_NOTES), json=px_wrap({"result": [_note(1)]})
        )
        with pytest.warns(DeprecationWarning):
            assert dao.get_content(10) == "消息1"

    def test_get_last_warns_deprecated(self, dao, monkeypatch):
        monkeypatch.setattr(
            msg_module.requests,
            "get",
            lambda *a, **kw: type(
                "R", (), {"text": json.dumps({"data": [{"lastMsgTips": "最近一条"}]})}
            )(),
        )
        with pytest.warns(DeprecationWarning):
            assert dao.get_last() == "最近一条"


class TestSend:
    def test_text_types_carry_content(self, http, dao):
        for type_code in (0, 1):
            http.add(http.POST, mcampus_url(POST_NOTE), json={"statusCode": 200})
            assert dao.send("你好", type_code).ok is True
            _, params = decode_action_request(http.calls[-1].request.body)
            assert params["content"] == "你好"
            assert params["senderType"] == "parent"
            assert params["type"] == type_code
            assert params["schoolUid"] == "mock_school_001"
            assert params["receiverUid"] == "mock_student_001"

    def test_media_types_carry_res_url(self, http, dao):
        for type_code in (2, 4, 5):
            http.add(http.POST, mcampus_url(POST_NOTE), json={"statusCode": 200})
            dao.send("", type_code, "http://cdn/a.png")
            _, params = decode_action_request(http.calls[-1].request.body)
            assert params["resUrl"] == "http://cdn/a.png"
            assert "content" not in params

    def test_audio_type_carries_voice_length(self, http, dao):
        http.add(http.POST, mcampus_url(POST_NOTE), json={"statusCode": 200})
        dao.send("", 3, "http://cdn/a.mp3", 666)
        _, params = decode_action_request(http.calls[-1].request.body)
        assert params["voiceLength"] == 666 and params["resUrl"] == "http://cdn/a.mp3"

    def test_rich_type_carries_res_config(self, http, dao):
        http.add(http.POST, mcampus_url(POST_NOTE), json={"statusCode": 200})
        dao.send("", 6, "http://cdn/a", resConfig="{}")
        assert (
            decode_action_request(http.calls[-1].request.body)[1]["resConfig"] == "{}"
        )

    def test_invalid_token_returns_false(self, http, dao):
        """-500 是 Token 失效：结果保留状态码，供上层决定是否重登"""
        http.add(
            http.POST,
            mcampus_url(POST_NOTE),
            json={"statusCode": -500, "message": "token无效"},
        )
        result = dao.send("x", 1)
        assert result.ok is False
        assert result.seewo_code == -500
        assert bool(result) is False

    def test_token_expired_code_is_preserved(self, http, dao):
        http.add(
            http.POST,
            mcampus_url(POST_NOTE),
            json={"statusCode": -505, "message": "token已过期"},
        )
        assert dao.send("x", 1).seewo_code == -505

    def test_business_error_returns_false(self, http, dao):
        """超过 200 字时服务器返回 40000"""
        http.add(
            http.POST,
            mcampus_url(POST_NOTE),
            json={"statusCode": 40000, "message": "留言内容不能超过200字符"},
        )
        result = dao.send("x" * 201, 1)
        assert result.ok is False
        assert result.seewo_code == 40000
        assert result.message == "留言内容不能超过200字符"


class TestDelete:
    def test_deletes_first_message_id(self, http, dao):
        http.add(
            http.POST, mcampus_url(GET_NOTES), json=px_wrap({"result": [_note(42)]})
        )
        http.add(http.POST, mcampus_url(DELETE_NOTE), json={"statusCode": 200})
        assert dao.delete(10) is True
        action, params = decode_action_request(http.calls[-1].request.body)
        assert action == DELETE_NOTE and params == {"ids": [42]}

    def test_invalid_token_returns_false(self, http, dao):
        http.add(
            http.POST, mcampus_url(GET_NOTES), json=px_wrap({"result": [_note(42)]})
        )
        http.add(http.POST, mcampus_url(DELETE_NOTE), json={"statusCode": -500})
        assert dao.delete(10) is False
