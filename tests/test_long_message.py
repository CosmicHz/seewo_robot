# -*- coding: utf-8 -*-
"""第 1 层：长消息拆分算法（api_server._split_long_message）。

不变量：join(chunks) == 原文（不丢字符）、每块 <= max_len、块数最少。
"""

import logging

import pytest

import api_server
from api_server import MSG_MAX_LEN, _split_long_message


def _chunks(content: str, max_len: int = MSG_MAX_LEN) -> list[str]:
    return _split_long_message(content, max_len)


class TestHardCut:
    def test_short_content_stays_single_chunk(self):
        assert _chunks("短消息") == ["短消息"]

    def test_exactly_max_len_is_single_chunk(self):
        content = "a" * MSG_MAX_LEN
        assert _chunks(content) == [content]

    def test_one_over_max_len_splits_into_two(self):
        content = "a" * (MSG_MAX_LEN + 1)
        chunks = _chunks(content)
        assert [len(c) for c in chunks] == [MSG_MAX_LEN, 1]

    def test_long_content_hard_cuts_every_max_len(self):
        content = "a" * 500
        chunks = _chunks(content)
        assert [len(c) for c in chunks] == [
            MSG_MAX_LEN,
            MSG_MAX_LEN,
            500 - 2 * MSG_MAX_LEN,
        ]

    def test_content_is_lossless(self):
        for content in ("a" * 500, "行\n" * 200, "中文" * 300):
            assert "".join(_chunks(content)) == content

    def test_every_chunk_within_limit(self):
        chunks = _chunks("x" * 1000)
        assert all(len(c) <= MSG_MAX_LEN for c in chunks)


class TestSmartSplit:
    def test_splits_at_last_newline_before_limit(self):
        chunks = _chunks("ab\ncd\nef", max_len=5)
        assert chunks == ["ab", "\ncd", "\nef"]

    def test_newline_moves_to_next_chunk_without_loss(self):
        chunks = _chunks("ab\ncde", max_len=5)
        assert chunks == ["ab", "\ncde"]
        assert "".join(chunks) == "ab\ncde"

    def test_crlf_is_a_split_point(self):
        chunks = _chunks("ab\r\ncd\r\nef", max_len=6)
        assert "".join(chunks) == "ab\r\ncd\r\nef"
        assert all(len(c) <= 6 for c in chunks)
        assert chunks[0] == "ab"

    def test_match_at_position_zero_falls_back_to_hard_cut(self):
        """段首即匹配时不能切出空段，回退硬切"""
        chunks = _chunks("\nabcdefgh", max_len=5)
        assert chunks == ["\nabcd", "efgh"]

    def test_no_match_falls_back_to_hard_cut(self):
        assert _chunks("abcdefgh", max_len=5) == ["abcde", "fgh"]

    def test_respects_each_chunk_limit_with_many_lines(self):
        content = ("测试一\n" * 100).strip()
        chunks = _chunks(content)
        assert "".join(chunks) == content
        assert all(len(c) <= MSG_MAX_LEN for c in chunks)


class TestPatternConfig:
    def test_empty_pattern_disables_smart_split(self, monkeypatch):
        """禁用智能拆分后换行符退化为普通字符，按 5 字硬切"""
        monkeypatch.setattr(api_server.config, "long_message_split_pattern", "")
        assert _chunks("ab\ncd\nef", max_len=5) == ["ab\ncd", "\nef"]

    def test_custom_pattern(self, monkeypatch):
        """以逗号拆分：匹配字符同样跟到下一段开头"""
        monkeypatch.setattr(api_server.config, "long_message_split_pattern", ",")
        assert _chunks("ab,cd,ef", max_len=4) == ["ab", ",cd", ",ef"]

    def test_invalid_pattern_falls_back_to_hard_cut(self, monkeypatch, caplog):
        monkeypatch.setattr(api_server.config, "long_message_split_pattern", "(")
        with caplog.at_level(logging.WARNING):
            assert _chunks("ab\ncd\nef", max_len=5) == ["ab\ncd", "\nef"]
        assert "回退硬切" in caplog.text

    def test_pattern_is_read_per_call_for_hot_reload(self, monkeypatch):
        """每次调用即时编译，热重载改 pattern 后下一次调用即生效"""
        monkeypatch.setattr(api_server.config, "long_message_split_pattern", r"\r?\n")
        assert _chunks("ab\ncde", max_len=5) == ["ab", "\ncde"]
        monkeypatch.setattr(api_server.config, "long_message_split_pattern", "")
        assert _chunks("ab\ncde", max_len=5) == ["ab\ncd", "e"]


def test_max_len_constant_keeps_room_for_ellipsis():
    """196 + "..." = 199 < 200，保留 1 字余量（服务器硬上限 200）"""
    assert MSG_MAX_LEN == 199


@pytest.mark.parametrize("content", ["", "\n", "a", "a" * 198, "a" * 199])
def test_short_inputs_are_single_chunk(content):
    assert _chunks(content) == ([content] if content else [])
