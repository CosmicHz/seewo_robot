# -*- coding: utf-8 -*-
"""数据模型层：进程内数据传输用 dataclass，跨进程边界用 dataclasses.asdict 转换。

- RawMessage：希沃原始消息（msg.get 返回的 result 元素），frozen + slots，只读
- Message：格式化后消息（chat_history.json 存储），slots + 可变（load_local 补 senderName）
- MessageResponse：msg.get 的响应包装，frozen + slots，容器只读但 result list 可变
"""

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


@dataclass(slots=True)  # 注意：不 frozen
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
            type=d.get("type", 1),  # 不强制 int()，保留文件原始类型
            sender=d.get("sender", "unknown"),
            senderName=d.get("senderName", ""),
            resUrl=d.get("resUrl", ""),
        )


@dataclass(slots=True)  # 可变：支持 reload_config 原地更新，供热重载
class Config:
    """config.json 的本地数据模型。

    mutable + slots，便于 reload_config() 原地 setattr 更新而保持
    `from init import config` 消费方仍引用同一对象（从而实现热重载）。

    typed 字段覆盖项目已知配置项；未建模的原始键收集到 extra 原样保留，
    避免模型化丢失用户自定义的未知配置项。banPaiConfig 这类嵌套业务配置
    单独建模为 ban_pai_config dict 字段。
    """

    api_key: str = "your-secret-key"
    api_port: int = 5001
    api_host: str = "0.0.0.0"
    poll_batch_size: int = 50
    base_interval: int = 1
    max_interval: int = 10
    max_errors: int = 5
    use_mock: bool = False
    mock_port: int = 9000
    long_message_strategy: str = "truncate"
    long_message_split_pattern: str = r"\r?\n"
    log_level: str = "INFO"
    ban_pai_config: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)
    # 由于字典是可变的，不能直接作为默认值

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        cfg = cls()
        for field_name in cfg.__dataclass_fields__:
            if field_name == "extra":
                continue
            json_key = "banPaiConfig" if field_name == "ban_pai_config" else field_name
            if json_key in d:
                setattr(cfg, field_name, d[json_key])
        known = {"banPaiConfig", *cfg.__dataclass_fields__}
        cfg.extra = {k: v for k, v in d.items() if k not in known}
        return cfg


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
