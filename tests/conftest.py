# -*- coding: utf-8 -*-
"""tests 共享夹具：把「真实世界边界」全部替换为可控的假实现。

隔离四原则
1. 文件隔离：聊天记录 / 上传台账 / 二维码 / Token 一律改指 tmp_path，绝不写真实状态文件
2. 出网隔离：responses 在 requests 适配器层拦截 HTTP（request_manager 与裸 requests 都覆盖），
   未注册的 URL 直接抛 ConnectionError，防止用例误连真实希沃
3. 会话隔离：api_server 的全局 session / datasource 换成假对象
4. 配置隔离：强制固定关键配置值，用例结果不依赖本机 config.json 内容

分层对应关系（详见各用例文件）：
- 第 1 层 纯单元：不 import api_server，只测 models / funcs / 算法
- 第 2 层 端点级：app.test_client() + 假 session + responses 拦截 HTTP
- 第 3 层 数据源层：真实 MessageDataSource + 临时 chat_history 文件
"""

import base64
import copy
import json

import pytest
import responses as responses_lib

import funcs
import init
from models import Message, MessageResponse, RawMessage, SendResult

# —— 生产端点（用例强制 use_mock=False，由 responses 在适配器层拦截）——
M_CAMPUS_API = "https://m-campus.seewo.com/class/apis.json"
CAMPUS_BASE = "https://campus.seewo.com"
YUNBAN_BASE = "https://campus.seewo.com/mis-cloud-route-server"
ID_BASE = "https://id.seewo.com"

TEST_API_KEY = "test-key"


# ============================ 构造 / 解析辅助 ============================


def mcampus_url(action: str) -> str:
    """m-campus 统一接口 URL（靠 action 区分接口）"""
    return f"{M_CAMPUS_API}?action={action}"


def px_wrap(payload: dict) -> dict:
    """把业务 JSON 包成 m-campus 响应体 {"data": "scData:<base64>"}"""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return {"data": "scData:" + base64.b64encode(raw).decode("ascii")}


def decode_action_request(body) -> tuple[str, dict]:
    """解析 api().action 发出的请求体，返回 (action, params)，params 自动解 pxSafeData"""
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    data = json.loads(body)
    params = data.get("params", {})
    if isinstance(params, dict) and "pxSafeData" in params:
        params = json.loads(base64.b64decode(params["pxSafeData"][7:]))
    return data.get("action", ""), params


def raw_msg(mid: int = 1, **kw) -> RawMessage:
    """构造一条希沃原始消息（默认：学生发的文本）"""
    base = {
        "id": mid,
        "senderUid": "mock_student_001",
        "senderType": "student",
        "senderName": "测试学生",
        "type": 1,
        "content": f"消息{mid}",
        "createTime": 1700000000000,
    }
    base.update(kw)
    return RawMessage.from_dict(base)


def msg_obj(mid: int = 1, **kw) -> Message:
    """构造一条已格式化消息（chat_history.json 存储形态）"""
    base = {
        "id": mid,
        "time": "2024-01-01 00:00:00",
        "content": f"消息{mid}",
        "type": 1,
        "sender": "student",
        "senderName": "测试学生",
    }
    base.update(kw)
    return Message.from_dict(base)


def write_chat_history(path, messages: list[Message]) -> None:
    """按 funcs 的存储格式写 chat_history（{"messages": [...]}）"""
    import dataclasses

    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"messages": [dataclasses.asdict(m) for m in messages]},
            f,
            ensure_ascii=False,
        )


# ============================ 假对象（会话 / DAO）============================


class FakeAccount:
    def __init__(self, uid="mock_parent_001", token_expired=False):
        self.uid = uid
        self.token_expired = token_expired
        self.headers = {"cookie": "x-token=fake"}
        self.mheaders = {"cookie": "x-token=fake"}


class FakeStudent:
    def __init__(self, name="测试学生", userUid="mock_student_001"):
        self.name = name
        self.userUid = userUid
        self.schoolUid = "mock_school_001"
        self.classUid = "mock_class_001"


class FakeStuMsg:
    """假留言 DAO：记录调用，按 start（1-based 页码）返回预设页"""

    def __init__(
        self, pages: dict | None = None, get_impl=None, send_result=None, send_impl=None
    ):
        self.pages = pages or {}
        self.get_impl = get_impl
        self.send_impl = send_impl
        self.send_result = send_result or SendResult(
            ok=True, seewo_code=200, message="发送成功"
        )
        self.get_calls: list[tuple] = []
        self.sent: list[dict] = []

    def get(self, count: int, start: int = 1) -> MessageResponse:
        self.get_calls.append((count, start))
        if self.get_impl is not None:
            return MessageResponse(
                statusCode=200, result=list(self.get_impl(count, start))
            )
        return MessageResponse(statusCode=200, result=list(self.pages.get(start, [])))

    def send(self, content, type, resUrl="", voiceLength=0, resConfig=""):
        self.sent.append(
            {
                "content": content,
                "type": type,
                "resUrl": resUrl,
                "voiceLength": voiceLength,
                "resConfig": resConfig,
            }
        )
        if self.send_impl is not None:
            return self.send_impl(len(self.sent), content, type)
        return self.send_result


class FakeSession:
    """假会话：模拟 api_server.Session 的最小对外契约"""

    def __init__(self, account=None, student=None, stu_msg=None):
        self.account = account or FakeAccount()
        self.student = student or FakeStudent()
        self.stu_msg = stu_msg or FakeStuMsg()
        self._initialized = True

    @property
    def needs_login(self):
        return self.account is not None and self.account.token_expired

    def init(self):
        return self

    def refresh(self):
        return self


# ============================ 夹具 ============================


@pytest.fixture(autouse=True)
def _pin_config(monkeypatch):
    """固定关键配置 + 强制走生产端点（由 responses 拦截），隔离本机 config.json"""
    monkeypatch.setattr(init.config, "use_mock", False)
    monkeypatch.setattr(init.config, "api_key", TEST_API_KEY)
    monkeypatch.setattr(init.config, "long_message_strategy", "truncate")
    monkeypatch.setattr(init.config, "long_message_split_pattern", r"\r?\n")
    monkeypatch.setattr(init.config, "poll_batch_size", 50)
    monkeypatch.setattr(init.config, "base_interval", 1)
    monkeypatch.setattr(init.config, "max_interval", 10)
    monkeypatch.setattr(init.config, "max_errors", 5)
    # urls() 读的是 init 模块级 _use_mock（与 config.use_mock 两个来源），必须一起固定
    monkeypatch.setattr(init, "_use_mock", False)
    # reload_config() 会重建这两个派生全局，登记为「测后还原原值」
    monkeypatch.setattr(init, "_mock_port", init._mock_port)
    monkeypatch.setattr(init, "_mock_base", init._mock_base)


@pytest.fixture(autouse=True)
def _restore_config():
    """reload_config() 会原地改全局 config 并重设日志级别，用例结束后整体还原"""
    import logging

    snapshot = copy.deepcopy(
        {f: getattr(init.config, f) for f in init.config.__dataclass_fields__}
    )
    yield
    for key, value in snapshot.items():
        setattr(init.config, key, value)
    # 按还原后的 log_level 重设级别（与 api_server._apply_log_level 同效），避免 DEBUG 泄漏到后续用例
    level = getattr(logging, init.config.log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(level)
    logging.getLogger("seewo.server").setLevel(level)


@pytest.fixture
def chat_file(tmp_path, monkeypatch):
    """把聊天记录改指临时文件（唯一来源是 funcs.chat_log_file()）"""
    path = tmp_path / "chat_history.json"
    monkeypatch.setattr(funcs, "chat_log_file", lambda: str(path))
    return path


@pytest.fixture
def uploads_ledger(tmp_path, monkeypatch):
    """把上传台账改指临时文件"""
    path = tmp_path / "uploads.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(init, "uploads_file", str(path))
    import upload

    monkeypatch.setattr(upload, "uploads_file", str(path))
    return path


@pytest.fixture
def login_files(tmp_path, monkeypatch):
    """把二维码 / Token 改指临时文件"""
    import login

    qr = tmp_path / "qrcode.png"
    token = tmp_path / "tokens.json"
    monkeypatch.setattr(login, "qrcode_file", str(qr))
    monkeypatch.setattr(login, "token_file", str(token))
    return {"qrcode": qr, "token": token}


@pytest.fixture
def http():
    """responses 打桩器：在 requests 适配器层拦截全部 HTTP"""
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rs:
        yield rs


@pytest.fixture
def auth_headers():
    return {"X-API-Key": TEST_API_KEY}


@pytest.fixture
def app_env(monkeypatch, chat_file, uploads_ledger, login_files):
    """端点级用例环境：api_server 的全局 session / datasource 换成隔离的假对象。

    返回 (flask test_client, FakeSession)
    """
    import api_server
    from message_service import MessageDataSource

    fake = FakeSession()
    monkeypatch.setattr(api_server, "session", fake)
    monkeypatch.setattr(api_server, "datasource", MessageDataSource(fake))
    monkeypatch.setattr(api_server, "qrcode_file", str(login_files["qrcode"]))
    monkeypatch.setattr(api_server, "token_file", str(login_files["token"]))
    return api_server.app.test_client(), fake
