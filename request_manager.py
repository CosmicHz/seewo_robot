# -*- coding: utf-8 -*-
"""统一请求层：所有对希沃的 HTTP 请求经此调度。

职责：
- 节流（最小请求间隔，防并发触发风控）
- HTTP 429 退避重试框架
- 队列化接口预留（本次不启 worker，当前同步执行 + _lock 串行化节流点）

后续可引入 queue.Queue + worker 线程串行消费所有请求，调用方接口不变。
"""

import time
import threading
import logging
from init import verify
import requests

logger = logging.getLogger("seewo.request")

_lock = threading.Lock()
_last_request_time = 0.0
# 最小请求间隔(秒)：全局保底节流，防并发触发风控
MIN_INTERVAL = 0.5
# 队列化预留：后续可引入 queue.Queue + worker 线程串行消费所有请求
# 当前同步执行 + 节流；多线程调用时靠 _lock 串行化节流点
# _request_queue = queue.Queue()
# _worker_started = False


def _throttle():
    """请求前节流：距上次请求不足 MIN_INTERVAL 则 sleep（线程安全）"""
    global _last_request_time
    with _lock:
        now = time.monotonic()
        wait = MIN_INTERVAL - (now - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.monotonic()


def post(url, headers, data):
    """统一 POST 入口：节流 + HTTP 风控检测框架。

    当前：节流后同步 POST，检测 HTTP 429 退避重试（最多 3 次，指数退避）。
    预留：后续可改为丢队列由 worker 串行消费，调用方接口不变。
    """
    for attempt in range(3):
        _throttle()
        resp = requests.post(url, headers=headers, data=data, verify=verify)
        if resp.status_code != 429:
            return resp
        backoff = 2**attempt
        logger.warning("HTTP 429 风控，退避 %ds 重试(attempt=%d)", backoff, attempt)
        time.sleep(backoff)
    return resp  # 重试用尽，返回最后一次响应
