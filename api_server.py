# -*- coding: utf-8 -*-
"""
希沃班牌机器人 API 服务端
提供 REST API 接口供客户端调用
"""

import os
import json
import time
import base64
import logging
import threading
from flask import Flask, request, jsonify
from functools import wraps

os.chdir(os.path.dirname(__file__))

from init import qrcode_file, config  # noqa: E402
from login import acc, download_qrcode, check_qrcode  # noqa: E402
from funcs import write_file, load_chat_history, prepend_messages, update_earliest_id  # noqa: E402
from stu import stu  # noqa: E402
from msg import msg  # noqa: E402
from upload import Upload  # noqa: E402

app = Flask(__name__)

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("seewo.server")

# 静默 Flask/Werkzeug 默认日志
logging.getLogger("werkzeug").setLevel(logging.WARNING)


@app.before_request
def log_request():
    logger.info(">> %s %s", request.method, request.path)


@app.after_request
def log_response(response):
    logger.info("<< %s %s -> %s", request.method, request.path, response.status_code)
    return response

API_KEY = config.get("api_key", "your-secret-key")
API_PORT = config.get("api_port", 5000)
API_HOST = config.get("api_host", "0.0.0.0")


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
        return jsonify({"status": "error", "message": "Token已过期，需要重新登录", "need_login": True}), 401
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
            write_file("tokens.json", json.dumps(data).encode())
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
            return jsonify({"status": "ok", "message": "登录流程进行中，请轮询 /api/login/status"})

    try:
        cookies = download_qrcode()
        with _login_lock:
            _login_state.update({"in_progress": True, "completed": False, "success": False})

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
        result = session.stu_msg.get(count)
        raw_messages = result.get("result", [])

        # 格式化消息数据
        messages = []
        parent_uid = session.account.uid
        student_uid = session.student.userUid

        for msg in raw_messages:
            # 解析时间
            create_time = msg.get("createTime", 0)
            if create_time:
                from datetime import datetime

                time_str = datetime.fromtimestamp(create_time / 1000).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            else:
                time_str = ""

            # 判断发送者
            sender_uid = msg.get("senderUid", "")
            if sender_uid == parent_uid:
                sender = "parent"
                sender_name = "家长"
            elif sender_uid == student_uid:
                sender = "student"
                sender_name = session.student.name
            else:
                sender = "unknown"
                sender_name = msg.get("senderName", "未知")

            messages.append(
                {
                    "id": msg.get("id", 0),
                    "time": time_str,
                    "content": msg.get("content", ""),
                    "type": msg.get("type", 1),
                    "sender": sender,
                    "senderName": sender_name,
                    "resUrl": msg.get("resUrl", ""),
                }
            )

        # result 按时间倒序（新→旧），TUI 需要正序（旧→新）才能正确显示
        messages.reverse()
        print(f"[API /api/messages] count={len(messages)}")
        if messages:
            print(f"  最早: id={messages[0].get('id')}, sender={messages[0].get('sender')}, senderName={messages[0].get('senderName')}")
            print(f"  最新: id={messages[-1].get('id')}, sender={messages[-1].get('sender')}, senderName={messages[-1].get('senderName')}")
        return jsonify({"status": "ok", "count": len(messages), "messages": messages})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/send", methods=["POST"])
@require_api_key
def send_message():
    """发送文本消息

    JSON body:
        content: 消息内容
    """
    err = _check_session()
    if err:
        return err
    try:
        data = request.get_json()
        content = data.get("content", "")

        if not content:
            return jsonify({"status": "error", "message": "content is required"}), 400

        if len(content) > 199:
            content = content[:196] + "..."

        success = session.stu_msg.send(content, 1)
        return jsonify(
            {
                "status": "ok" if success else "error",
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

        history = load_chat_history()
        raw_messages = history.get("messages", [])

        # 分页
        total = len(raw_messages)
        raw_messages = raw_messages[offset : offset + limit]

        # 补充 senderName（本地记录可能没有）
        parent_uid = session.account.uid if session else ""
        student_uid = session.student.userUid if session else ""
        student_name = session.student.name if session else ""
        messages = []
        for m in raw_messages:
            msg = dict(m)
            sender = msg.get("sender", "unknown")
            if not msg.get("senderName"):
                if sender == "parent":
                    msg["senderName"] = "家长"
                elif sender == "student":
                    msg["senderName"] = student_name
                else:
                    msg["senderName"] = "未知"
            messages.append(msg)

        result = {
                "status": "ok",
                "total": total,
                "earliest_id": history.get("earliest_id", 0),
                "last_id": history.get("last_id", 0),
                "count": len(messages),
                "messages": messages,
            }
        print(f"[API /api/history] total={total}, earliest_id={result['earliest_id']}, last_id={result['last_id']}, count={len(messages)}")
        if messages:
            print(f"  首条: id={messages[0].get('id')}, sender={messages[0].get('sender')}, senderName={messages[0].get('senderName')}, content={str(messages[0].get('content',''))[:50]}")
            print(f"  末条: id={messages[-1].get('id')}, sender={messages[-1].get('sender')}, senderName={messages[-1].get('senderName')}, content={str(messages[-1].get('content',''))[:50]}")
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/load_earlier", methods=["GET"])
@require_api_key
def load_earlier_messages():
    """加载更早的消息（滚动加载历史）

    Query params:
        count: 获取数量，默认50
    """
    err = _check_session()
    if err:
        return err
    try:
        count = int(request.args.get("count", 50))

        history = load_chat_history()
        earliest_id = history.get("earliest_id", 0)
        existing_ids = {int(m.get("id", 0)) for m in history.get("messages", [])}

        # 如果没有 earliest_id，先获取当前消息作为起点，并保存到本地
        latest_msgs = []
        if earliest_id == 0:
            latest_msgs = session.stu_msg.get(count).get("result", [])
            if not latest_msgs:
                return jsonify(
                    {"status": "ok", "message": "暂无消息", "has_more": False, "count": 0, "messages": []}
                )
            earliest_id = min(int(m.get("id", 0)) for m in latest_msgs)

        # 获取更早的消息
        earlier_msgs = session.stu_msg.get_earlier_messages(earliest_id, count)

        # 合并，去重
        msgs_to_format = earlier_msgs + latest_msgs
        msgs_to_format = [m for m in msgs_to_format if int(m.get("id", 0)) not in existing_ids]

        # 如果本地没有记录且没有更早消息，把最新消息也返回
        if not msgs_to_format:
            return jsonify(
                {
                    "status": "ok",
                    "message": "已到达最早消息",
                    "has_more": False,
                    "count": 0,
                    "messages": [],
                }
            )

        # 格式化并保存到本地
        parent_uid = session.account.uid
        student_uid = session.student.userUid
        formatted_msgs = []

        for msg in msgs_to_format:
            # 解析时间
            create_time = msg.get("createTime", 0)
            if create_time:
                from datetime import datetime

                time_str = datetime.fromtimestamp(create_time / 1000).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            else:
                time_str = ""

            # 判断发送者
            sender_uid = msg.get("senderUid", "")
            if sender_uid == parent_uid:
                sender = "parent"
                sender_name = "家长"
            elif sender_uid == student_uid:
                sender = "student"
                sender_name = session.student.name
            else:
                sender = "unknown"
                sender_name = msg.get("senderName", "未知")

            formatted_msg = {
                "id": int(msg.get("id", 0)),
                "time": time_str,
                "content": msg.get("content", ""),
                "type": msg.get("type", 1),
                "sender": sender,
                "senderName": sender_name,
                "resUrl": msg.get("resUrl", ""),
            }
            formatted_msgs.append(formatted_msg)

        # 插入到本地历史开头
        prepend_messages(formatted_msgs)

        # 判断是否还有更早的消息
        has_more = len(earlier_msgs) >= count

        print(f"[API /api/load_earlier] earliest_id={earliest_id}, count={len(formatted_msgs)}, has_more={has_more}")
        if formatted_msgs:
            print(f"  首条: id={formatted_msgs[0].get('id')}, sender={formatted_msgs[0].get('sender')}, senderName={formatted_msgs[0].get('senderName')}")
            print(f"  末条: id={formatted_msgs[-1].get('id')}, sender={formatted_msgs[-1].get('sender')}, senderName={formatted_msgs[-1].get('senderName')}")

        return jsonify(
            {
                "status": "ok",
                "has_more": has_more,
                "count": len(formatted_msgs),
                "messages": formatted_msgs,
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/sync_all", methods=["POST"])
@require_api_key
def sync_all_messages():
    """全量同步所有历史消息（后台执行，耗时较长）

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

        history = load_chat_history()
        earliest_id = history.get("earliest_id", 0)
        existing_ids = {int(m.get("id", 0)) for m in history.get("messages", [])}

        # 始终获取最新消息
        latest_msgs = session.stu_msg.get(100).get("result", [])

        # 确定最早的已知消息ID
        if earliest_id == 0 and latest_msgs:
            earliest_id = min(int(m.get("id", 0)) for m in latest_msgs)

        # 获取所有历史消息（比 earliest_id 更早的）
        earlier_msgs = []
        if earliest_id > 0:
            earlier_msgs = session.stu_msg.get_all_messages_until_earliest(
                earliest_id, batch_size, delay
            )

        # 合并：更早的消息 + 最新消息，去重（排除本地已有的）
        all_msgs = earlier_msgs + latest_msgs
        all_msgs = [m for m in all_msgs if int(m.get("id", 0)) not in existing_ids]

        # 格式化并保存
        parent_uid = session.account.uid
        student_uid = session.student.userUid
        formatted_msgs = []

        for msg in all_msgs:
            create_time = msg.get("createTime", 0)
            if create_time:
                from datetime import datetime

                time_str = datetime.fromtimestamp(create_time / 1000).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            else:
                time_str = ""

            sender_uid = msg.get("senderUid", "")
            if sender_uid == parent_uid:
                sender = "parent"
                sender_name = "家长"
            elif sender_uid == student_uid:
                sender = "student"
                sender_name = session.student.name
            else:
                sender = "unknown"
                sender_name = msg.get("senderName", "未知")

            formatted_msg = {
                "id": int(msg.get("id", 0)),
                "time": time_str,
                "content": msg.get("content", ""),
                "type": msg.get("type", 1),
                "sender": sender,
                "senderName": sender_name,
                "resUrl": msg.get("resUrl", ""),
            }
            formatted_msgs.append(formatted_msg)

        # 插入到本地历史开头
        prepend_messages(formatted_msgs)

        # 更新 earliest_id
        if formatted_msgs:
            update_earliest_id(min(m["id"] for m in formatted_msgs))

        total_count = len(load_chat_history().get("messages", []))
        print(f"[API /api/sync_all] synced_count={len(formatted_msgs)}, total_count={total_count}")
        return jsonify(
            {
                "status": "ok",
                "message": "全量同步完成",
                "synced_count": len(formatted_msgs),
                "total_count": total_count,
            }
        )
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
