"""
学生信息查询

通过 m-campus API 获取家长关联的学生列表、学校/班级 UID 等信息。
stu 对象是消息收发和云班功能的前置依赖。
"""

import json
from login import acc
from api import api
from funcs import pxdecode, pxencode


class stu:
    """学生信息对象

    初始化时自动加载家长关联的学生信息。

    Attributes:
        schoolUid: 学校 UID
        classUid: 班级 UID
        userUid: 学生用户 UID
        name: 学生姓名
    """

    def __init__(self, acc: acc, child_uid: str | None = None) -> None:
        """
        Args:
            acc: 已登录的账户对象
            child_uid: 目标学生的 userUid。不传时默认取关联列表的第一个学生；

        Raises:
            KeyError: child_uid 不在关联学生列表中
        """
        self.acc = acc
        students = self.info()
        if child_uid is None:
            info = students[0]
        else:
            matched = [s for s in students if s.get("userUid") == child_uid]
            if not matched:
                valid = [s.get("userUid") for s in students]
                raise KeyError(f"child_uid {child_uid!r} not in children list: {valid}")
            info = matched[0]
        self.schoolUid = info["schoolUid"]
        self.classUid = info["classUid"]
        self.userUid = info["userUid"]
        self.name = info.get("realName", "学生")

    def info(self) -> dict:
        """获取家长关联的所有学生列表

        Returns:
            学生信息列表，每项含 schoolUid/classUid/userUid/realName 等

        Raises:
            Exception: 未添加学生时抛出异常
        """
        data = {"parentId": self.acc.uid}
        result = json.loads(
            pxdecode(
                api().action(
                    "GET_STUDENT_V1_PARENT_BYPARENTID_CHILDREN_LIST",
                    pxencode(data),
                    self.acc,
                )
            )
        )
        if result == []:
            raise Exception("错误：未添加学生")
        return result

    def get_stu(self, name):
        """按姓名在班级中搜索学生

        Args:
            name: 学生姓名

        Returns:
            匹配的学生信息字典
        """
        data = {"schoolUid": self.schoolUid, "classUid": self.classUid, "name": name}
        return json.loads(
            pxdecode(
                api().action(
                    "POST_STUDENT_V1_BYSCHOOLUID_CLASS_BYCLASSUID_STUDENTS",
                    pxencode(data),
                    self.acc,
                )
            )
        )

    def add_stu(self, stu_uid):
        """添加学生暂未实现；不伪装成成功或静默忽略请求。"""
        raise NotImplementedError("adding students is not implemented")
