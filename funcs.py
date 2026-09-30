# -*- coding: utf-8 -*-

import base64
import dataclasses
import json
import time
import os

from init import config, project_path
from models import Message

filedate = time.strftime("%Y-%m-%d", time.localtime())


def encode_json(data: dict):
    return base64.b64encode(json.dumps(data).encode("utf-8")).decode("utf-8")


def pxdecode(data: dict):
    return base64.b64decode(data["data"][7:])


def pxencode(data: dict):
    return {"pxSafeData": f"scData:{encode_json(data)}"}


def read_file(file: str):
    with open(file, "r", encoding="utf-8") as f:
        content = f.read()
        return content


def write_file(file: str, data: bytes) -> bool:
    with open(file, "wb") as f:
        f.write(data)
    return True


def load_json(file: str) -> dict:
    return json.loads(read_file(file))


def datenow() -> str:
    return "[" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + "]: "


def logw(t: str) -> None:
    global filedate
    log = datenow() + t + "\n"
    log_dir = project_path("logs/")
    dirc = log_dir + filedate + ".log"
    if not os.path.isdir(log_dir):
        os.mkdir(log_dir)
    with open(dirc, "a") as file:
        file.write(log)


def chat_log_file() -> str:
    """聊天记录文件路径（mock 模式用独立文件，避免测试数据污染真实记录）。

    每次调用即时读 config.use_mock：配置读后即过期，不能缓存成模块常量，
    否则热重载切换 mock 模式后仍会往旧文件写（污染真实记录）。
    """
    return project_path(
        "chat_history_mock.json" if config.use_mock else "chat_history.json"
    )


class CorruptChatHistoryError(ValueError, KeyError):
    """聊天记录文件存在但无法解析 —— 快速失败，避免被当成空记录覆盖清零。

    这里是**故意不吞异常**：读容错 + 全量覆盖写 会把「文件损坏」当「没有记录」，
    一次 append 就把真实历史整体覆盖。宁可在启动时直接崩掉，让人立刻发现。
    """

    pass


def load_chat_history() -> list[Message]:
    """从文件加载聊天记录（文件不存在视为空记录）。

    Returns:
        消息列表（list[Message]，按文件中的顺序，不保证已排序）

    Raises:
        CorruptChatHistoryError: 文件存在但结构损坏（非法 JSON / 缺 messages / 元素不是对象）
    """
    path = chat_log_file()
    if not os.path.exists(path):
        return []
    try:
        data = load_json(path)
        messages = data["messages"] if isinstance(data, dict) else None
        if not isinstance(messages, list):
            raise ValueError("缺少 messages 列表")
        if not all(isinstance(m, dict) for m in messages):
            raise ValueError("messages 元素不是对象")
        return [Message.from_dict(m) for m in messages]
    except (json.JSONDecodeError, KeyError, ValueError, OSError) as e:
        raise CorruptChatHistoryError(
            f"聊天记录文件无法解析，请修复或删除后重建（sync_all 可重新拉取全量）: {path} → {e}"
        ) from e


def overwrite_chat_history_file(messages: list[Message]) -> None:
    """保存聊天记录到文件（messages: list[Message]）"""
    with open(chat_log_file(), "w", encoding="utf-8") as f:
        json.dump(
            {"messages": [dataclasses.asdict(m) for m in messages]},
            f,
            ensure_ascii=False,
            indent=2,
        )


def append_message(
    msg_id: int,
    content: str,
    msg_type: str = "text",
    sender: str = "",
    sender_name: str = "",
) -> None:
    """追加一条消息到聊天记录

    注意：消息会按ID排序，确保顺序正确（旧→新）
    """
    messages = load_chat_history()
    messages.append(
        Message(
            id=msg_id,
            time=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            content=content,
            type=msg_type,
            sender=sender,
            senderName=sender_name,
        )
    )
    # 按ID排序（旧→新）
    messages.sort(key=lambda m: m.id)
    overwrite_chat_history_file(messages)


def merge_messages(messages: list[Message]) -> None:
    """批量合并消息到聊天记录（用于加载更早的历史消息）

    合并后按 ID 排序，确保顺序为旧→新（与 append_message 行为一致），
    不假设传入 messages 的顺序，也不假设它们与本地已有消息的相对位置。

    **不去重**：与本地已有消息 id 相同的条目会被原样保留（合并后可能出现重复 id），
    调用方需自行保证不重复传入（如 sync_all 会先按已有 id 过滤）。

    Args:
        messages: 待合并的 Message 列表
    """
    if not messages:
        return
    existing = load_chat_history()
    merged = sorted(messages + existing, key=lambda m: m.id)
    overwrite_chat_history_file(merged)
