# -*- coding: utf-8 -*-
"""第 1 层：工具函数（funcs.py）—— px 编解码、聊天记录读写。"""

import dataclasses
import json
import os

import pytest

from conftest import msg_obj, px_wrap
from funcs import (
    CorruptChatHistoryError,
    append_message,
    datenow,
    encode_json,
    load_chat_history,
    load_json,
    merge_messages,
    overwrite_chat_history_file,
    pxdecode,
    pxencode,
    read_file,
    write_file,
)


def test_pxencode_pxdecode_serve_opposite_directions():
    """pxencode 编码请求参数（pxSafeData），pxdecode 解响应体（data 字段），两者不可互换"""
    encoded = pxencode({"a": 1})
    assert "pxSafeData" in encoded
    assert json.loads(pxdecode(px_wrap({"a": 1}))) == {"a": 1}
    with pytest.raises(KeyError):
        pxdecode(encoded)


def test_pxencode_shape():
    assert pxencode({"a": 1}) == {"pxSafeData": "scData:" + encode_json({"a": 1})}


def test_pxdecode_reads_mcampus_response_body():
    """pxdecode 用的正是 {"data": "scData:<base64>"} 形态的响应体"""
    body = px_wrap({"statusCode": 200, "result": [{"id": 1}]})
    assert json.loads(pxdecode(body))["result"] == [{"id": 1}]


def test_write_file_is_binary_and_read_file_is_text(tmp_path):
    bin_path = tmp_path / "blob.bin"
    assert write_file(str(bin_path), b"\x00\x01abc") is True
    assert bin_path.read_bytes() == b"\x00\x01abc"

    text_path = tmp_path / "x.json"
    text_path.write_text('{"a": 1}', encoding="utf-8")
    assert read_file(str(text_path)) == '{"a": 1}'


def test_load_json(tmp_path):
    path = tmp_path / "x.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    assert load_json(str(path)) == {"a": 1}


def test_datenow_format():
    stamp = datenow()
    assert stamp.startswith("[") and stamp.endswith("]: ")
    assert len(stamp) == len("[2026-01-01 00:00:00]: ")


class TestLoadChatHistory:
    def test_missing_file_returns_empty(self, chat_file):
        assert load_chat_history() == []

    def test_broken_json_raises(self, chat_file):
        """损坏文件快速失败：宁可启动就崩，也不能被当成空记录（否则一次 append 清零历史）"""
        chat_file.write_text("{not json", encoding="utf-8")
        with pytest.raises(CorruptChatHistoryError):
            load_chat_history()

    def test_non_dict_json_raises(self, chat_file):
        chat_file.write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(CorruptChatHistoryError):
            load_chat_history()

    def test_missing_messages_key_raises(self, chat_file):
        chat_file.write_text('{"other": 1}', encoding="utf-8")
        with pytest.raises(CorruptChatHistoryError):
            load_chat_history()

    def test_reads_messages(self, chat_file):
        chat_file.write_text(
            json.dumps(
                {
                    "messages": [
                        dataclasses.asdict(msg_obj(1)),
                        dataclasses.asdict(msg_obj(2)),
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        msgs = load_chat_history()
        assert [m.id for m in msgs] == [1, 2]
        assert msgs[0].content == "消息1"


class TestCorruptHistoryProtection:
    """R2：损坏文件一律快速失败 —— 不吞异常、不覆盖，真实历史必须原样留在磁盘上。

    背景：读容错（返回 []）+ 全量覆盖写 会把「文件损坏」当「没有记录」，
    一次 append 就把全部历史覆盖成 1 条新消息。
    """

    def test_broken_json_file_raises_and_keeps_file(self, chat_file):
        original = '{"messages": [{"id": 1, "content": "真实记录"'
        chat_file.write_text(original, encoding="utf-8")
        with pytest.raises(CorruptChatHistoryError):
            append_message(999, "新消息")
        assert chat_file.read_text(encoding="utf-8") == original

    def test_non_list_messages_file_raises_and_keeps_file(self, chat_file):
        original = '{"messages": {"0": {"id": 1}}}'
        chat_file.write_text(original, encoding="utf-8")
        with pytest.raises(CorruptChatHistoryError):
            append_message(999, "新消息")
        assert chat_file.read_text(encoding="utf-8") == original

    def test_non_dict_entry_raises(self, chat_file):
        chat_file.write_text('{"messages": ["not-a-dict"]}', encoding="utf-8")
        with pytest.raises(CorruptChatHistoryError):
            load_chat_history()

    def test_intact_file_is_still_written(self, chat_file):
        """快速失败不能误伤正常路径"""
        append_message(1, "既有")
        append_message(2, "新的")
        assert [m.id for m in load_chat_history()] == [1, 2]

    def test_recovery_after_removing_corrupt_file(self, chat_file):
        """恢复路径：手工删掉损坏文件后，服务可重新启动（再靠 sync_all 拉回全量）"""
        chat_file.write_text("{broken", encoding="utf-8")
        with pytest.raises(CorruptChatHistoryError):
            load_chat_history()
        chat_file.unlink()
        assert load_chat_history() == []
        append_message(1, "重建后第一条")
        assert [m.id for m in load_chat_history()] == [1]


class TestAppendMessage:
    def test_appends_and_keeps_existing(self, chat_file):
        append_message(2, "第二条", "1", "student", "测试学生")
        append_message(1, "第一条", "1", "parent", "家长")
        msgs = load_chat_history()
        assert [m.id for m in msgs] == [1, 2]
        assert msgs[1].content == "第二条"

    def test_sorts_by_id(self, chat_file):
        for mid in (5, 3, 9, 1):
            append_message(mid, f"m{mid}")
        assert [m.id for m in load_chat_history()] == [1, 3, 5, 9]

    def test_fills_time(self, chat_file):
        append_message(1, "x")
        assert load_chat_history()[0].time != ""

    def test_keeps_msg_type_as_given(self, chat_file):
        """main.py 传 str(msg_type)，这里不强制转换"""
        append_message(1, "x", "2", "student", "学生")
        assert load_chat_history()[0].type == "2"

    def test_sender_defaults(self, chat_file):
        append_message(1, "x")
        m = load_chat_history()[0]
        assert (m.sender, m.senderName, m.resUrl) == ("", "", "")


class TestMergeMessages:
    def test_empty_batch_does_not_touch_file(self, chat_file):
        append_message(1, "既有")
        merge_messages([])
        assert [m.id for m in load_chat_history()] == [1]

    def test_merges_and_sorts_regardless_of_input_order(self, chat_file):
        append_message(5, "既有5")
        merge_messages([msg_obj(7), msg_obj(3)])
        assert [m.id for m in load_chat_history()] == [3, 5, 7]

    def test_does_not_deduplicate(self, chat_file):
        """按声明：合并不去重，同 id 重复传入会重复落盘（调用方自行保证不重复）"""
        batch = [msg_obj(1), msg_obj(2)]
        merge_messages(batch)
        merge_messages(batch)
        assert [m.id for m in load_chat_history()] == [1, 1, 2, 2]


def test_overwrite_chat_history_file_format(chat_file):
    overwrite_chat_history_file([msg_obj(1, content="中文")])
    raw = chat_file.read_text(encoding="utf-8")
    assert '"messages"' in raw
    assert "中文" in raw  # ensure_ascii=False
    assert raw.endswith("}") and "\n" in raw  # indent=2


def test_logw_writes_dated_log(tmp_path, monkeypatch):
    import funcs

    # 注意：logw 依赖 project_path("logs/") 保留结尾分隔符（log_dir + date + ".log"）
    monkeypatch.setattr(
        funcs, "project_path", lambda name: os.path.join(str(tmp_path), name)
    )
    funcs.logw("hello")
    logs = list((tmp_path / "logs").glob("*.log"))
    assert len(logs) == 1
    assert "hello" in logs[0].read_text(encoding="utf-8")
