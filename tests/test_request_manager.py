# -*- coding: utf-8 -*-
"""第 1 层：统一请求层（request_manager.py）—— 节流与 429 退避重试。"""

import threading
import time as real_time

import pytest

import request_manager

URL = "https://m-campus.seewo.com/class/apis.json?action=X"


class FakeClock:
    """替换 request_manager 模块内的 time 引用，让节流/退避可断言"""

    def __init__(self, now=1000.0, advance=True):
        self.now = now
        self.advance = advance
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.advance:
            self.now += seconds

    @property
    def backoffs(self):
        """退避睡眠（节流是 0.5 的整数倍密集出现，退避是 1/2/4）"""
        return sorted({s for s in self.sleeps if s >= 1})


@pytest.fixture
def rm(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(request_manager, "time", clock)
    monkeypatch.setattr(request_manager, "_last_request_time", 0.0)
    monkeypatch.setattr(request_manager, "MIN_INTERVAL", 0.5)
    return clock


class TestThrottle:
    def test_first_call_does_not_sleep(self, rm):
        request_manager._throttle()
        assert rm.sleeps == []

    def test_immediate_second_call_waits_min_interval(self, rm):
        request_manager._throttle()
        request_manager._throttle()
        assert rm.sleeps == [0.5]

    def test_sleeps_only_the_remaining_gap(self, rm):
        request_manager._throttle()
        rm.now += 0.2
        request_manager._throttle()
        assert rm.sleeps == [pytest.approx(0.3)]

    def test_no_sleep_when_interval_already_elapsed(self, rm):
        request_manager._throttle()
        rm.now += 0.5
        request_manager._throttle()
        assert rm.sleeps == []

    def test_records_last_request_time(self, rm):
        request_manager._throttle()
        assert request_manager._last_request_time == rm.now

    def test_serializes_concurrent_callers(self):
        """节流点被锁串行化：3 个线程并发也必须拉开间隔"""
        request_manager.MIN_INTERVAL = 0.05
        started = real_time.monotonic()
        threads = [threading.Thread(target=request_manager._throttle) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert real_time.monotonic() - started >= 0.1


class TestPost:
    def _add(self, rs, status, count=1, body="{}"):
        for _ in range(count):
            rs.add(rs.POST, URL, body=body, status=status)

    def test_returns_response_without_retry(self, rm, http):
        self._add(http, 200, body='{"statusCode": 200}')
        resp = request_manager.post(URL, headers={}, data="{}")
        assert resp.status_code == 200
        assert len(http.calls) == 1
        assert rm.backoffs == []

    def test_retries_after_429_with_exponential_backoff(self, rm, http):
        self._add(http, 429, count=2)
        self._add(http, 200, body='{"statusCode": 200}')
        resp = request_manager.post(URL, headers={}, data="{}")
        assert resp.status_code == 200
        assert len(http.calls) == 3
        assert rm.backoffs == [1, 2]

    def test_returns_last_response_when_retries_exhausted(self, rm, http):
        self._add(http, 429, count=3)
        resp = request_manager.post(URL, headers={}, data="{}")
        assert resp.status_code == 429
        assert len(http.calls) == 3
        assert rm.backoffs == [1, 2, 4]

    def test_throttles_each_attempt(self, monkeypatch, http):
        """每次尝试前都过 _throttle（含重试）"""
        clock = FakeClock(advance=False)
        monkeypatch.setattr(request_manager, "time", clock)
        monkeypatch.setattr(request_manager, "_last_request_time", 0.0)
        monkeypatch.setattr(request_manager, "MIN_INTERVAL", 0.5)
        self._add(http, 429, count=3)

        request_manager.post(URL, headers={}, data="{}")

        assert clock.sleeps.count(0.5) == 2  # 第 1 次不等待，后续 2 次各等一个间隔
        assert clock.backoffs == [1, 2, 4]

    def test_sends_request_as_post_with_payload(self, rm, http):
        self._add(http, 200)
        request_manager.post(URL, headers={"X-Api-Key": "k"}, data='{"a": 1}')
        call = http.calls[0]
        assert call.request.method == "POST"
        assert call.request.headers["X-Api-Key"] == "k"
        assert call.request.body == '{"a": 1}'
