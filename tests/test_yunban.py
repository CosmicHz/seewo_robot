# -*- coding: utf-8 -*-
"""第 2 层：云班 REST（yunban.py）—— 端点 URL、模型转换、考勤/留言请求。

注意：yunban.py 直接 requests.request，绕过 request_manager（无统一节流），
因此这里用 responses 拦截裸 requests，而不是打桩 request_manager。
"""

import json
import re

import pytest

import yunban as yunban_module
from models import YunbanClass, YunbanEvent, YunbanStudent

BASE = "https://campus.seewo.com/mis-cloud-route-server"
SCHOOL = "mock_school_001"


@pytest.fixture
def client():
    return yunban_module.yunban(token="acw-token", schoolid=SCHOOL)


def _classes_url():
    return f"{BASE}/api/classmember/v1/school/{SCHOOL}/classes"


def _students_url(class_uid):
    return f"{BASE}/api/classmember/v1/school/{SCHOOL}/students?classUids={class_uid}"


def _events_url(room_uid):
    return f"{BASE}/api/attendance/v3/{SCHOOL}/events?roomUid={room_uid}"


def _notes_url(uid, parent_uid):
    return f"{BASE}/api/kidnote/v4/parent/{parent_uid}/child/{uid}/notes"


def _parents_url(uid):
    return f"{BASE}/api/kidnote/v1/{uid}/parent/note/count"


EVENT_ROW = {
    "eventId": "e1",
    "name": "上午考勤",
    "startTime": "06:40",
    "endTime": "07:30",
    "overTime": "07:35",
    "config": json.dumps(
        {"banPaiConfig": {"topStartTime": "06:40", "topEndTime": "07:20"}}
    ),
    "classes": [{"className": "测试班A", "classUid": "c1"}],
}


class TestEndpoints:
    def test_class_list(self, http, client):
        http.add(
            http.GET,
            _classes_url(),
            json={"data": [{"uid": "c1", "name": "测试班A", "roomUid": "r1"}]},
        )
        classes = client.getclasslist()
        assert isinstance(classes[0], YunbanClass)
        assert (classes[0].uid, classes[0].name, classes[0].roomUid) == (
            "c1",
            "测试班A",
            "r1",
        )

    def test_class_list_sends_acw_tc_cookie(self, http, client):
        http.add(http.GET, _classes_url(), json={"data": []})
        client.getclasslist()
        assert http.calls[0].request.headers["Cookie"] == "acw_tc=acw-token"

    def test_student_list_unwraps_nested_data(self, http, client):
        """返回结构是 data[0]["students"]，需要剥两层"""
        http.add(
            http.GET,
            _students_url("c1"),
            json={
                "data": [
                    {
                        "classUid": "c1",
                        "students": [{"name": "测试学生乙", "uid": "u2", "sid": "S2"}],
                    }
                ]
            },
        )
        students = client.getstulist("c1")
        assert isinstance(students[0], YunbanStudent)
        assert students[0].name == "测试学生乙"

    def test_events_carry_config_and_classes(self, http, client):
        http.add(http.GET, _events_url("r1"), json={"data": [EVENT_ROW]})
        events = client.getevents("r1")
        assert isinstance(events[0], YunbanEvent)
        assert events[0].name == "上午考勤"
        assert events[0].classes == [{"className": "测试班A", "classUid": "c1"}]

    def test_events_empty(self, http, client):
        http.add(http.GET, _events_url("r1"), json={"data": []})
        assert client.getevents("r1") == []

    def test_notes_page(self, http, client):
        http.add(
            http.GET,
            re.compile(re.escape(_notes_url("u1", "p1")) + r".*"),
            json={
                "data": {
                    "page": 1,
                    "pageSize": 10,
                    "totalCount": 1,
                    "result": [{"id": "77", "content": "hi"}],
                }
            },
        )
        page = client.getnotes("u1", "p1", 1, 10)
        assert page.totalCount == 1
        assert page.result[0].id == "77"

    def test_notes_query_params(self, http, client):
        http.add(
            http.GET,
            re.compile(re.escape(_notes_url("u1", "p1")) + r".*"),
            json={"data": {"result": []}},
        )
        client.getnotes("u1", "p1", 3, 5)
        assert "start=3" in http.calls[0].request.url
        assert "pageSize=5" in http.calls[0].request.url

    def test_parent_list(self, http, client):
        http.add(
            http.GET,
            _parents_url("u1"),
            json={
                "data": [{"parentName": "某人", "parentUserUid": "p1", "bindWx": True}]
            },
        )
        parents = client.getparents("u1")
        assert parents[0].parentName == "某人"
        assert parents[0].bindWx is True


class TestSearch:
    @pytest.fixture
    def students(self):
        return [
            YunbanStudent.from_dict({"name": "甲", "uid": "u1"}),
            YunbanStudent.from_dict({"name": "乙", "uid": "u2"}),
        ]

    def test_find_by_name(self, client, students):
        assert client.searchstubyname("乙", students).uid == "u2"

    def test_find_by_name_miss_returns_none(self, client, students):
        assert client.searchstubyname("丙", students) is None

    def test_find_by_uid(self, client, students):
        assert client.searchstubyuid("u1", students).name == "甲"

    def test_find_by_uid_miss_returns_none(self, client, students):
        assert client.searchstubyuid("nope", students) is None


class TestEventTime:
    def test_parses_ban_pai_window(self, client):
        assert client.geteventtime(YunbanEvent.from_dict(EVENT_ROW)) == (
            "06:40",
            "07:20",
        )

    def test_missing_config_raises(self, client):
        with pytest.raises(json.JSONDecodeError):
            client.geteventtime(YunbanEvent.from_dict({"config": ""}))

    def test_random_time_within_window(self, client, monkeypatch):
        monkeypatch.setattr(yunban_module.random, "uniform", lambda a, b: b)
        assert client.random_time_in_range("06:40", "07:20") == "07:20:00"
        monkeypatch.setattr(yunban_module.random, "uniform", lambda a, b: a)
        assert client.random_time_in_range("06:40", "07:20") == "06:40:00"

    def test_random_time_format(self, client):
        assert re.fullmatch(
            r"\d{2}:\d{2}:\d{2}", client.random_time_in_range("06:40", "07:20")
        )

    def test_random_time_stays_inside_window(self, client):
        for _ in range(50):
            assert (
                "06:40:00"
                <= client.random_time_in_range("06:40", "07:20")
                <= "07:20:00"
            )

    def test_reversed_range_raises(self, client):
        with pytest.raises(ValueError, match="晚于开始时间"):
            client.random_time_in_range("08:00", "07:00")

    def test_equal_range_raises(self, client):
        with pytest.raises(ValueError):
            client.random_time_in_range("08:00", "08:00")

    def test_randomtime_uses_ban_pai_start_and_event_end(self, client, monkeypatch):
        captured = {}

        def fake(start, end):
            captured["range"] = (start, end)
            return "07:00:00"

        monkeypatch.setattr(client, "random_time_in_range", fake)
        assert client.randomtime(YunbanEvent.from_dict(EVENT_ROW)) == "07:00:00"
        assert captured["range"] == ("06:40", "07:30")


class TestAttend:
    def test_payload_shape(self, http, client):
        http.add(
            http.POST,
            f"{BASE}/api/attendance/v1/{SCHOOL}/data",
            json={"data": {"code": 0}},
        )
        event = YunbanEvent.from_dict(EVENT_ROW)
        result = client.attend(
            "甲", "u1", "S1", event, "2026-09-25", "07:00:00", "c1", "r1"
        )
        assert result == {"data": {"code": 0}}

        payload = json.loads(http.calls[0].request.body)
        row = payload["attendanceData"][0]
        assert row["eventId"] == "e1"
        assert row["userUid"] == "u1"
        assert row["userName"] == "甲"
        assert row["userSid"] == "S1"
        assert row["attendanceDate"] == "2026-09-25"
        assert row["attendanceTime"] == "07:00:00"
        assert row["roomUid"] == "r1"
        assert row["classUid"] == "c1"
        assert (row["eventStartTime"], row["eventEndTime"]) == ("06:40", "07:20")


class TestSendMsg:
    def test_text_sent(self, http, client):
        http.add(
            http.POST,
            f"{BASE}/api/kidnote/v1/note",
            json={"statusCode": 200, "data": {"id": 1}},
        )
        assert client.send_msg("u1", "u2", "你好", 1).ok is True
        payload = json.loads(http.calls[0].request.body)
        assert payload["content"] == "你好"
        assert payload["senderType"] == "student"
        assert payload["schoolUid"] == SCHOOL

    def test_media_payload(self, http, client):
        http.add(http.POST, f"{BASE}/api/kidnote/v1/note", json={"statusCode": 200})
        client.send_msg("u1", "u2", "", 3, resUrl="http://cdn/a.mp3", voiceLength=666)
        payload = json.loads(http.calls[0].request.body)
        assert payload["resUrl"] == "http://cdn/a.mp3"
        assert payload["voiceLength"] == 666
        assert payload["content"] == ""

    def test_non_200_returns_false_with_seewo_code(self, http, client):
        """非 200 返回 ok=False 且保留业务码（原实现是死分支，会返回响应 dict）"""
        http.add(
            http.POST,
            f"{BASE}/api/kidnote/v1/note",
            json={"statusCode": 40000, "message": "留言内容不能超过200字符"},
            status=400,
        )
        result = client.send_msg("u1", "u2", "x" * 201, 1)
        assert result.ok is False
        assert result.seewo_code == 40000
        assert result.http_status == 400
        assert result.is_business_reject is True
        assert bool(result) is False

    def test_http_error_without_json_body(self, http, client):
        """网关返回 HTML 时不应崩：HTTP 码进 http_status，业务码保持 0（两者不混）"""
        http.add(
            http.POST,
            f"{BASE}/api/kidnote/v1/note",
            body="<html>502</html>",
            status=502,
            content_type="text/html",
        )
        result = client.send_msg("u1", "u2", "x", 1)
        assert result.ok is False
        assert result.http_status == 502
        assert result.seewo_code == 0
        assert result.needs_relogin is False
