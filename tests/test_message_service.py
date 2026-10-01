# -*- coding: utf-8 -*-
"""第 3 层：消息数据源层（message_service.py）—— 格式化、缓存、分页、同步。

真实 MessageDataSource + 临时 chat_history 文件（打桩点 B）；出网部分用假 stu_msg（打桩点 A）。
"""

import json

import pytest

import message_service as ms_module
from conftest import FakeSession, FakeStuMsg, raw_msg, write_chat_history, msg_obj
from message_service import MessageDataSource


@pytest.fixture
def ds(chat_file):
    """默认：本地无记录、上游无数据的数据源"""
    return MessageDataSource(FakeSession())


def _ds_with(pages=None, get_impl=None):
    session = FakeSession(stu_msg=FakeStuMsg(pages=pages, get_impl=get_impl))
    return MessageDataSource(session), session


# ============================ 格式化 ============================


class TestFormatMsg:
    @pytest.fixture
    def source(self, chat_file):
        return MessageDataSource(FakeSession())

    def _fmt(self, source, raw):
        return source._format_msg(
            raw, "mock_parent_001", "mock_student_001", "测试学生"
        )

    def test_parent_sender(self, source):
        m = self._fmt(
            source,
            raw_msg(
                1, senderUid="mock_parent_001", senderType="parent", senderName="某人"
            ),
        )
        assert (m.sender, m.senderName) == ("parent", "家长")

    def test_student_sender_uses_student_name(self, source):
        m = self._fmt(
            source,
            raw_msg(
                1, senderUid="mock_student_001", senderType="student", senderName="忽略"
            ),
        )
        assert (m.sender, m.senderName) == ("student", "测试学生")

    def test_unknown_sender_keeps_server_name(self, source):
        m = self._fmt(
            source,
            raw_msg(
                1, senderUid="teacher_1", senderType="teacher", senderName="王老师"
            ),
        )
        assert (m.sender, m.senderName) == ("unknown", "王老师")

    def test_unknown_sender_without_name_falls_back(self, source):
        m = self._fmt(
            source,
            raw_msg(1, senderUid="teacher_1", senderType="teacher", senderName=""),
        )
        assert m.senderName == "未知"

    def test_millisecond_timestamp_formatted(self, source):
        from datetime import datetime

        m = self._fmt(source, raw_msg(1, createTime=1700000000000))
        assert m.time == datetime.fromtimestamp(1700000000).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    def test_zero_timestamp_becomes_blank(self, source):
        assert self._fmt(source, raw_msg(1, createTime=0)).time == ""

    def test_passes_through_content_type_and_url(self, source):
        m = self._fmt(source, raw_msg(1, type=2, content="", resUrl="http://cdn/a.png"))
        assert (m.type, m.resUrl, m.content) == (2, "http://cdn/a.png", "")


# ============================ 缓存与 mtime 感知 ============================


class TestRefresh:
    def test_missing_file_yields_empty_cache(self, ds):
        assert list(ds._messages) == []

    def test_reads_existing_file(self, chat_file):
        write_chat_history(chat_file, [msg_obj(1), msg_obj(2)])
        assert [m.id for m in MessageDataSource(FakeSession())._messages] == [1, 2]

    def test_reloads_when_file_changes_externally(self, chat_file):
        """感知 main.py 等外部进程对 chat_history.json 的追加写入"""
        ds = MessageDataSource(FakeSession())
        assert list(ds._messages) == []
        write_chat_history(chat_file, [msg_obj(1)])
        assert [m.id for m in ds._messages] == []  # 未 _refresh 前仍是旧缓存

        ds._refresh()
        assert [m.id for m in ds._messages] == [1]

    def test_reuses_cache_when_mtime_unchanged(self, chat_file, monkeypatch):
        write_chat_history(chat_file, [msg_obj(1)])
        calls = []
        original = ms_module.load_chat_history
        monkeypatch.setattr(
            ms_module, "load_chat_history", lambda: (calls.append(1), original())[1]
        )
        ds = MessageDataSource(FakeSession())
        ds._refresh()
        ds._refresh()
        assert len(calls) == 1

    def test_invalidate_forces_reload(self, chat_file, monkeypatch):
        write_chat_history(chat_file, [msg_obj(1)])
        calls = []
        original = ms_module.load_chat_history
        monkeypatch.setattr(
            ms_module, "load_chat_history", lambda: (calls.append(1), original())[1]
        )
        ds = MessageDataSource(FakeSession())
        ds._invalidate()
        ds._refresh()
        assert len(calls) == 2

    def test_file_disappearing_resets_cache(self, chat_file, ds):
        write_chat_history(chat_file, [msg_obj(1)])
        ds._refresh()
        assert len(ds._messages) == 1
        chat_file.unlink()
        ds._refresh()
        assert list(ds._messages) == []


# ============================ 持久化 ============================


class TestPersist:
    def test_formats_and_writes_to_disk(self, ds, chat_file):
        formatted = ds._persist([raw_msg(1), raw_msg(2)])
        assert [m.id for m in formatted] == [1, 2]
        stored = json.loads(chat_file.read_text(encoding="utf-8"))["messages"]
        assert [m["id"] for m in stored] == [1, 2]
        assert stored[0]["sender"] == "student"
        assert stored[0]["senderName"] == "测试学生"

    def test_empty_input_is_noop(self, ds, chat_file):
        assert ds._persist([]) == []
        assert not chat_file.exists()

    def test_cache_is_up_to_date_after_persist(self, ds):
        ds._persist([raw_msg(3)])
        assert [m.id for m in ds._messages] == [3]

    def test_merges_with_existing_without_losing(self, ds, chat_file):
        write_chat_history(chat_file, [msg_obj(5)])
        ds._persist([raw_msg(7)])
        assert [m.id for m in ds._messages] == [5, 7]


# ============================ 对外方法 ============================


class TestLoadLocal:
    def test_pagination_and_total(self, chat_file):
        write_chat_history(chat_file, [msg_obj(i) for i in range(1, 11)])
        ds = MessageDataSource(FakeSession())
        result = ds.load_local(offset=2, limit=3)
        assert result["total"] == 10
        assert result["count"] == 3
        assert [m["id"] for m in result["messages"]] == [3, 4, 5]

    def test_offset_beyond_end(self, chat_file):
        write_chat_history(chat_file, [msg_obj(1)])
        result = MessageDataSource(FakeSession()).load_local(offset=5, limit=5)
        assert result["messages"] == []

    def test_fills_sender_name(self, chat_file, ds):
        write_chat_history(
            chat_file,
            [
                msg_obj(1, sender="student", senderName=""),
                msg_obj(2, sender="parent", senderName=""),
            ],
        )
        result = ds.load_local()
        assert [m["senderName"] for m in result["messages"]] == ["测试学生", "家长"]

    def test_default_page_size(self, chat_file):
        write_chat_history(chat_file, [msg_obj(i) for i in range(1, 80)])
        result = MessageDataSource(FakeSession()).load_local()
        assert result["count"] == 50


class TestFetchLatest:
    def test_calls_upstream_and_returns_sorted(self, chat_file):
        ds, session = _ds_with(pages={1: [raw_msg(9), raw_msg(2), raw_msg(5)]})
        result = ds.fetch_latest(10)
        assert session.stu_msg.get_calls == [(10, 1)]
        assert [m["id"] for m in result["messages"]] == [2, 5, 9]

    def test_does_not_persist(self, chat_file, ds):
        ds, _ = _ds_with(pages={1: [raw_msg(1)]})
        ds.fetch_latest(10)
        assert not chat_file.exists()

    def test_formats_sender(self, ds):
        ds, _ = _ds_with(
            pages={1: [raw_msg(1, senderUid="mock_parent_001", senderType="parent")]}
        )
        assert ds.fetch_latest(10)["messages"][0]["sender"] == "parent"


class TestLoadEarlierFromLocal:
    def test_returns_newest_before_cursor(self, chat_file):
        write_chat_history(chat_file, [msg_obj(i) for i in range(1, 11)])
        result = MessageDataSource(FakeSession()).load_earlier_from_local(
            before_id=8, count=3
        )
        assert [m["id"] for m in result["messages"]] == [5, 6, 7]
        assert result["has_more"] is True

    def test_cursor_is_exclusive(self, chat_file):
        write_chat_history(chat_file, [msg_obj(i) for i in range(1, 5)])
        result = MessageDataSource(FakeSession()).load_earlier_from_local(
            before_id=4, count=10
        )
        assert [m["id"] for m in result["messages"]] == [1, 2, 3]

    def test_has_more_false_at_oldest(self, chat_file):
        write_chat_history(chat_file, [msg_obj(i) for i in range(1, 5)])
        result = MessageDataSource(FakeSession()).load_earlier_from_local(
            before_id=5, count=10
        )
        assert result["has_more"] is False

    def test_empty_cache(self, ds):
        result = ds.load_earlier_from_local(before_id=10, count=5)
        assert (result["messages"], result["has_more"]) == ([], False)

    def test_cursor_below_oldest(self, chat_file):
        write_chat_history(chat_file, [msg_obj(5)])
        result = MessageDataSource(FakeSession()).load_earlier_from_local(
            before_id=5, count=5
        )
        assert result["messages"] == []


class TestSyncAll:
    def test_pages_backwards_until_empty(self, chat_file):
        pages = {
            1: [raw_msg(i) for i in range(90, 101)],
            2: [raw_msg(i) for i in range(80, 90)],
            3: [],
        }
        ds, session = _ds_with(get_impl=lambda count, start: pages.get(start, []))
        result = ds.sync_all(batch_size=10, delay=0)
        assert result["status"] == "ok"
        assert result["synced_count"] == 21
        assert result["total_count"] == 21
        assert [m.id for m in ds._messages] == list(range(80, 101))
        assert [call[1] for call in session.stu_msg.get_calls] == [1, 1, 2, 3]

    def test_stops_when_page_older_than_local_and_not_full(self, chat_file):
        pages = {1: [raw_msg(5)], 2: [raw_msg(1), raw_msg(2)]}
        ds, session = _ds_with(get_impl=lambda count, start: pages.get(start, []))
        ds.sync_all(batch_size=10, delay=0)
        assert [call[1] for call in session.stu_msg.get_calls] == [1, 1, 2]

    def test_skips_ids_already_local(self, chat_file):
        write_chat_history(chat_file, [msg_obj(100)])
        ds, _ = _ds_with(
            get_impl=lambda count, start: {1: [raw_msg(100)], 2: []}.get(start, [])
        )
        assert ds.sync_all(batch_size=10, delay=0)["synced_count"] == 0

    def test_no_sleep_when_delay_zero(self, chat_file, monkeypatch):
        """delay=0 时不增加同步流程自身的等待"""
        slept = []
        monkeypatch.setattr(ms_module.time, "sleep", lambda s: slept.append(s))
        ds, _ = _ds_with(
            get_impl=lambda count, start: {1: [raw_msg(9)], 2: [raw_msg(1)], 3: []}.get(
                start, []
            )
        )
        ds.sync_all(batch_size=1, delay=0)
        # 第 1 页无更旧消息 → 第 2 页全旧且满页 → 第 3 页空；delay=0 时不 sleep
        assert slept == []

    def test_sleeps_when_delay_exceeds_min_interval(self, chat_file, monkeypatch):
        """delay 是同步流程额外增加的等待，与请求层节流相互独立"""
        slept = []
        monkeypatch.setattr(ms_module.time, "sleep", lambda s: slept.append(s))
        ds, _ = _ds_with(
            get_impl=lambda count, start: {1: [raw_msg(9)], 2: [raw_msg(1)]}.get(
                start, []
            )
        )
        ds.sync_all(batch_size=10, delay=2.0)
        assert slept == [2.0]

    def test_empty_upstream(self, chat_file):
        ds, _ = _ds_with(get_impl=lambda count, start: [])
        assert ds.sync_all(batch_size=10, delay=0)["synced_count"] == 0
        assert not chat_file.exists()
