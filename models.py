# -*- coding: utf-8 -*-
"""数据模型层：进程内数据传输用 dataclass，跨进程边界用 dataclasses.asdict 转换。

- RawMessage：希沃原始消息（msg.get 返回的 result 元素），frozen + slots，只读
- Message：格式化后消息（chat_history.json 存储），slots + 可变（load_local 补 senderName）
- MessageResponse：msg.get 的响应包装，frozen + slots，result 为 tuple，真正不可变
"""

from dataclasses import dataclass, field
from enum import IntEnum


def _collect_extra(data: dict, known: set[str]) -> dict:
    """保留未知字段，并让 dataclass round-trip 不把 extra 再嵌套一层。"""
    nested = data.get("extra", {})
    extra = dict(nested) if isinstance(nested, dict) else {}
    extra.update(
        {
            key: value
            for key, value in data.items()
            if key not in known and key != "extra"
        }
    )
    return extra


class SeewoCode(IntEnum):
    """希沃业务状态码（响应体里的 `statusCode` 字段）——状态码语义的唯一约定。

    **与 HTTP 状态码是两套互不相干的码，不得互相比较、互相赋值**：
    - HTTP 状态码：`requests` 的 `response.status_code`，只表示传输层结果（200/429/502…）
    - 希沃业务码：本枚举，表示业务结果（成功 / Token 失效 / 业务拒绝 / 服务器错误）
    """

    OK = 200
    TOKEN_INVALID = -500
    TOKEN_EXPIRED = -505
    BUSINESS_REJECTED = 40000  # 业务校验失败，如留言超 200 字
    SERVER_ERROR = 50000  # 服务器内部错误，如 start<1 触发 SQL 语法错误

    @classmethod
    def is_ok(cls, code: int) -> bool:
        return code == cls.OK

    @classmethod
    def is_invalid(cls, code: int) -> bool:
        """Token 无效（-500），需传入正确的 Token"""
        return code == cls.TOKEN_INVALID

    @classmethod
    def is_expired(cls, code: int) -> bool:
        """Token 过期（-505），需重新登录获取"""
        return code == cls.TOKEN_EXPIRED

    @classmethod
    def is_relogin_code(cls, code: int) -> bool:
        """Token 失效（-500/-505）：服务端未写入业务数据，重新登录后重试是安全的"""
        return cls.is_invalid(code) or cls.is_expired(code)

    @classmethod
    def is_business_reject(cls, code: int) -> bool:
        """业务拒绝（如 40000 超长）：重试也不会成功，不应触发重新登录"""
        return code == cls.BUSINESS_REJECTED


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


class SeewoQrCode(IntEnum):
    """扫码登录的 `statusCode`（仅登录流程使用）。

    `pcCheckQrcode` 接口用 200/201/202 表示扫码进度，与业务码
    （`SeewoCode`）语义不同，不要混用。
    """

    WAITING = 200  # 待扫码
    SCANNED = 201  # 已扫码待确认
    CONFIRMED = 202  # 已确认（登录成功，可写 tokens.json）

    @classmethod
    def is_confirmed(cls, status: int) -> bool:
        return status == cls.CONFIRMED


@dataclass(frozen=True, slots=True)
class MessageResponse:
    """msg.get 经 pxdecode 后的响应包装（含 result 消息列表）。

    frozen + slots，result 为 tuple：容器与内容都是真正不可变，
    消费方需要增删/排序时自行复制（sorted() 等），不改容器。
    """

    statusCode: int = 0
    message: str = ""
    result: tuple[RawMessage, ...] = ()

    @classmethod
    def from_dict(cls, d: dict) -> "MessageResponse":
        return cls(
            statusCode=d.get("statusCode", 0),
            message=d.get("message", ""),
            result=tuple(RawMessage.from_dict(m) for m in d.get("result", [])),
        )


@dataclass(frozen=True, slots=True)
class SendResult:
    """发送类操作的统一结果（msg.send / yunban.send_msg）。

    `http_status` 与 `seewo_code` 是**两套独立的码，分别存放、绝不互相赋值**：
    - `seewo_code`：希沃业务状态码（响应体 `statusCode`），语义见 `SeewoCode`
    - `http_status`：HTTP 传输层状态码；0 表示未取到（如 m-campus 网关只回 JSON）

    `__bool__` 委托 `ok`，使既有 `if result:` 写法保持正确语义。
    """

    ok: bool = False
    seewo_code: int = 0
    http_status: int = 0
    message: str = ""

    def __bool__(self) -> bool:
        return self.ok

    @property
    def needs_relogin(self) -> bool:
        """Token 失效（-500/-505），上层应刷新会话后重发"""
        return SeewoCode.is_relogin_code(self.seewo_code)

    @property
    def is_business_reject(self) -> bool:
        """业务拒绝（如超 200 字），重发无意义"""
        return SeewoCode.is_business_reject(self.seewo_code)

    @classmethod
    def from_seewo_code(
        cls, seewo_code: int, message: str = "", http_status: int = 0
    ) -> "SendResult":
        return cls(
            ok=SeewoCode.is_ok(seewo_code),
            seewo_code=int(seewo_code),
            http_status=int(http_status),
            message=message,
        )

    @classmethod
    def from_http_error(cls, http_status: int, message: str = "") -> "SendResult":
        """只在传输层失败、拿不到业务码时使用（如网关返回 HTML 502）"""
        return cls(
            ok=False, seewo_code=0, http_status=int(http_status), message=message[:200]
        )


# ---------------------------------------------------------------------------
# yunban 数据模型：云班接口的返回结构。
# frozen + slots，只读；from_dict 用 .get 兜底；extra 保留未被建模的原始字段，
# 避免模型化丢失服务端新增/未知字段（跨进程边界用 dataclasses.asdict 转 dict）。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class YunbanClass:
    """云班班级（getclasslist 返回的列表元素）。

    实测完整字段：uid/name/roomUid/roomName/description + schoolUid/schoolName/schoolType；
    多个班可共享同一 roomUid（含空串，表示未绑定实教室）。
    """

    uid: str = ""
    name: str = ""
    roomUid: str = ""
    roomName: str = ""
    description: str = ""
    schoolUid: str = ""
    schoolName: str = ""
    schoolType: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "YunbanClass":
        return cls(
            uid=d.get("uid", ""),
            name=d.get("name", ""),
            roomUid=d.get("roomUid", ""),
            roomName=d.get("roomName", ""),
            description=d.get("description", ""),
            schoolUid=d.get("schoolUid", ""),
            schoolName=d.get("schoolName", ""),
            schoolType=d.get("schoolType", ""),
            extra=_collect_extra(d, cls._known),
        )

    _known = {
        "uid",
        "name",
        "roomUid",
        "roomName",
        "description",
        "schoolUid",
        "schoolName",
        "schoolType",
    }


@dataclass(frozen=True, slots=True)
class YunbanStudent:
    """云班学生（getstulist 返回的列表元素）。

    注意：字段并非每个学生都存在（如 extendCardIds / headImageUrl 可能为空），
    gender 语义：0 未知、1 男、2 女。
    """

    name: str = ""
    sid: str = ""
    uid: str = ""
    classUid: str = ""
    gender: int = 0
    headImageUrl: str = ""
    extendCardIds: list[str] = field(default_factory=list)
    ucPassword: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "YunbanStudent":
        return cls(
            name=d.get("name", ""),
            sid=d.get("sid", ""),
            uid=d.get("uid", ""),
            classUid=d.get("classUid", ""),
            gender=d.get("gender", 0),
            headImageUrl=d.get("headImageUrl", ""),
            extendCardIds=list(d.get("extendCardIds", []) or []),
            ucPassword=d.get("ucPassword", ""),
            extra=_collect_extra(d, cls._known),
        )

    _known = {
        "name",
        "sid",
        "uid",
        "classUid",
        "gender",
        "headImageUrl",
        "extendCardIds",
        "ucPassword",
    }


@dataclass(frozen=True, slots=True)
class YunbanEvent:
    """云班考勤事件（getevents 返回的列表元素）。

    config 是 JSON 字符串，含班牌时段 banPaiConfig(topStartTime/topEndTime)；
    classes 是绑定的班级列表（元素为 {"className", "classUid"}）。
    多班共享同一 roomUid 时事件列表相同。
    """

    name: str = ""
    eventId: str = ""
    eventVersion: int = 0
    memberType: int = 0
    userType: str = ""
    attendanceType: int = 0
    startTime: str = ""
    endTime: str = ""
    overTime: str = ""
    delayMinute: int = 0
    cycleType: int = 0
    cycleContent: str = ""
    config: str = ""
    classes: list = field(default_factory=list)
    schoolCode: str = ""
    creator: str = ""
    classId: str = ""
    className: str = ""
    isRoomBaseOnClass: bool = False
    attendanceStudents: list = field(default_factory=list)
    attendanceTeachers: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "YunbanEvent":
        return cls(
            name=d.get("name", ""),
            eventId=d.get("eventId", ""),
            eventVersion=d.get("eventVersion", 0),
            memberType=d.get("memberType", 0),
            userType=d.get("userType", ""),
            attendanceType=d.get("attendanceType", 0),
            startTime=d.get("startTime", ""),
            endTime=d.get("endTime", ""),
            overTime=d.get("overTime", ""),
            delayMinute=d.get("delayMinute", 0),
            cycleType=d.get("cycleType", 0),
            cycleContent=d.get("cycleContent", ""),
            config=d.get("config", ""),
            classes=list(d.get("classes", []) or []),
            schoolCode=d.get("schoolCode", ""),
            creator=d.get("creator", ""),
            classId=d.get("classId", ""),
            className=d.get("className", ""),
            isRoomBaseOnClass=d.get("isRoomBaseOnClass", False),
            attendanceStudents=list(d.get("attendanceStudents", []) or []),
            attendanceTeachers=list(d.get("attendanceTeachers", []) or []),
            extra=_collect_extra(d, cls._known),
        )

    _known = {
        "name",
        "eventId",
        "eventVersion",
        "memberType",
        "userType",
        "attendanceType",
        "startTime",
        "endTime",
        "overTime",
        "delayMinute",
        "cycleType",
        "cycleContent",
        "config",
        "classes",
        "schoolCode",
        "creator",
        "classId",
        "className",
        "isRoomBaseOnClass",
        "attendanceStudents",
        "attendanceTeachers",
    }


@dataclass(frozen=True, slots=True)
class YunbanParent:
    """云班家长（getparents 返回的列表元素）。

    bindWx 是布尔，表示是否绑定了微信；
    parentShowIndex 是该家长在孩子侧的关系位（第几个家长）。
    """

    parentName: str = ""
    parentPhone: str = ""
    parentUserUid: str = ""
    parentShowIndex: int = 0
    bindWx: bool = False
    notReadNoteCount: int = 0
    tipsMessage: str = ""
    parentHeadImage: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "YunbanParent":
        return cls(
            parentName=d.get("parentName", ""),
            parentPhone=d.get("parentPhone", ""),
            parentUserUid=d.get("parentUserUid", ""),
            parentShowIndex=d.get("parentShowIndex", 0),
            bindWx=d.get("bindWx", False),
            notReadNoteCount=d.get("notReadNoteCount", 0),
            tipsMessage=d.get("tipsMessage", ""),
            parentHeadImage=d.get("parentHeadImage", ""),
            extra=_collect_extra(d, cls._known),
        )

    _known = {
        "parentName",
        "parentPhone",
        "parentUserUid",
        "parentShowIndex",
        "bindWx",
        "notReadNoteCount",
        "tipsMessage",
        "parentHeadImage",
    }


@dataclass(frozen=True, slots=True)
class YunbanNote:
    """云班留言（getnotes 返回的 result 元素）。

    双向往返：sender(某人)→receiver(孩子)、孩子→家长时 sender/receiver 互换；
    status 语义：2 未读、3 已读。多媒体类型（图片/音频等）走 resUrl / voiceUrl。
    字段并非每条都存在，用 .get 兜底。
    """

    id: str = ""
    content: str = ""
    type: int = 0
    status: int = 0
    isFeedback: int = 0
    isIllegal: int = 0
    senderUid: str = ""
    senderName: str = ""
    receiverUid: str = ""
    receiverName: str = ""
    senderHeadImage: str = ""
    receiverHeadImage: str = ""
    classUid: str = ""
    schoolUid: str = ""
    sendTime: int = 0
    createTime: int = 0
    updateTime: int = 0
    resUrl: str = ""
    voiceUrl: str = ""
    voiceLength: int = 0
    resFileKey: str = ""
    replies: list = field(default_factory=list)
    options: list = field(default_factory=list)
    tips: str = ""
    receiverTips: str = ""
    callStatus: int = 0
    callTime: int = 0
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "YunbanNote":
        return cls(
            id=str(d.get("id", "")),
            content=d.get("content", ""),
            type=d.get("type", 0),
            status=d.get("status", 0),
            isFeedback=d.get("isFeedback", 0),
            isIllegal=d.get("isIllegal", 0),
            senderUid=d.get("senderUid", ""),
            senderName=d.get("senderName", ""),
            receiverUid=d.get("receiverUid", ""),
            receiverName=d.get("receiverName", ""),
            senderHeadImage=d.get("senderHeadImage", ""),
            receiverHeadImage=d.get("receiverHeadImage", ""),
            classUid=d.get("classUid", ""),
            schoolUid=d.get("schoolUid", ""),
            sendTime=d.get("sendTime", 0),
            createTime=d.get("createTime", 0),
            updateTime=d.get("updateTime", 0),
            resUrl=d.get("resUrl", ""),
            voiceUrl=d.get("voiceUrl", ""),
            voiceLength=d.get("voiceLength", 0),
            resFileKey=d.get("resFileKey", ""),
            replies=list(d.get("replies", []) or []),
            options=list(d.get("options", []) or []),
            tips=d.get("tips", ""),
            receiverTips=d.get("receiverTips", ""),
            callStatus=d.get("callStatus", 0),
            callTime=d.get("callTime", 0),
            extra=_collect_extra(d, cls._known),
        )

    _known = {
        "id",
        "content",
        "type",
        "status",
        "isFeedback",
        "isIllegal",
        "senderUid",
        "senderName",
        "receiverUid",
        "receiverName",
        "senderHeadImage",
        "receiverHeadImage",
        "classUid",
        "schoolUid",
        "sendTime",
        "createTime",
        "updateTime",
        "resUrl",
        "voiceUrl",
        "voiceLength",
        "resFileKey",
        "replies",
        "options",
        "tips",
        "receiverTips",
        "callStatus",
        "callTime",
    }


@dataclass(frozen=True, slots=True)
class YunbanNotesPage:
    """云班留言分页（getnotes 返回的 data 结构）。"""

    page: int = 0
    pageSize: int = 0
    totalCount: int = 0
    result: tuple[YunbanNote, ...] = ()

    @classmethod
    def from_dict(cls, d: dict) -> "YunbanNotesPage":
        return cls(
            page=d.get("page", 0),
            pageSize=d.get("pageSize", 0),
            totalCount=d.get("totalCount", 0),
            result=tuple(YunbanNote.from_dict(m) for m in d.get("result", [])),
        )
