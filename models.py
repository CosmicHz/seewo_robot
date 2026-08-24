# -*- coding: utf-8 -*-
"""数据模型层：进程内数据传输用 dataclass，跨进程边界用 dataclasses.asdict 转换。

- RawMessage：希沃原始消息（msg.get 返回的 result 元素），frozen + slots，只读
- Message：格式化后消息（chat_history.json 存储），slots + 可变（load_local 补 senderName）
- MessageResponse：msg.get 的响应包装，frozen + slots，容器只读但 result list 可变
"""
import dataclasses
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RawMessage:
    """希沃原始消息（msg.get 返回的 result 列表元素）。

    frozen + slots：只读，从 from_dict 构造后不再改。
    字段缺失用默认值兜底，对齐 main.py 的 .get("senderType", "unknown") / .get("type", 1) 等。
    """
    id: int = 0
    senderUid: str = ""
    senderType: str = "unknown"
    senderName: str = ""
    type: int = 1
    content: str = ""
    resUrl: str = ""
    voiceLength: int = 0
    resConfig: str = ""
    createTime: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "RawMessage":
        return cls(
            id=int(d.get("id", 0)),
            senderUid=d.get("senderUid", ""),
            senderType=d.get("senderType", "unknown"),
            senderName=d.get("senderName", ""),
            type=d.get("type", 1),
            content=d.get("content", ""),
            resUrl=d.get("resUrl", ""),
            voiceLength=d.get("voiceLength", 0),
            resConfig=d.get("resConfig", ""),
            createTime=d.get("createTime", 0),
        )


@dataclass(slots=True)            # 注意：不 frozen
class Message:
    """格式化后消息（message_service._format_msg 产出 / chat_history.json 存储）。

    mutable：load_local 补 senderName 时可直接 m.senderName = name 赋值，
    无需 dataclasses.replace 重建。
    type 字段标注 int|str：保留 main.py 写 str / message_service 写 int 的既有混合行为。
    """
    id: int = 0
    time: str = ""
    content: str = ""
    type: int | str = 1
    sender: str = "unknown"
    senderName: str = ""
    resUrl: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        return cls(
            id=int(d.get("id", 0)),
            time=d.get("time", ""),
            content=d.get("content", ""),
            type=d.get("type", 1),       # 不强制 int()，保留文件原始类型
            sender=d.get("sender", "unknown"),
            senderName=d.get("senderName", ""),
            resUrl=d.get("resUrl", ""),
        )


@dataclass(frozen=True, slots=True)
class MessageResponse:
    """msg.get 经 pxdecode 后的响应包装（含 result 消息列表）。

    frozen + slots：容器本身只读；result 是 list，list 自身可变，
    需要 append/extend 时直接操作 list，不必改容器。
    """
    statusCode: int = 0
    message: str = ""
    result: list[RawMessage] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "MessageResponse":
        return cls(
            statusCode=d.get("statusCode", 0),
            message=d.get("message", ""),
            result=[RawMessage.from_dict(m) for m in d.get("result", [])],
        )
