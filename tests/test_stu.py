# -*- coding: utf-8 -*-
"""第 2 层：学生信息 DAO（stu.py）"""

import pytest

import stu as stu_module
from conftest import FakeAccount, decode_action_request, mcampus_url, px_wrap

LIST_ACTION = "GET_STUDENT_V1_PARENT_BYPARENTID_CHILDREN_LIST"
SEARCH_ACTION = "POST_STUDENT_V1_BYSCHOOLUID_CLASS_BYCLASSUID_STUDENTS"

STUDENTS = [
    {"schoolUid": "s1", "classUid": "c1", "userUid": "u1", "realName": "测试学生"},
    {"schoolUid": "s2", "classUid": "c2", "userUid": "u2", "realName": "测试学生乙"},
]


def _register_list(http, payload):
    http.add(http.POST, mcampus_url(LIST_ACTION), json=px_wrap(payload))


class TestInit:
    def test_loads_first_student_by_default(self, http):
        _register_list(http, STUDENTS)
        s = stu_module.stu(FakeAccount())
        assert (s.schoolUid, s.classUid, s.userUid, s.name) == (
            "s1",
            "c1",
            "u1",
            "测试学生",
        )

    def test_count_selects_student(self, http):
        _register_list(http, STUDENTS)
        assert stu_module.stu(FakeAccount(), count=1).userUid == "u2"

    def test_sends_parent_id(self, http):
        _register_list(http, STUDENTS)
        stu_module.stu(FakeAccount())
        action, params = decode_action_request(http.calls[0].request.body)
        assert action == LIST_ACTION
        assert params == {"parentId": "mock_parent_001"}

    def test_name_falls_back_when_absent(self, http):
        _register_list(http, [{"schoolUid": "s", "classUid": "c", "userUid": "u"}])
        assert stu_module.stu(FakeAccount()).name == "学生"

    def test_empty_student_list_raises(self, http):
        _register_list(http, [])
        with pytest.raises(Exception, match="未添加学生"):
            stu_module.stu(FakeAccount())

    def test_out_of_range_count_raises_clear_index_error(self, http):
        _register_list(http, STUDENTS)
        with pytest.raises(IndexError, match="student count out of range: 5"):
            stu_module.stu(FakeAccount(), count=5)


class TestSearch:
    def test_search_by_name_sends_class_scope(self, http):
        _register_list(http, STUDENTS)
        s = stu_module.stu(FakeAccount())
        http.add(
            http.POST,
            mcampus_url(SEARCH_ACTION),
            json=px_wrap([{"userUid": "u9", "realName": "测试学生乙"}]),
        )
        result = s.get_stu("测试学生乙")
        action, params = decode_action_request(http.calls[-1].request.body)
        assert action == SEARCH_ACTION
        assert params == {"schoolUid": "s1", "classUid": "c1", "name": "测试学生乙"}
        assert result[0]["userUid"] == "u9"


def test_add_stu_is_explicitly_not_implemented(http):
    """未确认真实接口前，add_stu 应明确报告未实现"""
    _register_list(http, STUDENTS)
    s = stu_module.stu(FakeAccount())
    with pytest.raises(NotImplementedError, match="adding students is not implemented"):
        s.add_stu("u9")
    assert len(http.calls) == 1
