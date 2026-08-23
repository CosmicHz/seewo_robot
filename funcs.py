# -*- coding: utf-8 -*-

import base64
import json
import time
import os

from init import _use_mock

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
    log_dir = "logs/"
    dirc = log_dir + filedate + ".log"
    if not os.path.isdir(log_dir):
        os.mkdir(log_dir)
    with open(dirc, "a") as file:
        file.write(log)


# 聊天记录存储（mock 模式用独立文件，避免测试数据污染真实记录）
CHAT_LOG_FILE = "chat_history_mock.json" if _use_mock else "chat_history.json"


def load_chat_history() -> dict[str, any]:
    """从文件加载聊天记录。

    Returns:
        聊天记录数据，形式为`{'messages': list}`"""
    if os.path.exists(CHAT_LOG_FILE):
        try:
            data = load_json(CHAT_LOG_FILE)
            if not isinstance(data, dict) or "messages" not in data:
                raise ValueError("invalid chat history")
            return data
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    return {"messages": []}


def overwrite_chat_history_file(history: dict[str, any]) -> None:
    """保存聊天记录到文件"""
    with open(CHAT_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def append_message(
    msg_id: int, content: str, msg_type: str = "text", sender: str = "", sender_name: str = ""
) -> None:
    """追加一条消息到聊天记录

    注意：消息会按ID排序，确保顺序正确（旧→新）
    """
    history = load_chat_history()

    # 添加新消息
    new_msg = {
        "id": msg_id,
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "content": content,
        "type": msg_type,
        "sender": sender,
        "senderName": sender_name,
    }
    history["messages"].append(new_msg)

    # 按ID排序（旧→新）
    history["messages"] = sorted(history["messages"], key=lambda m: m["id"])

    overwrite_chat_history_file(history)


def merge_messages(messages: list) -> None:
    """批量合并消息到聊天记录（用于加载更早的历史消息）

    合并后按 ID 排序，确保顺序为旧→新（与 append_message 行为一致），
    不假设传入 messages 的顺序，也不假设它们与本地已有消息的相对位置。

    Args:
        messages: 待合并的消息列表，每条需含 "id" 字段
    """
    if not messages:
        return
    history = load_chat_history()
    # 合并后按ID排序，确保顺序正确（旧→新）
    history["messages"] = sorted(
        messages + history["messages"], key=lambda m: m["id"]
    )
    overwrite_chat_history_file(history)
