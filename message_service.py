# -*- coding: utf-8 -*-
"""消息数据源层：独占 chat_history.json 读写 + 格式化 + 内存缓存。

读前检查文件 mtime，变了重新加载（感知 main.py 的 append_message 等外部写入）。
多页聚合（sync_all 翻页循环）在本层；msg.py 只做单次 DAO。
"""
import os
import time
import dataclasses
from datetime import datetime
from typing import Any
from funcs import load_chat_history, merge_messages, CHAT_LOG_FILE
from models import RawMessage, Message
import request_manager


class MessageDataSource:
    """消息数据源层：独占 chat_history.json 读写 + 格式化 + 内存缓存。

    读前检查文件 mtime，变了重新加载（感知 main.py 的 append_message 等外部写入）。
    多页聚合（sync_all 翻页循环）在本层；msg.py 只做单次 DAO。
    """

    def __init__(self, session):
        self._session = session
        self._messages: list[Message] = []
        self._mtime = -1
        self._refresh()

    def _refresh(self):
        """检查文件 mtime，变了就重新加载到缓存"""
        try:
            mtime = os.path.getmtime(CHAT_LOG_FILE)
        except OSError:
            self._messages = []
            self._mtime = -1
            return
        if mtime != self._mtime:
            self._messages = load_chat_history()
            self._mtime = mtime

    def _invalidate(self):
        """写操作后使缓存失效，下次 _refresh 重新加载"""
        self._mtime = -1

    def _format_msg(self, raw: RawMessage, parent_uid, student_uid, student_name) -> Message:
        """格式化单条原始消息（解析时间、判断 sender）

        入参 RawMessage（属性访问），返回 Message 实例。
        """
        create_time = raw.createTime
        time_str = (
            datetime.fromtimestamp(create_time / 1000).strftime("%Y-%m-%d %H:%M:%S")
            if create_time else ""
        )
        sender_uid = raw.senderUid
        if sender_uid == parent_uid:
            sender, sender_name = "parent", "家长"
        elif sender_uid == student_uid:
            sender, sender_name = "student", student_name
        else:
            sender, sender_name = "unknown", raw.senderName or "未知"
        return Message(
            id=raw.id,
            time=time_str,
            content=raw.content,
            type=raw.type,
            sender=sender,
            senderName=sender_name,
            resUrl=raw.resUrl,
        )

    def _persist(self, raw_messages: list[RawMessage]) -> list[Message]:
        """格式化批量 + merge 落盘 + 失效缓存，返回 formatted list[Message]"""
        if not raw_messages:
            return []
        parent_uid = self._session.account.uid
        student_uid = self._session.student.userUid
        student_name = self._session.student.name
        formatted = [self._format_msg(m, parent_uid, student_uid, student_name)
                     for m in raw_messages]
        merge_messages(formatted)
        self._invalidate()
        self._refresh()
        return formatted

    # —— 对外方法（对应 handler）——

    def load_local(self, offset=0, limit=50):
        """读本地缓存，分页，补充 senderName（对应 /api/history）

        返回 dict（messages 字段是 list[dict]，供 jsonify 直接用）。
        Message 可变，补 senderName 时直接 m.senderName = name 赋值。
        """
        self._refresh()
        msgs = sorted(self._messages, key=lambda m: m.id)
        total = len(msgs)
        page = msgs[offset:offset + limit]
        student_name = self._session.student.name if self._session.student else ""
        result = []
        for m in page:
            if not m.senderName:
                s = m.sender
                m.senderName = "家长" if s == "parent" else (
                    student_name if s == "student" else "未知")
            result.append(dataclasses.asdict(m))
        return {"status": "ok", "total": total, "count": len(result), "messages": result}

    def fetch_latest(self, count=10):
        """实时向希沃取最新一页，格式化，不持久化（对应 /api/messages）"""
        raw = self._session.stu_msg.get(count).result
        parent_uid = self._session.account.uid
        student_uid = self._session.student.userUid
        student_name = self._session.student.name
        messages = [self._format_msg(m, parent_uid, student_uid, student_name)
                    for m in raw]
        messages.sort(key=lambda m: m.id)
        return {"status": "ok", "count": len(messages),
                "messages": [dataclasses.asdict(m) for m in messages]}

    def load_earlier_from_local(self, before_id, count=50):
        """从本地缓存读 id < before_id 的更早消息（纯本地，不请求希沃）

        更早历史靠 sync_all 提前全量同步到本地。本地不足时返回 has_more=False。
        对应 /api/load_earlier（语义从"向希沃翻页"改为"本地读"）。

        返回早于 before_id 的消息中**最新**的 count 条（earlier[-count:]），
        保证分页连续无重叠/无间隙：客户端游标=其最早消息 id，每次取刚好更旧的一页。
        """
        self._refresh()
        earlier = [m for m in self._messages if m.id < before_id]
        earlier = sorted(earlier, key=lambda m: m.id)
        has_more = len(earlier) > count
        page = earlier[-count:] if earlier else []
        return {"status": "ok", "has_more": has_more, "count": len(page),
                "messages": [dataclasses.asdict(m) for m in page]}

    def sync_all(self, batch_size=50, delay=2.0):
        """全量同步所有历史到本地（对应 /api/sync_all）

        翻页循环逻辑（搬自原 msg.get_all_messages_until_earliest）：
        从 start=1 起逐页翻（递增往更旧），筛 id<earliest_id，翻到空页停止。
        request_manager 自动节流；delay 参数为全量同步的额外谨慎等待。
        """
        self._refresh()
        existing_ids = {m.id for m in self._messages}
        earliest_id = min(m.id for m in self._messages) if self._messages else 0

        latest = self._session.stu_msg.get(100).result
        if earliest_id == 0 and latest:
            earliest_id = min(m.id for m in latest)

        earlier = []
        if earliest_id > 0:
            start = 1
            while True:
                page = self._session.stu_msg.get(batch_size, start=start).result
                if not page:
                    break
                page_earlier = [m for m in page if m.id < earliest_id]
                earlier.extend(page_earlier)
                # 本页全部 id < earliest_id 且不满页 → 已到最早
                if all(m.id < earliest_id for m in page) and len(page) < batch_size:
                    break
                start += 1
                # delay > request_manager.MIN_INTERVAL 时额外等待（全量同步谨慎防风控）
                if delay > request_manager.MIN_INTERVAL:
                    time.sleep(delay - request_manager.MIN_INTERVAL)

        all_msgs = [m for m in earlier + latest if m.id not in existing_ids]
        formatted = self._persist(all_msgs)
        total_count = len(self._messages)
        return {"status": "ok", "message": "全量同步完成",
                "synced_count": len(formatted), "total_count": total_count}
