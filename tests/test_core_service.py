"""核心服务层（seewo/core/service.py + store.py）的正反测试。

本文件是「可靠性要求」的主要守卫，覆盖：
- 消息去重（内存 LRU + Store 双重）
- 消息顺序（`(timestamp, id)` 复合键）
- 断线恢复（游标持久化 + 续拉）
- 并发（多线程入站不重复不丢失，事件序稳定）
- 状态一致性（连接状态机拒绝非法跃迁）
- 错误传播（Provider 异常 → ERROR 事件 + 上抛，不吞）
- 认证失效自愈（刷新后重试一次；仍失效则 SessionExpiredError）
- 能力缺口（不支持的段抛 CapabilityError，不假装成功）
"""

import json
import os
import pathlib
import threading

import pytest

from seewo.core.service import MessageService
from seewo.core.store import JsonStore, MemoryStore
from seewo.errors import (
    CapabilityError,
    CorruptStoreError,
    InvalidStateTransition,
    ProviderAuthError,
    ProviderBusinessError,
    ProviderConnectionError,
    SessionExpiredError,
)
from seewo.models import (
    ConnectionState,
    EventType,
    Message,
    PlatformCapabilities,
    Segment,
)
from seewo.testing.fake_provider import FakeProvider


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def service(provider):
    svc = MessageService(provider, MemoryStore())
    svc.connect()
    return svc


def _events(svc):
    collected = []
    svc.on_any(collected.append)
    return collected


# ============================ 去重 ============================


class TestDeduplication:
    def test_same_id_received_twice_yields_one_message(self, service, provider):
        provider.emit_text("hello", message_id="m1")
        first = service.poll_once()
        assert len(first) == 1

        provider.emit_text("hello", message_id="m1")
        second = service.poll_once()
        assert len(second) == 0, "重复消息必须被去重"

    def test_duplicate_does_not_emit_second_event(self, service, provider):
        events = _events(service)
        provider.emit_text("a", message_id="m1")
        service.poll_once()
        provider.emit_text("a", message_id="m1")
        service.poll_once()
        message_events = [e for e in events if e.type is EventType.MESSAGE]
        assert len(message_events) == 1

    def test_dedup_survives_restart_via_store(self, provider):
        """Store 里已有该 id 时，即使内存 LRU 空也应去重"""
        store = MemoryStore()
        svc1 = MessageService(provider, store)
        svc1.connect()
        provider.emit_text("a", message_id="m1")
        svc1.poll_once()

        # 换一个新的 service（模拟进程重启，LRU 内存清空）
        svc2 = MessageService(provider, store)
        svc2.connect()
        provider.emit_text("a", message_id="m1")
        assert len(svc2.poll_once()) == 0

    def test_distinct_ids_both_accepted(self, service, provider):
        provider.emit_text("a", message_id="m1")
        provider.emit_text("b", message_id="m2")
        assert len(service.poll_once()) == 2

    def test_message_without_id_is_dropped(self, service, provider):
        provider.emit(Message(id="", segments=(Segment.text_seg("x"),)))
        assert service.poll_once() == ()

    def test_seen_lru_is_bounded(self, provider):
        svc = MessageService(provider, MemoryStore(), seen_capacity=10)
        svc.connect()
        for i in range(50):
            provider.emit_text(f"m{i}", message_id=f"id{i}")
            svc.poll_once()
        assert len(svc._seen) <= 10


# ============================ 顺序 ============================


class TestOrdering:
    def test_out_of_order_arrival_is_sorted(self, service, provider):
        provider.emit_text("c", message_id="m3", timestamp=3000)
        provider.emit_text("a", message_id="m1", timestamp=1000)
        provider.emit_text("b", message_id="m2", timestamp=2000)
        accepted = service.poll_once()
        assert [m.id for m in accepted] == ["m1", "m2", "m3"]

    def test_same_timestamp_broken_by_id(self, service, provider):
        provider.emit_text("b", message_id="mB", timestamp=1000)
        provider.emit_text("a", message_id="mA", timestamp=1000)
        accepted = service.poll_once()
        assert [m.id for m in accepted] == ["mA", "mB"]

    def test_history_is_ordered_across_batches(self, service, provider):
        provider.emit_text("late", message_id="m9", timestamp=9000)
        service.poll_once()
        provider.emit_text("early", message_id="m1", timestamp=1000)
        service.poll_once()
        history = service.history()
        assert [m.id for m in history] == ["m1", "m9"]

    def test_store_keeps_sorted_order(self, provider):
        store = MemoryStore()
        svc = MessageService(provider, store)
        svc.connect()
        provider.emit_text("b", message_id="m2", timestamp=2000)
        svc.poll_once()
        provider.emit_text("a", message_id="m1", timestamp=1000)
        svc.poll_once()
        assert [m.id for m in store.load()] == ["m1", "m2"]


# ============================ 断线恢复 ============================


class TestCursorResume:
    def test_cursor_is_persisted(self, service, provider):
        provider.emit_text("a", message_id="m1")
        service.poll_once()
        assert service.store.last_cursor() is not None

    def test_messages_during_disconnect_are_picked_up(self, service, provider):
        """断连期间到达的消息，重连后能补到"""
        provider.emit_text("before", message_id="m1")
        service.poll_once()
        cursor = service.store.last_cursor()

        # 断连期间上游积压
        provider.emit_text("during", message_id="m2")

        # 重连后带游标拉取
        result = provider.poll(cursor=cursor, limit=50)
        assert [m.id for m in result.messages] == ["m2"]

    def test_resume_does_not_redeliver_already_seen(self, service, provider):
        provider.emit_text("a", message_id="m1")
        service.poll_once()

        # 用旧游标重新拉：m1 已消费过，不应重复
        result = provider.poll(cursor="0", limit=50)
        assert "m1" in [m.id for m in result.messages]
        # 但核心层仍会因 Store 已有该 id 而去重
        accepted = service.ingest(result.messages)
        assert [m.id for m in accepted] == []

    def test_cursor_survives_service_restart_with_persistent_store(
        self, tmp_path, provider
    ):
        path = str(tmp_path / "chat.json")
        store = JsonStore(path, cursor_path=path + ".cursor")
        svc1 = MessageService(provider, store)
        svc1.connect()
        provider.emit_text("a", message_id="m1")
        svc1.poll_once()
        cursor = store.last_cursor()
        assert cursor is not None

        store2 = JsonStore(path, cursor_path=path + ".cursor")
        svc2 = MessageService(provider, store2)
        svc2.connect()
        assert store2.last_cursor() == cursor


# ============================ 并发 ============================


class TestConcurrency:
    def test_concurrent_ingest_has_no_duplicates_and_no_loss(self, service, provider):
        """N 个线程各投递不同 id 的消息 → 全部收到，无重复"""
        total = 100
        threads = []
        for i in range(total):
            provider.emit_text(f"m{i}", message_id=f"id{i}", timestamp=1000 + i)

        def worker(start, end):
            for i in range(start, end):
                service.ingest(
                    [
                        Message(
                            id=f"id{i}",
                            platform="fake",
                            timestamp=1000 + i,
                            segments=(Segment.text_seg(f"m{i}"),),
                        )
                    ]
                )

        for t in range(10):
            thread = threading.Thread(target=worker, args=(t * 10, (t + 1) * 10))
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()

        history = service.history(limit=0)
        assert len(history) == total
        assert len({m.id for m in history}) == total, "不得出现重复消息"

    def test_concurrent_duplicate_delivery_yields_one(self, service):
        """多线程同时投递同一 id → 只入一次"""
        barrier = threading.Barrier(8)
        results = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            accepted = service.ingest(
                [
                    Message(
                        id="same",
                        platform="fake",
                        timestamp=1,
                        segments=(Segment.text_seg("x"),),
                    )
                ]
            )
            with lock:
                results.append(len(accepted))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 1, "同一 id 只能被接收一次"
        assert len(service.history()) == 1

    def test_event_order_is_stable(self, service, provider):
        """事件派发顺序与消息顺序一致"""
        events = _events(service)
        for i in range(20):
            provider.emit_text(f"m{i}", message_id=f"id{i}", timestamp=1000 + i)
        service.poll_once()
        ids = [e.message.id for e in events if e.type is EventType.MESSAGE]
        assert ids == sorted(ids, key=lambda i: int(i[2:]))

    def test_poll_is_serialised(self, service, provider):
        """#30：并发 poll 时 Provider 不会被**同时**调用（检测重叠，不只数次数）"""
        import time

        active = 0
        max_active = 0
        guard = threading.Lock()
        calls = []
        original = provider.poll

        def tracking_poll(cursor=None, limit=50):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)  # 给其它线程插入的机会
            try:
                calls.append(1)
                return original(cursor=cursor, limit=limit)
            finally:
                with guard:
                    active -= 1

        provider.poll = tracking_poll
        threads = [threading.Thread(target=service.poll_once) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(calls) == 5
        assert max_active == 1

    def test_concurrent_ingest_dispatches_once(self, service):
        """#14：多个线程同时 ingest 同一 id，只接收并派发一次"""
        events = _events(service)
        barrier = threading.Barrier(8)
        results = []

        def worker():
            barrier.wait()
            results.append(
                service.ingest([Message(id="dup", platform="fake", timestamp=1)])
            )

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(len(r) for r in results) == 1
        assert len([e for e in events if e.type is EventType.MESSAGE]) == 1


# ============================ 状态机 ============================


class TestStateMachine:
    def test_initial_state_is_disconnected(self, provider):
        svc = MessageService(provider, MemoryStore())
        assert svc.state is ConnectionState.DISCONNECTED

    def test_connect_moves_to_connected(self, service):
        assert service.state is ConnectionState.CONNECTED

    def test_disconnect_moves_to_disconnected(self, service):
        service.disconnect()
        assert service.state is ConnectionState.DISCONNECTED

    def test_illegal_transition_is_rejected(self, service):
        service.disconnect()
        # disconnected → connected 不允许（必须先 connecting）
        with pytest.raises(InvalidStateTransition):
            service.provider._transition(ConnectionState.CONNECTED)

    def test_connect_emits_connected_event(self, service):
        svc = MessageService(FakeProvider(), MemoryStore())
        events = []
        svc.on_any(events.append)
        svc.connect()
        assert any(e.type is EventType.CONNECTED for e in events)

    def test_disconnect_emits_disconnected_event(self, service):
        events = _events(service)
        service.disconnect()
        assert any(e.type is EventType.DISCONNECTED for e in events)


# ============================ 错误传播 ============================


class TestErrorPropagation:
    def test_poll_error_raises_and_emits_error_event(self, service, provider):
        events = _events(service)
        provider.fail_next_poll(ProviderConnectionError("网络断了"))
        with pytest.raises(ProviderConnectionError):
            service.poll_once()
        assert any(e.type is EventType.ERROR for e in events)

    def test_poll_auth_error_refreshes_and_retries_once(self, service, provider):
        """#15：拉取遇认证失效 → 刷新凭证 → 用原游标重试一次（与 send 对称）"""
        provider.emit_text("a", message_id="m1")
        provider.fail_next_poll(ProviderAuthError("凭证失效"))
        accepted = service.poll_once()
        assert [m.id for m in accepted] == ["m1"]
        assert provider.refresh_calls == 1
        assert service.session_expired is False

    def test_poll_auth_error_with_failed_refresh_raises_session_expired(
        self, service, provider
    ):
        events = _events(service)
        provider.expire_auth()
        provider.fail_next_poll(ProviderAuthError("凭证失效"))
        with pytest.raises(SessionExpiredError):
            service.poll_once()
        assert service.session_expired is True
        errors = [e for e in events if e.type is EventType.ERROR]
        assert len(errors) == 1
        assert errors[0].data.get("auth_expired") is True

    def test_poll_auth_error_twice_raises_session_expired(self, service, provider):
        """刷新成功但重试仍认证失效 → 只重试一次，不循环"""
        original = provider.poll
        calls = []

        def always_auth_fail(cursor=None, limit=50):
            calls.append(1)
            raise ProviderAuthError("仍失效")

        provider.poll = always_auth_fail
        with pytest.raises(SessionExpiredError):
            service.poll_once()
        assert len(calls) == 2
        provider.poll = original

    def test_poll_forever_emits_one_error_per_failure(self, service, provider):
        """#16：一次拉取失败只派发一个 ERROR 事件"""
        events = _events(service)
        provider.fail_next_poll(ProviderConnectionError("网络断了"))
        with pytest.raises(ProviderConnectionError):
            service.poll_forever(interval=0, max_consecutive_errors=1)
        assert len([e for e in events if e.type is EventType.ERROR]) == 1

    def test_retry_business_failure_emits_message_failed(self, service, provider):
        """#17：刷新后重试遇非认证失败，也要派发 MESSAGE_FAILED"""
        events = _events(service)
        original = provider._send
        state = {"n": 0}

        def flaky(outgoing):
            state["n"] += 1
            if state["n"] == 1:
                raise ProviderAuthError("失效")
            raise ProviderBusinessError("被拒")

        provider._send = flaky
        with pytest.raises(ProviderBusinessError):
            service.send([Segment.text_seg("x")])
        failed = [e for e in events if e.type is EventType.MESSAGE_FAILED]
        assert len(failed) == 1
        assert "被拒" in failed[0].error
        provider._send = original

    def test_send_error_raises_and_emits_failed_event(self, service, provider):
        events = _events(service)
        provider.fail_next_send(ProviderBusinessError("内容超长"))
        with pytest.raises(ProviderBusinessError):
            service.send([Segment.text_seg("x")])
        assert any(e.type is EventType.MESSAGE_FAILED for e in events)

    def test_handler_exception_does_not_break_dispatch(self, service, provider):
        """一个 handler 抛异常不应阻断其它 handler"""
        seen = []

        def bad_handler(event):
            raise RuntimeError("boom")

        service.on(EventType.MESSAGE, bad_handler)
        service.on(EventType.MESSAGE, seen.append)
        provider.emit_text("a", message_id="m1")
        service.poll_once()
        assert any(e.message and e.message.id == "m1" for e in seen)


# ============================ 认证自愈 ============================


class TestAuthRecovery:
    def test_auth_failure_triggers_refresh_and_one_retry(self, service, provider):
        """第一次发送遇认证失效 → 刷新凭证 → 重试一次成功"""
        calls = {"n": 0}

        def flaky_send(outgoing):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ProviderAuthError("凭证失效")
            return Message(id="ok", platform="fake")

        provider.send = flaky_send
        result = service.send([Segment.text_seg("hi")])
        assert result.id == "ok"
        assert provider.refresh_calls == 1
        assert calls["n"] == 2

    def test_auth_failure_after_refresh_raises_session_expired(self, service, provider):
        """刷新后仍认证失效 → SessionExpiredError（不无限重试）"""

        def always_fail(outgoing):
            raise ProviderAuthError("凭证失效")

        provider.send = always_fail
        provider.expire_auth()  # 刷新也失败
        with pytest.raises(SessionExpiredError):
            service.send([Segment.text_seg("hi")])

    def test_session_expired_flag_cleared_after_success(self, service, provider):
        """一次认证失效 → 刷新 → 重试成功 → 标志位被清除"""
        provider.fail_next_send(ProviderAuthError("x"))
        # 首次发送遇认证失效，核心层自动刷新并重试成功（不抛异常）
        service.send([Segment.text_seg("hi")])
        assert provider.refresh_calls == 1
        assert service.session_expired is False

    def test_session_expired_stays_true_when_refresh_fails(self, service, provider):
        """刷新失败 → 抛 SessionExpiredError 且标志位保持为 True"""
        provider.expire_auth()
        provider.fail_next_send(ProviderAuthError("x"))
        with pytest.raises(SessionExpiredError):
            service.send([Segment.text_seg("hi")])
        assert service.session_expired is True


# ============================ 能力缺口 ============================


class TestCapabilityGaps:
    def test_unsupported_segment_is_rejected(self, provider):
        caps = PlatformCapabilities(send_text=True, send_image=False)
        provider.capabilities = caps
        svc = MessageService(provider, MemoryStore())
        svc.connect()
        with pytest.raises(CapabilityError) as exc:
            svc.send([Segment.image("http://x/a.png")])
        assert exc.value.capability == "send_image"

    def test_unsupported_segment_is_not_sent(self, provider):
        provider.capabilities = PlatformCapabilities(send_text=True)
        svc = MessageService(provider, MemoryStore())
        svc.connect()
        with pytest.raises(CapabilityError):
            svc.send([Segment.image("http://x/a.png")])
        assert provider.sent == (), "能力不足时不得发送"

    def test_text_length_limit_is_enforced(self, provider):
        provider.capabilities = PlatformCapabilities(send_text=True, max_text_length=10)
        svc = MessageService(provider, MemoryStore())
        svc.connect()
        with pytest.raises(CapabilityError) as exc:
            svc.send([Segment.text_seg("x" * 20)])
        assert exc.value.capability == "max_text_length"

    def test_provider_recall_raises_when_unsupported(self, service, provider):
        provider.capabilities = PlatformCapabilities(send_text=True)
        with pytest.raises(CapabilityError):
            provider.recall("m1")

    def test_provider_history_raises_when_unsupported(self, provider):
        """基类的 history 在平台未声明 supports_history 时抛 CapabilityError"""
        from seewo.provider.base import Provider

        class Minimal(Provider):
            platform = "minimal"

            def poll(self, cursor=None, limit=50):
                from seewo.provider.base import PollResult

                return PollResult()

            def _send(self, outgoing):
                from seewo.models import Message

                return Message(id="x", platform="minimal")

        minimal = Minimal()
        minimal.capabilities = PlatformCapabilities(send_text=True)
        with pytest.raises(CapabilityError):
            minimal.history()


# ============================ Store ============================


class TestStore:
    def test_memory_store_dedups_by_id(self):
        store = MemoryStore()
        m = Message(id="m1", platform="fake", timestamp=1)
        assert store.append([m]) == 1
        assert store.append([m]) == 0
        assert len(store.load()) == 1

    def test_memory_store_pagination(self):
        store = MemoryStore()
        store.append(
            [Message(id=f"m{i}", platform="f", timestamp=i) for i in range(10)]
        )
        assert [m.id for m in store.load(limit=3, offset=2)] == ["m2", "m3", "m4"]

    def test_json_store_roundtrip(self, tmp_path):
        path = str(tmp_path / "chat.json")
        store = JsonStore(path)
        store.append([Message(id="m1", platform="f", timestamp=1)])
        raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        assert raw["messages"][0]["id"] == "m1"

    def test_json_store_refuses_v1_file_and_leaves_it_untouched(self, tmp_path):
        """误把 v1 `chat_history.json` 交给 JsonStore → 拒绝加载，且不改写原文件。

        回归（B1）：此前 v1 记录被读成 `segments=()`（内容丢失），
        一次 append 就把整份文件写成 v2 结构，v1 再读全是空内容。
        """
        path = tmp_path / "chat.json"
        original = json.dumps(
            {
                "messages": [
                    {
                        "id": 1,
                        "time": "2024-01-01 00:00:00",
                        "content": "旧消息",
                        "type": 1,
                        "sender": "student",
                        "senderName": "学生",
                        "resUrl": "",
                    }
                ]
            },
            ensure_ascii=False,
        )
        path.write_text(original, encoding="utf-8")
        store = JsonStore(str(path))
        with pytest.raises(CorruptStoreError):
            store.load()
        with pytest.raises(CorruptStoreError):
            store.append([Message(id="2", platform="f", timestamp=1)])
        assert path.read_text(encoding="utf-8") == original

    def test_json_store_roundtrip_preserves_content(self, tmp_path):
        """v2 结构写入后重新打开，内容与发送者都在（不只是 id）。"""
        path = str(tmp_path / "chat.v2.json")
        JsonStore(path).append(
            [
                Message(
                    id="m1",
                    platform="f",
                    user_id="u1",
                    segments=(
                        Segment.text_seg("你好"),
                        Segment.image("http://x/a.png"),
                    ),
                    timestamp=1,
                )
            ]
        )
        loaded = JsonStore(path).load()
        assert len(loaded) == 1
        assert loaded[0].user_id == "u1"
        assert loaded[0].segments[0].text == "你好"
        assert loaded[0].segments[1].url == "http://x/a.png"

    def test_concurrent_load_and_append_do_not_crash(self, tmp_path):
        """HTTP 线程读历史 + 轮询线程写入并发发生时不应报错（B9）。"""
        import threading

        store = JsonStore(str(tmp_path / "chat.v2.json"))
        errors: list[BaseException] = []

        def writer():
            try:
                for i in range(200):
                    store.append([Message(id=f"m{i}", platform="f", timestamp=i)])
            except BaseException as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(200):
                    store.load()
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(store.load()) == 200

    def test_json_store_corrupt_file_raises(self, tmp_path):
        """损坏文件必须抛错（不吞），避免把损坏当空记录覆盖历史"""
        path = tmp_path / "chat.json"
        path.write_text("{broken", encoding="utf-8")
        with pytest.raises(CorruptStoreError):
            JsonStore(str(path)).load()

    def test_json_store_missing_messages_key_raises(self, tmp_path):
        path = tmp_path / "chat.json"
        path.write_text('{"foo": 1}', encoding="utf-8")
        with pytest.raises(CorruptStoreError):
            JsonStore(str(path)).load()

    def test_json_store_cursor_persistence(self, tmp_path):
        cursor_path = str(tmp_path / "chat.cursor")
        store = JsonStore(str(tmp_path / "chat.json"), cursor_path=cursor_path)
        assert store.last_cursor() is None
        store.save_cursor("42")
        assert store.last_cursor() == "42"

    def test_json_store_write_is_atomic(self, tmp_path):
        """写盘用 os.replace：中途失败不会留下半个文件"""
        path = tmp_path / "chat.json"
        store = JsonStore(str(path))
        store.append([Message(id="m1", platform="f", timestamp=1)])
        # 目录里不应残留临时文件
        leftovers = [p for p in os.listdir(tmp_path) if p.startswith(".tmp-")]
        assert leftovers == []

    def test_store_reload_discards_cache(self, tmp_path):
        path = str(tmp_path / "chat.json")
        store = JsonStore(path)
        store.append([Message(id="m1", platform="f", timestamp=1)])
        store.reload()
        assert len(store.load()) == 1
