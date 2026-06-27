# -*- coding: utf-8 -*-
"""
Mock Seewo Server - 调试用模拟希沃服务器

实现一个简单的 IM 平台，模拟希沃云班的所有接口，
特别是亲情留言相关功能，用于本地开发调试。

用法:
    python mock_server.py [--port 9000] [--load]

启动后修改 config.json 添加:
    "use_mock": true,
    "mock_port": 9000

管理接口:
    POST /mock/add_message   手动添加消息（模拟学生发消息）
    GET  /mock/data          查看所有数据
    POST /mock/reset         重置为默认数据
    POST /mock/save          持久化数据到文件
    POST /mock/load          从文件加载数据
"""

import argparse
import base64
import json
import os
import time
import uuid
from datetime import datetime

from flask import Flask, request, jsonify, Response

app = Flask(__name__)

MOCK_PORT = 9000
DATA_FILE = "mock_data.json"


# ============ 工具函数 ============


def pxencode(data):
    """编码为 pxSafeData 格式"""
    encoded = base64.b64encode(
        json.dumps(data, ensure_ascii=False).encode("utf-8")
    ).decode("utf-8")
    return {"pxSafeData": f"scData:{encoded}"}


def pxdecode_str(data_str):
    """解码 pxSafeData 字符串"""
    return base64.b64decode(data_str[7:])


def make_px_response(result):
    """构建 px 编码的响应（用于 m-campus API）"""
    encoded = base64.b64encode(
        json.dumps(result, ensure_ascii=False).encode("utf-8")
    ).decode("utf-8")
    return {"data": f"scData:{encoded}"}


def decode_params(body):
    """从请求体中解码参数（自动处理 pxSafeData）"""
    params = body.get("params", {})
    if "pxSafeData" in params:
        return json.loads(pxdecode_str(params["pxSafeData"]))
    return params


# ============ 数据模型 ============


class MockData:
    """模拟数据存储"""

    def __init__(self):
        self.users = {}       # uid -> {userId, token, name, type}
        self.students = {}    # uid -> {schoolUid, classUid, userUid, realName, name, uid, sid}
        self.classes = {}     # classUid -> {uid, name, roomUid, schoolUid}
        self.messages = []    # 留言列表
        self.uploads = {}     # fileId -> {downloadUrl, filename, uploadTime}
        self.events = []      # 考勤事件
        self._next_msg_id = 1000
        self._init_defaults()

    def _init_defaults(self):
        """初始化默认测试数据"""
        parent_uid = "mock_parent_001"
        self.users[parent_uid] = {
            "userId": parent_uid,
            "token": "mock_token_parent_001",
            "name": "测试家长",
            "type": "parent",
        }

        student_uid = "mock_student_001"
        school_uid = "mock_school_001"
        class_uid = "mock_class_001"
        self.students[student_uid] = {
            "schoolUid": school_uid,
            "classUid": class_uid,
            "userUid": student_uid,
            "realName": "测试学生",
            "name": "测试学生",
            "uid": student_uid,
            "sid": "S001",
        }

        self.classes[class_uid] = {
            "uid": class_uid,
            "name": "测试班级",
            "roomUid": "mock_room_001",
            "schoolUid": school_uid,
        }

        # 预置几条消息
        now = int(time.time() * 1000)
        self._add_message(
            school_uid, class_uid, student_uid, parent_uid,
            "student", 1, "爸爸/妈妈，我今天在学校很开心！", now - 3600000,
        )
        self._add_message(
            school_uid, class_uid, parent_uid, student_uid,
            "parent", 1, "宝贝加油！放学我来接你", now - 1800000,
        )
        self._add_message(
            school_uid, class_uid, student_uid, parent_uid,
            "student", 1, "好的！", now - 900000,
        )

        # 考勤事件
        self.events = [
            {
                "eventId": "event_morning",
                "eventName": "早上签到",
                "startTime": "06:00",
                "endTime": "08:30",
                "roomUid": "mock_room_001",
                "config": json.dumps({
                    "banPaiConfig": {"topStartTime": "06:00", "topEndTime": "08:30"}
                }),
            },
            {
                "eventId": "event_afternoon",
                "eventName": "下午签到",
                "startTime": "13:00",
                "endTime": "14:30",
                "roomUid": "mock_room_001",
                "config": json.dumps({
                    "banPaiConfig": {"topStartTime": "13:00", "topEndTime": "14:30"}
                }),
            },
        ]

    def _get_sender_name(self, uid, sender_type):
        if sender_type == "parent" and uid in self.users:
            return self.users[uid].get("name", "家长")
        if uid in self.students:
            return self.students[uid].get("realName", "学生")
        return "未知"

    def clear_messages(self):
        """清空所有消息"""
        self.messages = []

    def adopt_parent(self, uid, name="家长"):
        """动态适配：如果 UID 未知，自动注册并接管 mock_parent_001 的身份和消息"""
        if uid in self.users:
            return
        old_uid = "mock_parent_001"
        print(f"[ADOPT] 新 UID {uid}，接管 {old_uid} 的身份和消息")
        self.users[uid] = {"userId": uid, "token": "mock_token", "name": name, "type": "parent"}
        for m in self.messages:
            if m["senderUid"] == old_uid:
                m["senderUid"] = uid
            if m["receiverUid"] == old_uid:
                m["receiverUid"] = uid
        self.users.pop(old_uid, None)

    def _add_message(self, schoolUid, classUid, senderUid, receiverUid,
                     senderType, msgType, content, createTime=None,
                     resUrl="", voiceLength=0):
        self._next_msg_id += 1
        msg = {
            "id": self._next_msg_id,
            "schoolUid": schoolUid,
            "classUid": classUid,
            "senderUid": senderUid,
            "receiverUid": receiverUid,
            "senderType": senderType,
            "senderName": self._get_sender_name(senderUid, senderType),
            "type": msgType,
            "content": content,
            "resUrl": resUrl,
            "voiceLength": voiceLength,
            "createTime": createTime or int(time.time() * 1000),
            "isIllegal": 0,
        }
        self.messages.append(msg)
        return msg

    def get_messages(self, parent_uid, child_uid, page=1, page_size=10):
        """获取两人之间的消息，按 ID 倒序（新→旧）"""
        msgs = [
            m for m in self.messages
            if (m["senderUid"] == parent_uid and m["receiverUid"] == child_uid)
            or (m["senderUid"] == child_uid and m["receiverUid"] == parent_uid)
        ]
        msgs.sort(key=lambda m: m["id"], reverse=True)
        start = (page - 1) * page_size
        return msgs[start:start + page_size]

    def save(self):
        data = {
            "users": self.users,
            "students": self.students,
            "classes": self.classes,
            "messages": self.messages,
            "events": self.events,
            "_next_msg_id": self._next_msg_id,
        }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load(self):
        if not os.path.exists(DATA_FILE):
            return
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.users = data.get("users", self.users)
        self.students = data.get("students", self.students)
        self.classes = data.get("classes", self.classes)
        self.messages = data.get("messages", self.messages)
        self.events = data.get("events", self.events)
        self._next_msg_id = data.get("_next_msg_id", self._next_msg_id)


mock_data = MockData()


# ============ 登录相关 API (id.seewo.com) ============


@app.route("/auth/loginApi", methods=["GET"])
def login_api():
    """获取初始 Cookie"""
    resp = jsonify({"status": "ok"})
    resp.set_cookie("JSESSIONID", "mock_session_" + uuid.uuid4().hex[:8])
    return resp


@app.route("/scan/qrcode", methods=["GET"])
def scan_qrcode():
    """下载二维码图片（返回 1x1 最小 PNG）"""
    png_data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVQI12NgAAIABQABNjN9GQAAAABJRU5ErkJggg=="
    )
    resp = Response(png_data, mimetype="image/png")
    resp.set_cookie("qrcode_cookie", "mock_qr_" + uuid.uuid4().hex[:8])
    return resp


@app.route("/scan/pcCheckQrcode", methods=["GET"])
def check_qrcode():
    """查询扫码状态 - 自动确认登录，立即返回成功"""
    # 返回当前已知的家长用户（可能已被 adopt 过）
    uid = list(mock_data.users.keys())[0] if mock_data.users else "mock_parent_001"
    user = mock_data.users.get(uid, {})
    data = {
        "statusCode": 202,
        "message": "登录成功",
        "userId": user.get("userId", uid),
        "token": user.get("token", "mock_token_parent_001"),
    }
    return jsonify({"data": data})


# ============ 用户状态 / 消息摘要 (campus.seewo.com) ============


@app.route(
    "/soul-bootstrap/seewo-phoenix-blood-server/mobile/user/v1/<uid>/functionality",
    methods=["GET"],
)
def check_user_status(uid):
    """验证 Token 有效性 - 始终返回有效"""
    return jsonify({"statusCode": 200, "message": "ok"})


@app.route("/home-school-service/mobile/kidnote/v1/note/dialogs", methods=["GET"])
def get_last_msg():
    """获取最近消息摘要"""
    user_uid = request.args.get("userUid", "")
    msgs = [
        m for m in mock_data.messages
        if m["senderUid"] == user_uid or m["receiverUid"] == user_uid
    ]
    if not msgs:
        return jsonify({"data": None})
    latest = max(msgs, key=lambda m: m["id"])
    dialog = {
        "lastMsgTips": latest["content"],
        "childUid": latest["receiverUid"] if latest["senderUid"] == user_uid else latest["senderUid"],
    }
    return jsonify({"data": [dialog]})


# ============ m-campus 统一 API (m-campus.seewo.com) ============


@app.route("/class/apis.json", methods=["POST", "GET"])
def mcampus_api():
    """m-campus 统一 API 网关"""
    action = request.args.get("action", "")
    body = request.get_json(silent=True) or {}
    params = decode_params(body)

    handler = ACTION_HANDLERS.get(action)
    if handler:
        result = handler(params)
    else:
        result = {"statusCode": -500, "message": f"Unknown action: {action}"}
        print(f"[WARN] 未知 action: {action}")

    return jsonify(result)


# ---- Action Handlers ----


def handle_get_student_list(params):
    """获取家长关联的学生列表（忽略 parentId，始终返回所有学生）"""
    return make_px_response(list(mock_data.students.values()))


def handle_search_students(params):
    """按姓名搜索学生"""
    name = params.get("name", "")
    results = [s for s in mock_data.students.values() if name in s.get("realName", "")]
    return make_px_response(results)


def handle_get_notes(params):
    """获取亲情留言列表"""
    parent_uid = params.get("parentUid", "")
    child_uid = params.get("childUid", "")
    mock_data.adopt_parent(parent_uid)
    page = params.get("page", 1)
    page_size = params.get("pageSize", 10)
    print(f"[NOTES] parentUid={parent_uid}, childUid={child_uid}, 总消息数={len(mock_data.messages)}")
    msgs = mock_data.get_messages(parent_uid, child_uid, page, page_size)
    print(f"[NOTES] 匹配到 {len(msgs)} 条")
    return make_px_response({"statusCode": 200, "result": msgs})


def handle_post_note(params):
    """发送留言"""
    sender_uid = params.get("senderUid", "")
    mock_data.adopt_parent(sender_uid)
    msg = mock_data._add_message(
        params.get("schoolUid", ""),
        params.get("classUid", ""),
        params.get("senderUid", ""),
        params.get("receiverUid", ""),
        params.get("senderType", "parent"),
        params.get("type", 1),
        params.get("content", ""),
        resUrl=params.get("resUrl", ""),
        voiceLength=params.get("voiceLength", 0),
    )
    print(f"[MSG] {msg['senderType']}({msg['senderUid']}) -> {msg['receiverUid']}: {msg['content'][:50]}")
    return {"statusCode": 200, "message": "发送成功", "data": {"id": msg["id"]}}


def handle_delete_note(params):
    """删除留言"""
    ids = params.get("ids", [])
    mock_data.messages = [m for m in mock_data.messages if m["id"] not in ids]
    return {"statusCode": 200, "message": "删除成功"}


def handle_upload_policy(params):
    """获取文件上传策略"""
    mock_port = app.config.get("MOCK_PORT", MOCK_PORT)
    return {
        "statusCode": 200,
        "data": {
            "policyList": [{
                "uploadUrl": f"http://localhost:{mock_port}/upload/cos",
                "expireSeconds": 3600,
                "formFields": [
                    {"value": f"mock_key_{uuid.uuid4().hex[:8]}"},
                    {"value": f"mock_policy_{uuid.uuid4().hex[:8]}"},
                    {"value": f"mock_sig_{uuid.uuid4().hex[:8]}"},
                    {"value": f"mock_key_time_{uuid.uuid4().hex[:8]}"},
                    {"value": f"mock_ak_{uuid.uuid4().hex[:8]}"},
                    {"value": "sha1"},
                    {"value": f"mock_callback_{uuid.uuid4().hex[:8]}"},
                    {"value": "200"},
                    {"value": "10388"},
                    {"value": f"mock_sid_{uuid.uuid4().hex[:8]}"},
                    {"value": f"mock_bid_{uuid.uuid4().hex[:8]}"},
                ],
            }],
        },
    }


def handle_offline_verify(params):
    """获取离线验证码"""
    return {
        "statusCode": 200,
        "data": {
            "code": "MOCK_" + uuid.uuid4().hex[:8].upper(),
            "schoolUid": params.get("schoolUid", ""),
            "snCode": params.get("snCode", ""),
        },
    }


ACTION_HANDLERS = {
    "GET_STUDENT_V1_PARENT_BYPARENTID_CHILDREN_LIST": handle_get_student_list,
    "POST_STUDENT_V1_BYSCHOOLUID_CLASS_BYCLASSUID_STUDENTS": handle_search_students,
    "GET_KIDNOTE_V1_BYPARENTUID_BYCHILDUID_NOTES": handle_get_notes,
    "POST_KIDNOTE_V1_NOTE": handle_post_note,
    "DELETE_KIDNOTE_V1_NOTE": handle_delete_note,
    "POST_MOBILE_V1_RESOURCE_CSTORE_UPLOADPOLICY": handle_upload_policy,
    "GET_AUTHORIZATION_V1_USER_OFFLINE_VERIFY": handle_offline_verify,
}


# ============ 云班 API (campus.seewo.com/mis-cloud-route-server) ============


@app.route(
    "/mis-cloud-route-server/api/classmember/v1/school/<school_uid>/classes",
    methods=["GET"],
)
def get_class_list(school_uid):
    """获取班级列表"""
    return jsonify({"data": list(mock_data.classes.values())})


@app.route(
    "/mis-cloud-route-server/api/kidnote/v4/parent/<parent_uid>/child/<child_uid>/notes",
    methods=["GET"],
)
def get_yunban_notes(parent_uid, child_uid):
    """获取云班留言"""
    start = int(request.args.get("start", 1))
    page_size = int(request.args.get("pageSize", 10))
    msgs = mock_data.get_messages(parent_uid, child_uid, start, page_size)
    return jsonify({"data": msgs})


@app.route(
    "/mis-cloud-route-server/api/kidnote/v1/<uid>/parent/note/count",
    methods=["GET"],
)
def get_parent_note_count(uid):
    """获取家长留言计数"""
    count = len([
        m for m in mock_data.messages
        if m["receiverUid"] == uid or m["senderUid"] == uid
    ])
    return jsonify({"data": {"count": count}})


@app.route(
    "/mis-cloud-route-server/api/classmember/v1/school/<school_uid>/students",
    methods=["GET"],
)
def get_class_students(school_uid):
    """获取班级学生列表"""
    class_uids = request.args.get("classUids", "")
    students = list(mock_data.students.values())
    return jsonify({"data": [{"uid": class_uids, "students": students}]})


@app.route(
    "/mis-cloud-route-server/api/attendance/v3/<school_uid>/events",
    methods=["GET"],
)
def get_attendance_events(school_uid):
    """获取考勤事件"""
    return jsonify({"data": mock_data.events})


@app.route(
    "/mis-cloud-route-server/api/attendance/v1/<school_uid>/data",
    methods=["POST"],
)
def submit_attendance(school_uid):
    """提交考勤数据"""
    data = request.get_json(silent=True) or {}
    print(f"[ATTEND] 考勤: {json.dumps(data, ensure_ascii=False)[:200]}")
    return jsonify({"data": {"code": 0, "message": "签到成功"}})


@app.route("/mis-cloud-route-server/api/kidnote/v1/note", methods=["POST"])
def yunban_send_note():
    """云班直接发送留言"""
    data = request.get_json(silent=True) or {}
    msg = mock_data._add_message(
        data.get("schoolUid", ""),
        data.get("classUid", ""),
        data.get("senderUid", ""),
        data.get("receiverUid", ""),
        data.get("senderType", "student"),
        data.get("type", 1),
        data.get("content", ""),
        resUrl=data.get("resUrl", ""),
        voiceLength=data.get("voiceLength", 0),
    )
    print(f"[MSG-YUNBAN] {msg['senderType']} -> {msg['receiverUid']}: {msg['content'][:50]}")
    return jsonify({"statusCode": 200, "data": {"id": msg["id"]}})


# ============ 文件上传 (模拟 COS) ============


@app.route("/upload/cos", methods=["POST"])
def upload_to_cos():
    """模拟 COS 文件上传"""
    file = request.files.get("file")
    if not file:
        return jsonify({"code": -1, "message": "no file"}), 400

    filename = file.filename or "unknown"
    file_id = uuid.uuid4().hex[:12]
    mock_port = app.config.get("MOCK_PORT", MOCK_PORT)
    download_url = f"http://localhost:{mock_port}/upload/files/{file_id}/{filename}"

    mock_data.uploads[file_id] = {
        "downloadUrl": download_url,
        "filename": filename,
        "uploadTime": datetime.now().isoformat(),
    }
    print(f"[UPLOAD] {filename} -> {download_url}")
    return jsonify({
        "code": 0,
        "data": {"downloadUrl": download_url, "fileId": file_id, "filename": filename},
    })


@app.route("/upload/files/<file_id>/<filename>", methods=["GET"])
def download_uploaded_file(file_id, filename):
    """下载上传的文件（返回占位内容）"""
    return Response(b"mock file content", mimetype="application/octet-stream")


# ============ 管理接口 ============


@app.route("/mock/add_message", methods=["POST"])
def mock_add_message():
    """手动添加消息（模拟学生/其他人发消息）

    JSON body:
        senderUid:   发送者 UID，默认 mock_student_001
        receiverUid: 接收者 UID，默认 mock_parent_001
        content:     消息内容
        type:        消息类型，默认 1（文本）
        senderType:  发送者类型，默认 student
    """
    data = request.get_json(silent=True) or {}
    sender_uid = data.get("senderUid", "mock_student_001")
    # 默认接收者用当前家长 UID（可能已被 adopt 过）
    default_parent = list(mock_data.users.keys())[0] if mock_data.users else "mock_parent_001"
    sender_info = mock_data.students.get(sender_uid, {})
    msg = mock_data._add_message(
        sender_info.get("schoolUid", "mock_school_001"),
        sender_info.get("classUid", "mock_class_001"),
        sender_uid,
        data.get("receiverUid", default_parent),
        data.get("senderType", "student"),
        data.get("type", 1),
        data.get("content", "测试消息"),
    )
    return jsonify({"status": "ok", "message": msg})


@app.route("/mock/data", methods=["GET"])
def mock_get_data():
    """查看所有 mock 数据"""
    return jsonify({
        "users": mock_data.users,
        "students": mock_data.students,
        "classes": mock_data.classes,
        "messages_count": len(mock_data.messages),
        "messages": mock_data.messages,
        "events": mock_data.events,
        "uploads": mock_data.uploads,
    })


@app.route("/mock/reset", methods=["POST"])
def mock_reset():
    """重置为默认数据"""
    global mock_data
    mock_data = MockData()
    return jsonify({"status": "ok", "message": "数据已重置"})


@app.route("/mock/clear_messages", methods=["POST"])
def mock_clear_messages():
    """清空所有消息（保留用户/学生配置）"""
    mock_data.clear_messages()
    return jsonify({"status": "ok", "message": "消息已清空"})


@app.route("/mock/save", methods=["POST"])
def mock_save():
    """持久化数据到文件"""
    mock_data.save()
    return jsonify({"status": "ok", "message": f"数据已保存到 {DATA_FILE}"})


@app.route("/mock/load", methods=["POST"])
def mock_load():
    """从文件加载数据"""
    mock_data.load()
    return jsonify({"status": "ok", "message": f"数据已从 {DATA_FILE} 加载"})


# ============ 启动 ============


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock Seewo Server - 调试用模拟希沃服务器")
    parser.add_argument("--port", type=int, default=MOCK_PORT, help="服务端口 (默认 9000)")
    parser.add_argument("--load", action="store_true", help="从文件加载已有数据")
    args = parser.parse_args()

    app.config["MOCK_PORT"] = args.port

    if args.load:
        mock_data.load()

    print("=" * 55)
    print("  Mock Seewo Server - 调试用模拟希沃服务器")
    print("=" * 55)
    print(f"  端口:     {args.port}")
    print(f"  初始家长:  mock_parent_001")
    print(f"  学生 UID:  mock_student_001 (测试学生)")
    print(f"  (遇到新 UID 时自动适配，无需手动配置)")
    print()
    print("  管理接口:")
    print(f"    POST /mock/add_message       添加消息")
    print(f"    POST /mock/clear_messages    清空消息")
    print(f"    GET  /mock/data              查看数据")
    print(f"    POST /mock/reset             重置数据")
    print(f"    POST /mock/save              保存到文件")
    print(f"    POST /mock/load              从文件加载")
    print()
    print("  使用方法:")
    print("    1. 启动本服务器")
    print('    2. config.json 添加 "use_mock": true, "mock_port": ' + str(args.port))
    print("    3. 正常运行 main.py / api_server.py 等")
    print("=" * 55)

    app.run(host="0.0.0.0", port=args.port, debug=False)
