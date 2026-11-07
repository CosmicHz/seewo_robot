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
    这段前缀，本服务的 **HTTP API** 同时接受带前缀与不带前缀两种形式
    （事件 WS 只有 ``{config.path}`` 一个地址）。

**鉴权**
    配置了令牌时：HTTP API 校验 ``Authorization: Bearer <token>``（不符 → 401）；
    事件 WS 校验 IDENTIFY 帧里的 ``token``，通过后才开始推送事件，
    不通过则回一个 error 事件帧后断开。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from ...integrations.base import IntegrationConfig, request_token
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
            # Satori HTTP API 鉴权：`Authorization: Bearer <token>`（不接受 query 参数）
            if not adapter.check_token(request_token(request, allow_query=False)):
                return jsonify({"error": "Unauthorized"}), 401
            params = request.get_json(silent=True)
            if params is None:
                params = {}
            if not isinstance(params, dict):
                return jsonify({"error": "请求体必须是 JSON 对象"}), 400
            result = adapter.handle_api(action, params)
            # 成功结果可能是 None（如 message.delete）、数组或对象；
            # 只有「对象且含 error 键」才是失败。
            if isinstance(result, dict) and "error" in result:
                return jsonify(result), 400
            return jsonify(result if result is not None else {})

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
            # IDENTIFY 通过之前**不**登记进广播集合 —— 未认证的连接收不到任何事件。
            identified = False
            try:
                while True:
                    # 文档承诺 IDENTIFY 必须在 10 秒内完成：未认证前带超时等待，
                    # 超时（返回 None）即关闭，避免未认证连接长期占用线程。
                    raw = ws.receive(timeout=None if identified else 10.0)
                    if raw is None:
                        if not identified:
                            logger.warning(
                                "Satori WS 未在超时内完成 IDENTIFY，关闭连接"
                            )
                        break
                    try:
                        result = adapter.process_signal(raw)
                    except Exception as e:
                        logger.warning("Satori 信令帧处理失败: %s", e)
                        continue
                    for frame in result.frames:
                        ws.send(frame)
                    if result.rejected:
                        logger.warning("Satori IDENTIFY 鉴权失败，关闭连接")
                        break
                    if result.identified and not identified:
                        identified = True
                        adapter.register_connection(ws)
            except Exception as e:
                logger.debug("Satori WS 连接结束: %s", e)
            finally:
                adapter.unregister_connection(ws)
                adapter._safe_close(ws)

    def start_ws(self, app: Any, host: str, port: int) -> None:
        """挂载 WS 端点并用 Flask 开发服务器运行（阻塞）。

        flask-sock 只在 `Sock(app)` 上注册路由，没有独立的 `SockServer`；
        `api_server.py` 走的是同一条路径（HTTP 与 WS 共用端口）。
        """
        from flask_sock import Sock

        self._sock_server = Sock(app)
        self.attach_websocket(self._sock_server)
        app.run(host=host, port=port)

    def stop(self) -> None:
        """停止服务。"""
        self.adapter.stop()


__all__ = ["IntegrationConfig", "SatoriServer"]
