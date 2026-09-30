# -*- coding: utf-8 -*-

import json
import time
import random
import requests
from datetime import datetime, date, timedelta
from login import acc
from api import api
from init import config, verify
from models import (
    SendResult,
    YunbanClass,
    YunbanStudent,
    YunbanEvent,
    YunbanParent,
    YunbanNotesPage,
)


def _get_yunban_base():
    """获取云班 API 基础 URL（支持 mock 模式）"""
    if config.use_mock:
        return f"http://localhost:{config.mock_port}/mis-cloud-route-server"
    return "https://campus.seewo.com/mis-cloud-route-server"


def getpass(account: acc, schoolUid, snCode, time, classUid=""):
    data = {
        "schoolUid": schoolUid,
        "snCode": snCode,
        "version": "1",
        "timestamp": time,  # "1754063970000",
        "classUid": classUid,
    }
    return api().action("GET_AUTHORIZATION_V1_USER_OFFLINE_VERIFY", data, account)


def getpass2():
    import pyperclip

    account = acc()
    while True:
        url: str
        url = pyperclip.paste()
        if url.startswith("https://id.seewo.com/"):
            snCode = url.split("snCode%3D")[1].split("%26")[0]
            timestamp = url.split("timestamp%3D")[1].split("%26")[0]
            schoolUid = url.split("schoolUid%3D")[1].split("%26")[0]
            print(getpass(account, schoolUid, snCode, timestamp), end="\r")
            # print(snCode+' ' +timestamp)
            time.sleep(0.5)


class yunban:
    def __init__(self, token, schoolid) -> None:
        self.token = token
        self.schoolid = schoolid
        self.headers = {
            "User-Agent": "okhttp/4.8.0",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "Cookie": f"acw_tc={self.token}",
        }

    def getclasslist(self) -> list[YunbanClass]:
        """获取学校下所有班级列表。

        uid 为该班唯一标识，roomUid 绑定教室
        多个班可能共享同一 `roomUid`（含空串，表示未绑定实教室），
        """
        base = _get_yunban_base()
        url = f"{base}/api/classmember/v1/school/{self.schoolid}/classes"
        response = requests.request("GET", url, headers=self.headers, verify=verify)
        return [YunbanClass.from_dict(c) for c in response.json()["data"]]

    def getnotes(self, uid, parentuid, num, size=1) -> YunbanNotesPage:
        """获取某个孩子(child uid=uid)给指定家长(parentuid)的留言，分页。

        `num` 为页码，从 1 开始
        `size` 每页条数。`result` 为 `list[YunbanNote]` 消息列表，无数据时 `totalCount=0`。HTTP 200。
        """
        base = _get_yunban_base()
        url = f"{base}/api/kidnote/v4/parent/{parentuid}/child/{uid}/notes?start={num}&pageSize={size}"
        response = requests.request("GET", url, headers=self.headers, verify=verify)
        return YunbanNotesPage.from_dict(response.json()["data"])

    def getparents(self, uid) -> list[YunbanParent]:
        """获取某个孩子绑定的家长列表。"""
        base = _get_yunban_base()
        url = f"{base}/api/kidnote/v1/{uid}/parent/note/count"
        response = requests.request("GET", url, headers=self.headers, verify=verify)
        return [YunbanParent.from_dict(p) for p in response.json()["data"]]

    def getstulist(self, classid) -> list[YunbanStudent]:
        """获取某个班级的学生列表，返回 `list[YunbanStudent]`。

        学生字段含 name/sid/uid/性别/卡号列表等，gender: 0未知 1男 2女；
        注意某些字段并非每个学生都存在（如卡号/头像可能为空串）；
        """
        base = _get_yunban_base()
        url = f"{base}/api/classmember/v1/school/{self.schoolid}/students?classUids={classid}"
        response = requests.request("GET", url, headers=self.headers, verify=verify)
        return [
            YunbanStudent.from_dict(s) for s in response.json()["data"][0]["students"]
        ]

    def searchstubyname(
        self, stuname, students: list[YunbanStudent]
    ) -> YunbanStudent | None:
        """按学生姓名精确查找（线性扫描 students 列表），返回 `YunbanStudent`。

        找不到返回 `None`。
        """
        for stu in students:
            if stu.name == stuname:
                return stu
        return None

    def searchstubyuid(
        self, stuname, students: list[YunbanStudent]
    ) -> YunbanStudent | None:
        """按学生 uid 精确查找（线性扫描 students 列表），返回 `YunbanStudent`。

        找不到返回 `None`。
        """
        for stu in students:
            if stu.uid == stuname:
                return stu
        return None

    def getevents(self, roomUid) -> list[YunbanEvent]:
        """获取某个教室(roomUid)的考勤事件列表，返回 `list[YunbanEvent]`。

        走 `/api/attendance/v3/{schoolid}/events?roomUid={roomUid}`。
        事件字段含 name/eventId/memberType/起止时间/周期/绑班(classes)/班牌时段(config)等；
        **多班共享同一 roomUid 时结果相同；无事件返回 `[]`。**
        """
        base = _get_yunban_base()
        url = f"{base}/api/attendance/v3/{self.schoolid}/events?roomUid={roomUid}"
        response = requests.request("GET", url, headers=self.headers, verify=verify)
        return [YunbanEvent.from_dict(e) for e in response.json()["data"]]

    # Get time from events

    def geteventtime(self, event: YunbanEvent) -> tuple[str, str]:
        """解析考勤事件的班牌展示时段，返回 (开始, 结束) 字符串。

        从 `event.config`（JSON 字符串）取 `banPaiConfig.topStartTime` 与
        `topEndTime`，形如 ("06:40", "07:20")，代表班牌上允许显示考勤的时间窗，
        与实际考勤起止（startTime/endTime）不一定相同。
        """
        config = json.loads(
            event.config
        )  # '{"banPaiConfig": {"topEndTime": "07:20", "topStartTime": "06:16"}}'
        return config["banPaiConfig"]["topStartTime"], config["banPaiConfig"][
            "topEndTime"
        ]

    """
    Random generate attend data
            "attendanceDate": "2025-11-08", this decides the now date
        "attendanceTime": "17:56:05", this should be random between self.geteventtime(event)[0] and event["endTime"]
    """

    def random_time_in_range(self, start_str: str, end_str: str) -> str:
        """
        生成当天在指定时间范围内的随机时间（包含秒）。

        参数:
            start_str: 开始时间，格式 "HH:MM"（例如 "09:00"）
            end_str:   结束时间，格式 "HH:MM"（例如 "17:30"）

        返回:
            字符串，格式 "YYYY-MM-DD HH:MM:SS"，为当天在该范围内的随机时间。

        注意:
            - 假设 start_str <= end_str（不跨午夜）。
            - 如果结束时间早于或等于开始时间，会抛出 ValueError。
        """
        # 解析时间字符串
        start_time = datetime.strptime(start_str, "%H:%M").time()
        end_time = datetime.strptime(end_str, "%H:%M").time()

        # 获取当天日期
        today = date.today()

        # 组合成完整的 datetime 对象
        start_dt = datetime.combine(today, start_time)
        end_dt = datetime.combine(today, end_time)

        # 检查时间范围有效性
        if end_dt <= start_dt:
            raise ValueError("结束时间必须晚于开始时间（不支持跨天范围）")

        # 计算时间间隔的总秒数
        total_seconds = (end_dt - start_dt).total_seconds()

        # 生成随机秒数（浮点数，确保秒部分也能随机）
        random_seconds = random.uniform(0, total_seconds)

        # 添加随机偏移
        random_dt = start_dt + timedelta(seconds=random_seconds)

        # 格式化输出（秒会自动四舍五入到整数）
        return random_dt.strftime("%H:%M:%S")

    def randomtime(self, event: YunbanEvent) -> str:
        """生成签到用随机时间字符串 "HH:MM:SS"。

        在班牌时段起点（`geteventtime` 的 topStartTime）与事件 `endTime` 之间
        随机取一个时刻，用于自动签到时伪装真实的签到时间。
        """
        # datenow=time.strftime('%Y-%m-%d', time.localtime())
        start = self.geteventtime(event)[0]
        end = event.endTime
        return self.random_time_in_range(start, end)

    def attend(self, name, uid, sid, event: YunbanEvent, date, time, classUid, roomUid):
        payload = {
            "attendanceData": [
                {
                    "eventId": event.eventId,
                    "eventVersion": 1,
                    "attendanceType": 1,
                    "forwardEventType": 10,
                    "eventStartTime": self.geteventtime(event)[0],
                    "eventAttendTime": event.endTime,
                    "eventEndTime": self.geteventtime(event)[1],
                    "classUid": classUid,
                    "attendanceDate": date,
                    "attendanceTime": time,
                    "roomUid": roomUid,
                    "userUid": uid,
                    "userName": name,
                    "userSid": sid,
                }
            ]
        }
        print(payload)
        base = _get_yunban_base()
        url = f"{base}/api/attendance/v1/{self.schoolid}/data"
        response = requests.post(url, json=payload, headers=self.headers, verify=verify)
        return response.json()

    def send_msg(
        self,
        sender,
        receiver,
        content,
        type: int,
        resUrl="",
        voiceLength=0,
        resConfig="",
    ):
        base = _get_yunban_base()
        url = f"{base}/api/kidnote/v1/note"

        data = {
            "schoolUid": self.schoolid,
            "type": type,
            "senderUid": sender,
            "receiverUid": receiver,
            "content": "",
            "resConfig": "",
            "tips": "",
            "resUrl": "",
            #  "classUid": ,
            "isIllegal": 0,
            "senderType": "student",
        }
        match type:
            case 0:
                data["content"] = content
            case 1:
                data["content"] = content
            case 2:
                data["resUrl"] = resUrl
            case 3:
                data["voiceLength"] = voiceLength
                data["resUrl"] = resUrl
            case 4:
                data["resUrl"] = resUrl
            case 5:
                data["resUrl"] = resUrl
            case 6:
                data["resUrl"] = resUrl
                data["resConfig"] = resConfig
        # post=api().action("POST_KIDNOTE_V1_NOTE",data,self.acc)
        # code = post["statusCode"]

        response = requests.post(url, json=data, headers=self.headers, verify=verify)
        http_status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = {}
        # 业务码与 HTTP 码分开放：body 里有 statusCode 就用它，没有则只记传输层失败
        if isinstance(body, dict) and "statusCode" in body:
            result = SendResult.from_seewo_code(
                body["statusCode"], body.get("message", ""), http_status=http_status
            )
        else:
            result = SendResult.from_http_error(http_status, response.text)
        if result.ok:
            print("发送成功：" + content)
        else:
            print(
                f"发送失败：{result.message or response.text}"
                f"（希沃码 {result.seewo_code}，HTTP {result.http_status}）"
            )
        return result
