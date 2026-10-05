# -*- coding: utf-8 -*-
"""第 1 层：数据模型（models.py）—— from_dict 兜底、可变性、跨进程边界转换"""

import dataclasses

import pytest

from models import (
    Config,
    Message,
    MessageResponse,
    RawMessage,
    SeewoCode,
    SeewoQrCode,
    SendResult,
    YunbanClass,
    YunbanEvent,
    YunbanNote,
    YunbanNotesPage,
    YunbanParent,
    YunbanStudent,
)


class TestRawMessage:
    def test_from_dict_maps_all_fields(self):
        raw = RawMessage.from_dict(
            {
                "id": 42,
                "senderUid": "u1",
                "senderType": "parent",
                "senderName": "家长",
                "type": 2,
                "content": "hi",
                "resUrl": "http://x/a.png",
                "voiceLength": 5,
                "resConfig": "{}",
                "createTime": 1700000000000,
            }
        )
        assert raw == RawMessage(
            id=42,
            senderUid="u1",
            senderType="parent",
            senderName="家长",
            type=2,
            content="hi",
            resUrl="http://x/a.png",
            voiceLength=5,
            resConfig="{}",
            createTime=1700000000000,
        )

    def test_from_dict_missing_fields_use_defaults(self):
        """字段缺失时兜底，对齐原 dict.get(key, default) 行为"""
        raw = RawMessage.from_dict({})
        assert raw.id == 0
        assert raw.senderType == "unknown"
        assert raw.type == 1
        assert raw.content == ""
        assert raw.voiceLength == 0

    def test_from_dict_coerces_id_to_int(self):
        assert RawMessage.from_dict({"id": "123"}).id == 123

    def test_from_dict_ignores_unknown_keys(self):
        raw = RawMessage.from_dict({"id": 1, "somethingNew": "x"})
        assert not hasattr(raw, "somethingNew")

    def test_frozen(self):
        raw = RawMessage.from_dict({"id": 1})
        with pytest.raises(dataclasses.FrozenInstanceError):
            raw.id = 2


class TestMessage:
    def test_from_dict_keeps_type_as_is(self):
        """type 不强制 int：保留 chat_history 文件里的原始类型"""
        assert Message.from_dict({"id": 1, "type": "2"}).type == "2"
        assert Message.from_dict({"id": 1, "type": 2}).type == 2

    def test_from_dict_defaults(self):
        m = Message.from_dict({})
        assert m.id == 0
        assert m.sender == "unknown"
        assert m.senderName == ""
        assert m.time == ""

    def test_asdict_roundtrip(self):
        m = Message.from_dict(
            {
                "id": 7,
                "time": "t",
                "content": "c",
                "type": 3,
                "sender": "parent",
                "senderName": "家长",
                "resUrl": "u",
            }
        )
        assert Message.from_dict(dataclasses.asdict(m)) == m


class TestMessageResponse:
    def test_from_dict_wraps_result(self):
        resp = MessageResponse.from_dict(
            {"statusCode": 200, "message": "ok", "result": [{"id": 1}, {"id": 2}]}
        )
        assert resp.statusCode == 200
        assert [m.id for m in resp.result] == [1, 2]
        assert all(isinstance(m, RawMessage) for m in resp.result)

    def test_from_dict_missing_result(self):
        assert MessageResponse.from_dict({"statusCode": 200}).result == []


class TestConfig:
    def test_from_dict_known_fields(self):
        cfg = Config.from_dict({"api_key": "k", "api_port": 1234, "use_mock": True})
        assert (cfg.api_key, cfg.api_port, cfg.use_mock) == ("k", 1234, True)

    def test_from_dict_maps_ban_pai_config(self):
        cfg = Config.from_dict({"banPaiConfig": {"topStartTime": "06:40"}})
        assert cfg.ban_pai_config == {"topStartTime": "06:40"}

    def test_from_dict_keeps_unknown_keys_in_extra(self):
        """未建模的键进 extra 原样保留，模型化不丢用户自定义配置"""
        cfg = Config.from_dict({"api_key": "k", "myCustomKey": {"a": 1}})
        assert cfg.extra == {"myCustomKey": {"a": 1}}

    def test_from_dict_empty_uses_defaults(self):
        cfg = Config.from_dict({})
        assert cfg.api_port == 5001
        assert cfg.use_mock is False
        assert cfg.long_message_strategy == "truncate"
        assert cfg.long_message_split_pattern == r"\r?\n"
        assert cfg.ban_pai_config == {}

    def test_each_instance_has_independent_dict_fields(self):
        """dict 字段不能共享默认值（用 default_factory）"""
        a, b = Config(), Config()
        a.extra["x"] = 1
        a.ban_pai_config["y"] = 1
        assert b.extra == {} and b.ban_pai_config == {}

    def test_mutable_for_hot_reload(self):
        cfg = Config()
        cfg.api_key = "new"
        assert cfg.api_key == "new"


class TestSeewoCode:
    """状态码语义的唯一约定：业务码与 HTTP 码是两套，不得互相赋值/比较"""

    def test_members(self):
        assert int(SeewoCode.OK) == 200
        assert int(SeewoCode.TOKEN_INVALID) == -500
        assert int(SeewoCode.TOKEN_EXPIRED) == -505
        assert int(SeewoCode.BUSINESS_REJECTED) == 40000
        assert int(SeewoCode.SERVER_ERROR) == 50000

    def test_relogin_codes(self):
        assert SeewoCode.is_relogin_code(SeewoCode.TOKEN_INVALID)
        assert SeewoCode.is_relogin_code(SeewoCode.TOKEN_EXPIRED)
        assert not SeewoCode.is_relogin_code(SeewoCode.OK)
        assert not SeewoCode.is_relogin_code(SeewoCode.BUSINESS_REJECTED)
        assert not SeewoCode.is_relogin_code(502)  # HTTP 码不是业务码

    def test_business_reject(self):
        assert SeewoCode.is_business_reject(SeewoCode.BUSINESS_REJECTED)
        assert not SeewoCode.is_business_reject(SeewoCode.TOKEN_INVALID)

    def test_ok(self):
        assert SeewoCode.is_ok(200)
        assert not SeewoCode.is_ok(SeewoCode.SERVER_ERROR)


class TestSeewoQrCode:
    def test_members(self):
        assert (
            int(SeewoQrCode.WAITING),
            int(SeewoQrCode.SCANNED),
            int(SeewoQrCode.CONFIRMED),
        ) == (200, 201, 202)


class TestSendResult:
    def test_from_seewo_code_success(self):
        result = SendResult.from_seewo_code(SeewoCode.OK, "发送成功")
        assert result.ok is True
        assert bool(result) is True
        assert result.seewo_code == 200
        assert result.message == "发送成功"

    def test_from_seewo_code_token_invalid(self):
        result = SendResult.from_seewo_code(SeewoCode.TOKEN_INVALID, "token无效")
        assert result.ok is False
        assert bool(result) is False
        assert result.needs_relogin is True
        assert result.is_business_reject is False

    def test_from_seewo_code_business_rejected(self):
        result = SendResult.from_seewo_code(SeewoCode.BUSINESS_REJECTED, "超长")
        assert result.needs_relogin is False
        assert result.is_business_reject is True

    def test_http_status_is_kept_separate_from_seewo_code(self):
        """业务码与 HTTP 码分别存放：502 绝不会被写成业务码"""
        result = SendResult.from_seewo_code(SeewoCode.OK, http_status=200)
        assert (result.seewo_code, result.http_status) == (200, 200)

        transport_failure = SendResult.from_http_error(502, "Bad Gateway")
        assert transport_failure.seewo_code == 0
        assert transport_failure.http_status == 502
        assert transport_failure.needs_relogin is False

    def test_missing_code_is_not_ok(self):
        assert SendResult().ok is False
        assert SendResult().seewo_code == 0


class TestYunbanClass:
    def test_from_dict_full(self):
        c = YunbanClass.from_dict(
            {
                "uid": "c1",
                "name": "测试班A",
                "roomUid": "r1",
                "roomName": "R",
                "description": "d",
                "schoolUid": "s1",
                "schoolName": "S",
                "schoolType": "t",
            }
        )
        assert (c.uid, c.name, c.roomUid, c.schoolUid) == (
            "c1",
            "测试班A",
            "r1",
            "s1",
        )
        assert c.extra == {}

    def test_from_dict_collects_extra(self):
        c = YunbanClass.from_dict({"uid": "c1", "newField": 1})
        assert c.extra == {"newField": 1}
        assert c.name == ""

    def test_asdict_then_from_dict_preserves_extra(self):
        c = YunbanClass.from_dict({"uid": "c1", "newField": 1})
        again = YunbanClass.from_dict(dataclasses.asdict(c))
        assert again.uid == "c1"
        assert again.extra == {"newField": 1}
        assert YunbanClass.from_dict(dataclasses.asdict(again)) == again

    def test_top_level_extra_overrides_nested_extra(self):
        c = YunbanClass.from_dict(
            {"uid": "c1", "extra": {"newField": "old"}, "newField": "new"}
        )
        assert c.extra == {"newField": "new"}

    def test_non_dict_nested_extra_is_ignored(self):
        """extra 字段为非 dict 时按 {} 兜底，未知字段仍正常收集"""
        c = YunbanClass.from_dict({"uid": "c1", "extra": 5, "newField": 1})
        assert c.extra == {"newField": 1}


@pytest.mark.parametrize(
    "model", [YunbanStudent, YunbanEvent, YunbanParent, YunbanNote]
)
def test_yunban_models_round_trip_extra(model):
    instance = model.from_dict({"unknownField": {"value": 1}})
    assert model.from_dict(dataclasses.asdict(instance)) == instance


class TestYunbanStudent:
    def test_from_dict_coerces_card_list_and_handles_none(self):
        s = YunbanStudent.from_dict(
            {"name": "测试学生乙", "sid": "S1", "extendCardIds": None}
        )
        assert s.extendCardIds == []
        assert s.gender == 0
        assert YunbanStudent.from_dict({"extendCardIds": ["A", "B"]}).extendCardIds == [
            "A",
            "B",
        ]

    def test_gender_semantics(self):
        assert YunbanStudent.from_dict({"gender": 1}).gender == 1
        assert YunbanStudent.from_dict({"gender": 2}).gender == 2


class TestYunbanEvent:
    def test_from_dict_list_fields_default_empty(self):
        e = YunbanEvent.from_dict(
            {"name": "上午考勤", "classes": None, "attendanceStudents": None}
        )
        assert e.name == "上午考勤"
        assert e.classes == []
        assert e.attendanceStudents == []
        assert e.isRoomBaseOnClass is False

    def test_from_dict_keeps_config_json_string(self):
        e = YunbanEvent.from_dict(
            {"config": '{"banPaiConfig": {"topStartTime": "06:40"}}'}
        )
        assert e.config.startswith("{")


class TestYunbanParentAndNote:
    def test_parent_from_dict(self):
        p = YunbanParent.from_dict(
            {
                "parentName": "某人",
                "bindWx": True,
                "notReadNoteCount": 3,
                "parentShowIndex": 1,
            }
        )
        assert (p.parentName, p.bindWx, p.notReadNoteCount, p.parentShowIndex) == (
            "某人",
            True,
            3,
            1,
        )

    def test_note_from_dict_coerces_id_to_str(self):
        """YunbanNote.id 是 str（云班接口返回字符串 id）"""
        n = YunbanNote.from_dict(
            {"id": 12345, "content": "hi", "replies": None, "options": None}
        )
        assert n.id == "12345"
        assert n.replies == [] and n.options == []
        assert YunbanNote.from_dict({"id": "9"}).id == "9"

    def test_notes_page_wraps_notes(self):
        page = YunbanNotesPage.from_dict(
            {
                "page": 1,
                "pageSize": 10,
                "totalCount": 2,
                "result": [{"id": "1"}, {"id": "2"}],
            }
        )
        assert page.totalCount == 2
        assert [n.id for n in page.result] == ["1", "2"]
        assert all(isinstance(n, YunbanNote) for n in page.result)

    def test_notes_page_empty(self):
        page = YunbanNotesPage.from_dict({})
        assert (page.page, page.result) == (0, [])
