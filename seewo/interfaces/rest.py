"""REST v2 接口层：统一请求/响应/错误契约。

设计要点
--------
v1（`/api/*`）保持**逐字段兼容**，不删不改；v2（`/api/v2/*`）是加法，
采用**统一契约**：

**统一响应信封**

- 成功：``{"status": "ok", "data": <payload>}``
- 失败：``{"status": "error", "error": {"code": "...", "message": "..."},
  "data": null}``

**统一错误码**（`ApiErrorCode`）—— 与 HTTP 状态码一一对应，
调用方既可以看 HTTP 码也可以看稳定的字符串码：

=================  ======  ==================================
code               HTTP    含义
=================  ======  ==================================
``ok``             200     成功
``invalid_request``400     请求参数不合法
``unauthorized``   401     API Key 缺失或不匹配
``need_login``     401     会话失效，需重新认证
``not_found``      404     资源不存在
``conflict``       409     状态冲突（如连接状态非法跃迁）
``capability``     501     平台不具备该能力
``upstream``       502     上游平台失败
``timeout``        504     上游超时
``internal``       500     内部错误
=================  ======  ==================================

**版本化**：``/api/v2`` 前缀即版本；v1 与 v2 可长期共存。
"""

from __future__ import annotations

import hmac
import logging
import queue
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..core.service import MessageService
from ..errors import (
    CapabilityError,
    ContractError,
    InvalidStateTransition,
    ProviderAuthError,
    ProviderBusinessError,
    ProviderConnectionError,
    ProviderError,
    ProviderTimeoutError,
    SeewoError,
    SessionExpiredError,
)
from ..models import Segment

logger = logging.getLogger("seewo.interfaces")


class ApiErrorCode(str, Enum):
    """稳定的错误码（字符串），供客户端做分支判断。"""

    OK = "ok"
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    NEED_LOGIN = "need_login"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    CAPABILITY = "capability"
    UPSTREAM = "upstream"
    TIMEOUT = "timeout"
    INTERNAL = "internal"


#: 错误码 → HTTP 状态码
HTTP_STATUS_BY_CODE: dict[ApiErrorCode, int] = {
    ApiErrorCode.OK: 200,
    ApiErrorCode.INVALID_REQUEST: 400,
    ApiErrorCode.UNAUTHORIZED: 401,
    ApiErrorCode.NEED_LOGIN: 401,
    ApiErrorCode.NOT_FOUND: 404,
    ApiErrorCode.CONFLICT: 409,
    ApiErrorCode.CAPABILITY: 501,
    ApiErrorCode.UPSTREAM: 502,
    ApiErrorCode.TIMEOUT: 504,
    ApiErrorCode.INTERNAL: 500,
}


@dataclass(frozen=True, slots=True)
class ApiError:
    """统一错误结构。"""

    code: ApiErrorCode
    message: str
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload


def error_response(error: ApiError):
    """构造 Flask 错误响应。"""
    from flask import jsonify

    body = {
        "status": "error",
        "error": error.to_dict(),
        "data": None,
    }
    return jsonify(body), HTTP_STATUS_BY_CODE[error.code]


def ok_response(data: Any = None):
    """构造 Flask 成功响应。"""
    from flask import jsonify

    return jsonify({"status": "ok", "data": data})


def map_exception(exc: Exception) -> ApiError:
    """把异常映射为统一错误码。

    这是"错误契约统一"的唯一入口 —— 所有端点共用。
    """
    if isinstance(exc, CapabilityError):
        return ApiError(
            ApiErrorCode.CAPABILITY,
            str(exc),
            {"capability": exc.capability},
        )
    if isinstance(exc, SessionExpiredError):
        return ApiError(ApiErrorCode.NEED_LOGIN, str(exc))
    if isinstance(exc, ProviderAuthError):
        return ApiError(ApiErrorCode.NEED_LOGIN, str(exc))
    if isinstance(exc, ProviderTimeoutError):
        return ApiError(ApiErrorCode.TIMEOUT, str(exc))
    if isinstance(exc, ProviderConnectionError):
        return ApiError(ApiErrorCode.UPSTREAM, str(exc))
    if isinstance(exc, ProviderBusinessError):
        return ApiError(ApiErrorCode.UPSTREAM, str(exc))
    if isinstance(exc, ProviderError):
        return ApiError(ApiErrorCode.UPSTREAM, str(exc))
    if isinstance(exc, InvalidStateTransition):
        return ApiError(ApiErrorCode.CONFLICT, str(exc))
    if isinstance(exc, ContractError):
        return ApiError(ApiErrorCode.INVALID_REQUEST, str(exc))
    if isinstance(exc, ValueError):
        return ApiError(ApiErrorCode.INVALID_REQUEST, str(exc))
    return ApiError(ApiErrorCode.INTERNAL, str(exc))


class ApiV2:
    """REST v2 端点集合。

    以 Flask Blueprint 形式挂载到任意 Flask app。
    """

    def __init__(
        self, service: MessageService, api_key: str | Callable[[], str] = ""
    ) -> None:
        self.service = service
        #: 字符串或无参可调用对象。传可调用对象时**每次请求实时读取**，
        #: 这样 `config.json` 热重载换 key 后立即生效（旧 key 立刻失效），
        #: 与 v1 `require_api_key` 每次读 `config.api_key` 的行为一致。
        self._api_key = api_key

    @property
    def api_key(self) -> str:
        """当前生效的 API Key。"""
        key = self._api_key() if callable(self._api_key) else self._api_key
        return key or ""

    # ------------------------------------------------------------------
    # 蓝图
    # ------------------------------------------------------------------

    def blueprint(self, name: str = "api_v2"):
        """构造 Flask Blueprint。"""
        from flask import Blueprint

        bp = Blueprint(name, __name__, url_prefix="/api/v2")
        self._register(bp)
        return bp

    def _register(self, bp) -> None:
        bp.add_url_rule("/status", "status", self.status, methods=["GET"])
        bp.add_url_rule(
            "/capabilities", "capabilities", self.capabilities, methods=["GET"]
        )
        bp.add_url_rule("/messages", "messages", self.messages, methods=["GET"])
        bp.add_url_rule(
            "/messages", "send_message", self.send_message, methods=["POST"]
        )
        bp.add_url_rule("/history", "history", self.history, methods=["GET"])
        bp.add_url_rule("/sync", "sync", self.sync, methods=["POST"])
        bp.add_url_rule("/events", "events", self.events, methods=["GET"])
        bp.add_url_rule("/connection", "connection", self.connection, methods=["POST"])

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------

    def _authorize(self) -> ApiError | None:
        """校验 API Key。未配置 `api_key` 时放行（本地部署场景）。"""
        expected = self.api_key
        if not expected:
            return None
        from flask import request

        key = request.headers.get("X-API-Key") or request.args.get("api_key") or ""
        if not hmac.compare_digest(key.encode(), expected.encode()):
            return ApiError(ApiErrorCode.UNAUTHORIZED, "Invalid API key")
        return None

    # ------------------------------------------------------------------
    # 端点
    # ------------------------------------------------------------------

    def status(self):
        """服务状态：平台、连接、能力、会话。"""
        err = self._authorize()
        if err:
            return error_response(err)
        provider = self.service.provider
        payload = {
            "platform": provider.platform,
            "connection": {
                "state": self.service.state.value,
                "session_expired": self.service.session_expired,
            },
            "capabilities": provider.capabilities.to_dict(),
            "version": "2.0.0",
        }
        describe = getattr(provider, "describe", None)
        if callable(describe):
            payload["provider"] = describe()
        return ok_response(payload)

    def capabilities(self):
        """平台能力声明（供客户端判断哪些操作可用）。"""
        err = self._authorize()
        if err:
            return error_response(err)
        return ok_response(self.service.capabilities.to_dict())

    def messages(self):
        """拉取最新消息（实时，不落库）。

        Query: ``count``（默认 20，上限 200）
        """
        err = self._authorize()
        if err:
            return error_response(err)
        from flask import request

        count = _int_arg(
            request.args.get("count", 20), default=20, minimum=1, maximum=200
        )
        try:
            accepted = self.service.poll_once(limit=count)
        except SeewoError as e:
            return error_response(map_exception(e))
        except Exception as e:
            logger.exception("/api/v2/messages 异常")
            return error_response(map_exception(e))
        return ok_response(
            {
                "count": len(accepted),
                "messages": [m.to_dict() for m in accepted],
            }
        )

    def history(self):
        """读历史消息（有序）。

        Query: ``limit``（默认 50，上限 500）、``offset``
        """
        err = self._authorize()
        if err:
            return error_response(err)
        from flask import request

        limit = _int_arg(
            request.args.get("limit", 50), default=50, minimum=1, maximum=500
        )
        offset = _int_arg(request.args.get("offset", 0), default=0, minimum=0)
        messages = self.service.history(limit=limit, offset=offset)
        return ok_response(
            {
                "count": len(messages),
                "total": len(self.service.store),
                "messages": [m.to_dict() for m in messages],
            }
        )

    def send_message(self):
        """发送消息。

        Body::

            {
              "segments": [{"type": "text", "text": "你好"}],
              "conversation_id": "c1",
              "reply_to": "m1"
            }

        也接受简写 ``{"content": "你好"}``（自动包成文本段）。
        """
        err = self._authorize()
        if err:
            return error_response(err)
        from flask import request

        data = request.get_json(silent=True) or {}
        segments = _parse_segments(data)
        if not segments:
            return error_response(
                ApiError(
                    ApiErrorCode.INVALID_REQUEST,
                    "segments 不能为空（或提供 content 字段）",
                )
            )
        try:
            message = self.service.send(
                segments,
                conversation_id=str(data.get("conversation_id", "") or ""),
                reply_to=str(data.get("reply_to", "") or ""),
                user_id=str(data.get("user_id", "") or ""),
            )
        except SeewoError as e:
            return error_response(map_exception(e))
        except Exception as e:
            logger.exception("/api/v2/messages 发送异常")
            return error_response(map_exception(e))
        return ok_response(message.to_dict())

    def sync(self):
        """触发一次拉取并接收（供外部调度器调用）。"""
        err = self._authorize()
        if err:
            return error_response(err)
        from flask import request

        data = request.get_json(silent=True) or {}
        limit = _int_arg(data.get("limit", 50), default=50, minimum=1, maximum=200)
        try:
            accepted = self.service.poll_once(limit=limit)
        except SeewoError as e:
            return error_response(map_exception(e))
        except Exception as e:
            logger.exception("/api/v2/sync 异常")
            return error_response(map_exception(e))
        return ok_response({"count": len(accepted)})

    def events(self):
        """事件流（Server-Sent Events）。

        把核心事件以 SSE 推给浏览器 / 第三方消费方。
        这是**平台中立**的事件出口 —— 不依赖任何具体框架协议。
        """
        err = self._authorize()
        if err:
            return error_response(err)
        from flask import Response, stream_with_context

        service = self.service
        q: queue.Queue[Any] = queue.Queue(maxsize=256)

        def handler(event):
            # 慢消费方不能阻塞核心派发线程：满了就丢弃最旧的一条
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass

        service.on_any(handler)

        @stream_with_context
        def generate():
            try:
                yield f"data: {event_to_dict_line('connected')}\n\n"
                while True:
                    try:
                        event = q.get(timeout=15)
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {event_to_dict_line(event)}\n\n"
            finally:
                # 连接断开：必须注销 handler，否则 handler 列表会无限增长
                _remove_handler(service, handler)

        return Response(generate(), mimetype="text/event-stream")

    def connection(self):
        """连接控制：``{"action": "connect"|"disconnect"|"reconnect"}``。"""
        err = self._authorize()
        if err:
            return error_response(err)
        from flask import request

        data = request.get_json(silent=True) or {}
        action = str(data.get("action", "")).lower()
        try:
            if action == "connect":
                self.service.connect()
            elif action == "disconnect":
                self.service.disconnect()
            elif action == "reconnect":
                self.service.disconnect()
                self.service.connect()
            else:
                return error_response(
                    ApiError(
                        ApiErrorCode.INVALID_REQUEST,
                        f"未知 action: {action!r}（可选 connect/disconnect/reconnect）",
                    )
                )
        except SeewoError as e:
            return error_response(map_exception(e))
        except Exception as e:
            logger.exception("/api/v2/connection 异常")
            return error_response(map_exception(e))
        return ok_response({"state": self.service.state.value})


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _int_arg(
    raw: Any, *, default: int, minimum: int | None = None, maximum: int | None = None
) -> int:
    """解析整数参数，非法值回落到 default。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


def _parse_segments(data: dict[str, Any]) -> tuple[Segment, ...]:
    """从请求体解析消息段。

    支持两种形态：
    - ``{"segments": [...]}`` —— 完整形态
    - ``{"content": "文本"}`` —— 简写
    """
    raw_segments = data.get("segments")
    if isinstance(raw_segments, list) and raw_segments:
        return tuple(Segment.from_dict(s) for s in raw_segments if isinstance(s, dict))
    content = data.get("content")
    if isinstance(content, str) and content:
        return (Segment.text_seg(content),)
    return ()


def register(
    app, service: MessageService, api_key: str | Callable[[], str] = ""
) -> ApiV2:
    """把 v2 端点挂到 Flask app，返回 `ApiV2` 实例。

    ``api_key`` 传可调用对象（如 ``lambda: config.api_key``）可让热重载生效。
    """
    api = ApiV2(service, api_key=api_key)
    app.register_blueprint(api.blueprint())
    return api


def event_to_dict_line(event: Any) -> str:
    """把事件序列化成 SSE data 行。"""
    import json

    if isinstance(event, str):
        return event
    payload = event.to_dict() if hasattr(event, "to_dict") else {"raw": str(event)}
    return json.dumps(payload, ensure_ascii=False)


def _remove_handler(service: MessageService, handler) -> None:
    """从服务上注销一个事件 handler（线程安全）。

    SSE 连接断开时必须调用，否则 handler 列表会随连接数无限增长。
    """
    service.off_any(handler)


__all__ = [
    "HTTP_STATUS_BY_CODE",
    "ApiError",
    "ApiErrorCode",
    "ApiV2",
    "error_response",
    "ok_response",
    "register",
]
