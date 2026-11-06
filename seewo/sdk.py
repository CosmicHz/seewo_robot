"""类型化 Python SDK —— 对接 REST v2 接口。

与旧 `client.py` 的 `SeewoClient` 并存：
- `SeewoClient`（v1）：**保持不变**，兼容既有调用方
- `SeewoRobotClient`（本模块）：面向 v2 的类型化客户端

用法::

    from seewo.sdk import SeewoRobotClient

    with SeewoRobotClient("http://localhost:5001", api_key="xxx") as client:
        status = client.status()
        client.send_text("你好")
        for msg in client.messages(count=10):
            print(msg.text)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Self

import requests

from .models import Message, Segment

DEFAULT_TIMEOUT = 30


class SeewoApiError(Exception):
    """SDK 侧的统一异常。

    Attributes:
        code: 稳定的错误码字符串（见 `seewo.interfaces.rest.ApiErrorCode`）
        status: HTTP 状态码
        details: 附加详情
    """

    def __init__(
        self, code: str, message: str, status: int = 0, details: dict | None = None
    ) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    @property
    def status_code(self) -> int:
        """HTTP 状态码（`status` 的别名，与 docs/MIGRATION.md 一致）。"""
        return self.status


@dataclass(frozen=True, slots=True)
class SendResult:
    """发送结果。"""

    ok: bool
    message_id: str = ""
    message: Message | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


class SeewoRobotClient:
    """REST v2 客户端。

    Args:
        base_url: 服务地址，如 ``http://localhost:5001``
        api_key: API Key
        timeout: 单次请求超时（秒）
        session: 可选的 ``requests.Session``（便于复用连接 / 注入测试）
    """

    def __init__(
        self,
        base_url: str = "http://localhost:5001",
        api_key: str = "",
        *,
        timeout: int = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.session = session or requests.Session()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """释放底层连接。"""
        self.session.close()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        """发请求并解包统一响应信封。"""
        url = f"{self.base_url}/api/v2{endpoint}"
        try:
            response = self.session.request(
                method, url, headers=self._headers(), timeout=self.timeout, **kwargs
            )
        except requests.RequestException as e:
            # 网络层失败（连不上 / 超时）也统一成 SeewoApiError，
            # 调用方只需捕获一种异常；原始异常保留在 __cause__。
            raise SeewoApiError("network", f"请求失败: {e}") from e
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            raise SeewoApiError(
                "invalid_response",
                f"服务端返回了非 JSON 响应 (HTTP {response.status_code})",
                status=response.status_code,
            ) from None

        if not isinstance(body, dict) or body.get("status") not in ("ok", "error"):
            # 不是统一信封：不能当成功（例如 `{}` 曾被误判为发送成功）
            raise SeewoApiError(
                "invalid_response",
                f"服务端响应不是统一信封 (HTTP {response.status_code})",
                status=response.status_code,
            )

        if response.status_code >= 400 or body["status"] == "error":
            error = body.get("error")
            if not isinstance(error, dict):
                error = {}
            raise SeewoApiError(
                error.get("code", "internal"),
                error.get("message", "未知错误"),
                status=response.status_code,
                details=error.get("details"),
            )
        return body.get("data")

    # ------------------------------------------------------------------
    # 端点
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """服务状态。"""
        return self._request("GET", "/status")

    def capabilities(self) -> dict[str, Any]:
        """平台能力声明。"""
        return self._request("GET", "/capabilities")

    def messages(self, count: int = 20) -> list[Message]:
        """实时拉取最新消息，返回**新接收**的消息（服务端去重后落入 v2 存储）。"""
        data = self._request("GET", "/messages", params={"count": count})
        return [Message.from_dict(m) for m in (data or {}).get("messages", [])]

    def history(self, limit: int = 50, offset: int = 0) -> list[Message]:
        """读历史消息。"""
        data = self._request(
            "GET", "/history", params={"limit": limit, "offset": offset}
        )
        return [Message.from_dict(m) for m in (data or {}).get("messages", [])]

    def send(
        self,
        segments: list[Segment],
        *,
        conversation_id: str = "",
        reply_to: str = "",
        user_id: str = "",
    ) -> SendResult:
        """发送消息段序列。"""
        payload: dict[str, Any] = {
            "segments": [s.to_dict() for s in segments],
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if reply_to:
            payload["reply_to"] = reply_to
        if user_id:
            payload["user_id"] = user_id
        data = self._request("POST", "/messages", json=payload)
        message = Message.from_dict(data) if isinstance(data, dict) else None
        return SendResult(
            ok=True,
            message_id=message.id if message else "",
            message=message,
            raw=data if isinstance(data, dict) else {},
        )

    def send_text(
        self,
        text: str,
        *,
        conversation_id: str = "",
        reply_to: str = "",
        user_id: str = "",
    ) -> SendResult:
        """发送纯文本。"""
        return self.send(
            [Segment.text_seg(text)],
            conversation_id=conversation_id,
            reply_to=reply_to,
            user_id=user_id,
        )

    def send_image(
        self, url: str, *, conversation_id: str = "", name: str = ""
    ) -> SendResult:
        """发送图片（需传可访问的 URL）。"""
        return self.send(
            [Segment.image(url, name=name)],
            conversation_id=conversation_id,
        )

    def send_audio(
        self, url: str, *, conversation_id: str = "", duration: int = 0
    ) -> SendResult:
        """发送音频。"""
        return self.send(
            [Segment.audio(url, duration=duration)],
            conversation_id=conversation_id,
        )

    def sync(self, limit: int = 50) -> int:
        """触发一次拉取，返回新接收条数。"""
        data = self._request("POST", "/sync", json={"limit": limit})
        return int((data or {}).get("count", 0))

    def connect(self) -> str:
        """连接平台，返回新状态。"""
        data = self._request("POST", "/connection", json={"action": "connect"})
        return (data or {}).get("state", "")

    def disconnect(self) -> str:
        """断开平台连接。"""
        data = self._request("POST", "/connection", json={"action": "disconnect"})
        return (data or {}).get("state", "")


__all__ = ["SeewoApiError", "SeewoRobotClient", "SendResult"]
