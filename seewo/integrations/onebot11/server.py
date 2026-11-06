"""OneBot 11 的 HTTP + WebSocket 服务端。

暴露两类端点：

**HTTP（OneBot 11 标准 API）**
    ``POST {prefix}/{action}``，body 为标准参数。
    例：``POST /onebot/v11/send_msg``

**WebSocket（OneBot 11 正向 WS）**
    ``GET {ws_prefix}`` —— 客户端连上来后收到 ``meta_event/lifecycle``
    与后续的 ``message`` 事件。

这两个端点是**同一个适配器**的两种接入方式，业务逻辑完全共用。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from ...integrations.base import IntegrationConfig
from ...integrations.onebot11.adapter import OneBot11Adapter

logger = logging.getLogger("seewo.integration.onebot11.server")


class OneBot11Server:
    """OneBot 11 HTTP/WS 服务端。

    Args:
        adapter: 已配置好的适配器
        app: 可选的 Flask app；提供时把 HTTP 端点挂上去
    """

    def __init__(
        self,
        adapter: OneBot11Adapter,
        app: Any = None,
        *,
        ws_path: str = "/onebot/v11/ws",
    ) -> None:
        self.adapter = adapter
        self.app = app
        self.ws_path = ws_path
        self._sock_server = None
        self._thread: threading.Thread | None = None
        if app is not None:
            self._register_http(app)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _register_http(self, app: Any) -> None:
        """把 OneBot 11 API 挂到 Flask app 上。"""
        from flask import jsonify, request

        prefix = self.adapter.api_prefix
        adapter = self.adapter

        @app.route(f"{prefix}/<action>", methods=["POST"])
        def onebot_action(action: str):
            params = request.get_json(silent=True) or {}
            result = adapter.handle_api(action, params)
            return jsonify(result)

        @app.route(f"{prefix}/get_status", methods=["GET"])
        def onebot_status():
            return jsonify(adapter.handle_api("get_status", {}))

    def handle_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """直接调用适配器（不经 HTTP，便于测试）。"""
        return self.adapter.handle_api(action, params)

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    def attach_websocket(self, sock_server: Any) -> None:
        """把 WebSocket 端点挂到 flask-sock 的 SockServer 上。

        Args:
            sock_server: `flask_sock.SockServer` 实例
        """
        adapter = self.adapter

        @sock_server.route(self.ws_path)
        def onebot_ws(ws):  # pragma: no cover - 需要真实 WS 客户端
            adapter.register_connection(ws)
            try:
                # OneBot 11 正向 WS：连上即下发 lifecycle
                ws.send(adapter.build_lifecycle("connect"))
                while True:
                    message = ws.receive()
                    if message is None:
                        break
                    _handle_ws_frame(adapter, ws, message)
            except Exception as e:
                logger.debug("OneBot WS 连接结束: %s", e)
            finally:
                adapter.unregister_connection(ws)

    def start_ws(self, app: Any, host: str, port: int) -> None:
        """用 flask-sock 启动 WS 服务（阻塞）。"""
        from flask_sock import SockServer

        self._sock_server = SockServer(app)
        self.attach_websocket(self._sock_server)
        self._sock_server.run(host=host, port=port)

    def stop(self) -> None:
        """停止服务。"""
        self.adapter.stop()


def _handle_ws_frame(adapter: OneBot11Adapter, ws: Any, raw: str) -> None:
    """处理客户端 WS 帧。

    OneBot 11 的正向 WS 中，客户端可以发送 API 调用请求
    （``{"action": "send_msg", "params": {...}, "echo": "..."}``）。
    """
    import json

    try:
        frame = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(frame, dict):
        return
    action = frame.get("action")
    if not action:
        return
    params = frame.get("params") or {}
    result = adapter.handle_api(action, params)
    response: dict[str, Any] = {"status": result.get("status", "ok")}
    if "retcode" in result:
        response["retcode"] = result["retcode"]
    if "data" in result:
        response["data"] = result["data"]
    if "msg" in result:
        response["msg"] = result["msg"]
    if "echo" in frame:
        response["echo"] = frame["echo"]
    ws.send(response)


__all__ = ["IntegrationConfig", "OneBot11Server"]
