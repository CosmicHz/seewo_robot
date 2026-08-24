# -*- coding: utf-8 -*-
"""
希沃班牌机器人 API 服务端
提供 REST API 接口供客户端调用
"""

import os
import re
import json
import time
import base64
import logging
import threading
from flask import Flask, request, jsonify
from functools import wraps

from init import qrcode_file, token_file, config
from login import acc, download_qrcode, check_qrcode
from funcs import write_file
from stu import stu
from msg import msg
from upload import Upload
from message_service import MessageDataSource

app = Flask(__name__)

# 日志配置
LOG_LEVEL = getattr(logging, config.get("log_level", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("seewo.server")

# 静默 Flask/Werkzeug 默认日志
logging.getLogger("werkzeug").setLevel(logging.WARNING)


@app.before_request
def log_request():
    logger.debug(">> %s %s", request.method, request.path)


@app.after_request
def log_response(response):
    logger.debug("<< %s %s -> %s", request.method, request.path, response.status_code)
    return response


API_KEY = config.get("api_key", "your-secret-key")
API_PORT = config.get("api_port", 5001)
API_HOST = config.get("api_host", "0.0.0.0")

# 长消息处理策略：truncate=截断为199字(默认) / split=拆分多条发送
# 注：希沃服务器对单条留言强制200字硬上限，超长返回 statusCode=40000
# 仅影响 api_server 路径；main.py 自有硬编码截断，不读此项
LONG_MSG_STRATEGY = config.get("long_message_strategy", "truncate")
MSG_MAX_LEN = 199

# 长消息拆分模式：按正则匹配点智能拆分，匹配到的字符跟到后段开头（不丢失）
# 默认 \r?\n —— 在 CRLF(Windows) 或 LF(Unix) 之前切割，换行符整体跟到下一段开头，
# 不会把 \r 与 \n 拆散到两段。空字符串 = 禁用智能拆分，回退到硬切 MSG_MAX_LEN 字符
# 仅影响 api_server 路径的 split 策略；main.py 不读此项
LONG_MSG_SPLIT_PATTERN = config.get("long_message_split_pattern", r"\r?\n")
_SPLIT_REGEX = None
if LONG_MSG_SPLIT_PATTERN:
    try:
        _SPLIT_REGEX = re.compile(LONG_MSG_SPLIT_PATTERN)
    except re.error as e:
        logger.warning(
            "long_message_split_pattern 编译失败，回退硬切: %s (pattern=%r)",
            e,
            LONG_MSG_SPLIT_PATTERN,
        )
        _SPLIT_REGEX = None


def _split_long_message(content: str, max_len: int = MSG_MAX_LEN) -> list:
    """按 _SPLIT_REGEX 智能拆分长消息

    - 在每段前 max_len 字符范围内，找最后一个正则匹配点，在匹配点之前切割；
      匹配到的字符（如换行符）跟到下一段开头，不丢失。
    - 无匹配点、或唯一匹配落在段首会导致空段时，回退到硬切 max_len 字符。
    - _SPLIT_REGEX 为 None（配置禁用或编译失败）时，全程硬切 max_len 字符。
    """
    chunks = []
    rest = content
    while len(rest) > max_len:
        cut = 0
        if _SPLIT_REGEX is not None:
            # 在前 max_len 字符范围内取最后一个 start>0 的匹配，避免空段
            for m in _SPLIT_REGEX.finditer(rest, 0, max_len):
                if m.start() > 0:
                    cut = m.start()
        if cut == 0:
            cut = max_len
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks


def require_api_key(f):
    """API密钥验证装饰器"""

    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if key != API_KEY:
            return jsonify({"error": "Unauthorized", "message": "Invalid API key"}), 401
        return f(*args, **kwargs)

    return decorated


# 全局会话对象
class Session:
    def __init__(self):
        self.account = None
        self.student = None
        self.stu_msg = None
        self._initialized = False

    def init(self):
        """初始化会话"""
        if not self._initialized:
            self.account = acc(auto_login=False)
            if self.account.token_expired:
                self._initialized = False
                return self
            self.student = stu(self.account)
            self.stu_msg = msg(self.account, self.student)
            self._initialized = True
        return self

    @property
    def needs_login(self):
        return self.account is not None and self.account.token_expired

    def refresh(self):
        """刷新会话"""
        self._initialized = False
        return self.init()


session = Session()

# 消息数据源层：独占 chat_history.json 读写 + 格式化 + 内存缓存
# __init__ 仅 _refresh（读文件 mtime，不碰 session），handler 调用时 session 已 init
datasource = MessageDataSource(session)

# 登录状态
_login_state = {
    "in_progress": False,
    "completed": False,
    "success": False,
}
_login_lock = threading.Lock()


def _check_session():
    """检查会话是否有效，无效则返回需要登录的响应"""
    session.init()
    if session.needs_login:
        return jsonify(
            {
                "status": "error",
                "message": "Token已过期，需要重新登录",
                "need_login": True,
            }
        ), 401
    return None


def upload_file_to_cloud(file_path: str, content_type: str = "image/png") -> str:
    """上传文件到云存储"""
    up = Upload(session.account)
    up.upload(file=file_path, type=content_type)
    return up.downloadUrl


# ============== 登录相关 API ==============


def _poll_login(cookies):
    """后台线程：轮询扫码状态"""
    status = 200
    data = None
    max_attempts = 150  # 5分钟超时 (150 * 2秒)
    attempt = 0
    while (status == 200 or status == 201) and attempt < max_attempts:
        try:
            data = check_qrcode(cookies)["data"]
            status = data["statusCode"]
        except Exception:
            break
        attempt += 1
        time.sleep(2)

    with _login_lock:
        if status == 202 and data:
            write_file(token_file, json.dumps(data).encode())
            _login_state["success"] = True
            session.refresh()
        _login_state["completed"] = True
        _login_state["in_progress"] = False


@app.route("/api/login/qrcode", methods=["GET"])
@require_api_key
def get_login_qrcode():
    """获取登录二维码（Base64编码图片），同时启动后台轮询"""
    with _login_lock:
        if _login_state["in_progress"]:
            return jsonify(
                {"status": "ok", "message": "登录流程进行中，请轮询 /api/login/status"}
            )

    try:
        cookies = download_qrcode()
        with _login_lock:
            _login_state.update(
                {"in_progress": True, "completed": False, "success": False}
            )

        # 读取二维码图片并转为 Base64
        with open(qrcode_file, "rb") as f:
            qr_base64 = base64.b64encode(f.read()).decode("utf-8")

        # 启动后台轮询
        thread = threading.Thread(target=_poll_login, args=(cookies,), daemon=True)
        thread.start()

        return jsonify({"status": "ok", "qrcode": qr_base64})
    except Exception as e:
        with _login_lock:
            _login_state["in_progress"] = False
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/login/status", methods=["GET"])
@require_api_key
def get_login_status():
    """查询登录状态"""
    with _login_lock:
        if not _login_state["in_progress"] and not _login_state["completed"]:
            return jsonify({"status": "idle", "message": "无登录流程"})
        if _login_state["completed"]:
            if _login_state["success"]:
                return jsonify({"status": "ok", "message": "登录成功"})
            else:
                return jsonify({"status": "error", "message": "登录失败或超时"})
    return jsonify({"status": "pending", "message": "等待扫码"})


# ============== 业务 API ==============


@app.route("/api/status", methods=["GET"])
@require_api_key
def get_status():
    """获取服务状态"""
    err = _check_session()
    if err:
        return err
    try:
        return jsonify(
            {
                "status": "ok",
                "student": {
                    "name": getattr(session.student, "name", "unknown"),
                    "schoolUid": getattr(session.student, "schoolUid", ""),
                    "classUid": getattr(session.student, "classUid", ""),
                    "userUid": getattr(session.student, "userUid", ""),
                },
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/messages", methods=["GET"])
@require_api_key
def get_messages():
    """获取消息列表

    Query params:
        count: 获取数量，默认10
    """
    err = _check_session()
    if err:
        return err
    try:
        count = int(request.args.get("count", 10))
        result = datasource.fetch_latest(count)
        logger.info("/api/messages count=%d", result["count"])
        if result["messages"]:
            logger.info(
                "  最早: id=%s, sender=%s, senderName=%s",
                result["messages"][0].get("id"),
                result["messages"][0].get("sender"),
                result["messages"][0].get("senderName"),
            )
            logger.info(
                "  最新: id=%s, sender=%s, senderName=%s",
                result["messages"][-1].get("id"),
                result["messages"][-1].get("sender"),
                result["messages"][-1].get("senderName"),
            )
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/send", methods=["POST"])
@require_api_key
def send_message():
    """发送文本消息

    JSON body:
        content: 消息内容
        strategy: 可选，长消息处理策略 "truncate"|"split"，缺省取全局配置
                  truncate=截断为199字(默认) / split=按199字拆分多条发送
                  split 模式下，拆分位置优先取 long_message_split_pattern 正则
                  匹配点（默认 \r?\n，兼容 CRLF 与 LF）之前，匹配字符整体
                  跟到下一段开头；无处可拆时回退到硬切 199 字
    """
    err = _check_session()
    if err:
        return err
    try:
        data = request.get_json()
        content = data.get("content", "")
        strategy = data.get("strategy") or LONG_MSG_STRATEGY

        if not content:
            return jsonify({"status": "error", "message": "content is required"}), 400

        # 短消息：直接发送
        if len(content) <= MSG_MAX_LEN:
            success = session.stu_msg.send(content, 1)
            return jsonify(
                {
                    "status": "ok" if success else "error",
                    "message": "发送成功" if success else "发送失败",
                }
            )

        # 长消息：按策略处理
        if strategy == "split":
            chunks = _split_long_message(content)
            results = []
            for chunk in chunks:
                results.append(bool(session.stu_msg.send(chunk, 1)))
            success = all(results)
            return jsonify(
                {
                    "status": "ok" if success else "error",
                    "strategy": "split",
                    "chunks": len(chunks),
                    "results": results,
                    "message": (
                        f"已拆分为 {len(chunks)} 条发送"
                        if success
                        else f"部分发送失败({sum(results)}/{len(chunks)})"
                    ),
                }
            )
        else:  # truncate（默认）
            content = content[: MSG_MAX_LEN - 3] + "..."
            success = session.stu_msg.send(content, 1)
            return jsonify(
                {
                    "status": "ok" if success else "error",
                    "strategy": "truncate",
                    "truncated": True,
                    "message": "发送成功" if success else "发送失败",
                }
            )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/send_image", methods=["POST"])
@require_api_key
def send_image():
    """发送图片

    JSON body:
        file_path: 图片文件路径
    或 multipart/form-data:
        file: 图片文件
    """
    err = _check_session()
    if err:
        return err
    try:
        # 方式1: JSON body 传文件路径
        if request.is_json:
            data = request.get_json()
            file_path = data.get("file_path")
            if not file_path or not os.path.exists(file_path):
                return jsonify({"status": "error", "message": "file_path invalid"}), 400
        # 方式2: 上传文件
        else:
            if "file" not in request.files:
                return jsonify({"status": "error", "message": "no file uploaded"}), 400
            file = request.files["file"]
            file_path = f"temp_{file.filename}"
            file.save(file_path)

        # 上传并发送
        url = upload_file_to_cloud(file_path, "image/png")
        if url:
            session.stu_msg.send("", 2, url)
            return jsonify({"status": "ok", "url": url})
        else:
            return jsonify({"status": "error", "message": "upload failed"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/send_audio", methods=["POST"])
@require_api_key
def send_audio():
    """发送音频

    JSON body:
        file_path: 音频文件路径
        voice_length: 音频时长(毫秒)，默认666
    """
    err = _check_session()
    if err:
        return err
    try:
        data = request.get_json()
        file_path = data.get("file_path")
        voice_length = data.get("voice_length", 666)

        if not file_path or not os.path.exists(file_path):
            return jsonify({"status": "error", "message": "file_path invalid"}), 400

        # 发送文件名
        session.stu_msg.send(os.path.basename(file_path), 1)

        # 上传并发送音频
        url = upload_file_to_cloud(file_path, "audio/mp3")
        if url:
            session.stu_msg.send("", 3, url, voice_length)
            return jsonify({"status": "ok", "url": url})
        else:
            return jsonify({"status": "error", "message": "upload failed"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/history", methods=["GET"])
@require_api_key
def get_history():
    """获取本地聊天记录

    Query params:
        limit: 返回条数，默认50
        offset: 偏移量，默认0
    """
    try:
        limit = int(request.args.get("limit", 50))
        offset = int(request.args.get("offset", 0))
        result = datasource.load_local(offset, limit)
        logger.info(
            "/api/history total=%d, count=%d",
            result["total"],
            result["count"],
        )
        if result["messages"]:
            logger.debug(
                "  首条: id=%s, sender=%s, senderName=%s, content=%.50s",
                result["messages"][0].get("id"),
                result["messages"][0].get("sender"),
                result["messages"][0].get("senderName"),
                result["messages"][0].get("content", ""),
            )
            logger.debug(
                "  末条: id=%s, sender=%s, senderName=%s, content=%.50s",
                result["messages"][-1].get("id"),
                result["messages"][-1].get("sender"),
                result["messages"][-1].get("senderName"),
                result["messages"][-1].get("content", ""),
            )
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/load_earlier", methods=["GET"])
@require_api_key
def load_earlier_messages():
    """加载更早的消息（滚动加载历史，纯本地读）

    Query params:
        count: 获取数量，默认50
        before_id: 客户端当前最早消息 id（游标），返回早于它的最新 count 条；
                   未传时默认 max(local)+1（返回本地最新的 count 条）
    """
    err = _check_session()
    if err:
        return err
    try:
        count = int(request.args.get("count", 50))
        # before_id 优先取客户端传入的游标；未传则用 max(local)+1 兜底
        datasource._refresh()
        local = datasource._messages
        before_id = int(request.args.get("before_id", 0))
        if before_id <= 0:
            before_id = (
                max(m.id for m in local) + 1 if local else 0
            )
        if before_id <= 0 or not local:
            return jsonify(
                {
                    "status": "ok",
                    "message": "暂无消息",
                    "has_more": False,
                    "count": 0,
                    "messages": [],
                }
            )
        result = datasource.load_earlier_from_local(before_id, count)
        logger.info(
            "/api/load_earlier before_id=%s, count=%d, has_more=%s",
            before_id,
            result["count"],
            result["has_more"],
        )
        if result["messages"]:
            logger.info(
                "  首条: id=%s, sender=%s, senderName=%s",
                result["messages"][0].get("id"),
                result["messages"][0].get("sender"),
                result["messages"][0].get("senderName"),
            )
            logger.info(
                "  末条: id=%s, sender=%s, senderName=%s",
                result["messages"][-1].get("id"),
                result["messages"][-1].get("sender"),
                result["messages"][-1].get("senderName"),
            )
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/sync_all", methods=["POST"])
@require_api_key
def sync_all_messages():
    """全量同步所有历史消息

    JSON body:
        batch_size: 每次获取数量，默认50
        delay: 每次请求间隔(秒)，默认2.0（防风控）
    """
    err = _check_session()
    if err:
        return err
    try:
        data = request.get_json() or {}
        batch_size = data.get("batch_size", 50)
        delay = data.get("delay", 2.0)
        result = datasource.sync_all(batch_size, delay)
        logger.info(
            "/api/sync_all synced_count=%d, total_count=%d",
            result["synced_count"],
            result["total_count"],
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/refresh", methods=["POST"])
@require_api_key
def refresh_session():
    """刷新会话（重新登录）"""
    try:
        session.refresh()
        return jsonify({"status": "ok", "message": "会话已刷新"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/execute", methods=["POST"])
@require_api_key
def execute_command():
    """执行命令（慎用）

    JSON body:
        command: 命令内容
    """
    err = _check_session()
    if err:
        return err
    try:
        data = request.get_json()
        command = data.get("command", "")

        if not command:
            return jsonify({"status": "error", "message": "command is required"}), 400

        # 安全限制：只允许特定命令
        allowed_prefixes = ["getpass", "发送音乐"]
        if not any(command.startswith(p) for p in allowed_prefixes):
            return jsonify({"status": "error", "message": "command not allowed"}), 403

        result = os.popen(command).read()
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    print("=" * 50)
    print("希沃班牌机器人 API 服务")
    print("=" * 50)
    print(f"API Key: {API_KEY}")
    print(f"端口: {API_PORT}")
    print(f"主机: {API_HOST}")
    if config.get("use_mock"):
        print(f"[MOCK] 已启用 -> localhost:{config.get('mock_port', 9000)}")
    else:
        print("[MOCK] 未启用，连接真实服务器")
    print("=" * 50)

    app.run(host=API_HOST, port=API_PORT, debug=False)
