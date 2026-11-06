"""Satori 的 HTTP + WebSocket 服务端。

暴露：

**事件 WebSocket**
    ``GET {config.path}``（默认 ``/v1/events``）
    —— 客户端连上后必须在 10 秒内发 ``IDENTIFY``（op=3），
    服务端回 ``READY``（op=4）；随后客户端每 10 秒发 ``PING``（op=1），
    服务端回 ``PONG``（op=2）。

**HTTP API**
    ``POST {api_prefix}/{resource}.{method}``，如
    ``POST /satori/v1/message.create``。

**Satori 规范 URL 前缀**
    ``/{platform}/{account}/satori/v1/...`` —— 兼容规范的调用方会带上
    这段前缀，本服务同时接受带前缀与不带前缀两种形式。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from ...integrations.base import IntegrationConfig
from ...integrations.satori.adapter import SatoriAdapter

logger = logging.getLogger("seewo.integration.satori.server")


class SatoriServer:
    """Satori HTTP/WS 服务端。"""

    def __init__(
        self,
        adapter: SatoriAdapter,
        app: Any = None,
        *,
        ws_path: str | None = None,
    ) -> None:
        self.adapter = adapter
        self.app = app
        self.ws_path = ws_path or adapter.config.path
        self._sock_server = None
        self._thread: threading.Thread | None = None
        if app is not None:
            self._register_http(app)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _register_http(self, app: Any) -> None:
        """把 Satori API 挂到 Flask app 上。

        接受两种路径：
        - ``/satori/v1/message.create``
        - ``/{platform}/{account}/satori/v1/message.create``（Satori 规范前缀）
        """
        from flask import jsonify, request

        adapter = self.adapter
        base = adapter.api_prefix

        @app.route(
            f"{base}/<action>",
            methods=["POST"],
        )
        @app.route(
            f"/<platform>/<account>{base}/<action>",
            methods=["POST"],
        )
        def satori_action(action: str, platform: str = "", account: str = ""):
            params = request.get_json(silent=True) or {}
            result = adapter.handle_api(action, params)
            if "error" in result:
                return jsonify(result), 400
            return jsonify(result)

    def handle_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """直接调用适配器（不经 HTTP，便于测试）。"""
        return self.adapter.handle_api(action, params)

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    def attach_websocket(self, sock_server: Any) -> None:
        """把 `/v1/events` 端点挂到 flask-sock 上。"""
        adapter = self.adapter
        ws_path = self.ws_path

        @sock_server.route(ws_path)
        def satori_events(ws):  # pragma: no cover - 需要真实 WS 客户端
            adapter.register_connection(ws)
            try:
                while True:
                    raw = ws.receive()
                    if raw is None:
                        break
                    try:
                        frames = adapter.handle_signal(raw)
                    except Exception as e:
                        logger.warning("Satori 信令帧处理失败: %s", e)
                        continue
                    for frame in frames:
                        ws.send(frame)
            except Exception as e:
                logger.debug("Satori WS 连接结束: %s", e)
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


__all__ = ["IntegrationConfig", "SatoriServer"]
